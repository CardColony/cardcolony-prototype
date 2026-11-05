# -*- coding: utf-8 -*-
"""worker 卡在**自动模式**里能改目标、能重新激活（不是靠人点按钮）。

[2026-10-21 · 用户令]「我说的不是给我按钮，我说的是在自动模式下，让模型能更新
goal（哪怕是 complete 状态），并重新激活这张 worker 卡，否则明明是继续的任务，
它也只能新开卡执行」。

改前的三处硬拦（都在代码里，不是模型不愿意）：
1. ``authority.require_direct_human``：``update_goal(edit/pause/resume)`` 只允许
   **直接人类回合**，自动续轮里模型改不了自己的目标；
2. ``GoalService.resume`` 的允许集合不含 ``complete``；``resume_child`` 明写
   「已完成不能恢复，要新任务请新建子卡」；
3. ``post_work_message`` 对 ``state=completed`` 的 worker 直接 400「不再激活；
   新任务请新建worker 卡」，K3 调起也跳过它。

本文件钉住：自动续轮里 edit/resume 可用、complete 能重新激活、完工卡收到消息
即重新派活。
"""
from __future__ import annotations

import asyncio

import pytest

from src.api import card_tools, control, routes
from src.goal import authority
from src.goal import tools as goal_tools
from src.goal.service import get_service
from src.storage import card_store


@pytest.fixture(autouse=True)
def _clean_goal_state():
    """goal 服务是进程单例，跨用例会带着上一个沙箱的事件 seq ⇒ 必须清。"""
    from src.api import message_runner, serial_gate

    def _reset():
        get_service()._caches.clear()
        serial_gate.reset()
        message_runner._TRIGGER_TASKS.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def auto_round():
    """模拟**自动续轮**（source.kind=goal）——改前 edit/resume 在这种回合里被拒。"""
    token = authority.set_turn_source(
        {"kind": "goal", "goalId": "g-test", "revision": 1, "round": 1})
    yield
    authority.reset_turn_source(token)


async def _mk_coder(work, *, goal: str = "旧目标", phase: str = "active",
                    state: str = "active") -> tuple[str, str]:
    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "worker 卡", "模型生成", parent=root,
                                     card_type="worker")
    assert "error" not in r, r
    cid = r["meta"]["card_id"]
    await card_store.update_card(
        wid, cid, {"auto_mode": 1, "goal": goal, "goal_state": phase, "state": state},
        "测试置态")
    return wid, cid


async def _update_goal(wid: str, cid: str, **args) -> dict:
    svc = get_service()
    goal = await svc.get(wid, cid)
    payload = {"goal_id": goal.id, "revision": goal.revision}
    payload.update(args)
    cards = card_tools._collect_card_meta(wid)
    return await goal_tools.h_update_goal(wid, cid, payload, cards)


# ---------------------------------------------------------------- 自动续轮里改目标


async def test_edit_goal_allowed_in_auto_round(work, auto_round):
    """自动续轮（不是人类回合）里，模型也能改自己的目标。"""
    wid, cid = await _mk_coder(work, goal="旧目标", phase="active")

    r = await _update_goal(wid, cid, action="edit", objective="新目标：接着做 Y")
    assert r["ok"] is True, r

    goal = await get_service().get(wid, cid)
    assert goal.objective == "新目标：接着做 Y"
    assert goal.phase == "active"


async def test_edit_on_complete_starts_new_lifecycle(work, auto_round):
    """complete 状态下 edit ⇒ 新目标 + 重新激活（不是「保持 complete 改个字」）。"""
    wid, cid = await _mk_coder(work, goal="旧目标", phase="complete")

    r = await _update_goal(wid, cid, action="edit", objective="后续：把 Y 也做掉")
    assert r["ok"] is True, r

    goal = await get_service().get(wid, cid)
    assert goal.phase == "active", "complete 改了目标就必须能继续跑"
    assert goal.objective == "后续：把 Y 也做掉"
    meta = card_store._meta_of(wid, cid)
    assert meta["goal_state"] == "active"
    assert int(meta.get("goal_rounds") or 0) == 0, "新生命周期 ⇒ 轮次归零"


async def test_edit_on_paused_also_reactivates(work, auto_round):
    """paused 时改目标 ⇒ 顺手激活（只改不激活＝改了也不跑）。"""
    wid, cid = await _mk_coder(work, goal="旧目标", phase="paused")
    r = await _update_goal(wid, cid, action="edit", objective="新目标")
    assert r["ok"] is True, r
    goal = await get_service().get(wid, cid)
    assert goal.phase == "active"
    assert goal.objective == "新目标"


async def test_resume_on_complete_reactivates_same_objective(work, auto_round):
    """resume 对 complete：目标内容不变，重新开一轮。"""
    wid, cid = await _mk_coder(work, goal="把 X 做完", phase="complete")

    r = await _update_goal(wid, cid, action="resume")
    assert r["ok"] is True, r

    goal = await get_service().get(wid, cid)
    assert goal.phase == "active"
    assert goal.objective == "把 X 做完"


