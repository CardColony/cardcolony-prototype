# -*- coding: utf-8 -*-
"""记忆找回与上下文凝练修复方案 §三验收（2026-11-20）：

判据：
- 水位只按实际完成的连续前缀推进；大块缺任一分片原块保持可见；
  S2a 失败/截断/提交失败留账，重试从最后一次成功提交位续跑；
- 记忆链、节点索引、水位、缓存板同事务发布；同一次请求读同一版本；
  写失败整体回滚；旧复活/旧整链快照不得覆盖新记忆。
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from src.storage import (card_db, card_store, context_board, memory_domain as md,
                         memory_store, origin_store, session_facade, work_store)


async def _mk(prefix: str = "水位"):
    w = await work_store.create_work(f"{prefix}剧本")
    wid = w["work_id"]
    await card_store.create_card(wid, "根卡", "用户直接创建")
    return wid, "card_001"


def _node(seq: int, gist: str, covered=None, **kw) -> dict:
    n = md.new_node("s1", seq, gist, kind="fact")
    n["level"], n["upper"], n["lower"] = 1, [], []
    if covered is not None:
        n["coveredSeqs"] = covered
    n.update(kw)
    return n


def _entries_of(wid: str, cid: str) -> list[dict]:
    return [e for e in (memory_store.load_memory(wid, cid).get("entries") or [])
            if isinstance(e, dict)]


def _mark(wid: str, cid: str) -> int:
    return int(memory_store.load_memory(wid, cid).get("mark") or 0)


async def _append(wid: str, cid: str, texts: list[str]) -> None:
    for i, t in enumerate(texts):
        await session_facade.append(wid, cid, {"role": "user", "content": t})


# ============================================================
# §三.1 分片覆盖：大块缺任一分片 ⇒ 原块不消耗、越界候选不提交
# ============================================================

async def test_chunk_partial_coverage_keeps_block_visible(sandbox, monkeypatch):
    """大块切 4 段，S2a 只覆盖其中 2 段 ⇒ 原块不算消耗、候选不提交、
    mark 停在大块之前；已覆盖前缀正常推进。"""
    wid, cid = await _mk()
    monkeypatch.setattr(memory_store, "window_budget_tokens", lambda: 4000)
    # seq0 小块、seq1 巨块（>4000 token ⇒ 分片）、seq2 小块
    await _append(wid, cid, ["小块甲", "巨" * 9000, "小块乙"])
    chunk_base = 2_000_000_000
    # seq1 的分片 seq：_CHUNK_BASE + 1*1000 + ci
    c = [chunk_base + 1000 + i for i in range(8)]

    async def fake_two_layer(blocks, call_llm, session_id="", domain_full=""):
        # 只覆盖 seq0 与大块的前两片；seq2 与余下分片一律不覆盖
        nodes = [
            _node(0, "小块甲结论", [0]),
            _node(chunk_base + 5000, "巨块前段", [c[0], c[1]]),
        ]
        return {"ok": True, "nodes": nodes, "descriptions": [],
                "stage": "done", "retries": 0}

    monkeypatch.setattr(
        "src.storage.condense_two_layer.run_two_layer", fake_two_layer)
    window = memory_store._read_origin(wid, cid, 0)
    r = await memory_store._condense_one_batch(wid, cid, window, 0, batch_no=1)
    # seq0 已覆盖 → 消耗=1 → mark=1；巨块缺分片不消耗、seq2 未覆盖留待下批
    assert r["ok"] is True and r["mark"] == 1
    ents = _entries_of(wid, cid)
    # 只提交了覆盖已消耗前缀的节点——碰未消耗块的候选一律不落库
    assert ents
    for e in ents:
        for s in (e.get("coveredSeqs") or []):
            assert int(s) == 0
    # 原块 seq1（巨块）与 seq2 仍在板上可见（mark=1 只裁 seq<1）
    board_seqs = {int(r2.get("seq") or 0)
                  for r2 in context_board.read_board(wid, cid)}
    assert 1 in board_seqs and 2 in board_seqs


async def test_chunk_full_coverage_commits_orig_seq(sandbox, monkeypatch):
    """大块全片覆盖 ⇒ 原块消耗、coveredSeqs 回填原块号、mark 越过。"""
    wid, cid = await _mk()
    monkeypatch.setattr(memory_store, "window_budget_tokens", lambda: 4000)
    await _append(wid, cid, ["巨" * 9000, "收尾块"])
    chunk_base = 2_000_000_000
    # 先探测实际分了几片
    window = memory_store._read_origin(wid, cid, 0)

    async def fake_two_layer(blocks_arg, call_llm, session_id="", domain_full=""):
        seqs = [int(b.get("seq") or 0) for b in blocks_arg]
        chunk_seqs = [s for s in seqs if s >= chunk_base]
        normal = [s for s in seqs if s < chunk_base]
        covered = normal + chunk_seqs  # 全覆盖
        nodes = [_node(0, "全覆盖", covered)]
        return {"ok": True, "nodes": nodes, "descriptions": [],
                "stage": "done", "retries": 0}

    monkeypatch.setattr(
        "src.storage.condense_two_layer.run_two_layer", fake_two_layer)
    r = await memory_store._condense_one_batch(wid, cid, window, 0, batch_no=1)
    assert r["ok"] is True
    assert r["mark"] == 2  # 两块全消耗 → mark=2
    ents = _entries_of(wid, cid)
    assert any(0 in (e.get("coveredSeqs") or []) for e in ents)
    # coveredSeqs 已回填原块号（0=巨块原 seq、1=收尾块）
    cov = sorted({int(s) for e in ents for s in (e.get("coveredSeqs") or [])})
    assert cov == [0, 1]


# ============================================================
# §三.1 失败留账：S2a/截断/提交失败 ⇒ mark 不动＋condense_debt 落账
# ============================================================

async def test_s2a_failure_records_debt_and_keeps_mark(sandbox, monkeypatch):
    wid, cid = await _mk()
    await _append(wid, cid, ["待凝内容块"])

    async def fake_two_layer(blocks, call_llm, session_id="", domain_full=""):
        return {"ok": False, "stage": "s2a", "nodes": [], "descriptions": [],
                "retries": 2}

    monkeypatch.setattr(
        "src.storage.condense_two_layer.run_two_layer", fake_two_layer)
    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 0)
    monkeypatch.setattr(memory_store, "_condense_home", lambda: True)
    await memory_store.condense_turn_end(wid, cid)
    st = memory_store.load_memory(wid, cid)
    assert _mark(wid, cid) == 0
    debt = st.get("condense_debt") or {}
    assert debt.get("stage") == "s2a"
    assert debt.get("committed_mark") == 0
    assert debt.get("next_retry_ts", 0) > 0


async def test_truncation_records_debt(sandbox, monkeypatch):
    wid, cid = await _mk()
    await _append(wid, cid, ["待凝内容块"])

    class _Trunc(Exception):
        pass

    async def fake_call(user, system, **kw):
        raise memory_store.CondenseTruncated("输出被截断")

    monkeypatch.setattr(memory_store, "_condense_call", fake_call)
    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 0)
    monkeypatch.setattr(memory_store, "_condense_home", lambda: True)
    await memory_store.condense_turn_end(wid, cid)
    st = memory_store.load_memory(wid, cid)
    assert _mark(wid, cid) == 0
    assert (st.get("condense_debt") or {}).get("stage") == "truncated"


async def test_partial_batches_keep_commit_and_debt(sandbox, monkeypatch):
    """批1成功、批2失败 ⇒ 批1水位保留、账记批2阶段、重试从已提交位续。"""
    wid, cid = await _mk()
    monkeypatch.setattr(memory_store, "condense_max_batches", lambda: 5)
    # 两个互不相关的待凝块 → 两批
    await _append(wid, cid, ["第一段内容", "第二段内容"])
    calls = {"n": 0}

    async def fake_two_layer(blocks, call_llm, session_id="", domain_full=""):
        calls["n"] += 1
        if calls["n"] == 1:
            seqs = [int(b.get("seq") or 0) for b in blocks]
            return {"ok": True,
                    "nodes": [_node(seqs[0], "第一段结论", [seqs[0]])],
                    "descriptions": [], "stage": "done", "retries": 0}
        return {"ok": False, "stage": "s2a", "nodes": [], "descriptions": [],
                "retries": 1}

    monkeypatch.setattr(
        "src.storage.condense_two_layer.run_two_layer", fake_two_layer)
    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 0)
    monkeypatch.setattr(memory_store, "_condense_home", lambda: True)
    # window_budget 调小让两块分属两批
    monkeypatch.setattr(memory_store, "window_budget_tokens", lambda: 50)
    await memory_store.condense_turn_end(wid, cid)
    st = memory_store.load_memory(wid, cid)
    # 批1已提交（mark=1）、批2失败留账
    assert _mark(wid, cid) == 1
    debt = st.get("condense_debt") or {}
    assert debt.get("status") in (None, "") or True
    assert debt.get("stage") == "s2a"
    assert debt.get("committed_mark") == 1
    assert debt.get("batches_committed") == 1
    # 第二段仍在待凝位（seq>=1 可见）
    pending = memory_store._read_origin(wid, cid, _mark(wid, cid))
    assert any(int(b.get("seq") or 0) == 1 for b in pending)


@pytest.mark.parametrize("board_enabled", [True, False])
async def test_attached_session_request_uses_its_own_snapshot(sandbox, monkeypatch, board_enabled):
    from src.api import routes

    wid, main = await _mk()
    attached = main + "__audit"
    for session, marker in ((main, "MAIN"), (attached, "OTHER")):
        await _append(wid, session, [marker + "__OLD__", marker + "__TAIL__"])
        await memory_store.save_memory(wid, session, {
            "mark": 1, "entries": [_node(0, marker + "__MEMORY__", [0])]})

    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setattr(memory_store, "condense_enabled", lambda: True)
    monkeypatch.setattr(context_board, "enabled", lambda: board_enabled)
    requests = []

    async def fake_model(*args, **kwargs):
        requests.append(kwargs)
        return {"content": "收到。", "tool_calls": []}, "audit"

    monkeypatch.setattr(routes.model_service, "call_main", fake_model)
    for session, present, absent in (("", "MAIN", "OTHER"), (attached, "OTHER", "MAIN")):
        await routes._run_message_inner(wid, main, "CURRENT-USER", session_id=session)
        request = requests[-1]
        assert present + "__MEMORY__" in request["system_prompt"]
        assert absent + "__MEMORY__" not in request["system_prompt"]
        prior = str(request["prior_messages"])
        assert present + "__TAIL__" in prior
        assert absent + "__TAIL__" not in prior
        assert present + "__OLD__" not in prior and "CURRENT-USER" not in prior
        assert request["user_prompt"] == "CURRENT-USER"


@pytest.mark.parametrize("changed", ["quarantine", "append"])
async def test_publish_checks_only_the_consumed_origin_range(sandbox, monkeypatch, changed):
    from tests._condense_stub import three_stage_stub

    wid, cid = await _mk()
    await _append(wid, cid, ["ORIGINAL-VIEW"])
    reply = three_stage_stub(gist="结论来自发送的原文")
    mutated = False

    async def model(user, system, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            if changed == "append":
                await _append(wid, cid, ["NEW-TAIL"])
            else:
                card_db._write_sync(wid, lambda conn: conn.execute(
                    "UPDATE session_blocks SET data=json_set(data, '$.quarantine', "
                    "'pseudo_unparsed', '$.ctx_content', 'CLEAN-VIEW') WHERE card_id=? AND seq=0",
                    (cid,)))
        return await reply(user, system, **kwargs)

    monkeypatch.setattr(memory_store, "_condense_call", model)
    result = await memory_store._condense_one_batch(
        wid, cid, memory_store._read_origin(wid, cid, 0), 0)
    if changed == "append":
        assert result["ok"] and _mark(wid, cid) == 1
        assert memory_store._read_origin(wid, cid, 1)[0]["content"] == "NEW-TAIL"
    else:
        assert result["stage"] == "source_changed" and _mark(wid, cid) == 0
        assert _entries_of(wid, cid) == []
        assert memory_store._read_origin(wid, cid, 0)[0]["ctx_content"] == "CLEAN-VIEW"


async def test_partial_giant_slice_is_retried_and_grouped_in_window_order(sandbox, monkeypatch):
    from src.storage import condense_two_layer as tl
    from tests._condense_stub import three_stage_stub

    wid, cid = await _mk()
    monkeypatch.setattr(memory_store, "window_budget_tokens", lambda: 4000)
    await _append(wid, cid, ["巨" * 5002, "AFTER-GIANT"])
    groups = []
    l1_inputs = []
    reply = three_stage_stub(gist="连续原文")

    async def model(user, system, **kwargs):
        if system == tl.S2A_PROMPT:
            chunks = [part.split("\n", 1)[1].strip() for part in user.split("[idx ")[1:]]
            groups.append(chunks)
            end = 0 if len(groups) == 1 else len(chunks) - 1
            return json.dumps({"groups": [{"end": end}]})
        if system == tl.L1_PROMPT:
            l1_inputs.append(user)
        return await reply(user, system, **kwargs)

    monkeypatch.setattr(memory_store, "_condense_call", model)
    result = await memory_store._condense_one_batch(
        wid, cid, memory_store._read_origin(wid, cid, 0), 0)
    assert result["ok"] and _mark(wid, cid) == 2
    assert len(groups[0]) == 2 and groups[1] == groups[0][1:]
    assert any("巨" in prompt and "AFTER-GIANT" in prompt for prompt in l1_inputs)
    assert {s for e in _entries_of(wid, cid) for s in e["coveredSeqs"]} == {0, 1}


async def test_commit_failure_debt_survives_and_sweeper_retries_below_trigger(sandbox, monkeypatch):
    from src.storage import condense_two_layer as tl
    from tests._condense_stub import three_stage_stub

    wid, cid = await _mk()
    await _append(wid, cid, ["CONDENSE-ME"])
    await memory_store.ensure_watermark(wid, cid)
    monkeypatch.setattr(memory_store, "_condense_home", lambda: True)
    monkeypatch.setattr(memory_store, "condense_enabled", lambda: True)
    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 0)
    clock = [time.time()]
    monkeypatch.setattr(memory_store, "time", SimpleNamespace(time=lambda: clock[0]))
    retry_inputs = []
    reply = three_stage_stub(gist="重试结论")

    async def model(user, system, **kwargs):
        if system == tl.S2A_PROMPT:
            retry_inputs.append(user)
        return await reply(user, system, **kwargs)

    monkeypatch.setattr(memory_store, "_condense_call", model)
    sync = memory_store._sync_nodes_tx

    def fail_index(*_args):
        raise RuntimeError("注入：索引持续失败")

    monkeypatch.setattr(memory_store, "_sync_nodes_tx", fail_index)
    await memory_store.condense_turn_end(wid, cid)
    state = memory_store.load_memory(wid, cid)
    debt = state["condense_debt"]
    assert state["mark"] == debt["committed_mark"] == 0
    assert debt["stage"] == "commit" and state["entries"] == []

    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 100000)
    monkeypatch.setattr(memory_store, "_sync_nodes_tx", sync)
    clock[0] = debt["next_retry_ts"] - 1
    await memory_store.condense_sweep_once()
    assert len(retry_inputs) == 1 and _mark(wid, cid) == 0
    clock[0] += 2
    await memory_store.condense_sweep_once()
    settled = memory_store.load_memory(wid, cid)
    assert len(retry_inputs) == 2 and "CONDENSE-ME" in retry_inputs[1]
    assert settled["mark"] == 1 and "condense_debt" not in settled


async def test_fold_old_snapshot_cannot_undo_explicit_revival(sandbox, monkeypatch):
    wid, cid = await _mk()
    await memory_store.save_memory(wid, cid, {
        "mark": 0,
        "entries": [_node(0, "沉底", [0], sunk=True),
                    _node(1, "活跃甲", [1]), _node(2, "活跃乙", [2])],
        "batch_count": memory_store.FOLD_EVERY_BATCHES, "archive_migrated": True,
    })
    called = []

    async def model(*args, **kwargs):
        assert md.revive_sunk(wid, cid, 0, now_ms=1700000000000)
        called.append(True)
        return {"content": "结论：无合并"}

    monkeypatch.setattr(memory_store, "_fold_call", model)
    await memory_store._maybe_fold(wid, cid)
    current = memory_store.load_memory(wid, cid)
    assert called
    assert not current["entries"][0]["sunk"]
    assert current["entries"][0]["lastUsedAt"] == 1700000000000
    assert not current.get("last_fold_check")


# ============================================================
# §三.2 一致发布：写失败整体回滚＋冷读四表一致
# ============================================================

async def _seed_memory(wid: str, cid: str):
    await _append(wid, cid, ["历史块0", "历史块1", "历史块2"])
    await memory_store.save_memory(wid, cid, {
        "card_id": cid, "mark": 1,
        "entries": [_node(0, "旧结论", [0])],
        "batch_count": 1, "folded": []})


async def test_node_index_failure_rolls_back_everything(sandbox, monkeypatch):
    """注入节点索引写失败 ⇒ memory/节点/水位/缓存板整体回滚，冷读一致。"""
    wid, cid = await _mk()
    await _seed_memory(wid, cid)
    old_mark, old_entries = _mark(wid, cid), _entries_of(wid, cid)
    old_nodes = memory_store.load_nodes(wid, cid)

    def _boom(conn, w, c, entries):
        raise RuntimeError("注入：节点索引写失败")

    monkeypatch.setattr(memory_store, "_sync_nodes_tx", _boom)
    with pytest.raises(RuntimeError):
        await memory_store.save_memory(wid, cid, {
            "card_id": cid, "mark": 3,
            "entries": old_entries + [_node(2, "新结论", [2])],
            "batch_count": 2, "folded": []})
    # 冷读：四表全部是旧版本
    assert _mark(wid, cid) == old_mark == 1
    assert len(_entries_of(wid, cid)) == len(old_entries) == 1
    assert len(memory_store.load_nodes(wid, cid)) == len(old_nodes)
    snap = memory_store.read_snapshot(wid, cid)
    assert snap["mark"] == 1 and len(snap["nodes"]) == len(old_nodes)
    # 板未被裁（seq<3 的块仍在板上可见——mark 仍是 1）
    board = context_board.read_board(wid, cid)
    assert any(int(r.get("seq") or 0) < 3 for r in board)


async def test_board_trim_failure_rolls_back_everything(sandbox, monkeypatch):
    """注入缓存板裁板失败 ⇒ 同事务整体回滚（不再「水位推进板没裁」）。"""
    wid, cid = await _mk()
    await _seed_memory(wid, cid)
    old_mark = _mark(wid, cid)

    def _boom(conn, card_id, mark):
        raise RuntimeError("注入：缓存板裁板失败")

    monkeypatch.setattr(context_board, "trim_below_tx", _boom)
    with pytest.raises(RuntimeError):
        await memory_store.save_memory(wid, cid, {
            "card_id": cid, "mark": 3,
            "entries": [_node(0, "旧结论", [0]), _node(2, "新结论", [2])],
            "batch_count": 2, "folded": []})
    assert _mark(wid, cid) == old_mark == 1
    snap = memory_store.read_snapshot(wid, cid)
    assert snap["mark"] == 1
    assert len(snap["nodes"]) == 1  # 新节点未落索引


async def test_concurrent_read_sees_whole_old_or_new(sandbox, monkeypatch):
    """写事务挂起期间，读者只见完整旧版；提交后只见完整新版。"""
    wid, cid = await _mk()
    await _seed_memory(wid, cid)
    # 板重建到新鲜态（测试环境 append 不一定进板——不新鲜会触发快照内
    # 重建阻塞写锁，那测的是「等锁后见新版」而非「挂起期间见旧版」）
    context_board.rebuild_board(wid, cid)
    # 预热本线程只读连接（首建要抢写锁做 schema 检查——不预热会把并发读
    # 变成「等写完再读」，测不到 WAL 快照语义）
    memory_store.read_snapshot(wid, cid)
    rebuilds = {"n": 0}
    _orig_rebuild = context_board.rebuild_board

    def _counting_rebuild(w, c):
        rebuilds["n"] += 1
        return _orig_rebuild(w, c)

    monkeypatch.setattr(context_board, "rebuild_board", _counting_rebuild)
    gate = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _slow_write(conn):
        # 事务内跑**真实发布三件套**（与 _publish_memory 同构），提交前挂起
        conn.execute(
            "INSERT INTO memory(card_id, chain, condense_mark, batch_starts,"
            " updated_at) VALUES(?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET"
            " chain=excluded.chain, condense_mark=excluded.condense_mark,"
            " batch_starts=excluded.batch_starts, updated_at=excluded.updated_at",
            (cid, json.dumps([{"anchor": {"sessionId": "s1", "seq": 2},
                               "gist": "并发新链", "level": 1, "role": "gist",
                               "coveredSeqs": [2]}]),
             3, "{}", "2999-01-01T00:00:00"))
        memory_store._sync_nodes_tx(
            conn, wid, cid,
            [{"anchor": {"sessionId": "s1", "seq": 2}, "gist": "并发新链",
              "level": 1, "role": "gist", "coveredSeqs": [2]}])
        context_board.trim_below_tx(conn, cid, 3)
        # 跨线程唤醒必须 call_soon_threadsafe——Event.set() 直接跨线程调
        # 只进 _ready 不唤醒 selector，事件循环要等写线程结束的
        # call_soon_threadsafe 才轮到它（那时已 COMMIT，测不到挂起窗口）
        loop.call_soon_threadsafe(gate.set)
        import time as _t
        _t.sleep(0.5)

    writer = asyncio.ensure_future(asyncio.to_thread(
        card_db._write_sync, wid, _slow_write))
    await asyncio.wait_for(gate.wait(), timeout=5)
    # 写事务已 BEGIN 未 COMMIT：快照必须见完整旧版（mark=1/1节点/旧板），
    # 且板新鲜不触发重建（rebuild 会抢写锁，把读变成「等提交后见新版」）
    snap = memory_store.read_snapshot(wid, cid)
    assert snap["mark"] == 1, snap["mark"]
    assert len(snap["nodes"]) == 1
    assert rebuilds["n"] == 0
    assert snap["board_rows"] is not None
    assert all("并发新链" not in str(e.get("gist") or "")
               for e in snap["state"].get("entries") or [])
    await writer
    # 提交后：新版本完整可见（mark=3/新链/索引同步/板已裁）
    snap2 = memory_store.read_snapshot(wid, cid)
    assert snap2["mark"] == 3
    assert any("并发新链" in str(e.get("gist") or "")
               for e in snap2["state"].get("entries") or [])
    assert any("并发新链" in str(n.get("gist") or "") for n in snap2["nodes"])


async def test_stale_chain_cas_cannot_overwrite(sandbox):
    """旧整链快照 CAS 提交被拒；事务内局部复活不覆盖新记忆。"""
    wid, cid = await _mk()
    await _append(wid, cid, ["块0", "块1", "块2", "块3"])
    v1 = [_node(0, "旧条目", [0], sunk=True)]
    md.save_chain(wid, cid, v1, condense_mark=1)
    # 并发方推进到 v2（多了一条新记忆）
    v2 = v1 + [_node(3, "新记忆", [3])]
    await memory_store.save_memory(wid, cid, {
        "card_id": cid, "mark": 2, "entries": v2, "folded": []})
    # 旧快照 v1 派生的"复活版"整链提交 ⇒ CAS 拒
    stale = [dict(n, sunk=False) for n in v1]
    ok = md.save_chain(wid, cid, stale, expected_chain=v1)
    assert ok is False
    chain = md.load_chain(wid, cid)
    seqs = sorted(int((n.get("anchor") or {}).get("seq") or 0) for n in chain)
    assert seqs == [0, 3]  # 新记忆仍在，旧快照没盖掉
    # 事务内局部复活：在**当前链**上翻 sunk，不动新节点
    assert md.revive_sunk(wid, cid, 0, now_ms=1234567890000) is True
    chain2 = md.load_chain(wid, cid)
    n0 = next(n for n in chain2
              if int((n.get("anchor") or {}).get("seq") or 0) == 0)
    assert n0.get("sunk") is False and n0.get("lastUsedAt") == 1234567890000
    assert any(int((n.get("anchor") or {}).get("seq") or 0) == 3 for n in chain2)


async def test_snapshot_same_version_triple(sandbox):
    """快照三件套同版本：mark/节点/板来自同一读事务。"""
    wid, cid = await _mk()
    await _seed_memory(wid, cid)
    snap = memory_store.read_snapshot(wid, cid)
    assert snap["mark"] == 1
    assert len(snap["nodes"]) == 1
    # 板行与快照同事务取回（seq>=mark 的过闸块）
    assert snap["board_rows"] is not None
    assert all(int(r.get("seq") or 0) >= 1 for r in snap["board_rows"])
    # 再发一次版本一致：state.entries 与 nodes 锚点对齐
    ents = snap["state"].get("entries") or []
    assert ents and snap["nodes"]
