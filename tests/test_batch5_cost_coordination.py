# -*- coding: utf-8 -*-
"""[成本优化批五] 唤醒合并（COST_WAKE_COALESCE）＋同跳重试预算
（COST_HOP_RETRY_BUDGET）专项。

两开关缺省关闭 ⇒ 关态逐字同基线；开态按任务书 §一/§二 验收：
  · 同一连续超限期只受理一次提醒，材料刷到最新事实，恢复销账；
  · 同一原异常身份（pair_id+round+anomaly）只生效一次补救决定；
  · 同一逻辑跳次的重试＋同层换座共用一次发送边界尝试额度。
"""
from __future__ import annotations

import json
import time

import pytest

from src.infra import fact_store as fs
from src.infra import intervention_runner as ir
from src.infra import intervention_store as ist


# ================================================================ 台账夹具
@pytest.fixture()
def ledgers(tmp_path, monkeypatch):
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


def _coalesce(monkeypatch, on: bool):
    monkeypatch.setenv("COST_WAKE_COALESCE", "1" if on else "0")


def _oversize_fired(card="c1", new_len=12000, seq=10, ts=None, block="status_block"):
    return {
        "policy": "status_block_oversize_reminder",
        "scope": [card],
        "then": {"target": "self", "mode": "message", "wake": True,
                 "message": "现状块超限 {event.new_len} 字",
                 "matter": {"kind": "status_oversize", "key": "status_block",
                            "check_on": ["card_block_edited"]}},
        "ctx": {"work_id": "w1", "card_id": card, "parent": "",
                "scope": [card],
                "trigger_data": {"block": block, "new_len": new_len,
                                  "old_len": 9000}},
        "basis": {"card": card, "seq": seq, "ts": ts or time.time(),
                   "type": "card_block_edited"},
    }


def _len_patch(monkeypatch, length: int):
    """当前卡面字数权威源：_status_len 直读 cards 表——用例直接喂。"""
    monkeypatch.setattr(ir, "_status_len", lambda w, cid: length)


# ================================================================ 一.1 超限合并
def test_off_every_over_limit_edit_fires(ledgers, monkeypatch):
    """关态基线：每次超限编辑各登记一条动作（旧行为逐字不变）。"""
    s, _ = ledgers
    _coalesce(monkeypatch, False)
    ir.record_fired("w1", [_oversize_fired(seq=10, new_len=10000)])
    ir.record_fired("w1", [_oversize_fired(seq=11, new_len=12000)])
    ir.record_fired("w1", [_oversize_fired(seq=12, new_len=13000)])
    assert len(s.open_matters("w1")) == 0                    # 关态不立案
    assert len(s.pending_actions("w1")) == 3               # 三次超限＝三动作


def test_on_coalesce_one_matter_one_action(ledgers, monkeypatch):
    """开态：9999→10000→12000→13000 连续段＝同一事项同一动作。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    _len_patch(monkeypatch, 13000)
    f1 = _oversize_fired(seq=10, new_len=10000)
    ir.record_fired("w1", [f1])
    mid, aid = f1["_matter_id"], f1["_actions"]["c1"]
    assert mid and aid
    f2 = _oversize_fired(seq=11, new_len=12000)
    ir.record_fired("w1", [f2])
    f3 = _oversize_fired(seq=12, new_len=13000)
    ir.record_fired("w1", [f3])
    matters = s.open_matters("w1")
    assert len(matters) == 1
    assert f2["_matter_reused"] and f3["_matter_reused"]
    assert f3["_actions"]["c1"] == aid                      # 同一动作编号
    assert len(s.matter_actions("w1", mid)) == 1
    m = s.get_matter("w1", mid)
    assert m["basis"]["seq"] == 12                          # 事实锚到最新
    assert m["basis"]["current_len"] == 13000


def test_on_coalesce_pending_material_refreshed(ledgers, monkeypatch):
    """开态：待受理动作的投递材料刷到**最新有效字数**（非首发旧值）。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    _len_patch(monkeypatch, 13000)
    f1 = _oversize_fired(seq=10, new_len=10000)
    ir.record_fired("w1", [f1])
    aid = f1["_actions"]["c1"]
    a0 = s.get_action("w1", aid)
    assert "超限 {event.new_len}" in a0["message"]           # 台账存模板
    ir.record_fired("w1", [_oversize_fired(seq=11, new_len=13000)])
    a = s.get_action("w1", aid)
    assert "13000" in a["message"] and "{event.new_len}" not in a["message"]


