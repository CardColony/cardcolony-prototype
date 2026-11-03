# -*- coding: utf-8 -*-
"""``then.deploy`` 部署差事（第四手段）单测。

守五件事：
  ① 命中落账：record_fired 把场（venue）当 target、职能/场型/退出规格落 spec，
     fire_key 缀职能——同触发同一场派不同职能是两笔账；
  ② 部署执行：deploy_runner.execute 落差事账（幂等）→ 激活 → act_as 唤起，
     回执事件带 action_id（重启查重锚）；
  ③ 两段式失败：账建起来但场叫不醒 ⇒ failed，resume 只补唤起不重建账；
  ④ 职能解不出 ⇒ failed 且零副作用（没落账没叫人）；
  ⑤ e2e：msg_report(kind=done) → 策略命中 → 部署真跑。
"""
from __future__ import annotations

import json
import time

import pytest

from src.infra import intervention_runner as ir
from src.infra import intervention_store as ist
from src.infra import fact_store as fs


@pytest.fixture()
def qa_env(sandbox, tmp_path, monkeypatch):
    """沙箱上补：qa 职能档案（jurisdiction.catalog_code）＋开闸目录（含授面）。"""
    from src.api import function_room as _fr
    cat = {
        "seed_on_create": False,
        "_meta": {
            "venue_grants": {"deploy": ["read_card", "ledger"]},
            "mandate_templates": {},
        },
        "functions": [{"code": "qa", "name": "测试验收总监",
                       "face_blocks": [], "toolface": {"allow": []},
                       "ledgers": []}],
    }
    p = tmp_path / "fc_deploy.json"
    p.write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(_fr.ENV_CATALOG_PATH, str(p))
    _fr.reset_cache()

    ist.reset(ist.InterventionStore(works_dir=sandbox.works))
    fs.reset(fs.FactStore(works_dir=sandbox.works))
    yield sandbox
    ist.reset()
    fs.reset(fs.FactStore(works_dir=sandbox.works))


async def _seed_qa(work_id: str = "w1") -> None:
    from src.storage import function_store as _fs
    await _fs.ensure_profile(work_id, "fn_qa", {
        "function_name": "测试验收总监",
        "jurisdiction": {"catalog_code": "qa"},
        "charter": {"text": "验收职责书"},
    })


def _dep_fired(**kw) -> dict:
    f = {
        "policy": "staff_qa_on_stage_done",
        "scope": ["c1"],
        "then": {"deploy": {
            "function": "qa",
            "venue": "event:card",
            "ctx": "event:card",
            "target_card": "event:card",
            "venue_kind": "deploy",
            "wake": True,
            "mandate": "验收 {event.card}，回报给 {event.to}。"}},
        "ctx": {"work_id": "w1", "card_id": "c1", "parent": "",
                "scope": ["c1"],
                "trigger_data": {"card": "c1", "from": "c1", "to": "p1",
                                 "kind": "done"}},
        "basis": {"card": "c1", "seq": 9, "type": "msg_report"},
    }
    f.update(kw)
    return f


# ---------------------------------------------------------- 命中落账
async def test_record_fired_deploy_action_row(qa_env):
    await _seed_qa()
    f = _dep_fired()
    ir.record_fired("w1", [f])
    st = ist.store()
    assert f["_deploy"] is True
    assert f["_targets"] == ["c1"]          # 场＝报完工的子卡
    aid = f["_actions"]["c1"]
    row = st.get_action("w1", aid)
    assert row["mode"] == "deploy"
    assert row["target"] == "c1"
    assert "c1" in row["message"] and "p1" in row["message"]  # mandate 已渲染
    spec = json.loads(row["spec"])
    assert spec["function_id"] == "fn_qa"   # code 已解析成实例
    assert spec["venue_kind"] == "deploy"
    assert spec["ctx"] == "c1" and spec["target_card"] == "c1"


async def test_record_fired_deploy_fn_unresolved_still_recorded(qa_env):
    """职能解不出也要落账（义务不丢），投递侧记 failed。"""
    f = _dep_fired()
    ir.record_fired("w1", [f])
    st = ist.store()
    aid = f["_actions"]["c1"]
    spec = json.loads(st.get_action("w1", aid)["spec"])
    assert spec["function"] == "qa" and spec["function_id"] == ""


