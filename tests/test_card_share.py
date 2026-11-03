# -*- coding: utf-8 -*-
"""卡面信息共享（[2026-10-27 用户裁定]）。

三条：
1. 父卡上下文里，直接子卡给**使命 ＋ 现状**（需求文档不给——太长）；
2. 写工具的回执**带落面后的内容**，不再只回「改成功了」。
   依据：work_027 实测 91 次「刚写完就 read_card 回读」；
3. 精准编辑只动目标块 ⇒ 另外两块结构上不可能被顺手清空。
"""
from __future__ import annotations

from src.api import card_tools
from src.api.context_regions import render_direct_child_status_region
from src.storage import card_store, work_store


# ---------------------------------------------------------------- 170 区
def test_child_region_has_mission_and_status_not_requirements():
    kids = [{"card_id": "card_021", "title": "底层重构",
             "mission": "把底层重写一遍。",
             "status_block": "第一段已完成。",
             "requirements": "C-1 这条需求文档不该出现。"}]
    out = render_direct_child_status_region(kids)
    assert out.startswith("## 直接子卡（活性·使命·现状）")
    assert "把底层重写一遍。" in out
    assert "第一段已完成。" in out
    assert "C-1 这条需求文档不该出现。" not in out, "需求文档不给（用户裁定：太长）"
    assert "card_021" in out and "底层重构" in out


def test_child_region_shows_activity_line():
    """[2026-11-02 用户令] 每个子卡头部一行活性（在跑还是停了），实时装配值。"""
    out = render_direct_child_status_region(
        [{"card_id": "card_024", "title": "在跑的卡",
          "activity": "运行中 · tool_call（2 分钟前）· 已跑 5 轮",
          "mission": "收数据", "status_block": "已收一半"},
         {"card_id": "card_025", "title": "停下的卡",
          "activity": "已暂停", "mission": "", "status_block": ""}])
    assert "**活性**：运行中 · tool_call（2 分钟前）· 已跑 5 轮" in out
    assert "### card_025 · 停下的卡" in out and "**活性**：已暂停" in out, \
        "两块都空的子卡靠活性行露面——父卡要看的就是它停了"


def test_child_region_mission_only_still_shows():
    """只有使命、现状空 ⇒ 该子卡仍要出现（父卡要知道「它有使命、还没开工」）。"""
    out = render_direct_child_status_region(
        [{"card_id": "card_022", "title": "只有使命",
          "mission": "查清楚为什么收敛不了。", "status_block": ""}])
    assert "查清楚为什么收敛不了。" in out
    assert "**现状**" not in out


def test_child_region_empty_both_skipped():
    out = render_direct_child_status_region(
        [{"card_id": "card_023", "title": "两块都空",
          "mission": "", "status_block": ""}])
    assert out == "", "两块都空 ⇒ 该子卡不出现；全空 ⇒ 整个区不出现"


# ---------------------------------------------------------------- 写工具回执
async def _make_card():
    w = await work_store.create_work("共享回执测试")
    wid = str(w["work_id"])
    root = str(w["work_card_id"])
    r = await card_store.create_card(
        wid, "子项目·回执", "用户直接创建", card_type="sub_project",
        charter="## 使命\n做一件事。\n\n## 现状\n旧现状。\n\n## 需求文档\n旧需求。")
    return wid, root, str(r["meta"]["card_id"])


async def test_update_status_returns_landed_content(sandbox):
    wid, root, cid = await _make_card()
    cards = card_store.iter_card_metas(wid)
    # [2026-11-02] _h_update_status 已退役为 _h_edit_status（精准编辑）：
    # 断言不变——回执必须带落面后的现状全文（否则模型还得回读）。
    out = await card_tools._h_edit_status(
        wid, root, {"card_id": cid, "old_string": "旧现状。",
                    "new_string": "新现状：已经跑通了。"}, cards)
    assert out["blocks"]["现状"] == "新现状：已经跑通了。", out


async def test_edit_requirements_returns_landed_content(sandbox):
    wid, root, cid = await _make_card()
    cards = card_store.iter_card_metas(wid)
    # [2026-10-28] 整块写通道已取消：精准编辑把占位换成新内容，回执仍带全文
    cur = (await card_store.read_card(wid, cid))["body"]["requirements"]
    out = await card_tools._h_edit_requirements(
        wid, root, {"card_id": cid, "old_string": cur,
                    "new_string": "C-1 新需求。"}, cards)
    assert out["blocks"]["需求文档"] == "C-1 新需求。"


async def test_edit_card_cannot_clear_other_blocks(sandbox):
    """精准编辑只动目标块 ⇒ 另外两块**结构上不可能**被顺手清空。

    改前 edit_card 收整篇正文：只提交两块时第三块会被清空（card_067 那类事故）。
    """
    wid, root, cid = await _make_card()
    before = (await card_store.read_card(wid, cid))["body"]
    cards = card_store.iter_card_metas(wid)
    # [2026-10-28] 现状只能走 edit_status（edit_card 传「现状」会被拒）
    out = await card_tools._h_edit_status(
        wid, root, {"card_id": cid,
                    "old_string": "旧现状。", "new_string": "新现状。"}, cards)
    assert out.get("ok"), out
    after = (await card_store.read_card(wid, cid))["body"]
    assert after["mission"] == before["mission"]
    assert after["requirements"] == before["requirements"]
    assert after["status_block"] == "新现状。"