def test_on_coalesce_delivered_action_not_rewritten(ledgers, monkeypatch):
    """已受理动作（sending/delivered）只更新台账事实，不回写文案。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    _len_patch(monkeypatch, 12000)
    f1 = _oversize_fired(seq=10, new_len=10000)
    ir.record_fired("w1", [f1])
    aid = f1["_actions"]["c1"]
    s.mark_action("w1", aid, "delivered", receipt={"ok": True})
    ir.record_fired("w1", [_oversize_fired(seq=11, new_len=12000)])
    a = s.get_action("w1", aid)
    assert "{event.new_len}" in a["message"]                # 历史原样可查
    m = s.get_matter("w1", f1["_matter_id"])
    assert m["basis"]["seq"] == 11                          # 台账事实仍更新


def test_recovery_closes_matter_stales_action(ledgers, monkeypatch):
    """回阈值内 ⇒ 事项 resolved、pending 动作 stale；再超限起新事项。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    seqs = {"n": 13000}
    monkeypatch.setattr(ir, "_status_len", lambda w, cid: seqs["n"])
    f1 = _oversize_fired(seq=10, new_len=10000)
    ir.record_fired("w1", [f1])
    mid, aid = f1["_matter_id"], f1["_actions"]["c1"]
    # 恢复编辑（new_len<10000）——卡面同步回阈值内
    seqs["n"] = 9000
    ir.on_fact("w1", {"type": "card_block_edited",
                       "trigger_data": {"block": "status_block", "new_len": 9000},
                       "card": "c1"})
    assert s.get_matter("w1", mid)["state"] == "resolved"
    assert s.get_action("w1", aid)["state"] == "stale"
    # 再超限 ⇒ 新事项新动作
    seqs["n"] = 11000
    f2 = _oversize_fired(seq=20, new_len=11000)
    ir.record_fired("w1", [f2])
    assert f2["_matter_id"] != mid
    assert f2["_actions"]["c1"] != aid
    assert len(s.matter_actions("w1", f2["_matter_id"])) == 1


def test_late_fact_does_not_reopen_after_recovery(ledgers, monkeypatch):
    """晚到/回放的旧超限事实：卡面已恢复 ⇒ 不立案不登记。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    _len_patch(monkeypatch, 9000)                           # 卡面现状已恢复
    f = _oversize_fired(seq=10, new_len=15000)              # 迟到的旧超限事实
    ir.record_fired("w1", [f])
    assert s.open_matters("w1") == []
    assert s.pending_actions("w1") == []
    assert f["_matter_reused"]                              # 标记跳投


def test_other_card_unaffected(ledgers, monkeypatch):
    """另一张卡的超限不影响本卡事项（scope 按卡隔离）。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    _len_patch(monkeypatch, 12000)
    fa = _oversize_fired(card="c1", seq=10)
    fb = _oversize_fired(card="c2", seq=10)
    ir.record_fired("w1", [fa])
    ir.record_fired("w1", [fb])
    matters = s.open_matters("w1")
    assert len(matters) == 2                                # 各卡各一事项
    assert fb["_matter_id"] != fa["_matter_id"]
    # c1 恢复不影响 c2
    monkeypatch.setattr(ir, "_status_len",
                        lambda w, cid: 9000 if cid == "c1" else 12000)
    ir.on_fact("w1", {"type": "card_block_edited",
                       "trigger_data": {"block": "status_block", "new_len": 9000},
                       "card": "c1"})
    assert s.get_matter("w1", fa["_matter_id"])["state"] == "resolved"
    assert s.get_matter("w1", fb["_matter_id"])["state"] == "open"


