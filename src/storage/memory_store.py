# -*- coding: utf-8 -*-
"""凝练记忆存储（THINK-ECO-T12 · condense_fold）。

记忆链单位＝卡片对话（一卡一会话一记忆链，方案 §3.1）：键＝(work_id, card_id)，
落点 works/{work_id}/memory/{card_id}.json。memory 文件独立于卡片与会话、
互不写入（红线 RD-06/V3）；凝练不写卡片 front matter、不改写会话文件
（RD-01——卡片正文仍只能经 read_card 拉取，记忆条目 ≠ 卡片正文）。

水位 mark＝已凝练到的会话块序号（半开区间：mark 前已凝、对上下文不可见；
mark 后未凝）。块序号＝读时现算（session_store.read_session 解析循环暴露，
存储格式零改动）。

写锁沿用 session_store 锁家族（同款按 (work_id, card_id) 键控的
asyncio.Lock，单进程 uvicorn 足够；同键与会话写互不并发——凝练回合末写
memory 不与该卡会话追加交错）。写走 tmp→os.replace 原子替换，读免锁
（替换原子，读侧只见旧或新完整态）。

开关（RD-08）：环境变量 CONDENSE_ENABLED，缺省关；字面 "1"/"true" 开、
其余关（与 TOOL_FOLD_ENABLED 同款字面约定）。关＝装配逐字旧路（V6）。

触发线 K（SP-01/RD-07）：「何时凝」非「限制模型」，不是资源闸（RD-04）。
[2026-10-10 计量口径改 token] 用户 10-07 原话口径＝「触发 80 万 token」；
首版实现用自造的 2.1 B/token 系数把它折成字节 1,680,000（该系数无任何出处，
实测中文 B/token 为 cl100k 2.93／o200k 4.30／源侧估算口径 3.0），且该线
自上线起从未触发过。现按用户 10-10 令改为本地真 token 计数——tiktoken
cl100k_base 真编码，触发线＝800,000 token，不再做任何字节折算。
环境变量 CONDENSE_TRIGGER_TOKENS 覆盖；旧键 CONDENSE_TRIGGER_K（字节口径）
已废弃：检测到只告警、绝不读其值（防旧值 1680000 被当 168 万 token 静默读入）。
做不到的部分如实写明：本地任何计数都得不到 haiku-5.5 的真实分词数（该模型走
Nous 聚合端点，真实 BPE 未知），cl100k 给的是确定性近似，不是该模型的准确值。

命名（方案 §七 双折叠命名）：T11 工具轮折叠＝tool_fold（边界锁死
tool_fold.py:22-23，本模块零触碰）；凝练归拢＝condense_fold（落 memory、
物理分离）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import tiktoken

from . import memory_domain, session_facade, session_store

logger = logging.getLogger("storage.memory")

# [2026-10-10 计量口径改 token] 触发线＝800,000 token——用户 10-07 原话口径就是
# 「触发 80 万 token」；旧实现按自造系数 2.1 B/token 折成字节 1,680,000（该系数
# 无任何出处，实测中文 B/token 为 cl100k 2.93／o200k 4.30／源侧估算口径 3.0），
# 且该线自 10-07 上线起从未触发过。现改为本地 tiktoken cl100k_base 真编码计数。
DEFAULT_TRIGGER_TOKENS = 800_000

# [凝练折叠开启 2026-10-07 / 2026-10-10 改 token 口径] 源侧机制搬回：
# 单批窗口预算＝300,000 token（用户令：一次凝练 30 万 token；旧实现折成 630,000
# 字节系同一自造系数的产物）。★与触发线必须同尺——两者共用 _msg_meter_tokens，
# 只改一个会造成「触发按 80 万 token、一次只切 30 万字节」的口径分裂。
# 单 job 批上限（照抄源侧 dsh `condense.ts:2905` 的 **2026-09-09 用户裁定**）：
#   「maxBatchesOverride：自动触发（maybeCondense）传 1——一次触发只跑一批，
#    批后 mark 即停、未凝从窗口大小重新起算，等下一次自然触发；不再续派连跑。」
#   源侧常量 `CONDENSE_MAX_BATCHES_PER_JOB = 2` **只给 import-drive 全量导入**用。
# ★ 本项目此前缺省写成 20 并让自动触发吃缺省值 ⇒ 一次触发把触发线以上的积压
#   全部压完（实测 card_006：3 批把 80 万 token 全吃掉，mark 推到 1825/1915，
#   历史区只剩 90 块，模型近乎失忆）。**缺省必须＝1。**
DEFAULT_WINDOW_BUDGET_TOKENS = 300_000
DEFAULT_MAX_BATCHES = 1
FOLD_EVERY_BATCHES = 5

# [T15-任务 3] 凝练 prompt 骨架（诸葛定，用户可事后推翻）：纯文本产出、
# 不涉工具解析（call_main tools=None——统合官文本泄漏路径不触发）。
# [记忆因果链 2026-10-20] 凝练 prompt 升级：照抄 dsh `condense/prompts.ts` 的
# **S2 gist 写作契约（史书标准）** ＋ 节点化 JSON 产出（`CausalNode` 的 anchor/gist/
# kind/coveredSeqs/process/lesson/start/result）。旧【结论】/【依据】格式**保留兼容**
# （解析器 JSON 优先、标记回退），存量 fake 与老模型输出都不会因此失败。
#
# dsh 原契约要点（逐条保留）：
#   · 读者模型＝**没有任何上下文的未来读者**（不知编号/章节/缩写，只读这一条就要懂）
#   · 谁-因-事-果：谁（有意义的施事主语）→ 因（触发原因/具体诉求）→ 事（外人能懂的动词）→ 果（终态）
#   · 编号/章节引用：首现即人话括注，或下沉到 process；不得裸留
#   · 保真红线优先：原文没有的「因」不得补——宁可缺因，不虚构
CONDENSE_SYSTEM_PROMPT = (
    "你是记忆凝练器。把给定的对话段压成**因果链节点**，只输出纯 JSON，不要任何解释文字、"
    "不要代码块包裹。\n"
    "\n"
    "# gist 写作契约（史书标准）\n"
    "★ 读者模型：每条 gist 写给**没有任何上下文的未来读者**（几个月后的模型/换过的模型/人）。"
    "他不知道编号指什么、章节是哪章、缩写是什么。他只读你这一条就要判断“历史上发生了什么”"
    "——每条默认承受“去掉所有上下文后读者仍懂”的检验。\n"
    "★ 谁-因-事-果：每条 gist 按「谁 → 因 → 事 → 果」组织：\n"
    "- 谁＝有意义的施事主语（用户要求／实施者完成／某卡决定）；纯编号自指不算主语；\n"
    "- 因＝触发原因或具体诉求——**情绪事件必须记因**（“用户怒斥”要写明怒的本质诉求）；\n"
    "- 事＝外人能理解的自然语言动词；禁止仅“完成/处理 + 编号”的流水句；\n"
    "- 果＝终态（落地了／被拒／通过）。\n"
    "★ 编号/章节引用：翻译成人话并首现括注，或整体下沉到 process；不得裸留外层。\n"
    "★ 保真红线优先：原文没有的「因」不得补——宁可缺因，不虚构。\n"
    "\n"
    "# 输出格式（纯 JSON）\n"
    '{"nodes": [{"anchor": {"seq": <该节点起点块号>}, "gist": "<一句话，按上面的契约>", '
    '"kind": "fact|decision|todo|exclusion|assumption", '
    '"coveredSeqs": [<本节点覆盖的块号，连续区间>, ...], '
    '"start": "<起因，可空>", "result": "<结果，可空>", '
    '"process": "<过程记述，可空>", "lesson": "<教训，可空>"}]}\n'
    "块号只能取给定原文里出现过的 [块N]；拿不准就拆细，不要猜。"
)

# 【结论】/【依据】标记与块号引用的机械解析（旧格式兼容）
_CONCLUSION_MARK = "【结论】"
_EVIDENCE_MARK = "【依据】"
_BLOCK_REF_RE = re.compile(r"\[?块(\d+)\]?")


def memory_path(work_id: str, card_id: str) -> Path:
    """记忆文件路径：works/{work_id}/memory/{card_id}.json（RD-06 独立文件）。

    WORKS_DIR 按调用时从 session_store 解引用——沙箱/测试重定向自动跟随。
    """
    return session_store.WORKS_DIR / work_id / "memory" / f"{card_id}.json"


def _empty_state(card_id: str) -> dict[str, Any]:
    return {"card_id": card_id, "mark": 0, "entries": []}


def condense_enabled() -> bool:
    """RD-08：缺省关；字面 "1"/"true" 开（每次调用读环境，改开关即生效）。"""
    return os.environ.get("CONDENSE_ENABLED", "").strip().lower() in ("1", "true")


# [2026-10-10] 旧字节口径键的废弃告警（进程内只告警一次；只告警、绝不读值）
_DEPRECATED_KEY_WARNED: set[str] = set()


def _warn_deprecated_once(key: str, new_key: str) -> None:
    """旧键已废弃：存在则告警一次，**绝不读它的值**。

    [2026-10-10] 防「静默错读」：若沿用旧键名而 .env 残留 CONDENSE_TRIGGER_K=1680000，
    代码会把 168 万当 token 读入（比 800,000 高一倍多，永不触发且不报任何错）。
    做法照抄源侧 condense.ts:2361 逐字「已 deprecated……该键不再参与任何判定」。
    """
    if key in _DEPRECATED_KEY_WARNED:
        return
    if not os.environ.get(key, "").strip():
        return
    _DEPRECATED_KEY_WARNED.add(key)
    logger.warning(
        "[condense_fold] CONFIG-WARN: %r 已 deprecated（计量口径由字节改 token）"
        "——该键不再参与任何判定；请改用 %r。", key, new_key)


def trigger_k_tokens() -> int:
    """触发线（token）：CONDENSE_TRIGGER_TOKENS 覆盖优先；旧键 CONDENSE_TRIGGER_K 已废弃。

    [2026-10-23 用户口径] 触发 **10 万**、单批 **6 万**、只凝 1 批（留 4 万未凝）；
    计量只看**用户消息＋助手消息**（工具结果不进上下文、也不进凝练）。
