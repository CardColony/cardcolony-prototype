# -*- coding: utf-8 -*-
"""凝练三处口径（用户 2026-10-22 原话）：

「凝练触发 80 万按照带工具结果的来算，但送去凝练的内容截 30 万的批次，
  但只送用户和助手消息，不带工具结果；销账则要带上工具结果一起销账。」

判据：
C1 触发按**真实请求体积**（含工具结果）——落盘块再小，request_tokens 到线也触发
C2 窗口预算只累计 user/assistant；工具块不占预算，但仍在窗口范围内
C3 销账含工具结果：mark 推进到窗口末块 seq+1（越过工具块）
C4 窗口里只有工具块 ⇒ 不送空 prompt，直接销账
"""
from __future__ import annotations

import pytest

from src.storage import memory_store


def _mk(seq: int, actor: str, content: str) -> dict:
    return {"seq": seq, "actor": actor, "content": content}


def _tool(seq: int, n: int = 600) -> dict:
    return _mk(seq, "tool", "工" * n)


# ---------------------------------------------------------------- C2 / C3

def test_window_budget_counts_tool_blocks():
    """窗口预算**按含工具结果**的块量算（决定这一批推进到哪）；送模型的内容另滤。"""
    pending = [
        _mk(0, "human", "六" * 6),      # 13 token
        _tool(1),                       # 600 字 ⇒ 占预算
        _mk(2, "assistant", "六" * 6),  # 13 token
        _mk(3, "human", "六" * 6),      # 13 token
    ]
    win = memory_store._truncate_window(pending, 630)
    seqs = [m["seq"] for m in win]
    print("窗口 seq:", seqs)
    assert seqs == [0, 1, 2], "13+600+13=626 ≤ 630，再加第 4 条越线"
    # 送凝练的内容只留 user/assistant
    send = [m for m in win if m["actor"] in ("human", "assistant")]
    assert [m["seq"] for m in send] == [0, 2], "工具块不进 prompt"


def test_mark_advances_past_tool_blocks(work, monkeypatch):
    """销账含工具结果：窗口末块是工具块时，mark 也要越过它。"""
    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "700")
    pending = [_mk(0, "human", "六" * 6), _tool(1), _mk(2, "assistant", "六" * 6)]

    async def fake(prompt: str) -> str:
        return "【结论】有结论【依据】块0"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    import asyncio
    asyncio.get_event_loop_policy()
    asyncio.run(memory_store._run_condense_pipeline(wid, cid, pending, 0))
    st = memory_store.load_memory(wid, cid)
    print("mark =", st["mark"])
    assert st["mark"] == 3, "窗口末块 seq=2 ⇒ mark=3（工具块 seq=1 被一起销账）"


# ---------------------------------------------------------------- C4

def test_tool_only_window_is_sold_without_prompt(work, monkeypatch):
    """整窗只有工具块 ⇒ 不送空 prompt，直接销账。"""
    wid, cid = work["work_id"], work["work_card_id"]
    called: list[str] = []

    async def fake(prompt: str) -> str:
        called.append(prompt)
        return "不该被调用"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    import asyncio
    asyncio.run(memory_store._run_condense_pipeline(wid, cid, [_tool(0), _tool(1)], 0))
    st = memory_store.load_memory(wid, cid)
    assert called == [], "全是工具块时不许调模型"
    assert st["mark"] == 2, "但要销账"


# ---------------------------------------------------------------- C1

def test_trigger_uses_real_request_tokens(work, monkeypatch):
    """落盘块远不到线，但本轮真实请求到线 ⇒ 必须触发凝练。"""
    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "300000")
    # 落盘只有一条小消息（按块量远远不到 80 万）
    from src.storage import session_facade

    import asyncio

    async def seed():
        await session_facade.append_many(wid, cid, [
            {"actor": "human", "content": "一句话"},
        ])

    asyncio.run(seed())
    ran: list[str] = []

    async def fake(prompt: str) -> str:
        ran.append(prompt)
        return "【结论】触发了【依据】块0"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    asyncio.run(memory_store.condense_turn_end(wid, cid, request_tokens=1_040_000))
    st = memory_store.load_memory(wid, cid)
    print("ran =", len(ran), "mark =", st["mark"])
    assert ran, "真实请求 104 万 ⇒ 必须触发（不能因为落盘块小就不触发）"
    assert st["mark"] >= 1


