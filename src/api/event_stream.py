# -*- coding: utf-8 -*-
"""事件流过程通道（信息流与快车道整合 · Task 3/4，甲线核心）。

纯展示层：内存环形事件日志（每 (work_id, card_id) 一份、seq 单调、默认 300 条、
单条 data 8KB 截断并标记）＋ SSE 订阅 fanout。铁则：
- 事件不进 session/prior/模型输入（A42）——本模块不写任何 works 文件；
- 单进程限制继承（A44）：不落盘、不另起进程；
- 同卡并发 journal 清零重放（A45）：run_start 清空该卡日志（新请求以 run_start 开场）；
- 与打点分离（A22/A24/A96）：审计打点走 logging（本模块 emit 内同步打一行结构化
  日志），事件走内存环形，两通道不混。

事件本体论 17 类（接口①并表，A101）：15 原（run_start/context/round_start/
round_end/rescue/tool_call/tool_result/seats_enter/seat_start/seat_end/deliver/
proposal/done/error/ping）＋ 乙线并入 fastlane_route/card_op。
Schema 统一 {"seq":n,"ts":...,"type":"...","data":{...}}。ping 为 SSE 保层
心跳帧（不占 journal，由 SSE 生成器定期发送）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

logger = logging.getLogger("api.event_stream")

# 事件本体论（17 类；V-T4-1 判据锚——本体论文件含 17 类）
EVENT_TYPES: tuple[str, ...] = (
    "run_start", "context", "round_start", "round_end", "rescue",
    "tool_call", "tool_result", "seats_enter", "seat_start", "seat_end",
    "deliver", "proposal", "done", "error", "ping",
    "fastlane_route", "card_op",
)

MAX_EVENTS = 300
MAX_DATA_BYTES = 8 * 1024

# 兼容映射：emit 兼调 progress.set_stage（旧 /progress 端点与旧前端零感知兼容 A67）
_PROGRESS_STAGE = {
    "run_start": "started",
    "round_start": "tools",
    "tool_call": "tools",
    "seats_enter": "viewpoints",
    "seat_end": "viewpoints",
    "deliver": "synthesize",
    "proposal": "cards",
    "done": "done",
    "error": "error",
}

_journals: dict[tuple[str, str], list[dict[str, Any]]] = {}
_subscribers: dict[tuple[str, str], set[asyncio.Queue]] = {}
# seq 按卡全局单调：run_start 清 journal（A45）不清计数器——长连监听者的
# seq 游标跨请求安全（新请求事件不会被当作已读吞掉）
_seq_counters: dict[tuple[str, str], int] = {}


def _key(work_id: str, card_id: str) -> tuple[str, str]:
    return (work_id, card_id)


def _truncate(data: dict[str, Any]) -> dict[str, Any]:
    """单条 data 8KB 截断并标记（A31）。"""
    try:
        raw = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_note": "unserializable"}
    if len(raw.encode("utf-8")) <= MAX_DATA_BYTES:
        return data
    return {"_truncated": True, "_preview": raw[:MAX_DATA_BYTES // 2]}


def emit(work_id: str, card_id: str, ev_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """落一条事件：seq 单调、环形 300、fanout 订阅者、兼调 progress 兼容映射。

    零阻塞零抛出（展示层绝不影响主流程）；run_start 清空该卡 journal（A45）。
    """
    try:
        key = _key(work_id, card_id)
        if ev_type == "run_start":
            _journals[key] = []
        journal = _journals.setdefault(key, [])
        _seq_counters[key] = _seq_counters.get(key, 0) + 1
        evt = {
            "seq": _seq_counters[key],
            "ts": time.time(),
            "type": ev_type,
            "data": _truncate(data or {}),
        }
        journal.append(evt)
        while len(journal) > MAX_EVENTS:
            journal.pop(0)
        for q in list(_subscribers.get(key, ())):
            try:
                q.put_nowait(evt)
            except Exception:  # noqa: BLE001 单订阅者满/失效不拖累其他
                pass
        # 兼容映射（旧 /progress 零感知）
        stage = _PROGRESS_STAGE.get(ev_type)
        if stage:
            try:
                from . import progress
                progress.set_stage(work_id, card_id, stage, str((data or {}).get("detail") or ev_type)[:120])
            except Exception:  # noqa: BLE001
                pass
        # 审计打点（与事件分离的日志通道；只写不判）
        logger.info("[flow] type=%s work=%s card=%s seq=%s data=%s",
                    ev_type, work_id, card_id, evt["seq"],
                    json.dumps(evt["data"], ensure_ascii=False)[:300])
        return evt
    except Exception as exc:  # noqa: BLE001 展示层零阻断
        logger.warning("[flow] emit 失败（不阻断主流程）type=%s card=%s: %s", ev_type, card_id, exc)
        return {}


def journal_of(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """当前环形日志快照（from=0 重放源）。"""
    return list(_journals.get(_key(work_id, card_id), ()))


async def subscribe(work_id: str, card_id: str, from_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
    """SSE 订阅：先重放 journal 中 seq>=from_seq 的存量，再实时跟进。

    断连（生成器被关闭/取消）自动清理订阅者队列（A31）。
    """
    key = _key(work_id, card_id)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.setdefault(key, set()).add(queue)
    try:
        for evt in journal_of(work_id, card_id):
            if evt["seq"] >= from_seq:
                yield evt
        last = from_seq - 1
        while True:
            evt = await queue.get()
            if evt["seq"] <= last:
                continue
            last = evt["seq"]
            yield evt
    finally:
        try:
            _subscribers.get(key, set()).discard(queue)
        except Exception:  # noqa: BLE001
            pass


def subscriber_count(work_id: str, card_id: str) -> int:
    return len(_subscribers.get(_key(work_id, card_id), ()))