# ================================================================ 一.2 伪调用同异常
def _pseudo_fired(card="c9", pair="pair_1", rnd=3, seq=50, anomaly="pseudo_unparsed"):
    return {
        "policy": "pseudo_call_reinvoke",
        "scope": [card],
        "then": {"target": "self", "mode": "message", "wake": True,
                 "message": "重发真实调用",
                 "dedup_fields": ["pair_id", "round", "anomaly"]},
        "ctx": {"work_id": "w1", "card_id": card, "parent": "",
                "scope": [card],
                "trigger_data": {"recent": 1, "pair_id": pair,
                                  "round": rnd, "anomaly": anomaly,
                                  "chars": 500}},
        "basis": {"card": card, "seq": seq, "ts": time.time(),
                   "type": "pseudo_call_unparsed"},
    }


def test_same_anomaly_identity_one_action(ledgers, monkeypatch):
    """同一原异常（pair+round+anomaly）重复/换 seq 再来 ⇒ 同一动作。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    f1 = _pseudo_fired(seq=50)
    f2 = _pseudo_fired(seq=51)                                # 换事件编号同一异常
    ir.record_fired("w1", [f1])
    ir.record_fired("w1", [f2])
    acts = s.pending_actions("w1")
    assert len(acts) == 1
    assert f2["_actions"]["c9"] == f1["_actions"]["c9"]


def test_different_anomaly_new_action(ledgers, monkeypatch):
    """不同 pair/round ⇒ 新动作（不同异常不互相吞）。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    ir.record_fired("w1", [_pseudo_fired(seq=50, pair="pair_1", rnd=3)])
    ir.record_fired("w1", [_pseudo_fired(seq=51, pair="pair_1", rnd=4)])
    ir.record_fired("w1", [_pseudo_fired(seq=52, pair="pair_2", rnd=3)])
    assert len(s.pending_actions("w1")) == 3


