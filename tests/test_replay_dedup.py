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
import asyncio
import json
import sqlite3
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


# ================================================================ ⑰ 第四轮返工1·expect 贯穿属主入口
async def test_ctl_review_step_passes_expect(work, monkeypatch):
    """[第四轮返工1] 属主接口 ``/ctl/review_step`` 必须把 ``expect``
    透传给 ``run_once``——跨 runner 转发在边界不许丢步骤身份。"""
    from src.api import routes, review_runner
    got: dict = {}

    async def _once(wid, rid, *, hint="", expect=None):
        got.update(expect=expect, hint=hint)
        return {"continued": True, "reason": "spoken", "accepted": True}

    monkeypatch.setattr(review_runner, "run_once", _once)
    req = routes.CtlReviewStepRequest(
        work_id="w", review_id="card_rv", hint="h",
        expect={"batch": "rv_b", "draft": "2", "next": "seat:a:speak"})
    await routes.ctl_review_step(req)
    assert got["expect"] == {"batch": "rv_b", "draft": "2",
                             "next": "seat:a:speak"}, \
        "属主接口必须接住并透传 expect"
    assert got["hint"] == "h"


async def test_run_once_forwards_expect_to_owner(work, monkeypatch):
    """[第四轮返工1] 非属主进程转发时，``expect`` 必须进请求体——
    属主拿不到身份就无法复核。"""
    from src.api import review_runner
    from src.infra import procs
    got: dict = {}

    async def _fwd(wid, rid, payload):
        got.update(payload)
        return {"continued": True, "reason": "spoken", "accepted": True}

    monkeypatch.setattr(procs, "is_web", lambda: True)
    monkeypatch.setattr(procs, "forward_review_step", _fwd)
    monkeypatch.delenv("ECO_NO_AUTO_REVIEW", raising=False)   # 沙箱默认=1 会入口闸短路
    await review_runner.run_once(
        "w", "card_rv", hint="h",
        expect={"batch": "rv_b", "draft": "2", "next": "dispatch:promote"})
    assert got.get("expect") == {"batch": "rv_b", "draft": "2",
                                 "next": "dispatch:promote"}, \
        "转发请求体必须携带 expect"
    assert got.get("hint") == "h"


async def test_expect_verified_on_execution_snapshot(work, monkeypatch):
    """[第四轮返工1·TOCTOU] 身份复核与实际执行用**同一份状态快照**：
    ``run_once`` 不再先跑 ``_step_identity`` 预检再让 ``_run_step``
    重读——「查 A 执行 B」的窗口在结构上不存在；换批 ⇒
    ``stale_step`` 销账、dispatch 不跑。"""
    from src.api import review_runner
    from src.storage import card_store, review_store, work_store
    wid = work["work_id"]
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被评子项目", "用户直接创建",
                                     card_type="sub_project", parent=root,
                                     skip_kick=True)
    cid = str(r["meta"]["card_id"])
    rid = await review_store.ensure_review_card(wid, cid, "被评子项目")
    assert rid
    _set_review_state(wid, rid, batch="rv_new", target=cid)
    monkeypatch.delenv("ECO_NO_AUTO_REVIEW", raising=False)

    # 预检探针：新代码里 run_once 不得再调 _step_identity 做第二次读
    ident_calls: list = []
    real_ident = review_runner._step_identity

    async def _ident_probe(w, t):
        ident_calls.append((w, t))
        return await real_ident(w, t)

    monkeypatch.setattr(review_runner, "_step_identity", _ident_probe)
    dispatched: list = []

    async def _disp(w, r_, st, *, kind):
        dispatched.append((kind, str((st or {}).get("batch") or "")))
        return True

    monkeypatch.setattr(review_runner, "_dispatch_child", _disp)
    real_classify = review_runner._classify

    # ① 过期 expect ⇒ stale 销账；全程零次 _step_identity 预检读
    r1 = await review_runner.run_once(
        wid, rid,
        expect={"batch": "rv_old", "draft": "1",
                "next": "dispatch:promote"})
    assert r1.get("stale") is True and r1["reason"] == "stale_step"
    assert ident_calls == [], \
        "复核必须绑在 _run_step 的执行快照上，不得另起预检读"
    assert dispatched == [], "过期步骤一个动作都不许跑"

    # ② 换批时序：state_of 首读 rv_old（匹配 expect）随后翻 rv_new——
    #    前后读到不同批次时，旧步骤销账，不用混合状态执行。
    flips = {"n": 0}
    real_state_of = review_store.state_of

    async def _flip(w, rid_):
        flips["n"] += 1
        if flips["n"] == 1:
            return {"batch": "rv_old", "target": cid}
        return {"batch": "rv_new", "target": cid}

    monkeypatch.setattr(review_store, "state_of", _flip)
    monkeypatch.setattr(review_runner, "_classify",
                        lambda *a, **k: (("dispatch", "promote"), ""))
    r2 = await review_runner.run_once(
        wid, rid,
        expect={"batch": "rv_old", "draft": "1",
                "next": "dispatch:promote"})
    assert r2.get("stale") is True and r2.get("reason") == "stale_step", r2
    assert dispatched == []

    # ③ 匹配现状 ⇒ 不 stale（还原真实分类与状态读）
    monkeypatch.setattr(review_store, "state_of", real_state_of)
    monkeypatch.setattr(review_runner, "_classify", real_classify)
    r3 = await review_runner.run_once(
        wid, rid,
        expect={"batch": "rv_new", "draft": "1", "next": ""})
    assert r3.get("stale") is not True, f"匹配身份不得判过期: {r3}"


