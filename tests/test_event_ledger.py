# -*- coding: utf-8 -*-
"""事件台账 · E1–E4 回归（2026-10-20 · 照抄 dsh event-ledger 机械层）。

判据：
E1 悬空 eventRef → **进违例清单**（拒写口径）；同批立案台账豁免
E2 open/active 零成员超 N 天 → 孤儿告警（不删）；evt-misc 豁免
E3 新节点指向 closed → 不拒绝，产出 reopen 指令
E4 同指纹（subject+objectKey）多条 open → 告警
S1 event_id 形如 evt-+21 位；ensure_misc_event 幂等
"""
from __future__ import annotations

import asyncio

from src.storage import card_db, card_store, event_ledger as el, memory_domain as md, work_store

DAY = 86_400_000


def _node(seq: int, ref: str | None = None) -> dict:
    n = md.new_node("s1", seq, f"节点{seq}", kind="fact")
    if ref is not None:
        n["eventRef"] = ref
    return n


def _ev(eid: str, status: str = "open", *, subject="s", object_key="o", ts=0) -> dict:
    return {"event_id": eid, "title": "t", "status": status, "summary": "",
            "subject": subject, "object_key": object_key,
            "created_at": ts, "updated_at": ts}


# ---------------------------------------------------------------- E1

def test_e1_dangling_event_ref_is_violation():
    res = el.validate_event_invariants([[ _node(1, "evt-nope") ]], [_ev("evt-real")])
    assert res["e1Dangling"] == [{"nodeSeq": 1, "eventRef": "evt-nope"}]
    assert any(v.startswith("E1:") for v in res["violations"])


def test_e1_same_batch_ledger_exempts():
    res = el.validate_event_invariants([[ _node(1, "evt-fresh") ]], [],
                                       same_batch_event_ids={"evt-fresh"})
    assert res["e1Dangling"] == [] and res["violations"] == []


# ---------------------------------------------------------------- E2

def test_e2_orphan_open_event_after_ttl():
    events = [_ev("evt-old", ts=0)]
    res = el.validate_event_invariants([[]], events, now=10 * DAY)
    assert [o["eventId"] for o in res["e2Orphans"]] == ["evt-old"]
    assert res["violations"] == [], "E2 只告警，不进违例清单"


def test_e2_member_event_not_orphan():
    events = [_ev("evt-used", ts=0)]
    res = el.validate_event_invariants([[ _node(1, "evt-used") ]], events, now=10 * DAY)
    assert res["e2Orphans"] == []


def test_e2_misc_exempt():
    events = [_ev(el.MISC_EVENT_ID, ts=0)]
    res = el.validate_event_invariants([[]], events, now=100 * DAY)
    assert res["e2Orphans"] == [], "evt-misc 常驻，不参与孤儿判定"


# ---------------------------------------------------------------- E3

def test_e3_closed_event_yields_reopen():
    events = [_ev("evt-closed", status="closed")]
    res = el.validate_event_invariants([[]], events, new_nodes=[_node(2, "evt-closed")])
    assert res["e3ReopenIds"] == ["evt-closed"]
    assert res["violations"] == [], "E3 不拒绝"


# ---------------------------------------------------------------- E4

def test_e4_duplicate_open_fingerprint():
    events = [_ev("evt-a", subject="user", object_key="k"),
              _ev("evt-b", subject="user", object_key="k"),
              _ev("evt-c", subject="user", object_key="k2")]
    res = el.validate_event_invariants([[]], events)
    assert len(res["e4Dupes"]) == 1
    assert set(res["e4Dupes"][0]["eventIds"]) == {"evt-a", "evt-b"}


# ---------------------------------------------------------------- S1 表与 id

def test_event_id_format():
    eid = el.new_event_id()
    assert eid.startswith("evt-") and len(eid) == 4 + 21


async def test_ensure_misc_event_idempotent(sandbox):
    wdata = await work_store.create_work("事件台账剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡", "用户直接创建")
    assert el.ensure_misc_event(wid, "card_001") is True
    assert el.ensure_misc_event(wid, "card_001") is False, "幂等：第二次不新建"
    rows = el.load_events(wid, "card_001")
    assert [r["event_id"] for r in rows] == [el.MISC_EVENT_ID]
    assert rows[0]["summary"] == "", "机械层永不产摘要文本"


def test_events_table_in_schema():
    ddl = "\n".join(card_db._MEMORY_DDL)
    assert "CREATE TABLE IF NOT EXISTS events" in ddl
    assert "PRIMARY KEY (card_id, event_id)" in ddl
