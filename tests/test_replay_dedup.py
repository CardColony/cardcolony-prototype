# -*- coding: utf-8 -*-
"""[批次A · 双 work 回放与去重基线 2026-11-20] 任务书 §3.A 通过条件：

- 同一事件重放、同一等待状态的新事件、重启后恢复——等待期间
  **不增加投递/模型请求**、**不出现第二个 open 事项**；
- 可运行步骤确实出现时恰好恢复一次；
- 旧事件缺 ``runnable`` ⇒ 记「信息不足」（不按新旧语义猜）；
- 事项按业务状态去重、动作沿用原动作身份（matter_reused → 原
  action_id，零新投递）。

夹具：``tests/fixtures/replay/livelock_fragments.json``——
work_073/card_050（awaiting_halted×98万活锁）与 work_027/card_230
（seat:vote_nudge×8万活锁）的逐字段快照；盲回放用原 seq/ts，
后见之明只留在断言侧。
"""
import json
from pathlib import Path

import pytest

from src.infra import (
    intervention_runner, intervention_store, policy_set, replay,
)

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "livelock_fragments.json"
FRAGS = json.loads(FIXTURE.read_text(encoding="utf-8"))["fragments"]


def _frag(name):
    return FRAGS[name]["events"]


def _real_policies() -> list[dict]:
    """直读仓库真配置——conftest 沙箱把 ECO_POLICIES_PATH 指到空表
    （防差事策略真点火），本组用例钉的恰是两条真实评审策略
    （与 test_policy_set._real_policies 同一先例）。"""
    from src.infra import policy_config
    raw = json.loads(policy_config.DEFAULT_PATH.read_text(encoding="utf-8"))
    items = raw.get("policies") if isinstance(raw, dict) else raw
    return [x for x in (items or []) if isinstance(x, dict)]


@pytest.fixture(autouse=True)
def _real_policy_set():
    """真实策略表装进 dispatcher 单例＋每用例一副干净内存本子。"""
    st = policy_set.PolicySet()
    assert st.load(_real_policies()) == []
    policy_set.reset_set(st)
    yield
    policy_set.reset_set()


def _mk_event(cid, seq, ts, typ, data):
    """新格式事件（老库里不存在的形态）——沿用真实记录键序。"""
    return {"seq": seq, "card_id": cid, "run_id": "replay",
            "round": 1, "type": typ, "data": dict(data),
            "model": "", "ts": ts}


def _set_review_state(wid, cid, **state):
    from src.storage import card_db

    def _fn(conn):
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(state, ensure_ascii=False), cid))
    card_db._write_sync(wid, _fn)


def _actions(wid):
    st = intervention_store.store()
    conn = st._conn(wid)
    return [dict(r) for r in conn.execute("SELECT * FROM actions")]


def _open_matters(wid):
    return intervention_store.store().open_matters(wid)


# ================================================================ ① 同一事件重放
async def test_same_event_replay_dedup(work):
    wid = work["work_id"]
    ev = _mk_event("card_050", 5000, "2026-11-15T10:00:00+08:00",
                   "review_round_done",
                   {"batch": "rv_x", "draft": 1, "seat": "fn_001",
                    "next": "seat:fn_001:speak",
                    "reason": "seat:speak", "continued": True,
                    "has_next": True, "runnable": True})
    r1 = await replay.replay_events(wid, [ev])
    assert r1["rows"][0]["verdict"] == "fired"
    assert len(r1["deliveries"]) == 1
    aid1 = r1["deliveries"][0]["action_id"]

    r2 = await replay.replay_events(wid, [ev])   # 同一条再喂一遍
    assert r2["rows"][0]["verdict"] == "fired"   # 条件照样命中
    assert r2["deliveries"] == []                # 但零新投递
    rows2 = r2["rows"][0]["fired"]
    assert rows2 and list(rows2[0]["actions"].values()) == [aid1], \
        "同一触发事实 ⇒ 沿用同一 action_id（fire_key 去重）"
    assert len(_actions(wid)) == 1


# ================================================================ ② 同一等待状态的新事件
async def test_halted_streak_zero_delivery(work):
    """073/050 活锁片段：三条连续 awaiting_halted（无 runnable 字段）——
    runnable 闸拒发 ⇒ 每条如实记『信息不足』，全程零投递零动作。"""
    wid = work["work_id"]
    res = await replay.replay_events(wid, _frag("card_050_halted_streak"))
    assert len(res["rows"]) == 3
    for row in res["rows"]:
        assert row["verdict"] == "insufficient_info", row
        assert row["deliveries_new"] == 0
        assert "runnable" in row["insufficient"]["review_step_not_finished"]
    assert res["deliveries"] == [] and _actions(wid) == []