def test_missing_pair_id_falls_back_no_biz_dedup(ledgers, monkeypatch):
    """缺原回合身份（无 pair_id）⇒ 不用业务键：事实级 fire_key＋缺证账。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    ir.record_fired("w1", [_pseudo_fired(seq=50, pair="")])
    ir.record_fired("w1", [_pseudo_fired(seq=51, pair="")])
    assert len(s.pending_actions("w1")) == 2                # 不合并
    errs = [e for e in s.list_errors("w1")
            if e.get("kind") == "missing_identity"]
    assert errs and "pair_id" in str((errs[0].get("detail") or {}))


def test_off_pseudo_no_biz_dedup(ledgers, monkeypatch):
    """关态：dedup_fields 不生效——同异常两条事实各登各的动作。"""
    s, _ = ledgers
    _coalesce(monkeypatch, False)
    ir.record_fired("w1", [_pseudo_fired(seq=50)])
    ir.record_fired("w1", [_pseudo_fired(seq=51)])
    assert len(s.pending_actions("w1")) == 2


# ================================================================ 二 同跳预算（单元层）
def test_hop_budget_gate_and_report():
    """发送边界领用：限额含首次；耗尽抛控制异常；账目分 sent/terminal。"""
    from src.model_gateway.hop_budget import HopBudget, HopBudgetExhausted
    b = HopBudget(limit=2, identity={"pair_id": "p", "logical_hop": 1})
    assert b.gate("seatA") == 1
    b.confirm_sent(); b.note_terminal(); b.settle("ok")
    assert b.gate("seatB") == 2
    with pytest.raises(HopBudgetExhausted):
        b.gate("seatB")
    rep = b.report()
    assert rep["used"] == 2 and rep["sent"] == 1 and rep["terminal"] == 1
    assert rep["exhausted"] and rep["seats"] == ["seatA", "seatB"]
    assert rep["attempts"][0]["outcome"] == "ok"


def test_budget_retry_ok_reserve_last_for_next_seat():
    """有合规下座时最后一次尝试留给它：额度2 ⇒ A→B，3 ⇒ A→A→B。"""
    from src.model_gateway.gateway import _budget_retry_ok
    from src.model_gateway.hop_budget import HopBudget
    b2 = HopBudget(limit=2)
    b2.gate("A")
    assert _budget_retry_ok(0, 3, "503", b2, True) is False   # 剩1留 B
    b3 = HopBudget(limit=3)
    b3.gate("A")
    assert _budget_retry_ok(0, 3, "503", b3, True) is True    # 剩2可在 A 再试
    b3.gate("A")
    assert _budget_retry_ok(1, 3, "503", b3, True) is False   # 剩1留 B
    # 确定性拒绝不重发
    b4 = HopBudget(limit=3)
    b4.gate("A")
    assert _budget_retry_ok(0, 3, "auth", b4, True) is False
    assert _budget_retry_ok(0, 3, "deterministic", b4, False) is False
    # 关态（None）逐字同旧
    assert _budget_retry_ok(0, 3, "auth", None, True) is True
    assert _budget_retry_ok(2, 3, "503", None, True) is False


# ================================================================ 二 同跳预算（真实出站）
class _SendProbe:
    """本批出站探针：记录每次 ``client.send`` 的请求体＋响应选择。

    与 ``OutboundRecorder`` 同一拦截点（``httpx.AsyncClient.send``），但
    直接回**真实 Response 对象**——批五要注 503/401 这类非 200 状态，
    OutboundRecorder 会把返回值一律包成 200（注不进状态码）。"""

    def __init__(self, responder):
        self.requests: list[dict] = []
        self._responder = responder

    async def send(self, client, request, **kwargs):
        raw = request.content or b""
        self.requests.append({"url": str(request.url), "bytes": len(raw),
                              "raw": raw})
        out = self._responder(len(self.requests) - 1, request)
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, httpx.Response):
            out.request = request
            return out
        return httpx.Response(200, content=out, request=request)

    def install(self, monkeypatch):
        import httpx  # noqa: F401  # 顶部已引；此处仅为阅读锚点
        probe = self

        async def _send(client, request, **kwargs):
            return await probe.send(client, request, **kwargs)
        monkeypatch.setattr(httpx.AsyncClient, "send", _send)
        return self


import httpx  # noqa: E402


def _r503(_i, _req):
    return httpx.Response(503, content=b'{"error":{"message":"unavailable"}}')


@pytest.mark.asyncio
async def test_stream_call_budget_shared_across_seats(monkeypatch):
    """真实适配器出站：同层两座连续 503，额度3 ⇒ A→A→B 共3次实际出站。"""
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    for env in ("OPENDESIGN_API_KEY", "OPENDESIGN2_API_KEY"):
        monkeypatch.setenv(env, "stub-key")
    probe = _SendProbe(_r503).install(monkeypatch)
    gw = model_service.get_gateway()
    budget = HopBudget(limit=3, identity={"hop": 1})
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "sys", "user",
                fallbacks=["ds-v41-flash-eco-amr2"],
                hop_budget=budget):
            pass
    except Exception:
        pass
    rep = budget.report()
    assert rep["used"] == 3                                  # 额度耗尽
    assert rep["seats"] == ["ds-v41-flash-eco-amr",
                            "ds-v41-flash-eco-amr2"]          # A→A→B
    assert len(probe.requests) == 3                          # 实际出站=3
    assert all(a["seat"] in rep["seats"] for a in rep["attempts"])


@pytest.mark.asyncio
async def test_stream_call_budget_two_seats(monkeypatch):
    """额度2＋两座 ⇒ A→B 各一（最后一次留给下一合规座）。"""
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    for env in ("OPENDESIGN_API_KEY", "OPENDESIGN2_API_KEY"):
        monkeypatch.setenv(env, "stub-key")
    probe = _SendProbe(_r503).install(monkeypatch)
    gw = model_service.get_gateway()
    budget = HopBudget(limit=2)
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "sys", "user",
                fallbacks=["ds-v41-flash-eco-amr2"],
                hop_budget=budget):
            pass
    except Exception:
        pass
    rep = budget.report()
    assert rep["used"] == 2
    assert rep["attempts"][0]["seat"] == "ds-v41-flash-eco-amr"
    assert rep["attempts"][1]["seat"] == "ds-v41-flash-eco-amr2"
    assert len(probe.requests) == 2


@pytest.mark.asyncio
async def test_stream_call_no_budget_legacy_multiplies(monkeypatch):
    """关态（无预算对象）：同层两座各跑满自身 max_attempts（旧乘法）。"""
    from src.api import model_service
    for env in ("OPENDESIGN_API_KEY", "OPENDESIGN2_API_KEY"):
        monkeypatch.setenv(env, "stub-key")
    probe = _SendProbe(_r503).install(monkeypatch)
    gw = model_service.get_gateway()
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "sys", "user",
                fallbacks=["ds-v41-flash-eco-amr2"]):
            pass
    except Exception:
        pass
    # 两座各 retry.max_attempts（缺省3）⇒ 6 次实际出站（旧乘法实证）
    assert len(probe.requests) == 6


@pytest.mark.asyncio
async def test_stream_call_auth_no_resend_under_budget(monkeypatch):
    """预算模式＋401：确定性拒绝零自动重发（出站=1）。"""
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    probe = _SendProbe(
        lambda i, r: httpx.Response(
            401, content=b'{"error":{"message":"bad key"}}')).install(monkeypatch)
    gw = model_service.get_gateway()
    budget = HopBudget(limit=3)
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "sys", "user",
                hop_budget=budget):
            pass
    except Exception:
        pass
    assert len(probe.requests) == 1                          # 不重发
    assert budget.report()["attempts"][0]["outcome"] == "auth"


@pytest.mark.asyncio
async def test_stream_call_auth_legacy_retries(monkeypatch):
    """关态对照：401 也按次数重试满（旧行为如实记录）。"""
    from src.api import model_service
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    probe = _SendProbe(
        lambda i, r: httpx.Response(
            401, content=b'{"error":{"message":"bad key"}}')).install(monkeypatch)
    gw = model_service.get_gateway()
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "sys", "user"):
            pass
    except Exception:
        pass
    assert len(probe.requests) == 3                          # 旧行为：满 3 次


@pytest.mark.asyncio
async def test_normal_next_hop_gets_fresh_budget(monkeypatch):
    """正常续跳＝新预算对象：hop1 耗尽不泄漏到 hop2。"""
    from outbound_probe import frame_finish, frame_text, sse
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    state = {"fail": True}

    def responder(i, req):
        if state["fail"]:
            return httpx.Response(503, content=b'{"error":{"message":"x"}}')
        return sse(frame_text("ok"), frame_finish("stop"))

    probe = _SendProbe(responder).install(monkeypatch)
    gw = model_service.get_gateway()
    b1 = HopBudget(limit=2)
    try:
        async for _ev in gw.stream_call("ds-v41-flash-eco-amr", "s", "u",
                                        hop_budget=b1):
            pass
    except Exception:
        pass
    assert b1.report()["used"] == 2
    state["fail"] = False
    b2 = HopBudget(limit=2)                                   # 续跳新预算
    out = []
    async for ev in gw.stream_call("ds-v41-flash-eco-amr", "s", "u",
                                   hop_budget=b2):
        out.append(ev)
    assert any(isinstance(e, dict) and e.get("content") for e in out)
    assert b2.report()["used"] == 1 and b2.report()["terminal"] == 1
    assert len(probe.requests) == 3                           # 2 失败＋1 成功


@pytest.mark.asyncio
async def test_disconnect_counts_attempt_not_confirmed(monkeypatch):
    """断连：进入发送调用即记尝试；未确认发送（sent=False）如实分账。"""
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    probe = _SendProbe(
        lambda i, r: httpx.ConnectError("conn reset")).install(monkeypatch)
    gw = model_service.get_gateway()
    budget = HopBudget(limit=2)
    try:
        async for _ev in gw.stream_call("ds-v41-flash-eco-amr", "s", "u",
                                        hop_budget=budget):
            pass
    except Exception:
        pass
    rep = budget.report()
    assert rep["used"] == 2 and rep["sent"] == 0             # 发出未确认
    assert rep["attempts"][0]["sent"] is False


# ================================================================ 返修钉死
def test_shared_decision_suppresses_pseudo_action(ledgers, monkeypatch):
    """旧 runner 占位只拦同一跳的裸事实。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    aid, _ = s.record_action(
        "w1", policy_id="turn_remediation", scope_key="c1",
        fire_key="remediation|c9|pair_1|op:finish", target="c9",
        mode="remediation", wake=False, message='{"rounds":3}')
    assert aid
    f = _pseudo_fired(seq=50, pair="pair_1", rnd=3)
    ir.record_fired("w1", [f])
    pend = s.pending_actions("w1")
    assert len(pend) == 1                                     # 无第二笔
    assert pend[0]["action_id"] == aid                        # 仍是占位
    assert f.get("_shared_decision") == "turn_remediation"
    other = _pseudo_fired(seq=51, pair="pair_1", rnd=4)
    ir.record_fired("w1", [other])
    assert other["_actions"]["c9"] != aid


