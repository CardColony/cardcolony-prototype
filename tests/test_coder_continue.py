# -*- coding: utf-8 -*-
"""coder 卡在**自动模式**里能改目标、能重新激活（不是靠人点按钮）。

[2026-10-21 · 用户令]「我说的不是给我按钮，我说的是在自动模式下，让模型能更新
goal（哪怕是 complete 状态），并重新激活这张 coder 卡，否则明明是继续的任务，
它也只能新开卡执行」。

改前的三处硬拦（都在代码里，不是模型不愿意）：
1. ``authority.require_direct_human``：``update_goal(edit/pause/resume)`` 只允许
   **直接人类回合**，自动续轮里模型改不了自己的目标；
2. ``GoalService.resume`` 的允许集合不含 ``complete``；``resume_child`` 明写
   「已完成不能恢复，要新任务请新建子卡」；
3. ``post_work_message`` 对 ``state=completed`` 的 coder 直接 400「不再激活；
   新任务请新建编程卡」，K3 调起也跳过它。

本文件钉住：自动续轮里 edit/resume 可用、complete 能重新激活、完工卡收到消息
即重新派活。
"""
from __future__ import annotations

import pytest

from src.api import card_tools, routes
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
    r = await card_store.create_card(wid, "coder 卡", "模型生成", parent=root,
                                     card_type="coder")
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


async def test_message_to_completed_coder_revives_it(work, client, monkeypatch):
    """给完工 coder 发消息＝重新派活：不再 400，卡复位、目标重开。"""
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
    child = await card_store.create_card(wid, "子 coder", "模型生成", parent=pcid,
                                         card_type="coder")
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