async def test_230_nudge_streak_zero_delivery(work):
    """027/230 活锁片段：seat:vote_nudge 连发——同样信息不足、零投递。"""
    wid = work["work_id"]
    res = await replay.replay_events(wid, _frag("card_230_nudge_streak"))
    assert {r["verdict"] for r in res["rows"]} == {"insufficient_info"}
    assert res["deliveries"] == []


# ================================================================ ③ 可运行步骤恢复一次
async def test_runnable_step_resumes_once(work):
    """runnable=true 的 round_done ⇒ 恰好一次 continue 投递，重放不再发。"""
    wid = work["work_id"]
    base = dict(_frag("card_050_normal_rounds")[1])     # align_passed 片段
    base["data"] = dict(base["data"]); base["data"]["runnable"] = True
    # [第三轮返工] 夹具是旧格式事件（无 next）——补生产者身份字段
    # 才算新格式可投递；缺 next 的旧事件走「只做诊断」销账分支。
    base["data"]["next"] = "seat:fn_001:speak"
    base["seq"] = 5001
    res = await replay.replay_events(wid, [base, dict(base)])
    assert res["rows"][0]["verdict"] == "fired"
    assert res["rows"][0]["deliveries_new"] == 1
    assert res["deliveries"][0]["mode"] == "continue"
    assert res["rows"][1]["deliveries_new"] == 0       # 重放同事实零追加


# ================================================================ ④ 业务状态→事项/动作去重
async def test_matter_same_state_reuses_action(work):
    """同 batch 的 review_all_passed 连到三条（不同 seq/ts＝不同事实、
    同一业务状态）：一个 open 事项、一条动作行、一次投递；
    后两条 fired 行如实标 reused 并沿用原 action_id。"""
    wid = work["work_id"]
    cid = "card_001"                                   # work 根卡充当评审卡
    _set_review_state(wid, cid, batch="rv_p1")
    evs = [_mk_event(cid, 100 + i, f"2026-11-20T10:0{i}:00+08:00",
                     "review_all_passed",
                     {"batch": "rv_p1", "need_promote": True})
           for i in range(3)]
    res = await replay.replay_events(wid, evs)
    ms = _open_matters(wid)
    assert len(ms) == 1, "同一业务状态只许一个 open 事项"
    acts = _actions(wid)
    assert len(acts) == 1, "同一业务状态只许一条动作行"
    assert len(res["deliveries"]) == 1
    aid = res["deliveries"][0]["action_id"]
    assert res["rows"][0]["fired"][0]["reused"] is False
    for row in res["rows"][1:]:
        f = row["fired"][0]
        assert f["reused"] is True
        assert f["matter"] == ms[0]["matter_id"]
        assert list(f["actions"].values()) == [aid], "动作沿用原身份"
        assert row["deliveries_new"] == 0


async def test_matter_resolves_when_batch_closed(work):
    """定稿：批次收口＋正稿 verdict ⇒ 事项 resolved（不再误报未解）。"""
    wid = work["work_id"]
    cid = "card_001"
    _set_review_state(wid, cid, batch="rv_p2")
    ev = _mk_event(cid, 200, "2026-11-20T11:00:00+08:00",
                   "review_all_passed",
                   {"batch": "rv_p2", "need_promote": True})
    await replay.replay_events(wid, [ev])
    assert len(_open_matters(wid)) == 1
    _set_review_state(wid, cid, batch="", verdict="通过",
                    closed_batch="rv_p2")
    res = await replay.replay_events(
        wid, [_mk_event(cid, 201, "2026-11-20T11:01:00+08:00",
                        "review_batch_closed",
                        {"batch": "rv_p2", "verdict": "通过"})])
    assert _open_matters(wid) == []
    st = intervention_store.store()
    m = st.get_matter(wid, res["rows"][0].get("matter_id") or
                    st._conn(wid).execute(
                        "SELECT matter_id FROM matters").fetchone()["matter_id"])
    assert m and m["state"] == "resolved"


