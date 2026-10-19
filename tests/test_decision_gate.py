# -*- coding: utf-8 -*-
"""收口定论闸：complete 前本卡正文必须至少有一条定论。

[画布 2026-10-19] 定论（`D{nn}【裁定/决定/收敛】…`）是这张卡对外的**唯一结论形态**：
画布上显示的是它，别的卡 read_card 读到的也是它。实测 real_work_002 39 张卡里
只有 1 张写过定论——于是画布上 38 张卡只剩一个标题，用户扫完整棵树也不知道
每条线决定了什么。

闸的位置和"未回应询问不许收工"同一类：complete 是唯一宣称"做完了"的动作，
它必须留下"做完了什么"的凭据。

锁死三条：
1. 正文有内容、但没有定论 ⇒ 拒绝，且拒绝信息自带出路；
2. 拒绝时卡面零改动（goal_state 仍是 active）；
3. 追加定论后放行。
"""
from __future__ import annotations

import pytest

from src.api import auto_driver, card_tools
from src.storage import card_store


@pytest.fixture
def no_kick(monkeypatch):
    """拦掉真实起轮（同 test_auto_mode 的 no_kick，只记录真 kick 会点火的调用）。"""
    calls: list[tuple[str, str]] = []

    def _rec(w: str, c: str):
        if auto_driver.will_continue(w, c):
            calls.append((w, c))
        return None

    monkeypatch.setattr(auto_driver, "kick", _rec)
    return calls


async def _mk_auto(work, goal: str) -> tuple[str, str]:
    """建一张自动模式卡（沿用 test_auto_mode 的建法）。"""
    wid, root = work["work_id"], work["work_card_id"]
    r = await card_store.create_card(wid, "自动卡", "用户直接创建", parent=root)
    cid = r["meta"]["card_id"]
    await card_store.update_card(wid, cid, {"auto_mode": 1, "goal": goal}, "测试置自动模式")
    return wid, cid


async def test_complete_rejected_without_decision(typed_registry, work, no_kick):
    wid, cid = await _mk_auto(work, "把这件事做完")
    # 写一段"有内容但没有定论"的正文
    await card_store.update_card(
        wid, cid, {"status_block": "## 现状\n做了一半，卡在选型上。"}, "写现状")

    _dbg = await card_store.read_card(wid, cid)
    assert "做了一半" in (_dbg["body"]["status_block"] or ""), f"正文没写进去: {_dbg['body']}"

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools.execute_tool(wid, cid, "update_goal", {"action": "complete"}, cards)

    assert res["ok"] is False and res["error"]["code"] == "validation", res
    assert "定论" in res["error"]["message"]
    assert "edit_card" in res["error"]["message"], "拒绝必须自带出路（验收纪律4）"
    # 被拒时卡面零改动
    assert (card_store._meta_of(wid, cid) or {}).get("goal_state") != "complete"


async def test_complete_passes_after_decision_appended(typed_registry, work, no_kick):
    wid, cid = await _mk_auto(work, "把这件事做完")
    await card_store.update_card(
        wid, cid,
        {"status_block": "## 现状\n做了一半，卡在选型上。",
         "rulings": "D01【收敛】选 A 方案，因为并发量低（2026-10-19·实测）"},
        "补定论")

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools.execute_tool(wid, cid, "update_goal", {"action": "complete"}, cards)

    assert res["ok"] is True, res
    assert card_store._meta_of(wid, cid)["goal_state"] == "complete"


async def test_skeleton_placeholder_is_not_a_decision(typed_registry, work, no_kick):
    """骨架里的示例行 `D01【裁定】……（日期·依据）` **不算定论**。

    这条是踩出来的：`create_card` 会把 NEW_CARD_SKELETON 写进正文，里面带着一行
    示例 D 条目。判据若只匹配 `^D\\d{2}【`，每张新建卡一出生就"有 1 条定论"——
    闸等于不存在，画布上还会给每张空卡显示 `定1`。
    """
    wid, cid = await _mk_auto(work, "把这件事做完")
    # 写掉现状（这样正文不再是"未写"状态），但定论仍留着骨架示例行
    await card_store.update_card(
        wid, cid, {"status_block": "## 现状\n做了一半，卡在选型上。"}, "写现状")
    _cur = await card_store.read_card(wid, cid)
    _full = (_cur["body"]["status_block"] or "") + (_cur["body"]["rulings"] or "")
    assert "D01【裁定】……" in _full, f"骨架里应有示例行：{_cur['body']}"

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools.execute_tool(wid, cid, "update_goal", {"action": "complete"}, cards)
    assert res["ok"] is False, "骨架示例行不该被当成定论放行"
    assert "定论" in res["error"]["message"]
