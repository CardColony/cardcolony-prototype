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
首版估值 300,000 B 未凝历史正文（≈dsh 14 万 token 触发逻辑按 2.1 B/token
折算的字节代理），标「待实测标定」；环境变量 CONDENSE_TRIGGER_K 覆盖
（标定通道，非闸）。

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
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import session_store

logger = logging.getLogger("storage.memory")

# 触发线 K 首版估值（SP-01：行为参数先落一版看真实分布；待实测标定）
DEFAULT_TRIGGER_K_BYTES = 300_000

# [T15-任务 3] 凝练 prompt 骨架（诸葛定，用户可事后推翻）：纯文本产出、
# 不涉工具解析（call_main tools=None——统合官文本泄漏路径不触发）。
CONDENSE_SYSTEM_PROMPT = (
    "你是记忆凝练器。把给定的对话段压成记忆条目。每条输出：【结论】一句话陈述"
    "这段对话确立的事实或判断；【依据】指向结论所依据的具体对话块号。"
    "不写感想、不写计划、不改写原文。"
)

# 【结论】/【依据】标记与块号引用的机械解析（任务 3/4）
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


def trigger_k_bytes() -> int:
    """触发线 K（字节）：CONDENSE_TRIGGER_K 覆盖优先（标定通道），缺省首版估值。"""
    raw = os.environ.get("CONDENSE_TRIGGER_K", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            logger.warning("[condense_fold] CONDENSE_TRIGGER_K 非法（%r），用首版估值", raw)
    return DEFAULT_TRIGGER_K_BYTES


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
    """为 work 下每张有会话的卡落空水位（任务 1-2 批量面）。返回新建 card_id 列表。"""
    created: list[str] = []
    cards_dir = session_store.WORKS_DIR / work_id / "cards"
    if not cards_dir.exists():
        return created
    for path in sorted(cards_dir.glob("card_*.md")):
        if await ensure_watermark(work_id, path.stem):
            created.append(path.stem)
    return created


# 回合末触发后台任务句柄（防 GC；完成即弃）
_background_tasks: set[asyncio.Task] = set()

# [T15-任务 7] 同卡互斥标记（竞态防线）：在凝的卡二次触发→跳过留痕，不排队不设闸。
# 单事件循环内「查＋占」之间无 await，asyncio 语义下原子。
_condensing_cards: set[tuple[str, str]] = set()


def _build_condense_user_prompt(pending: list[dict[str, Any]]) -> str:
    """凝练 user prompt（任务 2/3）：未凝段全文，每块前缀 `[块{seq}]`。"""
    parts: list[str] = []
    for m in pending:
        parts.append(f"[块{m.get('seq', '?')}]\n{str(m.get('content', '') or '')}")
    return "\n\n".join(parts)


def _parse_condense_output(text: str, default_seq: int) -> list[dict[str, Any]]:
    """按【结论】/【依据】标记逐条切分（任务 3）。

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

    def _flush() -> None:
        nonlocal buf_conc, buf_evi
        conclusion = "\n".join(buf_conc).strip()
        if conclusion:
            evidence = "\n".join(buf_evi).strip()
            refs = [int(n) for n in _BLOCK_REF_RE.findall(evidence)]
            parsed.append({
                "seq": min(refs) if refs else default_seq,
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
    """
    from ..api import model_service  # 延迟导入：storage 装载期不依赖 api（唯一调用点）

    result, _model = await model_service.call_main(
        CONDENSE_SYSTEM_PROMPT, user_prompt, prior_messages=[], tools=None)
    return str((result or {}).get("content") or "")


async def _run_condense_pipeline(work_id: str, card_id: str,
                                 pending: list[dict[str, Any]], mark: int) -> None:
    """凝练管线本体（T15 任务 2→5，路线甲）：取段→模型写→解析→落 entries→推进 mark。

    失败（调用异常/空产出/解析失败）＝水位一分不动、warning 留痕一条、
    不阻断不重试不设闸（V5 哲学/RD-04）。entries＋mark 单文件原子落盘
    （save_memory 一次写），无中间态。
    """
    if not pending:
        logger.info("[condense_fold] 未凝段为空——跳过 work=%s card=%s", work_id, card_id)
        return
    user_prompt = _build_condense_user_prompt(pending)
    input_bytes = len(user_prompt.encode("utf-8"))
    fingerprint = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()[:16]
    # 任务 3：模型写【结论】＋【依据】
    try:
        text = await _condense_call(user_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[condense_fold] 凝练失败——水位一分不动 work=%s card=%s 阶段=模型调用 原因=%s",
                       work_id, card_id, exc)
        return
    # 任务 3：产出解析（失败/空产出→不凝）
    fresh = _parse_condense_output(text, default_seq=int(pending[0].get("seq", mark) or mark))
    if not fresh:
        logger.warning("[condense_fold] 凝练失败——水位一分不动 work=%s card=%s 阶段=产出解析 原因=空产出或无【结论】条目",
                       work_id, card_id)
        return
    target_mark = int(pending[-1].get("seq", 0) or 0) + 1
    state = load_memory(work_id, card_id)
    existing = [e for e in (state.get("entries") or []) if isinstance(e, dict)]
    # 任务 5 幂等：同窗口（同 fingerprint）已凝 → 条目零新增，mark 幂等到位
    if any(e.get("fingerprint") == fingerprint for e in existing):
        if int(state.get("mark", 0) or 0) < target_mark:
            await save_memory(work_id, card_id,
                              {"card_id": card_id, "mark": target_mark, "entries": existing})
            logger.info("[condense_fold] 幂等补推 mark work=%s card=%s mark→%d 条目零新增 fingerprint=%s",
                        work_id, card_id, target_mark, fingerprint)
        else:
            logger.info("[condense_fold] 幂等跳过（同指纹已凝、mark 已到位）work=%s card=%s mark=%d 条目数=%d fingerprint=%s",
                        work_id, card_id, int(state.get("mark", 0) or 0), len(existing), fingerprint)
        return
    # 任务 4：五字段落 entries（seq/conclusion/evidence/occurred_at/fingerprint）
    occurred_at = datetime.now().astimezone().isoformat()
    final_entries = [dict(e, occurred_at=occurred_at, fingerprint=fingerprint) for e in fresh]
    await save_memory(work_id, card_id,
                      {"card_id": card_id, "mark": target_mark, "entries": existing + final_entries})
    logger.info("[condense_fold] 凝练完成 work=%s card=%s 输入块数=%d 输入字节=%d 产出条数=%d mark %d→%d fingerprint=%s",
                work_id, card_id, len(pending), input_bytes, len(final_entries), mark, target_mark, fingerprint)


async def condense_turn_end(work_id: str, card_id: str) -> None:
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
        prior = await session_store.read_session(work_id, card_id)
        pending = prior[mark:] if 0 <= mark <= len(prior) else prior
        size = sum(len(str(m.get("content", "")).encode("utf-8")) for m in pending)
        k = trigger_k_bytes()
        if size < k:
            return  # 未到线：安静观察（K 首版估值下常态；标定期看 debug）
        if (work_id, card_id) in _condensing_cards:
            logger.info("[condense_fold] 触发跳过（同卡凝练进行中）work=%s card=%s 未凝字节=%d K=%d 原因=互斥",
                        work_id, card_id, size, k)
            return
        logger.info("[condense_fold] 触发线 K 已到线，启动凝练 work=%s card=%s 未凝字节=%d K=%d mark=%d 未凝块数=%d",
                    work_id, card_id, size, k, mark, len(pending))
        _condensing_cards.add((work_id, card_id))
        try:
            await _run_condense_pipeline(work_id, card_id, pending, mark)
        finally:
            _condensing_cards.discard((work_id, card_id))
    except Exception as exc:  # noqa: BLE001 触发面零阻断（V5 哲学）
        logger.warning("[condense_fold] 回合末触发失败（不阻断、留痕、水位不动）work=%s card=%s: %s",
                       work_id, card_id, exc)


def maybe_schedule_turn_end(work_id: str, card_id: str) -> None:
    """回合末触发调度口（routes 层唯一调用点）：开关关＝零动作零开销；
    开＝create_task 异步触发、不阻塞对话（SP-04）。调度失败仅留痕。"""
    if not condense_enabled():
        return
    try:
        task = asyncio.get_running_loop().create_task(condense_turn_end(work_id, card_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception as exc:  # noqa: BLE001 调度失败不阻断回合
        logger.warning("[condense_fold] 触发调度失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)
