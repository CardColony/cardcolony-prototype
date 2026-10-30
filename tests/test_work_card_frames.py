# -*- coding: utf-8 -*-
"""工作级流「顺带」带卡级帧（P28 · 连接预算）的**后端**验收。

病根（实测，不是推测）：浏览器对同一源 HTTP/1.1 只有 **6 条**并发连接，而画布
「监控态」原来给**每张被盯的卡**各开一条 SSE ⇒ 10 张卡并行 ⇒ 10 条长连接 ⇒
**必然打满** ⇒ 普通请求（timeline / card / nav）排队拿不到连接 ⇒ fetch 抛
TypeError ⇒ 弹「网络错误」、切卡卡死。

解法：卡级帧**顺带**投给工作级订阅者 ⇒ **一条**工作级连接带全部卡的帧
⇒ 连接数与卡数**解耦**（实测：盯 3 张卡，改前 4 条、改后 1 条）。

本文件钉住这条通道的四条边界：
1. **opt-in**：只有 ``?cards=1`` 才带卡级帧；不带就一条都不带（对话页零浪费）；
2. 卡级帧**带 card 字段**（卡号），工作级原生帧**不带** ⇒ 前端可区分
   （⚠ 且必须**先判 card 再判 type**：卡级帧也可能有 type="card_message"）；
3. **逐字帧不投**（content_chunk / reasoning_chunk）——每个 token 一帧，
   10 张卡并行会把工作级队列打爆，而画布并不需要逐字；
4. 订阅者断开后队列被清理（不泄漏）。
"""
from __future__ import annotations

import asyncio
import json

from src.api import event_stream, routes


async def _pump(resp, frames: list) -> None:
    """把 SSE 响应体里的 data 帧收进 frames。"""
    async for chunk in resp.body_iterator:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))


async def _collect(wid, cards: int, action, wait: float = 0.35) -> list[dict]:
    """订阅工作级流（cards=1 才带卡帧）→ 触发 action → 收回帧。"""
    resp = await routes.stream_work_events(wid, cards)
    frames: list[dict] = []
    task = asyncio.create_task(_pump(resp, frames))
    await asyncio.sleep(0.15)          # 让订阅者先就位
    action()
    await asyncio.sleep(wait)
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001 取消是预期路径
        pass
    return frames


async def test_work_level_carries_card_frames_when_opted_in(work):
    """opt-in（?cards=1）⇒ 卡的帧会顺着工作级连接过来，且带 card 字段。"""
    wid, cid = work["work_id"], work["work_card_id"]
    frames = await _collect(
        wid, 1, lambda: event_stream.emit(wid, cid, "tool_call", {"tool": "x"}))
    card_frames = [f for f in frames if f.get("card") == cid]
    assert card_frames, f"工作级流没有带出这张卡的帧：{frames}"
    assert card_frames[0]["type"] == "tool_call", card_frames[0]
    assert (card_frames[0].get("data") or {}).get("tool") == "x", card_frames[0]


async def test_work_level_native_frames_have_no_card_field(work):
    """工作级**原生**帧（emit_work）不带 card 字段 ⇒ 前端可据此区分两类帧。"""
    wid, cid = work["work_id"], work["work_card_id"]
    frames = await _collect(
        wid, 1, lambda: event_stream.emit_work(wid, "card_changed", {"card_id": cid}))
    native = [f for f in frames if f.get("type") == "card_changed"]
    assert native, f"没收到工作级原生帧：{frames}"
    assert "card" not in native[0], f"工作级原生帧不该带 card 字段：{native[0]}"


async def test_work_level_without_cards_carries_no_card_frames(work):
    """不带 ?cards=1 ⇒ **一条卡级帧都不带**（对话页不该为画布付代价）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    frames = await _collect(
        wid, 0, lambda: event_stream.emit(wid, cid, "tool_call", {"tool": "x"}))
    assert not [f for f in frames if f.get("card")], \
        f"未 opt-in 却收到了卡级帧：{frames}"


async def test_token_frames_are_not_pushed_to_work_channel(work):
    """逐字帧**不投**工作级通道：每个 token 一帧，10 张卡并行会把队列打爆。"""
    wid, cid = work["work_id"], work["work_card_id"]
    frames = await _collect(
        wid, 1, lambda: event_stream.emit_token(wid, cid, "content_chunk", "你好"))
    assert not [f for f in frames if f.get("card")], \
        f"逐字帧不该投到工作级通道：{frames}"


async def test_work_card_subscriber_cleaned_on_disconnect(work):
    """订阅者断开 ⇒ 队列被清理（长连接反复开关不许泄漏）。"""
    wid = work["work_id"]
    sub = event_stream.subscribe_work_cards(wid)
    task = asyncio.create_task(sub.__anext__())   # 第一次迭代才登记（异步生成器惰性）
    await asyncio.sleep(0.05)
    assert event_stream.work_card_subscriber_count(wid) >= 1, "订阅者应当已登记"
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001 取消是预期路径
        pass
    await asyncio.sleep(0.05)
    assert event_stream.work_card_subscriber_count(wid) == 0, \
        "断开后订阅者队列应当被清空"