async def test_resume_unverifiable_step_refreshes_current(work, monkeypatch):
    """[第四轮返工1b] 无 ``step_json`` 的旧 continue 账（契约策略）
    ⇒ resume **重读当前状态**：现状有可跑下一步 ⇒ 以现状身份
    ``expect`` 投递；没有 ⇒ 销账 stale。"""
    from src.api import review_runner
    wid = work["work_id"]
    st = intervention_store.store()
    # 契约策略（review_step_not_finished 有 dedup_fields）但登记时
    # 无 step ⇒ step_json='' —— 登记早于 step 列的旧账形态。
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:11|t",
        target="card_050", mode="continue", wake=True,
        message="（续接）评审还没结束，继续跑下一步。",
        biz_key="legacy|1|")
    assert aid
    seen: dict = {}

    async def _ident(w, t):
        # 「当前状态」：批次已是 rv_now、下一步是叫 fn_009 发言
        return {"batch": "rv_now", "draft": "2",
                "next": "seat:fn_009:speak"}

    monkeypatch.setattr(review_runner, "_step_identity", _ident)

    def _fn(entry):
        seen.update(entry)
        return "continue:1"

    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1, \
        "现状有可跑步 ⇒ 恢复入口重读后应投递"
    assert seen.get("expect") == {"batch": "rv_now", "draft": "2",
                                  "next": "seat:fn_009:speak"}, \
        "expect 必须是重读出的**现状**身份，不是旧账猜的"
    assert st.get_action(wid, aid)["state"] == "delivered"


async def test_resume_unverifiable_step_stale_when_idle(work):
    """[第四轮返工1b·反例] 重读当前状态**没有**可跑下一步 ⇒
    销账 stale、零投递（不拿旧身份硬发）。"""
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:12|t",
        target="card_050", mode="continue", wake=True,
        message="m", biz_key="legacy2|1|")
    # card_050 不是评审卡 ⇒ _step_identity 现状 next='' ⇒ refresh None
    out = await replay.resume_after_restart(wid)
    assert out["deliveries"] == []
    assert st.get_action(wid, aid)["state"] == "stale"


async def test_non_contract_continue_policy_not_blocked(work):
    """[第四轮返工1b·回归] 未声明 ``dedup_fields`` 的 continue 策略
    本无步骤身份概念 ⇒ 无 step_json **不算**不可核验，照常投递。"""
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_all_passed_need_promote",   # 无 dedup_fields
        scope_key="card:card_001", fire_key="p|card:card_001|c:13|t",
        target="card_001", mode="continue", wake=True,
        message="m", biz_key="")
    seen: dict = {}

    def _fn(entry):
        seen.update(entry)
        return "continue:1"

    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1, \
        "无契约策略的 continue 不得被步骤核验误拦"
    assert seen.get("expect") is None


# ================================================================ ⑱ 第四轮返工2·在途续约
async def test_live_sender_lease_blocks_takeover(work):
    """[第四轮返工2] 活发送者在途 ⇒ resume **不得抢投**：心跳让
    ``sending`` 行保鲜，「租约超时」不等于「发送者退出」。
    验收分别数**真实发送**与**台账写回**。"""
    import asyncio
    import time as _time
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:5|t",
        target="card_050", mode="continue", wake=True,
        message="m", biz_key="rv_l|1|seat:a:speak",
        step={"batch": "rv_l", "draft": "1", "next": "seat:a:speak"})
    token = st.claim_action(wid, aid,
                            dead_sending_before=_time.time() - 1800)
    assert token
    sends: list = []

    async def _slow_sender():
        # 模拟第一笔投递：领取后长时间在途（远超"租约"），期间续约
        async with intervention_runner.claim_lease(
                wid, aid, token, interval=0.05):
            await asyncio.sleep(0.4)
            sends.append("send1")
            return "continue:1"

    task = asyncio.ensure_future(_slow_sender())
    await asyncio.sleep(0.15)                     # 让心跳先跑起来
    # 把行拨旧（模拟租约时间推进/第二 runner 启动）——心跳下个间隔
    # 会把 updated_ts 写回 now，resume 看到的是"活人"不是"死账"
    conn = st._conn(wid)
    conn.execute("UPDATE actions SET updated_ts=? WHERE action_id=?",
                 (_time.time() - 99999, aid))
    conn.commit()
    await asyncio.sleep(0.15)                     # 等至少一次心跳写回
    out = await replay.resume_after_restart(wid)
    assert out["deliveries"] == [], \
        "活发送者在途 resume 不得抢投（真实发送次数不得增加）"
    await task
    assert sends == ["send1"], "第一笔投递只真实发送一次"
    # 发送者持凭据写回 ⇒ delivered
    assert st.mark_action(wid, aid, "delivered",
                          claim_token=token) is True
    assert st.get_action(wid, aid)["state"] == "delivered"


