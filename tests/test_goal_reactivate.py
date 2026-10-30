# -*- coding: utf-8 -*-
"""coder 卡：可以改目标，可以重新激活任务。

[2026-10-21 · 用户令]「再改一个，coder 卡，可以更新目标，可以重新激活任务」。

改前是两条死胡同（实测 work_018 里 7 张 coder 卡全是 ``goal_state=complete``）：
  · ``POST /auto/resume`` 对 ``complete`` 直接 400「已完成的目标不能恢复」；
  · ``ensure_goal_from_message`` 在 ``goal`` 非空时早退 ⇒ 再发消息也不会动；
  · 右栏「恢复自动推进」按钮只在 paused/blocked 显示，目标只读不可改。
⇒ 跑完的卡想接着干，只能新建一张卡。

本文件钉住四条：
1. ``complete`` 能被重新激活（用同一 objective 新建 goal，轮次归零）；
2. HTTP ``/auto/resume`` 对 ``complete`` 返回 200（不再是 400）；
3. ``/goal edit`` 改完目标后卡会按新目标跑起来；
4. 非 auto 卡一概不碰。
"""
from __future__ import annotations

import pytest

from src.api import auto_driver
from src.storage import card_store


@pytest.fixture(autouse=True)
def _clean_goal_state():
    """goal 服务是进程单例，跨用例会带着上一个沙箱的事件 seq / 缓存 ⇒ 必须清。"""
    from src.api import message_runner, serial_gate
    from src.goal.service import get_service

    def _reset():
        get_service()._caches.clear()
        serial_gate.reset()
        message_runner._TRIGGER_TASKS.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def kicks(monkeypatch):
    """拦掉真实起轮，只记录「确实点火了」。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(auto_driver, "kick", lambda w, c: calls.append((w, c)))
    return calls


async def _mk_coder(work, *, state: str = "complete", goal: str = "把 X 做完",
                    auto: bool = True) -> tuple[str, str]:
    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "coder 卡", "模型生成", parent=root,
                                     card_type="coder")
    assert "error" not in r, r
    cid = r["meta"]["card_id"]
    await card_store.update_card(
        wid, cid,
        {"auto_mode": 1 if auto else 0, "goal": goal, "goal_state": state},
        "测试置态")
    return wid, cid


# ---------------------------------------------------------------- 1：complete → active


async def test_reactivate_complete_creates_new_active_goal(work, kicks):
    """跑完的卡重新激活 ⇒ 同一目标、新生命周期（轮次归零）、立刻起轮。"""
    wid, cid = await _mk_coder(work, state="complete", goal="把 X 做完")
    kicks.clear()

    ok = await auto_driver.reactivate_card(wid, cid)
    assert ok is True

    meta = card_store._meta_of(wid, cid)
    assert meta["goal_state"] == "active", "complete 必须能回到 active"
    assert str(meta["goal"]).strip() == "把 X 做完", "重新激活不改目标内容"
    assert int(meta.get("goal_rounds") or 0) == 0, "新生命周期 ⇒ 轮次归零"
    assert (wid, cid) in kicks, "重新激活必须真的点火（否则看着激活、实际不动）"


async def test_reactivate_paused_card_still_uses_resume(work, kicks):
    """paused/blocked 走原 resume 路径（清等待位与活锁计数）。"""
    wid, cid = await _mk_coder(work, state="paused")
    await card_store.update_card(wid, cid, {"goal_await_wakes": 3}, "造活锁计数")
    kicks.clear()

    ok = await auto_driver.reactivate_card(wid, cid)
    assert ok is True
    meta = card_store._meta_of(wid, cid)
    assert meta["goal_state"] == "active"
    assert int(meta.get("goal_await_wakes") or 0) == 0
    assert (wid, cid) in kicks


async def test_reactivate_ignores_non_auto_card(work, kicks):
    """非 auto 卡：一个字都不许动。"""
    wid, cid = await _mk_coder(work, state="complete", auto=False)
    kicks.clear()
    ok = await auto_driver.reactivate_card(wid, cid)
    assert ok is False
    assert card_store._meta_of(wid, cid)["goal_state"] == "complete"
    assert not kicks


# ---------------------------------------------------------------- 2：HTTP 端点


async def test_resume_endpoint_accepts_complete(work, client, kicks):
    """``POST /auto/resume`` 对 complete 返回 200（改前是 400）。"""
    wid, cid = await _mk_coder(work, state="complete")
    kicks.clear()

    r = await client.post(f"/api/works/{wid}/cards/{cid}/auto/resume")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resumed"] is True
    assert body["from"] == "complete"
    assert card_store._meta_of(wid, cid)["goal_state"] == "active"


async def test_resume_endpoint_still_idempotent_when_active(work, client):
    wid, cid = await _mk_coder(work, state="active")
    r = await client.post(f"/api/works/{wid}/cards/{cid}/auto/resume")
    assert r.status_code == 200
    assert r.json()["resumed"] is False


# ---------------------------------------------------------------- 3：改目标


async def test_goal_edit_reactivates_complete_card(work, client, kicks):
    """``/goal edit <新目标>``：目标换了，而且卡按新目标跑起来。"""
    wid, cid = await _mk_coder(work, state="complete", goal="旧目标")
    kicks.clear()

    r = await client.post(f"/api/works/{wid}/cards/{cid}/goal",
                          json={"input": "edit 新目标：把 Y 也做掉"})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True, r.text

    meta = card_store._meta_of(wid, cid)
    assert str(meta["goal"]).strip() == "新目标：把 Y 也做掉"
    assert meta["goal_state"] == "active", "改完目标必须处于 active（否则改了也不跑）"
    assert (wid, cid) in kicks, "改完目标要立刻起跑"


async def test_goal_edit_does_not_touch_non_auto_card(work, client, kicks):
    wid, cid = await _mk_coder(work, state="complete", auto=False)
    kicks.clear()
    r = await client.post(f"/api/works/{wid}/cards/{cid}/goal",
                          json={"input": "edit 随便改"})
    assert r.status_code == 200
    assert not kicks, "非 auto 卡不许被点火"


def test_round_prompt_uses_card_goal_not_stale_copy():
    """续轮念的是**卡面那段文字**，不是 goal 域里 fold 出来的旧副本。

    用户 2026-10-28：「不以我发给它对话框的文字为goal，而以goal区域内的文字为goal」。
    两份并存（卡面 / goal 域）时以卡面为准，否则「改了卡面，续轮还在念旧目标」。
    """
    from src.goal.prompt import render_goal_round_prompt
    from src.goal.types import GoalView

    stale = GoalView(id="g1", revision=1, objective="旧目标：已经过时",
                     phase="active", max_goal_rounds=10, rounds_started=0,
                     created_at=0.0, updated_at=0.0, activation="armed")
    text = render_goal_round_prompt(stale, 1, objective="新目标：按这个干")
    assert "新目标：按这个干" in text
    assert "旧目标" not in text
    # 不给 objective ⇒ 退回 goal 域（旧行为不变）
    assert "旧目标" in render_goal_round_prompt(stale, 1)


# ---------------------------------------------------------------- 5：重启恢复


async def test_arm_restores_activation_for_active_goal(work, kicks):
    """重启后重新授权：`phase=active` 的目标能被重新 arm 回来。

    守的是用户实测的那句「goal 明明没完成，但模型却停下来了」——
    `activation` 永不落盘，不重新 arm 的话重启一次就静默挂死。
    """
    from src.goal.service import get_service

    wid, cid = await _mk_coder(work, state="active", goal="目标X", auto=True)
    svc = get_service()
    await svc.disarm(wid, cid)
    assert svc.is_armed(wid, cid) is False

    view = await svc.arm(wid, cid)
    assert view is not None
    assert svc.is_armed(wid, cid) is True


async def test_arm_refuses_non_active_goal(work):
    """`complete` / `paused` 不给权——它们本来就不该自动跑。"""
    from src.goal.service import get_service

    wid, cid = await _mk_coder(work, state="complete", goal="X", auto=True)
    svc = get_service()
    assert await svc.arm(wid, cid) is None
    assert svc.is_armed(wid, cid) is False


async def test_rearm_skips_card_without_goal_text(work):
    """卡面 goal 为空 ⇒ 不恢复（目标已经搬成卡面一块，空着就是没目标）。"""
    from src.goal import driver as goal_driver
    from src.goal.service import get_service

    wid, cid = await _mk_coder(work, state="active", goal="", auto=True)
    await goal_driver.rearm_active_goals()
    assert get_service().is_armed(wid, cid) is False


# ---------------------------------------------------------------- 5：重启恢复


async def test_arm_restores_activation_for_active_goal(work, kicks):
    """重启后重新授权：`phase=active` 的目标能被重新 arm 回来。

    守的是用户实测的那句「goal 明明没完成，但模型却停下来了」——
    `activation` 永不落盘，不重新 arm 的话重启一次就静默挂死。
    """
    from src.goal.service import get_service

    wid, cid = await _mk_coder(work, state="active", goal="目标X", auto=True)
    svc = get_service()
    await svc.disarm(wid, cid)
    assert svc.is_armed(wid, cid) is False

    view = await svc.arm(wid, cid)
    assert view is not None
    assert svc.is_armed(wid, cid) is True


async def test_arm_refuses_non_active_goal(work):
    """`complete` / `paused` 不给权——它们本来就不该自动跑。"""
    from src.goal.service import get_service

    wid, cid = await _mk_coder(work, state="complete", goal="X", auto=True)
    svc = get_service()
    assert await svc.arm(wid, cid) is None
    assert svc.is_armed(wid, cid) is False


async def test_rearm_skips_card_without_goal_text(work):
    """卡面 goal 为空 ⇒ 不恢复（目标已经搬成卡面一块，空着就是没目标）。"""
    from src.goal import driver as goal_driver
    from src.goal.service import get_service

    wid, cid = await _mk_coder(work, state="active", goal="", auto=True)
    await goal_driver.rearm_active_goals()
    assert get_service().is_armed(wid, cid) is False