[2026-10-10 计量口径改 token] 用户 10-07 原话「触发 80 万 token」；旧实现按自造
    系数 2.1 折成字节 1,680,000（无出处、且从未触发）。现直接以 _meter_sum 的
    cl100k_base token 数与 800,000 比较，不做任何字节折算。
    """
    _warn_deprecated_once("CONDENSE_TRIGGER_K", "CONDENSE_TRIGGER_TOKENS")
    raw = os.environ.get("CONDENSE_TRIGGER_TOKENS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            logger.warning("[condense_fold] CONDENSE_TRIGGER_TOKENS 非法（%r），用缺省值", raw)
    return DEFAULT_TRIGGER_TOKENS


def window_budget_tokens() -> int:
    """单批窗口预算（token）[源侧 windowTokenBudget 形态]：CONDENSE_WINDOW_BUDGET_TOKENS
    覆盖优先，缺省 300,000；旧键 CONDENSE_WINDOW_BUDGET_BYTES 已废弃。

    ★与触发线同尺：两者共用 _msg_meter_tokens，必须一起改（只改一个会造成
    「触发按 80 万 token、一次只切 30 万字节」的口径分裂）。
    """
    _warn_deprecated_once("CONDENSE_WINDOW_BUDGET_BYTES", "CONDENSE_WINDOW_BUDGET_TOKENS")
    raw = os.environ.get("CONDENSE_WINDOW_BUDGET_TOKENS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            logger.warning("[condense_fold] CONDENSE_WINDOW_BUDGET_TOKENS 非法（%r），用缺省值", raw)
    return DEFAULT_WINDOW_BUDGET_TOKENS


def condense_max_batches() -> int:
    """单 job 批上限（照抄源侧 2026-09-09 用户裁定）。

    自动触发**只跑一批**（缺省 1）：批后 mark 即停，未凝从窗口大小重新起算，
    等下一次自然触发。`CONDENSE_MAX_BATCHES` 仅供「全量导入」这类显式场景上调
    （源侧对应 import-drive 用 `condenseMaxBatchesPerJob`，常量 2）。
    """
    raw = os.environ.get("CONDENSE_MAX_BATCHES", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            logger.warning("[condense_fold] CONDENSE_MAX_BATCHES 非法（%r），用缺省值", raw)
    return DEFAULT_MAX_BATCHES


def load_memory(work_id: str, card_id: str) -> dict[str, Any]:
    """读记忆文件（routes 层 mark 读取/200 区渲染源，SP-05 IO 分离的 IO 端）。

    缺文件 → 空水位；坏 JSON/形态不对 → 空水位＋warning 留痕（V5 哲学：
    失败不凝、不阻断、绝不静默改写原文件）。
    """
    path = memory_path(work_id, card_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state(card_id)
    except OSError as exc:
        logger.warning("[condense_fold] 记忆文件读取失败（按空水位、留痕）work=%s card=%s: %s",
                       work_id, card_id, exc)
        return _empty_state(card_id)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("顶层不是 JSON 对象")
    except (ValueError, TypeError) as exc:
        logger.warning("[condense_fold] 记忆文件解析失败（按空水位、留痕）work=%s card=%s: %s",
                       work_id, card_id, exc)
        return _empty_state(card_id)
    data.setdefault("card_id", card_id)
    mark = data.get("mark", 0)
    data["mark"] = int(mark) if isinstance(mark, (int, float)) and mark >= 0 else 0
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


# 写锁（session_store 锁家族同款：按 (work_id, card_id) 键控 asyncio.Lock）
_memory_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _memory_lock(work_id: str, card_id: str) -> asyncio.Lock:
    key = (work_id, card_id)
    lock = _memory_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _memory_locks[key] = lock
    return lock


async def save_memory(work_id: str, card_id: str, data: dict[str, Any]) -> None:
    """写记忆文件：锁家族内 tmp→os.replace 原子替换（只写 memory 文件本身，
    不触碰卡片与会话——RD-01/RD-06/V3）。"""
    path = memory_path(work_id, card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    async with _memory_lock(work_id, card_id):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)


async def ensure_watermark(work_id: str, card_id: str) -> bool:
    """空水位建账（任务 1-2，幂等）：已有记忆文件不覆盖；该卡无会话不建。
    返回是否新建。"""
    if memory_path(work_id, card_id).exists():
        return False
    if not session_store._session_path(work_id, card_id).exists():
        return False
    await save_memory(work_id, card_id, _empty_state(card_id))
    return True


async def ensure_work_watermarks(work_id: str) -> list[str]:
    """为 work 下每张有会话的卡落空水位（任务 1-2 批量面）。返回新建 card_id 列表。

    [卡片存储迁移 SQLite 2026-10-14] 卡清单数据源从 ``cards/*.md`` 改为库内
    ``cards`` 表（是否「有会话」仍由会话文件判——会话不进库）。
    """
    from . import card_db

    created: list[str] = []

    def _fn(conn):
        return [r["card_id"] for r in conn.execute("SELECT card_id FROM cards ORDER BY card_id")]

    for cid in card_db.read_sync(work_id, _fn, []) or []:
        if await ensure_watermark(work_id, cid):
            created.append(cid)
    return created


# 回合末触发后台任务句柄（防 GC；完成即弃）
_background_tasks: set[asyncio.Task] = set()

# [T15-任务 7] 同卡互斥标记（竞态防线）：在凝的卡二次触发→跳过留痕，不排队不设闸。
# 单事件循环内「查＋占」之间无 await，asyncio 语义下原子。
# [A4/T086] 原 _condensing_cards 集合已并入 session_facade._active_turns 逻辑
# [2026-10-22 用户裁定] 凝练是后台任务，**不参与回合串行锁**——只用 _CONDENSING 防自身并发。
# 旧注释（「凝练走 facade.turn_try_acquire」）已作废：那把锁主线在 finally 才放，凝练永远抢不到。
# (owner="condense")，主线回合持有期间凝练跳过留痕，凝练持有期间主线回合 409。


# [2026-10-10·计量口径改 token] 统一计量尺：触发求和与切片累计共用同一函数。
# 本函数返回 **token 数**（tiktoken cl100k_base 真编码），不是字节数：
#   · 旧实现为 len(utf-8) + 1 的字节计量 + 配置层 ×2.1 折算——该系数是自造的
#     （先定 300000 字节再倒推「≈14 万 token」配出来的），实测中文 B/token 为
#     cl100k 2.93／o200k 4.30／p50k·r50k 1.47／源侧估算口径 3.0，2.1 对不上任何一个；
#   · 现按用户 10-10 令直接数 token，不再做任何字节折算。
# 选 cl100k_base 的理由：① 真 BPE 编码器（源侧 estimateTokens 自注「不追求与
#   具体 tokenizer 逐字对齐」）；② 与 litellm.token_counter 对 anthropic 模型的
#   兜底口径一致（实测 litellm 对 haiku-5.5 与 tiktoken cl100k 同值），避免系统内
#   出现两套 token 数；③ 比 o200k 多算约 22%，同内容更早触发。
# 做不到的部分（如实记，勿包装）：本地任何计数都得不到 haiku-5.5 的真实分词数
#   （该模型走 Nous 聚合端点，真实 BPE 未知）；cl100k 是确定性近似，不是准确值。
_ENC = tiktoken.get_encoding("cl100k_base")

# 逐条 +1 的分隔符记账：源侧 truncateWindow 逐条累加公式为
# `used += estimateTokens(serializeEvent(window[i])) + 1`，+1 即 join('\n') 分隔符。
# 此处保留该 +1（token 口径下仍记 1 个分隔符 token，与源侧同构）。
_MSG_SEP_TOKENS = 1


def _msg_meter_tokens(m: dict[str, Any]) -> int:
    """单条消息的 token 计数（统一计量尺本体）。

    [工具结果落会话 2026-10-17] 计量口径＝含工具（用户 10-17 裁定）：
    - 基础＝content 的 cl100k token 数（照旧）＋ 1 个 join 分隔符 token；
    - 若该块带 ``tool_calls``，再把每个调用的 ``arguments`` 长度一并计入
      （write_file 这类工具参数可能很大，不计入会漏算 ⇒ 80 万线算小、
      凝练触发偏晚，线就不准）。_truncate_window 与 _meter_sum 共用本函数
      ⇒ 自动跟随，无需另改。
    """
    total = len(_ENC.encode(str(m.get("content", "") or ""))) + _MSG_SEP_TOKENS
    tool_calls = m.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            args = fn.get("arguments") if isinstance(fn, dict) else ""
            if args:
                total += len(_ENC.encode(str(args)))
    return total


def _meter_sum(msgs: list[dict[str, Any]]) -> int:
    """统一计量尺求和（触发判定专用读数，与切片累计同尺、tolerance=0）。"""
    return sum(_msg_meter_tokens(m) for m in msgs)


def _context_meter(blocks: list[dict[str, Any]]) -> int:
    """**进上下文的量**（token）——触发线、窗口预算、销账统一用这一把尺。

    [2026-10-23 用户需求] 工具结果不进上下文，所以它们**不算**在这里：
    按含工具的块量去量，会拿 96% 的"根本不在上下文里的东西"去顶触发线，
    结果就是白凝（压不存在的体积、白烧额度）。
    """
    from . import predicates
    return sum(_msg_meter_tokens(m) for m in blocks if predicates.enters_context(m))


def _truncate_window(pending: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """单批窗口切片 [任务2，源侧 truncateWindow 原样搬回]：按统一计量尺（token）
    逐条累加，取「最接近且不超预算」的切片（宁少不超，用户 14:56 口径）；累计越线
    即停（继续只会更远，源侧同款剪枝）。单条即超预算的病态窗口保底取 1 条并留痕
    （防该卡窗口永久卡死；源侧取最近点会越线，任务书判据为不超——按任务书执行、
    差异如实声明）。"""
    # [口径 2026-10-22 复核] 预算按**含工具结果的原始块量**累计（决定这一批推进到哪），
    # 送模型的内容另由 `_build_condense_user_prompt` 只取 user/assistant。
    # 用户原话：「窗口/计量按含工具算，喂给凝练的内容不含工具」——两处不得写反。
    from . import predicates

    used = 0
    best = 0
    for i, m in enumerate(pending):
        # [2026-10-23 口径统一] 只有**进上下文**的块占预算；工具块照旧落在窗口里
        # （销账要越过它们），但不计入 30 万。
        if predicates.enters_context(m):
            used += _msg_meter_tokens(m)
            if used > budget:
                break
        best = i + 1
    if best == 0 and pending:
        logger.warning("[condense_fold] 单条即超窗口预算 budget=%d——保底取 1 条防卡死", budget)
        return pending[:1]
    return pending[:best]


def _build_condense_user_prompt(pending: list[dict[str, Any]]) -> str:
    """凝练 user prompt（任务 2/3）：未凝段全文，每块前缀 `[块{seq}]`。

    [工具结果落会话 2026-10-17] 凝练输入**只收有内容的 human/assistant 块**：
    - 排除工具块（actor=tool 的工具结果块／环节留痕块）；
    - 排除 content 为空的块（含带 tool_calls 的 assistant 块 A——空内容块
      无内容可压，空 ``[块n]`` 会干扰模型）。
    这与「计量含工具」方向相反，两处都不得写错（用户口径：窗口/计量按含工具算，
    喂给凝练的内容不含工具——「凝练时把工具结果去掉，只塞 user 和 assistant」）。
    """
    parts: list[str] = []
    for m in pending:
        # [原文判定收口 2026-10-20 · 照抄 dsh predicates] 用**共享判据**决定什么算原文：
        # `is_condense_original_event` ＝ 原文 且非工具块。它比原来的
        # `actor in ("human","assistant")` 多排除了两类**注入块**（source.kind =
        # plugin / steer）——dsh 红线：不排除的话机器生成的指令会被记忆吸收、每轮重放累积。
        from . import predicates
        if not predicates.is_condense_original_event(m):
            continue
        content = str(m.get("content", "") or "")
        if not content.strip():
            continue
        parts.append(f"[块{m.get('seq', '?')}]\n{content}")
    return "\n\n".join(parts)


def _drop_overlapping(entries: list[dict[str, Any]], lo: int, hi: int) -> list[dict[str, Any]]:
    """摘掉覆盖区间与 [lo, hi] 相交的旧条目（重凝去重）。

    条目覆盖区间取 `seq_from..seq_to`（无则退化为单点 `seq`）。
    区间不相交 ⇒ 原样保留（不同段的结论互不影响）。
    """
    out: list[dict[str, Any]] = []
    for e in entries or []:
        a = e.get("seq_from")
        b = e.get("seq_to")
        if a is None or b is None:
            a = b = int(e.get("seq", 0) or 0)
        try:
            a = int(a); b = int(b)
        except (TypeError, ValueError):
            out.append(e)
            continue
        if b < lo or a > hi:      # 不相交
            out.append(e)
    return out


def _parse_condense_output(text: str, default_seq: int,
                           block_seqs: list[int] | None = None) -> list[dict[str, Any]]:
    """凝练产出解析：**JSON 节点优先，旧【结论】/【依据】格式回退**。

    [记忆因果链 2026-10-20] dsh 的凝练产出是**节点 JSON**（`{"nodes":[{anchor,gist,
    kind,coveredSeqs,start,result,process,lesson}]}`）。本项目升级到该格式，但保留
    旧标记格式的解析——存量 fake、老模型、以及模型偶发退回旧写法时都不至于全批失败。

    统一返回条目字典（下游 render/落盘只认这些键）：
    `seq / conclusion / evidence / kind / coveredSeqs / start / result / process / lesson`
    （旧格式只有前三键，其余缺省——`chain_from_entries` 会补缺省）。
    """
    raw = str(text or "").strip()
    nodes = _try_parse_node_json(raw)
    if nodes is not None:
        out: list[dict[str, Any]] = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            gist = str(n.get("gist") or "").strip()
            if not gist:
                continue
            anchor = n.get("anchor") or {}
            seq = anchor.get("seq") if isinstance(anchor, dict) else None
            if not isinstance(seq, int):
                covered = n.get("coveredSeqs") or []
                seq = int(covered[0]) if covered else default_seq
            covered = [int(x) for x in (n.get("coveredSeqs") or []) if isinstance(x, (int, float))]
            out.append({
                "seq": int(seq),
                "conclusion": gist,
                "evidence": str(n.get("process") or ""),
                "kind": str(n.get("kind") or "fact"),
                "coveredSeqs": covered or [int(seq)],
                "start": str(n.get("start") or ""),
                "result": str(n.get("result") or ""),
                "process": str(n.get("process") or ""),
                "lesson": str(n.get("lesson") or ""),
            })
        if out:
            return out
    return _parse_condense_markers(raw, default_seq, block_seqs)


def _try_parse_node_json(text: str) -> Optional[list[dict[str, Any]]]:
    """宽松取 JSON：整段能解就用；否则截取第一个 `{` 到最后一个 `}`。解不出＝None。"""
    candidates = [text]
    lo, hi = text.find("{"), text.rfind("}")
    if 0 <= lo < hi:
        candidates.append(text[lo:hi + 1])
    lo, hi = text.find("["), text.rfind("]")
    if 0 <= lo < hi:
        candidates.append(text[lo:hi + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            return data["nodes"]
        if isinstance(data, list):
            return data
    return None


def _parse_condense_markers(text: str, default_seq: int,
                            block_seqs: list[int] | None = None) -> list[dict[str, Any]]:
    """按【结论】/【依据】标记逐条切分（**旧格式兼容**，任务 3 原实现）。

    以标记为分隔符做全文分词（真模型实测会把【依据】写在与【结论】同一行——
    按行切会丢锚）；标记间的续行/续段归当前段（长产出不截断，RD-05 同源）。
    首个标记之前的杂文丢弃。seq＝该条依据引用的起始块号（依据中块号取最小；
    无块号→default_seq＝窗口首块兜底）。空产出或无有效【结论】→ 返回 []
    （调用方走失败路径）。
    """
    parsed: list[dict[str, Any]] = []
    mode: str | None = None  # None=前言（丢）｜'conclusion'｜'evidence'
    buf_conc: list[str] = []
    buf_evi: list[str] = []
    # [seq 身份修复 2026-10-22] 旧格式条目常常不带块号，原来一律兜底成窗口首块 ⇒
    # 一批条目全挤在同一个 seq（实测 447 上挤了 12 条），触发「I1 gist 身份重复」、
    # 记忆链拒绝物化。改为：不带块号的条目**按窗口内可送块逐条分配**（顺序对应），
    # 带块号的仍用块号；两者都避让已占用的 seq。
    _used: set[int] = set()
    _pool: list[int] = [int(x) for x in (block_seqs or [])]

    def _pick_seq(refs: list[int]) -> int:
        if refs:
            cand = min(refs)
            if cand not in _used:
                _used.add(cand)
                return cand
        for s_ in _pool:
            if s_ not in _used:
                _used.add(s_)
                return s_
        _used.add(default_seq)
        return default_seq

    def _flush() -> None:
        nonlocal buf_conc, buf_evi
        conclusion = "\n".join(buf_conc).strip()
        if conclusion:
            evidence = "\n".join(buf_evi).strip()
            refs = [int(n) for n in _BLOCK_REF_RE.findall(evidence)]
            parsed.append({
                "seq": _pick_seq(refs),
                "conclusion": conclusion,
                "evidence": evidence,
            })
        buf_conc, buf_evi = [], []

    for tok in re.split(r"(【结论】|【依据】)", str(text or "")):
        if tok == "" or tok is None:
            continue
        if tok == _CONCLUSION_MARK:
            _flush()
            mode = "conclusion"
        elif tok == _EVIDENCE_MARK:
            if mode is not None:
                mode = "evidence"
            # 无主的【依据】（结论前）丢弃
        elif mode == "conclusion":
            buf_conc.append(tok.strip())
        elif mode == "evidence":
            buf_evi.append(tok.strip())
    if mode is not None:
        _flush()
    return parsed


async def _condense_call(user_prompt: str) -> str:
    """凝练模型调用（任务 3）：call_main 纯文本（prior_messages=[]、tools=None）。

    测试经 monkeypatch 本接缝注入脚本化产出——MockGateway 罐头内容无
    【结论】/【依据】形态且 gateway.py 禁改（model_service 头注），RD-10
    两文件改动面不容第三件。
    [凝练折叠开启·任务5] 模型位改走 bindings.condense 绑定（当前 glm-flash，经 model_config.resolve("condense")）。
    """
    from ..api import model_config, model_service  # 延迟导入：storage 装载期不依赖 api（唯一调用点）

    result, _model = await model_service.call_main(
        CONDENSE_SYSTEM_PROMPT, user_prompt, prior_messages=[], tools=None,
        model_name=model_config.pick_one("condense"))
    return str((result or {}).get("content") or "")

async def _condense_one_batch(work_id: str, card_id: str,
                              window: list[dict[str, Any]], mark: int) -> bool:
    """单批凝练（原管线本体逐段保留，[任务3] 批化拆出）：取段→模型写→解析→
    幂等判定→落 entries→推进 mark。返回该批是否成功（幂等补推/幂等跳过均算
    成功——窗口已凝过，游标应越过）。

    失败（调用异常/空产出/解析失败）＝水位一分不动、warning 留痕一条、
    不阻断不重试不设闸（V5 哲学/RD-04）。entries＋mark 单文件原子落盘。
    """
    user_prompt = _build_condense_user_prompt(window)
    # [工具结果落会话 2026-10-17] 整窗筛完为空（全是工具块/空块）：无内容可凝，
    # **不得送空 prompt 给模型**。★ 与任务书字面「mark 不动」的偏离：此处改为
    # **销账**（推进 mark 越过该窗口）后继续——若 mark 不动，该卡凝练会永久卡死
    # 在同一窗口（窗口恒为空、mark 恒不动、每轮重取同一窗）。销账语义与用户口径
    # 「销账时带工具结果一起销」一致（详见施工回报）。
    if not user_prompt.strip():
        target_mark = int(window[-1].get("seq", 0) or 0) + 1
        state = load_memory(work_id, card_id)
        if int(state.get("mark", 0) or 0) < target_mark:
            await save_memory(work_id, card_id, {
                "card_id": card_id, "mark": target_mark,
                "entries": state.get("entries") or [],
                "batch_count": state.get("batch_count", 0),
                "last_fold_check": state.get("last_fold_check", 0),
                "folded": state.get("folded", [])})
        logger.info("[condense_fold] 窗口无内容块——销账不凝（不发空 prompt）"
                    "work=%s card=%s 窗口块数=%d mark %d→%d",
                    work_id, card_id, len(window), mark, target_mark)
        return True
    # [状态监控 2026-10-23] 每批开始/结束都留痕：原来整批几分钟毫无输出，
    # 只能靠猜"是不是卡死了"。现在能看到它在哪、多大、跑了多久。
    _t0 = time.time()
    logger.info("[condense_fold] 批开始 work=%s card=%s 窗口块=%d 送凝练=%d块 prompt=%d字符",
                work_id, card_id, len(window),
                sum(1 for m in window if str(m.get("content") or "").strip()),
                len(user_prompt))
    input_bytes = len(user_prompt.encode("utf-8"))
    fingerprint = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()[:16]
    # 任务 3：模型写【结论】＋【依据】
    # [防挂死 2026-10-23] 模型调用加硬超时：原来一旦上游不回就永远挂住，
    # 整条凝练线卡死（实测 card_001 一批跑十几分钟没动静）。超时＝该批失败、
    # mark 不动，下轮再来，绝不无限等。
    # [2026-10-23 用户裁定] **默认不设超时**：实测"看起来卡住"其实是每批几分钟、
    # 一直在推进（card_001 五批把 mark 从 539 推到 845）。机械超时会掐掉这些
    # "慢但能完成"的批，属掩盖问题。要设上限须显式给 CONDENSE_CALL_TIMEOUT>0。
    _call_timeout = float(os.environ.get("CONDENSE_CALL_TIMEOUT", "0") or 0)
    try:
        if _call_timeout > 0:
            text = await asyncio.wait_for(_condense_call(user_prompt), timeout=_call_timeout)
        else:
            text = await _condense_call(user_prompt)
    except asyncio.TimeoutError:
        logger.warning("[condense_fold] 凝练模型 %.0fs 超时——该批放弃、水位不动 work=%s card=%s",
                       _call_timeout, work_id, card_id)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[condense_fold] 凝练失败——水位一分不动 work=%s card=%s 阶段=模型调用 原因=%s",
                       work_id, card_id, exc)
        return False
    # 任务 3：产出解析（失败/空产出→不凝）
    from . import predicates as _pred
    _send_seqs = [int(m.get("seq") or 0) for m in window
                  if _pred.is_condense_original_event(m) and str(m.get("content") or "").strip()]
    fresh = _parse_condense_output(
        text, default_seq=int(window[0].get("seq", mark) or mark),
        block_seqs=_send_seqs)
    if not fresh:
        logger.warning("[condense_fold] 凝练失败——水位一分不动 work=%s card=%s 阶段=产出解析 原因=空产出或无【结论】条目",
                       work_id, card_id)
        return False
    logger.info("[condense_fold] 批完成 work=%s card=%s 耗时=%.1fs 解析=%d条 mark %d→%d",
                work_id, card_id, time.time() - _t0, len(fresh), mark,
                int(window[-1].get("seq", 0) or 0) + 1)
    target_mark = int(window[-1].get("seq", 0) or 0) + 1
    state = load_memory(work_id, card_id)
    existing = [e for e in (state.get("entries") or []) if isinstance(e, dict)]
    # 任务 5 幂等：同窗口（同 fingerprint）已凝 → 条目零新增，mark 幂等到位
    if any(e.get("fingerprint") == fingerprint for e in existing):
        if int(state.get("mark", 0) or 0) < target_mark:
            await save_memory(work_id, card_id,
                              {"card_id": card_id, "mark": target_mark,
                               "entries": existing,
                               "batch_count": state.get("batch_count", 0),
                               "last_fold_check": state.get("last_fold_check", 0),
                               "folded": state.get("folded", [])})
            logger.info("[condense_fold] 幂等补推 mark work=%s card=%s mark→%d 条目零新增 fingerprint=%s",
                        work_id, card_id, target_mark, fingerprint)
        else:
            logger.info("[condense_fold] 幂等跳过（同指纹已凝、mark 已到位）work=%s card=%s mark=%d 条目数=%d fingerprint=%s",
                        work_id, card_id, int(state.get("mark", 0) or 0), len(existing), fingerprint)
        return True
    # 任务 4：五字段落 entries（seq/conclusion/evidence/occurred_at/fingerprint）
    # [原文入库 2026-10-20] 再加两键 `seq_from`/`seq_to`：**这条记忆压自哪一段原文**
    # （块序号闭区间）。追溯链＝ entry.seq_from/seq_to → origin_store.trace_entry →
    # session_blocks 行。原先只记 `seq`（最小块号），覆盖范围只能靠相邻条目反推。
    occurred_at = datetime.now().astimezone().isoformat()
    _win_from = int(window[0].get("seq", 0) or 0) if window else int(mark or 0)
    _win_to = int(window[-1].get("seq", 0) or 0) if window else _win_from
    final_entries = [dict(e, occurred_at=occurred_at, fingerprint=fingerprint,
                          seq_from=_win_from, seq_to=_win_to) for e in fresh]
    # [重凝去重 2026-10-21] 同一段原文只留一条结论：窗口 [win_from, win_to] 已被
    # 旧条目覆盖的，把旧条目摘掉再落新的。否则一旦水位回退（人工恢复/重跑）就会
    # 出现「同一 seq 两条结论」⇒ 因果链 I1「身份重复」违例、记忆区重复占 token。
    _kept_existing = _drop_overlapping(existing, _win_from, _win_to)
    if len(_kept_existing) != len(existing):
        logger.info("[condense_fold] 重凝去重：窗口 %s..%s 摘掉 %d 条旧结论（保留 %d 条）card=%s",
                    _win_from, _win_to, len(existing) - len(_kept_existing),
                    len(_kept_existing), card_id)
    existing = _kept_existing
    await save_memory(work_id, card_id,
                      {"card_id": card_id, "mark": target_mark, "entries": existing + final_entries,
                       "batch_count": state.get("batch_count", 0),
                       "last_fold_check": state.get("last_fold_check", 0),
                       "folded": state.get("folded", [])})
    # [记忆因果链 2026-10-20] 条目 → 单层链落库，并跑 I1–I5 不变量（违例只记日志、不阻断）。
    # 链落 `memory` 表（dsh 同表：每会话一条 = 链 + 两个半开水位）；追溯走 coveredSeqs。
    try:
        chain = memory_domain.chain_from_entries(existing + final_entries, card_id,
                                                 folded_fingerprints=set(state.get("folded") or []))
        viol = memory_domain.check_invariants(chain)
        if viol:
            logger.warning("[memory_domain] 条目不变量违例 %d 条 card=%s: %s",
                           len(viol), card_id, viol[:3])
        memory_domain.save_chain(work_id, card_id, chain, condense_mark=target_mark)
        # [archive 接线 2026-10-20] 描述层（process/docPaths/docRefs）进 memory_archive——
        # dsh 口径：chain 只留 gist（结论），长正文进 archive，展开时按锚点取。
        memory_domain.save_archive(work_id, card_id, chain)
        # [多层链 2026-10-20 · 照抄 dsh condense/migrate-archive] 把描述层正文**物化**成
        # description 子节点（anchor 与父同、level=父+1、upper=[父]、coveredSeqs 拷父、
        # 文本同父 gist ⇒ degraded）——这才是 dsh 的**两层链**（gist 结论 + description 描述）。
        # 物化后若 I1–I5 有违例 ⇒ **拒绝写**（只记日志，保留原单层链）。
        _arc_rows = [{"seq": int((n.get("anchor") or {}).get("seq", 0) or 0),
                      "process": n.get("process")}
                     for n in chain if n.get("process")]
        if _arc_rows:
            _migrated, _mrep = memory_domain.migrate_archive(chain, _arc_rows)
            if _mrep["created"]:
                if _mrep["violations"]:
                    logger.warning("[condense_fold] 多层物化违例（拒绝写，保留单层链）card=%s: %s",
                                   card_id, _mrep["violations"][:3])
                else:
                    memory_domain.save_chain(work_id, card_id, _migrated,
                                             condense_mark=target_mark)
                    chain = _migrated
    except Exception as exc:  # noqa: BLE001 链落库/校验失败不阻断凝练
        logger.warning("[memory_domain] 链落库/校验失败（不阻断）card=%s: %s", card_id, exc)
    logger.info("[condense_fold] 凝练完成 work=%s card=%s 输入块数=%d 输入字节=%d 产出条数=%d mark %d→%d fingerprint=%s",
                work_id, card_id, len(window), input_bytes, len(final_entries), mark, target_mark, fingerprint)
    return True


async def _run_condense_pipeline(work_id: str, card_id: str,
                                 pending: list[dict[str, Any]], mark: int) -> None:
    """凝练管线本体 [凝练折叠开启 2026-10-07·任务2/3 批循环版]：每批＝源侧 S0 窗口
    组装（_truncate_window 宁少不超）→凝练→落 entries→推进 mark；循环直至窗口
    清空或达批上限（源侧 condenseMaxBatchesPerJob）。单批失败零阻断：水位一分
    不动、留痕、不重试不排队，剩余未凝段留 mark 之后由后续回合继续取（L252-253
    /L327-329 哲学不变）。成功批计入 batch_count（源侧 batchStarts.length 同位），
    满 FOLD_EVERY_BATCHES 游标差即挂归拢线（任务 6）。
    """
    if not pending:
        logger.info("[condense_fold] 未凝段为空——跳过 work=%s card=%s", work_id, card_id)
        return
    budget = window_budget_tokens()
    max_batches = condense_max_batches()
    idx = 0
    batches = 0
    while batches < max_batches:
        window = _truncate_window(pending[idx:], budget)
        if not window:
            break   # 窗口清空
        ok = await _condense_one_batch(work_id, card_id, window, mark)
        if not ok:
            break   # 单批失败零阻断：留痕已打，剩余未凝段留 mark 之后（不重试不排队）
        idx += len(window)
        batches += 1
    if batches == 0:
        return
    state = load_memory(work_id, card_id)
    state["batch_count"] = int(state.get("batch_count", 0) or 0) + batches
    await save_memory(work_id, card_id, state)
    logger.info("[condense_fold] 本轮批循环收口 work=%s card=%s 批数=%d 累计批=%d 未凝余=%d块",
                work_id, card_id, batches, int(state["batch_count"]), len(pending) - idx)
    # [防挂死 2026-10-23] 归拢线只是"把条目再折一层"，不是凝练本体；
    # 它卡住不许拖死凝练——加超时，超时就跳过、下轮再说。
    _fold_timeout = float(os.environ.get("CONDENSE_FOLD_TIMEOUT", "0") or 0)
    try:
        if _fold_timeout > 0:
            await asyncio.wait_for(_maybe_fold(work_id, card_id), timeout=_fold_timeout)
        else:
            await _maybe_fold(work_id, card_id)
    except asyncio.TimeoutError:
        logger.warning("[condense_fold] 归拢线 %.0fs 超时——跳过，下轮再试 work=%s card=%s",
                       _fold_timeout, work_id, card_id)
    except Exception as exc:  # noqa: BLE001 归拢线零阻断（失败游标不动，下轮重试）
        logger.warning("[condense_fold] 归拢线异常（不阻断、游标不动、下轮重试）work=%s card=%s: %s",
                       work_id, card_id, exc)


# ── [凝练折叠开启 2026-10-07·任务6] fold-l1 归拢线（源侧 fold-l1.ts runFoldJob 三阶段
#    照抄适配）──
# ①判断＝模型一次、无果零落库（judge-empty 合法终态，守「不能为了凝练而凝练」）；
# ②机械校验＝被归拢成员须为未折叠的已凝条（源侧 validateConvergeBatch 入口 A 平铺适配）；
# ③提交＝mark 不动、旧成员降层保留（源侧「挂 upper」平铺适配为 state.folded 指纹账：
#   被归拢成员物理保留在 entries、消费侧按账过滤＝不再作为顶层散条；entry 五字段契约不变）。
# 触发游标（源侧流程 C）：batch_count − last_fold_check ≥ FOLD_EVERY_BATCHES；
# 三态语义：committed/judge-empty → 推游标；abort（调用异常）→ 游标不动（失败不消费触发线）。
FOLD_SYSTEM_PROMPT = (
    "你是记忆归拢器。给定一批已凝练的记忆条目（每条带条号与五字段形态），只做一件事："
    "判断哪些条目是同一件事且已收尾完成，把它们归拢成一条更大的条目。输出严格 JSON："
    '{"merges":[{"covers":[条号,条号],"conclusion":"【结论】一句话陈述这组条目共同确立的事实或判断",'
    '"evidence":"【依据】指向所依据的具体对话块号"}]}。covers 至少两条；'
    '无该归拢的输出 {"merges":[]}；不得新增内容、不得改写未选中条目、不得输出 JSON 以外的文字。'
)

_FOLD_JSON_RE = re.compile(r"\{.*\}", re.S)


def _parse_fold_output(text: str) -> list[dict[str, Any]]:
    """归拢产出解析（源侧 parseJsonLoose 同位）：提取首个 JSON 对象取 merges 数组。
    解析失败/空 merges → []（调用方按 judge-empty 终态推游标——源侧三态语义）。"""
    match = _FOLD_JSON_RE.search(str(text or ""))
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    merges = data.get("merges") if isinstance(data, dict) else None
    if not isinstance(merges, list):
        return []
    return [m for m in merges if isinstance(m, dict)]


async def _fold_call(user_prompt: str) -> str:
    """归拢模型调用（与凝练同走 bindings.condense 绑定）。"""
    from ..api import model_config, model_service

    result, _model = await model_service.call_main(
        FOLD_SYSTEM_PROMPT, user_prompt, prior_messages=[], tools=None,
        model_name=model_config.pick_one("condense"))
    return str((result or {}).get("content") or "")


def _foldable_tops(state: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """可归拢顶层条（源侧 foldableTop 平铺适配）：fingerprint 不在 folded 账的条目。
    返回 [(entries 下标, 条目)]。"""
    folded = set(state.get("folded") or [])
    return [(i, e) for i, e in enumerate(state.get("entries") or [])
            if isinstance(e, dict) and str(e.get("fingerprint") or "") not in folded]


async def _maybe_fold(work_id: str, card_id: str) -> None:
    """fold 触发与执行（游标式，源侧流程 C 三阶段照抄）。"""
    state = load_memory(work_id, card_id)
    count = int(state.get("batch_count", 0) or 0)
    last = int(state.get("last_fold_check", 0) or 0)
    if count - last < FOLD_EVERY_BATCHES:
        return
    tops = _foldable_tops(state)
    if len(tops) < 2:
        state["last_fold_check"] = count
        await save_memory(work_id, card_id, state)
        logger.info("[condense_fold] 归拢判断空转（fold-l1.judge-empty tops<2，零模型）work=%s card=%s 批数=%d",
                    work_id, card_id, count)
        return
    # ①判断（必调模型一次）：可归拢顶层条全文（五字段形态＋原始条号）
    entries = state.get("entries") or []
    lines = []
    for i, e in tops:
        lines.append(f"条{i + 1}｜seq={e.get('seq', '?')}｜【结论】{e.get('conclusion', '')}｜【依据】{e.get('evidence', '')}")
    user_prompt = "\n".join(lines)
    try:
        text = await _fold_call(user_prompt)
    except Exception as exc:  # noqa: BLE001 abort：游标不动（失败不消费触发线）
        logger.warning("[condense_fold] 归拢失败——游标不动（abort，账保留下轮重试）work=%s card=%s 阶段=模型调用 原因=%s",
                       work_id, card_id, exc)
        return
    merges = _parse_fold_output(text)
    if not merges:
        state["last_fold_check"] = count
        await save_memory(work_id, card_id, state)
        logger.info("[condense_fold] 归拢判断无果（fold-l1.judge-empty 解析失败或 merges=0，零落库推游标）work=%s card=%s 批数=%d",
                    work_id, card_id, count)
        return
    # ②机械校验：covers ≥2 条、下标合法、全部未折叠、结论非空（入口 A 平铺适配）
    folded = set(state.get("folded") or [])
    accepted: list[tuple[list[int], dict[str, Any]]] = []
    for m in merges:
        covers = m.get("covers")
        if not isinstance(covers, list):
            continue
        covers = [c for c in covers if isinstance(c, int) and 1 <= c <= len(entries)]
        if len(set(covers)) < 2:
            continue
        if any(str(entries[c - 1].get("fingerprint") or "") in folded for c in covers):
            continue
        if not str(m.get("conclusion") or "").strip():
            continue
        accepted.append((sorted(set(covers)), m))
    if not accepted:
        state["last_fold_check"] = count
        await save_memory(work_id, card_id, state)
        logger.info("[condense_fold] 归拢校验全拒（fold-l1.judge-empty accepted=0，零落库推游标）work=%s card=%s 批数=%d",
                    work_id, card_id, count)
        return
    # ③提交：mark 不动；归拢大条入列（五字段，seq=成员最小块号）；成员指纹入 folded 账
    occurred_at = datetime.now().astimezone().isoformat()
    new_entries: list[dict[str, Any]] = []
    new_folded = set(folded)
    for covers, m in accepted:
        members = [entries[c - 1] for c in covers]
        seqs = sorted({int(e.get("seq", 0) or 0) for e in members})
        fp = hashlib.sha256(
            ("fold|" + "|".join(sorted(str(e.get("fingerprint") or "") for e in members))).encode("utf-8")
        ).hexdigest()[:16]
        evidence = str(m.get("evidence") or "").strip()
        if not evidence:
            evidence = "【依据】归拢自 " + "、".join(f"块{s}" for s in seqs)
        new_entries.append({
            "seq": min(seqs),
            "conclusion": str(m.get("conclusion")).strip(),
            "evidence": evidence,
            "occurred_at": occurred_at,
            "fingerprint": fp,
        })
        new_folded.update(str(e.get("fingerprint") or "") for e in members)
    state["entries"] = list(entries) + new_entries
    state["folded"] = sorted(new_folded)
    state["last_fold_check"] = count
    await save_memory(work_id, card_id, state)
    logger.info("[condense_fold] 归拢完成（fold-l1.committed）work=%s card=%s merges=%d covers=%d mark不动=%d 批数=%d",
                work_id, card_id, len(accepted), sum(len(c) for c, _ in accepted),
                int(state.get("mark", 0) or 0), count)


#: 进程内防重入（force 路径不走 session_facade 的锁，靠这个挡住自己和自己打架）
_CONDENSING: set[tuple[str, str]] = set()


async def condense_turn_end(work_id: str, card_id: str,
                            request_tokens: int | None = None) -> None:
    """回合末凝练触发（SP-04：assistant 落盘之后、异步、不阻塞对话）。

    [T15 任务 1] 路线甲闸开（CONDENSE_ROUTE_RULINGS 2026-10-05 21:12）：K 到线
    即启动凝练管线本体（任务 2 取未凝段→任务 3 模型写→任务 4 落 entries→任务 5
    推进 mark）。失败路径不变：任何异常 catch 留痕零阻断、水位不动、不设闸
    （V5/RD-04）。同卡互斥（任务 7）：上一轮未完成→本轮跳过留痕，不排队不设闸。
    """
    try:
        await ensure_watermark(work_id, card_id)
        state = load_memory(work_id, card_id)
        mark = int(state.get("mark", 0) or 0)
        # [A6a/T069-T072] 读切换：统一读入门面（块形态含 content，计量/切片同尺）
        prior = await session_facade.read(work_id, card_id)
        pending = prior[mark:] if 0 <= mark <= len(prior) else prior
        # [2026-10-10 计量口径改 token] 统一计量尺：tiktoken cl100k_base 真编码，
        # 与切片累计同尺。旧版为字节计量 + 配置层 ×2.1 折算（系数自造、无出处）。
        # [三尺统一 2026-10-22] 触发与窗口/销账读**同一份数据**（会话块[mark:]）。
        # 本轮真实请求体积只作第二见证：本轮刚执行完、块尚未落盘的那部分会让它偏大，
        # 取两者较大值＝不低估；两者偏差 >20% 说明落盘又出了问题，打警告。
        block_size = _context_meter(pending)
        size = block_size
        if request_tokens is not None:
            req = int(request_tokens)
            size = max(block_size, req)
            if block_size and req > block_size * 1.2:
                logger.warning(
                    "[condense_fold] 三尺偏差告警：块计量=%d 请求体积=%d（%.1f 倍）"
                    "work=%s card=%s —— 落盘可能又丢/截了工具结果",
                    block_size, req, req / max(block_size, 1), work_id, card_id)
        k = trigger_k_tokens()
        if size < k:
            return  # 未到线：安静观察（K 首版估值下常态；标定期看 debug）
        # [A4/T086] 同卡互斥并入 facade._active_turns（同一登记处）：凝练持有
        # 期间主线回合 409、主线回合持有期间凝练跳过留痕——一个登记处两头生效。
        # [去锁 2026-10-22 · 用户裁定] 凝练是**后台任务**，只读会话块、只写记忆文件，
        # 与「同卡回合串行」那把锁毫无关系——原实现去抢它，而主线要到 finally 才放，
        # 于是永远抢不过（实测 fn_002 六小时没凝过）。这里只用一个进程内 per-card
        # 集合防「两个凝练同时跑」，不再碰回合锁。
        _key = (work_id, card_id)
        if _key in _CONDENSING:
            logger.info("[condense_fold] 跳过（进程内已在凝）work=%s card=%s", work_id, card_id)
            return
        _CONDENSING.add(_key)
        logger.info("[condense_fold] 触发线 K 已到线，启动凝练 work=%s card=%s 未凝token=%d K=%d mark=%d 未凝块数=%d",
                    work_id, card_id, size, k, mark, len(pending))
        try:
            await _run_condense_pipeline(work_id, card_id, pending, mark)
        finally:
            _CONDENSING.discard(_key)
    except Exception as exc:  # noqa: BLE001 触发面零阻断（V5 哲学）
        logger.warning("[condense_fold] 回合末触发失败（不阻断、留痕、水位不动）work=%s card=%s: %s",
                       work_id, card_id, exc)


async def condense_sweep_once(max_cards: int = 8) -> int:
    """扫一遍所有 work 的会话文件：未凝量到线的卡就地凝到窗口 90% 以下。

    纯后台：不看回合、不看前台状态，只按触发线记账。返回本轮凝过的卡数。
    """
    if not condense_enabled():
        return 0
    works = sorted((session_store.WORKS_DIR).glob("*/sessions"))
    done = 0
    for sess_dir in works:
        work_id = sess_dir.parent.name
        for f in sorted(sess_dir.glob("session_*.md")):
            if done >= max_cards:
                return done
            card_id = "card_" + f.stem.split("_", 1)[1]
            try:
                st = load_memory(work_id, card_id)
                mark = int(st.get("mark", 0) or 0)
                blocks = await session_facade.read(work_id, card_id)
                pending = blocks[mark:] if 0 <= mark <= len(blocks) else blocks
                if _context_meter(pending) < trigger_k_tokens():
                    continue
                logger.warning("[condense_fold] 后台扫描命中：work=%s card=%s 未凝=%d token "
                               "（线 %d）", work_id, card_id, _context_meter(pending), trigger_k_tokens())
                await condense_before_send(work_id, card_id)
                done += 1
            except Exception as exc:  # noqa: BLE001 单卡失败不影响其它卡
                logger.warning("[condense_fold] 后台扫描单卡失败 work=%s card=%s: %s",
                               work_id, card_id, exc)
    return done


