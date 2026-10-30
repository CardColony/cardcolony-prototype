# -*- coding: utf-8 -*-
"""[停止不看模式 2026-10-29] 用户令「停止为什么要分自动还是手动？」

改前 ``control.stop_auto`` 循环第一行是 ``if not auto_mode: continue`` ——
**手动卡按下停止时，它的目标根本不会被暂停**。实测 work_027/card_061
（auto_mode=0）连按三次停止，goal 域零 pause 事件，``goal_state`` 仍是 active，
界面一直显示「推进中」，用户感受就是「不论如何也停不下来」。

``auto_mode`` 只该决定「要不要自动续轮」，**不该决定「能不能被停」**。
本文件钉住：手动卡的目标同样必须被暂停。
"""
from __future__ import annotations

from src.api import control
from src.goal import service as goal_service
from src.goal.types import CreateGoalRequest
from src.storage import card_store


async def _mk(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title=title, parent=parent,
                                     source="用户直接创建")
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_stop_pauses_goal_of_manual_card(work):
    """手动卡（auto_mode=0）有 active 目标 ⇒ 停止必须把它暂停。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "手动卡", root)

    gs = goal_service.get_service()
    await gs.create(wid, cid, CreateGoalRequest(objective="测试目标：停得下来"))

    # 明确切成手动（建卡继承可能是 0，显式写一次保证前提）
    await card_store.update_card(wid, cid, {"auto_mode": 0}, "切手动")
    assert card_store._meta_of(wid, cid)["auto_mode"] == 0

    g = await gs.get(wid, cid)
    assert g is not None and g.phase == "active", "本用例前提：目标处于 active"

    res = await control.stop_auto(wid, cid)

    assert cid in res["paused"], f"手动卡的目标没被暂停：{res}"
    g2 = await gs.get(wid, cid)
    assert g2 is not None and g2.phase == "paused", \
        f"goal 域权威仍是 {getattr(g2, 'phase', None)}（停止没写事件）"
    assert card_store._meta_of(wid, cid)["goal_state"] == "paused", \
        "投影列没跟着变成 paused"


async def test_stop_pauses_goal_of_auto_card(work):
    """自动卡的老行为不许退化：仍然要暂停。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "自动卡", root)

    gs = goal_service.get_service()
    await gs.create(wid, cid, CreateGoalRequest(objective="自动卡目标"))
    await card_store.update_card(wid, cid, {"auto_mode": 1}, "开自动")

    res = await control.stop_auto(wid, cid)
    assert cid in res["paused"]
    g = await gs.get(wid, cid)
    assert g is not None and g.phase == "paused"


async def test_stop_without_goal_is_noop(work):
    """没有目标的卡：停止不制造目标、不报错、返回形状不变。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "无目标卡", root)

    res = await control.stop_auto(wid, cid)
    assert res == {"paused": [], "lock_released": res["lock_released"]}
    g = await goal_service.get_service().get(wid, cid)
    assert g is None or g.phase != "active"