async def test_pause_still_requires_human_round(work, auto_round):
    """pause 仍然只认人类回合——停是人的决定，这条不放宽。"""
    wid, cid = await _mk_coder(work, goal="旧目标", phase="active")
    r = await _update_goal(wid, cid, action="pause")
    assert r["ok"] is False
    assert r["error"]["code"] == "GOAL_TOOL_AUTHORITY_REQUIRED", r


# ---------------------------------------------------------------- 完工卡重新派活


async def test_message_to_completed_worker_revives_it(work, client, monkeypatch):
    """给完工 worker 发消息＝重新派活：不再 400，卡复位、目标重开。"""
    wid, cid = await _mk_coder(work, goal="旧目标", phase="complete", state="completed")

    async def _fake_run(*a, **kw):
        return {"reply": "好，接着做", "model_used": "fake"}

    monkeypatch.setattr(routes, "_run_message", _fake_run)

    r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                          json={"content": "接着做 Y"})
    assert r.status_code == 200, r.text

    meta = card_store._meta_of(wid, cid)
    assert meta["state"] == "active", "完工标记必须复位，否则 report_done/调起还会拦"
    goal = await get_service().get(wid, cid)
    assert goal.phase == "active"
    assert goal.objective == "接着做 Y", "新派的任务就是新目标"


async def test_resume_child_accepts_completed_child(work):
    """父卡给**已完工**的子卡派活：resume_child 直接重新激活，不必新建子卡。"""
    wid, root = work["work_id"], work["work_card_id"]
    parent = await card_store.create_card(wid, "父卡", "模型生成", parent=root)
    pcid = parent["meta"]["card_id"]
    child = await card_store.create_card(wid, "子 worker", "模型生成", parent=pcid,
                                         card_type="worker")
    ccid = child["meta"]["card_id"]
    await card_store.update_card(
        wid, ccid, {"auto_mode": 1, "goal": "子目标", "goal_state": "complete",
                    "state": "completed"}, "测试置态")

    cards = card_tools._collect_card_meta(wid)
    r = await goal_tools.h_resume_child(wid, pcid, {"card_id": ccid}, cards)
    assert r["ok"] is True, r
    goal = await get_service().get(wid, ccid)
    assert goal.phase == "active"
    assert goal.objective == "子目标"


# ---------------------------------------------------------------- 唤醒不看模式
# [2026-11-01 · 用户令「唤醒子卡不需要自动」] 实测 work_027/card_140：
# 手动 worker 被 stop_card(halt) 停在中途后，state 恒为 active、halt 标记在位，
# 父卡的续派/唤醒全部无路（continue_task 嫌 state=active、resume_child 嫌
# auto_mode=0、内部消息被 halt 拒掉）。以下钉住这条链已经通了。


async def test_resume_child_works_on_manual_halted_child(work, monkeypatch):
    """手动子卡被强制停止后：resume_child 解除停止标记＋恢复目标相位＋亲手点一轮。"""
    wid, root = work["work_id"], work["work_card_id"]
    parent = await card_store.create_card(wid, "父卡", "模型生成", parent=root)
    pcid = parent["meta"]["card_id"]
    child = await card_store.create_card(wid, "子 worker", "模型生成", parent=pcid,
                                         card_type="worker")
    ccid = child["meta"]["card_id"]
    await card_store.update_card(
        wid, ccid, {"auto_mode": 0, "goal": "子目标", "goal_state": "paused",
                    "state": "active"}, "测试置态：手动卡被停在中途")
    control.halt(wid, ccid, reason="测试：强制停止", by=pcid)
    assert control.is_halted(wid, ccid) is not None

    calls = []

    async def fake_run(*a, **kw):
        calls.append({"a": a, "kw": kw})
        return {"ok": True}

    monkeypatch.setattr(routes, "_run_message", fake_run)

    cards = card_tools._collect_card_meta(wid)
    r = await goal_tools.h_resume_child(wid, pcid, {"card_id": ccid}, cards)
    assert r["ok"] is True, r
    assert control.is_halted(wid, ccid) is None, "resume 必须解除停止标记"
    goal = await get_service().get(wid, ccid)
    assert goal.phase == "active"
    assert r["data"]["ignited"] is True, "手动卡没有续轮驱动，必须亲手点火"
    await asyncio.sleep(0.05)
    assert calls, "手动卡 resume 后必须真起一轮，不能只摆相位"


