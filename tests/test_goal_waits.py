# -*- coding: utf-8 -*-
"""[声明式等待 2026-11-20 · 目标推进方案 §一 · 第二批隔离验证]

以 007→057 派工形态搭最小自动卡链：自动父卡 declare_wait 声明
「等哪笔派工的哪个回执」——只剩等待时 goal 驱动让出回合（零模型
请求）；真实回执（msg_report/worker_done，reply_to＝原派工 msg_id）
经机械核销后恰好续一次。

覆盖：错任务/错来源/旧版本/重复回执/先到回执/同卡两活/人工停止/
缺关联（待核实）/独立待办/双回执类型/旧目标版本等待。

[验收修 2026-11-20] 三处实测定案的补测：
- 错任务回执经**真实 report_up 入口**（deliver_now）也不起轮——
  等待核对前置在唤醒之前，核销的续轮归驱动统一入口；
- 子卡真 active＋independent 声明 ⇒ 父卡不被整体让位；
- 派工版本印：目标换版后旧派工的回执核销不了新等待，证不了
  版本归属的等待记「待核实」不拦轮次。"""
import asyncio

import pytest

from src.api import card_tools, control, event_stream
from src.api import routes as _routes
from src.goal import driver as goal_driver
from src.goal import shadow_eval, wait_store
from src.goal.service import get_service
from src.goal.tools import h_declare_wait
from src.goal.types import CreateGoalRequest, EditGoalRequest
from src.storage import card_store, session_facade


@pytest.fixture(autouse=True)
def _clean():
    from src.api import message_runner, serial_gate

    def _reset():
        get_service()._caches.clear()
        serial_gate.reset()
        message_runner._TRIGGER_TASKS.clear()
        goal_driver._states.clear()
        shadow_eval.reset()
        control._HALTED.clear()
        control._HALTED_LOADED.clear()
    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
async def _pilot(work, monkeypatch):
    monkeypatch.setenv("ECO_SHADOW_WORKS", work["work_id"])
    monkeypatch.setenv("ECO_GOAL_WAIT_WORKS", work["work_id"])
    yield


@pytest.fixture
def spawned(monkeypatch):
    """只观察驱动意图，在纯驱动测试中模拟起轮和消费。"""
    calls = []

    async def _fake(wid, cid, content, source):
        calls.append({"card": cid, "source": source})
        goal_driver.accept_model_request(wid, cid, source.as_json())
        goal_driver.drop_attempt(wid, cid)
    monkeypatch.setattr(goal_driver, "_spawn_goal_round", _fake)
    return calls


async def _mk_chain(work):
    """007→057 形态：自动父卡（带卡面目标）＋普通子卡＋worker 子卡。"""
    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "父卡007", "模型生成", parent=root)
    assert "error" not in r, r
    parent = r["meta"]["card_id"]
    await card_store.update_card(
        wid, parent,
        {"auto_mode": 1, "goal": "推进ACC-1", "goal_state": "active"},
        "测试置态")
    r = await card_store.create_card(wid, "普通子卡", "模型生成",
                                     parent=parent)
    assert "error" not in r, r
    child = r["meta"]["card_id"]
    r = await card_store.create_card(wid, "worker057", "模型生成",
                                     parent=parent, card_type="worker")
    assert "error" not in r, r
    worker = r["meta"]["card_id"]
    return wid, parent, child, worker


async def _goal(wid, cid):
    """卡面 goal 字段会被 service legacy 回填立成 active 目标（disarmed）
    ——drive 只推 armed 目标，回填的要先 arm；没有才 create。"""
    svc = get_service()
    g = await svc.get(wid, cid)
    if g is None:
        g = await svc.create(
            wid, cid, CreateGoalRequest(objective="推进ACC-1",
                                        max_goal_rounds=20))
    if g.activation != "armed":
        g = await svc.arm(wid, cid)
    return g


async def _dispatch(wid, parent, child, text="派工内容"):
    rec = await session_facade.append_card_message(
        wid, child, from_card=parent, text=text, msg_type="msg_directive")
    return str((rec or {}).get("msg_id") or "")


