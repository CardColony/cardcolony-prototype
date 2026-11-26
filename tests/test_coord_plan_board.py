# -*- coding: utf-8 -*-
"""[统筹计划板 2026-11-23 · 施工任务书] 计划板条目层 + coord_plan_board
工具 + turn_tail 板投影 + 版本/权限闸的隔离验收（PB1–PB6 可测面）。

覆盖口径：
- 条目 schema：priority 缺省投影 100/200/300、(priority,item_id) 稳定序、
  todo/doing/blocked/done、depends 依赖核验、status=done 必填 result_note；
- 兼容：无 priority 的旧 items 读侧投影、首次有效编辑随新版本落库；
- 原子/并发：整批校验、双客户端同 rev 只有一个保存成功、同批重放不重复；
- 工具：coord_plan_board read/apply 进强模型跳内协议、apply 成功后同轮
  rev 换新快照、独立回看轮只读；
- 每跳出站只带一份当轮板（turn_tail）：未完成按序、done 缺席、改板后
  新快照；用户对话同样携带；
- 权限：paused 用户可维护、revoked/finished 拒、模型轮在活跃态才起。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.api import coordination_runner, model_config, model_service, routes, function_room
from src.storage import (card_store, coordination_board, coordination_store,
                         function_store, session_facade, work_store)

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()
    monkeypatch.setattr(model_config, "load",
                        lambda: {"models": {"strong-test-route": {},
                                            "weak-test-route": {}}})


async def _seeded_work(monkeypatch, title: str = "计划板任务"):
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
                                     card_type="sub_project", skip_kick=True)
    return str(r["meta"]["card_id"])


def _future_ts(hours: float = 1.0) -> str:
    return (datetime.now(timezone(timedelta(hours=8)))
            + timedelta(hours=hours)).isoformat()


def _appoint_kwargs(**over):
    kw = dict(
        models={"strong": "strong-test-route", "weak": "weak-test-route"},
        next_review_at=_future_ts(),
        observe={"scope": "work"},
        instruct={"scope": "cards", "ids": ["card_x"]},
        mode="observe", objective="按需求推进底座重构",
    )
    kw.update(over)
    return kw


async def _appointed(monkeypatch, wid: str, fids: dict, plan_card: str,
                     **over) -> str:
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs(**over))
    assert out.get("ok"), out
    return out["appointment"]["assignment_id"]


def _seq_call_main(responses: list[str], hook=None):
    """按跳返回不同输出，同时记录每跳 system/user/prior/turn_tail。"""
    calls = []

    async def _fake(system_prompt, user_prompt, **kw):
        calls.append({"system": system_prompt, "user": user_prompt,
                      "tail": kw.get("turn_tail"),
                      "model_name": kw.get("model_name")})
        if hook is not None:
            await hook(len(calls))
        i = min(len(calls) - 1, len(responses) - 1)
        return ({"content": responses[i],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
                "strong-test-route")
    return _fake, calls


def _board_tails(call: dict) -> list[str]:
    """该跳 turn_tail 里计划板快照的数量（协议：每次出站恰一份）。"""
    tail = call.get("tail") or []
    return [m["content"] for m in tail
            if isinstance(m, dict) and "统筹计划板" in str(m.get("content") or "")]


def _tail_snapshot(call: dict) -> dict:
    tails = _board_tails(call)
    assert len(tails) == 1, f"每跳应恰一份板快照，实际 {len(tails)}"
    return json.loads(tails[0].split("\n", 1)[1])


PLAN_OK = json.dumps({"plan": {
    "objective": "重构接入底座",
    "requirements": [{"ref": "card_x", "version": "v", "quote": "q"}],
    "items": [{"item_id": "base", "title": "底座重构", "owner_card": "card_x",
               "depends": [], "next_result": "完成接入", "status": "todo"}],
    "wake_standards": [{"standard_id": "s1", "protect": "完成", "ignore": "闲聊",
                        "escalate": "失败"}],
}}, ensure_ascii=False)

# 不带 items 的整份提交——已登板条目以计划板为准（提交不回改板）
PLAN_NO_ITEMS = json.dumps({"plan": {
    "objective": "重构接入底座",
    "requirements": [{"ref": "card_x", "version": "v", "quote": "q"}],
    "wake_standards": [{"standard_id": "s1", "protect": "完成", "ignore": "闲聊",
                        "escalate": "失败"}],
}}, ensure_ascii=False)


# ---------------------------------------------------------------- 条目层


async def test_board_apply_add_update_projection(sandbox, monkeypatch):
    """PB1/PB2：add 分配宿主 ID＋优先级插项/提前；投影按 (priority,item_id)。"""
    wid, _metas, fids = await _seeded_work(monkeypatch)
    card_a, card_b = await _mk_card(wid, "域A"), await _mk_card(wid, "域B")
    await card_store.update_card(wid, card_b, {"requirements": "核查旧调用方兼容"},
                                 "准备需求正稿")
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card,
                           instruct={"scope": "cards", "ids": [card_a, card_b]})
    appt = coordination_store.panel(wid, plan_card)
    rev = int(appt["rev"])

    # 三条登记（PB1）：标题/下一结果/优先级/责任卡/需求引用
    rec = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=rev,
        changes=[
            {"op": "add", "title": "核查旧调用方兼容", "priority": 200,
             "next_result": "兼容核查记录", "owner_card": card_b,
             "requirement_refs": [card_b]},
            {"op": "add", "title": "接入协议确认", "priority": 50,
             "next_result": "协议版本确认", "owner_card": card_a},
            {"op": "add", "title": "底座重构", "priority": 300,
             "next_result": "重构完成"},
        ], reason="首次登记", by="model")
    assert rec["ok"] and rec["rev"] == rev + 1
    ids = [rec["allocated"][f"add:{i}"] for i in range(3)]
    assert len(set(ids)) == 3  # 宿主分配的三个不同 ID

    # PB2：在两件事之间插项（150）、另一项提前（40）
    rec2 = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=rec["rev"],
        changes=[
            {"op": "add", "title": "中间插入项", "priority": 150,
             "next_result": "x"},
            {"op": "update", "item_id": ids[1], "priority": 40},
        ], reason="调整顺序", by="user")
    assert rec2["ok"]

    p = coordination_store.panel(wid, plan_card)
    order = [(it["priority"], it["item_id"]) for it in p["board"]["unfinished"]]
    assert order == sorted(order)
    priorities = [it["priority"] for it in p["board"]["unfinished"]]
    assert priorities == [40, 150, 200, 300]
    # owner 保留、无 owner 的项标待核（字段为空）
    owners = {it["item_id"]: it["owner_card"] for it in p["board"]["items"]}
    assert owners[ids[1]] == card_a and owners[ids[2]] == ""


async def test_legacy_items_projected_and_materialized(sandbox, monkeypatch):
    """兼容：旧 items 无 priority ⇒ 读侧投影 100/200/300；首次有效编辑
    随新版本实体化落库（读取本身不改库）。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    legacy = {"objective": "旧计划", "items": [
        {"item_id": "legacy-a", "title": "旧事项A", "status": "doing"},
        {"item_id": "legacy-b", "title": "旧事项B", "status": "done",
         "result_note": "早前完成"},
    ], "wake_standards": [{"standard_id": "s1", "protect": "x",
                           "ignore": "y", "escalate": "z"}]}
    appt = coordination_store.panel(wid, plan_card)
    saved = await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid,
        expected_rev=int(appt["rev"]), plan=legacy, by="user")
    rev = int(saved["appointment"]["rev"])

    # 读侧投影：100/200 按旧顺序；done 项保留原 ID/状态/说明
    p = coordination_store.panel(wid, plan_card)
    proj = {it["item_id"]: it for it in p["board"]["items"]}
    assert proj["legacy-a"]["priority"] == 100
    assert proj["legacy-b"]["priority"] == 200
    assert proj["legacy-b"]["status"] == "done"
    assert proj["legacy-b"]["result_note"] == "早前完成"
    # 未完成区只有 doing 的 A
    assert [it["item_id"] for it in p["board"]["unfinished"]] == ["legacy-a"]

    # 首次有效编辑随新版本实体化（保存后库内 priority 落库）
    rec = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=rev,
        changes=[{"op": "update", "item_id": "legacy-a", "priority": 50}],
        reason="提前旧事项", by="user")
    assert rec["rev"] == rev + 1
    p2 = coordination_store.panel(wid, plan_card)
    plan_items = {it["item_id"]: it for it in p2["plan"]["items"]}
    assert plan_items["legacy-a"]["priority"] == 50
    assert plan_items["legacy-b"]["priority"] == 200  # 投影值实体化