def start_condense_sweeper(interval_sec: float | None = None) -> "asyncio.Task | None":
    """启动后台凝练扫描器（纯后台，与前台回合无关）。返回 Task 便于测试/停止。"""
    if not condense_enabled():
        return None
    if interval_sec is None:
        try:
            interval_sec = float(os.environ.get("CONDENSE_SWEEP_INTERVAL", "300") or 300)
        except (TypeError, ValueError):
            interval_sec = 300.0

    async def _loop() -> None:
        while True:
            try:
                await condense_sweep_once()
            except Exception as exc:  # noqa: BLE001 扫描失败不停摆
                logger.warning("[condense_fold] 后台扫描失败（下轮继续）: %s", exc)
            await asyncio.sleep(max(10.0, interval_sec))

    task = asyncio.get_event_loop().create_task(_loop())
    logger.info("[condense_fold] 后台凝练扫描器已启动 interval=%.0fs", interval_sec)
    return task


async def condense_before_send(work_id: str, card_id: str) -> bool:
    """[发送前预检 2026-10-22] 未凝量已到触发线 ⇒ **先凝一批再发请求**。

    为什么要它：凝练原本只在「回合成功收口」时触发；一旦请求本身超窗失败，
    回合永远不成功收口 ⇒ 凝练永远不触发 ⇒ 上下文永远降不下来（实测
    work_025/card_005：134 万 > 1M 窗口，每轮 502 死锁）。预检把自救提前到
    发请求之前，不再依赖"先失败一次"。

    返回是否真的跑了凝练。开关关 / 未到线 / 异常 ⇒ 一律返回 False，不阻断回合。
    """
    if not condense_enabled():
        return False
    try:
        await ensure_watermark(work_id, card_id)
        state = load_memory(work_id, card_id)
        mark = int(state.get("mark", 0) or 0)
        blocks = await session_facade.read(work_id, card_id)
        pending = blocks[mark:] if 0 <= mark <= len(blocks) else blocks
        size = _meter_sum(pending)
        if size < trigger_k_tokens():
            return False
        # [连跑多批 2026-10-22] 一批只压 30 万；超窗的卡（实测 fn_002 196%）一批压完
        # 仍 > 窗口 ⇒ 请求照样 502 ⇒ 又没人重试 ⇒ 还是死。故：只要还没降到窗口
        # 90% 以下、且每批确实推进了 mark，就继续，最多 8 批（防呆上限）。
        _WIN = int(os.environ.get("MODEL_WINDOW_TOKENS", "1000000") or 1000000)
        _target = int(_WIN * 0.9)
        ran = 0
        cur = size
        while ran < 8 and cur >= min(trigger_k_tokens(), _target):
            logger.warning("[condense_fold] 发送前预检：未凝 %d token（线 %d / 窗口目标 %d）"
                           "—— 第 %d 批 work=%s card=%s",
                           cur, trigger_k_tokens(), _target, ran + 1, work_id, card_id)
            before = int(load_memory(work_id, card_id).get("mark", 0) or 0)
            await condense_turn_end(work_id, card_id, request_tokens=cur)
            after = int(load_memory(work_id, card_id).get("mark", 0) or 0)
            ran += 1
            if after <= before:      # 没推进（失败/无内容）⇒ 停，别空转
                break
            blocks = await session_facade.read(work_id, card_id)
            cur = _meter_sum(blocks[after:])
        return ran > 0
    except Exception as exc:  # noqa: BLE001 预检失败不阻断回合
        logger.warning("[condense_fold] 发送前预检失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)
        return False


def maybe_schedule_turn_end(work_id: str, card_id: str,
                            request_tokens: int | None = None) -> None:
    """回合末触发调度口（routes 层唯一调用点）：开关关＝零动作零开销；
    开＝create_task 异步触发、不阻塞对话（SP-04）。调度失败仅留痕。"""
    if not condense_enabled():
        return
    try:
        task = asyncio.get_running_loop().create_task(
            condense_turn_end(work_id, card_id, request_tokens=request_tokens))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception as exc:  # noqa: BLE001 调度失败不阻断回合
        logger.warning("[condense_fold] 触发调度失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)
