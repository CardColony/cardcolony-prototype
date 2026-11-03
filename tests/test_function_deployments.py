# -*- coding: utf-8 -*-
"""[职能底座 2026-10-31] function_deployments 差事账本 + 装配接口测试。

钉的事：
T1 差事 CRUD + 幂等（同 venue/batch/mandate 的在派差事不双开）
T2 verdict 惰性核对：票立着→办完；revoke→回在派
T3 「作者不评自己」：目标卡上有 author/deploy 差事的职能不进 members
T4 [两源模型 2026-11-01] write_ledger 退役——原 required_meta 测试随台账删
T5 面块：在派任务/参与记录渲染
T6 [两源模型 2026-11-01] direct 到职能卡拒收（会诊差事已退役）
T7 identity_for / grant_for / render_mandate / venue_charter_for 装配
T8 open_review 建评审差事（members 每人一条，exit=verdict）
"""
from __future__ import annotations

import pytest

from src.api import card_tools, function_room
from src.storage import card_db, card_store, function_store, work_store
from src.storage import review_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()


async def _seeded_work(monkeypatch, title: str = "差事") -> tuple[str, dict, dict]:
    """建 work + 播种 + 返回 (wid, cards, {code: fid})。"""
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


async def _mk_target(wid: str) -> str:
    """一张有草稿的子项目卡（open_review 的前置）。"""
    r = await card_store.create_card(wid, "目标子项目", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid, {"requirements_draft": "D1 用户需求一条\nD2 另一条",
                   "requirements_state": "draft"}, "测试前置：写草稿")
    return cid


# ---------------------------------------------------------------- T1 CRUD/幂等

async def test_deployment_create_idempotent_close(sandbox):
    wdata = await work_store.create_work("差事账")
    wid = wdata["work_id"]

    d1 = await function_store.create_deployment(
        wid, "fn_001", venue_kind="review", venue_ref="card_009",
        mandate="评审席", delegator="card_001", batch_id="b1",
        target_card="card_002", exit_kind="verdict",
        tools=["read_card", "approve"], status="active")
    assert d1["id"] > 0 and d1["status"] == "active"

    # 幂等：同 key 同 mandate 再建 ⇒ 返回同一行
    d2 = await function_store.create_deployment(
        wid, "fn_001", venue_kind="review", venue_ref="card_009",
        mandate="评审席", delegator="card_001", batch_id="b1",
        target_card="card_002", exit_kind="verdict")
    assert d2["id"] == d1["id"]
    # 换个 mandate ⇒ 是新差事
    d3 = await function_store.create_deployment(
        wid, "fn_001", venue_kind="review", venue_ref="card_009",
        mandate="另一份差事", delegator="card_001", batch_id="b1",
        exit_kind="verdict")
    assert d3["id"] != d1["id"]

    act = function_store.active_for_function(wid, "fn_001")
    assert {d["id"] for d in act} == {d1["id"], d3["id"]}
    assert function_store.active_for_venue(wid, "card_009")

    # 显式关账：非终态拒收，终态带 closed_at + deployment_closed
    with pytest.raises(ValueError):
        await function_store.close_deployment(wid, d1["id"], status="active")
    closed = await function_store.close_deployment(
        wid, d1["id"], status="done", result="已投票")
    assert closed["status"] == "done" and closed["closed_at"]

    hist = function_store.deployment_history(wid, "fn_001")
    assert [d["id"] for d in hist] == [d1["id"]]
    assert hist[0]["effective_status"] == "done"

    # 非法枚举一律拒
    with pytest.raises(ValueError):
        await function_store.create_deployment(
            wid, "fn_001", venue_kind="bogus", venue_ref="x")
    with pytest.raises(ValueError):
        await function_store.create_deployment(
            wid, "fn_001", venue_kind="review", venue_ref="x",
            exit_kind="bogus")


# ---------------------------------------------------------------- T2 verdict 惰性