async def test_dead_sender_lease_allows_takeover(work):
    """[第四轮返工2·对照] 发送者**退出**（无心跳）⇒ 租约过期可接管
    重投一次；旧属主迟到写回被凭据拒写。"""
    import time as _time
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:6|t",
        target="card_050", mode="continue", wake=True,
        message="m", biz_key="rv_d|1|seat:a:speak",
        step={"batch": "rv_d", "draft": "1", "next": "seat:a:speak"})
    t1 = st.claim_action(wid, aid,
                         dead_sending_before=_time.time() - 1800)
    assert t1
    # 发送者退出：不开 claim_lease（无心跳）⇒ 手动拨旧模拟租约过期
    conn = st._conn(wid)
    conn.execute("UPDATE actions SET updated_ts=? WHERE action_id=?",
                 (_time.time() - 99999, aid))
    conn.commit()
    sends: list = []

    def _fn(entry):
        sends.append(entry)
        return "continue:1"

    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1, "确认退出后 resume 恰好补投一次"
    assert len(sends) == 1
    # 接管者写回 delivered；旧属主迟到写回被拒
    row = st.get_action(wid, aid)
    assert row["state"] == "delivered"
    assert st.mark_action(wid, aid, "stale",
                          claim_token=t1) is False, \
        "旧属主凭据不得覆盖新属主的回执"
    assert st.get_action(wid, aid)["state"] == "delivered"


async def test_touch_claim_fenced_by_token(work):
    """[第四轮返工2·单元] ``touch_claim`` 只认当前凭据：错 token/
    非 sending 态一律 False。"""
    import time as _time
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="p", scope_key="card:c", fire_key="f|1",
        target="c", mode="message", wake=True, message="m")
    assert st.touch_claim(wid, aid, "clm-nope") is False
    t = st.claim_action(wid, aid,
                        dead_sending_before=_time.time() - 1800)
    assert t
    assert st.touch_claim(wid, aid, t) is True
    assert st.touch_claim(wid, aid, "clm-other") is False


# ================================================================ ⑲ 第四轮返工3·存量迁移
async def test_migration_releases_biz_key_and_builds_index(work):
    """[第四轮返工3] 存量库有**重复 biz 行** ⇒ 迁移在同一事务内收编：
    败选行释键（``biz_key=''``）保审计 ⇒ ``ux_actions_biz`` 真建成
    ——只标 stale 不释键，部分索引照样撞键建不起来。"""
    import sqlite3
    import time as _time
    from src.infra.intervention_store import InterventionStore
    # 独立 work_id——不碰 work_001 已建的 harness.db（表已存在会撞）
    wid = "work_mig_rt"
    s0 = InterventionStore()
    path = s0.db_path(wid)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    # 模拟升级前库：actions 表已带 biz_key 列，但还没有 ux_actions_biz
    conn.execute(
        "CREATE TABLE actions("
        " action_id TEXT PRIMARY KEY, matter_id TEXT NOT NULL DEFAULT '',"
        " policy_id TEXT NOT NULL, scope_key TEXT NOT NULL DEFAULT '',"
        " fire_key TEXT NOT NULL, biz_key TEXT NOT NULL DEFAULT '',"
        " step_json TEXT NOT NULL DEFAULT '',"
        " claim_token TEXT NOT NULL DEFAULT '',"
        " target TEXT NOT NULL DEFAULT '',"
        " mode TEXT NOT NULL DEFAULT 'message',"
        " wake INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',"
        " spec TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',"
        " receipt TEXT NOT NULL DEFAULT '{}', created_ts REAL, updated_ts REAL,"
        " created_iso TEXT NOT NULL DEFAULT '',"
        " updated_iso TEXT NOT NULL DEFAULT '')")
    now = _time.time()
    cols = ("action_id,policy_id,scope_key,fire_key,biz_key,target,mode,"
            "state,created_ts,updated_ts")
    # 同业务步骤两行：delivered（权威）vs pending（败选）
    conn.execute(
        f"INSERT INTO actions({cols}) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("act-keep", "p", "card:c", "fk-1", "BIZDUP", "c", "continue",
         "delivered", now - 10, now - 10))
    conn.execute(
        f"INSERT INTO actions({cols}) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("act-drop", "p", "card:c", "fk-2", "BIZDUP", "c", "continue",
         "pending", now - 5, now - 5))
    conn.commit()
    conn.close()

    # 新连接 ⇒ _conn 迁移：收编 + 建索引 + 核验
    s1 = InterventionStore()
    c = s1._conn(wid)
    idx = c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND name='ux_actions_biz'").fetchone()
    assert idx, "重复行释键后 ux_actions_biz 必须真实建成"
    keep = dict(c.execute(
        "SELECT biz_key,state FROM actions WHERE action_id='act-keep'"
    ).fetchone())
    drop = dict(c.execute(
        "SELECT biz_key,state,receipt FROM actions WHERE action_id='act-drop'"
    ).fetchone())
    assert keep["biz_key"] == "BIZDUP" and keep["state"] == "delivered", \
        "权威行（delivered）保留业务键"
    assert drop["biz_key"] == "", "败选行必须释键退出部分索引"
    assert drop["state"] == "stale"
    assert "BIZDUP" in str(drop.get("receipt") or ""), \
        "审计记录必须保留原 biz_key（receipt.biz_dedup）"