async def _receipt(wid, parent, frm, reply_to, msg_type="msg_report",
                   extra=None):
    return await session_facade.append_card_message(
        wid, parent, from_card=frm, text="回执",
        msg_type=msg_type, reply_to=reply_to, extra=extra)


def _waits(wid, cid):
    return wait_store._load(wid).get(cid) or []


# ---------------------------------------------------------------- 机械层


async def test_wait_only_zero_model_requests(work, spawned):
    """只剩已声明的等待 ⇒ 让出回合，新增模型请求为零。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    await goal_driver.drive(wid, parent)
    assert spawned == [], "只剩等待不许起模型回合"
    res = wait_store.blocking_waits(wid, parent, goal_id=g.id,
                                    revision=g.revision)
    assert len(res["held"]) == 1


async def test_correct_receipt_releases_once(work, spawned):
    """正确回执到达 ⇒ 核销、恰好续一次。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    await goal_driver.drive(wid, parent)
    assert spawned == []
    await _receipt(wid, parent, child, disp, "msg_report")
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1, "回执核销后驱动恰好续一次"
    waits = _waits(wid, parent)
    assert waits[0]["satisfied"], "等待账应有核销记录"


async def test_duplicate_receipt_not_reconsumed(work):
    """重复回执幂等：已核销的等待不会二次满足、不产新副作用。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    await _receipt(wid, parent, child, disp)
    res1 = wait_store.refresh_and_open(wid, parent, goal_id=g.id,
                                       revision=g.revision)
    assert len(res1["satisfied_now"]) == 1
    seq1 = _waits(wid, parent)[0]["satisfied"]["by_seq"]
    await _receipt(wid, parent, child, disp)   # 同内容再到一条
    res2 = wait_store.refresh_and_open(wid, parent, goal_id=g.id,
                                       revision=g.revision)
    assert res2["satisfied_now"] == []
    assert _waits(wid, parent)[0]["satisfied"]["by_seq"] == seq1


async def test_wrong_task_receipt_keeps_wait(work, spawned):
    """错任务回执（同卡连续两活的另一笔）不解除当前等待。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp_a = await _dispatch(wid, parent, child, "任务A")
    disp_b = await _dispatch(wid, parent, child, "任务B")
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp_a, from_card=child)
    await _receipt(wid, parent, child, disp_b)   # B 的回执
    await goal_driver.drive(wid, parent)
    assert spawned == [], "另一笔任务的回执不能释放当前等待"
    waits = _waits(wid, parent)
    assert waits[0]["satisfied"] is None


async def test_wrong_source_receipt_keeps_wait(work, spawned):
    """回执来源卡不符 ⇒ 不解除（登记了 from 就机械核）。"""
    wid, parent, child, worker = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    await _receipt(wid, parent, worker, disp)     # 来源错
    await goal_driver.drive(wid, parent)
    assert spawned == []


async def test_reversioned_dispatch_receipt_rejected(work, spawned):
    """[验收修·场景实录] 同一派工编号已有回执，目标换版后重新登记等待：
    旧版回执**不能**核销新版等待——版本归属认原派工上的版本印，
    缺可靠版本归属的等待记「待核实」、不拦轮次。"""
    wid, parent, child, _w = await _mk_chain(work)
    svc = get_service()
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)   # 落印 rev=g.revision
    # 目标换版（edit ⇒ revision+1）后，模型对**同一笔旧派工**重新登记等待
    g2 = await svc.edit(wid, parent, g.ref(),
                        EditGoalRequest(objective="推进ACC-1（换版）"))
    assert g2.revision == g.revision + 1
    res = await h_declare_wait(wid, parent,
                               {"task": disp, "from_card": child}, {})
    assert res["ok"], res
    assert str(res["data"]["state"]).startswith("unverified"), res["data"]
    await _receipt(wid, parent, child, disp)     # 旧版派工的回执
    await goal_driver.drive(wid, parent)
    # 「待核实」不拦轮次（与 missing_linkage 同口径）——续轮照起，
    # 但回执绝没核销掉这笔等待。
    assert len(spawned) == 1
    waits = _waits(wid, parent)
    assert waits[0]["satisfied"] is None, "旧版派工的回执核销不了新版等待"
    res = wait_store.blocking_waits(wid, parent, goal_id=g2.id,
                                    revision=g2.revision)
    assert res["held"] == []
    assert any(u.get("type") == "dispatch_rev_mismatch"
               for u in res["unverified"])
    # 对照：换版后**重新派工**（新印＝rev2）⇒ 回执照常核销续轮
    spawned.clear()
    disp2 = await _dispatch(wid, parent, child, "换版后重派")
    wait_store.declare(wid, parent, goal_id=g2.id, revision=g2.revision,
                       task_id=disp2, from_card=child)
    res = wait_store.blocking_waits(wid, parent, goal_id=g2.id,
                                    revision=g2.revision)
    assert len(res["held"]) == 1, "新版派工的等待是 bound 等待，应拦"
    await _receipt(wid, parent, child, disp2)
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1