def test_trigger_falls_back_to_block_meter(work, monkeypatch):
    """不传 request_tokens ⇒ 仍按落盘块量（老路径行为不变）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")
    ran: list[str] = []

    async def fake(prompt: str) -> str:
        ran.append(prompt)
        return "【结论】不该触发【依据】块0"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    import asyncio
    asyncio.run(memory_store.condense_turn_end(wid, cid))
    assert ran == [], "没给真实体积、块量又不到线 ⇒ 不触发"


# ---------------------------------------------------------------- 三尺同尺

def test_three_rulers_share_one_source(work, monkeypatch):
    """触发、窗口、销账读同一份数据：销账落在**窗口末块**，不因请求体积偏大而多推。"""
    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "630")
    pending = [_mk(0, "human", "六" * 6), _tool(1), _mk(2, "assistant", "六" * 6),
               _mk(3, "human", "六" * 6)]

    async def fake(prompt: str) -> str:
        return "【结论】只压第一批【依据】块0"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    import asyncio
    asyncio.run(memory_store._run_condense_pipeline(wid, cid, pending, 0))
    st = memory_store.load_memory(wid, cid)
    # 窗口 = 13+600+13 ≤ 700，第 4 条越线 ⇒ mark 必须停在 3（末块 seq=2 的下一块）
    assert st["mark"] == 3, f"销账要落在窗口末块，实际 {st['mark']}"


def test_request_tokens_never_shrinks_the_window(work, monkeypatch, caplog):
    """请求体积只作见证：它偏大不会让销账越过窗口，偏大 >20% 要打警告。"""
    import asyncio
    import logging

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "700")
    from src.storage import session_facade

    async def seed():
        await session_facade.append_many(wid, cid, [
            {"actor": "human", "content": "六" * 6},
            {"actor": "tool", "content": "工" * 600, "tool_call_id": "c1"},
            {"actor": "assistant", "content": "六" * 6},
        ])

    asyncio.run(seed())

    async def fake(prompt: str) -> str:
        return "【结论】压一批【依据】块0"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    with caplog.at_level(logging.WARNING, logger="storage.memory_store"):
        asyncio.run(memory_store.condense_turn_end(wid, cid, request_tokens=1_040_000))
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 3, f"窗口 3 块全在预算内 ⇒ mark=3，实际 {st['mark']}"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("三尺偏差告警" in m for m in msgs), msgs


# ---------------------------------------------------------------- 发送前预检（防超窗死锁）

@pytest.mark.asyncio
async def test_preflight_condenses_when_over_line(work, monkeypatch):
    """未凝量到线 ⇒ 发请求前先凝一批（否则超窗失败后永远不触发）。"""
    import asyncio

    from src.storage import session_facade

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "50")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "300000")

    await session_facade.append_many(wid, cid, [
        {"actor": "human", "content": "六" * 200},
        {"actor": "assistant", "content": "六" * 200},
    ])

    async def fake(prompt: str) -> str:
        return "【结论】预检压了【依据】块0"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    ran = await memory_store.condense_before_send(wid, cid)
    st = memory_store.load_memory(wid, cid)
    assert ran is True
    assert int(st.get("mark") or 0) > 0, "到线就该凝并推进 mark"


@pytest.mark.asyncio
async def test_preflight_does_nothing_below_line(work, monkeypatch):
    """未到线 ⇒ 什么都不做（不调模型、mark 不动）。"""
    import asyncio

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")
    called: list[str] = []

    async def fake(prompt: str) -> str:
        called.append(prompt)
        return "不该被调用"

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    ran = await memory_store.condense_before_send(wid, cid)
    assert ran is False
    assert called == []