# ================================================================ ⑤ 重启后恢复
async def test_restart_replay_no_double(work):
    """重启（内存本子清零、台账落盘不动）后重放同一事件：
    fire_key 去重＋投前复核 ⇒ 零新增投递、零第二事项。"""
    wid = work["work_id"]
    cid = "card_001"
    _set_review_state(wid, cid, batch="rv_p3")
    ev = _mk_event(cid, 300, "2026-11-20T12:00:00+08:00",
                   "review_all_passed",
                   {"batch": "rv_p3", "need_promote": True})
    await replay.replay_events(wid, [ev])
    assert len(_actions(wid)) == 1

    policy_set.dispatcher().reset()                    # 模拟重启
    res = await replay.replay_events(wid, [ev])
    assert res["deliveries"] == []
    assert res["rows"][0]["fired"][0]["reused"] is True
    assert len(_open_matters(wid)) == 1 and len(_actions(wid)) == 1


async def test_resume_pending_redelivers_once(work):
    """pending（上次没送成）动作：resume 恰重投一次；delivered 的不动。"""
    wid = work["work_id"]
    cid = "card_001"
    _set_review_state(wid, cid, batch="rv_p4")
    ev = _mk_event(cid, 400, "2026-11-20T13:00:00+08:00",
                   "review_all_passed",
                   {"batch": "rv_p4", "need_promote": True})
    res = await replay.replay_events(wid, [ev])
    assert len(res["deliveries"]) == 1
    aid = res["deliveries"][0]["action_id"]

    st = intervention_store.store()
    st.mark_action(wid, aid, "pending")                # 模拟「没送成」
    out = await replay.resume_after_restart(wid)
    assert len(out["deliveries"]) == 1, "pending 动作恰重投一次"
    assert out["deliveries"][0]["action_id"] == aid
    assert st.get_action(wid, aid)["state"] == "delivered"
    # 已 delivered 的再 resume ⇒ 零重投
    out2 = await replay.resume_after_restart(wid)
    assert out2["deliveries"] == []


# ================================================================ ⑥ 盲回放边界
async def test_finalize_and_recovery_events_insufficient(work):
    """席位恢复/正常定稿/唤醒失败样本——老格式同样缺 runnable，
    盲回放口径下全部如实记信息不足、零投递。"""
    wid = work["work_id"]
    for name in ("card_050_finalize", "card_050_seat_confirm",
                 "card_230_seat_recovery", "card_230_wake_failed",
                 "card_230_finalize", "card_230_normal_rounds",
                 "card_050_normal_rounds"):
        res = await replay.replay_events(wid, _frag(name))
        for row in res["rows"]:
            assert row["deliveries_new"] == 0, (name, row)
            assert row["verdict"] in ("insufficient_info", "no_fire"), (name, row)
        policy_set.dispatcher().reset()                # 片段间互不串账


# ================================================================ ⑦ 同一步骤去重（返工1）
_STEP = {"batch": "rv_s1", "draft": 1, "seat": "fn_001",
         "pending": ["fn_002"], "next": "seat:fn_002:speak",
         "runnable": True, "has_next": True,
         "all_passed": False, "continued": True}


async def test_same_step_new_fact_dedup(work):
    """[返工1] 同一可运行步骤只改 seq/ts（批次/稿次/席位/待投全同）
    ⇒ 一条动作行、一次投递；第二条 fired 沿用同一 action_id。"""
    wid = work["work_id"]
    ev1 = _mk_event("card_050", 6001, "2026-11-20T10:00:00+08:00",
                    "review_round_done", dict(_STEP))
    ev2 = _mk_event("card_050", 6002, "2026-11-20T10:00:01+08:00",
                    "review_round_done", dict(_STEP))
    res = await replay.replay_events(wid, [ev1, ev2])
    assert res["rows"][0]["verdict"] == "fired"
    assert res["rows"][1]["verdict"] == "fired"
    assert len(_actions(wid)) == 1, "同一步骤只许一条动作行"
    assert len(res["deliveries"]) == 1, "同一步骤只许一次投递"
    aid = res["deliveries"][0]["action_id"]
    f2 = res["rows"][1]["fired"][0]
    assert list(f2["actions"].values()) == [aid], "biz_key 去重沿用原动作身份"
    assert res["rows"][1]["deliveries_new"] == 0


async def test_next_step_delivers_once(work):
    """[返工1正例] 真正进入下一步——评审生产者上报的 ``next`` 动作身份
    变了（seat:speak → dispatch:promote）⇒ 新 biz_key ⇒ 新动作行、
    恰好再投递一次——去重不误伤真实推进。"""
    wid = work["work_id"]
    step2 = dict(_STEP); step2["next"] = "dispatch:promote"
    res = await replay.replay_events(wid, [
        _mk_event("card_050", 6101, "2026-11-20T10:10:00+08:00",
                  "review_round_done", dict(_STEP)),
        _mk_event("card_050", 6102, "2026-11-20T10:10:01+08:00",
                  "review_round_done", step2)])
    acts = _actions(wid)
    assert len(acts) == 2, "不同步骤 ⇒ 两条动作行"
    assert len(res["deliveries"]) == 2, "每步恰好一次投递"
    assert res["rows"][1]["deliveries_new"] == 1
    assert res["deliveries"][1]["action_id"] != res["deliveries"][0]["action_id"]


