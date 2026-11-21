# -*- coding: utf-8 -*-
"""[项目统筹 2026-11-21 · 任务三] 事件接入＋合并窗口＋到期标记。

钉的事（对齐任务书任务三）：
T1  六类事件进批：card_block_edited / goal\/change / msg_directive /
    worker_done / review_batch_closed 进 open 批次（coord 契约、范围过滤）
T2  msg_report 仅关联事项（来源卡=计划 items 责任卡）进批；其余类型
    （逐字流/工具逐跳/统筹记账）不进批——防统筹自唤醒
T3  坐标去重：同一 (任命,卡,seq) 重复 feed 只进批一次（持久化）
T4  合并窗口：窗口内合批；超窗派弱模型观察（mock 断言批次内容）；
    无事件循环时批次留 open 由 tick 补派
T5  到期回看标记：next_review_at 过期 ⇒ pending 标 review_due（同事务核对
    活跃）；**不推进到期值**（欠办保留原值）；恢复后不重复登记
T6  暂停中的任命不进批
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.api import coordination_runner, function_room, model_config, model_service, routes
from src.infra import coordination_watcher
from src.storage import card_store, coordination_store, function_store, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


async def _until(check):
    for _ in range(100):
        if check():
            return
        await asyncio.sleep(0.02)
    assert check()


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()
    monkeypatch.setattr(model_config, "load",
                        lambda: {"models": {"strong-test-route": {},
                                            "weak-test-route": {}}})


async def _seeded_work(monkeypatch, title: str = "统筹任务三"):
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work(title)
    wid = wdata["work_id"]
    await function_room.seed_work(wid)
    fids = {}
    for p in function_store.list_profiles(wid):
        code = function_room.catalog_code_of(p)
        if code:
            fids[code] = str(p.get("function_id") or "")
    return wid, card_store.iter_card_metas(wid), fids


async def _mk_card(wid: str, title: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成",
                                     card_type="sub_project",
                                     skip_kick=True)
    return str(r["meta"]["card_id"])


def _future_ts(hours: float = 1.0) -> str:
    return (datetime.now(timezone(timedelta(hours=8)))
            + timedelta(hours=hours)).isoformat()


async def _appointed(monkeypatch, wid: str, fids: dict, plan_card: str,
                     **over) -> str:
    called = []

    async def _fake_first(work_id, card_id, assignment_id=""):
        called.append((work_id, card_id, assignment_id))
        return {"status": "parsed", "rev": 1}
    monkeypatch.setattr(routes, "_coord_first_turn", _fake_first)
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **{
            "models": {"strong": "strong-test-route",
                       "weak": "weak-test-route"},
            "next_review_at": _future_ts(),
            "observe": {"scope": "work"},
            "instruct": {"scope": "cards", "ids": ["card_x"]},
            "mode": "observe",
            "objective": "按现行需求推进底座重构",
            **over})
    assert out.get("ok"), out
    return out["appointment"]["assignment_id"]


def _rec(card: str, seq: int, etype: str,
         data: dict | None = None) -> dict:
    return {"seq": seq, "card_id": card, "type": etype,
            "data": data or {}, "run_id": "", "round": 0, "model": ""}


def card_db_read_changes(wid: str, pc: str, aid: str) -> list[dict]:
    from src.storage import card_db

    def _fn(conn):
        import sqlite3 as _sq
        try:
            return _query(conn)
        except _sq.OperationalError:
            return []

    def _query(conn):
        row = conn.execute(
            "SELECT changes FROM coordination_batches"
            " WHERE work_id=? AND plan_card_id=? AND assignment_id=?"
            " AND status='open' ORDER BY id DESC LIMIT 1",
            (wid, pc, aid)).fetchone()
        return json.loads(row["changes"]) if row else []
    return card_db.read_sync(wid, _fn, [])


# ---------------------------------------------------------------- T1 六类进批

async def test_six_event_types_enter_batch(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "进批计划")
    src_card = await _mk_card(wid, "来源卡")
    await _appointed(monkeypatch, wid, fids, plan_card)

    for i, etype in enumerate(coordination_watcher.WATCH_EVENT_TYPES):
        coordination_watcher.feed(
            wid, src_card, _rec(src_card, 100 + i, etype, {"k": i}))

    changes = card_db_read_changes(wid, plan_card,
                                   _appointed_ids(wid, plan_card))
    kinds = [c["kind"] for c in changes]
    assert kinds == list(coordination_watcher.WATCH_EVENT_TYPES)
    # coord 契约
    assert changes[0]["coord"] == f"event:{wid}:{src_card}:100"
    assert changes[0]["card_id"] == src_card
    assert "card_block_edited" in changes[0]["summary"]


def _appointed_ids(wid: str, pc: str) -> str:
    rows = coordination_store.list_plans(wid)
    for r in rows:
        if r["plan_card_id"] == pc:
            return r["assignment_id"]
    raise AssertionError("任命不存在")


# ---------------------------------------------------------------- T2 过滤

async def test_filtering_rules(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "过滤计划")
    owner_card = await _mk_card(wid, "责任卡")
    other_card = await _mk_card(wid, "无关卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)

    # 先登记计划 items（msg_report 的关联判据＝来源卡是责任卡）
    await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=0,
        plan={"objective": "按现行需求推进底座重构",
              "items": [{"item_id": "i1", "title": "t",
                         "owner_card": owner_card}]})

    # 逐字流/工具逐跳/记账事件不进批
    for i, etype in enumerate(("run_start", "turn_end", "step_start",
                               "deliver", "error", "done")):
        coordination_watcher.feed(wid, owner_card,
                                  _rec(owner_card, 1 + i, etype))
    # msg_report：来源卡不是责任卡 ⇒ 不进批
    coordination_watcher.feed(wid, other_card, _rec(other_card, 10, "msg_report"))
    coordination_watcher.feed(wid, owner_card, _rec(owner_card, 11, "msg_report"))
    for seq in (12, 13):
        coordination_watcher.feed(wid, owner_card, _rec(owner_card, seq, "msg_report", {
            "evidence_refs": ["evidence/one.md"],
        }))

    changes = card_db_read_changes(wid, plan_card, aid)
    kinds = [c["kind"] for c in changes]
    assert "run_start" not in kinds and "deliver" not in kinds  # 记账类全滤
    assert kinds == ["msg_report"]                              # 只有关联的软进批
    assert changes[0]["card_id"] == owner_card


async def test_scope_filtering_and_pause(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "范围暂停计划")
    in_scope = await _mk_card(wid, "范围内卡")
    out_scope = await _mk_card(wid, "范围外卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card, observe={
        "scope": "cards", "ids": [in_scope]})

    # 范围外不进批
    coordination_watcher.feed(wid, out_scope, _rec(out_scope, 1, "card_block_edited"))
    assert card_db_read_changes(wid, plan_card, aid) == []
    # 范围内进批
    coordination_watcher.feed(wid, in_scope, _rec(in_scope, 2, "card_block_edited"))
    assert len(card_db_read_changes(wid, plan_card, aid)) == 1

    # 暂停中的任命不进批
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause")
    coordination_watcher.feed(wid, in_scope, _rec(in_scope, 3, "card_block_edited"))
    assert len(card_db_read_changes(wid, plan_card, aid)) == 1  # 不增长


# ---------------------------------------------------------------- T3 去重

async def test_coord_dedup(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "去重计划")
    src_card = await _mk_card(wid, "来源卡")
    await _appointed(monkeypatch, wid, fids, plan_card)

    # 同一 (卡, seq) 重复 feed（两个 runner 同见）⇒ 只进批一次
    coordination_watcher.feed(wid, src_card, _rec(src_card, 5, "card_block_edited"))
    coordination_watcher.feed(wid, src_card, _rec(src_card, 5, "card_block_edited"))
    coordination_watcher.feed(wid, src_card, _rec(src_card, 5, "goal/change"))
    changes = card_db_read_changes(wid, plan_card, _appointed_ids(wid, plan_card))
    assert len(changes) == 1


# ---------------------------------------------------------------- T4 窗口派发

async def test_window_dispatch(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "窗口计划")
    src_card = await _mk_card(wid, "来源卡")
    await _appointed(monkeypatch, wid, fids, plan_card)

    captured = []
    clock = [coordination_watcher.now()]
    monkeypatch.setattr(coordination_watcher, "now", lambda: clock[0])

    async def _fake_weak(work_id, card_id, batch):
        captured.append({"work": work_id, "card": card_id, "batch": batch})
        return {"status": "passed", "batch_id": batch.get("batch_id")}
    monkeypatch.setattr(coordination_runner, "run_weak_observation", _fake_weak)

    # 窗口内（默认 120s）：第一条变化进批不派发
    coordination_watcher.feed(wid, src_card, _rec(src_card, 1, "card_block_edited"))
    assert captured == []
    assert len(card_db_read_changes(wid, plan_card,
                                    _appointed_ids(wid, plan_card))) == 1

    clock[0] += 60
    coordination_watcher.feed(wid, src_card, _rec(src_card, 2, "worker_done"))
    clock[0] += 61
    coordination_watcher.feed(wid, src_card, _rec(src_card, 3, "goal/change"))
    await _until(lambda: len(captured) == 1)
    assert len(captured) == 1
    assert captured[0]["work"] == wid and captured[0]["card"] == plan_card
    assert [c["kind"] for c in captured[0]["batch"]["changes"]] ==         ["card_block_edited", "worker_done"]      # 同批合并（open 批次追加）
    # 批次标 sent
    from src.storage import card_db

    def _fn(conn):
        coordination_watcher._ensure(conn)
        rows = conn.execute(
            "SELECT status FROM coordination_batches WHERE work_id=?",
            (wid,)).fetchall()
        return [r["status"] for r in rows]
    await _until(lambda: sorted(card_db.read_sync(wid, _fn, [])) == ["open", "sent"])


async def test_review_due_marking(sandbox, monkeypatch):
    """[任务三] 到期回看标记：过期 ⇒ pending 标 review_due；**不推进到期值**
    （欠办保留原值）；已标记不重复登记。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "到期计划")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    monkeypatch.setattr(coordination_watcher, "_spawn_strong_turn", lambda *args: False)

    # 注入时钟推到到期时间之后（测试无需真实等待）
    real_now = coordination_watcher.now()
    monkeypatch.setattr(coordination_watcher, "now",
                        lambda: real_now + 2 * 3600)

    out = coordination_watcher.tick(wid)
    assert out["review_marked"] == 1
    panel = coordination_store.panel(wid, plan_card)
    dues = [p for p in panel["pending"] if p["kind"] == "review_due"]
    assert len(dues) == 1
    assert dues[0]["due_at"] == panel["next_review_at"]  # 到期值未推进

    # 再 tick：记账不重复登记（仍一条 review_due），但**认领不阻塞**
    # [2026-11-21 · 用户令] 模型故障后恢复 ⇒ 每次到期 tick 都重新认领
    out2 = coordination_watcher.tick(wid)
    assert out2["review_marked"] == 0
    panel2 = coordination_store.panel(wid, plan_card)
    assert len([p for p in panel2["pending"]
                if p["kind"] == "review_due"]) == 1  # 记账不重复

    # plan_a 回看收口（实际完成回看才推进）：到期值推到将来＋欠办消除
    next_due = _future_ts(3)
    closure = await coordination_store.review_closure(
        wid, plan_card, assignment_id=aid, expected_rev=0,
        due_at=panel2["next_review_at"], receipt={
            "next_review_at": next_due, "open_items": ["需求未取得"],
            "evidence": [{"action": "coord_read_card", "status": "failed"}],
        })
    assert closure.get("ok")
    panel_c = coordination_store.panel(wid, plan_card)
    assert panel_c["next_review_at"] == next_due
    assert not any(p["kind"] == "review_due" for p in panel_c["pending"])

    # 未到期的任命不标记（注入时钟 +2h < plan_b 的 +3h）
    plan_b = await _mk_card(wid, "未到期计划")
    await coordination_store.appoint(
        wid, plan_b, function_id=fids["cto"],
        **{"models": {"strong": "strong-test-route", "weak": "weak-test-route"},
           "next_review_at": _future_ts(3), "observe": {"scope": "work"},
           "instruct": {"scope": "cards", "ids": ["card_x"]},
           "mode": "observe", "objective": "B 计划"})
    out3 = coordination_watcher.tick(wid)
    assert out3["review_marked"] == 0