# ---------------------------------------------------------- 部署执行
async def test_execute_creates_deployment_and_wakes(qa_env, monkeypatch):
    await _seed_qa()
    from src.api import deploy_runner, routes, event_stream

    calls = {}
    emits = []

    async def _fake(wid, card_id, content, **kw):
        calls["card_id"] = card_id
        calls["kw"] = kw
        return {"ok": True}

    def _emit(wid, card_id, ev_type, data=None, **kw):
        emits.append((card_id, ev_type, dict(data or {})))

    monkeypatch.setattr(routes, "_run_message", _fake)
    monkeypatch.setattr(event_stream, "emit", _emit)
    res = await deploy_runner.execute(
        "w1", spec={"function": "qa", "venue_kind": "deploy",
                    "ctx": "c1", "target_card": "c1", "wake": True},
        venue="c1", mandate="去验收。", action_id="act-1",
        policy_id="staff_qa_on_stage_done")
    assert res["ok"] and res["reason"] == "woken"
    assert res["deployment_id"] > 0
    assert calls["card_id"] == "c1"
    kw = calls["kw"]
    assert kw["act_as"] == "fn_qa"
    assert kw["ctx_card"] == "c1"
    assert isinstance(kw["tool_allow"], set)
    assert kw["append_user"] is False
    assert kw["source"]["kind"] == "deploy"
    assert kw["source"]["action_id"] == "act-1"

    # 差事账：active + 在派清单能查到
    from src.storage import function_store as _fs
    dep = _fs.get_deployment("w1", res["deployment_id"])
    assert dep["status"] == "active"
    assert dep["venue_ref"] == "c1" and dep["mandate"] == "去验收。"
    act = _fs.active_for_function("w1", "fn_qa")
    assert [d["id"] for d in act] == [res["deployment_id"]]

    # 回执事件：harness_message 带 action_id＋deployment_id（重启查重锚）
    hm = [d for _c, t, d in emits if t == "harness_message"]
    assert hm and hm[-1]["action_id"] == "act-1"
    assert hm[-1]["deployment_id"] == res["deployment_id"]
    assert hm[-1]["mode"] == "deploy"
    # 差事账自己的生命周期事件也发了
    assert any(t == "deployment_created" for _c, t, _d in emits)


async def test_execute_idempotent_same_spec(qa_env, monkeypatch):
    """同一 spec 重派：差事账幂等——返回既有行，不双开。"""
    await _seed_qa()
    from src.api import deploy_runner, routes

    async def _fake(wid, card_id, content, **kw):
        return {"ok": True}

    monkeypatch.setattr(routes, "_run_message", _fake)
    spec = {"function": "qa", "venue_kind": "deploy", "ctx": "c1",
            "target_card": "c1", "wake": True}
    r1 = await deploy_runner.execute("w1", spec=spec, venue="c1",
                                     mandate="去验收。", policy_id="p1")
    r2 = await deploy_runner.execute("w1", spec=spec, venue="c1",
                                     mandate="去验收。", policy_id="p1")
    assert r1["ok"] and r2["ok"]
    assert r1["deployment_id"] == r2["deployment_id"]


async def test_execute_unresolved_function_no_side_effects(qa_env):
    from src.api import deploy_runner
    res = await deploy_runner.execute(
        "w1", spec={"function": "ghost", "venue_kind": "deploy"},
        venue="c1", mandate="x", policy_id="p1")
    assert res["ok"] is False
    assert res["reason"] == "function_resolve_failed"
    from src.storage import function_store as _fs
    assert _fs.deployment_history("w1") == []


async def test_execute_wake_failed_marks_failed_keeps_ledger(qa_env, monkeypatch):
    """场叫不醒（强制停止等）：差事账仍落，ok=False ⇒ resume 会补唤起。"""
    await _seed_qa()
    from src.api import deploy_runner, routes

    async def _fake(wid, card_id, content, **kw):
        return {"error": {"code": "halted", "message": "卡已停止"}}

    monkeypatch.setattr(routes, "_run_message", _fake)
    res = await deploy_runner.execute(
        "w1", spec={"function": "qa", "venue_kind": "deploy", "wake": True},
        venue="c1", mandate="去验收。", policy_id="p1")
    assert res["ok"] is False and res["reason"] == "wake_failed"
    assert res["deployment_id"] > 0
    from src.storage import function_store as _fs
    assert _fs.get_deployment("w1", res["deployment_id"]) is not None