async def test_apply_atomic_invalid_batch(sandbox, monkeypatch):
    """PB5：任一条不合法 ⇒ 整批零写入（rev 不动、条目不变）。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    rev = int(coordination_store.panel(wid, plan_card)["rev"])
    before = coordination_store.panel(wid, plan_card)["plan"].get("items")

    bad_batches = [
        [{"op": "add", "title": "OK"},                       # 后面一条非法
         {"op": "update", "item_id": "不存在", "priority": 10}],
        [{"op": "add", "title": "X", "priority": 0}],        # 优先级非正
        [{"op": "add"}],                                     # 缺 title
        [{"op": "add", "title": "X", "priority": "abc"}],    # 类型错
        [{"op": "frobnicate", "item_id": "x"}],              # 非法 op
        [{"op": "add", "title": "X", "depends": ["ghost"]}], # 悬空依赖
    ]
    for i, batch in enumerate(bad_batches):
        try:
            await coordination_store.board_apply(
                wid, plan_card, assignment_id=aid, expected_rev=rev,
                changes=batch, reason="bad", by="user")
            raise AssertionError(f"批次 {i} 不应被接受")
        except coordination_store.CoordinationError as exc:
            assert exc.code == "validation", (i, exc.code)
    after = coordination_store.panel(wid, plan_card)
    assert int(after["rev"]) == rev
    assert after["plan"].get("items") == before


async def test_apply_conflict_and_replay(sandbox, monkeypatch):
    """PB5：双客户端同 rev ⇒ 一个成功一个 409；同批重放不重复新增。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    rev = int(coordination_store.panel(wid, plan_card)["rev"])
    batch = [{"op": "add", "title": "客户端A的事项", "priority": 100}]

    rec_a = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=rev,
        changes=batch, reason="客户端A", by="user")
    assert rec_a["ok"]
    # 客户端B：同 rev 的并发写 ⇒ conflict 带当前版本与条目
    try:
        await coordination_store.board_apply(
            wid, plan_card, assignment_id=aid, expected_rev=rev,
            changes=[{"op": "add", "title": "客户端B的事项"}],
            reason="客户端B", by="user")
        raise AssertionError("同 rev 并发写不应成功")
    except coordination_store.CoordinationError as exc:
        assert exc.code == "conflict"
        assert exc.extra["current_rev"] == rec_a["rev"]
        assert len(exc.extra["items"]) == 1
    # 旧 rev 同批重放 ⇒ 仍只有首次保存的一份
    try:
        await coordination_store.board_apply(
            wid, plan_card, assignment_id=aid, expected_rev=rev,
            changes=batch, reason="重放", by="user")
        raise AssertionError("旧版本重放不应成功")
    except coordination_store.CoordinationError as exc:
        assert exc.code == "conflict"
    titles = [it["title"] for it in
              coordination_store.panel(wid, plan_card)["board"]["items"]]
    assert titles.count("客户端A的事项") == 1


