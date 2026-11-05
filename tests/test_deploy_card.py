# -*- coding: utf-8 -*-
"""[差事卡 2026-11-01 · 用户令「建卡即召唤」] create_deploy_card + report 测试。

钉的事：
T1  QA 差事卡：qa_card 挂召唤方树下（看得见）、差事账字段齐、授面带 report
T2  资格闸：无派驻职责书的职能/幽灵职能/非法 target 一律拒
T3  sibling 对象：父卡替子卡召 QA；差事读权放行对象卡（QA 无线也能读）
T4  report 回执：落派遣方会话 msg_report ⇒ deliverable 判据读派遣方流关账
T5  PM 执笔卡：pm_card + cardwrite 判据读**对象卡**流（不是场卡流）
"""
from __future__ import annotations

import pytest

from src.api import card_tools, function_room, routes
from src.storage import card_store, function_store, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()


async def _seeded_work(monkeypatch, title: str = "差事卡") -> tuple[str, dict, dict]:
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
    """差事唤起会真跑 _run_message（起模型回合）——打桩只验证派遣本身。"""
    async def _fake(*a, **kw):
        return {"ok": True, "stub": True}
    monkeypatch.setattr(routes, "_run_message", _fake)


async def _mk_card(wid: str, title: str, parent: str = "",
                   card_type: str = "sub_project") -> str:
    r = await card_store.create_card(wid, title, "模型生成",
                                     card_type=card_type, parent=parent,
                                     skip_kick=True)
    return str(r["meta"]["card_id"])


# ---------------------------------------------------------------- T1 QA 差事卡

async def test_create_deploy_card_qa(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch, "QA 差事卡")
    _stub_wake(monkeypatch)
    cid = await _mk_card(wid, "做登录的子卡")
    cards = card_store.iter_card_metas(wid)

    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "qa", "task": "验收当前产物的实现情况"}, cards)
    assert out.get("ok"), out
    assert out["card_type"] == "qa_card"
    assert out["function"] == "qa"
    assert out["parent"] == cid and out["target_card"] == cid
    assert out["started"] is True and out["deployment_id"] > 0

    # qa_card 挂在召唤方树下——看得见，不是离树伴生件
    metas = card_store.iter_card_metas(wid)
    assert out["card_id"] in metas
    assert str(metas[out["card_id"]].get("parent") or "") == cid
    assert str(metas[out["card_id"]].get("card_type") or "") == "qa_card"

    # 差事账：deploy 场、delegator＝召唤卡、target＝对象卡、exit=report
    dep = function_store.get_deployment(wid, out["deployment_id"])
    assert dep["venue_kind"] == "deploy" and dep["venue_ref"] == out["card_id"]
    assert dep["delegator"] == cid and dep["target_card"] == cid
    assert dep["exit_kind"] == "deliverable" and dep["exit_spec"] == "report"
    assert dep["status"] == "active"
    assert "report" in (dep.get("tools") or [])


# ---------------------------------------------------------------- T2 资格闸

async def test_create_deploy_card_gates(sandbox, monkeypatch):
    wid, cards, fids = await _seeded_work(monkeypatch, "资格闸")
    _stub_wake(monkeypatch)
    cid = await _mk_card(wid, "召唤卡")
    other = await _mk_card(wid, "不相干的卡")
    cards = card_store.iter_card_metas(wid)

    # 无派驻职责书 ⇒ 不接差事（守门员、CTO）
    for fn in ("emergence_keeper", "cto"):
        out = await card_tools._h_create_deploy_card(
            wid, cid, {"function": fn, "task": "随便干点什么活干一干"}, cards)
        assert not out.get("ok"), (fn, out)
        assert out["error"]["code"] == "denied", out

    # 幽灵职能 ⇒ not_found
    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "fn_999", "task": "随便干点什么活干一干"}, cards)
    assert out["error"]["code"] == "not_found", out

    # 非法 target：不存在的卡
    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "qa", "task": "验收某张卡的产物",
                   "target": "card_zzz"}, cards)
    assert out["error"]["code"] == "not_found", out

    # 非法 target：非本卡非直接子卡（other 与 cid 无父子关系）
    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "qa", "task": "验收某张卡的产物",
                   "target": other}, cards)
    assert out["error"]["code"] == "denied", out

    # task 空/过短拒
    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "qa", "task": ""}, cards)
    assert out["error"]["code"] == "validation", out


# ---------------------------------------------------------------- T3 sibling 读权

