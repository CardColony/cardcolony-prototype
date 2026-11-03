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

from . import memory_domain, memory_lifecycle, session_facade, session_store
from ..infra import observe as _obs  # [F22] 观测打点（不阻断业务；不引 api/storage 依赖）

logger = logging.getLogger("storage.memory")

# [2026-10-10 计量口径改 token] 触发线按用户口径以 token 计。
# [F18 · 2026-10-31 常量统一] 代码常量与 .env 对齐：触发 **100,000** token、
# 单批 **60,000** token（.env 实值＝2026-10-23 用户口径「触发 10 万、单批 6 万、
# 只凝 1 批」）。改前代码常量写 800,000/300,000、.env 写 100000/60000、docstring
# 又写 10万/6万——三处口径分裂。现在代码缺省＝.env 实值＝注释口径，env 覆盖仍优先。
DEFAULT_TRIGGER_TOKENS = 100_000

# [凝练折叠开启 2026-10-07 / 2026-10-10 改 token 口径] 源侧机制搬回。
# [F18] 单批窗口预算缺省与 .env 对齐＝60,000 token。★与触发线必须同尺——两者共用
# _msg_meter_tokens，只改一个会造成「触发按 A、切片按 B」的口径分裂。
# 单 job 批上限（照抄源侧 dsh `condense.ts:2905` 的 **2026-09-09 用户裁定**）：
#   「maxBatchesOverride：自动触发（maybeCondense）传 1——一次触发只跑一批，
#    批后 mark 即停、未凝从窗口大小重新起算，等下一次自然触发；不再续派连跑。」
#   源侧常量 `CONDENSE_MAX_BATCHES_PER_JOB = 2` **只给 import-drive 全量导入**用。
# ★ 本项目此前缺省写成 20 并让自动触发吃缺省值 ⇒ 一次触发把触发线以上的积压
#   全部压完（实测 card_006：3 批把 80 万 token 全吃掉，mark 推到 1825/1915，
#   历史区只剩 90 块，模型近乎失忆）。**缺省必须＝1。**
DEFAULT_WINDOW_BUDGET_TOKENS = 60_000
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
    计量只看**进上下文的块**（工具结果不进上下文、也不进凝练，_context_meter 同尺）。
[2026-10-10 计量口径改 token] 直接以 cl100k_base token 数与触发线比较，
    不做任何字节折算。[F18] 代码缺省常量已与 .env 对齐（10 万）。
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


def _load_from_file(work_id: str, card_id: str) -> dict[str, Any]:
    """**历史路径**：读旧 JSON 记忆文件（只在懒迁移时用）。"""
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


def _load_from_db(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """从 `memory` 表读（唯一事实源）。表里没这行 ⇒ None（区别于"空水位"）。"""
    from . import card_db  # 延迟：本模块被 card_db 的调用方引用

    def _fn(conn: Any) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT chain, condense_mark, batch_starts FROM memory WHERE card_id=?",
            (card_id,)).fetchone()
        if row is None:
            return None
        try:
            entries = json.loads(row["chain"] or "[]")
        except (ValueError, TypeError):
            entries = []
        mark = row["condense_mark"]
        out = {
            "card_id": card_id,
            "mark": int(mark) if isinstance(mark, int) and mark >= 0 else 0,
            "entries": entries if isinstance(entries, list) else [],
        }
        # [2026-10-27 修] 记忆状态不只有 mark/entries：`batch_count` / `last_fold_check`
        # / `folded` 等也存在同一个 dict 里。改前只存 entries+mark ⇒ 这些字段全丢，
        # 批循环与去重直接失效（`KeyError: batch_count`）。现在把它们放进
        # `batch_starts` 列（JSON），读时并回顶层。
        try:
            extra = json.loads(row["batch_starts"] or "{}")
        except (ValueError, TypeError):
            extra = {}
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in ("mark", "entries", "card_id"):
                    out[k] = v
        return out

    try:
        return card_db.read_sync(work_id, _fn, None)
    except Exception as exc:  # noqa: BLE001 读库失败按空水位，不阻断
        logger.warning("[condense_fold] 记忆表读取失败（按空水位）work=%s card=%s: %s",
                       work_id, card_id, exc)
        return None


