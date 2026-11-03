# -*- coding: utf-8 -*-
"""干预生命周期（intervention_runner）单测。

守四件事：
  ① 命中落账：record_fired 把目标、动作行、事项一次性写进 harness.db；
  ② 解除有据：on_fact 按 check_on 复核，权威状态说解决了才关事项；
  ③ 投前复核：action_skip 拦掉已解决/已送达/已作废的动作；
  ④ 重启接责：resume 先复核事项再重投，已送达的靠事实库 action_id 查重。
"""
from __future__ import annotations

import time

import pytest

from src.infra import fact_store as fs
from src.infra import intervention_runner as ir
from src.infra import intervention_store as ist


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """台账 + 事实库都指到沙箱；review_state 用 monkeypatch 直接喂。"""
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    s = ist.InterventionStore(works_dir=tmp_path)
    ist.reset(s)
    fstore = fs.FactStore(works_dir=tmp_path)
    fs.reset(fstore)
    yield s, fstore
    s.close()
    fstore.close()
    ist.reset(ist.InterventionStore(works_dir=tmp_path))
    fs.reset(fs.FactStore(works_dir=tmp_path))


def _fired(**kw) -> dict:
    f = {
        "policy": "review_all_passed_need_promote",
        "scope": ["r1"],
        "then": {"target": "self", "mode": "continue", "wake": True,
                 "message": "请调 promote_draft",
                 "matter": {"kind": "review_promote", "key": "event:batch",
                            "check_on": ["card_block_edited",
                                          "review_batch_closed"]}},
        "ctx": {"work_id": "w1", "card_id": "r1", "parent": "",
                "scope": ["r1"],
                "trigger_data": {"batch": "b1", "target": "c9"}},
        "basis": {"card": "r1", "seq": 42, "type": "review_all_passed"},
    }
    f.update(kw)
    return f


def _patch_state(monkeypatch, batch="b1", verdict=""):
    monkeypatch.setattr(ir, "_review_state",
                        lambda w, rid: {"batch": batch, "verdict": verdict})


# ---------------------------------------------------------- 命中落账
def test_record_fired_creates_matter_and_action(wired):
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    assert f["_targets"] == ["r1"]
    aid = f["_actions"]["r1"]
    mid = f["_matter_id"]
    assert aid and mid
    m = s.get_matter("w1", mid)
    assert m["kind"] == "review_promote" and m["matter_key"] == "b1"
    a = s.get_action("w1", aid)
    assert a["state"] == "pending" and a["matter_id"] == mid


def test_record_fired_second_trigger_same_matter(wired):
    """同一批次重复命中：事项不重复立案，新动作仍登记（fire_key 不同）。"""
    s, _ = wired
    ir.record_fired("w1", [_fired()])
    f2 = _fired()
    f2["basis"] = {"card": "r1", "seq": 55, "type": "review_all_passed"}
    ir.record_fired("w1", [f2])
    assert f2["_matter_id"] == s.open_matters("w1")[0]["matter_id"]
    assert len(s.open_matters("w1")) == 1


def test_record_fired_replay_same_trigger_no_dup(wired):
    """重启重演：同一触发事实（同 seq）不重复登记动作。"""
    s, _ = wired
    ir.record_fired("w1", [_fired()])
    f2 = _fired()
    ir.record_fired("w1", [f2])
    acts = s.pending_actions("w1")
    assert len(acts) == 1 and f2["_actions"]["r1"] == acts[0]["action_id"]


def test_record_fired_without_matter_spec(wired):
    """没声明 matter 的策略：动作照样登记，不立案。"""
    s, _ = wired
    f = _fired()
    f["then"] = {"target": "self", "mode": "message", "wake": False,
                 "message": "x"}
    ir.record_fired("w1", [f])
    assert s.open_matters("w1") == []
    assert len(s.pending_actions("w1")) == 1


# ---------------------------------------------------------- 解除检查
def test_on_fact_resolves_matter(wired, monkeypatch):
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    _patch_state(monkeypatch, batch="", verdict="批次b1全员通过")
    ir.on_fact("w1", {"type": "review_batch_closed"})
    m = s.get_matter("w1", f["_matter_id"])
    assert m["state"] == "resolved" and m["evidence"]["verdict"]
    assert s.get_action("w1", f["_actions"]["r1"])["state"] == "stale"


def test_on_fact_ignores_unwatched_types(wired, monkeypatch):
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    _patch_state(monkeypatch, batch="", verdict="过")
    ir.on_fact("w1", {"type": "done"})   # 不在 check_on 里 ⇒ 不复核
    assert s.get_matter("w1", f["_matter_id"])["state"] == "open"