async def test_verdict_lazy_close_and_reopen(sandbox, monkeypatch):
    """票立着 ⇒ 差事惰性判完；revoke 撤票 ⇒ 回在派（判据是读的，不改写）。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "惰性核对")
    cid = await _mk_target(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    assert out.get("ok"), out
    rid = out["data"]["review_card_id"]
    seat = fids["pm"]

    assert function_store.active_for_function(wid, seat, "review")

    # 落座 + 投一票 ⇒ 惰性核对办完
    await review_store.mark_spoken(wid, rid, seat)
    await card_tools._h_approve(wid, seat, {"review_card_id": rid}, cards)
    assert not function_store.active_for_function(wid, seat, "review")
    hist = function_store.deployment_history(wid, seat)
    assert hist and hist[0]["venue_kind"] == "review"
    assert hist[0]["effective_status"] == "done"

    # revoke 把票撤回 ⇒ 差事在惰性核对里回在派（撤票=待定，不是办完）
    await review_store.revoke(wid, rid, seat)
    assert function_store.active_for_function(wid, seat, "review")


# ---------------------------------------------------------------- T3 作者不评自己

async def test_author_excluded_from_review_members(sandbox, monkeypatch):
    """PM 在目标卡上有 author 差事 ⇒ open_review 把它从 members 剔除。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "作者排除")
    cid = await _mk_target(wid)
    pm = fids["pm"]

    # PM 执笔过这张卡的需求稿（差事账直接判，不另立状态）
    await function_store.create_deployment(
        wid, pm, venue_kind="author", venue_ref=cid,
        mandate="执笔需求初稿", delegator="root", exit_kind="deliverable",
        status="done")
    assert function_store.has_prior(wid, pm, cid)

    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    assert out.get("ok"), out
    members = out["data"]["members"]
    assert pm not in members, "作者只能答辩，不能当评委"
    assert fids["ceo"] in members and fids["cto"] in members

    # withdrawn 的执笔差事不算数（撤了＝没真执笔）
    wid2 = wid
    await function_store.create_deployment(
        wid2, fids["cto"], venue_kind="author", venue_ref=cid,
        mandate="被撤的执笔", status="withdrawn")
    out2 = await card_tools._h_open_review(
        wid2, cid, {"card_id": cid, "brief": "再开一批"}, cards)
    assert fids["cto"] in out2["data"]["members"]


# ---------------------------------------------------------------- T5 面块

async def test_face_shows_deploy_blocks(sandbox, monkeypatch):
    """目录职能的脸上出「在派任务」「参与记录」两块。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "差事面")
    pm = fids["pm"]
    await function_store.create_deployment(
        wid, pm, venue_kind="deploy", venue_ref="card_001",
        venue_label="card_001 的派驻", mandate="验收这张卡",
        delegator="card_001", exit_kind="deliverable", status="active")
    await function_store.create_deployment(
        wid, pm, venue_kind="review", venue_ref="card_050",
        venue_label="旧评审批次", mandate="评过一轮", status="done")

    s = function_room.summary(wid, pm)
    assert "## 在派任务" in s["text"] and "card_001 的派驻" in s["text"]
    assert "## 参与记录" in s["text"] and "旧评审批次" in s["text"]


# ---------------------------------------------------------------- T6 职能不收指令

async def test_direct_to_function_denied(sandbox, monkeypatch):
    """[两源模型 2026-11-01 · 用户令] direct 到职能卡一律拒——
    会诊差事已退役，职能不收指令、不私下问答。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "拒指令")
    root_id = work_store.work_card_id_of(wid)
    cto = fids["cto"]

    # 就算有通信线也拒——硬边界不是线，是「职能不是干活的卡」
    def _ln(conn):
        card_db.upsert_link(conn, root_id, cto, "comm")
    await card_db.run_write(wid, _ln)
    cards = card_store.iter_card_metas(wid)

    out = await card_tools._h_scene_direct(
        wid, root_id,
        {"card_id": cto, "instruction": "登录这块用 Cookie 还是 Token？",
         "acceptance": "给出选择与理由"}, cards)
    assert not out.get("ok"), out
    assert out["error"]["code"] == "denied", out
    # 不落差事账
    assert function_store.active_for_function(wid, cto) == []


# ---------------------------------------------------------------- T7 装配接口

async def test_identity_and_grant(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch, "装配")
    pm = fids["pm"]

    ident = function_room.identity_for(wid, pm, "review")
    assert ident["function_id"] == pm and ident["catalog_code"] == "pm"
    # 评审场穿评审职责书（review_charters.json 按目录 code 命中）
    assert "只有需求文档草稿" in ident["charter"]["text"]
    # 未知场型 ⇒ 回日常职责书
    ident2 = function_room.identity_for(wid, pm, "mystery")
    assert "前提" in ident2["charter"]["text"] or ident2["charter"]["text"]

    # 目录 code 也能直接解
    ident3 = function_room.identity_for(wid, "qa", "review")
    assert ident3["function_id"] == fids["qa"]

    # 授面：review 场的默认授面 ∩ 挂载 ⇒ approve/reject（台账已退役）
    tools = function_room.grant_for(wid, pm, "review")
    assert "approve" in tools and "reject" in tools
    assert "write_ledger" not in tools
    # 没配的场型 ⇒ 空授权（场不授就不发）
    assert function_room.grant_for(wid, pm, "nowhere") == []

    # mandate 模板渲染
    m = function_room.render_mandate(
        "review", function=pm, review_card_id="card_777",
        batch="b3", target="card_002")
    assert "card_777" in m and "b3" in m and "card_002" in m
    assert "{batch}" not in m