def test_pseudo_action_seen_by_runner_check(ledgers, monkeypatch):
    """harness 先到也共用原决定，另一个异常身份可以单独登记。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    f = _pseudo_fired(seq=50, pair="pair_9", rnd=2)
    ir.record_fired("w1", [f])
    aid = f["_actions"]["c9"]
    s.mark_action("w1", aid, "unverified")
    step = {"sid": "c9", "pair_id": "pair_9", "round": 2,
            "anomaly": "pseudo_unparsed", "task_id": ""}

    def claim(**change):
        return s.claim_anomaly_decision(
            "w1", policy_id="turn_remediation", scope_key="c9",
            fire_key="remediation|c9|pair_9|op:finish", target="c9",
            mode="remediation", wake=False, message="{}", step={**step, **change})

    assert claim() == (aid, False, "pseudo_call_reinvoke")
    assert claim(round=3)[1]
    assert claim(anomaly="pseudo_mixed")[1]
    assert claim(sid="c9:seat")[1]
    assert claim(task_id="task1")[1]


@pytest.mark.asyncio
async def test_resume_oversize_delivers_latest_len(ledgers, monkeypatch):
    """超限动作领取后按**当前卡面**重渲染——存稿旧读数不进会话。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    seqs = {"n": 10000}
    monkeypatch.setattr(ir, "_status_len", lambda w, cid: seqs["n"])
    ir.record_fired("w1", [_oversize_fired(seq=10, new_len=10000)])
    a = s.pending_actions("w1")[0]
    assert "{event.new_len}" in a["message"]                  # 存稿是模板
    seqs["n"] = 13000                                         # 卡面又涨
    sent: list[str] = []
    from src.infra import harness

    async def _fake_deliver(w, t, text, **kw):
        sent.append(text)
        return {"ok": True}

    monkeypatch.setattr(harness, "deliver", _fake_deliver)
    await ir.resume("w1")
    assert sent and "13000" in sent[0] and "10000" not in sent[0]