async def test_unstamped_dispatch_marked_unverified(work, spawned):
    """派工没印上版本（本例：发派工时卡上还没有目标）⇒ 等待记「待核实」
    不拦轮次；回执自己也说不出版本 ⇒ 证不了，不解除。"""
    wid, root = work["work_id"], work["work_card_id"]
    # 无 goal 字段的父卡——legacy 回填无从发生，派工落信时印不上版本。
    r = await card_store.create_card(wid, "无目标父卡", "模型生成",
                                     parent=root)
    assert "error" not in r, r
    parent = r["meta"]["card_id"]
    await card_store.update_card(wid, parent, {"auto_mode": 1}, "置态")
    r = await card_store.create_card(wid, "子卡", "模型生成", parent=parent)
    child = r["meta"]["card_id"]
    disp = await _dispatch(wid, parent, child)   # 无目标 ⇒ 无印
    g = await _goal(wid, parent)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    res = wait_store.blocking_waits(wid, parent, goal_id=g.id,
                                    revision=g.revision)
    assert res["held"] == []
    assert any(u.get("type") == "dispatch_unstamped"
               for u in res["unverified"])
    await _receipt(wid, parent, child, disp)     # 无版本回执
    res = wait_store.refresh_and_open(wid, parent, goal_id=g.id,
                                      revision=g.revision)
    assert res["satisfied_now"] == [], "证不了版本的回执不解除"
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1, "待核实不拦独立推进"


async def test_manual_stop_blocks_resume(work, spawned):
    """人工停止优先：回执到了也不许自动续；显式解除后才恢复。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    control.halt(wid, parent, reason="人工停止", by="test")
    await _receipt(wid, parent, child, disp)
    await goal_driver.drive(wid, parent)
    assert spawned == [], "停着的卡回执不得点火"
    control.unhalt(wid, parent, by="test")
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1


async def test_receipt_before_declare_still_counts(work, spawned):
    """回执先于等待登记到达 ⇒ 登记时回扫认账，不漏接。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    await _receipt(wid, parent, child, disp)      # 先到的回执
    res = await h_declare_wait(
        wid, parent,
        {"task": disp, "from_card": child}, {})
    assert res["ok"], res
    assert res["data"]["state"] in ("satisfied", "registered")
    waits = _waits(wid, parent)
    assert waits[0]["satisfied"], "先到的回执登记后必须核上"
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1


async def test_report_and_worker_done_separate(work):
    """普通子卡走 msg_report、worker 走 worker_done；expect 收窄时
    另一类回执不顶数。"""
    wid, parent, child, worker = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp_c = await _dispatch(wid, parent, child)
    disp_w = await _dispatch(wid, parent, worker)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp_c, from_card=child,
                       expect="msg_report")
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp_w, from_card=worker,
                       expect="worker_done")
    await _receipt(wid, parent, child, disp_c, "worker_done")   # 类型错
    res = wait_store.refresh_and_open(wid, parent, goal_id=g.id,
                                      revision=g.revision)
    assert res["satisfied_now"] == [], "类型不符不得核销"
    await _receipt(wid, parent, worker, disp_w, "worker_done")
    await _receipt(wid, parent, child, disp_c, "msg_report")
    res = wait_store.refresh_and_open(wid, parent, goal_id=g.id,
                                      revision=g.revision)
    assert len(res["satisfied_now"]) == 2
    assert res["open"] == []