def test_on_fact_batch_replaced_cancels(wired, monkeypatch):
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    _patch_state(monkeypatch, batch="b2", verdict="")
    ir.on_fact("w1", {"type": "review_batch_closed"})
    assert s.get_matter("w1", f["_matter_id"])["state"] == "cancelled"


def test_on_fact_still_open_keeps(wired, monkeypatch):
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    _patch_state(monkeypatch, batch="b1", verdict="")
    ir.on_fact("w1", {"type": "tool_result"})
    assert s.get_matter("w1", f["_matter_id"])["state"] == "open"


# ---------------------------------------------------------- 投前复核
def test_action_skip_after_resolved(wired, monkeypatch):
    f = _fired()
    ir.record_fired("w1", [f])
    aid = f["_actions"]["r1"]
    _patch_state(monkeypatch, batch="", verdict="过")
    assert ir.action_skip("w1", aid) is True      # 投前复核顺手关账
    assert ir.already_delivered is not None       # 模块面完好


def test_action_skip_normal_pending(wired, monkeypatch):
    f = _fired()
    ir.record_fired("w1", [f])
    _patch_state(monkeypatch, batch="b1")          # 批还在 ⇒ 放行
    assert ir.action_skip("w1", f["_actions"]["r1"]) is False


def test_action_skip_delivered(wired, monkeypatch):
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    aid = f["_actions"]["r1"]
    s.mark_action("w1", aid, "delivered")
    _patch_state(monkeypatch, batch="b1")
    assert ir.action_skip("w1", aid) is True


# ---------------------------------------------------------- 重启恢复
async def test_resume_redelivers_pending(wired, monkeypatch):
    f = _fired()
    ir.record_fired("w1", [f])
    aid = f["_actions"]["r1"]
    _patch_state(monkeypatch, batch="b1")

    sent = {}

    async def _fake(wid, target, text, **kw):
        sent["kw"] = kw
        return "continue:1"

    monkeypatch.setattr("src.infra.harness.deliver", _fake)
    stats = await ir.resume("w1")
    assert stats["redelivered"] == 1
    assert sent["kw"]["action_id"] == aid
    assert sent["kw"]["mode"] == "continue"
    s, _ = wired
    assert s.get_action("w1", aid)["state"] == "delivered"


async def test_resume_skips_already_delivered(wired, monkeypatch):
    """投递成功但进程在写回执前崩了：事实库里的 action_id 让它不重发。"""
    s, fstore = wired
    f = _fired()
    ir.record_fired("w1", [f])
    aid = f["_actions"]["r1"]
    fstore.ingest("w1", {"seq": 1, "ts": time.time(),
                       "type": "harness_message", "card": "r1",
                       "data": {"rule_id": "review_all_passed_need_promote",
                                "to": "r1", "action_id": aid,
                                "mode": "continue"}})
    _patch_state(monkeypatch, batch="b1")
    called = []

    async def _fake(*a, **kw):
        called.append(1)
        return "continue:1"

    monkeypatch.setattr("src.infra.harness.deliver", _fake)
    stats = await ir.resume("w1")
    assert stats["redelivered"] == 0 and called == []
    assert s.get_action("w1", aid)["state"] == "delivered"


async def test_resume_closes_resolved_matter_without_wake(wired, monkeypatch):
    """重启前模型已自己定稿：事项带证据关闭，不再叫醒。"""
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    aid = f["_actions"]["r1"]
    _patch_state(monkeypatch, batch="", verdict="批次b1全员通过")
    called = []

    async def _fake(*a, **kw):
        called.append(1)
        return "continue:1"

    monkeypatch.setattr("src.infra.harness.deliver", _fake)
    stats = await ir.resume("w1")
    assert stats["closed"] == 1 and called == []
    assert s.get_matter("w1", f["_matter_id"])["state"] == "resolved"
    assert s.get_action("w1", aid)["state"] == "stale"