def _save_to_db(work_id: str, card_id: str, data: dict[str, Any]) -> None:
    """写 `memory` 表（唯一事实源）。"""
    from . import card_db  # 延迟

    # mark/entries 各占一列，**其余状态**（batch_count / last_fold_check / folded …）
    # 打包进 batch_starts 列，读时并回顶层——否则这些字段一进库就丢。
    _extra = {k: v for k, v in (data or {}).items()
              if k not in ("mark", "entries", "card_id")}

    def _fn(conn: Any) -> None:
        conn.execute(
            "INSERT INTO memory(card_id, chain, condense_mark, batch_starts, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET "
            "chain=excluded.chain, condense_mark=excluded.condense_mark, "
            "batch_starts=excluded.batch_starts, updated_at=excluded.updated_at",
            (card_id,
             json.dumps(data.get("entries") or [], ensure_ascii=False),
             int(data.get("mark") or 0),
             json.dumps(_extra, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")),
        )

    card_db._write_sync(work_id, _fn)  # noqa: SLF001 同包内复用写事务边界


def load_memory(work_id: str, card_id: str) -> dict[str, Any]:
    """读记忆：**只读 sqlite 的 `memory` 表**。

    [2026-10-27 · 用户令「所有都以一个事实源，否则还是会混乱」]
    改前读 `works/{wid}/memory/{card}.json`；现在表是唯一源，JSON 文件降级为
    历史备份——**只在表里还没有这行时懒迁移一次**，之后再也不读文件。
    """
    got = _load_from_db(work_id, card_id)
    if got is not None:
        return got
    legacy = _load_from_file(work_id, card_id)
    if legacy.get("mark") or legacy.get("entries"):
        try:
            _save_to_db(work_id, card_id, legacy)
            logger.info("[condense_fold] 记忆懒迁移入库 work=%s card=%s mark=%s entries=%d",
                        work_id, card_id, legacy.get("mark"), len(legacy.get("entries") or []))
        except Exception as exc:  # noqa: BLE001 迁移失败照旧用文件值，不阻断
            logger.warning("[condense_fold] 记忆懒迁移失败（本次仍用文件值）%s/%s: %s",
                           work_id, card_id, exc)
    return legacy


def load_nodes(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """读记忆节点表 `memory_nodes`（编号体系的条目级事实源）——按批、批内序升序。

    [记忆编号体系 2026-10-29] 每行一个节点，自带编号
    `{work}/{card}/B{批次}/{批内序}`（L2 细节节点＝父编号 + `/d`）与 `occurred_at`。
    批次号按时间升序发 ⇒ 编号序 == 时间序，「最近一批」＝批次号最大者。

    与 `load_memory`（读 `memory.chain` 派生物、供凝练管线自己用）分工：
    渲染/装配只吃本函数，**不再解析 chain**——两处各读一份才不会再次错位。
    表不存在或无行 ⇒ 空列表（渲染侧按「没有来源就不出现」处理），并留痕。
    """
    from . import card_db  # 延迟：本模块被 card_db 的调用方引用

    def _fn(conn: Any) -> list[dict[str, Any]]:
        # [F9/F10] 新列（doc_paths/doc_refs/timeline_from/timeline_to）——旧库未
        # 迁移时 SELECT 会炸 ⇒ 降级用旧列集补空（下次 save_memory 的 sync_nodes
        # 会 ALTER 迁移）。表不存在＝未回填，留痕不阻断。
        new_cols = ("id, work_id, card_id, batch, ordinal, occurred_at, level, role,"
                    " gist, anchor_seq, covered_seqs, upper_id, status, kind, process,"
                    " start_text, result, lesson, sunk, superseded_by,"
                    " doc_paths, doc_refs, timeline_from, timeline_to, extra")
        old_cols = ("id, work_id, card_id, batch, ordinal, occurred_at, level, role,"
                    " gist, anchor_seq, covered_seqs, upper_id, status, kind, process,"
                    " start_text, result, lesson, sunk, superseded_by, extra")
        sql = "SELECT {cols} FROM memory_nodes WHERE work_id=? AND card_id=? ORDER BY batch, ordinal, level"
        try:
            rows = conn.execute(sql.format(cols=new_cols),
                                (work_id, card_id)).fetchall()
        except Exception:
            try:
                rows = conn.execute(sql.format(cols=old_cols),
                                    (work_id, card_id)).fetchall()
                rows = [dict(r) | {"doc_paths": "", "doc_refs": "",
                                   "timeline_from": "", "timeline_to": ""}
                        for r in rows]
            except Exception as exc:  # noqa: BLE001 表不存在＝未回填，留痕不阻断
                logger.warning("[memory_nodes] 查询失败（未回填？）work=%s card=%s: %s",
                               work_id, card_id, exc)
                return []
        return [dict(r) for r in rows]

    try:
        return card_db.read_sync(work_id, _fn, []) or []
    except Exception as exc:  # noqa: BLE001 读库失败按空，不阻断装配
        logger.warning("[memory_nodes] 读取失败 work=%s card=%s: %s", work_id, card_id, exc)
        return []


#: memory_nodes 的列序（写口/读口共用，改列只改这一处）
_NODE_COLS = ("id", "work_id", "card_id", "batch", "ordinal", "occurred_at", "level",
              "role", "gist", "anchor_seq", "covered_seqs", "upper_id", "status",
              "kind", "process", "start_text", "result", "lesson", "sunk",
              "superseded_by", "doc_paths", "doc_refs", "timeline_from",
              "timeline_to", "extra")

_NODE_DDL = """
CREATE TABLE IF NOT EXISTS memory_nodes (
    id TEXT PRIMARY KEY, work_id TEXT NOT NULL, card_id TEXT NOT NULL,
    batch INTEGER NOT NULL DEFAULT 0, ordinal INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL DEFAULT '', level INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL DEFAULT 'gist', gist TEXT NOT NULL DEFAULT '',
    anchor_seq INTEGER NOT NULL DEFAULT 0, covered_seqs TEXT NOT NULL DEFAULT '[]',
    upper_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '', process TEXT NOT NULL DEFAULT '',
    start_text TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
    lesson TEXT NOT NULL DEFAULT '', sunk INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT NOT NULL DEFAULT '',
    doc_paths TEXT NOT NULL DEFAULT '', doc_refs TEXT NOT NULL DEFAULT '',
    timeline_from TEXT NOT NULL DEFAULT '', timeline_to TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT '')
"""

#: 旧表缺列的增量迁移（ALTER 只加不改，重复执行被列存在性检查挡住）
_NODE_ALTERS: tuple[tuple[str, str], ...] = (
    ("doc_paths", "TEXT NOT NULL DEFAULT ''"),
    ("doc_refs", "TEXT NOT NULL DEFAULT ''"),
    ("timeline_from", "TEXT NOT NULL DEFAULT ''"),
    ("timeline_to", "TEXT NOT NULL DEFAULT ''"),
)

#: CausalNode 里已单独成列的键；其余进 extra，保证字段不丢
#: （fingerprint/occurred_at/seq_from/seq_to/title 无列 ⇒ 留在 extra，随行保全）
_NODE_KNOWN = {
    "anchor", "gist", "status", "kind", "coveredSeqs", "level", "role",
    "upper", "lower", "process", "start", "result", "lesson",
    "sunk", "supersededBy", "degraded", "merged", "lastUsedAt",
    "docPaths", "docRefs", "timeline",
}


def _seq_role(node: dict[str, Any]) -> tuple[int, str]:
    """(原文块号, 角色) —— 条目身份键（I1 保证 gist 按 seq 唯一）。"""
    a = node.get("anchor")
    seq = 0
    if isinstance(a, dict):
        try:
            seq = int(a.get("seq", 0) or 0)
        except (TypeError, ValueError):
            seq = 0
    return seq, str(node.get("role") or "gist")


def _content_of(node: dict[str, Any]) -> dict[str, Any]:
    """CausalNode → 行里的内容列（不含身份/编号）。

    [F9/F10] docPaths/docRefs/timeline 单独成列（渲染/展开直读行，不再翻 extra）。
    """
    tl = node.get("timeline")
    tl_from = tl_to = ""
    if isinstance(tl, dict):
        tl_from = str(tl.get("from") or "")
        tl_to = str(tl.get("to") or "")
    return {
        "gist": str(node.get("gist") or ""),
        "level": int(node.get("level", 1) or 1),
        "role": str(node.get("role") or "gist"),
        "anchor_seq": _seq_role(node)[0],
        "covered_seqs": json.dumps(node.get("coveredSeqs") or [], ensure_ascii=False),
        "status": str(node.get("status") or ""),
        "kind": str(node.get("kind") or ""),
        "process": str(node.get("process") or ""),
        "start_text": str(node.get("start") or ""),
        "result": str(node.get("result") or ""),
        "lesson": str(node.get("lesson") or ""),
        "sunk": 1 if node.get("sunk") else 0,
        "superseded_by": str(node.get("supersededBy") or ""),
        "doc_paths": json.dumps(node.get("docPaths") or [], ensure_ascii=False)
        if node.get("docPaths") else "",
        "doc_refs": json.dumps(node.get("docRefs") or [], ensure_ascii=False)
        if node.get("docRefs") else "",
        "timeline_from": tl_from,
        "timeline_to": tl_to,
        "extra": json.dumps({k: v for k, v in node.items() if k not in _NODE_KNOWN},
                            ensure_ascii=False),
    }


def sync_nodes(work_id: str, card_id: str, entries: list[dict[str, Any]]) -> None:
    """把 `entries`（CausalNode 数组）同步进 `memory_nodes`——编号体系的**写口**。

    [记忆编号体系 2026-10-29] 编号 `{work}/{card}/B{批}/{序}`（L2 为父编号 + `/d`）。

    - **已有条目**（按 `(anchor_seq, role)` 认）沿用原编号、批次与时间戳 ⇒ 编号稳定，
      不会因为数组重排而变；
    - **新条目**归入「下一批」（`现有最大批次 + 1`），整批共用**一个**时间戳；
    - 内容列一律以 `entries` 为准刷新（process 等后补字段能同步）；
    - 全量重建该卡的行，可重复执行。

    这是**派生写**：`memory.chain` 仍是凝练管线的事实源，本表供渲染/查询。
    两处都由本函数与 `load_nodes` 对齐，不再各自解析——上次「92 行 [块?]」
    正是装配与渲染各读一份不同结构造成的。
    """
    from . import card_db  # 延迟：本模块被 card_db 的调用方引用

    existing = load_nodes(work_id, card_id)
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    max_batch = 0
    max_ord: dict[int, int] = {}
    for r in existing:
        by_key[(int(r.get("anchor_seq") or 0), str(r.get("role") or "gist"))] = r
        b = int(r.get("batch") or 0)
        max_batch = max(max_batch, b)
        max_ord[b] = max(max_ord.get(b, 0), int(r.get("ordinal") or 0))

    keys = [_seq_role(n) for n in entries]
    has_new = any(k not in by_key for k in keys)
    new_batch = max_batch + 1 if has_new else 0
    now = datetime.now().isoformat(timespec="seconds")

    # 第一遍：定编号（L1 先落 id，L2 用父 id 拼 `/d`）
    id_of_seq: dict[int, str] = {}
    planned: list[tuple[dict[str, Any], str, int, int, str]] = []
    for node, key in zip(entries, keys):
        seq, _role = key
        level = int(node.get("level", 1) or 1)
        old = by_key.get(key)
        if old:
            nid = str(old.get("id") or "")
            batch = int(old.get("batch") or 0)
            ordv = int(old.get("ordinal") or 0)
            occurred = str(old.get("occurred_at") or "")
        else:
            batch = new_batch
            max_ord[batch] = max_ord.get(batch, 0) + 1
            ordv = max_ord[batch]
            occurred = now
            nid = f"{work_id}/{card_id}/B{batch:04d}/{ordv:04d}"
            if level > 1:
                nid += "/d"
        # [F2 · 2026-10-31 修] 记录**所有层**的 seq→编号（原只记 level≤1）：
        # 归拢降层后成员是 level=2，它名下的子条 upper 指向成员——只记 L1 会让
        # 这批子条的 upper_id 反查落空（渲染成孤儿）。锚点按 I1 全链唯一，首见即准。
        if seq not in id_of_seq:
            id_of_seq[seq] = nid
        planned.append((node, nid, batch, ordv, occurred))

    # 第二遍：拼行（upper_id 用第一遍定好的父编号）
    rows: list[dict[str, Any]] = []
    for node, nid, batch, ordv, occurred in planned:
        row: dict[str, Any] = {"id": nid, "work_id": work_id, "card_id": card_id,
                               "batch": batch, "ordinal": ordv,
                               "occurred_at": occurred, "upper_id": "",
                               "updated_at": now}
        row.update(_content_of(node))
        up = node.get("upper")
        if isinstance(up, list) and up and isinstance(up[0], dict):
            try:
                useq = int(up[0].get("seq", 0) or 0)
            except (TypeError, ValueError):
                useq = 0
            row["upper_id"] = id_of_seq.get(useq, "")
        rows.append(row)

    def _fn(conn: Any) -> None:
        conn.execute(_NODE_DDL)
        # [F9/F10] 旧表缺列的增量迁移（存在性检查防重复 ALTER）
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_nodes)").fetchall()}
        for col, ddl in _NODE_ALTERS:
            if col not in _cols:
                conn.execute(f"ALTER TABLE memory_nodes ADD COLUMN {col} {ddl}")
        conn.execute("DELETE FROM memory_nodes WHERE work_id=? AND card_id=?",
                     (work_id, card_id))
        if rows:
            cols = list(_NODE_COLS)
            conn.executemany(
                "INSERT OR REPLACE INTO memory_nodes(" + ",".join(cols) + ")"
                " VALUES(" + ",".join("?" * len(cols)) + ")",
                [[r[c] for c in cols] for r in rows])

    card_db._write_sync(work_id, _fn)  # noqa: SLF001 同包内复用写事务边界


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
    """写记忆：**只写 sqlite 的 `memory` 表**（唯一事实源）。

    [2026-10-27 · 用户令「所有都以一个事实源」] 改前写 JSON 文件；现在不再写文件，
    表是唯一落点。
    """
    async with _memory_lock(work_id, card_id):
        _save_to_db(work_id, card_id, data)
        # [记忆编号体系 2026-10-29] chain 落库后同步派生表 memory_nodes。
        # 渲染/查询只吃那张表，两处由 sync_nodes 一处对齐——不再各读一份。
        try:
            sync_nodes(work_id, card_id, data.get("entries") or [])
        except Exception as exc:  # noqa: BLE001 派生失败不阻断凝练落库
            logger.warning("[memory_nodes] 同步失败 work=%s card=%s: %s",
                           work_id, card_id, exc)


async def ensure_watermark(work_id: str, card_id: str) -> bool:
    """空水位建账（任务 1-2，幂等）：已有记忆文件不覆盖；该卡无会话不建。
    返回是否新建。"""
    # [2026-10-27] 建账判据改看**表里有没有行**（唯一事实源），不看文件。
    if _load_from_db(work_id, card_id) is not None:
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


# ---------------------------------------------------------------------------
# [F4 · 2026-10-31] 凝练全景渲染（dsh `renderCondenseMemory` 同位）
# ---------------------------------------------------------------------------

def render_condense_memory(entries: list[dict[str, Any]]) -> str:
    """凝练 prompt 用的「memory 全景」：判据 **level==1 ∧ !sunk ∧ role==gist**
    ＝ 全部 L1 行 ＋ 最近一批 L2 gist 行（dsh `isTopLevelEntry` 同口径）。

    ★ 与上下文基线渲染（context_regions.render_memory_region）**口径不同**：
    本渲染**看 sunk**（reclaim 沉没的条目不进凝练背景——F15 的沉没只控制凝练
    全景体积）；基线渲染不看 sunk。两处判据不得混用。
    空链 ⇒ 空串（调用方不注入全景头）。
    """
    rows = [e for e in (entries or []) if isinstance(e, dict)]
    tops: list[tuple[int, dict[str, Any]]] = []
    l2_nodes: list[dict[str, Any]] = []
    for e in rows:
        if str(e.get("role") or "gist") != "gist":
            continue
        level = int(e.get("level", 1) or 1)
        if e.get("sunk") is True:
            continue
        seq = _entry_anchor_seq(e) or 0
        if level <= 1:
            tops.append((seq, e))
        else:
            l2_nodes.append(e)
    if not tops and not l2_nodes:
        return ""
    lines: list[str] = []
    for seq, t in sorted(tops, key=lambda x: x[0]):
        lines.append(f"[seq {seq}][{t.get('status') or 'done'}][{t.get('kind') or 'fact'}] "
                     f"{t.get('gist') or ''}")
    if l2_nodes:
        # 「最近一批 L2」＝occurred_at 最晚的那一组（批级时间戳，同批同值）；
        # 无时间戳的旧条目全量给出（有界：L2 gist 行本来就短）。
        stamps = sorted({str(e.get("occurred_at") or "") for e in l2_nodes})
        latest = stamps[-1] if stamps and stamps[-1] else ""
        picked = [e for e in l2_nodes if str(e.get("occurred_at") or "") == latest] \
            if latest else l2_nodes
        for e in picked:
            seq = _entry_anchor_seq(e) or 0
            lines.append(f"  ∟ [seq {seq}] {e.get('gist') or ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# [F9 · 2026-10-31] docPaths/docRefs 机械归属（dsh extractToolUseArtifacts +
# attachDocPaths 同位）——从工具调用入参机械提真实路径（杀幻觉），按 coveredSeqs
# 归属、保留首现 seq 成 docRefs。原版 2026-09-24 已拆「路径须字面出现在窗口文本」
# 的裁决——代码不对产出做筛选，机械提取即全部。
# ---------------------------------------------------------------------------

#: 工具入参里的路径类键（机械提取面；值必须是字符串且非空）
_DOC_ARG_KEYS = frozenset({"path", "file_path", "filepath", "file"})


def extract_tool_use_artifacts(window: list[dict[str, Any]]) -> dict[int, list[str]]:
    """窗口内每块的 tool_calls 入参 → `{seq: [路径,...]}`（机械提取，不去重跨块）。"""
    out: dict[int, list[str]] = {}
    for m in window or []:
        if not isinstance(m, dict):
            continue
        tcs = m.get("tool_calls")
        if not isinstance(tcs, list):
            continue
        paths: list[str] = []
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    args = None
            if not isinstance(args, dict):
                continue
            for k, v in args.items():
                if k in _DOC_ARG_KEYS and isinstance(v, str) and v.strip() \
                        and v.strip() not in paths:
                    paths.append(v.strip())
        if paths:
            out.setdefault(int(m.get("seq") or 0), [])
            for p in paths:
                if p not in out[int(m.get("seq") or 0)]:
                    out[int(m.get("seq") or 0)].append(p)
    return out


def attach_doc_paths(nodes: list[dict[str, Any]],
                     artifacts: dict[int, list[str]]) -> int:
    """按 coveredSeqs 归属：节点覆盖到提了路径的块 ⇒ docPaths 收路径、docRefs 记
    首现 seq。就地改节点；返回挂上的节点数。"""
    if not artifacts:
        return 0
    touched = 0
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        paths: list[str] = []
        refs: list[dict[str, Any]] = []
        for s in (n.get("coveredSeqs") or []):
            try:
                si = int(s)
            except (TypeError, ValueError):
                continue
            for p in artifacts.get(si, []):
                if p not in paths:
                    paths.append(p)
                    refs.append({"path": p, "seq": si})
        if paths:
            n["docPaths"] = paths
            n["docRefs"] = refs
            touched += 1
    return touched


# ---------------------------------------------------------------------------
# [F10 · 2026-10-31] timeline 回填（dsh condense.ts:3488-3523 同位）：
# 节点 coveredSeqs → 事件时间 MIN/MAX → timeline{from,to}
# ---------------------------------------------------------------------------

def _origin_times(work_id: str, card_id: str, seqs: list[int]) -> dict[int, str]:
    """按块号查事件时间（ISO 串；取不到的块号不出键）。"""
    if not seqs:
        return {}
    from . import origin_store  # 延迟：避免 storage 内部循环
    try:
        span = origin_store.read_origin_span(work_id, card_id, min(seqs), max(seqs))
    except Exception as exc:  # noqa: BLE001 读不到不阻断，timeline 缺席
        logger.warning("[condense_fold] timeline 取时失败 work=%s card=%s: %s",
                       work_id, card_id, exc)
        return {}
    want = set(int(s) for s in seqs)
    out: dict[int, str] = {}
    for ev in (span.get("events") or []):
        seq = int(ev.get("seq") or 0)
        if seq not in want:
            continue
        data = ev.get("data")
        ts = ""
        if isinstance(data, dict):
            ts = str(data.get("ts") or "")
        if ts:
            out[seq] = ts
    return out


def _backfill_timeline(work_id: str, card_id: str, nodes: list[dict[str, Any]]) -> int:
    """给节点补 timeline{from,to}（coveredSeqs 内事件时间的 MIN/MAX）；就地改，
    返回补上的节点数。整批一次 span 读（不逐节点查库）。"""
    if not nodes:
        return 0
    all_seqs = sorted({int(s) for n in nodes for s in (n.get("coveredSeqs") or [])
                       if isinstance(s, (int, float))})
    if not all_seqs:
        return 0
    times = _origin_times(work_id, card_id, all_seqs)
    if not times:
        return 0
    filled = 0
    for n in nodes:
        if not isinstance(n, dict) or n.get("timeline"):
            continue
        ts = [times[int(s)] for s in (n.get("coveredSeqs") or [])
              if isinstance(s, (int, float)) and int(s) in times]
        if ts:
            n["timeline"] = {"from": min(ts), "to": max(ts)}
            filled += 1
    return filled


# ---------------------------------------------------------------------------
# [F17 · 2026-10-31] archive 冻结 + 一次性迁移（dsh migrateIfNeeded 门控同位）
# ---------------------------------------------------------------------------

async def migrate_archive_if_needed(work_id: str, card_id: str) -> None:
    """存量 archive → description 子条的一次性物化（幂等，带不变量门控）。

    [F17] 改前 `save_archive` 每批写 memory_archive——L2 已随批直接落链后，archive
    只剩历史存量，没有再写的理由（冻结）。本函数在凝练/归拢入口挂一次：
    - 已迁移（state.archive_migrated）或 archive 无行 ⇒ 只补标记、零写；
    - 物化走 memory_domain.migrate_archive（孤儿跳过、幂等）；
    - **不变量违例 ⇒ 拒绝写**（dsh 门控同口径），不标记、下轮再试；
    - 物化结果同时写回 entries（chain 是 entries 的派生物——只写链、下批
      chain_from_entries 会把子条重建没了）。
    """
    state = load_memory(work_id, card_id)
    if state.get("archive_migrated"):
        return
    rows = memory_domain.load_archive(work_id, card_id)
    archive_list = [{"seq": k[0], "process": (v or {}).get("process") or ""}
                    for k, v in (rows or {}).items()
                    if isinstance(k, tuple) and k[1] == "gist" and (v or {}).get("process")]
    if not archive_list:
        state["archive_migrated"] = True
        await save_memory(work_id, card_id, state)
        logger.info("[condense_fold] archive 冻结确认（无存量行）work=%s card=%s", work_id, card_id)
        return
    chain = memory_domain.load_chain(work_id, card_id)
    new_chain, report = memory_domain.migrate_archive(chain, archive_list)
    if report.get("violations"):
        logger.warning("[condense_fold] archive 迁移被不变量门控拒绝（不写、下轮再试）"
                       "work=%s card=%s 违例=%d: %s",
                       work_id, card_id, len(report["violations"]), report["violations"][:3])
        return
    if report.get("created"):
        memory_domain.save_chain(work_id, card_id, new_chain)
        state["entries"] = new_chain          # entries 与链同源推进（见上注）
        await save_memory(work_id, card_id, state)
        logger.info("[condense_fold] archive 一次性迁移完成 work=%s card=%s created=%d 孤儿=%d",
                    work_id, card_id, report["created"], report.get("skippedOrphans", 0))
    state["archive_migrated"] = True
    await save_memory(work_id, card_id, state)


# ---------------------------------------------------------------------------
# [F15 · 2026-10-31] reclaim 接线（时效回收，dsh maybeReclaim 挂 turn/end 同位）
# ---------------------------------------------------------------------------

async def reclaim_turn_end(work_id: str, card_id: str) -> None:
    """回合末时效回收：深层超 7d 未触达整子树沉没、顶层 done 超 3d（非最近 6 条）
    退常驻——标 sunk=True。★ 口径（方案 F15 复核修正）：sunk 只作存储层标记，
    **上下文基线渲染不过滤 sunk**（只看 level==1 ∧ role==gist）；sunk 只影响 F4
    凝练全景（render_condense_memory 的 !sunk 判据）。无 touch 账的条目豁免
    （零误伤优先），mark 不动、原文不动。"""
    try:
        chain = memory_domain.load_chain(work_id, card_id)
        if not chain:
            return
        now_ms = int(time.time() * 1000)
        new_chain, report = memory_lifecycle.reclaim(chain, now_ms)
        if report.get("newly_sunk"):
            memory_domain.save_chain(work_id, card_id, new_chain)
            state = load_memory(work_id, card_id)
            # entries 与链同源：把 sunk 位同步回 entries（链是派生物）
            sunk_by_key = {(int((n.get("anchor") or {}).get("seq") or 0),
                            str(n.get("role") or "gist"))
                           for n in new_chain if n.get("sunk")}
            changed = False
            for e in (state.get("entries") or []):
                if not isinstance(e, dict):
                    continue
                key = (_entry_anchor_seq(e) or 0, str(e.get("role") or "gist"))
                if key in sunk_by_key and not e.get("sunk"):
                    e["sunk"] = True
                    changed = True
            if changed:
                await save_memory(work_id, card_id, state)
        from ..infra import observe as _obs
        _obs.log_reclaim(work_id=work_id, card_id=card_id,
                         sunk_total=int(report.get("sunk_total") or 0),
                         newly_sunk=int(report.get("newly_sunk") or 0))
        if report.get("newly_sunk"):
            logger.info("[condense_fold] 时效回收 work=%s card=%s 新沉没=%d",
                        work_id, card_id, report["newly_sunk"])
    except Exception as exc:  # noqa: BLE001 回收零阻断
        logger.warning("[condense_fold] 时效回收失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)


def _entry_anchor_seq(e: dict[str, Any]) -> int | None:
    """条目锚点 seq（`anchor.seq` 优先，旧格式顶层 `seq` 兜底；取不出 ⇒ None）。"""
    a = e.get("anchor")
    if isinstance(a, dict):
        v = a.get("seq")
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    v = e.get("seq")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _merge_recondensed(existing: list[dict[str, Any]],
                       fresh: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """重凝幂等（F16 · 对齐 dsh「从不删节点」）：同 anchor.seq 的旧条目 → **gist
    原位替换 ＋ coveredSeqs 并集**（不删）；不同 seq 的旧条目原样不动，新条目追加。

    [2026-10-31] 原实现 `_drop_overlapping` 把覆盖相交的旧条目**物理删掉**——dsh
    的裁定是幂等跳过/原位更新（L1 终身单节点：同一事件一条 L1），节点与原文
    全程可查。原版靠 eventRef 实现，移植版无事件台账 ⇒ 用 anchor.seq 近似
    （同一原文块不重复出两条），这是最终形态、无需升级（方案 F16/F20 口径）。

    返回 `(合并后列表, 原位替换数)`；旧条目顺序保持，新条目按序追加在尾部。
    """
    out = [dict(e) if isinstance(e, dict) else e for e in (existing or [])]
    _existing_len = len(out)
    by_seq: dict[int, int] = {}
    for i, e in enumerate(out):
        if isinstance(e, dict):
            s = _entry_anchor_seq(e)
            if s is not None and s not in by_seq:
                by_seq[s] = i
    replaced = 0
    for n in fresh or []:
        if not isinstance(n, dict):
            continue
        s = _entry_anchor_seq(n)
        if s is not None and s in by_seq:
            i = by_seq[s]
            old = out[i]
            # [2026-11-01 · card_fn_005 批内同锚实证] 被替换槽位本身来自**本批**（索引
            # 越过存量段）⇒ 同一凝练批产出了两条同 anchor.seq 的不同节点，后者把
            # 前者的 gist 静默顶掉——重凝幂等（换旧条）是正当的，批内互顶是产出
            # 质量信号，必须可见而不是无声丢条目。
            if i >= _existing_len:
                logger.warning(
                    "[condense_fold] 批内同锚静默覆盖 seq=%s：本条 gist=%r 顶掉同批前条 gist=%r",
                    s, str(n.get("gist") or "")[:80],
                    str(old.get("gist") or old.get("conclusion") or "")[:80])
            cov = set(old.get("coveredSeqs") or []) | set(n.get("coveredSeqs") or [])
            merged = dict(n)
            merged["coveredSeqs"] = sorted(int(x) for x in cov)
            # 原位替换：保留旧条目的落账时刻（出生时间不改写），内容以新产出为准
            if old.get("occurred_at"):
                merged.setdefault("occurred_at", old["occurred_at"])
            out[i] = merged
            replaced += 1
        else:
            out.append(n)
            if s is not None:
                by_seq[s] = len(out) - 1
    return out, replaced


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


# ── [F8 · 2026-10-31 模型调用容错（对齐 dsh completeLlm）] ─────────────────────
# ★ [2026-10-31 用户令「所有的机制应该围绕调用反馈来做，是不是停下来了，看状态
#   就知道，根本不需要墙钟」「杀进程就解决问题了？杀进程任务就完成了？」]
#   本层**没有任何墙钟判死、没有任何机械杀任务**。首版实现塞过「静默判死 600s
#   ＋cancel」，当日拆除（与 10-23 取消 STREAM_IDLE_TIMEOUT 同一裁定）。
#   机制只剩三类，全部吃**调用反馈**：
#   - 真断连 ⇒ socket 立刻抛异常——反馈，重试梯消费；
#   - 真截断 ⇒ 端点回 finish_reason=length——反馈，截断信号消费；
#   - 慢（长思考/排队/本地模型）⇒ 帧在来——不是故障，一个字都不动它；
#   外加一条**观测日志**（无帧时长/帧数，纯状态汇报）——是不是停了，看状态就
#   知道。任务能不能完成靠**续跑**（步骤产出落盘缓存，见 _fold_step_cache_*），
#   不靠杀。
#: 重试梯：最多 30 次、每次间隔 10s（dsh completeLlm 同值；退避是重试节奏，
#: 不是判死钟——每次重试的前提都是端点/socket 给了真实失败反馈）。
CONDENSE_CALL_MAX_ATTEMPTS = 30
CONDENSE_CALL_BACKOFF_SEC = 10.0
#: 熔断：**连续** 3 次真实失败反馈（超时/连接类）⇒ 本批放弃（不再重试）。
CONDENSE_BREAKER_THRESHOLD = 3
#: 观测日志间隔（纯状态汇报：已等待/帧数/无帧时长；不参与任何裁决）。
CONDENSE_OBSERVE_LOG_SEC = 60.0


def _stall_sec() -> float:
    """流静默判死阈值（秒）。**判的是「无帧」不是「慢」**：帧还在流、再久也不
    动；零帧超阈 ⇒ socket 假活（连接开着但上游不再产帧——真断网 socket 会抛、
    假活永远不抛，2026-11-01 card_074 实测挂死 17min+）。处置＝取消挂起的调用、
    按可重试失败进既有重试梯（重发换新连接）。env CONDENSE_STALL_SEC 调，
    0＝回到纯观测旧语义。"""
    try:
        return float(os.environ.get("CONDENSE_STALL_SEC", "300") or 300)
    except (TypeError, ValueError):
        return 300.0

#: 可重试的失败类别（**端点/socket 的真实失败反馈**按消息机械归类；auth/配置类
#: 不进梯——网关自己已重试过，再挂 30×10s 只会白等）
_RETRYABLE_PAT = re.compile(r"timeout|timed out|超时|connection|503|rate|429|overload", re.I)


# [F8a · 2026-10-31 补口] 截断信号类移到 condense_two_layer（截断要从三段调用的
# 宽 except 里透传出去，定义在被吞的那一侧才拦得住）；此处 re-export 维持
# `memory_store.CondenseTruncated` 的既有引用面（_condense_one_batch / 测试）。
from .condense_two_layer import CondenseTruncated  # noqa: E402, F811


def compute_output_token_cap() -> int:
    """自适应输出帽（F8b · dsh computeOutputTokenCap 同位）。

    - `CONDENSE_OUTPUT_TOKEN_CAP` 未设 ⇒ **0＝不设帽**（拆闸铁律：网关不注入
      默认 max_tokens；缺省行为与改前逐字一致）；
    - 设为 ``auto`` ⇒ 按窗口预算自适应：min(窗口预算 // 4, 32768)；
    - 设为数字 ⇒ 该值。
    """
    raw = os.environ.get("CONDENSE_OUTPUT_TOKEN_CAP", "").strip()
    if not raw:
        return 0
    if raw.lower() == "auto":
        return min(window_budget_tokens() // 4, 32768)
    try:
        v = int(raw)
        return v if v > 0 else 0
    except ValueError:
        logger.warning("[condense_fold] CONDENSE_OUTPUT_TOKEN_CAP 非法（%r），不设帽", raw)
        return 0


def _retryable_error(exc: BaseException) -> bool:
    return bool(_RETRYABLE_PAT.search(str(exc) or ""))


# [处置 · 2026-10-31] 失效/断连分类器与截断信号同址（condense_two_layer）——
# run_two_layer 的重试逻辑吞异常，fatal 必须在被吞之前识别；此处 re-export。
from .condense_two_layer import model_error_disposition as _model_error_disposition  # noqa: E402


def _report_to_user(work_id: str, card_id: str, what: str) -> None:
    """后台凝练/归拢线把「无法自行恢复」的失败**报给用户**（事件面，UI 可见）。

    改前这些失败全部吞在 logger 里（用户永不可见）——审计与用户令共同指向的缺口。
    频控天然存在：fatal 触发 F14 失败账 30min 退避 ⇒ 同卡至多 30min 报一次。
    """
    try:
        from ..api import event_stream
        event_stream.emit(work_id, card_id, "error",
                          {"error": f"[记忆凝练] {what}"[:200],
                           "error_type": "CondenseFatal",
                           "source": "condense_fold"})
    except Exception as exc:  # noqa: BLE001 报错通道坏了也不阻断
        logger.warning("[condense_fold] 用户报错通道失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)


async def _guarded_model_call(make_call, *, work_id: str, card_id: str,
                              stage: str, trace_id: str = "") -> dict[str, Any]:
    """[F8 · 2026-10-31 模型调用容错核心（对齐 dsh completeLlm），凝练/归拢共用]。

    `make_call(on_chunk, trace_id) -> (result, model)`：由调用方注入实际调用
    （凝练三段 / 归拢三步各自带 system/tools）。全部机制**只吃调用反馈**：

    - **截断必抛**（F8a）：端点回 finish_reason=length / 截断的 tool_call ⇒ 抛
      `CondenseTruncated`——整批作废、游标不动、留痕（上次归拢 step3 烧 145k
      token 全被截断、正文 0，就是这个病）；
    - **重试梯**：socket/端点的真实失败反馈（超时/连接/503/429）最多 30 次、
      每次隔 10s；非可重试类（auth/配置）如实上抛（网关自己已重试过）；
    - **熔断**：连续 3 次真实失败 ⇒ 放弃本批；
    - **观测日志**（纯状态、不裁决）：每分钟一行「已等待/帧数/无帧时长」——
      是不是停了，看状态就知道。慢（长思考/排队）不是故障，绝不按钟杀；
      任务断了靠续跑（步骤产出缓存）接着付，不靠杀进程"止损"；
    - **无帧判死**（`_stall_sec`，默认 300s）：帧还在流、再久也不动——这是
      「按钟杀」与「判死」的分界；零帧超阈 ⇒ socket 假活（连接未断但上游不产
      帧，真断网才会抛错），取消挂起调用、计一次可重试失败进梯重发。
    """
    from ..infra import observe as _obs
    from ..api import event_stream as _es

    _trace = trace_id or _obs.new_trace_id()
    # [2026-11-01 逐字流脱钩] 本壳服务的全是管线内部调用（凝练批/归拢组）——
    # 它们不该出现在任何卡的对话流。但回合末 create_task 会继承卡回合的
    # stream_ctx，其模型帧被归到那张卡名下：reasoning 落对话流、content 帧
    # 进实时 token 流（card_104 实案：L2 子题生成过程混进了用户可见的推理区）。
    # 全程脱钩；进出本壳即还原，不污染调用方上下文。
    _stream_tok = _es.clear_stream_context()
    try:
        consecutive_fail = 0
        last_err: BaseException | None = None
        for attempt in range(1, CONDENSE_CALL_MAX_ATTEMPTS + 1):
            chunk_state = {"last": time.monotonic(), "n": 0}

            def _on_chunk(_t: str) -> None:      # 流式 chunk 计数（观测数据源）
                chunk_state["n"] += 1
                chunk_state["last"] = time.monotonic()

            call_task = asyncio.ensure_future(make_call(_on_chunk, _trace))
            # 观测循环：每分钟一行状态；另加**无帧判死**——帧还在走永不触发，
            # 零帧超阈 ⇒ socket 假活，取消挂起的调用当可重试失败进梯（重发换新连接）。
            t0 = time.monotonic()
            _stall_exc: BaseException | None = None
            while True:
                _done, _pending = await asyncio.wait({call_task},
                                                     timeout=CONDENSE_OBSERVE_LOG_SEC)
                if call_task in _done:
                    break
                _no_chunk = time.monotonic() - chunk_state["last"]
                logger.info(
                    "[condense_fold] 调用观测 work=%s card=%s stage=%s 已等待=%.0fs "
                    "帧=%d 无帧=%.0fs（纯观测，不裁决）",
                    work_id, card_id, stage, time.monotonic() - t0,
                    chunk_state["n"], _no_chunk)
                _stall = _stall_sec()
                if _stall > 0 and _no_chunk > _stall:
                    _stall_exc = TimeoutError(
                        f"流式调用疑似超时（{_no_chunk:.0f}s 零响应帧——socket 假活，"
                        "连接未断但上游不再产出），判死重发")
                    call_task.cancel()
                    break
            if _stall_exc is not None:
                # 等取消落地最多 5s——任务若吞了取消继续跑，不等它（结果已不需要，
                # 让它自己在后台凉），随后照常进重试梯换新连接。
                _wd, _wp = await asyncio.wait({call_task}, timeout=5.0)
                if _wp:
                    logger.warning("[condense_fold] 挂死任务取消后 5s 未落地，弃置后台 "
                                   "work=%s card=%s", work_id, card_id)
                    call_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                last_err = _stall_exc
                consecutive_fail += 1
                logger.warning("[condense_fold] 模型调用假活判死（attempt=%d/%d，连续失败=%d，"
                               "%.0fs 后重试换新连接）work=%s card=%s: %s",
                               attempt, CONDENSE_CALL_MAX_ATTEMPTS, consecutive_fail,
                               CONDENSE_CALL_BACKOFF_SEC, work_id, card_id, _stall_exc)
                if consecutive_fail >= CONDENSE_BREAKER_THRESHOLD:
                    raise RuntimeError(
                        f"连续 {consecutive_fail} 次真实失败反馈（超时/连接类），熔断本批") from _stall_exc
                await asyncio.sleep(CONDENSE_CALL_BACKOFF_SEC)
                continue
            try:
                result, _model = call_task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if not _retryable_error(exc):
                    raise                        # auth/配置类：网关已重试过，如实上抛
                consecutive_fail += 1
                logger.warning("[condense_fold] 模型调用失败反馈（可重试，attempt=%d/%d，"
                               "连续失败=%d，%.0fs 后重试）work=%s card=%s: %s",
                               attempt, CONDENSE_CALL_MAX_ATTEMPTS, consecutive_fail,
                               CONDENSE_CALL_BACKOFF_SEC, work_id, card_id, exc)
                if consecutive_fail >= CONDENSE_BREAKER_THRESHOLD:
                    raise RuntimeError(
                        f"连续 {consecutive_fail} 次真实失败反馈（超时/连接类），熔断本批") from exc
                await asyncio.sleep(CONDENSE_CALL_BACKOFF_SEC)
                continue
            # F8a：截断识别（finish_reason=length ＝ max-tokens 掐断——端点反馈）
            if (result or {}).get("finish_reason") == "length" or (result or {}).get("truncated_tool_call"):
                raise CondenseTruncated(
                    f"产出被 max-tokens 截断（finish_reason={(result or {}).get('finish_reason')}）"
                    "——整批作废、游标不动")
            consecutive_fail = 0
            return result or {}
        raise RuntimeError(f"模型调用重试 {CONDENSE_CALL_MAX_ATTEMPTS} 次仍失败: {last_err}")
    finally:
        _es.reset_stream_context(_stream_tok)


async def _condense_call(user_prompt: str, system: str | None = None, *,
                         trace_id: str = "", work_id: str = "", card_id: str = "",
                         stage: str = "") -> str:
    """凝练模型调用（任务 3）：call_main 纯文本（prior_messages=[]、tools=None）。

    测试经 monkeypatch 本接缝注入脚本化产出——MockGateway 罐头内容无
    【结论】/【依据】形态。
    [三步凝练 2026-10-29] 加 `system` 形参：S2a / L1 / L2 三段各用各的 system；
    缺省 None ⇒ 沿用单段 `CONDENSE_SYSTEM_PROMPT`。
    [F8 · 2026-10-31] 容错（反馈重试梯/熔断/截断信号/观测日志）下沉到
    `_guarded_model_call`，凝练与归拢共用同一套。无墙钟、无杀任务。
    """
    from ..api import model_config, model_service  # 延迟导入：storage 装载期不依赖 api（唯一调用点）

    cap = compute_output_token_cap()
    _stage = stage or "batch"
    _system = system or CONDENSE_SYSTEM_PROMPT
    _model_name = model_config.pick_one("condense")

    async def _make(on_chunk, trace: str):
        return await model_service.call_main(
            _system, user_prompt, prior_messages=[], tools=None,
            model_name=_model_name,
            on_chunk=on_chunk,
            trace_id=trace, caller=f"condense:{_stage}",
            work_id=work_id, card_id=card_id,
            max_tokens=cap or None)

    result = await _guarded_model_call(_make, work_id=work_id, card_id=card_id,
                                       stage=_stage, trace_id=trace_id)
    return str(result.get("content") or "")

async def _condense_one_batch(work_id: str, card_id: str,
                              window: list[dict[str, Any]], mark: int,
                              batch_no: int = 0) -> bool:
    """单批凝练（原管线本体逐段保留，[任务3] 批化拆出）：取段→模型写→解析→
    幂等判定→落 entries→推进 mark。返回该批是否成功（幂等补推/幂等跳过均算
    成功——窗口已凝过，游标应越过）。

    失败（调用异常/空产出/解析失败/截断）＝水位一分不动、warning 留痕一条、
    不阻断不设闸（V5 哲学/RD-04）；失败记账与退避归 condense_turn_end（F14）。

    [2026-10-31 本轮接入] F4 全景注入 / F9 docPaths 机械归属 / F10 timeline 回填 /
    F16 重凝幂等（不删旧）/ F17 archive 冻结（不再每批写）/ F22 每批六口径打点。
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
    logger.info("[condense_fold] 批开始 work=%s card=%s 批号=%d 窗口块=%d 送凝练=%d块 prompt=%d字符",
                work_id, card_id, batch_no, len(window),
                sum(1 for m in window if str(m.get("content") or "").strip()),
                len(user_prompt))
    input_bytes = len(user_prompt.encode("utf-8"))
    fingerprint = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()[:16]
    # 任务 3：模型写三段产出（S2a 定界 / L1 三栏 / L2 细节）
    # [防挂死 2026-10-23 / 2026-10-31 F8 接管] 调用层容错（反馈重试梯/熔断/
    # 截断信号/观测）已下沉到 `_condense_call`（F8，无墙钟无杀任务）；
    # 这里的 CONDENSE_CALL_TIMEOUT 保留为
    # 「总时长超时」的显式旋钮（区分"慢"与"死"的中间态，缺省 0＝不设）。
    _call_timeout = float(os.environ.get("CONDENSE_CALL_TIMEOUT", "0") or 0)

    # [三步凝练 2026-10-29 · 照抄 dsh S2a→L1→L2] **模型零锚点义务**：它只判事件边界
    # （给切点 end）、只写三栏文字与细节子题，坐标一个都不给；覆盖区间与锚点由
    # condense_two_layer.assemble 机械算。⇒「同一 seq 两条结论」在结构上不可能出现，
    # L2 随批直接落库——不再依赖 process 事后物化（那条路撞 I1 就整批拒写，实测
    # card_001 85 条 process 一条 L2 都没落成）。
    from . import condense_two_layer as _tl

    # ★ 送凝的块必须用与 `_build_condense_user_prompt` **同一个判据**过滤
    #   （`is_condense_original_event`：原文且非工具块）。漏了这层，工具输出会被
    #   原样塞进 S2a/L1/L2 的 prompt——实测 card_066 因此实际请求体达 639 万 token
    #   （块计量只有 14.6 万，差 43.7 倍），模型直接拒「Prompt exceeds max length」，
    #   整批作废、水位不动。
    from . import predicates as _pred

    _blocks = [{"seq": int(m.get("seq") or 0), "content": str(m.get("content") or "")}
               for m in window
               if _pred.is_condense_original_event(m)
               and str(m.get("content") or "").strip()]

    # [超大单块处置 2026-10-31 · 二版] 单块超窗口预算 ⇒ 塞不进任何窗口。一版用过
    # 「锚点墓碑」（不凝、记一块牌子）——被用户纠正：重凝令的目的就是质量，
    # 50 万字上块牌子等于没干活。二版＝**分块凝**：把巨块按窗口半预算切段，
    # 每段作为独立送凝单元走完整三段管线；各段锚点取合成段 2_000_000_000+序号
    # （与归拢的 1e9 段分开），coveredSeqs 统一记原块号——结构不变量可通过
    # （I1 锚点唯一；I3 只查同父兄弟），mark 靠 coveredSeqs 含原块号越过。
    _win_budget = window_budget_tokens()
    _chunk_size = max(2_000, _win_budget // 2)          # 段大小＝窗口半预算
    _CHUNK_BASE = 2_000_000_000
    _tombstones: list[dict[str, Any]] = []               # 兼容旧名：现在是「分块替换表」
    _new_blocks: list[dict[str, Any]] = []
    for _b in _blocks:
        _btok = _msg_meter_tokens({"content": _b["content"]})
        if _btok <= _win_budget:
            _new_blocks.append(_b)
            continue
        # 切段（按行边界切，避免把一行撕成两半）
        _content = _b["content"]
        _lines = _content.splitlines(keepends=True) or [_content]
        _chunks: list[str] = []
        _buf, _buf_tok = [], 0
        for ln in _lines:
            _ltok = _msg_meter_tokens({"content": ln})
            if _buf and _buf_tok + _ltok > _chunk_size:
                _chunks.append("".join(_buf))
                _buf, _buf_tok = [], 0
            _buf.append(ln)
            _buf_tok += _ltok
        if _buf:
            _chunks.append("".join(_buf))
        logger.warning(
            "[condense_fold] 超大单块分块凝 work=%s card=%s seq=%s tok=%d → %d 段",
            work_id, card_id, _b["seq"], _btok, len(_chunks))
        _ori_seq = int(_b["seq"])
        for _ci, _ck in enumerate(_chunks):
            _new_blocks.append({"seq": _CHUNK_BASE + _ori_seq * 1000 + _ci,
                                "content": _ck,
                                "orig_seq": _ori_seq})
    _blocks = _new_blocks
    # 段锚点 → 原块号映射（装配后回填 coveredSeqs 用）
    _chunk_of = {int(b["seq"]): int(b.get("orig_seq") or b["seq"]) for b in _blocks}
    _has_chunks = any(int(b["seq"]) >= _CHUNK_BASE for b in _blocks)

    # [F4 · 凝练注入 memory 全景] 现算（dsh renderCondenseMemory 同位）：判据
    # level==1 ∧ !sunk ∧ role==gist ＋ 最近一批 L2。注入 S2a/L1/L2 三段消息头——
    # 凝练产出不再与旧结论重复/碎片化。★ 不复用上下文基线渲染（后者不看 sunk）。
    domain_full = render_condense_memory(
        (load_memory(work_id, card_id).get("entries") or []))
    _trace = _obs.new_trace_id()

    async def _call_stage(system: str, user: str) -> str:
        if _call_timeout > 0:
            return await asyncio.wait_for(
                _condense_call(user, system, trace_id=_trace, work_id=work_id,
                               card_id=card_id, stage=f"batch{batch_no}"),
                timeout=_call_timeout)
        return await _condense_call(user, system, trace_id=_trace, work_id=work_id,
                                    card_id=card_id, stage=f"batch{batch_no}")

    # [分块凝 · 按预算逐片] 巨块切段后**不能再合成一个巨 prompt**（那就回到
    # 巨提示老路）——按窗口预算切片，每片独立跑三段；各片产物汇总。
    _slices: list[list[dict[str, Any]]] = []
    if _has_chunks:
        _cur, _cur_tok = [], 0
        for _b in _blocks:
            _t = _msg_meter_tokens({"content": _b["content"]})
            if _cur and _cur_tok + _t > _win_budget:
                _slices.append(_cur)
                _cur, _cur_tok = [], 0
            _cur.append(_b)
            _cur_tok += _t
        if _cur:
            _slices.append(_cur)
    else:
        _slices = [_blocks]

    fresh_all: list[dict[str, Any]] = []
    descs_all: list[dict[str, Any]] = []
    try:
        for _si, _slice in enumerate(_slices):
            _res = await _tl.run_two_layer(
                _slice, _call_stage, session_id=card_id,
                domain_full=domain_full,
                )
            if not _res.get("ok"):
                logger.warning("[condense_fold] 分片 %d/%d 整批作废——水位不动 work=%s card=%s 阶段=%s",
                               _si + 1, len(_slices), work_id, card_id, _res.get("stage"))
                return False
            if _si == 0:
                fresh_all = list(_res.get("nodes") or [])
                descs_all = list(_res.get("descriptions") or [])
            else:
                fresh_all += list(_res.get("nodes") or [])
                descs_all += list(_res.get("descriptions") or [])
        _res = {"ok": True, "nodes": fresh_all, "descriptions": descs_all,
                "retries": 0, "stage": "done"}
    except CondenseTruncated as exc:
        # [F8a] 截断：整批作废、mark 不动、明确留痕（不再伪装成解析失败）
        logger.warning("[condense_fold] 产出截断——整批作废、水位不动 work=%s card=%s 批号=%d: %s",
                       work_id, card_id, batch_no, exc)
        return False
    except asyncio.TimeoutError:
        logger.warning("[condense_fold] 凝练模型 %.0fs 超时——该批放弃、水位不动 work=%s card=%s",
                       _call_timeout, work_id, card_id)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[condense_fold] 凝练失败——水位一分不动 work=%s card=%s 阶段=模型调用 原因=%s",
                       work_id, card_id, exc)
        # [处置 2026-10-31 · 用户令] 断连类＝留给重试梯/下轮；失效类＝报错给用户。
        if _model_error_disposition(exc) == "fatal":
            _report_to_user(work_id, card_id,
                            f"凝练模型无法继续使用（{str(exc)[:120]}）——请检查模型配置/key/额度；"
                            "修复前本卡凝练暂停。")
        return False
    if not _res.get("ok"):
        logger.warning("[condense_fold] 三步凝练整批作废——水位不动 work=%s card=%s 阶段=%s",
                       work_id, card_id, _res.get("stage"))
        return False
    fresh = list(_res.get("nodes") or [])
    if not fresh:
        logger.warning("[condense_fold] 凝练失败——水位一分不动 work=%s card=%s 阶段=产出解析 原因=空产出",
                       work_id, card_id)
        return False

    # L2 子题 → level=2 的 gist 子节点（口径见 memory_domain.attach_descriptions）。
    fresh, _l2_added = memory_domain.attach_descriptions(
        fresh, list(_res.get("descriptions") or []), card_id)
    # [分块凝] 段节点的 coveredSeqs 统一回填**原块号**（mark 前缀判定与追溯都认原块号）
    if _has_chunks:
        for _n in fresh:
            _cs = _n.get("coveredSeqs") or []
            _mapped = sorted({_chunk_of.get(int(x), int(x)) for x in _cs
                              if isinstance(x, (int, float))})
            if _mapped:
                _n["coveredSeqs"] = _mapped

    # [F9] docPaths/docRefs 机械归属：从本批窗口的 tool_calls 入参提真实路径，
    # 按 coveredSeqs 归属（不做字面过滤——原版 2026-09-24 裁定）。
    _doc_n = attach_doc_paths(fresh, extract_tool_use_artifacts(window))
    # [F10] timeline 回填：coveredSeqs → 事件时间 MIN/MAX。
    _tl_n = _backfill_timeline(work_id, card_id, fresh)

    # 水位只推进到**连续消耗的前缀**（dsh `consumedPrefix` 同口径）：S2a 可能只定
    # 前面一段、把拿不准的尾巴留给下一批，此时不能把 mark 推到窗口末尾。
    # ★ 前缀在**整个 window** 上走，规则（2026-10-31 收口）：
    #   - 有正文的原块：被节点覆盖才算消耗，否则前缀到此为止（拿不准的尾巴留给
    #     下一批，不硬销）；
    #   - 空内容块 / 工具块（A 与 B）：随批销账、不打断前缀——它们本就不进上下文
    #     也不进凝练（卡_024 空块卡死第一批、以及"工具块一次只爬一块"的低效都
    #     出在把这两类当断点）。
    _covered = {int(s) for n in fresh for s in (n.get("coveredSeqs") or [])}
    _consumed = 0
    for _m in window:
        _seq = int(_m.get("seq") or 0)
        if _pred.is_condense_original_event(_m) and str(_m.get("content") or "").strip():
            if _seq in _covered:
                _consumed += 1
            else:
                break
        else:
            _consumed += 1
    if _consumed == 0:
        logger.warning("[condense_fold] 三步凝练未覆盖窗口首块——水位不动 work=%s card=%s",
                       work_id, card_id)
        return False
    target_mark = int(window[_consumed - 1].get("seq") or 0) + 1
    # [补凝护栏] 巨块补凝发生在水位**之后**（块在 mark 后面）——目标水位不许回退
    target_mark = max(target_mark, int((load_memory(work_id, card_id).get("mark", 0) or 0)))
    logger.info("[condense_fold] 批完成 work=%s card=%s 耗时=%.1fs 结论=%d条 细节=%d条 "
                "doc挂=%d timeline补=%d mark %d→%d",
                work_id, card_id, time.time() - _t0,
                len(fresh) - _l2_added, _l2_added, _doc_n, _tl_n, mark, target_mark)
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
    # [F16 · 重凝幂等（2026-10-31 对齐 dsh「从不删节点」）] 同 anchor.seq 的旧条目
    # **原位替换 + coveredSeqs 并集**，不物理删除——旧实现 `_drop_overlapping`
    # 把覆盖相交的旧条目摘掉，节点与原文的对应关系随删丢失。
    merged_entries, _replaced = _merge_recondensed(existing, final_entries)
    if _replaced:
        logger.info("[condense_fold] 重凝幂等：窗口 %s..%s 原位替换 %d 条旧条目（不删）card=%s",
                    _win_from, _win_to, _replaced, card_id)
    await save_memory(work_id, card_id,
                      {"card_id": card_id, "mark": target_mark, "entries": merged_entries,
                       "batch_count": state.get("batch_count", 0),
                       "last_fold_check": state.get("last_fold_check", 0),
                       "folded": state.get("folded", [])})
    # [记忆因果链 2026-10-20] 条目 → 链落库，并跑 I1–I5 不变量（违例只记日志、不阻断）。
    # 链落 `memory` 表（dsh 同表：每会话一条 = 链 + 两个半开水位）；追溯走 coveredSeqs。
    try:
        chain = memory_domain.chain_from_entries(merged_entries, card_id,
                                                 folded_fingerprints=set(state.get("folded") or []))
        viol = memory_domain.check_invariants(chain)
        if viol:
            logger.warning("[memory_domain] 条目不变量违例 %d 条 card=%s: %s",
                           len(viol), card_id, viol[:3])
        memory_domain.save_chain(work_id, card_id, chain, condense_mark=target_mark)
        # [F17 · archive 冻结 2026-10-31] 不再每批写 memory_archive——L2 已随批直接
        # 落链/落 nodes 表，archive 只承载历史存量；存量经 migrate_archive_if_needed
        # 一次性物化（凝练入口挂，带不变量门控）。
    except Exception as exc:  # noqa: BLE001 链落库/校验失败不阻断凝练
        logger.warning("[memory_domain] 链落库/校验失败（不阻断）card=%s: %s", card_id, exc)
    # [F22] 每批六口径（batchNo/budget/windowSeqs/consumed/newMark/nodeCount/retries）
    _obs.log_condense_batch(
        work_id=work_id, card_id=card_id, batch_no=batch_no,
        budget=window_budget_tokens(), window_seqs=len(window), consumed=_consumed,
        new_mark=target_mark, node_count=len(final_entries),
        retries=int(_res.get("retries") or 0), stage="done",
        duration_ms=int((time.time() - _t0) * 1000))
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
        ok = await _condense_one_batch(work_id, card_id, window, mark,
                                       batch_no=batches + 1)
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


# ── [凝练折叠重构 2026-11-01 · 活线维护] fold-l1 归拢线 ──
# 判据改口径（用户裁定）：归拢合的是「同一件事/同一条线的重复呈现」，**不问事件是否
# 收尾**——治理线程的宿命是不结束，「只合已收尾」让活跃线的逐批新顶层永不降层。
# 管线两步半：s1 分组裁决（自由推理＋JSON 尾机械解析）→ s2' **每组一次**线程综合
# （带界产出）→ 机械提交（降层建边，原有件全复用）。s3 转填步退役：s2' 直接产带界
# 文本，代码机械解析——省一次调用、消掉工具流不发 [DONE] 的降级坑，「转填失败被记
# 成判断无果」的静默丢活也不再有。
# 机械骨架（代码钉死、不靠模型判）：
#   ①成员时序＝_group_order_key：真覆盖 seq 末端＞timeline.to＞按最老——合成锚点
#     （≥1e9 保留段）绝不当时序，否则旧 digest 会假装成最新态；
#   ②digest 身份＝全景里锚点≥1e9 的行标 [digest]，s1 知道它是可吸收同线增量的
#     既有归拢产物，不当普通增量拆；
#   ③最新态保护＝组内时序最末成员的「结果」是当前态，钉标写进 s2' 输入。
# 游标四态：committed(≥1组落库)/judge-empty → 推；pipeline-failure/abort → 不动。
# 触发双闸：批间隔 FOLD_EVERY_BATCHES 或顶层水位 ≥ fold_top_trigger()（自调节）。
FOLD_STEP1_PROMPT = (
    "你是记忆归拢器（fold-l1·第一步：分组裁决）。下方是某会话的「memory 全景」——"
    "全部可归拢顶层 L1 行与 L2 gist 行（不含 process 长正文）。\n\n"
    "# 任务\n"
    "通读全部顶层行，把「同一件事/同一条线」的条目分组——**无论这件事是否已收尾**。\n"
    "- 同一条活跃线的逐批增量也是同一件事，一样要合——这是归拢的主要工作；"
    "「已收尾」只是「最新态＝终态」的特例，不是合并前提；\n"
    "- 行上标 [digest] 的是既有归拢产物（合成的线程摘要）：它可以作为成员继续"
    "吸收同线的新增量再合并，不要把它当普通增量去拆；\n"
    "- 拿不准的不合：宁可散着留顶层，不为相近硬收敛；\n"
    "- 每组 ≥2 条；成员一律用渲染行上的 seq 标识（[seq N] 里的整数）。\n\n"
    "# 输出格式（硬性）\n"
    "- 先自由推理（分组依据、为什么这些是同一件事）；\n"
    "- **末两行固定**：倒数第二行写「结论：有合并」或「结论：无合并」；\n"
    "- 结论为「有合并」时，最后一行只输出一个 JSON 对象（无代码块、无解释），形如：\n"
    '  {"groups":[{"label":"一句话线程名","members":[seq整数,...]},...]}\n\n'
    "# 硬约束\n"
    "- 本步不写新 L1、不做综合；只裁决分组。"
)

FOLD_STEP2_GROUP_PROMPT = (
    "你是记忆归拢器（fold-l1·第二步：线程综合）。输入 = 一组被裁决为「同一件事」的"
    "记忆条目：成员 L1 各栏 + 各成员名下 L2 子条（gist+process 全文）。\n"
    "**成员已按真实时间排好序，首尾已钉标——不要自行改判哪条更早、哪条更新。**\n\n"
    "# 任务\n"
    "把这组成员综合成**一条**新 L1（线程 digest），栏位：\n"
    "- 起因：以标记为【最早成员】的起因/动机为准；\n"
    "- 经过：**全局因果重整**——不是成员经过的拼接。允许并鼓励重排因果线："
    "哪条增量其实是另条的因、哪条是被推翻的旧决定、哪里是弯路与回退；\n"
    "- 结果：**以标记为【最新成员】的结果/状态为准**——它是这条线的当前态。"
    "可润色表述，不得改立场、不得把更早成员的旧态当现状；\n"
    "- 教训：跨成员提取的模式（没有可省略）。\n\n"
    "# 权限与约束\n"
    "- 只依据所给材料，不臆造；细看后认为**本组不构成同一件事**＝合法判断，"
    "输出「【不合并】理由」即可；\n"
    "- 成员可以只取所给集合的**子集**（确有混进别线的条目时剔除即可），"
    "但不许点名材料里没有的 seq；\n"
    "- 材料仍不足以综合时，可请求展开：输出「【需展开：seq N,M】」（≤3 个、"
    "只点本组成员的 seq），系统回灌更深材料后让你重写一次——全程最多一次；\n"
    "- digest 是给人读的**当前视图**：写清这条线现在到哪一步，不要写成流水账。\n\n"
    "# 输出格式（硬性，带界产出）\n"
    "【成员】seq A, B, C（本组实际入 digest 的成员）\n"
    "【标题】一句话（这条线是什么）\n"
    "【起因】…\n"
    "【经过】…\n"
    "【结果】…\n"
    "【教训】…（可选）\n"
    "【kind】fact/decision/todo/exclusion/assumption 之一（可选）"
)


def fold_top_trigger() -> int:
    """顶层水位触发闸（自调节）：可归拢顶层 ≥N 即触发归拢，不等批间隔。
    归拢有效 ⇒ tops 压住 ⇒ 归拢变懒；积压失控 ⇒ 每轮凝练批后自动连发。"""
    try:
        return max(2, int(os.environ.get("CONDENSE_FOLD_TOP_TRIGGER", "40") or 40))
    except ValueError:
        return 40


def fold_group_cap() -> int:
    """单组综合的成员上限：超长线先合最老 N 条，最新增量留顶层、下轮自然续上。"""
    try:
        return max(2, int(os.environ.get("CONDENSE_FOLD_GROUP_CAP", "25") or 25))
    except ValueError:
        return 25


_FOLD_VERDICT_RE = re.compile(r"结论：\s*(有|无)\s*合并")


def _last_json_obj(text: str, want_key: str) -> dict[str, Any] | None:
    """从文本里捞最后一个含 `want_key` 的平衡花括号 JSON 对象。

    代码/散文里的 { } 干扰靠**括号配平**（含字符串转义感知）滤掉——
    s1 的 JSON 尾是自由推理后的最后一个对象，取「最后一个能解出且含键的」。
    """
    raw = str(text or "")
    best: dict[str, Any] | None = None
    i = 0
    while True:
        i = raw.find("{", i)
        if i < 0:
            break
        depth, in_str, esc, j = 0, False, False, i
        while j < len(raw):
            ch = raw[j]
            if esc:
                esc = False
            elif ch == "\\" and in_str:
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j < len(raw) and depth == 0:
            try:
                obj = json.loads(raw[i:j + 1])
                if isinstance(obj, dict) and want_key in obj:
                    best = obj
            except (ValueError, TypeError):
                pass
            i = j + 1
        else:
            i += 1
    return best


def _parse_fold_groups(text: str, top_seqs: set[int]) -> tuple[str, list[dict[str, Any]]]:
    """s1 产出机械解析 → (verdict, groups)。

    verdict ∈ {"merge", "empty", "broken"}：
    - "empty"：明说无合并；或给了组表但**有效组为零**（模型建议确实弱，
      与「无可合」同等处置＝诚实空转）；
    - "broken"：说了「有合并」却拿不出可解析组表、或连结论行都没有
      ——这是**管线失败不是判断无果**，调用方不许推游标。
    groups：members ⊆ top_seqs（点到不存在/不可归拢的 seq 剔除）、
    跨组先到先得去重（与提交侧 `_claimed` 同语义）、≥2 成员才留。
    """
    raw = str(text or "")
    m_last = None
    for m in _FOLD_VERDICT_RE.finditer(raw):
        m_last = m
    if m_last is None:
        return "broken", []
    if m_last.group(1) == "无":
        return "empty", []
    obj = _last_json_obj(raw, "groups")
    if not isinstance(obj, dict) or not isinstance(obj.get("groups"), list):
        return "broken", []
    claimed: set[int] = set()
    groups: list[dict[str, Any]] = []
    for g in obj["groups"]:
        if not isinstance(g, dict):
            continue
        members: list[int] = []
        for s in (g.get("members") or []):
            try:
                si = int(s)
            except (TypeError, ValueError):
                continue
            if si in top_seqs and si not in claimed:
                claimed.add(si)
                members.append(si)
        if len(members) >= 2:
            groups.append({"label": str(g.get("label") or ""), "members": members})
    if not groups:
        return "empty", []
    return "merge", groups


#: s2' 带界产出的栏目标记：【成员】【标题】【起因】…【需展开：seq N,M】——
#: 冒号可能在括号内（【需展开：…】），两种形态都认。
_FOLD_SEC_RE = re.compile(r"【(成员|标题|起因|经过|结果|教训|kind|不合并|需展开)\s*[：:】]")


def _parse_fold_digest(text: str, allowed: set[int],
                       card_id: str) -> tuple[str, Any]:
    """s2' 带界产出机械解析 → (kind, payload)：

    - ("merge", merge_dict)：covers 用 `allowed` 子集合成（≥2 才成组）；
    - ("bail", reason)：模型细看后诚实弃权（【不合并】）；
    - ("expand", [seqs])：请求更深材料（【需展开】，≤5、限本组成员锚点）；
    - ("bad", reason)：格式不可解析＝管线失败（调用方计 fail，不推游标）。
    """
    raw = str(text or "")
    secs: dict[str, str] = {}
    marks = list(_FOLD_SEC_RE.finditer(raw))
    for k, mk in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(raw)
        secs[mk.group(1)] = raw[mk.end():end].strip()
    if "需展开" in secs and "结果" not in secs:
        seqs = [int(x) for x in re.findall(r"\d+", secs["需展开"])][:5]
        return "expand", [s for s in seqs if s in allowed]
    if "不合并" in secs:
        return "bail", secs["不合并"][:200] or "细看后认为本组不构成同一件事"
    seen: set[int] = set()
    mems = [s for s in (int(x) for x in re.findall(r"\d+", secs.get("成员") or ""))
            if s in allowed and not (s in seen or seen.add(s))]
    if len(mems) < 2:
        return "bad", f"成员不足两条（{len(mems)}）"
    if not (secs.get("结果") or "").strip():
        return "bad", "缺【结果】栏"
    if not ((secs.get("起因") or "").strip() or (secs.get("经过") or "").strip()):
        return "bad", "缺【起因】/【经过】栏"
    kind = (secs.get("kind") or "").strip()
    return "merge", {
        "covers": [{"sessionId": card_id, "seq": s} for s in mems],
        "title": (secs.get("标题") or "").strip(),
        "start": (secs.get("起因") or "").strip(),
        "process": (secs.get("经过") or "").strip(),
        "result": secs["结果"].strip(),
        "lesson": (secs.get("教训") or "").strip(),
        "kind": kind if kind in memory_domain.KINDS else "fact",
    }


def _expand_for_fold(work_id: str, card_id: str, seqs: list[int],
                     page_chars: int = 6000) -> str:
    """s2' 展开钩子的回灌文本：沿锚点展开描述层＋有界原文（至多 3 条、各有界分页）。

    这是「归拢线有权限做进一步分析」的深材料出口：s2' 点名成员锚点，
    `memory_expand` 沿 coveredSeqs 拉回描述层/原文。有界往返（一轮一次），
    不是无限工具循环。
    """
    from . import memory_expand
    parts: list[str] = []
    for s in seqs[:3]:
        try:
            parts.append(memory_expand.render(
                memory_expand.expand(work_id, card_id, int(s), page_chars=page_chars)))
        except Exception as exc:  # noqa: BLE001 单条展开失败不阻断归拢
            parts.append(f"[seq {s}] 展开失败：{str(exc)[:120]}")
    return "\n\n".join(parts)


async def _fold_call(system: str, user: str,
                     tools: list[dict[str, Any]] | None = None,
                     *, work_id: str = "", card_id: str = "",
                     stage: str = "", trace_id: str = "") -> dict[str, Any]:
    """归拢模型调用（与凝练同走 bindings.condense 绑定）；返回**完整 result**
    （s1/s2' 一律产纯文本、取 content；tools 参数留作兼容形参）。

    [2026-10-31 · F8 接入] 上次归拢 step3 的两个死法（死等 1412s+ 无任何保护、
    烧 145k token 被 max-tokens 截断且正文 0）都源于这条裸调用——现在与凝练共用
    `_guarded_model_call`：反馈重试梯 / 截断必抛 / 熔断 / 观测日志同款
    （无墙钟、无杀任务）。
    """
    from ..api import model_config, model_service

    _model_name = model_config.pick_one("condense")
    _stage = stage or "fold"

    async def _make(on_chunk, trace: str):
        return await model_service.call_main(
            system, user, prior_messages=[], tools=tools,
            model_name=_model_name,
            on_chunk=on_chunk,
            trace_id=trace, caller=f"fold:{_stage}",
            work_id=work_id, card_id=card_id)

    return await _guarded_model_call(_make, work_id=work_id, card_id=card_id,
                                     stage=_stage, trace_id=trace_id)


def _foldable_tops(state: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """可归拢顶层条：`role=gist ∧ level=1 ∧ upper 空 ∧ sunk≠true`。

    [2026-11-01 · 判据放开] 去掉 `status=done` 闸——同一条活跃线的逐批增量
    也是「同一件事的重复呈现」，一样是归拢对象；「已收尾」不再是合并前提
    （治理卡的线程大多永不收尾，旧闸让活跃线顶层只增不降）。
    """
    out: list[tuple[int, dict[str, Any]]] = []
    for i, e in enumerate(state.get("entries") or []):
        if not isinstance(e, dict):
            continue
        if str(e.get("role") or "gist") != "gist":
            continue
        if int(e.get("level", 1) or 1) != 1:
            continue
        if e.get("upper"):
            continue
        if e.get("sunk") is True:
            continue
        out.append((i, e))
    return out


def _fold_children(entries: list[dict[str, Any]], parent: dict[str, Any]) -> list[dict[str, Any]]:
    """某顶层节点的子条（`upper` 指向父 anchor）。"""
    a = parent.get("anchor") or {}
    sid, seq = a.get("sessionId"), a.get("seq")
    out = []
    for e in entries:
        for u in (e.get("upper") or []):
            if isinstance(u, dict) and u.get("sessionId") == sid and u.get("seq") == seq:
                out.append(e)
                break
    return out


def _build_fold_messages(domain_full: str) -> str:
    """照抄 dsh `buildFoldMessages`：把 renderFoldStep1 的产出包成 user 消息正文。"""
    return f"【memory 全景】\n{domain_full}"


def _render_fold_step1(state: dict[str, Any]) -> str:
    """第一步输入（dsh `renderFoldStep1`）：顶层 L1 行 + 子条 gist 行，**不含 process**。

    子条行**不带 `[seq N]` 字面量**——第三步之外的代码用 `\[seq (\d+)\]` 从模型产出里
    提取可点名成员，子条若同款格式会被误点进来（dsh 原注：会落空成空指针合成节点）。
    """
    entries = state.get("entries") or []
    lines: list[str] = []
    for _i, t in _foldable_tops(state):
        seq = int((t.get("anchor") or {}).get("seq") or 0)
        tag = "digest" if seq >= SYNTH_SEQ_BASE else str(t.get("status") or "done")
        lines.append(f"[seq {seq}][{tag}][{t.get('kind') or 'fact'}] "
                     f"{t.get('gist') or ''}")
        for c in _fold_children(entries, t):
            lines.append(f"  ∟ 子条（不可点名）: {c.get('gist') or ''}")
    return "【因果链】\n" + "\n".join(lines)


def _render_fold_group(state: dict[str, Any],
                       members: list[dict[str, Any]]) -> str:
    """s2' 单组输入：成员按 `_group_order_key` **已排好序**传入，本函数只渲染。

    - 首/末机械钉标（【最早成员】/【最新成员】）——「哪条最新」是代码算的
      事实，不许模型自行改判（这是「结果栏＝最新态」的锚）；
    - 成员带全部栏位（起因/经过/结果/教训，含 L1 自己的 process）＋子条
      gist/process 全文——全局因果重整需要完整材料；
    - [digest] 标既有归拢产物（合成锚点 ≥1e9）——它是线程摘要不是普通增量。
    """
    entries = state.get("entries") or []
    n = len(members)
    lines = ["【成员材料】（已按真实时间排好序：首条最早、末条最新——顺序由系统钉死）"]
    for idx, t in enumerate(members):
        seq = int((t.get("anchor") or {}).get("seq") or 0)
        tag = "digest" if seq >= SYNTH_SEQ_BASE else str(t.get("status") or "done")
        pin = ""
        if idx == 0:
            pin = "【最早成员·「起因」以此为准】"
        elif idx == n - 1:
            pin = "【最新成员·「结果」以此为准】"
        lines.append(f"[seq {seq}][{tag}][{t.get('kind') or 'fact'}]{pin} "
                     f"{t.get('gist') or ''}")
        for k, label in (("start", "起因"), ("process", "经过"),
                         ("result", "结果"), ("lesson", "教训")):
            v = str(t.get(k) or "").strip()
            if v:
                lines.append(f"  {label}：{v}")
        for c in _fold_children(entries, t):
            cseq = int((c.get("anchor") or {}).get("seq") or 0)
            lines.append(f"  [seq {cseq}] 子条：{c.get('gist') or ''}")
            p = str(c.get("process") or "").strip()
            if p:
                lines.append(f"    {p}")
    return "\n".join(lines)


# ── [F1/F2 · 2026-10-31 对齐 dsh applyFoldToChain/freshSyntheticSeq] 归拢提交的
#    结构机械件：1e9 保留段锚点、子树降层、timeline 合并、按时间插入 ──

#: 合成锚点保留段起点（dsh `SYNTH_SEQ_BASE` 同值）：原文块号远到不了 1e9，
#: 归拢新锚点从这里起取，**绝不**取 covers 最旧 seq（必与成员锚点撞号 ⇒ I1）。
SYNTH_SEQ_BASE = 1_000_000_000


def _fresh_synthetic_seq(entries: list[dict[str, Any]]) -> int:
    """1e9 保留段取一个链上未占用的合成锚点（dsh `freshSyntheticSeq` 同口径）：
    起点＝1e9 ＋ 链上已有 ≥1e9 的节点数，撞号顺延。"""
    used = {int((e.get("anchor") or {}).get("seq") or 0) for e in entries or []
            if isinstance(e, dict)}
    synth_count = sum(1 for s in used if s >= SYNTH_SEQ_BASE)
    seq = SYNTH_SEQ_BASE + synth_count
    while seq in used:
        seq += 1
    return seq


def _covered_or_anchor(node: dict[str, Any]) -> list[int]:
    """节点的覆盖块号（`coveredSeqs` 优先，退化为锚点单点）。"""
    cov = node.get("coveredSeqs")
    if isinstance(cov, list) and cov:
        return [int(x) for x in cov if isinstance(x, (int, float))]
    return [int((node.get("anchor") or {}).get("seq") or 0)]


def _node_timeline(node: dict[str, Any]) -> dict[str, str] | None:
    """取节点 timeline{from,to}（没有＝None）。"""
    tl = node.get("timeline")
    if isinstance(tl, dict) and (tl.get("from") or tl.get("to")):
        return {"from": str(tl.get("from") or ""), "to": str(tl.get("to") or "")}
    return None


def _merge_timeline(tls: list[dict[str, str] | None]) -> dict[str, str] | None:
    """成员 timeline 的 min(from)/max(to)（dsh `mergeCoverTimeline` 同口径）；
    全员无 timeline ⇒ None（不臆造）。"""
    froms = [t["from"] for t in tls if t and t.get("from")]
    tos = [t["to"] for t in tls if t and t.get("to")]
    if not froms and not tos:
        return None
    return {"from": min(froms) if froms else (min(tos) if tos else ""),
            "to": max(tos) if tos else (max(froms) if froms else "")}


def _demote_subtree(entries: list[dict[str, Any]], root_seq: int) -> None:
    """把 `root_seq` 节点及其 lower 子树整体 level+=1（就地改）。

    dsh `applyFoldToChain`：成员挂 upper 后「子树 level+=1」——成员从顶层降到
    第 2 层，它名下的 L2 降到第 3 层，层层保持 I2 层级差。沿 lower 边走，
    seen 防环；找不到的子节点（悬空边）跳过。
    """
    by_seq: dict[int, list[dict[str, Any]]] = {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        by_seq.setdefault(int((e.get("anchor") or {}).get("seq") or 0), []).append(e)
    seen: set[int] = set()
    stack = [int(root_seq)]
    while stack:
        seq = stack.pop()
        if seq in seen:
            continue
        seen.add(seq)
        for e in by_seq.get(seq, []):
            try:
                e["level"] = int(e.get("level", 1) or 1) + 1
            except (TypeError, ValueError):
                e["level"] = 2
            for lo in (e.get("lower") or []):
                try:
                    ls = int((lo or {}).get("seq", -1))
                except (TypeError, ValueError):
                    continue
                if ls >= 0 and ls not in seen:
                    stack.append(ls)


def _timeline_sort_key(node: dict[str, Any]) -> str:
    """timeline 排序键（from 优先、to 兜底；都没有 ⇒ 排最后）。"""
    tl = _node_timeline(node)
    if tl:
        return tl.get("from") or tl.get("to") or ""
    return ""


def _insert_by_timeline(entries: list[dict[str, Any]], node: dict[str, Any]) -> list[dict[str, Any]]:
    """按 timeline **时间序插入**（dsh `insertByTimeline` 同口径）：插到第一个
    timeline 晚于自己的顶层节点之前；无 timeline 的节点不参与比较（保持原位），
    新节点自己也无 timeline 时 append 末尾。"""
    my_key = _timeline_sort_key(node)
    if not my_key:
        out = list(entries or [])
        out.append(node)
        return out
    out: list[dict[str, Any]] = []
    placed = False
    for e in entries or []:
        if not placed:
            k = _timeline_sort_key(e)
            # 只与「有 timeline 的顶层 gist」比较；无 timeline 的行不挡插入位
            if k and k > my_key and int(e.get("level", 1) or 1) <= 1 \
                    and str(e.get("role") or "gist") == "gist":
                out.append(node)
                placed = True
        out.append(e)
    if not placed:
        out.append(node)
    return out


def _group_order_key(node: dict[str, Any]) -> tuple:
    """组内**真时序**键（排序＋钉首尾的机械事实）：

    - 有真实覆盖 seq（<1e9）⇒ 按覆盖末端排（最硬的时序信号）；
    - 只有 timeline ⇒ 按 timeline.to 排在真实覆盖之前（不可全信的按偏老）；
    - 都不可断代 ⇒ 排最老——**宁可钉成最早成员，绝不把来路不明的当成最新态**。

    合成锚点（≥1e9 保留段）绝不当时序信号：fold digest 的锚点天生「最大」，
    拿它排时序会让旧 digest 每次都假装成最新态、「结果」栏写歪。
    """
    seqs = [s for s in _covered_or_anchor(node) if int(s) < SYNTH_SEQ_BASE]
    if seqs:
        return (2, max(seqs), "")
    tl = _node_timeline(node)
    if tl and tl.get("to"):
        return (1, 0, tl["to"])
    return (0, 0, "")


async def _maybe_fold(work_id: str, card_id: str) -> None:
    """归拢触发与执行（双闸：批间隔 ≥FOLD_EVERY_BATCHES 或顶层水位 ≥fold_top_trigger）。

    管线：**s1 分组裁决 → s2' 每组一次线程综合（带界产出）→ 机械提交**。
    游标四态：committed(≥1组落库)/judge-empty → 推；pipeline-failure/abort → 不动
    （s1 说可合却拿不出组表、或全组机械失败零提交——管线失败不许记成判断无果）。
    """
    # [F17] 归拢入口挂 archive 一次性迁移（幂等；违例被门控拒绝时仅留痕）
    try:
        await migrate_archive_if_needed(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 迁移失败不挡归拢
        logger.warning("[condense_fold] archive 迁移失败（不挡归拢）work=%s card=%s: %s",
                       work_id, card_id, exc)
    state = load_memory(work_id, card_id)
    count = int(state.get("batch_count", 0) or 0)
    last = int(state.get("last_fold_check", 0) or 0)
    tops = _foldable_tops(state)
    if (count - last < FOLD_EVERY_BATCHES) and (len(tops) < fold_top_trigger()):
        return
    if len(tops) < 2:
        state["last_fold_check"] = count
        await save_memory(work_id, card_id, state)
        logger.info("[condense_fold] 归拢判断空转（fold-l1.judge-empty tops<2，零模型）work=%s card=%s 批数=%d",
                    work_id, card_id, count)
        return

    # [续跑基建 2026-10-31 · 用户令「杀进程任务就完成了？」] 步骤产出落盘缓存：
    # 断在哪一步，重跑就从那一步**续付**，s1/s2 的几十万 token 不用重交。
    # 缓存键＝可归拢集指纹（顶层 seq+gist 哈希）——链一变（提交/新凝）指纹即变，
    # 旧缓存自动失效，不会拿旧判断污染新链。step3 是转填、便宜，不缓存。
    _fp = hashlib.sha256("\n".join(
        f"{int((t.get('anchor') or {}).get('seq') or 0)}:{t.get('gist') or ''}"
        for _i, t in tops).encode("utf-8")).hexdigest()[:16]
    _cache_dir = session_store.WORKS_DIR.parent / "logs" / "fold_cache" / work_id / card_id
    try:
        _cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    def _cache_path(step: str) -> Path:
        return _cache_dir / f"{step}-{_fp}.txt"

    def _cache_read(step: str) -> str:
        try:
            txt = _cache_path(step).read_text(encoding="utf-8")
            if txt.strip():
                logger.info("[condense_fold] 归拢 step%s 复用缓存（指纹 %s，续跑不重付）"
                            "work=%s card=%s", step, _fp, work_id, card_id)
                return txt
        except OSError:
            pass
        return ""

    def _cache_write(step: str, text: str) -> None:
        try:
            _cache_path(step).write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("[condense_fold] 步骤缓存写失败（不阻断）%s: %s",
                           _cache_path(step), exc)

    # ── s1 分组裁决（自由推理＋JSON尾）───────────────────────────────────
    text1 = _cache_read("s1")
    if not text1:
        try:
            r1 = await _fold_call(FOLD_STEP1_PROMPT,
                                  _build_fold_messages(_render_fold_step1(state)),
                                  work_id=work_id, card_id=card_id, stage="s1")
        except Exception as exc:  # noqa: BLE001 abort：游标不动（失败不消费触发线）
            logger.warning("[condense_fold] 归拢失败——游标不动（abort，s1）work=%s card=%s 原因=%s",
                           work_id, card_id, exc)
            if _model_error_disposition(exc) == "fatal":
                _report_to_user(work_id, card_id,
                                f"归拢模型无法继续使用（{str(exc)[:120]}）——请检查模型配置/key/额度。")
            return
        text1 = str(r1.get("content") or "")
        _cache_write("s1", text1)
        logger.info("[condense_fold] 归拢 s1 分组完成 work=%s card=%s 产出=%d 字 思考帧=%s",
                    work_id, card_id, len(text1), r1.get("tokens_used"))
    top_seqs = {int((t.get("anchor") or {}).get("seq") or 0) for _i, t in tops}
    verdict, groups = _parse_fold_groups(text1, top_seqs)
    if verdict == "empty":
        state["last_fold_check"] = count
        await save_memory(work_id, card_id, state)
        logger.info("[condense_fold] 归拢判断无果（fold-l1.judge-empty）work=%s card=%s 批数=%d",
                    work_id, card_id, count)
        return
    if verdict == "broken":
        # 管线失败 ≠ 判断无果：s1 说可合却拿不出可解析组表（或连结论行都没写）——
        # 不许推游标；s1 产出已缓存，下轮同指纹直接复用、只重付后续步骤。
        logger.warning("[condense_fold] 归拢 s1 产出不可解析（pipeline-failure，游标不动）"
                       "work=%s card=%s 产出=%d 字", work_id, card_id, len(text1))
        return
    by_seq = {int((t.get("anchor") or {}).get("seq") or 0): t for _i, t in tops}
    cap = fold_group_cap()

    # ── s2' 每组一次线程综合（并行，信号量 3）─────────────────────────────
    # 每组独占一次调用的注意力：输入＝按 _group_order_key 排好的成员材料
    # （首尾钉标），产出＝带界文本或【不合并】/【需展开：seq N,M】（有界往返一次）。
    # 组产出按成员指纹落盘缓存（g{idx}-{成员哈希}[-{fp}].txt），重跑免重付。
    sem = asyncio.Semaphore(3)

    async def _one_group(idx: int, g: dict[str, Any]) -> tuple[str, Any]:
        async with sem:
            mems = sorted((by_seq[s] for s in g["members"] if s in by_seq),
                          key=_group_order_key)
            if len(mems) > cap:
                mems = mems[:cap]   # 超长线先合最老段；最新增量留顶层下轮自然续上
            allowed = {int((m.get("anchor") or {}).get("seq") or 0) for m in mems}
            gfp = hashlib.sha256(
                ",".join(str(s) for s in sorted(allowed)).encode("utf-8")).hexdigest()[:12]
            tag = f"g{idx}-{gfp}"
            step2_in = (f"【线程】{g.get('label') or ''}\n\n"
                        + _render_fold_group(state, mems))
            text2 = _cache_read(tag + "x")
            if not text2:
                text2 = _cache_read(tag)
                if not text2:
                    try:
                        r2 = await _fold_call(FOLD_STEP2_GROUP_PROMPT, step2_in,
                                              work_id=work_id, card_id=card_id,
                                              stage=tag)
                    except Exception as exc:  # noqa: BLE001 组调用失败＝fail（游标见尾态）
                        if _model_error_disposition(exc) == "fatal":
                            _report_to_user(work_id, card_id,
                                            f"归拢模型无法继续使用（{str(exc)[:120]}）"
                                            "——请检查模型配置/key/额度。")
                        return "fail", f"组{idx} 调用失败：{str(exc)[:120]}"
                    text2 = str(r2.get("content") or "")
                    _cache_write(tag, text2)
                k0, p0 = _parse_fold_digest(text2, allowed, card_id)
                if k0 == "expand" and p0:
                    # 展开钩子（有界往返一次）：点名成员锚点 → 深材料回灌 → 重写一次。
                    # 缓存在 tag"x" 名下——首产与往返产物分开，续跑不重付。
                    exp_in = (step2_in
                              + "\n\n【展开材料】（按你的【需展开】请求回灌）\n"
                              + _expand_for_fold(work_id, card_id, p0)
                              + "\n\n——已回灌；请按原格式输出 digest 或【不合并】。")
                    try:
                        r2b = await _fold_call(FOLD_STEP2_GROUP_PROMPT, exp_in,
                                               work_id=work_id, card_id=card_id,
                                               stage=tag + "x")
                    except Exception as exc:  # noqa: BLE001
                        if _model_error_disposition(exc) == "fatal":
                            _report_to_user(work_id, card_id,
                                            f"归拢模型无法继续使用（{str(exc)[:120]}）"
                                            "——请检查模型配置/key/额度。")
                        return "fail", f"组{idx} 展开往返失败：{str(exc)[:120]}"
                    text2 = str(r2b.get("content") or "")
                    _cache_write(tag + "x", text2)
            kind_, payload = _parse_fold_digest(text2, allowed, card_id)
            if kind_ == "merge":
                return "merge", payload
            if kind_ == "bail":
                return "bail", f"组{idx} 弃权：{payload}"
            if kind_ == "expand":
                return "fail", (f"组{idx} 展开请求未成立：seq 全越界（不在本组）"
                                "或往返配额已用完（每组限一次）")
            return "fail", f"组{idx} 产出不可解析：{payload}"

    results = await asyncio.gather(
        *[_one_group(i, g) for i, g in enumerate(groups)], return_exceptions=True)
    merges: list[dict[str, Any]] = []
    n_fail = 0
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            n_fail += 1
            logger.warning("[condense_fold] 组%d 综合异常 work=%s card=%s: %s",
                           i, work_id, card_id, res)
            continue
        st_, payload = res
        if st_ == "merge":
            merges.append(payload)
        else:
            if st_ == "fail":
                n_fail += 1
            logger.info("[condense_fold] %s work=%s card=%s", payload, work_id, card_id)
    logger.info("[condense_fold] s2' 组综合完成 work=%s card=%s 组=%d 成=%d 弃权=%d 失败=%d",
                work_id, card_id, len(groups), len(merges),
                len(groups) - len(merges) - n_fail, n_fail)

    # ── 提交：照抄 dsh `applyFoldToChain`（F2 归拢＝**降层建边**，弃 sunk 标记法）
    #    代码不裁判模型产出（2026-09-24 裁定「所有代码校验都拆掉」）：字段缺省补空、
    #    kind 缺省 fact、covers 原样透传，不做「成员是否存在 / 成员数≥2」之类拒绝。
    #    与旧实现（打 sunk=True + append 末尾）的三处结构差（对比文档 P0-1/P0-2）：
    #    ① 成员 `upper=[新锚点]`、沿 lower 子树 level+=1——可见性靠 level（渲染顶层
    #       判据 level==1 ∧ role==gist），不再靠 sunk；
    #    ② 新锚点取 **1e9 保留段**（dsh `freshSyntheticSeq` 同款）——取 covers 最旧 seq
    #       必与成员锚点撞号（I1 gist 身份重复），原版注释点名禁止；
    #    ③ 新节点按 timeline **insertByTimeline**（时间序插入，不 append 末尾）。
    #    ④ [2026-11-01] 认领后成员 <2 ⇒ 跳过（不产空壳 digest）；新节点 gist 取
    #       【标题】（s2' 给的一句话线程名），无标题才退回 起因+结果 拼接。
    entries = state.get("entries") or []
    n_merged_nodes = 0
    n_members = 0
    # [2026-11-01 · 修重叠认领] 模型的多组 merge 可能 covers 重叠（实测 card_076：
    # 6 个成员被两组同时认领）⇒ 顺序提交时后组改写成员 upper、先组 lower 全悬空。
    # 处置：**先认领者得**——已被前面 merge 认领的成员从后面各组剔除（lower 同步
    # 剔除，fold 节点的 lower 只含它真正降层的成员），I1 不再悬空。
    _claimed: set[int] = set()
    for m in merges:
        seqs = [int(c["seq"]) for c in (m.get("covers") or [])
                if isinstance(c, dict) and isinstance(c.get("seq"), int)]
        members = [by_seq[s] for s in seqs if s in by_seq and s not in _claimed]
        if len(members) < 2:
            continue   # 认领后不足两条 ⇒ 不产空壳 digest（成员已被别组认领/不存在）
        _claimed.update(int((mem.get("anchor") or {}).get("seq") or 0) for mem in members)
        anchor_seq = _fresh_synthetic_seq(entries)
        kind = str(m.get("kind") or "fact")
        start = str(m.get("start") or "")
        result = str(m.get("result") or "")
        node = memory_domain.new_node(
            card_id, anchor_seq,
            str(m.get("title") or "").strip()
            or "\n".join(p for p in (start, result) if p),
            kind=kind if kind in memory_domain.KINDS else "fact",
            status="done")
        node["level"] = 1
        node["role"] = "gist"
        node["upper"] = []
        # lower＝成员锚点（I1 互链的另一半由成员 upper 回指本锚点承担）
        node["lower"] = [dict(mem.get("anchor") or {}) for mem in members]
        node["start"] = start
        node["process"] = str(m.get("process") or "")
        node["result"] = result
        node["lesson"] = str(m.get("lesson") or "")
        node["coveredSeqs"] = sorted({s for mem in members
                                      for s in _covered_or_anchor(mem)})
        _tl = _merge_timeline([_node_timeline(mem) for mem in members])
        if _tl is not None:
            node["timeline"] = _tl
        # [2026-11-01 · 补齐] 文档追溯并集：旧 fold 节点 docPaths/docRefs 全空，
        # 文档行在归拢处断链——成员字段并集是免费的，补上。
        _dps = sorted({p for mem in members for p in (mem.get("docPaths") or [])})
        _drs: list[dict[str, Any]] = []
        _dseen: set[tuple[str, int]] = set()
        for mem in members:
            for r in (mem.get("docRefs") or []):
                if isinstance(r, dict):
                    k = (str(r.get("path") or ""), int(r.get("seq") or 0))
                    if k not in _dseen:
                        _dseen.add(k)
                        _drs.append(r)
        if _dps:
            node["docPaths"] = _dps
        if _drs:
            node["docRefs"] = _drs
        node["fingerprint"] = hashlib.sha256(
            ("fold|" + "|".join(sorted(str((mem.get("anchor") or {}).get("seq")) for mem in members))
             ).encode("utf-8")).hexdigest()[:16]
        # 成员挂 upper=[新锚点] 并沿 lower 子树整体降一层（level+=1）
        for mem in members:
            _ma = mem.get("anchor") or {}
            mem["upper"] = [dict(node["anchor"])]
            # [2026-11-01 · 脏链修 card_fn_005/007 实测] 同锚复本＝同一逻辑条目
            # 的重复行（凝练存量病，同 seq 多条 gist）——认领按 seq 只解到一份，
            # 但 _demote_subtree 会把所有复本一起降层；不给复本挂 upper 就造出
            # 无父深条（I4）。同锚即同一身份：顶层复本随成员一起归到本 digest。
            for _sib in entries:
                if _sib is mem or not isinstance(_sib, dict):
                    continue
                _sa = _sib.get("anchor") or {}
                if (int(_sa.get("seq") or 0) == int(_ma.get("seq") or 0)
                        and str(_sa.get("sessionId") or "")
                        == str(_ma.get("sessionId") or "")
                        and int(_sib.get("level") or 1) <= 1
                        and not _sib.get("upper")):
                    _sib["upper"] = [dict(node["anchor"])]
            _demote_subtree(entries, int(_ma.get("seq") or 0))
            n_members += 1
        entries = _insert_by_timeline(entries, node)
        n_merged_nodes += 1
    if not n_merged_nodes:
        if n_fail == 0:
            # 全部组诚实弃权 / 产出有效但认领后无成组 → judge-empty → 推游标
            state["last_fold_check"] = count
            await save_memory(work_id, card_id, state)
            logger.info("[condense_fold] 归拢判断无果（fold-l1.judge-empty 全组弃权）"
                        "work=%s card=%s 批数=%d", work_id, card_id, count)
        else:
            # pipeline-failure：有组机械失败且零提交 → 游标不动，
            # 组产出已缓存（成功组免重付），下轮只续付失败组。
            logger.warning("[condense_fold] 归拢管线失败（%d 组失败、零提交）"
                           "——游标不动、下轮续跑 work=%s card=%s",
                           n_fail, work_id, card_id)
        return
    state["entries"] = entries
    state["last_fold_check"] = count
    await save_memory(work_id, card_id, state)
    # [观测 F22] 归拢动作留痕（JSONL），附带结构不变量体检（违例只记、不阻断）。
    try:
        _viol = memory_domain.check_invariants(state.get("entries") or [])
    except Exception:  # noqa: BLE001
        _viol = []
    if _viol:
        logger.warning("[memory_domain] 归拢后链不变量违例 %d 条 card=%s: %s",
                       len(_viol), card_id, _viol[:3])
    _obs.log_fold(work_id=work_id, card_id=card_id, merges=n_merged_nodes,
                  members=n_members, mark=int(state.get("mark", 0) or 0),
                  batch_count=count, violations=len(_viol))
    logger.info("[condense_fold] 归拢完成（fold-l1.committed·降层建边）work=%s card=%s "
                "merges=%d 成员=%d mark不动=%d 批数=%d",
                work_id, card_id, n_merged_nodes, n_members,
                int(state.get("mark", 0) or 0), count)


#: 进程内防重入（force 路径不走 session_facade 的锁，靠这个挡住自己和自己打架）
_CONDENSING: set[tuple[str, str]] = set()



def _read_origin(work_id: str, card_id: str, mark: int = 0) -> list[dict[str, Any]]:
    """凝练结算的**唯一读数入口**：只读原文库，按 seq 过滤。

    [2026-10-27 · 用户令「凝练水位的计算、凝练窗口的投递，应该都只以 sqlite
    的信息来结算」「哪些信息要进凝练窗口，就把这个信息存进 sqlite 就行了」]

    两处修正：
    - **来源**：改前读 md 文件（解析脆弱、可被清空），现在读 `session_blocks`；
    - **切片**：改前 `blocks[mark:]` 把 `mark`（seq 水位）当**列表下标**用，
      seq 有空洞就错位；现在按 `seq >= mark` 过滤。
    - **评审流量**：[2026-11-02 · 用户裁定] 评审过程消息不需要记忆——
      触发计量、凝练窗口、排水都看不到它（keep=0 全蒸发）；
      评审型卡整卡恒空。原文在 origin_store 可审计，此层只管凝练池。
    """
    from . import origin_store  # 延迟：避免 storage 内部循环
    from . import review_scope
    blocks = origin_store.read_blocks(work_id, card_id, from_seq=int(mark or 0))
    return review_scope.drop_stale_review(work_id, card_id, blocks, keep=0)


# [F14 · 2026-10-31 触发调度（对齐 dsh maybeCondense+pumpCondenseQueue）]
#: 全局并发闸（dsh `globalFlying`=1 的放宽）。
#: [2026-11-02 · card_154 实案] 单飞（N=1）在密集触发下**饿死**：card_154 触发
#: 16 次、每次都撞上别卡持位，70 分钟未凝量从 14 万涨到 142 万 token、最终
#: 撞穿 1M 窗口——串行化在多卡同时到线时不是排队，是永久跳过。改**有界并发**：
#: 同刻最多 ``_condense_parallel()`` 个凝练 job（默认 3，CONDENSE_PARALLEL 调）；
#: per-card 互斥（_CONDENSING）照旧——同卡永不并发，不同卡放行到 N。
_FLYING: dict[tuple[str, str], float] = {}
#: [2026-10-31 · 二轮审计] 持有者账（谁、何时起）——跳过告警与自愈的状态源。
#: 纯事实记录；「挂死」的判定与处置见 _flying_release_if_stale（默认关）。
#: [2026-11-02] 随有界并发改为 key→since 映射（每位持有者一条）。


def _condense_parallel() -> int:
    """全局凝练并发上限：CONDENSE_PARALLEL 覆盖，缺省 3。"""
    raw = os.environ.get("CONDENSE_PARALLEL", "").strip()
    if not raw:
        return 3
    try:
        v = int(raw)
        return v if v > 0 else 3
    except ValueError:
        logger.warning("[condense_fold] CONDENSE_PARALLEL 非法（%r），用缺省 3", raw)
        return 3


def _flying_release_sec() -> float:
    """只放锁不杀任务的处置阈值（秒）。**默认 900**——按用户令「根据状态做相应
    的处置」：持有者的在途调用 15 分钟无任何帧（实测凝练调用帧流连续，27 分钟
    全程无此间隙），状态判定为「不可能再运转」⇒ 处置＝放锁让别的卡继续＋报错
    给用户；**绝不 cancel 挂住的任务**（它若最终返回，finally 幂等清账）。
    CONDENSE_FLYING_RELEASE_SEC=0 可关（回到纯观测）；阈值可用 env 调。"""
    try:
        return float(os.environ.get("CONDENSE_FLYING_RELEASE_SEC", "900") or 900)
    except ValueError:
        return 900.0


def _flying_release_if_stale() -> int:
    """逐持有者检查在途调用新鲜度；超阈 ⇒ 放位并如实告警。返回放了多少位。
    判定只吃**台账状态**（observe.inflight 的 no_chunk_for）；持有者不在途
    （两段调用之间的间隙）⇒ 判不了，不放（间隙挂死看 /observe/calls 由人处置）。
    [2026-11-02] 单飞→有界并发后按持有者逐条生效。"""
    if not _FLYING:
        return 0
    threshold = _flying_release_sec()
    if threshold <= 0:
        return 0
    released = 0
    for (hw, hc), _since in list(_FLYING.items()):
        for rec in _obs.inflight():
            if (str(rec.get("work_id") or "") == hw
                    and str(rec.get("card_id") or "") == hc
                    and rec.get("no_chunk_for", 0) > threshold):
                logger.warning(
                    "[condense_fold] 凝练持有者疑似挂死（trace=%s 卡=%s/%s 在途 %.0fs "
                    "无帧 %.0fs）——只放位不杀任务（CONDENSE_FLYING_RELEASE_SEC=%.0f）",
                    rec.get("trace_id"), hw, hc,
                    rec.get("age", 0), rec.get("no_chunk_for", 0), threshold)
                # [处置 · 用户令] 挂死＝不可能再运转的状态 ⇒ 报错给用户（该卡 UI 可见）
                _report_to_user(hw, hc,
                                f"凝练调用疑似挂死（{rec.get('no_chunk_for', 0):.0f}s 无任何响应帧），"
                                "已让路给其他卡；挂住的任务未杀，长期挂着请检查模型服务或重启进程。")
                _FLYING.pop((hw, hc), None)
                released += 1
                break
    return released
#: 失败记账退避（dsh `pendingCondenseAt` 账本 ＋ 30min 退避同口径，缺省 30min）。
CONDENSE_FAIL_BACKOFF_SEC = float(os.environ.get("CONDENSE_FAIL_BACKOFF_SEC", "1800") or 1800)


def _condense_debt_of(state: dict[str, Any]) -> dict[str, Any]:
    """失败账（state.condense_debt）：{"failed_at", "next_retry_at", "reason"}。"""
    d = state.get("condense_debt")
    return d if isinstance(d, dict) else {}


def _debt_blocks(state: dict[str, Any], now: float) -> bool:
    """失败退避闸：next_retry_at 未到 ⇒ True（本轮不触发）。"""
    try:
        nxt = float(_condense_debt_of(state).get("next_retry_ts") or 0)
    except (TypeError, ValueError):
        return False
    return nxt > now


def _card_is_archived(work_id: str, card_id: str) -> bool:
    """归档拦截（F14c）：卡已归档（卡表无此行）⇒ 不触发凝练。
    archive_card 把卡行从 `cards` 表移进归档记录 ⇒ 存在性判据即归档判据。"""
    from . import card_store
    try:
        return not card_store.card_exists(work_id, card_id)
    except Exception:  # noqa: BLE001 判不了就放行（宁多试，不误拦）
        return False


async def condense_turn_end(work_id: str, card_id: str,
                            request_tokens: int | None = None) -> None:
    """回合末凝练触发（SP-04：assistant 落盘之后、异步、不阻塞对话）。

    [T15 任务 1] 路线甲闸开（CONDENSE_ROUTE_RULINGS 2026-10-05 21:12）：K 到线
    即启动凝练管线本体。失败路径：异常 catch 留痕零阻断、水位不动（V5/RD-04）；
    [F14] 失败**记账**（condense_debt 落 state，30min 退避，失败不丢账必重试）、
    成功清账。同卡互斥（任务 7）：上一轮未完成→本轮跳过留痕，不排队不设闸；
    [2026-11-02 · card_154 实案] 全局改为**有界并发**（_FLYING 位满才跳过；
    原"全局单飞"在多卡同时到线时把 card_154 饿死 70 分钟、142 万 token 撞窗）。
    """
    from ..infra import observe as _obs
    try:
        await ensure_watermark(work_id, card_id)
        state = load_memory(work_id, card_id)
        mark = int(state.get("mark", 0) or 0)
        # [A6a/T069-T072] 读切换：统一读入门面（块形态含 content，计量/切片同尺）
        #
        # [2026-10-27 · 用户问「凝练的结算应该只看存进 sqlite 的信息」]
        # **结论：方向对，但现在还不能只读库。** 试过改 `origin_store.read_blocks`
        # 并跑测试，13 个凝练用例破——根因是 `write_blocks_sync` 的失败路径
        # **静默**（只记日志、不抛），库一旦缺行，凝练就静默读不到东西、
        # mark 永远不动。而文件是写路径的真相源，坏不了就不缺。
        #
        # 要切成只读库，前提是先做三件：
        #   ① 库写失败不再静默（至少打 error 并计数）；
        #   ② 有回填/校验（`scripts/backfill_session_blocks.py` 已写，work_027 补了 2941 行）；
        #   ③ 读库与读文件产出一致性作为常驻断言（已验证：card_021/4008块/189原文、
        #      fn_004/2104/113、card_061/1157/187，逐项相同）。
        # 三件齐了再切，别在"库可能静默缺数据"时切。
        pending = _read_origin(work_id, card_id, mark)
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
        # [F22] 触发判定数字留痕（原版 recordCondenseDebug 同位）
        _obs.log_condense_trigger(work_id=work_id, card_id=card_id,
                                  block_tokens=block_size, request_tokens=request_tokens,
                                  threshold=k, triggered=size >= k,
                                  source="turn_end", pending_blocks=len(pending), mark=mark)
        if size < k:
            return  # 未到线：安静观察（K 首版估值下常态；标定期看 debug）
        # [F14c 归档拦截] 卡已归档（卡表无行）⇒ 不再触发（原版 archivedAt 拦截同位）
        if _card_is_archived(work_id, card_id):
            logger.info("[condense_fold] 卡已归档——凝练拦截 work=%s card=%s", work_id, card_id)
            return
        # [F14b 失败退避] 上次凝练失败的账未到期 ⇒ 本轮跳过（失败不丢账，必重试）
        if _debt_blocks(state, time.time()):
            logger.info("[condense_fold] 失败退避中——本轮跳过（账：%s）work=%s card=%s",
                        _condense_debt_of(state), work_id, card_id)
            return
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
        # [2026-11-02 · card_154 实案] 全局并发闸：先清疑似挂死的持有者，
        # 仍满才跳过——满位不许静默，持有者明细全在告警里。
        if len(_FLYING) >= _condense_parallel():
            _flying_release_if_stale()
        if len(_FLYING) >= _condense_parallel():
            _holders = "、".join(
                f"{w}/{c}({time.time() - s:.0f}s)" for (w, c), s in _FLYING.items())
            logger.warning(
                "[condense_fold] 跳过（并发位满 %d/%d）work=%s card=%s 持有者=%s",
                len(_FLYING), _condense_parallel(), work_id, card_id, _holders)
            return
        _CONDENSING.add(_key)
        _FLYING[_key] = time.time()
        logger.info("[condense_fold] 触发线 K 已到线，启动凝练 work=%s card=%s 未凝token=%d K=%d mark=%d 未凝块数=%d",
                    work_id, card_id, size, k, mark, len(pending))
        try:
            # [F17] 凝练入口挂 archive 一次性迁移（幂等；失败不挡凝练）
            try:
                await migrate_archive_if_needed(work_id, card_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[condense_fold] archive 迁移失败（不挡凝练）work=%s card=%s: %s",
                               work_id, card_id, exc)
            await _run_condense_pipeline(work_id, card_id, pending, mark)
            # [F14b] 成功清账
            state = load_memory(work_id, card_id)
            if state.get("condense_debt"):
                state.pop("condense_debt", None)
                await save_memory(work_id, card_id, state)
        finally:
            _CONDENSING.discard(_key)
            _FLYING.pop(_key, None)
    except Exception as exc:  # noqa: BLE001 触发面零阻断（V5 哲学）
        logger.warning("[condense_fold] 回合末触发失败（不阻断、留痕、水位不动）work=%s card=%s: %s",
                       work_id, card_id, exc)
        # [处置 2026-10-31 · 用户令] 失效类（不可再运转）报错给用户；断连类走账退避。
        if _model_error_disposition(exc) == "fatal":
            _report_to_user(work_id, card_id,
                            f"凝练触发失败且无法自行恢复（{str(exc)[:120]}）。")
        else:
            try:
                from ..api import event_stream
                event_stream.emit(work_id, card_id, "error",
                                  {"error": "[记忆凝练] 本轮凝练失败（断连/超时类），"
                                   "已记账稍后自动重试。"[:200],
                                   "error_type": "CondenseRetryable",
                                   "source": "condense_fold"})
            except Exception:  # noqa: BLE001
                pass
        # [F14b] 失败记账：30min 退避（可 env 调），失败不丢账、到期必重试
        try:
            state = load_memory(work_id, card_id)
            state["condense_debt"] = {
                "failed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "next_retry_ts": time.time() + CONDENSE_FAIL_BACKOFF_SEC,
                "reason": str(exc)[:300],
            }
            await save_memory(work_id, card_id, state)
        except Exception:  # noqa: BLE001
            pass


def _card_id_from_session_stem(stem: str) -> str:
    """会话文件名 → card_id（与 `session_store._session_path` 互逆）。

    [扫描器撞号修复 2026-10-26] 原实现一律 `"card_" + stem.split("_",1)[1]`：
    `session_fn_004.md` → `card_fn_004`（不存在的卡）⇒ 职能卡的会话**永远扫不到**，
    到线也不会凝。实测 work_027/fn_004 14.4 万、fn_006 10 万，mark 恒为 0。
    互逆规则：纯数字 ⇒ `card_NNN`；其余（fn_/其它前缀）⇒ 原名即 card_id。
    """
    rest = stem[len("session_"):] if stem.startswith("session_") else stem
    return f"card_{rest}" if rest.isdigit() else rest


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
            card_id = _card_id_from_session_stem(f.stem)
            try:
                st = load_memory(work_id, card_id)
                mark = int(st.get("mark", 0) or 0)
                pending = _read_origin(work_id, card_id, mark)
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

    [F14 口径 · 2026-10-31 审计订正] 本路径**同样吃全局并发闸**（condense_turn_end
    内统一判）——不设豁免。[2026-11-02] 原"全局单飞"已放宽为有界并发
    （_condense_parallel，默认 3）：并发位满时本轮预检跳过，请求可能仍超窗
    失败；有位即接管，**不构成永久死锁**（死锁的根因「只在成功回合末触发」
    已被预检本身修掉）。同卡互斥（_CONDENSING）照旧生效。

    返回是否真的跑了凝练。开关关 / 未到线 / 异常 ⇒ 一律返回 False，不阻断回合。
    """
    if not condense_enabled():
        return False
    try:
        await ensure_watermark(work_id, card_id)
        state = load_memory(work_id, card_id)
        mark = int(state.get("mark", 0) or 0)
        pending = _read_origin(work_id, card_id, mark)
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
            cur = _meter_sum(_read_origin(work_id, card_id, after))
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


def maybe_schedule_reclaim(work_id: str, card_id: str) -> None:
    """[F15] 回合末时效回收调度口（message_runner.finish 调用）：异步、零阻断。

    回收是纯机械（无 LLM、无模型成本），不受 CONDENSE_ENABLED 闸——sunk 标记
    只影响凝练全景体积；上下文基线渲染不看 sunk（零误伤）。开关关时回收照跑：
    记忆链照样在长，时效账照样该记。
    """
    try:
        task = asyncio.get_running_loop().create_task(
            reclaim_turn_end(work_id, card_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception as exc:  # noqa: BLE001 调度失败不阻断回合
        logger.warning("[condense_fold] 回收调度失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)