# ---------------------------------------------------------------- T7 同步丢批修复

async def test_sync_path_no_batch_lost(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 同步入口落事件 ⇒ 窗口到 ⇒ 无事件循环 ⇒
    批次保留 open，
    tick 补派时弱模型真正起 1 轮。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "同步丢批计划")
    src_card = await _mk_card(wid, "来源卡")
    await _appointed(monkeypatch, wid, fids, plan_card)

    captured = []

    async def _fake_weak(work_id, card_id, batch):
        captured.append({"work": work_id, "card": card_id,
                         "batch": batch})
        return {"status": "passed", "batch_id": batch.get("batch_id")}
    monkeypatch.setattr(coordination_runner, "run_weak_observation", _fake_weak)
    monkeypatch.setattr(coordination_watcher, "merge_window_sec",
                        lambda w, p: 0)

    # 无事件循环的正式同步入口将批次留给主调度。
    from src.storage import event_log
    event = await asyncio.to_thread(event_log.append_sync, wid, src_card,
                                    {"type": "card_block_edited", "data": {"proof": "test"}})
    assert event["type"] == "card_block_edited"
    aid = _appointed_ids(wid, plan_card)

    def _status(conn):
        coordination_watcher._ensure(conn)
        row = conn.execute(
            "SELECT status FROM coordination_batches"
            " WHERE work_id=? AND plan_card_id=? AND assignment_id=?",
            (wid, plan_card, aid)).fetchone()
        return row["status"] if row else ""
    from src.storage import card_db
    assert card_db.read_sync(wid, _status, "") == "open"   # **没有丢批**

    # tick 补派（有事件循环）⇒ 弱模型真正起 1 轮，批次标 sent
    out = coordination_watcher.tick(wid)
    await _until(lambda: card_db.read_sync(wid, _status, "") == "sent")
    assert len(captured) == 1                              # 起了 1 轮（不是 0）
    assert captured[0]["batch"]["changes"][0]["card_id"] == src_card
    assert card_db.read_sync(wid, _status, "") == "sent"
    # 再 tick 不重复派发
    coordination_watcher.tick(wid)
    await asyncio.sleep(0.05)
    assert len(captured) == 1