async def test_missing_linkage_marked_unverified(work, spawned):
    """缺 task/from 的声明 ⇒ 「待核实」，不算等待也不拦轮次。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id="", from_card="")
    res = wait_store.blocking_waits(wid, parent, goal_id=g.id,
                                    revision=g.revision)
    assert res["held"] == []
    assert any(u.get("type") == "missing_linkage"
               for u in res["unverified"])
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1, "待核实不拦独立推进"


async def test_independent_wait_does_not_hold(work, spawned):
    """independent 标：等回执期间还有独立待办 ⇒ 不挡续轮。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child, independent=True)
    res = wait_store.blocking_waits(wid, parent, goal_id=g.id,
                                    revision=g.revision)
    assert res["held"] == [] and len(res["waiting_but_free"]) == 1
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1


# ---------------------------------------------------------------- 活跃子卡对照
# [验收修] 让位判据豁免「independent 等待覆盖的活跃子卡」——改前
# _yield_to_children 先整体让位、后读等待，independent 声明到不了判据面。


async def _arm_child_goal(wid, cid):
    """给子卡立一个真实 active 目标（service.create ⇒ goal_state 投影 active）。"""
    svc = get_service()
    g = await svc.get(wid, cid)
    if g is None:
        g = await svc.create(
            wid, cid, CreateGoalRequest(objective="子卡独立目标",
                                        max_goal_rounds=5))
    return g


async def test_independent_wait_active_child_continues(work, spawned):
    """子卡真 active＋等待声明带 independent ⇒ 父卡不被整体让位，
    续轮意图照常兑现（其它活跃子卡的串行约束不松）。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    await _arm_child_goal(wid, child)            # 子卡真 active
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child, independent=True)
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1, "independent 声明过的活跃子卡不该整卡让位"


async def test_active_child_no_wait_yields(work, spawned):
    """对照：子卡真 active、父卡**没有**豁免声明 ⇒ 原有串行让位不变。"""
    wid, parent, child, _w = await _mk_chain(work)
    await _goal(wid, parent)
    await _arm_child_goal(wid, child)
    await goal_driver.drive(wid, parent)
    assert spawned == [], "没声明 independent 的活跃子卡照旧让位"


async def test_blocking_wait_active_child_holds(work, spawned):
    """对照：等它的子卡在跑（blocking 等待）⇒ 让位/拦轮等效，零请求。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    await _arm_child_goal(wid, child)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    await goal_driver.drive(wid, parent)
    assert spawned == []


# ---------------------------------------------------------------- 真实汇报入口
# [验收修] report_up → deliver_now 直唤醒：等待核对前置——错任务/重放
# 不起轮；核销的统一走 goal 驱动续轮入口（不另起卡间回合）。


def _cards(wid, *cids):
    return {c: (card_store._meta_of(wid, c) or {}) for c in cids}


@pytest.fixture
def live_deliver(monkeypatch):
    """放开 ECO_NO_AUTO_DELIVER＋桩 _run_message——要数 deliver_now
    自己到底起没起卡间回合（与驱动续轮分账）。"""
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    calls = []

    async def _fake_run(*a, **k):
        calls.append((a, k))
        return {}
    monkeypatch.setattr(_routes, "_run_message", _fake_run)
    return calls


async def test_report_up_wrong_task_no_round(work, spawned, live_deliver):
    """错任务回执经真实 report_up：等待核对在唤醒之前——零模型请求，
    汇报本体仍落父卡会话历史（pending 如实回报）。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp_a = await _dispatch(wid, parent, child, "任务A")
    disp_b = await _dispatch(wid, parent, child, "任务B")
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp_a, from_card=child)
    res = await card_tools._h_report_up(
        wid, child, {"content": "B 做完了", "task": disp_b},
        _cards(wid, parent, child))
    await asyncio.sleep(0.1)
    assert res.get("pending"), f"回执应被等待闸按住转 pending: {res}"
    assert live_deliver == [], "deliver_now 不许为错任务回执起卡间回合"
    assert spawned == [], "驱动也不许续（等待没核销）"
    assert _waits(wid, parent)[0]["satisfied"] is None


async def test_report_up_correct_receipt_via_driver(work, spawned,
                                                    live_deliver):
    """正确回执经真实 report_up：核销后统一走 goal 驱动续轮——
    _spawn_goal_round 恰一次，deliver_now 不另起卡间回合。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    res = await card_tools._h_report_up(
        wid, child, {"content": "A 做完了", "task": disp},
        _cards(wid, parent, child))
    await asyncio.sleep(0.1)
    assert res.get("delivered"), f"核销回执应算送达: {res}"
    assert live_deliver == [], "核销的续轮不走 deliver_now 的卡间回合"
    assert len(spawned) == 1, "续轮恰好一次，经驱动统一入口"
    assert _waits(wid, parent)[0]["satisfied"]