@pytest.mark.asyncio
async def test_auth_no_seat_switch_under_budget(monkeypatch):
    """预算开＋401＋备选座：确定性裁定不换座（总出站=1）。"""
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    for env in ("OPENDESIGN_API_KEY", "OPENDESIGN2_API_KEY"):
        monkeypatch.setenv(env, "stub-key")
    probe = _SendProbe(
        lambda i, r: httpx.Response(
            401, content=b'{"error":{"message":"bad key"}}')).install(monkeypatch)
    gw = model_service.get_gateway()
    budget = HopBudget(limit=3)
    err = None
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "sys", "user",
                fallbacks=["ds-v41-flash-eco-amr2"],
                hop_budget=budget):
            pass
    except Exception as exc:  # noqa: BLE001
        err = exc
    assert len(probe.requests) == 1                           # 不换座
    # 结构化裁定随错误对象上抛——外层同位置重发据此收口
    assert err is not None and getattr(err, "reason", "") == "auth"


@pytest.mark.asyncio
async def test_insufficient_balance_deterministic(monkeypatch):
    """余额类拒绝归类 deterministic：不重发不换座（card_013 实录）。

    真实路径：端点回 HTTP 400 + ``insufficient_balance`` ⇒ 适配器归
    ``QUOTA`` 码 ⇒ 失败帧上岸（非异常）⇒ _classify 判 deterministic。"""
    from src.api import model_service
    from src.model_gateway.gateway import _classify_message
    from src.model_gateway.hop_budget import HopBudget
    assert _classify_message("insufficient_balance") == "deterministic"
    for env in ("OPENDESIGN_API_KEY", "OPENDESIGN2_API_KEY"):
        monkeypatch.setenv(env, "stub-key")
    probe = _SendProbe(
        lambda i, r: httpx.Response(
            400, content=b'{"error":{"code":"insufficient_balance",'
                         b'"message":"insufficient_balance"}}')).install(
        monkeypatch)
    gw = model_service.get_gateway()
    budget = HopBudget(limit=3)
    err = None
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "sys", "user",
                fallbacks=["ds-v41-flash-eco-amr2"],
                hop_budget=budget):
            pass
    except Exception as exc:  # noqa: BLE001
        err = exc
    assert len(probe.requests) == 1                           # 不重发不换座
    assert budget.report()["attempts"][0]["outcome"] == "deterministic"
    assert getattr(err, "reason", "") == "deterministic"      # 裁定随错误上抛


