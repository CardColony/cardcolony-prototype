# -*- coding: utf-8 -*-
"""轮内插话通道（[5715]）—— 置位／消费／清扫三件套 ＋ 轮槽插入 ＋ 事件内容覆盖。

**为什么要有这套测试**：插话此前是**假功能**（UI 写着「插话」，实现只弹一句
「轮结束后依序发出」）。假功能没有测试 ⇒ 没人发现它是假的。本文件把三件事钉死：

1. ``steer.request_steer``：**无在途回合 ⇒ 不投递**（applied:false，不假装成功）；
   有在途回合 ⇒ 置位、落一条 user_message **事件**（界面可见、刷新也在）。
2. ``steer.take_steer`` 消费即清（一次性，不重放）；``discard_steer`` 清残留
   （回合收口统一清，不能留给下一回合）。
3. ``message_runner._apply_steer`` 把插话**插进轮槽** ⇒ 下一次模型调用即可见
   （rounds>=2 直接进槽；rounds==1 暂存 steer_tail，随用户原文之后固化）。
4. ``session_facade.append(..., event_content=...)``：**会话块内容不变**、
   **事件流内容被覆盖**（Bug A 修复的核心机制：会话块给模型看、事件流给界面看）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api import message_runner, steer                      # noqa: E402
from src.storage import session_facade                        # noqa: E402


# ---------------------------------------------------------------- 1. 置位

@pytest.mark.asyncio
async def test_request_steer_without_inflight_turn_is_not_applied():
    """无在途回合 ⇒ **不投递**（宁可让前端回退成排队，也不假装插话成功）。"""
    res = await steer.request_steer("work_x", "card_x", "插话内容")
    assert res["applied"] is False
    assert res["reason"] == "no_inflight_turn"
    assert steer.pending_steer("work_x", "card_x") == 0


@pytest.mark.asyncio
async def test_request_steer_with_inflight_turn_is_applied_and_lands_event():
    """有在途回合 ⇒ 投递成功，且**界面看得见**（落一条 user_message 事件）。"""
    work_id, card_id = "work_steer", "card_001"
    owner = "turn:steer-test"
    assert session_facade.turn_try_acquire(work_id, card_id, owner) is True
    try:
        res = await steer.request_steer(work_id, card_id, "先别写报告")
        assert res["applied"] is True
        assert steer.pending_steer(work_id, card_id) == 1

        # 事件流里应当出现这条插话（前端唯一数据源 ⇒ 刷新后也看得见）
        items = session_facade.event_log_timeline(work_id, card_id) \
            if hasattr(session_facade, "event_log_timeline") else None
        if items is None:
            from src.storage import event_log
            items = event_log.timeline(work_id, card_id)
        hits = [e for e in items
                if e.get("type") == "user_message"
                and "先别写报告" in str((e.get("data") or {}).get("content") or "")]
        assert hits, "插话必须落一条 user_message 事件（否则界面看不见）"
    finally:
        session_facade.turn_release(work_id, card_id, owner)


# ---------------------------------------------------------------- 2. 消费／清扫

@pytest.mark.asyncio
async def test_take_steer_consumes_once():
    """消费即清：一次性，不重放。"""
    work_id, card_id = "work_steer2", "card_001"
    owner = "turn:steer-test2"
    assert session_facade.turn_try_acquire(work_id, card_id, owner) is True
    try:
        await steer.request_steer(work_id, card_id, "第一句")
        await steer.request_steer(work_id, card_id, "第二句")
        assert steer.pending_steer(work_id, card_id) == 2
        got = steer.take_steer(work_id, card_id)
        # 队列里存的是 mark+body 成品（request 时拼好，apply 取走即用）
        assert got == [steer.STEER_MARK + "第一句", steer.STEER_MARK + "第二句"]
        assert steer.pending_steer(work_id, card_id) == 0
        assert steer.take_steer(work_id, card_id) == []      # 不重放
    finally:
        session_facade.turn_release(work_id, card_id, owner)


def test_discard_steer_clears_leftover():
    """回合收口清扫：未消费的插话作废，不能留给下一回合。"""
    work_id, card_id = "work_steer3", "card_001"

    async def _go():
        owner = "turn:steer-test3"
        assert session_facade.turn_try_acquire(work_id, card_id, owner) is True
        try:
            await steer.request_steer(work_id, card_id, "来不及了")
            assert steer.pending_steer(work_id, card_id) == 1
        finally:
            session_facade.turn_release(work_id, card_id, owner)

    asyncio.run(_go())
    steer.discard_steer(work_id, card_id)
    assert steer.pending_steer(work_id, card_id) == 0


# ---------------------------------------------------------------- 3. 轮槽插入

def test_apply_steer_inserts_into_slot():
    """插话进**轮槽** ⇒ 下一次模型调用即可见（rounds>=2 直接进槽）。"""
    state = message_runner.RunState(work_id="work_steer4", card_id="card_001", content="原始问题")
    state.rounds = 2
    state.slot = _FakeSlot()
    # 队列存 mark+body 成品（request_steer 置位时拼好）；这里直种成品件。
    steer._pending[("work_steer4", "card_001")] = [
        steer.STEER_MARK + "插话：改成先说结论"]

    n = message_runner._apply_steer(state)
    assert n == [steer.STEER_MARK + "插话：改成先说结论"]  # 返回被消费条目（mark+body 成品）
    assert state.slot.messages == [
        {"role": "user", "content": steer.STEER_MARK + "插话：改成先说结论"}]
    # 取走即清，不重放
    assert steer.pending_steer("work_steer4", "card_001") == 0


def test_apply_steer_round1_goes_to_steer_tail():
    """rounds==1 ⇒ 暂存 steer_tail（随用户原文之后固化，顺序语义才对）。"""
    state = message_runner.RunState(work_id="work_steer5", card_id="card_001", content="原始问题")
    state.rounds = 1
    state.slot = _FakeSlot()
    steer._pending[("work_steer5", "card_001")] = ["首轮插话"]

    assert message_runner._apply_steer(state) == ["首轮插话"]  # 返回被消费条目
    assert state.slot.messages == []          # 还没进槽
    assert len(state.steer_tail) == 1
    assert state.steer_tail[0]["role"] == "user"


class _FakeSlot:
    """只记 append_plain 的最小轮槽替身（真轮槽是 tool_fold.ToolTurnSlot）。"""

    def __init__(self):
        self.messages = []

    def append_plain(self, msg):
        self.messages.append(msg)


# ------------------------------------------------- 5. 忙时借插话通道投递
@pytest.mark.asyncio
async def test_deliver_now_busy_steers_instead_of_dropping(monkeypatch):
    """[2026-11-08] 对方在跑 + text 模式 ⇒ 借插话通道进轮槽（返回 True），
    不再只落 False 让消息等下一回合——「父卡在跑时 report_up 推不进」的修复。
    插话件带 〔卡间消息〕 标、载正文（steer_text），不冒充人声。"""
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "0")   # 测试环境默认关自动投递
    work_id, card_id = "work_steer6", "card_001"
    owner = "turn:steer-test6"
    assert session_facade.turn_try_acquire(work_id, card_id, owner) is True
    try:
        ok = await message_runner.deliver_now(
            work_id, card_id,
            text="你收到一条来自 card_009 的汇报，正文已在上文。",
            steer_text="【汇报·子卡(card_009)】\n全部通过。",
            source={"kind": "card_msg", "from": "card_009"})
        assert ok is True
        got = steer.take_steer(work_id, card_id)
        assert got == ["卡间消息：【汇报·子卡(card_009)】\n全部通过。"]

        # 非卡间来源 ⇒ 〔系统消息〕 标；无 steer_text ⇒ 用 text 本体
        ok2 = await message_runner.deliver_now(
            work_id, card_id, text="系统给你发来一条提醒。",
            source={"kind": "system"})
        assert ok2 is True
        assert steer.take_steer(work_id, card_id) == ["系统消息：系统给你发来一条提醒。"]
    finally:
        session_facade.turn_release(work_id, card_id, owner)


def test_discard_steer_returns_remains_for_rescue():
    """收口清扫把残留件**还给**调用方（摘牌后补投用），不是静默吞掉。"""
    work_id, card_id = "work_steer7", "card_001"
    steer._pending[(work_id, card_id)] = ["〔用户插话〕来不及了"]
    got = steer.discard_steer(work_id, card_id)
    assert got == ["〔用户插话〕来不及了"]
    assert steer.pending_steer(work_id, card_id) == 0


# ---------------------------------------------------------------- 4. 事件内容覆盖

@pytest.mark.asyncio
async def test_append_event_content_overrides_event_but_not_block(tmp_path, monkeypatch):
    """**会话块内容不变、事件流内容被覆盖**（会话块给模型看、事件流给界面看）。

    这是「刷新后顺序/内容」修复的核心机制：各轮正文已按轮单独落事件，
    收口那条就不该再落一遍拼接全文。
    """
    from src.storage import session_facade as sf
    monkeypatch.setattr(sf, "_session_path",
                        lambda work_id, card_id: tmp_path / f"{card_id}.md")
    work_id, card_id = "work_ec", "card_001"
    blk = await sf.append(work_id, card_id, {
        "role": "assistant", "content": "合并全文（含各轮正文）",
        "model": "m1", "pair_id": "p1", "end": "model_done",
    }, event_content="只留末轮回执")

    # 块内容不变（模型上下文零变化）
    assert blk["content"] == "合并全文（含各轮正文）"
    raw = (tmp_path / f"{card_id}.md").read_text(encoding="utf-8")
    assert "合并全文" in raw, "会话块内容必须原样落盘（模型上下文不变）"

    # 事件流内容被覆盖
    from src.storage import event_log
    items = event_log.timeline(work_id, card_id)
    ats = [e for e in items if e.get("type") == "assistant_text"]
    assert ats, "应有 assistant_text 事件"
    assert ats[-1]["data"]["content"] == "只留末轮回执"