# ---------------------------------------------------------------- T8 假时钟到期排轮

async def test_review_due_spawns_strong_turn(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 假时钟推过到期时间、无新事件 ⇒ tick **真正排起
    强模型**（trigger=review_due、绑定任命标识）；重复 tick 不重复排。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "到期排轮计划")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)

    spawned = []

    async def _fake_strong(work_id, card_id, assignment_id="", trigger="appoint"):
        spawned.append((work_id, card_id, assignment_id, trigger))
        return {"status": "error"}
    monkeypatch.setattr(coordination_runner, "run_strong_first_turn",
                        _fake_strong)

    # 假时钟推到到期时间之后
    real_now = coordination_watcher.now()
    monkeypatch.setattr(coordination_watcher, "now",
                        lambda: real_now + 2 * 3600)

    out = coordination_watcher.tick(wid)
    assert out["review_marked"] == 1
    assert out["review_spawned"] == 1
    for _ in range(50):
        if spawned:
            break
        await asyncio.sleep(0.02)
    assert spawned and spawned[0] == (wid, plan_card, aid, "review_due")

    # 重复 tick：记账不重复，但认领继续（同计划互斥不重复——第一个 spawn
    # 已完成，第二次认领是**合法的重新认领**）
    out2 = coordination_watcher.tick(wid)
    assert out2["review_marked"] == 0 and out2["review_spawned"] == 1
    for _ in range(50):
        if len(spawned) == 2:
            break
        await asyncio.sleep(0.02)
    assert len(spawned) == 2                 # 恢复后认领一次