@pytest.mark.asyncio
async def test_retry_after_honored(monkeypatch):
    """429+Retry-After ⇒ 下一次出站前等满端点给的退避（非固定 backoff）。"""
    import asyncio
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    sleeps: list[float] = []

    async def _fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    def responder(i, req):
        if i == 0:
            return httpx.Response(
                429, headers={"Retry-After": "1.5"},
                content=b'{"error":{"message":"slow down"}}')
        return httpx.Response(503, content=b'{"error":{"message":"x"}}')

    _SendProbe(responder).install(monkeypatch)
    gw = model_service.get_gateway()
    monkeypatch.setattr(gw, "_retry_params", lambda m: (3, 0.01))
    budget = HopBudget(limit=3)
    try:
        async for _ev in gw.stream_call("ds-v41-flash-eco-amr", "s", "u",
                                        hop_budget=budget):
            pass
    except Exception:
        pass
    assert any(d >= 1.4 for d in sleeps), sleeps              # 1.5s 退避生效


# ================================================================ 返工整链
@pytest.mark.asyncio
async def test_stop_guard_blocks_next_attempt(monkeypatch):
    """真实停止在退避后落闸：before_send 拒权 ⇒ 零新增出站、额度未耗。"""
    from src.api import model_service
    from src.model_gateway.hop_budget import HopBudget
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    probe = _SendProbe(
        lambda i, r: httpx.Response(503, content=b'{"error":{}}')
    ).install(monkeypatch)
    gw = model_service.get_gateway()
    calls = {"n": 0}

    class _Stop(Exception):
        pass

    async def guard():
        calls["n"] += 1
        if calls["n"] > 1:
            raise _Stop("user stop")

    budget = HopBudget(limit=3)
    try:
        async for _ev in gw.stream_call(
                "ds-v41-flash-eco-amr", "s", "u",
                hop_budget=budget, before_send=guard):
            pass
    except Exception:
        pass
    assert len(probe.requests) == 1                            # 停后零新请求
    assert budget.report()["used"] == 1                        # 拒权不耗额度


@pytest.mark.asyncio
async def test_resume_chain_single_delivery(ledgers, monkeypatch):
    """重启恢复整链：pending 投一次即 delivered；卡面已恢复⇒销账不落信。"""
    s, _ = ledgers
    _coalesce(monkeypatch, True)
    seqs = {"n": 13000}
    monkeypatch.setattr(ir, "_status_len", lambda w, cid: seqs["n"])
    ir.record_fired("w1", [_oversize_fired(seq=10, new_len=10000)])
    sent: list[str] = []
    from src.infra import harness

    async def _fake(w, t, text, **kw):
        sent.append(text)
        return {"ok": True}

    monkeypatch.setattr(harness, "deliver", _fake)
    r1 = await ir.resume("w1")
    r2 = await ir.resume("w1")                                 # 模拟二次恢复
    assert len(sent) == 1 and "13000" in sent[0]               # 只投一次最新材料
    assert r1["redelivered"] == 1 and r2["redelivered"] == 0

    # 新超限期：登记后卡面回到阈值内（事项还开着）⇒ 落信边界销账零投递
    ir.record_fired("w1", [_oversize_fired(seq=20, new_len=11000)])
    seqs["n"] = 9000
    sent.clear()
    r3 = await ir.resume("w1")
    assert sent == []                                          # 零落信零起轮
    pend = [a for a in s.pending_actions("w1")]
    assert all(a["state"] == "stale" or a["state"] == "delivered"
               for a in s.matter_actions("w1", str(pend[0].get("matter_id") or ""))
               ) if pend else True


