# -*- coding: utf-8 -*-
"""画布 5711 · 工作级事件广播（``/api/works/{w}/events``）的**后端单元验收**。

为什么要有这个文件：工作级广播是"信息更新"这条诉求的**后端一半**，此前只有
浏览器侧的端到端探针（``tests/canvas_live_update.mjs``）在验它——后端那半
（帧的形状、通道隔离、四个写路径的挂钩、卡间信那条广播）**一条单测都没有**。
端到端探针看不见"帧发到了错误的通道""投影载荷是旧值""订阅者没被清理"这类问题。

本文件钉住六条：
1. ``emit_work`` 的帧形状（seq/ts/type/data）与**主通道隔离**（不占主 seq、不进 journal）；
2. 零阻塞零抛出（坏 work_id 只返 ``{}``，不抛）；
3. 写卡的**四个入口**（create/update/delete/restore）各广播一帧 ``card_changed``；
4. 载荷里的投影摘要是**重建过的新值**（前端据此就地换卡面，不会拿到旧值）；
5. 卡间信（收件箱）落信时**同时**广播一帧 ``card_message`` 到工作级通道
   ——这帧是画布页"未读角标增量"的**唯一**来源（原先那条独立 SSE 监听了错的键，
   从来没活过）；
6. 订阅者断开后队列被清理（不泄漏）。
"""
from __future__ import annotations

import asyncio

import pytest

from src.api import event_stream
from src.storage import card_store


async def _collect(sub, out, want=1, timeout=3.0):
    """从订阅流里收 want 帧（超时即返回已收到的）。"""
    async def _run():
        async for evt in sub:
            out.append(evt)
            if len(out) >= want:
                return
    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def _drain_frames(wid, want=1, timeout=3.0, action=None):
    """订阅工作级通道 → （可选）触发动作 → 收回帧。"""
    import inspect

    out: list[dict] = []
    sub = event_stream.subscribe_work(wid)
    task = asyncio.create_task(_collect(sub, out, want=want, timeout=timeout))
    await asyncio.sleep(0.15)          # 让订阅者先就位
    if action is not None:
        r = action()
        if inspect.isawaitable(r):
            await r
    await task
    try:
        await sub.aclose()
    except Exception:  # noqa: BLE001 关流失败不阻断断言
        pass
    return out


# ── 1/2 帧形状与通道隔离 ────────────────────────────────────────────────

async def test_emit_work_frame_shape_and_channel_isolation(work):
    wid = work["work_id"]
    frames = await _drain_frames(
        wid, want=1,
        action=lambda: event_stream.emit_work(wid, "card_changed", {"card_id": "card_x"}),
    )
    assert len(frames) == 1, f"订阅者没收到帧：{frames}"
    f = frames[0]
    assert f["type"] == "card_changed"
    assert f["data"] == {"card_id": "card_x"}
    assert isinstance(f["seq"], int) and f["seq"] >= 1
    assert isinstance(f["ts"], (int, float))

    # 隔离三条：工作级不占主通道的 seq、不进主 journal、不写 works 文件。
    before_seq = event_stream.journal_last_seq(wid, "card_001")
    before_journals = len(getattr(event_stream, "_journals", {}) or {})
    event_stream.emit_work(wid, "card_changed", {"card_id": "card_y"})
    assert event_stream.journal_last_seq(wid, "card_001") == before_seq, \
        "工作级事件不许占主通道的 seq"
    assert len(getattr(event_stream, "_journals", {}) or {}) == before_journals, \
        "工作级事件不许进主 journal"


async def test_emit_work_never_raises(work):
    """零阻塞零抛出：坏入参只返空字典，不把异常抛给写卡路径。"""
    assert event_stream.emit_work("", "card_changed", {}) == {}
    assert event_stream.emit_work(None, "card_changed", {}) == {}  # type: ignore[arg-type]
    assert event_stream.emit_work("w1", "card_changed", None)["type"] == "card_changed"


# ── 3/4 四个写路径各广播一帧，载荷是重建过的新值 ─────────────────────────