# ================================================================ ⑳ 第五轮返工1·派活写回 CAS
async def test_dispatch_writeback_fenced_by_batch(work, monkeypatch):
    """[第五轮返工1] 派活落信期间换批：``append_card_message`` 后批次
    rv_old→rv_new，写回携带快照身份 {batch,draft} 在**同一写事务内**
    核对——不符 ⇒ 不写、``_dispatch_child`` 返回 ``"stale"``，新批的
    ``promote_asked`` 保持 False（不谎报成送达）。"""
    from src.api import review_runner
    from src.storage import (card_store, review_store, session_facade,
                             work_store)
    wid = work["work_id"]
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被评子项目", "用户直接创建",
                                     card_type="sub_project", parent=root,
                                     skip_kick=True)
    cid = str(r["meta"]["card_id"])
    rid = await review_store.ensure_review_card(wid, cid, "被评子项目")
    assert rid
    # 派活时刻的快照：rv_old / draft 3
    _set_review_state(wid, rid, batch="rv_old", draft=3, target=cid)
    snapshot = {"batch": "rv_old", "draft": 3, "target": cid}

    real_append = session_facade.append_card_message

    async def _append_then_flip(*a, **k):
        rec = await real_append(*a, **k)
        assert rec, "信先落库"
        # 信已落、写回未发——此刻批次换人
        _set_review_state(wid, rid, batch="rv_new", draft=1,
                          target=cid)
        return rec

    monkeypatch.setattr(session_facade, "append_card_message",
                        _append_then_flip)
    out = await review_runner._dispatch_child(wid, rid, snapshot,
                                              kind="promote")
    assert out == "stale", \
        f"写回被 CAS 拒必须返回可区分结果 stale，不是成功: {out!r}"
    new_st = await review_store.state_of(wid, rid)
    assert new_st.get("batch") == "rv_new"
    assert new_st.get("promote_asked") in (False, None), \
        "旧批次的写回不得把新批次 promote_asked 置真"
    assert not new_st.get("promote_asked_seq"), \
        "旧批次的锚不得落进新批"

    # 正例：身份相符 ⇒ 正常写回 True
    ok = await review_store.mark_promote_asked(
        wid, rid, at_seq=9,
        expect={"batch": "rv_new", "draft": 1})
    assert ok is True
    st2 = await review_store.state_of(wid, rid)
    assert st2.get("promote_asked") is True \
        and st2.get("promote_asked_seq") == 9


async def test_mark_asked_writeback_fenced(work):
    """[第五轮返工1·revise 对称] ``mark_asked`` 同一条 CAS 通道——
    快照身份不符 ⇒ 不写；相符 ⇒ 写。"""
    from src.storage import card_store, review_store, work_store
    wid = work["work_id"]
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被评子项目", "用户直接创建",
                                     card_type="sub_project", parent=root,
                                     skip_kick=True)
    cid = str(r["meta"]["card_id"])
    rid = await review_store.ensure_review_card(wid, cid, "被评子项目")
    assert rid
    _set_review_state(wid, rid, batch="rv_b", draft=2,
                      responded_seq=4)
    ok = await review_store.mark_asked(
        wid, rid, at_seq=33, expect={"batch": "rv_a", "draft": 2})
    assert ok is False, "批次不符必须拒写"
    st = await review_store.state_of(wid, rid)
    assert st.get("asked_seq") in (None, 0), \
        "拒写不得改 asked_seq"
    ok2 = await review_store.mark_asked(
        wid, rid, at_seq=33, expect={"batch": "rv_b", "draft": 2})
    assert ok2 is True
    st = await review_store.state_of(wid, rid)
    assert st.get("asked_seq") == 5 and st.get("asked_at_seq") == 33