# ================================================================ ⑧ 动作写入失败恢复（返工2）
async def test_action_write_failure_no_unledgered_delivery(work, monkeypatch):
    """[返工2] 事项已立案、动作写入失败 ⇒ **零无 action_id 的裸投递**；
    重放同一事实 ⇒ 事项零动作落正常登记自愈 ⇒ 恰好一次投递。"""
    wid = work["work_id"]
    cid = "card_001"
    _set_review_state(wid, cid, batch="rv_p9")
    ev = _mk_event(cid, 900, "2026-11-20T14:00:00+08:00",
                   "review_all_passed", {"batch": "rv_p9", "need_promote": True})
    from src.infra import intervention_store as ist
    orig = ist.InterventionStore.record_action
    calls = {"n": 0}

    def _flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", False                       # 首次动作写入中断
        return orig(self, *a, **k)

    monkeypatch.setattr(ist.InterventionStore, "record_action", _flaky)
    res = await replay.replay_events(wid, [ev])
    assert res["deliveries"] == [], "登记失败 ⇒ 不许无编号投递"
    assert len(_open_matters(wid)) == 1, "事项照落"
    assert _actions(wid) == [], "动作行确实没写成"
    # 重放同一事实：open 事项但零动作 ⇒ 不走 reused，落正常登记自愈
    res2 = await replay.replay_events(wid, [ev])
    acts = _actions(wid)
    assert len(acts) == 1, "重放自愈：动作补齐"
    assert acts[0]["matter_id"] == _open_matters(wid)[0]["matter_id"]
    assert len(res2["deliveries"]) == 1
    assert res2["deliveries"][0]["action_id"] == acts[0]["action_id"]


async def test_resume_rebuilds_missing_action(work):
    """[返工2恢复路径] open 事项零动作（模拟中断残留）⇒ resume 重建
    动作行并投递一次——事项不许永久无主。"""
    wid = work["work_id"]
    cid = "card_001"
    _set_review_state(wid, cid, batch="rv_p8")
    st = intervention_store.store()
    mid, _new = st.open_matter(
        wid, policy_id="review_all_passed_need_promote",
        scope_key=f"card:{cid}", kind="review_promote",
        matter_key="rv_p8",
        objects={"review_card": cid, "target": ""},
        check_on=["card_block_edited", "review_batch_closed", "tool_result"],
        basis={"card": cid, "seq": 800, "ts": "2026-11-20T14:30:00+08:00",
               "type": "review_all_passed"})
    assert mid and _new
    assert _actions(wid) == []
    out = await replay.resume_after_restart(wid)
    acts = _actions(wid)
    assert len(acts) == 1, "resume 必须补齐缺失动作"
    assert acts[0]["matter_id"] == mid
    assert acts[0]["fire_key"].endswith("card_001:800|2026-11-20T14:30:00+08:00")
    assert len(out["deliveries"]) == 1
    assert out["deliveries"][0]["action_id"] == acts[0]["action_id"]
    assert st.get_action(wid, acts[0]["action_id"])["state"] == "delivered"


# ================================================================ ⑨ 续跑被拒记账（返工3）
async def test_continue_refused_not_delivered(work):
    """[返工3] 投递回执 ``continue:0``（评审侧 already_running/stopped…）
    ⇒ 台账 failed 不是 delivered；忙碌期 resume 重投仍被拒保持 failed；
    真正可运行后 resume ⇒ 恰好受理一次 delivered，再 resume 零重投。"""
    wid = work["work_id"]
    ev = _mk_event("card_050", 7001, "2026-11-20T15:00:00+08:00",
                   "review_round_done", dict(_STEP))
    refuse = {"on": True}

    def _fn(_entry):
        return "continue:0" if refuse["on"] else "continue:1"

    res = await replay.replay_events(wid, [ev], deliver_fn=_fn)
    assert len(res["deliveries"]) == 1                     # 尝试过一次
    acts = _actions(wid)
    assert len(acts) == 1
    st = intervention_store.store()
    assert acts[0]["state"] == "failed", "被拒绝 ⇒ failed，不是 delivered"
    # 忙碌期恢复：重投仍被拒 ⇒ 仍 failed（零受理）
    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1                     # 又试了一次
    assert st.get_action(wid, acts[0]["action_id"])["state"] == "failed"
    # 真正可运行：恢复 ⇒ 恰好受理一次
    refuse["on"] = False
    out2 = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out2["deliveries"]) == 1
    assert st.get_action(wid, acts[0]["action_id"])["state"] == "delivered"
    out3 = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert out3["deliveries"] == [], "delivered 后零重投"


