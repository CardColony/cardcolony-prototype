# -*- coding: utf-8 -*-
"""[差事卡 2026-11-01 · 用户令「harness 递话不派活」] staff 策略链测试。

新口径：
  · `staff_pm_draft_on_card_created` 不再直接 deploy——它给新卡**注入指令**，
    让模型自己调 `create_deploy_card(function="pm")` 建执笔卡。harness 管时机
    （建卡后缺需求文档），召唤动作归卡。
  · `staff_qa_on_stage_done` 已删：验收是卡的自主判断（它知道几轮编程算一
    阶段），不需要 harness 替它掐点；worker 完工回执里带召唤提示。
  · 卡侧召唤链（建差事卡→差事账→report 回执→自动关账）的钉子在
    `test_deploy_card.py`。
"""
from __future__ import annotations

import pytest

from src.api import function_room, routes
from src.infra import fact_store as fs
from src.infra import intervention_store as ist
from src.infra import policy_config, policy_runner as runner
from src.infra import policy_set as ps
from src.storage import card_store, event_log, function_store, work_store
from tests import async_settle

REPO_POLICIES = ps.__file__ and __import__("pathlib").Path(
    __file__).resolve().parents[1] / "config" / "harness_policies.json"


@pytest.fixture()
def qa_env(sandbox, monkeypatch):
    """真目录播种 + 干预/事实库落沙箱 + 真策略文件（不是测试副本）。"""
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH,
                       str(function_room._DEFAULT_PATH))
    function_room.reset_cache()
    ist.reset(ist.InterventionStore(works_dir=sandbox.works))
    fs.reset(fs.FactStore(works_dir=sandbox.works))
    monkeypatch.setenv("ECO_POLICIES_PATH", str(REPO_POLICIES))
    pset = ps.PolicySet()
    assert pset.load(policy_config.load_raw()) == [], "真策略文件加载必须零错误"
    ps.reset_set(pset)
    yield sandbox
    ist.reset()
    fs.reset(fs.FactStore(works_dir=sandbox.works))


async def test_card_created_no_auto_pm_inject(qa_env, monkeypatch):
    """[2026-11-01 用户令「关掉自动 PM」] staff_pm 策略已停用：建子卡
    不再自动注入「召 PM 写需求」指令——要不要 PM 执笔由卡自己决定
    （create_deploy_card 仍是可用的主动路径）。"""
    wid = (await work_store.create_work("PM不递话"))["work_id"]
    await function_room.seed_work(wid)
    pm_fid = function_room.identity_for(wid, "pm", "deploy")["function_id"]

    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(
        wid, "新子项目", "模型生成", parent=root, card_type="sub_project")
    cid = r["meta"]["card_id"]

    await async_settle.wait_until(
        lambda: any(str(e.get("type") or "") == "card_created"
                    for e in (event_log.read(wid, cid) or [])),
        what="card_created 落盘")
    rec = next(e for e in (event_log.read(wid, cid) or [])
               if e.get("type") == "card_created")

    fired = runner.feed(wid, rec)
    await runner.drain()
    hits = [f for f in fired
            if str(f.get("policy") or "") == "staff_pm_draft_on_card_created"]
    assert not hits, f"策略已停用，建子卡不该再命中 PM 递话: {hits}"

    # 既没有注入指令，也没有 PM 差事账
    assert not function_store.active_for_function(wid, pm_fid, "deploy")
    assert not any(str(e.get("type") or "") == "harness_message"
                   and "create_deploy_card" in str(e.get("data") or "")
                   for e in (event_log.read(wid, cid) or [])), \
        "停用后不该再注入召唤指令"


async def test_no_auto_qa_policy_on_goal_completed(qa_env, monkeypatch):
    """staff_qa 已删：goal_completed 不再自动派 QA——验收由卡自主发起
    （create_deploy_card 的差事卡路径，见 test_deploy_card）。"""
    wid = (await work_store.create_work("不自动派QA"))["work_id"]
    await function_room.seed_work(wid)
    qa_fid = function_room.identity_for(wid, "qa", "deploy")["function_id"]

    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "干活的卡", "模型生成", parent=root)
    cid = r["meta"]["card_id"]
    await card_store.update_card(
        wid, cid, {"goal": "做出 X", "goal_state": "active"}, "设目标")
    await card_store.update_card(wid, cid, {"goal_state": "complete"}, "完工")
    await async_settle.wait_until(
        lambda: any(str(e.get("type") or "") == "card_goal_completed"
                    for e in (event_log.read(wid, cid) or [])),
        what="card_goal_completed 落盘")
    rec = next(e for e in (event_log.read(wid, cid) or [])
               if e.get("type") == "card_goal_completed")
    fired = runner.feed(wid, rec)
    await runner.drain()
    assert not [f for f in fired
                if str(f.get("policy") or "").startswith("staff_qa")]
    assert not function_store.active_for_function(wid, qa_fid, "deploy")


async def test_non_complete_goal_change_no_deploy(qa_env, monkeypatch):
    """goal_state 其它变化（active/pause）不发完工事件、不派差事。"""
    wid = (await work_store.create_work("不误派"))["work_id"]
    await function_room.seed_work(wid)
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(wid, "干活的卡", "模型生成", parent=root)
    cid = r["meta"]["card_id"]
    await card_store.update_card(
        wid, cid, {"goal": "做出 X", "goal_state": "active"}, "设目标")
    await card_store.update_card(wid, cid, {"goal_state": "paused"}, "暂停")
    import asyncio as _aio
    await _aio.sleep(0.1)  # append_bg 异部落盘——给它一轮
    kinds = {str(e.get("type") or "")
             for e in (event_log.read(wid, cid) or [])}
    assert "card_goal_completed" not in kinds
    qa_fid = function_room.identity_for(wid, "qa", "deploy")["function_id"]
    assert function_store.active_for_function(wid, qa_fid, "deploy") == []