# ================================================================ ㉑ 第五轮返工2·旧账归并
async def test_resume_refresh_merges_with_current_action(work,
                                                         monkeypatch):
    """[第五轮返工2] 无身份旧账 + 已登记当前动作**同一** {batch,draft,
    next}：resume 刷新旧账到现状身份后按当前业务键并入既有
    ``action_id``——两条记录只产生**一次**真实投递，旧行
    ``superseded_by`` 审计留痕。"""
    from src.api import review_runner
    wid = work["work_id"]
    st = intervention_store.store()
    cur_ident = {"batch": "rv_cur", "draft": "2",
                 "next": "seat:fn_009:speak"}
    biz = "rv_cur|2|seat:fn_009:speak"
    # 旧账：登记早于 step 列（biz 旧、step_json 空）
    old_aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:19|t",
        target="card_050", mode="continue", wake=True,
        message="old", biz_key="legacy|1|")
    # 当前动作：同业务步骤已登记在案（不同 action_id）
    cur_aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:20|t",
        target="card_050", mode="continue", wake=True,
        message="cur", biz_key=biz, step=cur_ident)
    assert old_aid and cur_aid and old_aid != cur_aid

    async def _ident(w, t):
        return dict(cur_ident)
    monkeypatch.setattr(review_runner, "_step_identity", _ident)

    sends: list = []

    def _fn(entry):
        sends.append(entry)
        return "continue:1"

    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1, \
        "同键两条记录只能产生一次真实投递"
    assert len(sends) == 1
    old_row = st.get_action(wid, old_aid)
    cur_row = st.get_action(wid, cur_aid)
    assert cur_row["state"] == "delivered", \
        "当前动作负责这一次投递"
    assert old_row["state"] == "stale", "旧账并入，不得独立投递"
    rec = old_row.get("receipt") or {}
    assert rec.get("reason") == "superseded_by" \
        and rec.get("kept") == cur_aid, \
        "并入审计要指向被保留的 action_id"


async def test_resume_refresh_rekeys_lone_legacy(work, monkeypatch):
    """[第五轮返工2·登记路] 无身份旧账刷新后**无**同键动作 ⇒
    ``rekey`` 把现状身份（biz_key+step_json）登记回本行再投——
    后续恢复按现键查得到它，不会再造第二行。"""
    from src.api import review_runner
    wid = work["work_id"]
    st = intervention_store.store()
    cur_ident = {"batch": "rv_solo", "draft": "1",
                 "next": "seat:x:vote"}
    biz = "rv_solo|1|seat:x:vote"
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:21|t",
        target="card_050", mode="continue", wake=True,
        message="old", biz_key="legacy|9|")
    assert aid

    async def _ident(w, t):
        return dict(cur_ident)
    monkeypatch.setattr(review_runner, "_step_identity", _ident)

    sends: list = []

    def _fn(entry):
        sends.append(entry)
        return "continue:1"

    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1 and len(sends) == 1
    row = st.get_action(wid, aid)
    assert row["state"] == "delivered"
    assert row["biz_key"] == biz, \
        "刷新后应把现状业务键登记回本行"
    assert (row.get("step_json") or {}).get("next") == "seat:x:vote"