# ================================================================ ⑩ 返工2·真实 target 字段
async def test_resume_rebuild_uses_persisted_target(work, monkeypatch):
    """[返工2·真实形态] ``review_all_passed`` 的 ``data.target`` 是被评卡
    ``card_870``；策略 ``self`` 的解析目标是评审卡 ``card_001``——
    登记中断后 resume 必须沿立案时持久化的 ``deliver_targets`` 投
    ``card_001``，绝不投 ``card_870``。"""
    wid = work["work_id"]
    cid = "card_001"                       # 评审卡（scope/self 落点）
    reviewed = "card_870"                  # 被评卡（事件 target 字段）
    _set_review_state(wid, cid, batch="rv_t1")
    ev = _mk_event(cid, 950, "2026-11-20T16:00:00+08:00",
                   "review_all_passed",
                   {"batch": "rv_t1", "need_promote": True,
                    "target": reviewed})
    from src.infra import intervention_store as ist
    orig = ist.InterventionStore.record_action
    calls = {"n": 0}

    def _flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", False
        return orig(self, *a, **k)

    monkeypatch.setattr(ist.InterventionStore, "record_action", _flaky)
    res = await replay.replay_events(wid, [ev])
    assert res["deliveries"] == []
    assert len(_open_matters(wid)) == 1
    assert _actions(wid) == []
    # 恢复：重建动作必须落在 card_001（deliver_targets 持久化值）
    out = await replay.resume_after_restart(wid)
    acts = _actions(wid)
    assert len(acts) == 1
    assert acts[0]["target"] == cid, "投递落点是评审卡 self，不是被评卡"
    assert acts[0]["target"] != reviewed
    assert len(out["deliveries"]) == 1
    assert out["deliveries"][0]["target"] == cid
    assert out["deliveries"][0]["target"] != reviewed


# ================================================================ ⑪ 返工3·并发领取
async def test_inflight_action_not_double_delivered(work):
    """[返工3] 同 ``action_id`` 在途（``sending``）时，同状态新事实的
    第二个投递者被原子领取闸挡下——**实际投递次数不增**；在途属主死
    （updated_ts 超宽限）后 resume 可收、恰好再投一次。"""
    import time as _time
    wid = work["work_id"]
    ev1 = _mk_event("card_050", 8001, "2026-11-20T17:00:00+08:00",
                    "review_round_done", dict(_STEP))
    res1 = await replay.replay_events(wid, [ev1])
    assert len(res1["deliveries"]) == 1
    aid = res1["deliveries"][0]["action_id"]
    st = intervention_store.store()
    # 模拟「第一笔投递尚未返回」：动作在途
    st.mark_action(wid, aid, "sending")
    # 第二条同状态事实（异 seq/ts）⇒ 同 biz_key ⇒ 同 aid ⇒ 领取失败
    ev2 = _mk_event("card_050", 8002, "2026-11-20T17:00:01+08:00",
                    "review_round_done", dict(_STEP))
    res2 = await replay.replay_events(wid, [ev2])
    assert res2["rows"][0]["verdict"] == "fired"
    assert res2["deliveries"] == [], "在途动作不许二次投递"
    assert len(_actions(wid)) == 1
    # 在途属主死（时间戳超 boot/宽限）⇒ resume 凭 claim 收编 ⇒ 恰好再投一次
    conn = st._conn(wid)
    conn.execute("UPDATE actions SET updated_ts=? WHERE action_id=?",
                 (_time.time() - 4000, aid))
    conn.commit()
    out = await replay.resume_after_restart(wid)
    assert len(out["deliveries"]) == 1, "死在途动作 resume 恰好补投一次"
    assert st.get_action(wid, aid)["state"] == "delivered"


