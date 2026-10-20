# -*- coding: utf-8 -*-
"""**思考流落盘合并**回归网（[5715] · 性能）。

## 为什么有这张网

2026-10-20 出现了「5700 对话页卡死，刷新/切卡都不能操作」：

- **现象**：一张卡的事件日志 17,574 条，其中 **16,795 条是 reasoning**（3.68MB）；
  对话页回放时装配器**逐条 rev++ 重渲染** ⇒ 主线程卡死。
- **根因**：思考流**每一帧都落一条** `reasoning` 事件。
- **修法**（`src/api/event_stream.py`）：`reasoning_chunk` 不再逐帧落盘，改为按
  `(card, run_id, round, model)` 累积，**到轮界**（emit 任意别的��件）合并落**一条**。
  实时 SSE 仍逐帧出（前端照旧逐字流），**只是落盘合并**。

⇒ 这张网钉住三件事：**不逐帧落**、**一轮一条**、**顺序在轮界之前**。
（另一半——「回放时别把整块思考重写 N 遍」——在前端，见
``tests/chat_think_stream_dom.test.mjs``。）
"""
from __future__ import annotations

from src.api import event_stream
from src.storage import event_log


async def _drain():
    await event_log.drain()


def _types(items):
    return [i.get("type") for i in items]


def _reasoning(items):
    return [i for i in items if i.get("type") == "reasoning"]


async def test_reasoning_frames_are_not_written_per_frame(sandbox):
    """连续思考帧**不逐帧落盘**（落盘前事件流里一条 reasoning 都没有）。"""
    wid, cid = "work_perf_1", "card_001"
    event_stream.emit_token(wid, cid, "reasoning_chunk", "第一段", {"model": "m1"})
    event_stream.emit_token(wid, cid, "reasoning_chunk", "第二段", {"model": "m1"})
    await _drain()
    items = event_log.timeline(wid, cid)
    assert not _reasoning(items), \
        f"思考帧不该逐帧落盘（那正是页面卡死的根因）：{_types(items)}"


async def test_reasoning_merges_into_one_event_at_round_boundary(sandbox):
    """到轮界（emit 任意别的���件）⇒ 合并落**一条**，文本是所有帧的拼接。"""
    wid, cid = "work_perf_2", "card_001"
    event_stream.emit_token(wid, cid, "reasoning_chunk", "第一段", {"model": "m1"})
    event_stream.emit_token(wid, cid, "reasoning_chunk", "第二段", {"model": "m1"})
    event_stream.emit_token(wid, cid, "reasoning_chunk", "第三段", {"model": "m1"})
    event_stream.emit(wid, cid, "round_start", {"round": 1})
    await _drain()

    items = event_log.timeline(wid, cid)
    rs = _reasoning(items)
    assert len(rs) == 1, f"一轮的思考应合并成一条，实为 {len(rs)} 条"
    assert (rs[0].get("data") or {}).get("text") == "第一段第二段第三段", \
        f"合并后的文本应等于所有帧的拼接：{(rs[0].get('data') or {}).get('text')!r}"
    assert rs[0].get("model") == "m1", "合并的事件要带 model（顶层字段，落盘记录形状如此）"
    # 顺序：reasoning 必须**在**轮界事件之前（否则回放时思考会跑到下一轮后面）
    types = _types(items)
    assert types.index("reasoning") < types.index("round_start"), \
        f"思考要在轮界之前落盘：{types}"


async def test_reasoning_of_different_models_are_not_mixed(sandbox):
    """两个席位的思考**不混成一条**（model 不同 ⇒ 各自一条，前端据此分桶）。"""
    wid, cid = "work_perf_3", "card_001"
    event_stream.emit_token(wid, cid, "reasoning_chunk", "甲席的思考", {"model": "m1"})
    event_stream.emit_token(wid, cid, "reasoning_chunk", "乙席的思考", {"model": "m2"})
    event_stream.emit(wid, cid, "round_start", {"round": 1})
    await _drain()

    rs = _reasoning(event_log.timeline(wid, cid))
    assert len(rs) == 2, f"两个 model 应各落一条：{[r.get('model') for r in rs]}"
    texts = {r.get("model"): (r.get("data") or {}).get("text") for r in rs}
    assert texts.get("m1") == "甲席的思考", f"两席文本串了：{texts}"
    assert texts.get("m2") == "乙席的思考", f"两席文本串了：{texts}"


async def test_empty_reasoning_is_not_written(sandbox):
    """空思考不落盘（免得刷出空行）。"""
    wid, cid = "work_perf_4", "card_001"
    event_stream.emit_token(wid, cid, "reasoning_chunk", "   ", {"model": "m1"})
    event_stream.emit(wid, cid, "round_start", {"round": 1})
    await _drain()
    assert not _reasoning(event_log.timeline(wid, cid)), "空思考不该落盘"
