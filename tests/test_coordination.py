# -*- coding: utf-8 -*-
"""[项目统筹 2026-11-21 · 任务一] 任命/状态机/计划版本/并发唯一性测试。

钉的事（对齐任务书任务一验收）：
T1  任命已有 PM 成功；原 PM 执笔（deploy 差事）与评审仍可用
T2  重复任命 / 两位候选同时任命 ⇒ 不能产生两个在任负责人（写事务唯一）
T3  两个客户端用同一版本编辑，仅首个写入成功（409＋最新版本）
T4  状态机：pause/resume/revoke/finish；暂停不接唤醒；撤回后重新任命是新标识
T5  缺项校验：缺模型路由 / 缺将来回看时间 / 职能不存在 / 根卡作载体 / 幽灵计划卡
T6  越权：assignment_id 不匹配 denied（403 语义）
T7  set_mode/set_grant 递增 rev；wake 进 pending；列表去重
T8  HTTP 面：空面板不写库、400 缺项 / 409 冲突 / 403 越权、面板 history
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.api import card_tools, function_room, model_config, routes
from src.storage import card_store, coordination_store, function_store, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()
    # 模型路由校验随夹具：任务书口径「路由值由夹具创建」——
    # 校验器只认夹具造的两个路由名（model_config.load 读真 config，
    # 沙箱里没有这个隔离时 strong-test-route 会撞真配置 400）。
    monkeypatch.setattr(model_config, "load",
                        lambda: {"models": {"strong-test-route": {},
                                            "weak-test-route": {}}})


async def _seeded_work(monkeypatch, title: str = "统筹任务一"):
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


def _stub_wake(monkeypatch) -> None:
    async def _fake(*a, **kw):
        return {"ok": True, "stub": True}
    monkeypatch.setattr(routes, "_run_message", _fake)


async def _mk_card(wid: str, title: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成",
                                     card_type="sub_project",
                                     skip_kick=True)
    return str(r["meta"]["card_id"])


def _future_ts(hours: float = 1.0) -> str:
    return (datetime.now(timezone(timedelta(hours=8)))
            + timedelta(hours=hours)).isoformat()


def _appoint_kwargs(**over):
    kw = dict(
        models={"strong": "strong-test-route",
                "weak": "weak-test-route"},
        next_review_at=_future_ts(),
        observe={"scope": "work"},
        instruct={"scope": "cards", "ids": ["card_x"]},
        mode="observe", objective="按现行需求推进底座重构",
    )
    kw.update(over)
    return kw


# ---------------------------------------------------------------- T1 任命＋不干扰

async def test_appoint_pm_and_pm_still_usable(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    _stub_wake(monkeypatch)
    plan_card = await _mk_card(wid, "底座重构计划")
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    assert out.get("ok"), out
    appt = out["appointment"]
    assert appt["state"] == "active" and appt["mode"] == "observe"
    assert appt["assignment_id"].startswith("asg_")
    assert appt["rev"] == 0 and appt["op_seq"] == 0
    assert appt["plan"]["objective"].startswith("按现行需求")
    assert appt["observe_scope"] == {"scope": "work"}
    assert appt["instruct_scope"]["scope"] == "cards"
    assert appt["next_review_at"], appt

    # 任命后原 PM 执笔仍可用（coordination 不碰差事账/目录）
    cid = await _mk_card(wid, "登录功能卡")
    dep = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "pm", "task": "为登录功能写需求文档"},
        card_store.iter_card_metas(wid))
    assert dep.get("ok"), dep

    # 面板回读
    panel = coordination_store.panel(wid, plan_card)
    assert panel["assignment_id"] == appt["assignment_id"]
    assert panel["function_id"] == fids["pm"]
    assert panel["grants"]["observe"] == {"scope": "work"}
    assert panel["overdue"] is False


# ---------------------------------------------------------------- T2 唯一性（计划卡级）

async def test_only_one_active_assignment(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 唯一性＝**计划卡级**：同一份计划（一张卡）
    只能有一位在任统筹；同一 work 的不同计划卡各自任命都成功。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_a = await _mk_card(wid, "计划卡甲")
    plan_b = await _mk_card(wid, "计划卡乙")
    out_a = await coordination_store.appoint(
        wid, plan_a, function_id=fids["pm"], **_appoint_kwargs())
    assert out_a.get("ok")

    # 同卡重复任命 ⇒ conflict
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.appoint(
            wid, plan_a, function_id=fids["pm"], **_appoint_kwargs())
    assert ei.value.code == "conflict"
    assert ei.value.extra.get("plan_card_id") == plan_a

    # 不同计划卡任命另一职能 ⇒ **成功**（work 允许多份计划）
    out_b = await coordination_store.appoint(
        wid, plan_b, function_id=fids["cto"], **_appoint_kwargs())
    assert out_b.get("ok"), out_b
    assert out_b["appointment"]["assignment_id"] != \
        out_a["appointment"]["assignment_id"]

    # 库里两条在任（两张计划卡各一条）
    rows = coordination_store.list_plans(wid)
    active = [r for r in rows if r["state"] in ("active", "paused")]
    assert sorted(r["plan_card_id"] for r in active) == sorted([plan_a, plan_b])

    # 撤回后可以重新任命（新 assignment_id；旧 revoked 保留）
    revoke = await coordination_store.control_op(
        wid, plan_a, assignment_id=out_a["appointment"]["assignment_id"],
        op="revoke")
    assert revoke.get("ok") and revoke["appointment"]["state"] == "revoked"
    out_c = await coordination_store.appoint(
        wid, plan_a, function_id=fids["cto"], **_appoint_kwargs())
    assert out_c.get("ok")
    assert out_c["appointment"]["assignment_id"] != \
        out_a["appointment"]["assignment_id"]


async def test_concurrent_appoint_same_card(sandbox, monkeypatch):
    """同一张计划卡**并发**任命：BEGIN IMMEDIATE 串行化写 ⇒ 恰好一个成功，
    另一个拿 conflict——不能产生两个在任负责人。"""
    import asyncio
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "并发任命计划")

    results = await asyncio.gather(
        coordination_store.appoint(
            wid, plan_card, function_id=fids["pm"], **_appoint_kwargs()),
        coordination_store.appoint(
            wid, plan_card, function_id=fids["cto"], **_appoint_kwargs()),
        return_exceptions=True)

    ok_count, conflict_count = 0, 0
    aids = set()
    for r in results:
        if isinstance(r, coordination_store.CoordinationError):
            assert r.code == "conflict", r
            conflict_count += 1
        elif isinstance(r, dict) and r.get("ok"):
            ok_count += 1
            aids.add(r["appointment"]["assignment_id"])
        else:
            raise AssertionError(f"意外结果: {r!r}")
    assert ok_count == 1 and conflict_count == 1
    assert len(aids) == 1

    # 库里恰好一条在任
    rows = coordination_store.list_plans(wid)
    active = [r for r in rows if r["state"] in ("active", "paused")]
    assert len(active) == 1


# ---------------------------------------------------------------- T3 版本冲突

async def test_plan_rev_conflict(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "版本冲突计划")
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid = out["appointment"]["assignment_id"]

    plan_v1 = {"objective": "o1", "items": [{"item_id": "i1", "title": "a"}]}
    ok1 = await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=0, plan=plan_v1)
    assert ok1.get("ok") and ok1["appointment"]["rev"] == 1

    # 客户端乙还拿着 rev=0 的旧全文覆盖 ⇒ CONFLICT＋返回最新版本
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.save_plan(
            wid, plan_card, assignment_id=aid, expected_rev=0,
            plan={"objective": "旧全文覆盖", "items": []})
    assert ei.value.code == "conflict"
    assert ei.value.extra["current_rev"] == 1
    assert ei.value.extra["plan"]["objective"] == "o1"

    # 拿到最新版本后重写成功
    ok2 = await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=1,
        plan={"objective": "o2", "items": [{"item_id": "i1", "title": "b"}]})
    assert ok2.get("ok") and ok2["appointment"]["rev"] == 2

    # 历史版本可回读
    hist = coordination_store.panel(wid, plan_card, history=True)
    assert [h["rev"] for h in hist["history"]] == [2, 1, 0]


# ---------------------------------------------------------------- T4 状态机

async def test_state_machine(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "状态机计划")
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid = out["appointment"]["assignment_id"]

    # 暂停：停模型与新指令（wake 被拒）；用户改计划不受暂停限制（保留计划）
    paused = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause")
    assert paused["appointment"]["state"] == "paused"
    with pytest.raises(coordination_store.CoordinationError):
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid, op="wake")
    # 暂停中用户改计划不受限制（pause 已推进 rev 0→1，按 rev=1 提交 ⇒ rev=2）
    paused_edit = await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=1,
        plan={"objective": "暂停中改目标"})
    assert paused_edit.get("ok") and paused_edit["appointment"]["rev"] == 2

    # 恢复 ⇒ active（resume 推进 rev 2→3），wake 进 pending
    resumed = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="resume")
    assert resumed["appointment"]["state"] == "active"
    assert resumed["appointment"]["rev"] == 3
    wake = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="wake", note="用户点名")
    assert wake.get("ok")
    panel = coordination_store.panel(wid, plan_card)
    assert any(p["kind"] == "user_wake" for p in panel["pending"])

    # 活跃态可再暂停；暂停态重复 pause ⇒ 拒
    p2 = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause")
    assert p2["appointment"]["state"] == "paused"
    with pytest.raises(coordination_store.CoordinationError):
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid, op="pause")

    # 再恢复，finish ⇒ 终态；终态不能再改授权/计划
    fin = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="finish")
    assert fin["appointment"]["state"] == "finished"
    with pytest.raises(coordination_store.CoordinationError):
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid, op="set_mode", mode="live")


# ---------------------------------------------------------------- T5 缺项校验

async def test_appoint_validation_gates(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "缺项校验计划")

    async def _expect_reject(kwargs, *msgs):
        with pytest.raises(coordination_store.CoordinationError) as ei:
            await coordination_store.appoint(wid, plan_card, **kwargs)
        for m in msgs:
            assert m in ei.value.message, ei.value.message

    # 缺弱模型路由
    await _expect_reject(_appoint_kwargs(function_id=fids["pm"],
                                   models={"strong": "s"}), "缺少模型配置")
    # 缺回看时间 / 过去时间 / 无时区
    await _expect_reject(_appoint_kwargs(function_id=fids["pm"],
                                   next_review_at=""), "不能为空")
    await _expect_reject(_appoint_kwargs(function_id=fids["pm"],
                                   next_review_at="2020-01-01T00:00:00+08:00"),
                   "将来")
    await _expect_reject(_appoint_kwargs(function_id=fids["pm"],
                                   next_review_at="2026-12-01T10:00:00"),
                   "时区")
    # 职能不存在 / 总裁（根卡兼任）被拒
    await _expect_reject(_appoint_kwargs(function_id="fn_999"), "职能不存在")
    root_id = work_store.work_card_id_of(wid)
    await _expect_reject(_appoint_kwargs(function_id=root_id), "非根职能卡")
    # 根卡/职能卡作计划载体被拒；幽灵卡 404
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.appoint(
            wid, root_id, function_id=fids["pm"], **_appoint_kwargs())
    assert "根卡" in ei.value.message
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.appoint(
            wid, fids["pm"], function_id=fids["pm"], **_appoint_kwargs())
    assert "职能卡" in ei.value.message
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.appoint(
            wid, "card_zzz", function_id=fids["pm"], **_appoint_kwargs())
    assert ei.value.code == "not_found"
    # 范围口径非法 / cards 空 ids
    await _expect_reject(_appoint_kwargs(function_id=fids["pm"],
                                   observe={"scope": "galaxy"}), "observe")
    await _expect_reject(_appoint_kwargs(function_id=fids["pm"],
                                   instruct={"scope": "cards", "ids": []}),
                   "ids")


# ---------------------------------------------------------------- T6 越权

async def test_assignment_denied(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "越权计划")
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid = out["appointment"]["assignment_id"]

    # 旧/伪造的 assignment_id ⇒ denied（403 语义）
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.control_op(
            wid, plan_card, assignment_id="asg_forged", op="pause")
    assert ei.value.code == "denied"
    assert ei.value.extra.get("current_assignment") == aid

    # 改过计划后 assignment 恒定（assignment 不随 rev 变）
    await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=0,
        plan={"objective": "v2"})
    panel = coordination_store.panel(wid, plan_card)
    assert panel["assignment_id"] == aid and panel["rev"] == 1

    # 无任命的卡 panel ⇒ 空面板
    empty_card = await _mk_card(wid, "没任命过的卡")
    p = coordination_store.panel(wid, empty_card)
    assert p["state"] == "none" and p["assignment_id"] == ""


# ---------------------------------------------------------------- T7 控制变更与列表

async def test_control_rev_check(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 变更类控制操作校验读入版本：不匹配 ⇒ conflict
    带最新版本；**每次状态变更都推进 rev**（pause 后旧页面拿同版本恢复必得
    409）；expected_rev=None（直调兼容）不校验；wake 是入账不校验。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "控制版本校验计划")
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid = out["appointment"]["assignment_id"]

    # 读入版本过期（提交 5，当前 0）⇒ conflict 带 current_rev
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid, op="pause", expected_rev=5)
    assert ei.value.code == "conflict"
    assert ei.value.extra["current_rev"] == 0
    # 被拒的控制不产生状态变化
    assert coordination_store.panel(wid, plan_card)["state"] == "active"

    # 读入版本正确 ⇒ 成功，且 pause 推进 rev 0→1
    ok = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause", expected_rev=0)
    assert ok.get("ok") and ok["appointment"]["state"] == "paused"
    assert ok["appointment"]["rev"] == 1

    # [2026-11-21 · 用户令实测场景] 旧页面仍拿 rev.0 恢复 ⇒ 必得 conflict
    with pytest.raises(coordination_store.CoordinationError) as ei:
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid, op="resume", expected_rev=0)
    assert ei.value.code == "conflict"
    assert ei.value.extra["current_rev"] == 1

    # 拿最新版本恢复 ⇒ 成功，rev 1→2
    ok2 = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="resume", expected_rev=1)
    assert ok2.get("ok") and ok2["appointment"]["state"] == "active"
    assert ok2["appointment"]["rev"] == 2

    # set_mode 推进 rev 2→3；旧版本再暂停 ⇒ conflict；新版本 ⇒ 成功
    m = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="set_mode", mode="live",
        expected_rev=2)
    assert m["appointment"]["rev"] == 3
    with pytest.raises(coordination_store.CoordinationError):
        await coordination_store.control_op(
            wid, plan_card, assignment_id=aid, op="pause", expected_rev=2)
    p = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause", expected_rev=3)
    assert p.get("ok") and p["appointment"]["rev"] == 4

    # 恢复后再 wake：wake 是入账不是变更，不带版本也放行
    r2 = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="resume", expected_rev=4)
    assert r2.get("ok") and r2["appointment"]["state"] == "active"
    w = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="wake", note="点名")
    assert w.get("ok")


async def test_history_rev_readback(sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 历史版本正文回读：panel(rev=N) 返回该版本完整
    计划正文；不存在的版本 not_found。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "历史回读计划")
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid = out["appointment"]["assignment_id"]

    v1 = {"objective": "第一版目标", "items": [{"item_id": "i1", "title": "a"}]}
    ok1 = await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=0, plan=v1)
    assert ok1.get("ok")
    v2 = {"objective": "第二版目标", "items": []}
    ok2 = await coordination_store.save_plan(
        wid, plan_card, assignment_id=aid, expected_rev=1, plan=v2)
    assert ok2.get("ok")

    # 回读旧版正文（不是当前版）
    old = coordination_store.panel(wid, plan_card, rev=1)
    assert old["rev"] == 1 and old["plan"]["objective"] == "第一版目标"
    assert old["saved_by"] == "user"
    # 当前版
    cur = coordination_store.panel(wid, plan_card, rev=2)
    assert cur["plan"]["objective"] == "第二版目标"
    # appointment 版 0 也可回读
    v0 = coordination_store.panel(wid, plan_card, rev=0)
    assert v0["plan"]["objective"].startswith("按现行需求")

    # 不存在的版本 ⇒ not_found
    with pytest.raises(coordination_store.CoordinationError) as ei:
        coordination_store.panel(wid, plan_card, rev=99)
    assert ei.value.code == "not_found"


async def test_cross_assignment_history(client, sandbox, monkeypatch):
    """[2026-11-21 · 用户令] 撤回后重新任命出现两个 rev.0：按修订记录主键
    history_id 回读，前后两次任命的正文都能打开；?rev=0 取最新任命。"""
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "跨任命历史计划")

    # 第一次任命：A（rev=0）
    a = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid_a = a["appointment"]["assignment_id"]
    h1 = coordination_store.panel(wid, plan_card, history=True)["history"][0]
    assert h1["rev"] == 0 and h1["assignment_id"] == aid_a
    hid_a = h1["history_id"]

    # 撤回 A（appointments.rev 推进到 1；revoke 不产生修订行），重新任命 B（rev=0 重号）
    await coordination_store.control_op(
        wid, plan_card, assignment_id=aid_a, op="revoke")
    b = await coordination_store.appoint(
        wid, plan_card, function_id=fids["cto"], **_appoint_kwargs(
            objective="第二任期目标"))
    aid_b = b["appointment"]["assignment_id"]
    assert aid_b != aid_a

    # 历史摘要：两条任命版（各 rev=0），history_id 不同、assignment 不同
    hist = coordination_store.panel(wid, plan_card, history=True)["history"]
    appoint_rows = [h for h in hist if h["rev"] == 0]
    assert len(appoint_rows) == 2
    assert len({h["history_id"] for h in appoint_rows}) == 2
    assert {h["assignment_id"] for h in appoint_rows} == {aid_a, aid_b}
    hid_b = [h for h in appoint_rows
             if h["assignment_id"] == aid_b][0]["history_id"]
    assert hid_b != hid_a

    # 按主键回读：两次任命各自的正文都能打开
    pa = coordination_store.panel(wid, plan_card, history_id=hid_a)
    assert pa["plan"]["objective"].startswith("按现行需求")
    assert pa["assignment_id"] == aid_a
    pb = coordination_store.panel(wid, plan_card, history_id=hid_b)
    assert pb["plan"]["objective"] == "第二任期目标"
    assert pb["assignment_id"] == aid_b

    # ?rev=0（重号）取最新一次任命的版本
    p0 = coordination_store.panel(wid, plan_card, rev=0)
    assert p0["assignment_id"] == aid_b
    assert p0["plan"]["objective"] == "第二任期目标"

    # 不存在的 history_id ⇒ not_found
    with pytest.raises(coordination_store.CoordinationError) as ei:
        coordination_store.panel(wid, plan_card, history_id=999999)
    assert ei.value.code == "not_found"

    # HTTP 层：?history_id 各自 200，正文对得上
    for hid, aid, obj in ((hid_a, aid_a, "按现行需求推进底座重构"),
                          (hid_b, aid_b, "第二任期目标")):
        r = await client.get(
            f"/api/works/{wid}/cards/{plan_card}/coordination?history_id={hid}")
        assert r.status_code == 200
        assert r.json()["assignment_id"] == aid
        assert r.json()["plan"]["objective"] == obj


async def test_set_mode_grant_and_list(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    plan_card = await _mk_card(wid, "控制变更计划")
    out = await coordination_store.appoint(
        wid, plan_card, function_id=fids["pm"], **_appoint_kwargs())
    aid = out["appointment"]["assignment_id"]

    # set_mode observe→live：rev+1、op_seq+1
    m = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="set_mode", mode="live")
    assert m["appointment"]["mode"] == "live"
    assert m["appointment"]["rev"] == 1 and m["appointment"]["op_seq"] == 1

    # set_grant：改 instruct 范围 ⇒ rev+1
    g = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="set_grant",
        instruct={"scope": "work_development_cards"})
    assert g["appointment"]["instruct_scope"] == {"scope": "work_development_cards"}
    assert g["appointment"]["rev"] == 2

    # pause 也推进 rev（op_seq 同步+1）：rev 2→3、op_seq 2→3
    p = await coordination_store.control_op(
        wid, plan_card, assignment_id=aid, op="pause")
    assert p["appointment"]["rev"] == 3 and p["appointment"]["op_seq"] == 3

    # 列表：同一计划卡只出最新任命
    rows = coordination_store.list_plans(wid)
    assert [r["plan_card_id"] for r in rows] == [plan_card]
    assert rows[0]["mode"] == "live" and rows[0]["state"] == "paused"


# ---------------------------------------------------------------- T8 HTTP 面

async def test_http_panel_and_ops(client, sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch)
    _stub_wake(monkeypatch)
    plan_card = await _mk_card(wid, "HTTP 面计划")

    # 无任命 ⇒ 空面板（200，state=none，不写库）
    r = await client.get(f"/api/works/{wid}/cards/{plan_card}/coordination")
    assert r.status_code == 200
    assert r.json()["state"] == "none"

    # 列表（空）
    r = await client.get(f"/api/works/{wid}/coordination")
    assert r.status_code == 200 and r.json()["plans"] == []

    # appoint（缺模型 ⇒ 400 带缺项说明）
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "appoint", "function_id": fids["pm"],
                                "objective": "o"})
    assert r.status_code == 400
    assert "模型" in r.json()["error"]

    # appoint 完整 ⇒ 200
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "appoint", "function_id": fids["pm"],
                                "objective": "按现行需求推进",
                                "observe": {"scope": "work"},
                                "instruct": {"scope": "cards",
                                             "ids": ["card_demo_protocol"]},
                                "models": {"strong": "strong-test-route",
                                           "weak": "weak-test-route"},
                                "mode": "observe",
                                "next_review_at": _future_ts()})
    assert r.status_code == 200, r.text
    aid = r.json()["assignment_id"]
    assert aid.startswith("asg_") and r.json()["state"] == "active"

    # 重复任命（完整参数）⇒ 409（work 级唯一；参数不完整才会先撞 400）
    r2 = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                           json={"op": "appoint", "function_id": fids["pm"],
                                 "objective": "o2",
                                 "observe": {"scope": "work"},
                                 "instruct": {"scope": "cards",
                                              "ids": ["card_demo_protocol"]},
                                 "models": {"strong": "strong-test-route",
                                            "weak": "weak-test-route"},
                                 "next_review_at": _future_ts()})
    assert r2.status_code == 409

    # PATCH：expected_rev 过期 ⇒ 409＋最新
    r = await client.patch(f"/api/works/{wid}/cards/{plan_card}/coordination",
                           json={"assignment_id": aid, "expected_rev": 5,
                                 "plan": {"objective": "late"}})
    assert r.status_code == 409
    assert r.json()["current_rev"] == 0

    # PATCH 正确版本 ⇒ 200 rev=1
    r = await client.patch(f"/api/works/{wid}/cards/{plan_card}/coordination",
                           json={"assignment_id": aid, "expected_rev": 0,
                                 "plan": {"objective": "v1",
                                          "wake_standards": [
                                              {"standard_id": "s1",
                                               "protect": "外部 agent 无卡也能接入"}]}})
    assert r.status_code == 200 and r.json()["rev"] == 1

    # [2026-11-21 · 用户令] 控制请求须带读入版本：缺 ⇒ 400
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "pause", "assignment_id": aid})
    assert r.status_code == 400
    assert "expected_rev" in r.json()["error"]

    # 控制请求读入版本过期 ⇒ 409 带最新
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "pause", "assignment_id": aid,
                                "expected_rev": 99})
    assert r.status_code == 409
    assert r.json()["current_rev"] == 1

    # 控制请求读入版本正确 ⇒ 200；pause 推进 rev 1→2
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "pause", "assignment_id": aid,
                                "expected_rev": 1})
    assert r.status_code == 200 and r.json()["state"] == "paused"

    # [2026-11-21 · 用户令实测场景] 旧页面拿 rev.1 恢复 ⇒ 409（rev 已推进到 2）
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "resume", "assignment_id": aid,
                                "expected_rev": 1})
    assert r.status_code == 409
    assert r.json()["current_rev"] == 2

    # 拿最新版本恢复 ⇒ 200（rev 2→3）
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "resume", "assignment_id": aid,
                                "expected_rev": 2})
    assert r.status_code == 200 and r.json()["state"] == "active"

    # wake 是入账：不带 expected_rev 也放行
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "wake", "assignment_id": aid,
                                "note": "HTTP 点名"})
    assert r.status_code == 200

    # 越权 op（假 assignment）⇒ 403
    r = await client.post(f"/api/works/{wid}/cards/{plan_card}/coordination",
                          json={"op": "pause", "assignment_id": "asg_fake",
                                "expected_rev": 1})
    assert r.status_code == 403

    # 面板带 history：版本史只列**计划正文修订**（appoint 版0 + PATCH 版1）；
    # pause/resume 推进的 appointments.rev（=3）不插修订行
    r = await client.get(
        f"/api/works/{wid}/cards/{plan_card}/coordination?history=1")
    body = r.json()
    assert body["rev"] == 3 and len(body["history"]) == 2
    assert [h["rev"] for h in body["history"]] == [1, 0]
    assert body["grants"]["observe"] == {"scope": "work"}
    assert any(x["kind"] == "user_wake" for x in body["pending"])
    assert body["next_review_at"] and body["overdue"] is False
    assert all("history_id" in h and "assignment_id" in h
               for h in body["history"])

    # [2026-11-21 · 用户令] 历史版本正文回读：?rev=0 返回任命版正文
    r = await client.get(
        f"/api/works/{wid}/cards/{plan_card}/coordination?rev=0")
    assert r.status_code == 200
    assert r.json()["rev"] == 0
    assert r.json()["plan"]["objective"].startswith("按现行需求")
    # 不存在的版本 ⇒ 404
    r = await client.get(
        f"/api/works/{wid}/cards/{plan_card}/coordination?rev=99")
    assert r.status_code == 404
