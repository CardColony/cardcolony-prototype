# -*- coding: utf-8 -*-
"""[事件枢纽 2026-10-26] 基础设施：所有事件全量记录 + 按卡汇总。

钉四件事：
H1 运行时事件（emit 路径）⇒ jsonl 与 work_events 两处都有
H2 卡面编辑 ⇒ card_block_edited（带 block/old/new/by/cause）
H3 汇总按卡分开，两张卡的事件不串
H4 未登记的事件类型 ⇒ 开发期抛 UnknownEventType（防漏）
"""
from __future__ import annotations

import json

import pytest

from src.api import event_stream
from src.infra import event_hub
from src.storage import card_store, event_log, work_store


@pytest.fixture(autouse=True)
def _clean():
    event_hub.reset_state()
    yield
    event_hub.reset_state()


async def _seed(title="事件汇总"):
    w = await work_store.create_work(title)
    wid = w["work_id"]
    return wid, work_store.work_card_id_of(wid)


async def _settle():
    """等在途落盘 + 把缓冲全部落表。"""
    await event_log.drain()
    await event_hub.flush_all()


# ---------------------------------------------------------------- H1
async def test_runtime_event_lands_in_summary(sandbox):
    wid, root = await _seed()
    event_stream.emit(wid, root, "turn_end", {"outcome": "ok"})
    await _settle()
    rows = event_hub.list_events(wid, root, ev_type="turn_end")
    assert rows, "turn_end 未进汇总表"
    assert rows[0]["card_id"] == root


# ---------------------------------------------------------------- H2
async def test_card_edit_emits_state_change(sandbox):
    wid, root = await _seed()
    await card_store.update_card(wid, root, {"mission": "新使命"}, "init")
    await _settle()
    rows = event_hub.list_events(wid, root, ev_type="card_block_edited")
    assert rows, "卡面编辑未产生状态变更事件"
    data = json.loads(rows[0]["data"])
    assert data["block"] == "mission"
    assert data["new"] == "新使命"


async def test_state_change_carries_actor_and_cause(sandbox):
    """被动编辑（父卡改子卡）要能看出是谁、什么成因。"""
    wid, root = await _seed()
    sub = (await card_store.create_card(wid, "子", "模型生成",
                                        parent=root))["meta"]["card_id"]
    await card_store.update_card(
        wid, sub, {"status_block": "进度一半", "_by": root, "_cause": "parent"},
        "父卡执笔")
    await _settle()
    rows = event_hub.list_events(wid, sub, ev_type="card_block_edited")
    assert rows, "被动编辑未产生状态变更事件"
    data = json.loads(rows[0]["data"])
    assert data["block"] == "status_block"
    assert data["by"] == root
    assert data["cause"] == "parent"


# ---------------------------------------------------------------- H3
async def test_summary_is_per_card(sandbox):
    wid, root = await _seed()
    sub = (await card_store.create_card(wid, "子", "模型生成",
                                        parent=root))["meta"]["card_id"]
    event_stream.emit(wid, root, "turn_end", {"outcome": "root"})
    event_stream.emit(wid, sub, "turn_end", {"outcome": "sub"})
    await _settle()
    a = event_hub.list_events(wid, root, ev_type="turn_end")
    b = event_hub.list_events(wid, sub, ev_type="turn_end")
    assert len(a) == 1 and len(b) == 1
    assert a[0]["card_id"] == root
    assert b[0]["card_id"] == sub


async def test_count_by_card(sandbox):
    """一处统计、仍分卡。"""
    wid, root = await _seed()
    sub = (await card_store.create_card(wid, "子", "模型生成",
                                        parent=root))["meta"]["card_id"]
    event_stream.emit(wid, root, "turn_end", {})
    event_stream.emit(wid, root, "turn_end", {})
    event_stream.emit(wid, sub, "turn_end", {})
    await _settle()
    stats = {(r["card_id"], r["type"]): r["n"] for r in event_hub.count_by_card(wid)}
    assert stats.get((root, "turn_end")) == 2
    assert stats.get((sub, "turn_end")) == 1


# ---------------------------------------------------------------- H4
def test_unknown_event_type_raises(sandbox):
    with pytest.raises(event_hub.UnknownEventType):
        event_hub.validate_type("绝对不存在的类型", strict=True)


def test_catalog_covers_known_types(sandbox):
    cat = event_hub.load_catalog()
    for t in ("turn_end", "tool_call", "msg_notify", "card_block_edited",
              "goal_round", "wb_exec_done", "reasoning"):
        assert t in cat, f"{t} 未登记"