async def test_four_write_paths_each_emit_card_changed(work):
    wid, root = work["work_id"], work["work_card_id"]

    # create
    frames = await _drain_frames(
        wid, want=1,
        action=lambda: card_store.create_card(wid, "被广播的新卡", "用户直接创建", parent=root),
    )
    assert len(frames) == 1, f"建卡没有广播工作级事件：{frames}"
    assert frames[0]["type"] == "card_changed"
    new_id = frames[0]["data"]["card_id"]
    assert frames[0]["data"].get("kind") == "created"

    # update —— 载荷里的投影摘要必须是**新值**（前端据此就地换卡面）
    frames = await _drain_frames(
        wid, want=1,
        action=lambda: card_store.update_card(wid, new_id, {"title": "改过名字的卡"}, "单测改名"),
    )
    assert len(frames) == 1, f"改卡没有广播工作级事件：{frames}"
    assert frames[0]["data"]["card_id"] == new_id
    proj = frames[0]["data"].get("card") or {}
    assert proj.get("title") == "改过名字的卡", \
        f"载荷里的投影摘要必须是重建过的新值：{proj.get('title')!r}"

    # delete
    frames = await _drain_frames(
        wid, want=1,
        action=lambda: card_store.delete_card(wid, new_id),
    )
    assert len(frames) == 1, f"删卡没有广播工作级事件：{frames}"
    assert frames[0]["data"]["card_id"] == new_id
    assert frames[0]["data"].get("kind") == "deleted"

    # restore
    frames = await _drain_frames(
        wid, want=1,
        action=lambda: card_store.restore_card(wid, new_id),
    )
    assert len(frames) == 1, f"恢复卡没有广播工作级事件：{frames}"
    assert frames[0]["data"]["card_id"] == new_id
    assert frames[0]["data"].get("kind") == "created"


# ── 5 卡间信：落信时同时广播 card_message（未读角标的增量来源）──────────

async def test_card_message_broadcasts_to_work_channel(work):
    from src.api import card_tools
    from src.storage import card_store as cs

    wid, root = work["work_id"], work["work_card_id"]
    # 发信必须是**父子对**：产品规则（2026-10-09「同一棵任务树内」双向各查一次）
    # 明确挡住兄弟卡互发与跨树发信 ⇒ 用父卡→子卡才合法。
    b = (await cs.create_card(wid, "收信方", "用户直接创建", parent=root))["meta"]["card_id"]
    cards = {
        cid: (await cs.read_card(wid, cid)).get("meta") or {}
        for cid in (root, b)
    }

    async def _send():
        nonlocal res
        res = await card_tools._h_send_card_message(
            wid, root,
            {"target_card_id": b, "msg_type": "msg_notify", "content": "单测来信"},
            cards,
        )
        return res

    res = None

    frames = await _drain_frames(wid, want=1, action=_send)
    assert len(frames) == 1, f"卡间信没有广播到工作级通道：{frames}（发送结果：{res}）"
    f = frames[0]
    assert f["type"] == "card_message", f"帧类型不对：{f['type']}"
    assert f["data"].get("card_id") == b, "card_message 帧必须带**收件方**卡号"
    assert f["data"].get("from") == root


# ── 6 订阅者断开后队列被清理（不泄漏）──────────────────────────────────

async def test_subscriber_queue_cleaned_on_disconnect(work):
    wid = work["work_id"]
    out: list[dict] = []
    sub = event_stream.subscribe_work(wid)
    # 订阅者是在**第一次迭代**时才登记进队列的（异步生成器的惰性）⇒ 必须先跑起来。
    task = asyncio.create_task(_collect(sub, out, want=99, timeout=5.0))
    await asyncio.sleep(0.2)
    subs = getattr(event_stream, "_work_subscribers", {})
    assert wid in subs and len(subs[wid]) >= 1, "订阅者应当已登记"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.15)
    assert not subs.get(wid), f"断开后订阅者队列应当被清空：{subs.get(wid)}"
