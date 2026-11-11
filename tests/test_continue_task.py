# -*- coding: utf-8 -*-
"""[任务延续 2026-10-25 · 用户裁定] worker 完工后不折叠;续派=同一张卡接着干。

钉六件事:
T1 自动归档已撤:worker 完工后保持活卡(不再有收口即折叠)
T2 continue_task:完工卡续派 ⇒ 目标新生命周期+点火,不动 state 列
   ([完工不影响功能 2026-11-08] 改前会复位 active)
T3 护栏:真在跑的卡拒续派(在途回合判据,非 state 列);非 worker 拒;非祖先拒;给自己拒
T4 沉默语义统一:worker 沉默=治理标记(不折叠)
T5 工具面:父卡/根卡有 continue_task;治理段说明在
T6 上下文延续:续派不删会话/回执(toolcard_final 清空=新任务起跑,但历史在)
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import card_tools, coder_fold, routes as api_routes
from src.storage import card_store, coder_archive, work_store


async def _tree():
    w = await work_store.create_work("任务延续")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    parent = (await card_store.create_card(wid, "父卡", "模型生成",
                                           parent=root))["meta"]["card_id"]
    worker = (await card_store.create_card(
        wid, "worker 卡A", "模型生成", parent=parent, card_type="worker",
        charter="## 使命\n实现 X 并交付"))["meta"]["card_id"]
    return wid, root, parent, worker


def _cards(wid):
    return card_tools._collect_card_meta(wid)


async def test_no_auto_archive_on_completion(sandbox):
    """完工 = 保持活卡形态(自动归档已撤)。"""
    wid, root, parent, worker = await _work_with_done(sandbox)
    assert card_store.card_exists(wid, worker)              # 活卡还在
    meta = card_store._meta_of(wid, worker) or {}
    assert meta.get("state") == "completed"
    assert coder_archive.get_record(wid, worker) is None    # 没有被折叠


async def _work_with_done(sandbox):
    wid, root, parent, worker = await _tree()
    await card_store.update_card(
        wid, worker,
        {"state": "completed", "toolcard_final": '{"summary": "交付完毕"}'},
        "完工")
    return wid, root, parent, worker


async def test_continue_task_reactivates_with_new_goal(sandbox):
    wid, root, parent, worker = await _work_with_done(sandbox)
    cards = _cards(wid)
    out = await card_tools._h_continue_task(
        wid, parent, {"card_id": worker,
                      "task": "迭代 X:补错误处理与单元测试"}, cards)
    assert out.get("ok") is True, out
    meta = card_store._meta_of(wid, worker) or {}
    assert meta.get("state") == "completed", "完工是结果标记，续派不该改它"
    # 上下文延续:会话文件与卡史都在(没删卡重建)
    assert card_store.card_exists(wid, worker)


async def test_continue_task_guards(sandbox):
    wid, root, parent, worker = await _work_with_done(sandbox)
    cards = _cards(wid)
    # 非祖先拒
    other = (await card_store.create_card(wid, "别家", "模型生成",
                                          parent=root))["meta"]["card_id"]
    d1 = await card_tools._h_continue_task(
        wid, other, {"card_id": worker, "task": "x"}, cards)
    assert d1.get("ok") is False
    # 非 worker 拒
    d2 = await card_tools._h_continue_task(
        wid, parent, {"card_id": parent, "task": "x"}, cards)
    assert d2.get("ok") is False
    # 给自己拒
    root_id = work_store.work_card_id_of(wid)
    d3 = await card_tools._h_continue_task(
        wid, root_id, {"card_id": root_id, "task": "x"}, cards)
    assert d3.get("ok") is False
    # 真在跑的拒（[停在中途可续派 2026-11-01] 判据=在途回合，不是 state 列：
    # state=active 但无回合的卡是被停中途的，**可以**续派——card_140 病根）
    r = await card_store.create_card(wid, "在跑卡", "模型生成",
                                     parent=parent, card_type="worker")
    active = r["meta"]["card_id"]
    from src.storage import session_facade
    assert session_facade.turn_try_acquire(wid, active, "turn:test")
    try:
        cards = _cards(wid)
        d4 = await card_tools._h_continue_task(
            wid, parent, {"card_id": active, "task": "x"}, cards)
        assert d4.get("ok") is False and "正在干活" in d4["error"]["message"]
    finally:
        session_facade.turn_release(wid, active, "turn:test")


async def test_silence_worker_is_marker_not_archive(sandbox):
    """worker 沉默=治理标记(不折叠)。"""
    wid, root, parent, worker = await _work_with_done(sandbox)
    cards = _cards(wid)
    out = await card_tools._h_silence_card(
        wid, parent, {"card_id": worker, "action": "silence",
                      "reason": "静默免扰"}, cards)
    assert out.get("ok") is True
    assert card_store.card_exists(wid, worker)              # 不折叠
    assert coder_archive.get_record(wid, worker) is None


async def test_toolface_and_governance_docs(sandbox):
    wid, root, parent, worker = await _work_with_done(sandbox)
    cards = _cards(wid)
    for cid in (root, parent):
        tools = card_tools.build_tool_schemas(card_id=cid, work_id=wid,
                                              cards=cards)
        names = {str(t.get("name")) for t in tools}
        assert "continue_task" in names
    from src.api import context_assembler
    bundle = context_assembler.build_context_bundle(wid, parent)
    assert "continue_task" in bundle["main"]["system_prompt"]
    assert "续派" in bundle["main"]["system_prompt"]
