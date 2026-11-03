# -*- coding: utf-8 -*-
"""评审发言必须是**一轮正常对话**（2026-10-31 用户令）。

用户原话：「空白就是一条消息都没进评审卡的对话界面展示出来，只有工具调用事件」

根因（旧码）：评审轮没有 human 块 ⇒ `session_facade._assign_block` 的 `pair`
取不到 ⇒ 落的块 `pair_id="unknown"` ⇒ 事件流 `run_id="unknown"` ⇒
前端拿不到真实 turn id ⇒ 发言落不进任何一轮，对话界面只剩工具行。

修法（venue 架构）：席位回合＝普通卡回合（`_seat_turn` → `_run_message`），
场会话就是评审卡自己的会话——回合内所有块共用这个回合的真实 pair_id。
"""
from __future__ import annotations

from src.api import model_service, review_runner
from src.storage import review_store, session_facade


async def _setup(n: int = 2):
    """在基础夹具上**再开一个批次**：评审轮必须有进行中批次才会落会话。"""
    from tests.test_review_user_intervention import _setup as _base
    from src.storage import review_store
    wid, cid, fids, rid = await _base(n)
    await review_store.open_batch(wid, rid, "测试焦点", members=fids,
                                  target=cid, brief="测试背景")
    return wid, cid, fids, rid


async def _seat(wid, rid, seat):
    """叫一席发言（当前批次状态现读）。"""
    st = await review_store.state_of(wid, rid)
    return await review_runner._seat_turn(wid, rid, seat, st, kind="speak")


async def test_review_speech_has_real_pair_id(sandbox, monkeypatch):
    """发言块必须带真实 pair_id（不能是 unknown）。"""
    wid, cid, fids, rid = await _setup()
    speech = "按产品经理的职能读完草稿：方向认可，三处须改。"
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": speech, "tool_calls": [
                {"name": "reject", "arguments": {"review_card_id": rid}}]},
                "fake")
        return ({"content": speech, "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake)
    await _seat(wid, rid, fids[0])

    blocks = await session_facade.read(wid, rid) or []
    speech_blocks = [b for b in blocks
                     if str(b.get("actor")) == "assistant"
                     and speech in str(b.get("content") or "")]
    assert speech_blocks, "发言必须落进场会话"
    pid = str(speech_blocks[-1].get("pair_id") or "")
    assert pid and pid != "unknown", f"pair_id 必须是真实 turn id，实际 {pid!r}"
    assert len(pid) >= 16, f"pair_id 看起来不是 uuid：{pid!r}"


async def test_tool_blocks_share_the_same_turn(sandbox, monkeypatch):
    """同一轮的工具块与发言块 pair_id 相同（整段是一轮对话）。"""
    wid, cid, fids, rid = await _setup()
    speech = "我读了卡再表态。"
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": "", "tool_calls": [
                {"name": "read_card", "arguments": {"card_id": cid}}]}, "fake")
        if calls["n"] == 2:
            return ({"content": speech, "tool_calls": [
                {"name": "reject", "arguments": {"review_card_id": rid}}]}, "fake")
        return ({"content": speech, "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake)
    await _seat(wid, rid, fids[0])

    blocks = await session_facade.read(wid, rid) or []
    pids = {str(b.get("pair_id") or "") for b in blocks
            if str(b.get("actor")) in ("assistant", "tool")
            and str(b.get("pair_id") or "") != ""}
    assert len(pids) == 1, f"同一轮的所有块应共用一个 pair_id，实际 {pids}"
    assert list(pids)[0] != "unknown"


async def test_different_turns_get_different_pair_ids(sandbox, monkeypatch):
    """两次发言是不同的轮 ⇒ pair_id 不同（不能被并成一轮）。"""
    wid, cid, fids, rid = await _setup()
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": "发言", "tool_calls": [
                {"name": "reject", "arguments": {"review_card_id": rid}}]},
                "fake")
        return ({"content": "发言", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake)
    await _seat(wid, rid, fids[0])
    await _seat(wid, rid, fids[1])

    blocks = await session_facade.read(wid, rid) or []
    pids = [str(b.get("pair_id") or "") for b in blocks
            if str(b.get("actor")) == "assistant"]
    # 同一轮内的多块（tool_calls 块 + 回复块）共享一个 pair_id；
    # 两轮席位回合 ⇒ 恰好两个不同的 pair_id。
    assert len(set(pids)) == 2, f"两轮应恰有两个 pair_id，实际 {pids}"
    assert "unknown" not in pids, f"pair_id 不许落成 unknown：{pids}"
