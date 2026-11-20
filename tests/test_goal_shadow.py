# -*- coding: utf-8 -*-
"""[旁路对照 2026-11-20 · 目标推进方案 §施工顺序-1]
shadow_eval 影子评估器：现行决定与候选判断对同一现场各记一条。
钉的是：相同现场重复续轮 ⇒ 候选判 hold_identical（分歧行）；
有新事实/新签名 ⇒ advance；让位/机械闸 ⇒ 照实归类。"""
import json
from types import SimpleNamespace

import pytest

from src.goal import shadow_eval
from src.api import event_stream


def _goal(**kw):
    base = {"id": "g1", "revision": 2, "rounds_started": 3}
    base.update(kw)
    return SimpleNamespace(**base)


def _lines(path):
    return [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _clean_sigs():
    shadow_eval.reset()
    yield
    shadow_eval.reset()


@pytest.fixture(autouse=True)
async def _pilot(work, monkeypatch):
    """同步采样只在隔离试点——把本用例的 work 登记进白名单。"""
    monkeypatch.setenv("ECO_SHADOW_WORKS", work["work_id"])
    yield


def test_first_spawn_is_advance(sandbox, work):
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    rec = _lines(shadow_eval._log_path(wid))[-1]
    assert rec["decision"] == "spawn"
    assert rec["candidate"] == "advance"
    assert rec["goal"]["rounds"] == 3


def test_identical_state_holds(sandbox, work):
    """同一现场第二次 spawn ⇒ hold_identical——「相同状态的 idle
    不再开模型回合」的对照行。"""
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    rec = _lines(shadow_eval._log_path(wid))[-1]
    assert rec["candidate"] == "hold_identical"
    assert rec["same_sig_as_last"] is True
    assert rec["new_facts_since_last"] == 0


def test_new_fact_breaks_hold(sandbox, work):
    """相同签名但期间落了真实事实（工具结果/卡间信）⇒ advance——
    现场其实动了，不能算空转。"""
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    event_stream.emit(wid, "card_001", "tool_call",
                      {"tool": "read_card", "seq": 1})
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    rec = _lines(shadow_eval._log_path(wid))[-1]
    assert rec["candidate"] == "advance"
    assert rec["new_facts_since_last"] >= 1


def test_noise_event_does_not_break_hold(sandbox, work):
    """turn_end/goal_round 这类自产噪声不算「新事实」——否则
    每次续轮自身就污染签名，hold 永远不成立。"""
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    event_stream.emit(wid, "card_001", "turn_end", {"outcome": "ok"})
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    rec = _lines(shadow_eval._log_path(wid))[-1]
    assert rec["candidate"] == "hold_identical"


def test_yield_and_mechanical_gates(sandbox, work):
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "yield_children", goal=_goal())
    shadow_eval.note(wid, "card_001", "skip:auto_off")
    recs = _lines(shadow_eval._log_path(wid))
    assert recs[0]["candidate"] == "yield"
    assert recs[1]["candidate"] == "mirror"


def test_rounds_not_in_signature(sandbox, work):
    """rounds_started 单调增——进签名「相同现场」永不成立；
    它只进记录不进比对。"""
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal(rounds_started=4))
    rec = _lines(shadow_eval._log_path(wid))[-1]
    assert rec["candidate"] == "hold_identical"
    assert rec["goal"]["rounds"] == 4


def test_receipt_survives_noise_tail(sandbox, work):
    """反例二（2026-11-20）：真 worker_done 后跟 400 条噪声——游标按
    类型过滤＋增量走账，回执不再被「最新 N 截尾」挤出读数窗。"""
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    event_stream.emit(wid, "card_001", "worker_done",
                      {"from_card": "card_002", "reply_to": "cm_x"})
    for i in range(400):
        event_stream.emit(wid, "card_001", "turn_end", {"outcome": "ok", "i": i})
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    rec = _lines(shadow_eval._log_path(wid))[-1]
    assert rec["candidate"] == "advance", "回执被噪声挤掉 ⇒ 误判现场未变"
    assert rec["new_facts_since_last"] >= 1
    assert rec["read_complete"] is True


def test_partial_read_marks_pending(sandbox, work, monkeypatch):
    """读数不全 ⇒ candidate=待核实，不许装成 advance/hold。"""
    wid = work["work_id"]
    from src.infra import fact_store

    def _boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(fact_store.store(), "card_facts_after", _boom)
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    rec = _lines(shadow_eval._log_path(wid))[-1]
    assert rec["candidate"] == "pending_verification"
    assert rec["read_complete"] is False


def test_non_pilot_writes_nothing(sandbox, work, monkeypatch):
    """同步采样只在隔离试点：未登记的 work 一条不落。"""
    wid = work["work_id"]
    monkeypatch.setenv("ECO_SHADOW_WORKS", "work_other")
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    assert not shadow_eval._log_path(wid).exists()


def test_intent_and_request_accounted_separately(sandbox, work):
    """续轮意图与实际模型请求分账——accounting 行不是判断，不进
    advance/hold 序列。"""
    wid = work["work_id"]
    shadow_eval.note(wid, "card_001", "spawn", goal=_goal())
    shadow_eval.model_request(wid, "card_001", {
        "kind": "goal", "goalId": "g1", "revision": 2, "round": 4,
    }, pair_id="turn-1", attempt=1)
    shadow_eval.note(wid, "card_001", "intent_dropped", detail="stale")
    recs = _lines(shadow_eval._log_path(wid))
    assert [r["decision"] for r in recs] == [
        "spawn", "model_request", "intent_dropped"]
    assert recs[1]["pair_id"] == "turn-1"
    assert recs[1]["goal"] == {"id": "g1", "revision": 2, "rounds": 4}
    assert recs[2]["candidate"] == "accounting"
