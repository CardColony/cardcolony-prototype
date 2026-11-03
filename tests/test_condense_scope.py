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

from tests._condense_stub import three_stage_stub


def _mk(seq: int, actor: str, content: str) -> dict:
    return {"seq": seq, "actor": actor, "content": content}


def _tool(seq: int, n: int = 600) -> dict:
    return _mk(seq, "tool", "工" * n)


# ---------------------------------------------------------------- C2 / C3

def test_window_budget_ignores_tool_blocks():
    """窗口预算按**进上下文的量**算：工具块不占预算，但仍留在窗口内供销账推进。"""
    pending = [
        _mk(0, "human", "六" * 6),      # 13 token
        _tool(1),                       # 600 字，但**不进上下文 ⇒ 不占预算**
        _mk(2, "assistant", "六" * 6),  # 13 token
        _mk(3, "human", "六" * 6),      # 13 token
    ]
    win = memory_store._truncate_window(pending, 30)
    seqs = [m["seq"] for m in win]
    print("窗口 seq:", seqs)
    assert seqs == [0, 1, 2], "两条正文 26 token ≤ 30，第三条越线；工具块跟着进窗"
    assert memory_store._context_meter(win) <= 30


def test_mark_advances_past_tool_blocks(work, monkeypatch):
    """销账含工具结果：窗口末块是工具块时，mark 也要越过它。"""
    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "700")
    pending = [_mk(0, "human", "六" * 6), _tool(1), _mk(2, "assistant", "六" * 6)]

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="有结论"))
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

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="不该被调用", record=called))
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

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="触发了", record=ran))
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

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="不该触发", record=ran))
    import asyncio
    asyncio.run(memory_store.condense_turn_end(wid, cid))
    assert ran == [], "没给真实体积、块量又不到线 ⇒ 不触发"


# ---------------------------------------------------------------- 三尺同尺

def test_three_rulers_share_one_source(work, monkeypatch):
    """触发、窗口、销账读同一份数据：销账落在**窗口末块**，不因请求体积偏大而多推。"""
    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "30")
    pending = [_mk(0, "human", "六" * 6), _tool(1), _mk(2, "assistant", "六" * 6),
               _mk(3, "human", "六" * 6)]

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="只压第一批"))
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

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="压一批"))
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

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="预检压了"))
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

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="不该被调用", record=called))
    ran = await memory_store.condense_before_send(wid, cid)
    assert ran is False
    assert called == []


# ---------------------------------------------------------------- 条目 seq 身份

def test_entries_get_distinct_seqs_without_block_refs():
    """旧格式条目不带块号时，按窗口内可送块**逐条分配**，不再全落窗口首块。"""
    txt = "【结论】A【依据】无块号\n【结论】B【依据】无块号\n【结论】C【依据】无块号"
    got = [e["seq"] for e in memory_store._parse_condense_output(txt, 0, [10, 20, 30])]
    assert got == [10, 20, 30], got
    assert len(set(got)) == 3, "不许重复"


def test_entries_with_block_refs_keep_their_seq():
    """带块号的条目仍以自己的块号为准，且不与他人冲突。"""
    txt = "【结论】A【依据】块20\n【结论】B【依据】无块号"
    got = [e["seq"] for e in memory_store._parse_condense_output(txt, 0, [10, 20, 30])]
    assert got[0] == 20, got
    assert got[1] in (10, 30), got
    assert got[0] != got[1], "两条不能同 seq"


def test_more_entries_than_blocks_still_unique():
    """条目数超过可送块数时也不重复（超出的退到默认 seq 之前先避让）。"""
    txt = "".join(f"【结论】{i}【依据】无块号\n" for i in range(5))
    got = [e["seq"] for e in memory_store._parse_condense_output(txt, 0, [1, 2])]
    assert got[:2] == [1, 2]
    assert len(set(got[:2])) == 2


@pytest.mark.asyncio
async def test_preflight_loops_until_under_target(work, monkeypatch):
    """超窗的卡一批压不够 ⇒ 预检要**连跑多批**，直到降到目标以下。"""
    from src.storage import session_facade

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "100")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "100")
    monkeypatch.setenv("CONDENSE_MAX_BATCHES", "1")
    monkeypatch.setenv("MODEL_WINDOW_TOKENS", "300")

    # 3 段内容，每段约 100 token ⇒ 需要 2~3 批才降到 270 以下
    await session_facade.append_many(wid, cid, [
        {"actor": "human", "content": "六" * 120},
        {"actor": "assistant", "content": "六" * 120},
        {"actor": "human", "content": "六" * 120},
        {"actor": "assistant", "content": "六" * 120},
        {"actor": "human", "content": "六" * 120},
    ])

    batches: list[str] = []

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="压了", record=batches))
    ran = await memory_store.condense_before_send(wid, cid)
    st = memory_store.load_memory(wid, cid)
    print("跑了", len(batches), "批 | mark =", st.get("mark"))
    assert ran is True
    assert len(batches) >= 2, f"一批压不够就该继续跑，实际只跑了 {len(batches)} 批"


@pytest.mark.asyncio
async def test_condense_runs_even_when_turn_lock_held(work, monkeypatch):
    """主线回合占着锁时，凝练照跑——凝练是后台任务，本来就不该碰回合锁。"""
    from src.storage import session_facade

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "10")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "300000")

    await session_facade.append_many(wid, cid, [
        {"actor": "human", "content": "六" * 50},
        {"actor": "assistant", "content": "六" * 50},
    ])

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="锁着也压了"))

    # 主线回合先占锁（模拟 routes 里 finally 还没释放的那段）
    assert session_facade.turn_try_acquire(wid, cid, "main") is True
    try:
        await memory_store.condense_turn_end(wid, cid, request_tokens=100)
    finally:
        session_facade.turn_release(wid, cid, "main")
    st = memory_store.load_memory(wid, cid)
    assert int(st.get("mark") or 0) > 0, "锁被占也必须凝（否则永远凝不了）"


@pytest.mark.asyncio
async def test_sweeper_condenses_without_any_turn(work, monkeypatch):
    """纯后台：**没有任何回合**，扫描器自己按触发线把卡凝掉。"""
    from src.storage import session_facade

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "10")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "300000")
    monkeypatch.setenv("CONDENSE_MAX_BATCHES", "1")

    await session_facade.append_many(wid, cid, [
        {"actor": "human", "content": "六" * 60},
        {"actor": "assistant", "content": "六" * 60},
    ])

    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="后台自己凝的"))
    n = await memory_store.condense_sweep_once()
    st = memory_store.load_memory(wid, cid)
    print("扫到", n, "张 | mark =", st.get("mark"))
    assert n >= 1, "到线的卡应该被后台扫到"
    assert int(st.get("mark") or 0) > 0, "没有任何回合也要凝"


def test_sweeper_maps_fn_session_files():
    """扫描器要能把 session_fn_004.md 还原成 fn_004（否则职能卡永远扫不到、永不凝）。"""
    f = memory_store._card_id_from_session_stem
    assert f("session_004") == "card_004"
    assert f("session_fn_004") == "fn_004"
    assert f("session_card_004") == "card_004"