# ================================================================ ⑫ 返工4·真实受理链
async def test_continue_real_chain_acceptance(work, monkeypatch):
    """[返工4] 真实返回链：``harness.deliver(mode=continue)`` →
    ``_deliver_continue`` → ``review_runner.run_once``（真）→
    ``accepted`` → ``continue:N`` → ``deliver_accepted``。

    覆盖：``no_auto``（关自动评审）、``already_running``（评审忙）、
    ``step_error``（步内崩）⇒ ``continue:0`` 不受理；评审卡无批次
    （``no_batch`` 确定结局）⇒ 受理。"""
    from src.api import review_runner
    from src.storage import card_store, review_store, work_store
    from src.infra import harness
    wid = work["work_id"]
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被评子项目", "用户直接创建",
                                     card_type="sub_project", parent=root,
                                     skip_kick=True)
    cid = str(r["meta"]["card_id"])
    rid = await review_store.ensure_review_card(wid, cid, "被评子项目")
    assert rid

    # ① no_auto：关自动评审 ⇒ 入口闸拒收
    monkeypatch.setenv("ECO_NO_AUTO_REVIEW", "1")
    res = await harness.deliver(wid, rid, "续跑", mode="continue",
                                action_id="act-na")
    assert res == "continue:0", f"no_auto 必须拒收: {res}"
    assert not harness.deliver_accepted(res)

    # ② already_running：评审忙 ⇒ 拒收
    monkeypatch.delenv("ECO_NO_AUTO_REVIEW", raising=False)
    review_runner._RUNNING.add(f"{wid}/{rid}")
    try:
        res = await harness.deliver(wid, rid, "续跑", mode="continue",
                                    action_id="act-ar")
        assert res == "continue:0", f"already_running 必须拒收: {res}"
    finally:
        review_runner._RUNNING.discard(f"{wid}/{rid}")

    # ③ step_error：步内崩 ⇒ 拒收（重试可能改观）
    # [注意] 不调 monkeypatch.undo()——它会连沙箱夹具的 WORKS_DIR 等
    # 补丁一起回滚（monkeypatch 是函数级共享实例），库路径会指回真实
    # works 目录。只显式还原本用例自己 patch 的属性。
    orig_step = review_runner._run_step

    async def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(review_runner, "_run_step", _boom)
    res = await harness.deliver(wid, rid, "续跑", mode="continue",
                                action_id="act-se")
    assert res == "continue:0", f"step_error 必须拒收: {res}"
    monkeypatch.setattr(review_runner, "_run_step", orig_step)
    monkeypatch.delenv("ECO_NO_AUTO_REVIEW", raising=False)

    # ④ no_batch：评审无批次＝确定结局 ⇒ 受理（不烧重试）
    res = await harness.deliver(wid, rid, "续跑", mode="continue",
                                action_id="act-nb")
    assert res == "continue:1", f"no_batch 是确定结局应受理: {res}"
    assert harness.deliver_accepted(res)


async def test_step_accepted_reason_map():
    """[返工4·映射钉板] ``_step_accepted``：拒收只认
    step_error/child_busy/wake_failed/advance_no_members；到站、
    策略性唤不醒（awaiting_halted/busy/silenced）、已送达全受理。"""
    from src.api import review_runner
    acc = review_runner._step_accepted
    for reason in ("step_error", "child_busy", "wake_failed",
                   "advance_no_members"):
        assert acc({"reason": reason}) is False, reason
    for reason in ("seat_done", "dispatch_promote", "dispatch_revise",
                   "wake_delivered", "delivered_quiet", "align_passed",
                   "align_vetoed", "no_batch", "promoted",
                   "awaiting_promote", "awaiting_target", "awaiting_vote",
                   "awaiting_halted", "awaiting_busy", "awaiting_silenced",
                   "stalled", "draft_empty", "dispatch_capped"):
        assert acc({"reason": reason}) is True, reason


# ================================================================ ⑬ 第三轮返工1·过期步骤销账
async def test_stale_step_expect_blocks_execution(work, monkeypatch):
    """[第三轮返工1] rv_old 登记的 continue 动作，批次已切 rv_new ⇒
    评审属主执行前复核 {batch,draft,next}：不符 ⇒ ``stale_step``
    销账返回 ``continue:stale``，**不执行** rv_new 的步骤。"""
    from src.api import review_runner
    from src.storage import card_store, review_store, work_store
    from src.infra import harness
    wid = work["work_id"]
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被评子项目", "用户直接创建",
                                     card_type="sub_project", parent=root,
                                     skip_kick=True)
    cid = str(r["meta"]["card_id"])
    rid = await review_store.ensure_review_card(wid, cid, "被评子项目")
    assert rid
    # 当前评审批次＝rv_new（旧动作登记时是 rv_old）
    _set_review_state(wid, rid, batch="rv_new", target=cid)
    monkeypatch.delenv("ECO_NO_AUTO_REVIEW", raising=False)

    # ① 直接 run_once：expect 与现状不符 ⇒ stale_step，不进 _run_step
    r1 = await review_runner.run_once(
        wid, rid,
        expect={"batch": "rv_old", "draft": "1",
                "next": "dispatch:promote"})
    assert r1.get("stale") is True and r1["reason"] == "stale_step"
    assert r1["accepted"] is False

    # ② 真实投递链：harness.deliver ⇒ continue:stale
    res = await harness.deliver(
        wid, rid, "续跑", mode="continue", action_id="act-old",
        expect={"batch": "rv_old", "draft": "1",
                "next": "dispatch:promote"})
    assert res == "continue:stale", f"过期步骤必须销账: {res}"

    # ③ 身份匹配 ⇒ 不 stale（走正常受理链）
    res2 = await harness.deliver(
        wid, rid, "续跑", mode="continue", action_id="act-new",
        expect={"batch": "rv_new", "draft": "1", "next": ""})
    assert res2 != "continue:stale", f"匹配身份不得判过期: {res2}"