async def test_resume_child_manual_busy_does_not_double_fire(work):
    """手动子卡正在跑：resume 摆正相位但不重复点火（不打断在途回合）。"""
    wid, root = work["work_id"], work["work_card_id"]
    parent = await card_store.create_card(wid, "父卡", "模型生成", parent=root)
    pcid = parent["meta"]["card_id"]
    child = await card_store.create_card(wid, "子 worker", "模型生成", parent=pcid,
                                         card_type="worker")
    ccid = child["meta"]["card_id"]
    await card_store.update_card(
        wid, ccid, {"auto_mode": 0, "goal": "子目标", "goal_state": "paused",
                    "state": "active"}, "测试置态")

    from src.storage import session_facade
    assert session_facade.turn_try_acquire(wid, ccid, "turn:test")
    try:
        cards = card_tools._collect_card_meta(wid)
        r = await goal_tools.h_resume_child(wid, pcid, {"card_id": ccid}, cards)
        assert r["ok"] is True, r
        assert r["data"]["resumed"] is True
        assert r["data"]["ignited"] is False, "在途回合不该被再点一轮"
    finally:
        session_facade.turn_release(wid, ccid, "turn:test")


async def test_continue_task_on_halted_active_coder(work, monkeypatch):
    """被 halt 停在中途的 worker（state 恒为 active）也能续派：
    解除停止标记、新任务写进目标（不是只 resume 旧文本）、点火。"""
    wid, root = work["work_id"], work["work_card_id"]
    parent = await card_store.create_card(wid, "父卡", "模型生成", parent=root)
    pcid = parent["meta"]["card_id"]
    child = await card_store.create_card(wid, "子 worker", "模型生成", parent=pcid,
                                         card_type="worker")
    ccid = child["meta"]["card_id"]
    await card_store.update_card(
        wid, ccid, {"auto_mode": 0, "goal": "旧任务", "goal_state": "paused",
                    "state": "active"}, "测试置态：halt 后 state 仍是 active")
    control.halt(wid, ccid, reason="测试：父卡叫停", by=pcid)

    calls = []

    async def fake_run(*a, **kw):
        calls.append(a)
        return {"ok": True}

    monkeypatch.setattr(routes, "_run_message", fake_run)

    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_continue_task(
        wid, pcid, {"card_id": ccid, "task": "更正后的新任务全文"}, cards)
    assert r["ok"] is True, r
    assert control.is_halted(wid, ccid) is None, "续派必须解除停止标记"
    goal = await get_service().get(wid, ccid)
    assert goal.phase == "active"
    assert goal.objective == "更正后的新任务全文", (
        "续派文本必须写成新目标——只 resume 不改文本，跑的还是旧任务")
    await asyncio.sleep(0.05)
    assert calls, "续派必须点火"


async def test_continue_task_still_rejects_card_with_inflight_turn(work):
    """反证：有在途回合的卡仍拒续派——「防双派」闸保留，判据改成「真在跑」。"""
    wid, root = work["work_id"], work["work_card_id"]
    parent = await card_store.create_card(wid, "父卡", "模型生成", parent=root)
    pcid = parent["meta"]["card_id"]
    child = await card_store.create_card(wid, "子 worker", "模型生成", parent=pcid,
                                         card_type="worker")
    ccid = child["meta"]["card_id"]
    await card_store.update_card(
        wid, ccid, {"auto_mode": 0, "goal": "在跑的任务", "goal_state": "active",
                    "state": "active"}, "测试置态：真在跑")

    from src.storage import session_facade
    assert session_facade.turn_try_acquire(wid, ccid, "turn:test")
    try:
        cards = card_tools._collect_card_meta(wid)
        r = await card_tools._h_continue_task(
            wid, pcid, {"card_id": ccid, "task": "抢派"}, cards)
        assert r["ok"] is False, f"在途回合中的卡续派应被拒: {r}"
        assert r["error"]["code"] == "validation", r
    finally:
        session_facade.turn_release(wid, ccid, "turn:test")


async def test_continue_task_rejects_driven_auto_coder(work):
    """反证：auto 卡目标 active+armed＝驱动随时续轮——也算在跑，续派会撞车。"""
    wid, root = work["work_id"], work["work_card_id"]
    parent = await card_store.create_card(wid, "父卡", "模型生成", parent=root)
    pcid = parent["meta"]["card_id"]
    child = await card_store.create_card(wid, "子 worker", "模型生成", parent=pcid,
                                         card_type="worker")
    ccid = child["meta"]["card_id"]
    await card_store.update_card(
        wid, ccid, {"auto_mode": 1, "goal": "在跑的任务", "goal_state": "active",
                    "state": "active"}, "测试置态：auto 卡被驱动")

    svc = get_service()
    await svc.arm(wid, ccid)  # 授续轮权（activation 是进程态，不落盘）
    goal = await svc.get(wid, ccid)
    assert goal is not None and goal.phase == "active" \
        and goal.activation == "armed", "夹具应造成「被驱动的 auto 卡」"
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_continue_task(
        wid, pcid, {"card_id": ccid, "task": "抢派"}, cards)
    assert r["ok"] is False, f"auto 卡被驱动中续派应被拒: {r}"
    assert r["error"]["code"] == "validation", r
