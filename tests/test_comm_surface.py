# -*- coding: utf-8 -*-
"""通信面收拢（2026-10-28 用户裁定）——新面的钉子。

用户裁定（第一手）：
  · 接口只有两个：同步（进上下文、不唤醒）／唤醒（进上下文＋叫起一轮），**都只能沿连线**；
  · `direct` 语义＝**指令／下达任务**；`wake` 取消（可被 direct 替代）；
  · `report` **只向上一级汇报**，不发总裁、不需要总裁直达，**只有汇报一条路**（不分 kind）；
  · inbox 直接取消（消息留在上下文里直到被凝练）。

钉住的事：
  T1 注册表：六件退役件不在；在役通信件是 sync/direct/report_up/read_linked/link_*
  T2 `direct` 必带验收判据（没有判据，收令方无法自判做完没有）
  T3 `direct` 沿连线：两卡之间没有通信线 ⇒ 拒
  T4 `sync` 不唤醒（效果声明 wake=False，且执行时不调 deliver_now）
  T5 `report_up` 收件人只有直接父卡（不发总裁、不召集职能）
"""
from __future__ import annotations

import pytest

from src.api import card_tools, function_room, message_runner
from src.storage import card_store, work_store

RETIRED = ("wake_card", "scene_report", "send_card_message",
           "read_inbox", "issue_directive", "verify_directive")
ALIVE = ("sync", "direct", "report_up", "read_linked",
         "link_cards", "unlink_cards", "link_rel", "unlink_rel")


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH,
                       str(function_room._DEFAULT_PATH))
    function_room.reset_cache()


async def _seed(monkeypatch, title="通信收拢"):
    _use_repo_catalog(monkeypatch)
    w = await work_store.create_work(title)
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    await function_room.seed_work(wid)
    parent = (await card_store.create_card(
        wid, "父卡", "模型生成", parent=root))["meta"]["card_id"]
    child = (await card_store.create_card(
        wid, "子卡", "模型生成", parent=parent))["meta"]["card_id"]
    return wid, root, parent, child


# ---------------------------------------------------------------- T1

def test_t1_retired_and_alive():
    for name in RETIRED:
        assert name not in card_tools.TOOL_REGISTRY, f"{name} 必须已退役"
        assert name not in card_tools.TOOL_SWITCH, f"{name} 的开关映射也要清掉"
    for name in ALIVE:
        assert name in card_tools.TOOL_REGISTRY, f"{name} 必须还在注册表"
        assert name in card_tools.TOOL_SWITCH, f"{name} 缺开关映射"
    # 注册表里每件工具都有开关映射（悬空项会让它悄悄不下发）
    assert [n for n in card_tools.TOOL_REGISTRY
            if n not in card_tools.TOOL_SWITCH] == []


def test_t1b_report_up_has_no_kind():
    """汇报只有一条路：schema 里不该再有 kind。"""
    schema = card_tools.TOOL_REGISTRY["report_up"].schema
    props = (schema.get("parameters") or {}).get("properties") or {}
    assert "kind" not in props, props
    assert (schema.get("parameters") or {}).get("required") == ["content"]


# ---------------------------------------------------------------- T2

@pytest.mark.asyncio
async def test_t2_direct_requires_acceptance(sandbox, monkeypatch):
    wid, root, parent, child = await _seed(monkeypatch)
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_scene_direct(
        wid, parent, {"card_id": child, "instruction": "补错误处理"}, cards)
    assert res.get("ok") is False, res
    assert res["error"]["code"] == "validation"
    assert "acceptance" in res["error"]["message"]


# ---------------------------------------------------------------- T3

@pytest.mark.asyncio
async def test_t3_direct_needs_wire(sandbox, monkeypatch):
    """子卡在父卡子树内，但两卡之间没有通信线 ⇒ 拒（通信只走通信线）。"""
    wid, root, parent, child = await _seed(monkeypatch)
    # 建卡时父子之间会自动补一条默认通信线；这里先把它删掉，模拟"无线"
    await card_store.update_card(wid, parent, {"links": []}, "测试：清掉默认线")
    from src.storage import card_db
    await card_db.run_write(
        wid, lambda conn: conn.execute(
            "DELETE FROM card_links WHERE (from_card=? AND to_card=?)"
            " OR (from_card=? AND to_card=?)", (parent, child, child, parent)))
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_scene_direct(
        wid, parent, {"card_id": child, "instruction": "补错误处理",
                      "acceptance": "单测全绿"}, cards)
    assert res.get("ok") is False, res
    assert res["error"]["code"] == "denied"
    assert "通信线" in res["error"]["message"]


# ---------------------------------------------------------------- T4

@pytest.mark.asyncio
async def test_t4_sync_never_wakes(sandbox, monkeypatch):
    wid, root, parent, child = await _seed(monkeypatch)
    called: list = []

    async def _boom(*a, **kw):  # 只要被调用就说明唤醒发生了
        called.append(a)
        return False

    monkeypatch.setattr(message_runner, "deliver_now", _boom)
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_scene_sync(
        wid, parent, {"content": "接口改了", "card_ids": [child]}, cards)
    assert res.get("ok") is True, res
    assert called == [], "sync 不许唤醒对方"
    # 效果声明也要一致（声明与实现同源，test_tool_doc_facts 会核）
    eff = card_tools.TOOL_REGISTRY["sync"].doc.effect
    assert eff.wake is False


# ---------------------------------------------------------------- T5

@pytest.mark.asyncio
async def test_t5_report_only_to_parent(sandbox, monkeypatch):
    """收件人只有直接父卡：总裁不在列表里，也不召集职能。"""
    wid, root, parent, child = await _seed(monkeypatch)
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    started: list = []

    async def _fake_run(work_id, card_id, content, *a, **kw):
        started.append(card_id)

    from src.api import routes
    monkeypatch.setattr(routes, "_run_message", _fake_run)
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_report_up(
        wid, child, {"content": "做完了，证据在影子"}, cards)
    assert res.get("ok") is not False, res
    assert res["recipients"] == [parent], res
    assert root not in res["recipients"], "汇报不该直达总裁"