async def test_report_up_replay_held(work, spawned, live_deliver):
    """重放回执：已核销等待的同 reply_to 再来一笔 ⇒ 按住不起轮。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    await card_tools._h_report_up(
        wid, child, {"content": "第一笔", "task": disp},
        _cards(wid, parent, child))
    await asyncio.sleep(0.1)
    assert len(spawned) == 1
    spawned.clear()
    res = await card_tools._h_report_up(
        wid, child, {"content": "同一笔再投", "task": disp},
        _cards(wid, parent, child))
    await asyncio.sleep(0.1)
    assert res.get("pending"), f"重放回执应被按住: {res}"
    assert live_deliver == [] and spawned == [], \
        "已消费过的回执重放不产新副作用、不烧轮次"


async def test_wrong_receipt_across_owner_keeps_wait(work, monkeypatch, live_deliver):
    from src.infra import procs

    wid, parent, child, _ = await _mk_chain(work)
    goal = await _goal(wid, parent)
    a = await _dispatch(wid, parent, child, "任务 A")
    b = await _dispatch(wid, parent, child, "任务 B")
    wait_store.declare(wid, parent, goal_id=goal.id, revision=goal.revision,
                       task_id=a, from_card=child)
    owner = 0
    forwarded = []

    async def forward(work_id, card_id, path, body):
        nonlocal owner
        assert path == "/ctl/deliver"
        forwarded.append(body)
        owner = 1
        try:
            return await _routes.ctl_deliver(_routes.CtlDeliverRequest(**body))
        finally:
            owner = 0

    monkeypatch.setattr(procs, "is_runner", lambda: True)
    monkeypatch.setattr(procs, "runner_index", lambda: owner)
    monkeypatch.setattr(procs, "owner_index_of", lambda *_: 1)
    monkeypatch.setattr(procs, "forward_ctl", forward)
    result = await card_tools._h_report_up(
        wid, child, {"task": b, "content": "任务 B 的结果"}, _cards(wid, parent, child))
    assert result["pending"]
    assert forwarded[0]["msg_type"] == "msg_report"
    assert live_deliver == []
    assert not _waits(wid, parent)[0]["satisfied"]


async def test_new_goal_with_same_revision_cannot_claim_old_receipt(work):
    wid, parent, child, _ = await _mk_chain(work)
    service = get_service()
    old = await _goal(wid, parent)
    task = await _dispatch(wid, parent, child)
    await _receipt(wid, parent, child, task)
    await service.complete(wid, parent, old.ref())
    new = await service.create(wid, parent, CreateGoalRequest(objective="新目标"))
    assert new.id != old.id and new.revision == old.revision
    wait_store.declare(wid, parent, goal_id=new.id, revision=new.revision,
                       task_id=task, from_card=child)
    state = wait_store.blocking_waits(wid, parent, goal_id=new.id, revision=new.revision)
    assert not state["ready"]
    assert any(event["type"] == "dispatch_goal_mismatch" for event in state["unverified"])


async def test_wait_tool_scope_disabled_by_default(work, monkeypatch):
    wid, parent, child, _ = await _mk_chain(work)
    goal = await _goal(wid, parent)
    task = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=goal.id, revision=goal.revision,
                       task_id=task, from_card=child)
    monkeypatch.delenv("ECO_GOAL_WAIT_WORKS")
    tools = card_tools.build_tool_schemas(card_id=parent, work_id=wid,
                                           cards=card_store.iter_card_metas(wid))
    assert not any(tool["name"] == "declare_wait" for tool in tools)
    assert (await h_declare_wait(wid, parent, {"task": task, "from_card": child}, {}))["error"]["code"] == "denied"
    assert await wait_store.receipt_gate(wid, parent) == "normal"


async def test_wait_releases_serial_lock_before_and_after_receipt(work, spawned, monkeypatch):
    from src.api import serial_gate

    wid, parent, child, _ = await _mk_chain(work)
    goal = await _goal(wid, parent)
    task = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=goal.id, revision=goal.revision,
                       task_id=task, from_card=child)
    monkeypatch.setattr(serial_gate, "is_serial", lambda: True)
    await serial_gate.acquire(wid, parent)
    await goal_driver.drive(wid, parent)
    assert not spawned and not serial_gate.holder_of(wid)
    await _receipt(wid, parent, child, task)
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1
    await serial_gate.acquire(wid, parent)
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1 and not serial_gate.holder_of(wid)


async def test_real_wait_and_receipt_resume_once(work, monkeypatch):
    import json
    from src.api import message_runner, model_service
    from tests import async_settle

    wid, parent, child, _ = await _mk_chain(work)
    await _goal(wid, parent)
    a = await _dispatch(wid, parent, child, "任务 A")
    b = await _dispatch(wid, parent, child, "任务 B")
    await card_store.update_card(wid, child, {"goal_state": "active"}, "子卡承担其它活")
    calls = []
    tasks = []
    real_spawn = message_runner._spawn_tracked

    def track(coro, **kwargs):
        task = real_spawn(coro, **kwargs)
        if kwargs.get("card_id") == parent:
            tasks.append(task)
        return task

    async def model(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ({"content": "", "tool_calls": [{"name": "declare_wait", "arguments": {
                "task": a, "from_card": child, "expect": "msg_report"}}]}, "fake-model")
        return ({"content": "已阅。", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(message_runner, "_spawn_tracked", track)
    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    await _routes._run_message(wid, parent, "等任务 A 的汇报", None, source_kind="human")
    state = goal_driver.state_of(wid, parent)
    if state.run is not None:
        await state.run
    assert len(calls) == 2 and not tasks
    assert _waits(wid, parent)[0]["task_id"] == a

    cards = _cards(wid, parent, child)
    wrong = await card_tools._h_report_up(wid, child, {"task": b, "content": "B 已完成"}, cards)
    assert wrong["pending"] and len(calls) == 2 and not tasks
    correct = await card_tools._h_report_up(wid, child, {"task": a, "content": "A 已完成"}, cards)
    assert correct["delivered"]
    await async_settle.wait_until(lambda: bool(tasks), what="正确回执应触发属主续轮")
    await message_runner.await_spawned(tasks[0])
    if state.run is not None:
        await state.run
    assert len(tasks) == 1 and len(calls) == 3
    assert _waits(wid, parent)[0]["satisfied"]["resumed"]

    replay = await card_tools._h_report_up(wid, child, {"task": a, "content": "A 再次汇报"}, cards)
    assert replay["pending"] and len(tasks) == 1
    rows = [json.loads(line) for line in shadow_eval._log_path(wid).read_text(
        encoding="utf-8").splitlines()]
    requests = [row for row in rows if row["decision"] == "model_request"]
    assert len(requests) == len(calls)
    assert sum(row["source"] == "goal" for row in requests) == 1


async def test_worker_dispatch_and_real_completion_resume_once(work, monkeypatch):
    from src.api import message_runner, model_service
    from src.goal import authority
    from tests import async_settle

    wid, parent, _, _ = await _mk_chain(work)
    goal = await _goal(wid, parent)
    real_run = _routes._run_message
    configured = asyncio.Event()
    completed = asyncio.Event()
    requests = []

    async def run(work_id, card_id, *args, **kwargs):
        source = kwargs.get("source") or {}
        if source.get("kind") != "worker_dispatch":
            return await real_run(work_id, card_id, *args, **kwargs)
        await configured.wait()
        token = authority.set_turn_source(source)
        try:
            report = await card_tools._h_report_done(
                wid, card_id, {"summary": "任务已完成"},
                _cards(wid, parent, card_id))
            assert report.get("receipt_msg_id"), report
            completed.set()
            return report
        finally:
            authority.reset_turn_source(token)

    async def model(**kwargs):
        requests.append(kwargs)
        return ({"content": "已看到 worker 的回执。", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(_routes, "_run_message", run)
    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    created = await card_tools._h_create_worker_card(
        wid, parent, {"title": "执行卡", "task": "核对一个产物"},
        card_store.iter_card_metas(wid))
    assert created["started"] and created["task_id"]
    wait_store.declare(wid, parent, goal_id=goal.id, revision=goal.revision,
                       task_id=created["task_id"], from_card=created["card_id"],
                       expect="worker_done")
    await goal_driver.drive(wid, parent)
    assert not requests
    configured.set()
    await asyncio.wait_for(completed.wait(), timeout=5)
    await async_settle.wait_until(lambda: bool(requests), what="worker 回执恢复父卡")
    state = goal_driver.state_of(wid, parent)
    await async_settle.wait_until(lambda: state.attempt is None, what="父卡续轮结束")
    if state.run is not None:
        await state.run
    assert len(requests) == 1
    assert _waits(wid, parent)[0]["satisfied"]["resumed"]


async def test_stale_goal_wait_ignored(work, spawned):
    """旧 (goal_id, revision) 的等待留档不点火——换版后不挡当前目标。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id="g_old", revision=0,
                       task_id=disp, from_card=child)
    res = wait_store.blocking_waits(wid, parent, goal_id=g.id,
                                    revision=g.revision)
    assert res["held"] == [] and res["stale"]
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1