async def test_resume_stale_step_marked_stale(work):
    """[第三轮返工1] resume 重投带步骤身份的 continue 动作 ⇒ expect
    透传投递层；投递回执 ``continue:stale`` ⇒ 台账销账 ``stale``
    不再重试。"""
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:1|t",
        target="card_050", mode="continue", wake=True,
        message="（续接）评审还没结束，继续跑下一步。",
        biz_key="rv_old|1|dispatch:promote",
        step={"batch": "rv_old", "draft": "1",
              "next": "dispatch:promote"})
    assert aid
    seen = {}

    def _fn(entry):
        seen.update(entry)
        return "continue:stale"

    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1
    assert seen.get("expect") == {"batch": "rv_old", "draft": "1",
                                  "next": "dispatch:promote"}, \
        "resume 必须把登记的步骤身份传给投递层复核"
    assert st.get_action(wid, aid)["state"] == "stale", \
        "continue:stale ⇒ 销账 stale，不留重投面"
    out2 = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert out2["deliveries"] == [], "stale 后零重投"


# ================================================================ ⑭ 第三轮返工1b·旧格式事件只做诊断
async def test_old_format_event_diagnostic_only(work):
    """[第三轮返工1b] ``runnable=true`` 但**缺 ``next``** 的旧格式事件
    ⇒ 只做诊断：零投递、动作销账 ``stale``——它证明不了下一步身份，
    当前状态由评审属主/新事件另行立案。"""
    wid = work["work_id"]
    ev = _mk_event("card_050", 9001, "2026-11-20T18:00:00+08:00",
                   "review_round_done",
                   {"batch": "rv_old", "draft": 1, "seat": "fn_001",
                    "pending": ["fn_002"], "runnable": True,
                    "has_next": True, "all_passed": False,
                    "continued": True})          # 注意：无 next
    res = await replay.replay_events(wid, [ev])
    assert res["rows"][0]["verdict"] == "fired"
    assert res["deliveries"] == [], "缺 next 的旧事件不得触发投递"
    acts = _actions(wid)
    assert len(acts) == 1, "诊断账仍落动作行"
    assert acts[0]["state"] == "stale", \
        f"不可核验步骤必须销账: {acts[0]['state']}"


async def test_resume_unverifiable_step_marked_stale(work):
    """[第三轮返工1b·resume 面] 手工残留的不可核验 continue 动作
    （step.next 空）⇒ resume 销账 stale、零投递。"""
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:9|t",
        target="card_050", mode="continue", wake=True,
        message="m", biz_key="rv_d|1|",
        step={"batch": "rv_d", "draft": "1", "next": ""})
    out = await replay.resume_after_restart(wid)
    assert out["deliveries"] == [], "不可核验步骤零投递"
    assert st.get_action(wid, aid)["state"] == "stale"


