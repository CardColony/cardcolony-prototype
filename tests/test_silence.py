# -*- coding: utf-8 -*-
"""[沉默 2026-10-24 · 用户令] 父卡/根卡可让完工的卡沉默,直到再次激活。

钉七件事:
T1 silence_card 生命周期:silence ⇒ is_silenced;activate ⇒ 解除
T2 完工限定:active 卡被拒(指向 stop_card)
T3 权限:祖先可/兄弟拒/根卡不可被沉默
T4 信路闸①:deliver_now 对沉默卡不取件不唤醒(信留 pending)
T5 信路闸②:K3 对沉默卡跳过
T6 信路闸③:_run_message 内部唤醒拒(code=silenced),人类消息可达且不解除
T7 工具面:父卡有 silence_card;92 区治理段说明 stop_card+silence_card
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import card_tools, routes as api_routes
from src.api.message_runner import deliver_now, schedule_card_message_triggers
from src.api.message_runner import RunState
from src.storage import card_store, governance_store, work_store


async def _tree_with_completed():
    w = await work_store.create_work("沉默")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    mid = (await card_store.create_card(wid, "父卡", "模型生成",
                                        parent=root))["meta"]["card_id"]
    done = (await card_store.create_card(wid, "完工卡", "模型生成",
                                         parent=mid))["meta"]["card_id"]
    await card_store.update_card(wid, done, {"state": "completed"}, "完工")
    active = (await card_store.create_card(wid, "在跑卡", "模型生成",
                                           parent=mid))["meta"]["card_id"]
    return wid, root, mid, done, active


def _cards(wid):
    return card_tools._collect_card_meta(wid)


async def test_silence_lifecycle(sandbox):
    wid, root, mid, done, active = await _tree_with_completed()
    cards = _cards(wid)
    out = await card_tools._h_silence_card(
        wid, mid, {"card_id": done, "action": "silence", "reason": "完工免扰"},
        cards)
    assert out.get("ok") is True and out["state"] == "silenced"
    assert governance_store.is_silenced(wid, done)
    out2 = await card_tools._h_silence_card(
        wid, mid, {"card_id": done, "action": "activate", "reason": "复工"},
        cards)
    assert out2["state"] == "active"
    assert not governance_store.is_silenced(wid, done)


async def test_silence_requires_completed(sandbox):
    wid, root, mid, done, active = await _tree_with_completed()
    cards = _cards(wid)
    bad = await card_tools._h_silence_card(
        wid, mid, {"card_id": active, "reason": "它在跑"}, cards)
    assert bad.get("ok") is False
    assert "stop_card" in str(bad.get("error", {}).get("message", ""))


async def test_silence_permissions(sandbox):
    wid, root, mid, done, active = await _tree_with_completed()
    cards = _cards(wid)
    # 兄弟(另一父的子)不可
    other_parent = (await card_store.create_card(
        wid, "别家父", "模型生成", parent=root))["meta"]["card_id"]
    outsider = (await card_store.create_card(
        wid, "别家子", "模型生成", parent=other_parent,
        ))["meta"]["card_id"]
    await card_store.update_card(wid, outsider, {"state": "completed"}, "完工")
    cards = _cards(wid)
    denied = await card_tools._h_silence_card(
        wid, mid, {"card_id": outsider, "reason": "越权"}, cards)
    assert denied.get("ok") is False
    # 总裁(根卡)可跨树
    ok = await card_tools._h_silence_card(
        wid, root, {"card_id": done, "reason": "总裁令"}, cards)
    assert ok.get("ok") is True
    # 根卡不可被沉默
    root_done = root  # 根卡 state 不是 completed ⇒ 先被完工限定拒
    bad_root = await card_tools._h_silence_card(
        wid, mid, {"card_id": root, "reason": "下克上"}, cards)
    assert bad_root.get("ok") is False


async def test_silenced_card_not_delivered(sandbox, monkeypatch):
    """信路闸①:deliver_now 对沉默卡不取件不唤醒(信留 pending)。"""
    wid, root, mid, done, active = await _tree_with_completed()
    from src.api import card_msg
    from src.storage import card_db
    # 先沉默再发信:send_card_message 自带「立即投递」,先发会被当场消费
    await governance_store.set_state(wid, done, "silenced",
                                     reason="完工免扰", by=mid)
    r = await card_tools._h_send_card_message(
        wid, mid, {"target_card_id": done, "msg_type": "msg_notify",
                   "content": "完工后的骚扰信息"}, _cards(wid))
    assert r.get("msg_id")
    woke = await deliver_now(wid, done)
    assert woke is False
    pend = await card_db.list_pending(wid, done)
    assert any("完工后的骚扰信息" in str(p.get("content", "")) for p in pend), \
        "信必须留在收件箱(不投递也不丢)"
    # 激活后可取(conftest 设了 ECO_NO_AUTO_DELIVER=1 关自动投递——
    # 摘掉开关并桩掉下层回合,只验「取件+起回合」这条路通)
    await governance_store.set_state(wid, done, "active", reason="复工", by=mid)
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)

    async def _fake_turn(*a, **k):
        return {"ok": True, "stub": True}

    import src.api.routes as _routes_mod
    monkeypatch.setattr(_routes_mod, "_run_message", _fake_turn)
    woke2 = await deliver_now(wid, done)
    assert woke2 is True
    pend2 = await card_db.list_pending(wid, done)
    assert not any(p.get("status") == "pending" and
                   "完工后的骚扰信息" in str(p.get("content", ""))
                   for p in pend2), "激活后信被取走投递"


async def test_silenced_card_rejected_on_internal_wake(sandbox):
    """信路闸③:_run_message 内部唤醒拒(code=silenced);人类可达且不解除。"""
    wid, root, mid, done, active = await _tree_with_completed()
    await governance_store.set_state(wid, done, "silenced",
                                     reason="免扰", by=mid)
    r = await api_routes._run_message(wid, done, "内部激励文", None,
                                      append_user=False)
    assert isinstance(r, dict) and r.get("error", {}).get("code") == "silenced"
    assert governance_store.is_silenced(wid, done)          # 未被内部路径解除


async def test_toolface_and_governance_section(sandbox):
    wid, root, mid, done, active = await _tree_with_completed()
    cards = _cards(wid)
    for cid in (root, mid):
        tools = card_tools.build_tool_schemas(card_id=cid, work_id=wid,
                                              cards=cards)
        names = {str(t.get("name")) for t in tools}
        assert "silence_card" in names and "stop_card" in names
    # 92 区治理段(父卡上下文里有说明)
    from src.api import context_assembler, context_regions
    bundle = context_assembler.build_context_bundle(wid, mid)
    sysprompt = bundle["main"]["system_prompt"]
    assert "silence_card" in sysprompt and "stop_card" in sysprompt
    assert "治理工具" in sysprompt