def test_directive_msg_id_single_delivery(sandbox):
    """direct 已落信待确认+故障重投：接收方始终一封，原 msg_id 可读。"""
    from src.storage import session_facade
    r1 = session_facade.append_directive_sync(
        "w1", "c1", "c0", "指令A", "msg-1")
    r2 = session_facade.append_directive_sync(
        "w1", "c1", "c0", "指令A", "msg-1")
    assert r1.get("skipped") is not True
    assert r2.get("skipped") is True                           # 幂等重投
    path = session_facade._session_path("w1", "c1")
    body = path.read_text(encoding="utf-8")
    assert body.count("msg-1") == 1                            # 一封，原 id 可查


@pytest.mark.asyncio
async def test_partial_stream_archived_without_dispatching_last_candidate(work, monkeypatch):
    from src.api import routes
    from src.storage import model_override, predicates, session_facade
    from tests.outbound_probe import frame_finish, frame_text, frame_tool, sse

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    monkeypatch.setenv("COST_HOP_RETRY_BUDGET", "1")
    model_override.put_row(wid, cid, {"model": "ds-v41-flash-eco-amr"}, src="manual")
    partial = "已核对第一项，后续尚未核对。"

    class IncompleteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            frames = sse(
                frame_text(partial),
                frame_tool("uncommitted", "read_card", {"card_id": cid}),
            )
            yield frames.split(b"data: [DONE]")[0]
            raise httpx.ReadError("link closed before finish")

    def respond(index, _):
        if index == 0:
            return sse(frame_tool("completed_read", "read_card", {"card_id": cid}),
                       frame_finish("tool_calls"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=IncompleteStream())

    probe = _SendProbe(respond).install(monkeypatch)
    result = await routes._run_message(wid, cid, "核对记录", None, source_kind="human")
    blocks = await session_facade.read(wid, cid)
    reply = next(b for b in blocks if partial in str(b.get("content") or ""))
    assert result.status_code == 502
    assert len(probe.requests) == 2
    assert reply["end"] == "error"
    assert "交付未完成" in predicates.context_content(reply)
    assert partial not in predicates.context_content(reply)
    tools = [b for b in blocks if b.get("actor") == "tool"]
    assert len(tools) == 1 and tools[0]["status"] == "ok"
    calls = [b for b in blocks if b.get("tool_calls")]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reopened_store_refuses_unverified_same_hop(work, sandbox, monkeypatch):
    from types import SimpleNamespace

    from src.api import message_runner, model_service
    from src.model_gateway.hop_budget import HopBudgetUnverified
    from tests.outbound_probe import frame_finish, frame_text, sse

    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setenv("OPENDESIGN_API_KEY", "stub-key")
    monkeypatch.setenv("COST_HOP_RETRY_BUDGET", "1")
    monkeypatch.setattr(model_service.get_gateway(), "_retry_params", lambda _: (3, 0))
    success = sse(frame_text("本跳已收到正文"), frame_finish("stop"))
    probe = _SendProbe(lambda index, request: _r503(index, request) if index == 0
                      else success).install(monkeypatch)

    async def call(logical_hop):
        state = SimpleNamespace(work_id=wid, card_id=cid, session_id=cid,
                                pair_id="cold-hop-audit", rounds=logical_hop,
                                source={}, hop_transport={})
        result, _ = await message_runner._call_model_resumable(
            state, system_prompt="system", user_prompt="user",
            model_name="ds-v41-flash-eco-amr")
        return result["hop_budget"]

    assert (await call(1))["used"] == 2
    ist.reset(ist.InterventionStore(works_dir=sandbox.works))
    with pytest.raises(HopBudgetUnverified) as rejected:
        await call(1)
    assert rejected.value.facts["used"] == 2
    assert len(probe.requests) == 2
    assert (await call(2))["used"] == 1
    assert len(probe.requests) == 3