# ================================================================ ⑮ 第三轮返工2·领取凭据
async def test_claim_token_fences_late_writeback(work):
    """[第三轮返工2] 领取凭据：发送者仍活着（租约内）⇒ 第二属主抢不走；
    租约过期接管 ⇒ 拿到新 token；**旧属主迟到写回被拒**，不覆盖。"""
    import time as _time
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="p", scope_key="card:c", fire_key="f|1",
        target="c", mode="message", wake=True, message="m")
    # 属主甲领取
    t1 = st.claim_action(wid, aid,
                         dead_sending_before=_time.time() - 1800)
    assert t1, "pending ⇒ 必须可领"
    # 租约内第二属主 ⇒ 抢不走（发送者仍活着）
    t2 = st.claim_action(wid, aid,
                         dead_sending_before=_time.time() - 1800)
    assert t2 is None, "在途宽限内的 sending 不许抢"
    # 属主甲退出（租约过期）⇒ 属主乙接管
    conn = st._conn(wid)
    conn.execute("UPDATE actions SET updated_ts=? WHERE action_id=?",
                 (_time.time() - 4000, aid))
    conn.commit()
    t3 = st.claim_action(wid, aid,
                         dead_sending_before=_time.time() - 1800)
    assert t3 and t3 != t1, "租约过期必须可接管且凭据不同"
    # 旧属主迟到写回 ⇒ 凭据不符拒写
    assert st.mark_action(wid, aid, "delivered",
                          receipt={"result": "late"},
                          claim_token=t1) is False
    assert st.get_action(wid, aid)["state"] == "sending"
    # 新属主写回 ⇒ 成立
    assert st.mark_action(wid, aid, "delivered",
                          receipt={"result": "ok"},
                          claim_token=t3) is True
    assert st.get_action(wid, aid)["state"] == "delivered"


async def test_resume_does_not_steal_inflight_action(work):
    """[第三轮返工2·resume 面] 第一笔投递仍在途（sending 租约内）⇒
    resume 进入同一 action_id 也**抢不走**——实际投递次数不增。"""
    import time as _time
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:7|t",
        target="card_050", mode="continue", wake=True,
        message="m", biz_key="rv_i|1|seat:a:speak",
        step={"batch": "rv_i", "draft": "1", "next": "seat:a:speak"})
    t1 = st.claim_action(wid, aid,
                         dead_sending_before=_time.time() - 1800)
    assert t1
    out = await replay.resume_after_restart(wid)
    assert out["deliveries"] == [], "在途动作 resume 不得抢投"
    assert st.get_action(wid, aid)["state"] == "sending"
    assert st.get_action(wid, aid)["claim_token"] == t1, \
        "属主凭据不许被覆盖"



# ================================================================ ⑯ 第三轮返工3·并发登记唯一编号
async def test_concurrent_record_same_action_id(work):
    """[第三轮返工3] 两个独立 SQLite 连接同时登记同业务步骤 ⇒
    唯一索引兜底：后到者撞键读回**同一个 action_id**，不发行两行。"""
    import threading
    import time as _time
    from src.infra.intervention_store import InterventionStore
    wid = work["work_id"]
    s1, s2 = InterventionStore(), InterventionStore()
    conn1 = s1._conn(wid)
    now = _time.time()
    aid1 = "act-manual001"
    # 属主甲：写事务内插入同 biz 行但**暂不提交**（模拟并发窗口）
    conn1.execute("BEGIN IMMEDIATE")
    conn1.execute(
        "INSERT INTO actions(action_id,matter_id,policy_id,scope_key,"
        "fire_key,biz_key,step_json,target,mode,wake,message,spec,state,"
        "created_ts,updated_ts,created_iso,updated_iso)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
        (aid1, "", "p", "card:c", "fk-1", "BIZ-RACE", "",
         "c", "message", 1, "m", "", now, now, "", ""))

    def _commit_soon():
        _time.sleep(0.3)
        conn1.commit()

    threading.Thread(target=_commit_soon, daemon=True).start()
    # 属主乙：同业务步骤、不同 fire_key ⇒ SELECT 查无（甲未提交）
    # ⇒ INSERT 撞 ux_actions_biz ⇒ IntegrityError ⇒ 读回同一编号
    aid2, created2 = s2.record_action(
        wid, policy_id="p", scope_key="card:c", fire_key="fk-2",
        target="c", mode="message", wake=True, message="m",
        biz_key="BIZ-RACE")
    assert aid2 == aid1 and created2 is False, \
        f"并发登记必须收敛到同一编号: {aid2} vs {aid1}"
    rows = s1._conn(wid).execute(
        "SELECT action_id FROM actions WHERE biz_key='BIZ-RACE'"
    ).fetchall()
    assert len(rows) == 1, "业务键唯一性：只允许一行"


async def test_record_action_step_persisted(work):
    """[第三轮返工1] ``step`` 结构化身份随动作落账（step_json），
    投递/恢复凭它向评审属主复核。"""
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="p", scope_key="card:c", fire_key="f|s",
        target="c", mode="continue", wake=True, message="m",
        biz_key="b|1|n", step={"batch": "b", "draft": "1", "next": "n"})
    row = st.get_action(wid, aid)
    assert row["step_json"] == {"batch": "b", "draft": "1", "next": "n"}
    assert intervention_runner.step_expect(row) == {
        "batch": "b", "draft": "1", "next": "n"}
    assert intervention_runner.step_unverifiable(row) is False