async def test_review_closure_requires_evidence_and_runs_on_owner(client, sandbox, monkeypatch):
    from src.infra import procs
    from test_coordination_runner import PLAN_JSON

    wid, _, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "属主回看计划")
    source = await _mk_card(wid, "需求卡")
    await card_store.update_card(wid, source, {"requirements": "按正稿交付"}, "seed")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    due = coordination_store.panel(wid, plan_card)["next_review_at"]
    new_due = _future_ts(48)
    start = coordination_watcher.now()
    monkeypatch.setattr(coordination_watcher, "now", lambda: start + 7200)
    monkeypatch.setattr(procs, "_ROLE", "runner")
    monkeypatch.setattr(procs, "_RUNNER_COUNT", 2)
    monkeypatch.setattr(procs, "_RUNNER_INDEX", 0)
    monkeypatch.setattr(procs, "owner_index_of", lambda work, card: 1)
    owners = []
    outputs = [PLAN_JSON, {"action": "coord_read_card", "card_id": source},
               {"review": {"conclusion": "保留需求", "evidence_refs": ["read:1"],
                           "next_review_at": new_due}}]

    async def _model(system, user, **kwargs):
        owners.append(procs.runner_index())
        return {"content": json.dumps(outputs.pop(0)), "usage": {}}, "strong-test-route"

    async def _ctl(work, card, path, payload):
        assert path == "/ctl/coordination_turn" and payload["assignment_id"] == aid
        with monkeypatch.context() as scope:
            scope.setattr(procs, "_RUNNER_INDEX", 1)
            response = await client.post("/api" + path, json=payload)
        assert response.status_code == 200
        return response.json()

    monkeypatch.setattr(model_service, "call_main", _model)
    monkeypatch.setattr(procs, "forward_ctl", _ctl)
    key = (wid, plan_card)
    assert coordination_watcher.tick(wid)["review_spawned"] == 1
    await _until(lambda: key not in coordination_watcher._dispatching)
    first = coordination_store.panel(wid, plan_card)
    assert first["next_review_at"] == due and not first["last_review"]
    assert any(p["kind"] == "review_due" for p in first["pending"])
    assert coordination_watcher.tick(wid)["review_spawned"] == 1
    await _until(lambda: key not in coordination_watcher._dispatching)
    final = coordination_store.panel(wid, plan_card)
    assert owners == [1, 1, 1]
    assert final["next_review_at"] == new_due
    assert not any(p["kind"] == "review_due" for p in final["pending"])
    assert final["last_review"]["requirements"][0]["card_id"] == source
    assert final["last_review"]["evidence"][0]["ref"] == "read:1"