async def test_deploy_scope_grants_target_read(sandbox, monkeypatch):
    """父卡替子卡召 QA：差事卡的 QA 对对象卡（sibling）无线——
    差事作用域放行读（验收的命根子：读对象卡需求文档当判据）。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "sibling 验收")
    _stub_wake(monkeypatch)
    parent = await _mk_card(wid, "父卡")
    worker = await _mk_card(wid, "worker 卡", parent=parent, card_type="worker")
    stray = await _mk_card(wid, "不相干的卡", parent=parent)
    cards = card_store.iter_card_metas(wid)

    out = await card_tools._h_create_deploy_card(
        wid, parent,
        {"function": "qa", "task": "验收这张worker 卡的产物", "target": worker}, cards)
    assert out.get("ok"), out
    dep = function_store.get_deployment(wid, out["deployment_id"])
    assert dep["delegator"] == parent and dep["target_card"] == worker

    qa = fids["qa"]
    # 职能是离树观察者（_line_between 裁定「能读全树」）：对象卡随便读，
    # 差事读权其实不顶闸——验收要的需求文档/产物天然拿得到。
    got = await card_tools._h_read_card(
        wid, qa, {"card_id": worker}, cards)
    assert "error" not in got, got
    got2 = await card_tools._h_read_card(
        wid, qa, {"card_id": stray}, cards)
    assert "error" not in got2, got2


# ---------------------------------------------------------------- T4 report 回执

async def test_report_reaches_delegator_and_closes(sandbox, monkeypatch):
    """report 把结论落**派遣方**会话（msg_report）⇒ deliverable「report」
    判据读派遣方流 ⇒ 差事惰性判完。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "差事回执")
    _stub_wake(monkeypatch)
    cid = await _mk_card(wid, "召唤卡")
    cards = card_store.iter_card_metas(wid)

    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "qa", "task": "验收当前产物的实现"}, cards)
    assert out.get("ok"), out
    qa = fids["qa"]
    assert function_store.active_for_function(wid, qa, "deploy")

    rep = await card_tools._h_report_deploy(
        wid, qa, {"content": "逐条验收：D1 过、D2 不过（证据见卡面）"}, cards)
    assert rep.get("ok"), rep
    assert rep["delivered"][0]["to"] == cid

    # 派遣方事件流里出现该职能的 msg_report ⇒ 判完
    from src.storage import event_log
    from tests import async_settle
    await async_settle.wait_until(
        lambda: any(str(r.get("type") or "") == "msg_report"
                    for r in (event_log.read(wid, cid) or [])),
        what="回执事件落盘")
    assert not function_store.active_for_function(wid, qa, "deploy")

    # 没有差事的职能调 report ⇒ 拒
    pm = fids["pm"]
    nope = await card_tools._h_report_deploy(
        wid, pm, {"content": "没人派我"}, cards)
    assert nope.get("error", {}).get("code") == "denied", nope


# ---------------------------------------------------------------- T5 PM 执笔卡

async def test_create_deploy_card_pm_cardwrite(sandbox, monkeypatch):
    """PM 执笔：pm_card 场 + cardwrite 判据读**对象卡**流——PM 把草稿写
    在对象卡面上（不是场卡面上）即交差。"""
    wid, cards, fids = await _seeded_work(monkeypatch, "PM 执笔卡")
    _stub_wake(monkeypatch)
    cid = await _mk_card(wid, "要需求文档的子卡")
    cards = card_store.iter_card_metas(wid)

    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "pm", "task": "为这张卡写出需求文档"}, cards)
    assert out.get("ok"), out
    assert out["card_type"] == "pm_card" and out["parent"] == cid

    dep = function_store.get_deployment(wid, out["deployment_id"])
    assert dep["venue_kind"] == "deploy" and dep["venue_ref"] == out["card_id"]
    assert dep["target_card"] == cid
    assert dep["exit_spec"] == "cardwrite:requirements_draft"
    pm = fids["pm"]
    assert function_store.active_for_function(wid, pm, "deploy")

    # PM 把草稿写在**对象卡**（不是 pm_card）⇒ 判据读对象卡流 ⇒ 判完
    with card_store.actor_ctx(pm):
        await card_store.update_card(
            wid, cid, {"requirements_draft": "D1 登录秒开\nD2 断网可重试",
                       "requirements_state": "draft"}, "PM 执笔落稿")
    from src.storage import event_log
    from tests import async_settle
    await async_settle.wait_until(
        lambda: any(str(r.get("type") or "") == "card_block_edited"
                    and str((r.get("data") or {}).get("by") or "") == pm
                    for r in (event_log.read(wid, cid) or [])),
        what="PM 写对象卡事件落盘")
    assert not function_store.active_for_function(wid, pm, "deploy")


# ------------------------------------------------- T6 PM 内部路径真写（工具层）

