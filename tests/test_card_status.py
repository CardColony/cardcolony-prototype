# -*- coding: utf-8 -*-
"""[裁定《机制判断归harness》第 2 步] card_status 合成视图与对账。

钉四件事：
T1 derive 四维：事件派生 delivery + halted + turn_holder → operational
T2 delivery 无事件覆盖时回落 state 列（史前完工不谎报 active）
T3 reconcile：conflict（列撒谎）与 no_event_coverage（非 active 史前）分列；
   出生即 active 且无变更的卡不报
T4 全链：update_card(state→completed) 落事件后 derive 即得 completed
"""
from __future__ import annotations

from src.api import control
from src.infra import card_status
from src.storage import card_store, work_store


async def test_derive_dimensions(sandbox):
    w = await work_store.create_work("视图")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    cid = (await card_store.create_card(wid, "子卡", "模型生成",
                                        parent=root))["meta"]["card_id"]

    v = card_status.derive(wid, cid)
    assert v["delivery"] == "active" and v["halted"] is None
    assert v["turn_holder"] == "" and v["operational"] == "idle"

    control.halt(wid, cid, reason="按住", by="t")
    v = card_status.derive(wid, cid)
    assert v["operational"] == "halted" and v["halted"]["reason"] == "按住"
    control.unhalt(wid, cid, by="t", via="test")
    assert card_status.derive(wid, cid)["operational"] == "idle"


async def test_delivery_event_derived(sandbox):
    """update_card(state→completed) 落 card_block_edited ⇒ derive 走事件面。"""
    w = await work_store.create_work("派生")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    cid = (await card_store.create_card(wid, "子卡", "模型生成",
                                        parent=root))["meta"]["card_id"]
    await card_store.update_card(wid, cid, {"state": "completed"}, "交工")
    v = card_status.derive(wid, cid)
    assert v["delivery"] == "completed" and v["delivery_from_events"] is True
    assert v["operational"] == "completed"
    # 再续派回 active ⇒ 最后一条事件说了算
    await card_store.update_card(wid, cid, {"state": "active"}, "续派")
    v = card_status.derive(wid, cid)
    assert v["delivery"] == "active" and v["operational"] == "idle"


async def test_reconcile_classification(sandbox):
    """三种对账结果：conflict 真漂移 / no_event_coverage 史前非active /
    出生active无变更不报。"""
    import json
    from src.storage import event_log, session_store
    w = await work_store.create_work("对账")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)

    # a) 出生 active 无变更 ⇒ 不报
    #    （根卡与本卡都该缺席结果表）
    # b) 史前完工：列写 completed 但事件文件里没有任何 state 事件
    pre = (await card_store.create_card(wid, "史前完工", "模型生成",
                                        parent=root))["meta"]["card_id"]
    await card_store.update_card(wid, pre, {"state": "completed"}, "补账")
    # 抹掉刚写下的事件 ⇒ 模拟「完工早于事件接线」的史前列
    ep = event_log.event_path(wid, pre)
    ep.write_text("\n".join(
        ln for ln in ep.read_text(encoding="utf-8").splitlines()
        if '"block": "state"' not in ln), encoding="utf-8")
    event_log._seq_cursor.pop((wid, pre), None)

    # c) 真漂移：列 active 但事件最后一条说 completed（人为造冲突）
    liar = (await card_store.create_card(wid, "撒谎列", "模型生成",
                                         parent=root))["meta"]["card_id"]
    event_log.append_sync(wid, liar, {"type": "card_block_edited",
                                      "data": {"block": "state",
                                               "new": "completed"}})
    # 列保持 active（事件说 completed ⇒ 冲突）
    assert (card_store._meta_of(wid, liar) or {}).get("state") == "active"

    rows = card_status.reconcile_work(wid)
    kinds = {r["card_id"]: r["kind"] for r in rows}
    assert kinds.get(pre) == "no_event_coverage"
    assert kinds.get(liar) == "conflict"
    assert kinds.get(liar) != "no_event_coverage"
    # 出生 active 的卡不该出现在表里
    assert root not in kinds
    # derive 对史前卡回落列值，不谎报
    assert card_status.derive(wid, pre)["delivery"] == "completed"