async def test_done_requires_note_and_reopen(sandbox, monkeypatch):
    """PB3：done 必填完成依据；done 移出待推进；重开同 ID 回来。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    rec = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid,
        expected_rev=int(coordination_store.panel(wid, plan_card)["rev"]),
        changes=[{"op": "add", "title": "事项一", "priority": 100,
                  "next_result": "R1"}],
        reason="登记", by="model")
    iid = rec["allocated"]["add:0"]

    # 缺 result_note 的 done ⇒ 拒
    try:
        await coordination_store.board_apply(
            wid, plan_card, assignment_id=aid, expected_rev=rec["rev"],
            changes=[{"op": "update", "item_id": iid, "status": "done"}],
            reason="完成", by="model")
        raise AssertionError("无依据 done 不应通过")
    except coordination_store.CoordinationError as exc:
        assert exc.code == "validation"

    from src.storage import event_log

    event = event_log.append_sync(wid, plan_card, {
        "type": "card_block_edited", "data": {"item_id": iid, "block": "status"},
    })
    # 带依据的 done ⇒ 过；刷新仍 done；未完成区移除；按 ID 可读
    rec2 = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=rec["rev"],
        changes=[{"op": "update", "item_id": iid, "status": "done",
                  "result_note": "取得执行回执 R1",
                  "evidence_refs": [f"event:{wid}:{plan_card}:{event['seq']}"]}],
        reason="完成", by="model")
    assert rec2["ok"]
    p = coordination_store.panel(wid, plan_card)
    assert [it["item_id"] for it in p["board"]["unfinished"]] == []
    done = {it["item_id"]: it for it in p["board"]["done"]}
    assert done[iid]["result_note"] == "取得执行回执 R1"

    appt = coordination_store.panel(wid, plan_card)
    out = coordination_board.read_board(appt, item_id=iid)
    assert out["item"]["status"] == "done"

    # 历史里有完成记录（detail.kind=board_apply）
    hist = coordination_store.panel(wid, plan_card, history=True)["history"]
    apply_recs = [h for h in hist if h.get("detail", {}).get("kind") == "board_apply"]
    assert apply_recs and apply_recs[0]["detail"]["reason"] == "完成"

    # 重开：同 ID 回未完成区，原完成记录留在历史
    rec3 = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=rec2["rev"],
        changes=[{"op": "update", "item_id": iid, "status": "blocked",
                  "result_note": "重开：发现新问题"}],
        reason="重开", by="user")
    assert rec3["ok"]
    p2 = coordination_store.panel(wid, plan_card)
    assert [it["item_id"] for it in p2["board"]["unfinished"]] == [iid]


async def test_paused_user_can_edit_revoked_denied(sandbox, monkeypatch):
    """PB5：暂停期用户仍可维护（任务书口径）；撤回后一律拒。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    appt = coordination_store.panel(wid, plan_card)
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause",
        expected_rev=int(appt["rev"]))
    paused = coordination_store.panel(wid, plan_card)
    rec = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid, expected_rev=int(paused["rev"]),
        changes=[{"op": "add", "title": "暂停期登记", "priority": 100}],
        reason="暂停中维护", by="user")
    assert rec["ok"]

    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="revoke",
        expected_rev=int(rec["rev"]))
    try:
        await coordination_store.board_apply(
            wid, plan_card, assignment_id=aid, expected_rev=rec["rev"],
            changes=[{"op": "add", "title": "撤回后写"}], by="user")
        raise AssertionError("撤回后不应可写")
    except coordination_store.CoordinationError as exc:
        assert exc.code in ("denied", "conflict")