# ---------------------------------------------------------------- T8 open_review 建账

async def test_open_review_creates_seat_deployments(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch, "席位差事")
    cid = await _mk_target(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    assert out.get("ok"), out
    rid = out["data"]["review_card_id"]
    batch = out["data"]["batch"]

    for f in out["data"]["members"]:
        deps = function_store.active_for_function(wid, f, "review")
        assert len(deps) == 1, f"席位 {f} 应有一条评审差事"
        d = deps[0]
        assert d["venue_ref"] == rid and d["batch_id"] == batch
        assert d["exit_kind"] == "verdict"
        assert d["delegator"] == cid
        # 授面落账：评审场函数侧上限（发言+票，不私记台账）
        assert "approve" in d["tools"] and "write_ledger" not in d["tools"]

    # 非成员（qa/emergence 不在默认名单）⇒ 没有差事
    assert not function_store.active_for_function(wid, fids["qa"], "review")

    # 差事事件落在席位自己的卡键上
    from src.storage import event_log
    recs = event_log.read(wid, fids["pm"]) or []
    kinds = {str(r.get("type") or "") for r in recs}
    assert "deployment_created" in kinds


async def test_open_review_rejects_keeper_seat(sandbox, monkeypatch):
    """[评审席资格 2026-11-01 · 用户令「守门员评审席没有场景」]
    没有评审职责书的职能点名也不进席（emergence_keeper 天然被挡；
    qa 有职责书 ⇒ 点它仍合法）。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "席位资格")
    cid = await _mk_target(wid)

    # 只点守门员 ⇒ 整批拒开
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "x",
                   "members": [fids["emergence_keeper"]]}, cards)
    assert not out.get("ok")
    assert out["error"]["code"] == "validation"
    assert not function_store.active_for_function(
        wid, fids["emergence_keeper"], "review")

    # 守门员混在合法席位里 ⇒ 只剔它自己
    out2 = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "x",
                   "members": [fids["qa"], fids["emergence_keeper"]]}, cards)
    assert out2.get("ok"), out2
    assert out2["data"]["members"] == [fids["qa"]]


# ---------------------------------------------------------------- T9 deliverable/sweep

async def test_deliverable_exit_and_sweep_persist(sandbox, monkeypatch):
    """deliverable 判据（两源模型）：exit_spec=cardwrite[:<块>]/msg——
    职能在**场卡面上**落了产出事实即办完；sweep 把惰性判完的**持久关账**
    （status=done+closed_at+closed 事件）。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "交活判完")
    qa = fids["qa"]
    cid = await _mk_target(wid)

    dep = await function_store.create_deployment(
        wid, qa, venue_kind="deploy", venue_ref=cid,
        mandate="验收这张卡的产物", delegator="harness:staff_qa",
        exit_kind="deliverable", exit_spec="cardwrite",
        status="active")
    assert function_store.active_for_function(wid, qa, "deploy")

    # 别人写场卡——不是 QA 落的字 ⇒ 不算交活
    await card_store.update_card(
        wid, cid, {"status_block": "别人写的现状"}, "外部写入")
    from src.storage import event_log
    from tests import async_settle
    await async_settle.wait_until(
        lambda: any(str(r.get("type") or "") == "card_block_edited"
                    for r in (event_log.read(wid, cid) or [])),
        what="场卡写事件落盘")
    assert function_store.active_for_function(wid, qa, "deploy")

    # QA 行动者回合里写场卡（execute_tool 绑 actor ⇒ 事件带 by=qa）
    with card_store.actor_ctx(qa):
        await card_store.update_card(
            wid, cid, {"status_block": "QA 验收留字：D1 过 D2 不过"},
            "QA 验收留字")
    await async_settle.wait_until(
        lambda: any(str(r.get("type") or "") == "card_block_edited"
                    and str((r.get("data") or {}).get("by") or "") == qa
                    for r in (event_log.read(wid, cid) or [])),
        what="QA 写场卡事件落盘")
    assert not function_store.active_for_function(wid, qa, "deploy")

    # 持久关账：生产里 execute_tool 落完顺手扫场（见 card_tools），
    # 这里直调同一个 sweep。
    await function_store.sweep_deployments(wid)
    got = function_store.get_deployment(wid, dep["id"])
    assert got["status"] == "done" and got["closed_at"], got
    await async_settle.wait_until(
        lambda: "deployment_closed" in {
            str(r.get("type") or "") for r in (event_log.read(wid, qa) or [])},
        what="差事关账事件")
    kinds = {str(r.get("type") or "") for r in (event_log.read(wid, qa) or [])}
    assert "deployment_closed" in kinds

    # 历史按有效状态记
    hist = function_store.deployment_history(wid, qa)
    assert any(d["id"] == dep["id"] for d in hist)