# ---------------------------------------------------------- 端到端（feed 真链）
async def test_feed_promote_chain_e2e(wired, tmp_path, monkeypatch):
    """review_all_passed → feed → 命中 → 落账 → 投递带 action_id。

    顺带钉住 2026-10-31 修的死规则：need_promote 不在 catalog 零件里时
    flatten_parts 会丢弃它，本策略永远触发不了——现在零件已补上。
    """
    from src.infra import policy_config, policy_runner as runner
    from src.infra import policy_set as ps

    s, _ = wired
    policy = {
        "id": "review_all_passed_need_promote", "active": True,
        "scope": {"kind": "card"}, "watch": ["review_all_passed"],
        "window": "none",
        "when": {"event": {"types": ["review_all_passed"],
                           "where": {"need_promote": True},
                           "within": "turn"}},
        "then": {"target": "self", "mode": "continue", "wake": True,
                 "message": "去 promote_draft",
                 "matter": {"kind": "review_promote", "key": "event:batch",
                            "check_on": ["review_batch_closed"]}},
    }
    p = tmp_path / "policies.json"
    import json as _json
    p.write_text(_json.dumps({"policies": [policy]}, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setenv("ECO_POLICIES_PATH", str(p))
    pset = ps.PolicySet()
    assert pset.load(policy_config.load_raw()) == []
    ps.reset_set(pset)

    sent = {}

    async def _fake(wid, target, text, **kw):
        sent["kw"] = kw
        return "continue:1"

    monkeypatch.setattr("src.infra.harness.deliver", _fake)
    _patch_state(monkeypatch, batch="b1")      # 投前复核：批还开着 ⇒ 放行

    fired = runner.feed("w1", {"seq": 7, "ts": time.time(),
                             "type": "review_all_passed", "card": "r1",
                             "data": {"batch": "b1", "members": ["a", "b"],
                                      "target": "c9", "need_promote": True}})
    await runner.drain()
    assert len(fired) == 1, "need_promote 零件落库后策略必须真正命中"
    aid = fired[0]["_actions"]["r1"]
    assert sent["kw"]["action_id"] == aid
    assert sent["kw"]["mode"] == "continue"
    m = s.get_matter("w1", fired[0]["_matter_id"])
    assert m["matter_key"] == "b1" and m["state"] == "open"
    assert s.get_action("w1", aid)["state"] == "delivered"


# ---------------------------------------------------------- 错误收集
def test_resolve_failure_records_error(wired, monkeypatch):
    """目标解析抛异常：无动作行，但错误落账——义务不失声。"""
    s, _ = wired

    def _boom(*a, **kw):
        raise RuntimeError("event:xx 取不到")

    monkeypatch.setattr("src.infra.harness.resolve_targets", _boom)
    f = _fired()
    ir.record_fired("w1", [f])
    errs = s.list_errors("w1")
    assert len(errs) == 1
    e = errs[0]
    assert e["stage"] == "resolve" and e["kind"] == "RuntimeError"
    assert e["policy_id"] == "review_all_passed_need_promote"
    assert e["detail"]["where"] == "self"


def test_check_exception_records_error(wired, monkeypatch):
    """解除判定异常：事项保持 open（宁可多留），错误落账且累计。"""
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])

    def _boom(w, m):
        raise ValueError("review_state 坏了")

    monkeypatch.setitem(ir._CHECKERS, "review_promote", _boom)
    ir.on_fact("w1", {"type": "review_batch_closed"})
    ir.on_fact("w1", {"type": "review_batch_closed"})
    assert s.get_matter("w1", f["_matter_id"])["state"] == "open"
    errs = [e for e in s.list_errors("w1") if e["stage"] == "check"]
    assert len(errs) == 1 and errs[0]["count"] == 2
    assert errs[0]["matter_id"] == f["_matter_id"]


def test_mark_receipt_failure_records_error(wired):
    """投递失败：动作行 failed＋回执之外，错误台账也收一笔。"""
    s, _ = wired
    f = _fired()
    ir.record_fired("w1", [f])
    aid = f["_actions"]["r1"]
    ir.mark_receipt("w1", aid, None)
    errs = [e for e in s.list_errors("w1") if e["stage"] == "deliver"]
    assert len(errs) == 1
    assert errs[0]["kind"] == "delivery_failed"
    assert errs[0]["action_id"] == aid
    assert s.get_action("w1", aid)["state"] == "failed"


def test_load_errors_recorded_once_per_work(wired, monkeypatch):
    """策略加载错误：dispatcher() 丢弃的 load() 返回，首个 feed 时按
    清单指纹补记一轮——同一份错不重复计。"""
    from src.infra import policy_runner as runner
    from src.infra import policy_set as ps

    s, _ = wired
    runner._CFG_ERR_DONE.clear()
    pset = ps.PolicySet()
    errs = pset.load([{"id": "bad1", "scope": {"kind": "bogus"},
                      "watch": [], "when": {}, "then": {}}])
    assert errs
    ps.reset_set(pset)
    runner._record_load_errors("w1")
    rows = [e for e in s.list_errors("w1") if e["stage"] == "config"]
    assert len(rows) == len(errs)
    runner._record_load_errors("w1")          # 同一份清单 ⇒ 不再计
    rows2 = [e for e in s.list_errors("w1") if e["stage"] == "config"]
    assert all(e["count"] == 1 for e in rows2)
    runner._CFG_ERR_DONE.clear()


def test_status_includes_errors(wired):
    s, _ = wired
    s.record_error("w1", stage="config", kind="load_error", detail="x")
    st = ir.status("w1")
    assert st["errors"] and st["errors"][0]["kind"] == "load_error"