async def test_committed_event_replays_once_and_direct_node_skips_weak(sandbox, monkeypatch):
    from src.storage import event_log

    wid, _, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "节点计划")
    source = await _mk_card(wid, "来源卡")
    await event_log.append(wid, source, {"type": "worker_done", "data": {"result": "ok"}})
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    await coordination_store.save_plan(wid, plan_card, assignment_id=aid, expected_rev=0, plan={
        "objective": "看节点", "items": [{"item_id": "i1", "title": "t", "owner_card": source}],
        "direct_nodes": [{"node_id": "n1", "item_id": "i1", "card_id": source,
                          "event_type": "worker_done", "path": "result", "equals": "ok"}],
    })
    calls = []

    async def _strong(work, card, assignment_id, trigger, reason):
        calls.append((trigger, reason["evidence_refs"]))
        return {"status": "parsed"}

    monkeypatch.setattr(coordination_runner, "run_strong_first_turn", _strong)
    with monkeypatch.context() as scope:
        scope.setattr(event_log, "_notify_harness", lambda *args: None)
        committed = await event_log.append(wid, source, {"type": "worker_done", "data": {"result": "ok"}})
    coordination_watcher.tick(wid)
    await _until(lambda: (wid, plan_card) not in coordination_watcher._dispatching)
    assert calls == [("direct_node", [f'event:{wid}:{source}:{committed["seq"]}'])]
    coordination_watcher.feed(wid, source, committed)
    coordination_watcher.tick(wid)
    assert not coordination_store.panel(wid, plan_card)["pending"]
    assert card_db_read_changes(wid, plan_card, aid) == []


async def test_busy_named_wake_leaves_independent_review_debt(sandbox, monkeypatch):
    wid, _, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "忙时到期")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    out = await coordination_store.control_op(wid, plan_card, assignment_id=aid, op="wake")
    request = out["appointment"]["pending"][-1]
    reasons = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def _strong(work, card, assignment_id, trigger, **kwargs):
        reasons.append(trigger)
        if trigger == "user_wake":
            entered.set()
            await release.wait()
        return {"status": "parsed"}

    monkeypatch.setattr(coordination_runner, "run_strong_first_turn", _strong)
    key = (wid, plan_card)
    assert coordination_watcher._spawn_strong_turn(
        wid, plan_card, aid, "user_wake", coordination_store.wake_key(request))
    await asyncio.wait_for(entered.wait(), timeout=3)
    now = coordination_watcher.now()
    monkeypatch.setattr(coordination_watcher, "now", lambda: now + 7200)
    try:
        outcome = coordination_watcher.tick(wid)
        assert outcome["review_marked"] == 1 and outcome["review_spawned"] == 0
    finally:
        release.set()
    await _until(lambda: key not in coordination_watcher._dispatching)
    assert [p["kind"] for p in coordination_store.panel(wid, plan_card)["pending"]] == ["review_due"]
    assert coordination_watcher.tick(wid)["review_spawned"] == 1
    await _until(lambda: key not in coordination_watcher._dispatching)
    assert reasons == ["user_wake", "review_due"]