# ---------------------------------------------------------------- 强模型工具＋板投影


async def test_strong_turn_board_apply_and_tail(sandbox, monkeypatch):
    """PB1/PB4：模型走 coord_plan_board apply 登板；每跳出站恰一份板；
    apply 成功后下一跳拿到**新 rev** 快照；未完成按序、done 缺席。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    card_x = await _mk_card(wid, "域责任卡")
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card,
                           instruct={"scope": "cards", "ids": [card_x]})
    # 预置一条旧事项 + 一条已完成项（投影不应含 done）
    appt = coordination_store.panel(wid, plan_card)
    await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=int(appt["rev"]),
        plan={"objective": "o", "items": [
            {"item_id": "old-1", "title": "旧事项", "status": "todo",
             "priority": 300, "next_result": "旧R"},
            {"item_id": "old-done", "title": "已完成事项", "status": "done",
             "priority": 10, "result_note": "早已完成"}],
            "wake_standards": [{"standard_id": "s", "protect": "a",
                                "ignore": "b", "escalate": "c"}]}, by="user")
    rev0 = int(coordination_store.panel(wid, plan_card)["rev"])

    hop_apply = json.dumps({"action": "coord_plan_board", "args": {
        "op": "apply", "reason": "首批登记",
        "changes": [
            {"op": "add", "title": "新事项A", "priority": 50,
             "next_result": "RA", "owner_card": card_x},
            {"op": "update", "item_id": "old-1", "priority": 100},
        ]}}, ensure_ascii=False)
    fake, calls = _seq_call_main([hop_apply, PLAN_NO_ITEMS])
    monkeypatch.setattr(model_service, "call_main", fake)
    result = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid, trigger="user_wake")
    assert result["status"] == "parsed", result

    assert len(calls) == 2
    snap1 = _tail_snapshot(calls[0])
    # 首跳：未完成 old-1 在场、done 缺席、rev=起轮时
    assert snap1["rev"] == rev0
    ids1 = [it["item_id"] for it in snap1["items"]]
    assert ids1 == ["old-1"]
    # 次跳：apply 成功 ⇒ 新快照 rev=rev0+1，新条目 50 在前、old-1 改 100
    snap2 = _tail_snapshot(calls[1])
    assert snap2["rev"] == rev0 + 1
    ids2 = [it["item_id"] for it in snap2["items"]]
    assert len(ids2) == 2 and "old-1" in ids2 and "old-done" not in ids2
    prios = [it["priority"] for it in snap2["items"]]
    assert prios == sorted(prios) == [50, 100]
    # 落库验证：两条未完成＋历史留改板账
    p = coordination_store.panel(wid, plan_card)
    assert len(p["board"]["unfinished"]) == 2
    hist = coordination_store.panel(wid, plan_card, history=True)["history"]
    kinds = [h.get("detail", {}).get("kind") for h in hist]
    assert "board_apply" in kinds


async def test_turn_tail_each_trigger(sandbox, monkeypatch):
    """PB4：任命/手动/到期回看/用户对话每次出站都恰一份当轮板。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)

    for trigger in ("appoint", "user_wake", "weak_wake", "direct_node"):
        fake, calls = _seq_call_main([PLAN_OK])
        monkeypatch.setattr(model_service, "call_main", fake)
        r = await coordination_runner.run_strong_first_turn(
            wid, plan_card, assignment_id=aid, trigger=trigger)
        assert r["status"] == "parsed", (trigger, r)
        assert len(calls) >= 1
        for c in calls:
            tails = _board_tails(c)
            assert len(tails) == 1, f"{trigger} 每跳应恰一份板"
        # 每轮提交推进 rev ⇒ 各触发都在场
    # 用户对话
    fake2, calls2 = _seq_call_main(["收到，按板上优先级推进。"])
    monkeypatch.setattr(model_service, "call_main", fake2)
    appt = coordination_store.panel(wid, plan_card)
    await coordination_runner.run_user_chat(
        wid, plan_card, assignment_id=aid,
        expected_rev=int(appt["rev"]), content="现在进度如何？")
    assert len(_board_tails(calls2[0])) == 1