# ---------------------------------------------------------- 投递链与重启
async def test_deliver_fired_deploy_end_to_end(qa_env, monkeypatch):
    """record_fired → deliver_fired：deploy 走独立 dispatch，不进送话管线。"""
    await _seed_qa()
    from src.infra import policy_runner as runner
    from src.api import routes

    async def _fake(wid, card_id, content, **kw):
        return {"ok": True}

    monkeypatch.setattr(routes, "_run_message", _fake)
    f = _dep_fired()
    ir.record_fired("w1", [f])
    n = await runner.deliver_fired("w1", [f])
    assert n == 1
    st = ist.store()
    aid = f["_actions"]["c1"]
    row = st.get_action("w1", aid)
    assert row["state"] == "delivered"
    receipt = row["receipt"]
    if isinstance(receipt, str):
        receipt = json.loads(receipt or "{}")
    assert receipt["deployment_id"] > 0


async def test_resume_redelivers_pending_deploy(qa_env, monkeypatch):
    """重启恢复：pending deploy 动作走部署执行器（补唤起），不走送话。"""
    await _seed_qa()
    from src.api import routes

    ran = {}

    async def _fake(wid, card_id, content, **kw):
        ran["venue"] = card_id
        return {"ok": True}

    monkeypatch.setattr(routes, "_run_message", _fake)
    f = _dep_fired()
    ir.record_fired("w1", [f])
    stats = await ir.resume("w1")
    assert stats["redelivered"] == 1
    assert ran["venue"] == "c1"
    st = ist.store()
    assert st.get_action("w1", f["_actions"]["c1"])["state"] == "delivered"


async def test_feed_e2e_msg_report_done_deploys(qa_env, tmp_path, monkeypatch):
    """msg_report(kind=done) → flatten 留 kind → 策略命中 → deploy 执行。"""
    await _seed_qa()
    from src.infra import policy_config, policy_runner as runner
    from src.infra import policy_set as ps
    from src.api import routes

    policy = {
        "id": "staff_qa_on_stage_done", "active": True,
        "scope": {"kind": "card"}, "watch": ["msg_report"],
        "window": "none",
        "when": {"field": "trigger_data.kind", "cmp": "eq", "value": "done"},
        "then": {"deploy": {
            "function": "qa", "venue": "event:card", "ctx": "event:card",
            "target_card": "event:card", "venue_kind": "deploy",
            "wake": True, "mandate": "验收 {event.card}。"}},
    }
    p = tmp_path / "policies.json"
    p.write_text(json.dumps({"policies": [policy]}, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setenv("ECO_POLICIES_PATH", str(p))
    pset = ps.PolicySet()
    assert pset.load(policy_config.load_raw()) == []
    ps.reset_set(pset)

    async def _fake(wid, card_id, content, **kw):
        return {"ok": True}

    monkeypatch.setattr(routes, "_run_message", _fake)
    fired = runner.feed("w1", {"seq": 5, "ts": time.time(),
                             "type": "msg_report", "card": "c1",
                             "data": {"from": "c1", "to": "p1",
                                      "msg_type": "msg_report",
                                      "kind": "done",
                                      "content": "做完了"}})
    await runner.drain()
    assert len(fired) == 1, "kind 零件落库后策略必须命中"
    st = ist.store()
    aid = fired[0]["_actions"]["c1"]
    assert st.get_action("w1", aid)["state"] == "delivered"
    from src.storage import function_store as _fs
    act = _fs.active_for_function("w1", "fn_qa")
    assert len(act) == 1 and act[0]["venue_ref"] == "c1"

    # 非 done 汇报不触发（不误派）
    fired2 = runner.feed("w1", {"seq": 6, "ts": time.time(),
                              "type": "msg_report", "card": "c2",
                              "data": {"from": "c2", "to": "p1",
                                       "msg_type": "msg_report",
                                       "kind": "blocked",
                                       "content": "卡住了"}})
    await runner.drain()
    assert fired2 == []