async def test_pm_edit_requirements_passes_via_deployment(
        sandbox, monkeypatch):
    """[差事即作用域] PM 对对象卡调 ``edit_requirements``——组织权闸对
    离树职能恒拒，靠差事作用域回落放行；实发面里要有 edit_card
    （edit_other_face）工具。"""
    from src.api.message_runner import _synth_seat_meta
    wid, cards, fids = await _seeded_work(monkeypatch, "PM 执笔真写")
    _stub_wake(monkeypatch)
    cid = await _mk_card(wid, "缺需求文档的子卡")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "pm", "task": "为这张卡写出需求文档"}, cards)
    assert out.get("ok"), out
    pm = fids["pm"]
    cards = card_store.iter_card_metas(wid)
    cards[pm] = _synth_seat_meta(wid, pm, cards)
    with card_store.actor_ctx(pm):  # 直调 handler 时手工给 by 归因
        res = await card_tools._h_edit_requirements(
            wid, pm, {"card_id": cid, "old_string": "",
                      "new_string": "D1 登录秒开\nD2 断网自动重连"}, cards)
    assert res.get("ok"), res
    card = await card_store.read_card(wid, cid)
    draft = str((card.get("meta") or {}).get("requirements_draft") or "")
    assert "登录秒开" in draft
    # 差事已惰性判完（cardwrite 判据读对象卡流 by=pm）
    assert not function_store.active_for_function(wid, pm, "deploy")


# ------------------------------------------------- T7/T8 wb 执行体收口

class _FakeState:
    def __init__(self, work_id: str, card_id: str):
        self.work_id = work_id
        self.card_id = card_id


class _FakeEx:
    executor_id = "wb_test"
    model = "test_model"
    tool_calls = 3


async def test_wb_finish_qa_reports_and_closes(sandbox, monkeypatch):
    """[执行体收口] QA 差事卡走 wb：执行体正文 ⇒ msg_report 落派遣方
    会话 ⇒ 差事持久关账 ⇒ 差事卡 completed——执行体没有 card_tools，
    桥接层机械交差。"""
    from src.api.wb_exec import bridge
    from src.storage import event_log, session_facade
    wid, cards, fids = await _seeded_work(monkeypatch, "QA 收口")
    _stub_wake(monkeypatch)
    cid = await _mk_card(wid, "验收目标子卡")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "qa",
                   "task": "验收当前产物，逐条对需求文档"}, cards)
    assert out.get("ok"), out
    vcard = out["card_id"]
    meta = (await card_store.read_card(wid, vcard)).get("meta") or {}
    monkeypatch.setattr("src.api.message_runner.deliver_now",
                        lambda *a, **kw: _noop())
    await bridge._finish_deploy_card(
        _FakeState(wid, vcard), _FakeEx(),
        "D1 过（实测 0.8s）\nD2 不过（断网后不重连）\n总结论：差一项", meta)
    # 回执正文落在派遣方会话、from=QA（msg_type 编在 card-msg 标记里）
    msgs = await session_facade.read(wid, cid)
    reports = [m for m in msgs if "type=msg_report" in str(m.get("content") or "")
               and "D1 过" in str(m.get("content") or "")]
    assert reports, f"派遣方会话没有 QA 回执: {msgs[-3:]}"
    # 差事持久关账
    assert not function_store.active_for_function(wid, fids["qa"], "deploy")
    # 差事卡完工
    vmeta = (await card_store.read_card(wid, vcard)).get("meta") or {}
    assert vmeta.get("state") == "completed"


async def test_wb_finish_pm_writes_draft_and_closes(sandbox, monkeypatch):
    """[执行体收口] PM 差事卡走 wb：执行体正文＝需求文档草稿，写到
    对象卡草稿列（by=pm）⇒ cardwrite 判据关账 ⇒ 差事卡 completed。"""
    from src.api.wb_exec import bridge
    wid, cards, fids = await _seeded_work(monkeypatch, "PM 收口")
    _stub_wake(monkeypatch)
    cid = await _mk_card(wid, "执笔目标子卡")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_create_deploy_card(
        wid, cid, {"function": "pm", "task": "为这张卡写出需求文档"}, cards)
    assert out.get("ok"), out
    vcard = out["card_id"]
    meta = (await card_store.read_card(wid, vcard)).get("meta") or {}
    monkeypatch.setattr("src.api.message_runner.deliver_now",
                        lambda *a, **kw: _noop())
    await bridge._finish_deploy_card(
        _FakeState(wid, vcard), _FakeEx(),
        "D1 冷启动 <3s\nD2 断网自动重连\nD3 不做社交分享", meta)
    card = await card_store.read_card(wid, cid)
    draft = str((card.get("meta") or {}).get("requirements_draft") or "")
    assert "冷启动" in draft and "社交分享" in draft
    assert not function_store.active_for_function(wid, fids["pm"], "deploy")
    vmeta = (await card_store.read_card(wid, vcard)).get("meta") or {}
    assert vmeta.get("state") == "completed"


async def _noop():
    return None