# ================================================================ ㉒ 第五轮返工3·心跳重试
async def test_heartbeat_survives_transient_lock(work, monkeypatch):
    """[第五轮返工3] 一次 ``database is locked`` 不把心跳打死：
    ``touch_claim`` 三态区分——瞬时错误 None（下轮再来）、凭据失效
    False（停止）。心跳恢复续约 ⇒ 活发送者在途 resume 不抢；
    发送者退出+租约过期 ⇒ resume 恰补投一次；错 token ⇒ False。"""
    import asyncio
    import sqlite3
    import time as _time
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:30|t",
        target="card_050", mode="continue", wake=True,
        message="m", biz_key="rv_h|1|seat:a:speak",
        step={"batch": "rv_h", "draft": "1", "next": "seat:a:speak"})
    token = st.claim_action(
        wid, aid, dead_sending_before=_time.time() - 1800)
    assert token
    st._conn(wid)   # 温主连接——注入期内新连接只可能是 touch_claim

    # 注入恰好一次连接级 locked（touch_claim 的独立短连接），其后放行
    real_connect = sqlite3.connect
    calls = {"n": 0}

    def _flaky_connect(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*a, **k)

    # 单测层三态核对：瞬时错误 None／凭据失效 False
    monkeypatch.setattr(sqlite3, "connect", _flaky_connect)
    assert st.touch_claim(wid, aid, token) is None, \
        "锁冲突必须返回可重试 None，不是凭据失效 False"
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert st.touch_claim(wid, aid, token) is True
    assert st.touch_claim(wid, aid, "clm-bogus") is False, \
        "错凭据必须返回 False（停止续约），不是可重试"

    # 心跳循环：首个 beat 撞锁 ⇒ 续约恢复 ⇒ 活发送者不被抢
    calls["n"] = 0
    monkeypatch.setattr(sqlite3, "connect", _flaky_connect)
    sends: list = []

    async def _slow_sender():
        async with intervention_runner.claim_lease(
                wid, aid, token, interval=0.05):
            await asyncio.sleep(0.35)
            sends.append("send1")
            return "continue:1"

    task = asyncio.ensure_future(_slow_sender())
    await asyncio.sleep(0.2)   # ≥1 次锁败 + ≥1 次续约成功
    assert calls["n"] >= 2, "心跳必须在锁败后继续跑"
    out = await replay.resume_after_restart(wid)
    assert out["deliveries"] == [], \
        "心跳恢复后行仍新鲜，resume 不得抢活在途的发送者"
    await task
    assert sends == ["send1"], "原发送者只真实发送一次"

    # 发送者退出（租约出作用域，心跳停）+ 拨旧 ⇒ 可接管补投一次
    conn = st._conn(wid)
    conn.execute("UPDATE actions SET updated_ts=? WHERE action_id=?",
                 (_time.time() - 99999, aid))
    conn.commit()
    sends2: list = []

    def _fn2(entry):
        sends2.append(entry)
        return "continue:1"

    out2 = await replay.resume_after_restart(wid, deliver_fn=_fn2)
    assert len(out2["deliveries"]) == 1 and len(sends2) == 1, \
        "确认退出后恰好补投一次"
    assert st.get_action(wid, aid)["state"] == "delivered"


# ================================================================ ㉓ 第六轮返工1·收件属主核批
async def test_stale_directive_no_wake(work, monkeypatch):
    """[第六轮返工1] 派活写回成功后、唤醒前换批：``deliver_now`` 对
    ``review_dispatch`` 的批内指令（promote/revise/wake_*）按评审卡
    **当前** {batch,draft} 复核——过期 ⇒ 不起轮（指令块只留历史）；
    身份相符 ⇒ 正常起轮；终局通知（veto）不核。"""
    import asyncio
    from src.api import message_runner, routes
    from src.storage import card_store, review_store, work_store
    wid = work["work_id"]
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被评子项目", "用户直接创建",
                                     card_type="sub_project", parent=root,
                                     skip_kick=True)
    cid = str(r["meta"]["card_id"])
    rid = await review_store.ensure_review_card(wid, cid, "被评子项目")
    assert rid
    src = {"kind": "review_dispatch", "from": rid,
           "batch": "rv_old", "draft": 1, "step": "promote"}

    spawned: list = []

    async def _fake_run(*a, **k):
        spawned.append((a, k))

    monkeypatch.setattr(routes, "_run_message", _fake_run)

    # 唤醒前换批 ⇒ 属主复核不通过 ⇒ 不起轮
    _set_review_state(wid, rid, batch="rv_new", draft=1, target=cid)
    ok = await message_runner.deliver_now(wid, cid, text="请调 promote_draft",
                                          source=src)
    assert ok is False, "过期指令不得起轮"
    assert spawned == []

    # 身份相符 ⇒ 起轮一次
    _set_review_state(wid, rid, batch="rv_old", draft=1, target=cid)
    ok = await message_runner.deliver_now(wid, cid, text="请调 promote_draft",
                                          source=src)
    assert ok is True
    await asyncio.sleep(0.05)
    assert len(spawned) == 1

    # 终局通知（veto）不核批——批已终局没有可对照的"当前批次"
    _set_review_state(wid, rid, batch="", target=cid)
    ok = await message_runner.deliver_now(
        wid, cid, text="终局通知",
        source={"kind": "review_dispatch", "from": rid,
                "batch": "rv_old", "step": "veto"})
    assert ok is True, "终局通知不受批内身份闸拦截"
    await asyncio.sleep(0.05)
    assert len(spawned) == 2