async def test_deliverable_msg_spec(sandbox, monkeypatch):
    """exit_spec=msg：职能的消息落进场卡（验收结论 sync 进本卡会话）即办完。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "msg 交活")
    qa = fids["qa"]
    cid = await _mk_target(wid)

    await function_store.create_deployment(
        wid, qa, venue_kind="deploy", venue_ref=cid,
        mandate="验收这张卡", delegator="harness:staff_qa",
        exit_kind="deliverable", exit_spec="msg", status="active")
    assert function_store.active_for_function(wid, qa, "deploy")

    from src.storage import session_facade, event_log
    from tests import async_settle
    await session_facade.append_card_message(
        wid, cid, from_card=qa, text="验收：D1 过 D2 不过",
        msg_type="msg_sync")
    await async_settle.wait_until(
        lambda: any(str(r.get("type") or "") == "msg_sync"
                    for r in (event_log.read(wid, cid) or [])),
        what="QA 回执事件落盘")
    assert not function_store.active_for_function(wid, qa, "deploy")


async def test_identity_for_resolved_flag(sandbox, monkeypatch):
    """幽灵职能坑：identity_for 对查不到的职能回显输入串——
    判「解没解出来」看 resolved 位。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "幽灵职能")
    ok = function_room.identity_for(wid, fids["pm"], "review")
    assert ok["resolved"] is True
    ghost = function_room.identity_for(wid, "fn_999", "review")
    assert ghost["function_id"] == "fn_999" and ghost["resolved"] is False


# ---------------------------------------------------------------- T10 执笔/差事作用域

async def test_pm_deploy_face_and_deployment_scope(sandbox, monkeypatch):
    """PM 执笔链：目录挂载→deploy 场授面发写件→差事作用域放行 venue 卡。

    · grant_for(pm,"deploy") 含 edit_requirements/edit_card（挂载∩授面）；
    · _cap_gate：PM 对在派差事覆盖的卡放行（差事授面有 edit_card），
      对未覆盖的卡仍 denied（差事即作用域，不越界）。
    """
    from src.permissions import policy

    wid, cards, fids = await _seeded_work(monkeypatch, "执笔差事")
    pm = fids["pm"]
    x = await _mk_target(wid)
    y = await _mk_target(wid)

    # deploy 场授面：PM 挂载（含目录 extra_abilities）∪ 场授面 ⇒ 发写件
    tools = set(function_room.grant_for(wid, pm, "deploy"))
    assert {"edit_requirements", "edit_card"} <= tools, tools
    assert "write_ledger" not in tools
    # QA 没挂写件能力 ⇒ 同样授面也不发（∩ 保护）
    qa_tools = set(function_room.grant_for(wid, fids["qa"], "deploy"))
    assert "edit_requirements" not in qa_tools
    assert "edit_card" not in qa_tools

    # 席位合成面（照 message_runner._synth_seat_meta 形状）
    cards2 = dict(cards)
    cards2[pm] = {"card_id": pm, "card_type": "function"}

    # 没差事：PM 无子树 ⇒ 树闸拒
    bad = card_tools._cap_gate(pm, "edit_other_face", policy.SWITCH_EDIT,
                             x, cards2, "编辑", work_id=wid)
    assert bad is not None and bad["error"]["code"] == "denied"

    # 派执笔差事（实发面照 grant_for 结果落账）⇒ venue 卡放行
    await function_store.create_deployment(
        wid, pm, venue_kind="deploy", venue_ref=x, target_card=x,
        mandate="写需求初稿", delegator="harness:staff_pm",
        exit_kind="deliverable", exit_spec="cardwrite:requirements_draft",
        tools=sorted(tools), status="active")
    ok = card_tools._cap_gate(pm, "edit_other_face", policy.SWITCH_EDIT,
                            x, cards2, "编辑", work_id=wid)
    assert ok is None, ok
    # 没覆盖的卡仍拒（差事作用域不越界）
    still = card_tools._cap_gate(pm, "edit_other_face", policy.SWITCH_EDIT,
                               y, cards2, "编辑", work_id=wid)
    assert still is not None and still["error"]["code"] == "denied"

    # 差事实发面没有的能力不放行：dispose_card（没人发过）⇒ 仍拒
    nope = card_tools._cap_gate(pm, "dispose_card", policy.SWITCH_DELETE,
                              x, cards2, "删除", work_id=wid)
    assert nope is not None and nope["error"]["code"] == "denied"