async def test_review_turn_board_readonly(sandbox, monkeypatch):
    """独立回看轮：read 可用，apply 拒绝且零写入。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    appt = coordination_store.panel(wid, plan_card)
    rev0 = int(appt["rev"])

    read_then_apply = json.dumps({"action": "coord_plan_board",
                                  "args": {"op": "read"}})
    apply_try = json.dumps({"action": "coord_plan_board", "args": {
        "op": "apply", "changes": [{"op": "add", "title": "回看改板"}]}})

    async def _respond(system_prompt, user_prompt, **kw):
        calls.append({"tail": kw.get("turn_tail")})
        n = len(calls)
        content = read_then_apply if n == 1 else apply_try if n == 2 else "bad"
        return ({"content": content,
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                "strong-test-route")
    calls = []
    monkeypatch.setattr(model_service, "call_main", _respond)
    r = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid, trigger="review_due")
    # 第二跳 apply 被拒 ⇒ unparsed；零写入
    assert r["status"] == "unparsed"
    p = coordination_store.panel(wid, plan_card)
    assert int(p["rev"]) == rev0
    assert p["board"]["items"] == []


async def test_weak_pack_item_status(sandbox, monkeypatch):
    """§三：弱模型材料附本批涉及事项的状态与下一结果。"""
    wid, _m, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card)
    rec = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid,
        expected_rev=int(coordination_store.panel(wid, plan_card)["rev"]),
        changes=[{"op": "add", "title": "被观察事项", "priority": 100,
                  "next_result": "取得回执", "status": "doing"}],
        reason="登记", by="model")
    iid = rec["allocated"]["add:0"]
    appt = coordination_runner._current_assignment(wid, plan_card)
    batch = {"batch_id": "b1", "changes": [{
        "coord": f"event:{wid}:card_x:1", "card_id": "card_x",
        "kind": "worker_done", "item_id": iid, "summary": "完成", "evidence": "e"}]}
    pack = coordination_runner.weak_input_pack(wid, plan_card, batch, appt)
    assert "本批涉及事项现状" in pack["user"]
    assert iid in pack["user"] and "doing" in pack["user"]
    assert "取得回执" in pack["user"]


async def test_instruction_binds_board_item(sandbox, monkeypatch):
    """PB6：经过目标接纳、域内派工和正式回报，原事项才能凭回执完成。"""
    from src.api import card_tools, message_runner
    from test_coordination_runner import _link_actor

    wid, _m, fids = await _seeded_work(monkeypatch)
    domain = await card_store.create_card(
        wid, "域责任卡", "模型生成", card_type="sub_project",
        parent=work_store.work_card_id_of(wid), skip_kick=True)
    card_x = domain["meta"]["card_id"]
    plan_card = await _mk_card(wid, "计划卡")
    aid = await _appointed(monkeypatch, wid, fids, plan_card,
                           mode="live",
                           instruct={"scope": "cards", "ids": [card_x]})
    rec = await coordination_store.board_apply(
        wid, plan_card, assignment_id=aid,
        expected_rev=int(coordination_store.panel(wid, plan_card)["rev"]),
        changes=[{"op": "add", "title": "派工事项", "priority": 100,
                  "owner_card": card_x, "next_result": "交付物"}],
        reason="登记", by="model")
    iid = rec["allocated"]["add:0"]
    await _link_actor(wid, plan_card, card_x)
    worker_report = asyncio.Event()
    domain_report = asyncio.Event()
    worker_ids = []
    real_delivery = message_runner.deliver_now

    async def deliver(work, target, **kwargs):
        if (kwargs.get("source") or {}).get("coordination_msg_id"):
            return await real_delivery(work, target, **kwargs)
        return False

    async def model_actions(work, card, *args, **kwargs):
        if card == card_x:
            original = kwargs["source"]["coordination_msg_id"]
            created = await card_tools._h_create_worker_card(
                work, card, {"title": "域派执行", "task": "核对并交付结果"},
                card_store.iter_card_metas(work))
            assert created["ok"] and created["started"], created
            worker_ids.append(created["card_id"])
            await asyncio.wait_for(worker_report.wait(), 5)
            out = await card_tools._h_report_up(
                work, card, {"content": "已核对子卡回报并交付", "task": original,
                             "_coordination_msg_id": original}, card_store.iter_card_metas(work))
            assert any(r["msg_id"] for r in out.get("pending", [])), out
            domain_report.set()
        else:
            out = await card_tools._h_report_up(
                work, card, {"content": "执行结果已交域责任卡"}, card_store.iter_card_metas(work))
            assert any(r["msg_id"] for r in out.get("pending", [])), out
            worker_report.set()
        return {"content": "fixture"}

    errors = []

    async def run_fixture(*args, **kwargs):
        try:
            return await model_actions(*args, **kwargs)
        except Exception as exc:
            errors.append(exc)
            domain_report.set()
            raise

    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    monkeypatch.setattr(message_runner, "deliver_now", deliver)
    monkeypatch.setattr(routes, "_run_message_inner", run_fixture)
    instruction = json.dumps({"action": "coord_submit_instruction", "args": {
        "target_card": card_x, "item_id": iid, "instruction": "按板上事项交付",
        "acceptance": "取得回执", "evidence_refs": []}})
    fake, _ = _seq_call_main([instruction])
    monkeypatch.setattr(model_service, "call_main", fake)
    result = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid, trigger="user_wake")
    assert result["status"] == "parsed" and result["instruction"]["msg_id"], result
    await asyncio.wait_for(domain_report.wait(), 7)
    assert not errors, errors
    assert len(worker_ids) == 1
    assert card_store.iter_card_metas(wid)[worker_ids[0]]["parent"] == card_x
    [action] = coordination_runner.board_summary(wid, plan_card)["actions"]
    [evidence] = coordination_runner.read_execution_evidence(wid, plan_card, action["action_key"])
    assert action["item_id"] == iid and evidence["reply_to"] == result["instruction"]["msg_id"]
    receipt = f"receipt:{evidence['reply_msg_id']}"
    read = json.dumps({"action": "coord_plan_board", "args": {"op": "read", "item_id": iid}})
    apply = json.dumps({"action": "coord_plan_board", "args": {"op": "apply", "changes": [
        {"op": "update", "item_id": iid, "status": "done", "result_note": "域责任卡已回报",
         "evidence_refs": [receipt]}]}})
    fake, closing_calls = _seq_call_main([read, apply, PLAN_NO_ITEMS])
    monkeypatch.setattr(model_service, "call_main", fake)
    completed = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid, trigger="user_wake")
    assert completed["status"] == "parsed", completed
    assert receipt in closing_calls[1]["user"]
    fake, calls = _seq_call_main([PLAN_NO_ITEMS])
    monkeypatch.setattr(model_service, "call_main", fake)
    followup = await coordination_runner.run_strong_first_turn(
        wid, plan_card, assignment_id=aid, trigger="user_wake")
    assert followup["status"] == "parsed"
    assert iid not in {it["item_id"] for it in _tail_snapshot(calls[0])["items"]}
    assert _tail_snapshot(calls[0])["rev"] == completed["rev"]


async def test_board_retains_origin_and_rejects_unverified_refs(sandbox, monkeypatch):
    from src.storage import event_log

    wid, _, fids = await _seeded_work(monkeypatch)
    plan = await _mk_card(wid, "统筹计划")
    retained = await _mk_card(wid, "保留观察")
    revoked = await _mk_card(wid, "撤出观察")
    aid = await _appointed(monkeypatch, wid, fids, plan)
    await coordination_store.save_plan(
        wid, plan, assignment_id=aid, expected_rev=0,
        plan={"objective": "核查", "items": [{"item_id": "private", "title": "撤权原文"}]},
        by="strong:fixture", source_cards={retained, revoked})
    await coordination_store.control_op(
        wid, plan, assignment_id=aid, expected_rev=1, op="set_grant",
        observe={"scope": "cards", "ids": [retained]})
    added = await coordination_store.board_apply(
        wid, plan, assignment_id=aid, expected_rev=2,
        changes=[{"op": "update", "item_id": "private", "priority": 50},
                 {"op": "add", "title": "仍可看事项", "owner_card": retained}])
    item = added["allocated"]["add:1"]
    appt = coordination_runner._current_assignment(wid, plan)
    assert [it["item_id"] for it in coordination_board.board_snapshot(appt, {retained})["items"]] == [item]
    assert "item" not in coordination_board.read_board(appt, {retained}, "private")
    weak = coordination_runner.weak_input_pack(wid, plan, {"changes": [{"item_id": "private"}]}, appt)
    assert "撤权原文" not in weak["user"] and "事项现状" not in weak["user"]

    unrelated = event_log.append_sync(wid, retained, {"type": "worker_done", "data": {
        "item_id": "different"}})
    for fields in ({"evidence_refs": [f"event:{wid}:{retained}:999999"]},
                   {"evidence_refs": [f"event:{wid}:{retained}:{unrelated['seq']}"]},
                   {"requirement_refs": ["read:999999"]}):
        with pytest.raises(coordination_store.CoordinationError, match="不可核验"):
            await coordination_store.board_apply(
                wid, plan, assignment_id=aid, expected_rev=added["rev"],
                changes=[{"op": "update", "item_id": item, "status": "done",
                          "result_note": "尝试使用错误引用", **fields}])
    assert coordination_store.panel(wid, plan)["rev"] == added["rev"]

    result, _ = _seq_call_main([json.dumps({"plan": {"objective": "更新目标",
        "items": [{"item_id": "attempt-to-replace", "title": "旧提交"}]}})])
    monkeypatch.setattr(model_service, "call_main", result)
    assert (await coordination_runner.run_strong_first_turn(
        wid, plan, assignment_id=aid, trigger="user_wake"))["status"] == "parsed"
    assert {row["item_id"] for row in coordination_store.panel(wid, plan)["board"]["items"]} == {
        item, "private"}


async def test_project_board_updates_visible_item_with_archived_sources(sandbox, monkeypatch):
    from src.storage import event_log

    wid, _, _ = await _seeded_work(monkeypatch)
    owner = await _mk_card(wid, "负责验收")
    worker = await _mk_card(wid, "复跑实现")
    agent = coordination_store.PROJECT_AGENT_ID
    appointment = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appointment["assignment_id"]
    live_sources = {owner, worker}
    await coordination_store.save_plan(wid, agent, assignment_id=aid, expected_rev=0,
        plan={"items": [{"item_id": "implementation", "title": "核对施工", "owner_card": owner, "status": "doing"}]},
        by="strong:fixture", source_cards=live_sources | {"card_archived"})

    appt = coordination_runner._current_assignment(wid, agent)
    item = coordination_board.read_board(appt, live_sources, "implementation")["item"]
    assert item["status"] == "doing"
    event = event_log.append_sync(wid, worker, {"type": "tool_result", "ts": "2026-11-23T15:04:51+00:00",
                                               "data": {"content": "复跑已经开始"}})
    event_log.append_sync(wid, worker, {"type": "reasoning", "ts": "2026-11-23T15:04:43+00:00",
                                         "data": {"text": "稍后才落盘的较早事件"}})
    reference = f"event:{wid}:{worker}:{event['seq']}"
    saved = await coordination_store.board_apply(wid, agent, assignment_id=aid, expected_rev=1,
        changes=[{"op": "update", "item_id": "implementation", "next_result": "等待复跑回执",
                  "evidence_refs": [reference]}], by="strong:fixture", source_cards=live_sources)
    assert saved["rev"] == 2
    item, = coordination_store.panel(wid, agent)["board"]["items"]
    assert item["evidence_refs"] == [reference] and item["next_result"] == "等待复跑回执"

    with pytest.raises(coordination_store.CoordinationError, match="证据引用不可核验"):
        await coordination_store.board_apply(wid, agent, assignment_id=aid, expected_rev=2,
            changes=[{"op": "update", "item_id": "implementation", "status": "done",
                      "result_note": "过程不能作为完成证据", "evidence_refs": [reference]}],
            source_cards=live_sources)
    assert coordination_store.panel(wid, agent)["rev"] == 2


async def test_plan_board_wire_material_and_paging(sandbox, monkeypatch):
    from outbound_probe import OutboundRecorder, frame_finish, frame_text, sse
    from src.api import model_config
    from src.infra import coordination_watcher

    compiled = model_config.load()
    wid, _, fids = await _seeded_work(monkeypatch)
    monkeypatch.setattr(model_config, "load", lambda: compiled)
    monkeypatch.setenv("OPENDESIGN_API_KEY", "fixture-key")
    route = "ds-v41-flash-eco-amr"
    plan = await _mk_card(wid, "适配器出站计划")
    await card_store.update_card(wid, plan, {"requirements": "针对接入的需求正稿"}, "准备需求")
    aid = await _appointed(monkeypatch, wid, fids, plan, models={"strong": route, "weak": route})
    added = await coordination_store.board_apply(
        wid, plan, assignment_id=aid, expected_rev=0,
        changes=[{"op": "add", "title": f"工作事项 {i}"} for i in range(121)])
    done_id = added["allocated"]["add:0"]
    completed = await coordination_store.board_apply(
        wid, plan, assignment_id=aid, expected_rev=added["rev"],
        changes=[{"op": "update", "item_id": done_id, "status": "done",
                  "result_note": "已核对需求"}])
    marker = "统筹计划板（当前快照"
    replies = iter([
        json.dumps({"action": "coord_read_card", "card_id": plan}),
        json.dumps({"action": "coord_plan_board", "args": {"op": "apply",
            "changes": [{"op": "add", "title": "插项", "priority": 10,
                         "requirement_refs": ["read:1"]}]}}),
        PLAN_NO_ITEMS,
        PLAN_NO_ITEMS, PLAN_NO_ITEMS, PLAN_NO_ITEMS,
        json.dumps({"action": "coord_read_card", "card_id": plan}),
        json.dumps({"review": {"evidence_refs": ["read:1"], "conclusion": "已核查",
                               "next_review_at": _future_ts(3)}}),
        "统筹回复",
    ])
    wire = OutboundRecorder(lambda *_: sse(frame_text(next(replies)), frame_finish("stop"))).install(monkeypatch)
    for trigger in ("appoint", "user_wake", "weak_wake", "direct_node", "review_due"):
        if trigger == "review_due":
            now = coordination_watcher.now()
            with monkeypatch.context() as clock:
                clock.setattr(coordination_watcher, "now", lambda: now + 7200)
                clock.setattr(coordination_watcher, "_spawn_strong_turn", lambda *args: False)
                assert coordination_watcher.tick(wid)["review_marked"] == 1
        result = await coordination_runner.run_strong_first_turn(wid, plan, assignment_id=aid,
                                                                 trigger=trigger)
        assert result["status"] == "parsed", (trigger, result)
    current = coordination_store.panel(wid, plan)
    await coordination_runner.run_user_chat(wid, plan, assignment_id=aid,
                                           expected_rev=current["rev"], content="进度如何？")
    snapshots = []
    for i in range(len(wire.requests)):
        turns = [msg for msg in wire.texts(i) if msg.startswith(marker)]
        assert len(turns) == 1, wire.texts(i)
        snapshots.append(json.loads(turns[0].split("\n", 1)[1]))
        assert all(it["item_id"] != done_id for it in snapshots[-1]["items"])
    assert len(snapshots) == 9
    assert snapshots[0]["rev"] == completed["rev"]
    assert snapshots[2]["rev"] == completed["rev"] + 1
    assert snapshots[2]["items"][0]["priority"] == 10
    current = coordination_store.panel(wid, plan)
    stored = next(it for it in current["board"]["items"] if it["title"] == "插项")
    [req_ref] = stored["requirement_refs"]
    assert current["board"]["requirements"][req_ref]["text"] == "针对接入的需求正稿"
    snapshot = snapshots[-1]
    received = [it["item_id"] for it in snapshot["items"]]
    assert snapshot["unfinished_count"] == len(received) == 121
    assert received == [it["item_id"] for it in current["board"]["unfinished"]]
    page = coordination_board.read_board(current, offset=100, limit=5)
    assert [it["item_id"] for it in page["items"]] == received[100:105]
    assert page["next_offset"] == 105