@pytest.fixture
async def review_dispatch_target(work, monkeypatch):
    from src.storage import card_store, review_store, work_store

    wid = work["work_id"]
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "被评子项目", "用户直接创建",
                                     card_type="sub_project", parent=root,
                                     skip_kick=True)
    target = str(r["meta"]["card_id"])
    rid = await review_store.ensure_review_card(wid, target, "被评子项目")
    assert rid
    _set_review_state(wid, rid, batch="rv_old", draft=1, target=target)
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    return wid, target, {
        "kind": "review_dispatch", "from": rid, "batch": "rv_old",
        "draft": 1, "step": "promote",
    }


@pytest.mark.parametrize("change, expected", [("batch", 409), ("read_error", 503)])
async def test_dispatch_rechecks_after_waiting_for_turn(
        review_dispatch_target, monkeypatch, change, expected):
    from src.api import message_runner, model_service, routes
    from src.storage import card_db, session_facade

    wid, target, source = review_dispatch_target
    entered, resume = asyncio.Event(), asyncio.Event()
    tasks, model_requests = [], []
    acquire = routes.serial_gate.acquire
    run_read = card_db.run_read
    log = message_runner.session_ops.ops_log
    decisions = []

    async def pause_in_serial_gate(work_id, card_id):
        entered.set()
        await resume.wait()
        await acquire(work_id, card_id)

    async def block_review_state_read(work_id, fn, default=None):
        if fn.__qualname__.startswith("state_of."):
            raise sqlite3.OperationalError("database is locked")
        return await run_read(work_id, fn, default)

    def spawn(coro, **_):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    async def model(**kw):
        model_requests.append(kw)
        return {"content": "收到", "tool_calls": []}, "fake"

    def log_decision(action, work_id, card_id, details=None):
        decisions.append((action, details))
        return log(action, work_id, card_id, details)

    monkeypatch.setattr(routes.serial_gate, "acquire", pause_in_serial_gate)
    monkeypatch.setattr(message_runner, "_spawn_tracked", spawn)
    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.setattr(message_runner.session_ops, "ops_log", log_decision)
    assert await message_runner.deliver_now(wid, target, text="请调 promote_draft",
                                            source=source)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if change == "batch":
            _set_review_state(wid, source["from"], batch="rv_new", draft=1,
                              target=target)
        else:
            monkeypatch.setattr(card_db, "run_read", block_review_state_read)
    finally:
        resume.set()
    response = await asyncio.wait_for(tasks[0], timeout=10)
    assert response.status_code == expected
    assert model_requests == []
    assert not session_facade.turn_holder(wid, target)
    assert (wid, target) not in routes.control._turn_tasks
    if change == "read_error":
        assert any(action == "review_dispatch_unverified"
                   and details["phase"] == "turn" for action, details in decisions)
        # 同一真实指令在恢复读取后正常进入模型，证明未核验不是过期。
        monkeypatch.setattr(card_db, "run_read", run_read)
        assert await message_runner.deliver_now(
            wid, target, text="请调 promote_draft", source=source)
        await asyncio.wait_for(tasks[1], timeout=15)
        assert len(model_requests) == 1