async def test_receipt_noise_burst_not_lost(work, spawned):
    """回执被 400 条噪声包尾也不丢：游标按类型过滤＋增量，不截尾。"""
    wid, parent, child, _w = await _mk_chain(work)
    g = await _goal(wid, parent)
    disp = await _dispatch(wid, parent, child)
    wait_store.declare(wid, parent, goal_id=g.id, revision=g.revision,
                       task_id=disp, from_card=child)
    await _receipt(wid, parent, child, disp)
    for i in range(400):
        event_stream.emit(wid, parent, "turn_end", {"i": i})
        event_stream.emit(wid, parent, "context", {"i": i})
    await goal_driver.drive(wid, parent)
    assert len(spawned) == 1, "回执埋在噪声尾也必须核销续轮"
    waits = _waits(wid, parent)
    assert waits[0]["satisfied"]


# ---------------------------------------------------------------- 工具面


async def test_declare_tool_gates(work):
    """declare_wait 只在自动模式＋有目标时受理；expect 白名单。"""
    wid, parent, child, _w = await _mk_chain(work)
    # 自动但没目标 ⇒ no_goal（另造一张只有 auto_mode 的卡）
    r = await card_store.create_card(wid, "空目标自动卡", "模型生成",
                                     parent=work["work_card_id"])
    _no_goal = r["meta"]["card_id"]
    await card_store.update_card(wid, _no_goal, {"auto_mode": 1}, "置态")
    r = await h_declare_wait(wid, _no_goal, {"task": "cm_1"}, {})
    assert not r["ok"] and r["error"]["code"] == "no_goal"
    await _goal(wid, parent)
    # 坏 expect ⇒ validation
    r = await h_declare_wait(
        wid, parent, {"task": "cm_1", "from_card": child,
                      "expect": "msg_ask"}, {})
    assert not r["ok"] and r["error"]["code"] == "validation"
    # 正常 ⇒ ok 且落账
    r = await h_declare_wait(
        wid, parent, {"task": "cm_1", "from_card": child,
                      "expect": "msg_report"}, {})
    assert r["ok"] and r["data"]["wait_id"].startswith("wait_")
    assert _waits(wid, parent)


async def test_declare_tool_manual_card_rejected(work):
    """手动卡声明等待 ⇒ 拒绝（等待是自动卡的契约）。"""
    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "手动卡", "模型生成", parent=root)
    cid = r["meta"]["card_id"]
    res = await h_declare_wait(wid, cid, {"task": "cm_1"}, {})
    assert not res["ok"] and res["error"]["code"] == "validation"
