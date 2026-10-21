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