@pytest.mark.parametrize("draft, expected_calls", [(1, 2), (2, 1)])
async def test_busy_dispatch_rechecks_before_steer(
        review_dispatch_target, monkeypatch, draft, expected_calls):
    from src.api import message_runner, model_service, routes

    wid, target, source = review_dispatch_target
    entered, resume = asyncio.Event(), asyncio.Event()
    requests = []

    async def model(**kw):
        requests.append(kw)
        if len(requests) == 1:
            entered.set()
            await resume.wait()
        return {"content": "收到", "tool_calls": []}, "fake"

    monkeypatch.setattr(model_service, "call_main", model)
    task = asyncio.create_task(routes._run_message(wid, target, "现有用户任务"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert await message_runner.deliver_now(
            wid, target, text="请调 promote_draft", source=source)
        _set_review_state(wid, source["from"], batch="rv_old", draft=draft,
                          target=target)
    finally:
        resume.set()
    await asyncio.wait_for(task, timeout=15)
    assert len(requests) == expected_calls
    if expected_calls == 2:
        assert "请调 promote_draft" in str(requests[1]["prior_messages"])


# ================================================================ ㉔ 第六轮返工2·重锚不续约
async def test_resume_rekeys_dead_sending_once(work, monkeypatch):
    """[第六轮返工2] 已过期、无 step_json 的 ``sending`` 旧账：刷新
    重锚只改**业务身份**不碰 ``updated_ts`` ⇒ 同一次 resume 里租约
    判据仍看到死账 ⇒ 领取成功、恰续一次；动作不再挂 sending。"""
    import time as _time
    from src.api import review_runner
    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="p|card:card_050|c:40|t",
        target="card_050", mode="continue", wake=True,
        message="old", biz_key="legacy|1|")          # 无 step_json
    # 模拟死掉的发送者留下的在途行：sending + 过期租约 + 旧凭据
    conn = st._conn(wid)
    conn.execute(
        "UPDATE actions SET state='sending', claim_token='clm-dead',"
        " updated_ts=? WHERE action_id=?",
        (_time.time() - 99999, aid))
    conn.commit()

    async def _ident(w, t):
        return {"batch": "rv_now", "draft": "1", "next": "seat:x:speak"}
    monkeypatch.setattr(review_runner, "_step_identity", _ident)

    sends: list = []

    def _fn(entry):
        sends.append(entry)
        return "continue:1"

    out = await replay.resume_after_restart(wid, deliver_fn=_fn)
    assert len(out["deliveries"]) == 1 and len(sends) == 1, \
        "死 sending 旧账重锚后必须能在同一次恢复里接续一次"
    row = st.get_action(wid, aid)
    assert row["state"] == "delivered", "不得再挂 sending"
    assert row["biz_key"] == "rv_now|1|seat:x:speak", \
        "业务身份已重锚到现状键"


# ================================================================ ㉕ 第六轮返工3·回应水位入身份
def _re_eval_classify(responded: int, phase: str):
    """同一否决席在读到新回应前重评，重评后仍否决则请求下一次修订。"""
    from src.api import review_runner
    st = {"batch": "rv_r", "draft": 2, "target": "card_050",
          "responded_seq": responded, "asked_seq": responded,
          "seats": {"fn_B": {"response_seen": 0 if phase == "re_eval" else responded}}}
    st_now = {"batch": "rv_r", "draft": 2, "members": ["fn_B"],
              "votes": {"fn_B": {"verdict": "reject", "draft_ver": 2}},
              "all_passed": False, "stale": []}
    action, _ = review_runner._classify(
        st, st_now, {"fn_B"}, promoted=False, tail_seq=None,
        has_draft=True)
    return review_runner._next_tag(action)


@pytest.mark.parametrize("phase, expected", [
    ("re_eval", ("seat:fn_B:re_eval:1", "seat:fn_B:re_eval:2")),
    ("revise", ("dispatch:revise:2", "dispatch:revise:3")),
])
async def test_distinct_responses_can_enable_same_step(work, phase, expected):
    """同稿的两次有效回应各接力一次；同一水位重放零新增。"""
    tag1 = _re_eval_classify(1, phase)
    tag2 = _re_eval_classify(2, phase)
    assert (tag1, tag2) == expected
    assert tag1 != tag2, "两次有效回应必须产出不同步骤身份"

    wid = work["work_id"]
    step1 = {"batch": "rv_r", "draft": 2, "next": tag1,
             "runnable": True, "has_next": True,
             "all_passed": False, "continued": True}
    step2 = dict(step1); step2["next"] = tag2
    res = await replay.replay_events(wid, [
        _mk_event("card_050", 7001, "2026-11-20T11:00:00+08:00",
                  "review_round_done", step1),
        _mk_event("card_050", 7002, "2026-11-20T11:00:01+08:00",
                  "review_round_done", step1),   # 同水位重放
        _mk_event("card_050", 7003, "2026-11-20T11:00:02+08:00",
                  "review_round_done", step2)])  # 第二次有效回应
    acts = _actions(wid)
    assert len(acts) == 2, "两个不同水位各一条动作行"
    assert len(res["deliveries"]) == 2, \
        "同一水位重放零新增、第二次有效回应恰各接一次"
    assert res["deliveries"][0]["action_id"] \
        != res["deliveries"][1]["action_id"]
    assert res["rows"][1]["deliveries_new"] == 0, \
        "同一回应的重放不得新增投递"


@pytest.mark.parametrize("batch, delivered", [("rv_old", True), ("rv_new", False)])
async def test_resume_legacy_revise_only_within_original_draft(
        work, monkeypatch, batch, delivered):
    from src.api import review_runner

    wid = work["work_id"]
    st = intervention_store.store()
    aid, _ = st.record_action(
        wid, policy_id="review_step_not_finished",
        scope_key="card:card_050", fire_key="old-revise", target="card_050",
        mode="continue", wake=True, message="old",
        biz_key="rv_old|2|dispatch:revise",
        step={"batch": "rv_old", "draft": "2", "next": "dispatch:revise"})

    async def current_step(work_id, target):
        return {"batch": batch, "draft": "2", "next": "dispatch:revise:3"}

    monkeypatch.setattr(review_runner, "_step_identity", current_step)
    delivery = await replay.resume_after_restart(wid, deliver_fn=lambda _: "continue:1")
    assert len(delivery["deliveries"]) == int(delivered)
    row = st.get_action(wid, aid)
    assert row["state"] == ("delivered" if delivered else "stale")
    if delivered:
        assert row["biz_key"] == "rv_old|2|dispatch:revise:3"
