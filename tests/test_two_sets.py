# -*- coding: utf-8 -*-
"""[两套权限 2026-10-24 · 用户确认] 自动模式 vs 手动模式的权限分叉。

钉七件事:
T1 收权:自动子卡面无 edit_requirements/edit_card;手动子卡**仍在**(两套的核心)
T2 首轮需求闸:自动子卡 rounds=0+需求未策展 ⇒ 动手工具摘除,report_up 在;
   总裁策展后(需求有内容)⇒ 正常面
T3 两段式收口·首段:自动子卡 complete 被拒(COMPLETE_TWO_PHASE)+
   closure_requested 事件进 QA/总裁队列
T4 两段式收口·放行:总裁策展(decisions ledger)+QA 判词落账 ⇒ complete 通过
T5 两套:手动子卡 complete 不进两段闸;coder 不进;根卡不进
T6 fail-open:治理数据读不出 ⇒ 放行
T7 提示词:AUTO 版讲两段式;BASE(手动)不出现
"""
from __future__ import annotations

import pytest

from src.api import card_tools, function_room, prompts
from src.api import routes as api_routes
from src.goal import tools as goal_tools
from src.storage import card_store, function_store, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()


async def _tree(monkeypatch, title="两套权限"):
    _use_repo_catalog(monkeypatch)
    w = await work_store.create_work(title)
    wid = w["work_id"]
    await function_room.seed_work(wid)
    root = work_store.work_card_id_of(wid)
    auto_sub = (await card_store.create_card(
        wid, "自动子卡", "模型生成", parent=root))["meta"]["card_id"]
    await card_store.update_card(wid, auto_sub, {"auto_mode": 1}, "t")
    manual_sub = (await card_store.create_card(
        wid, "手动子卡", "模型生成", parent=root))["meta"]["card_id"]
    return wid, root, auto_sub, manual_sub


async def test_ladder_auto_vs_manual(sandbox, monkeypatch):
    wid, root, auto_sub, manual_sub = await _tree(monkeypatch)
    cards = card_tools._collect_card_meta(wid)
    auto_names = {str(t.get("name")) for t in card_tools.build_tool_schemas(
        card_id=auto_sub, work_id=wid, cards=cards)}
    manual_names = {str(t.get("name")) for t in card_tools.build_tool_schemas(
        card_id=manual_sub, work_id=wid, cards=cards)}
    # [2026-10-28 用户裁定「需求文档它可以自己改，这个权限交还给它」]
    # 两套的差别不再是「谁能写需求文档」，而是「能不能整卡版本回退」：
    #   · 需求文档（edit_requirements）—— 自动子卡与手动卡**都有**
    #   · 现状（edit_status）—— 都有
    #   · 版本回退（edit_card）—— 手动有，自动**没有**（回退会把三块整体还原，
    #     能绕过「需求文档只写确定需求」这条）
    assert "edit_requirements" in auto_names and "edit_status" in auto_names
    assert "edit_card" not in auto_names, "自动子卡不给版本回退"
    assert "edit_card" in manual_names
    assert "report_up" in auto_names                # 上行通道在


async def test_first_round_gate_and_approved_release(sandbox, monkeypatch):
    """激活闸在 kick：**没有正稿** ⇒ 拒点火 + 信告父卡；有正稿 ⇒ 放行。

    [需求文档生命周期 2026-10-28 · 用户令] 判据从「总裁决策簿里有本卡条目」
    换成「有没有正稿」：子卡自己写草稿、评审通过，同样可以点火。
    """
    wid, root, auto_sub, manual_sub = await _tree(monkeypatch)
    # 未定稿：没有正稿
    assert function_room.requirements_approved(wid, auto_sub) is False
    # 评审通过（草稿转正稿）⇒ True
    await card_store.update_card(wid, auto_sub,
                                 {"requirements": "R1 需求",
                                  "requirements_state": "approved"}, "测试：定稿")
    assert function_room.requirements_approved(wid, auto_sub) is True








def test_prompt_two_sets(sandbox, monkeypatch):
    _use_repo_catalog(monkeypatch)
    import importlib
    importlib.reload(prompts)
    try:
        assert "完工即收口" in prompts.AUTO_BASE_PROMPT
        assert "完工即收口" not in prompts.BASE_PROMPT   # 手动版不变
    finally:
        importlib.reload(prompts)
