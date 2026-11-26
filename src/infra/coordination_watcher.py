# -*- coding: utf-8 -*-
"""统筹链的事件接入与合并派发（任务三第一块）。

[任务书任务三 2026-11-21] 从**已落盘事件**接变化，按计划合并成批，到点派
弱模型观察；到期回看到点即标记（欠办不推进）。

## 一条事件走进统筹链的路

    event_log.append 落盘成功
      └ _notify_harness(work, rec)                     （与条件集同一接线点）
          └ coordination_watcher.feed(work_id, rec)     （本模块；零阻断）
              ├ 事件类型过滤：card_block_edited / goal\/change / msg_directive /
              │   worker_done / review_batch_closed 直接进批；
              │   msg_report 仅当来源卡是计划 items 的责任卡（关联事项）；
              │   其余类型记录过滤原因后丢弃（逐字流/工具逐跳/统筹记账
              │   不进批——避免统筹自己唤醒自己）。
              ├ 授权过滤：来源卡必须在任命观察范围内（scope=work 含后来
              │   加入的卡）；coord 契约（event:{work}:{card}:{seq}）。
              ├ 去重：coordination_seen 表 INSERT OR IGNORE——同 (任命,卡,seq)
              │   只进批一次，持久化，进程故障后不重复送模型。
              ├ 合批：每任命一个 open 批次（coordination_batches），
              │   第一条变化起记 first_at。
              └ 窗口到 ⇒ spawn 弱模型观察（run_weak_observation，后台）；
                  批次标 sent；无事件循环时批次留 open，由 tick 补派。

## 到期回看

    tick(work_id)：active 任命 next_review_at ≤ now ⇒ pending 标 review_due
    （同事务核对活跃；**不推进到期值**——欠办保留原值，界面可见）。
    独立回看轮的收口契约（核查引用/回执/结论/下一次绝对时间）在下一块实现。

## 时钟与配置

- ``now()`` 可注入（测试 monkeypatch）。
- 合并窗口：环境变量 ``COORD_MERGE_WINDOW_SEC``（试点显式配置，默认 120s）；
  计划里 ``merge_window_sec`` 字段可按计划覆盖。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..storage import card_db, coordination_store

logger = logging.getLogger("infra.coordination_watcher")

#: 直接进批的正式事件类型（任务书任务三清单）
WATCH_EVENT_TYPES = ("card_block_edited", "goal/change", "msg_directive",
                     "worker_done", "review_batch_closed")
#: 软进批：仅当来源卡是计划 items 的责任卡（关联事项）才进
SOFT_EVENT_TYPES = ("msg_report",)


def now() -> float:
    """可注入时钟（测试 monkeypatch 本函数推进时间）。"""
    return time.time()


def merge_window_sec(work_id: str, plan_card_id: str) -> int:
    """合并窗口：计划字段 merge_window_sec 优先，否则环境变量，默认 120s。"""
    try:
        p = coordination_store.panel(work_id, plan_card_id)
        v = (p.get("plan") or {}).get("merge_window_sec")
        if v is not None and int(v) > 0:
            return int(v)
    except Exception:  # noqa: BLE001 读不到计划用默认
        pass
    try:
        return max(1, int(os.environ.get("COORD_MERGE_WINDOW_SEC") or 120))
    except (TypeError, ValueError):
        return 120


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).isoformat()


def _parse_iso(ts: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- 存储

_BATCHES_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_batches ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " work_id TEXT NOT NULL,"
    " plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL,"
    " batch_id TEXT NOT NULL UNIQUE,"
    " changes TEXT NOT NULL DEFAULT '[]',"
    " status TEXT NOT NULL DEFAULT 'open',"
    " accepting INTEGER NOT NULL DEFAULT 1,"
    " first_at TEXT,"
    " sent_at TEXT,"
    " created_at TEXT, updated_at TEXT)"
)
_CURSORS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_event_cursors ("
    " card_id TEXT PRIMARY KEY, seq INTEGER NOT NULL)"
)
_REPORTS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_report_evidence ("
    " assignment_id TEXT NOT NULL, item_id TEXT NOT NULL, refs TEXT NOT NULL,"
    " PRIMARY KEY (assignment_id,item_id,refs))"
)


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_BATCHES_DDL)
    if not any(col[1] == "accepting" for col in conn.execute("PRAGMA table_info(coordination_batches)")):
        conn.execute("ALTER TABLE coordination_batches ADD COLUMN accepting INTEGER NOT NULL DEFAULT 1")
    conn.execute(coordination_store._SEEN_DDL)
    conn.execute(_CURSORS_DDL)
    conn.execute(_REPORTS_DDL)


def _node_hits(plan: dict[str, Any], card_id: str, kind: str, data: dict[str, Any]) -> list:
    hits = []
    for node in plan.get("direct_nodes") or ():
        if (not isinstance(node, dict) or node.get("card_id") != card_id
                or node.get("event_type") != kind
                or not node.get("item_id") or not node.get("node_id")
                or not isinstance(node.get("path"), str) or "equals" not in node):
            continue
        found = data
        for part in node["path"].split("."):
            found = found.get(part, _MISSING) if isinstance(found, dict) else _MISSING
        want = node["equals"]
        if type(found) is type(want) and found == want:
            hits.append(node)
    return hits


_MISSING = object()


def _stage_event(work_id: str, appt: dict[str, Any], change: dict[str, Any],
                 hits: list, report_refs: list[str]) -> bool:
    """坐标、证据去重、待办或批次在同一事务提交。"""
    from uuid import uuid4

    plan_card = appt["plan_card_id"]
    aid = appt["assignment_id"]
    now_ts = now()
    project_agent = plan_card == coordination_store.PROJECT_AGENT_ID
    window = 0 if project_agent else merge_window_sec(work_id, plan_card)
    stamp = _iso(now_ts)

    def _write(conn: sqlite3.Connection) -> bool:
        _ensure(conn)
        row = conn.execute("SELECT id,pending FROM coordination_appointments"
                           " WHERE assignment_id=? AND state='active' AND rev=?",
                           (aid, appt["rev"])).fetchone()
        if row is None:
            raise RuntimeError("合批前任命或版本已变化，保留事件待补读")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO coordination_seen"
            " (work_id,plan_card_id,assignment_id,card_id,seq,seen_at)"
            " VALUES (?,?,?,?,?,?)",
            (work_id, plan_card, aid, change["card_id"], change["seq"], stamp))
        if not cursor.rowcount:
            return False
        if report_refs:
            refs = coordination_store._dumps(sorted(set(report_refs)))
            cursor = conn.execute("INSERT OR IGNORE INTO coordination_report_evidence"
                                  " (assignment_id,item_id,refs) VALUES (?,?,?)",
                                  (aid, change["group"], refs))
            if not cursor.rowcount:
                logger.debug("统筹忽略重复汇报证据：%s", change["coord"])
                return False
        if hits:
            pending = coordination_store._loads(row["pending"], [])
            pending.extend({
                "kind": "direct_node", "request_id": f'{change["coord"]}:{node["node_id"]}',
                "at": stamp, "item_id": node["item_id"],
                "reason": f'计划节点 {node["node_id"]} 已达成',
                "evidence_refs": [change["coord"]], "source_cards": change["source_cards"],
            } for node in hits)
            conn.execute("UPDATE coordination_appointments SET pending=? WHERE id=?",
                         (coordination_store._dumps(pending), row["id"]))
            return True

        batches = conn.execute("SELECT * FROM coordination_batches WHERE assignment_id=?"
                               " AND status='open' ORDER BY id DESC", (aid,))
        for batch in batches:
            changes = json.loads(batch["changes"] or "[]")
            first = _parse_iso(batch["first_at"])
            if (changes and changes[0].get("group") == change["group"]
                    and (project_agent or (first is not None and now_ts < first + window))):
                changes.append(change)
                conn.execute("UPDATE coordination_batches SET changes=?, updated_at=?"
                             " WHERE id=?", (coordination_store._dumps(changes), stamp, batch["id"]))
                return True

        conn.execute("INSERT INTO coordination_batches"
                     " (work_id,plan_card_id,assignment_id,batch_id,changes,status,"
                     " first_at,created_at,updated_at) VALUES (?,?,?,?,?,'open',?,?,?)",
                     (work_id, plan_card, aid, f"b_{uuid4().hex[:12]}",
                      coordination_store._dumps([change]), stamp, stamp, stamp))
        return True

    return bool(card_db.write_sync(work_id, _write))


def _summary_of(rec: dict[str, Any]) -> str:
    return f'{rec["type"]} {coordination_store._dumps(rec.get("data") or {})[:500]}'


def feed(work_id: str, card_id: str, rec: dict[str, Any], *, project_only: bool = False,
         fact: dict[str, Any] | None = None) -> None:
    """事件已落盘。写入失败由主调度的事件水位补读。"""
    if not work_id or not isinstance(rec, dict):
        return
    try:
        _feed_impl(work_id, card_id, rec, project_only=project_only, fact=fact)
    except Exception as exc:
        logger.warning("[coordination_watcher] feed 失败 work=%s: %s", work_id, exc)


def _feed_impl(work_id: str, card_id: str, rec: dict[str, Any], *,
               schedule: bool = True, project_only: bool = False,
               fact: dict[str, Any] | None = None) -> None:
    from ..api import coordination_runner
    from . import procs

    kind = str(rec.get("type") or "")
    seq = rec.get("seq")
    if not kind or not isinstance(seq, int):
        return
    data = rec.get("data") or {}
    actor = str(data.get("from") or card_id) if kind in ("msg_report", "msg_directive") else card_id
    sources = sorted({card_id, actor})
    time_value = _parse_iso(rec.get("ts") or rec.get("time"))
    if not schedule and time_value is None:
        return  # 无时点的旧存档无法证明发生在任命启用之后
    for plan in coordination_store.list_plans(work_id, project_only=project_only):
        if plan["state"] != "active":
            continue
        plan_card = plan["plan_card_id"]
        project_agent = plan_card == coordination_store.PROJECT_AGENT_ID
        aid = plan["assignment_id"]
        if project_agent or kind not in (*WATCH_EVENT_TYPES, *SOFT_EVENT_TYPES):
            continue
        appt = _current_appt(work_id, plan_card)
        if appt.get("assignment_id") != aid or actor == plan_card and kind in SOFT_EVENT_TYPES:
            continue
        activated = _parse_iso(appt.get("created_at"))
        if time_value is not None and activated is not None and time_value < activated:
            continue
        allowed = coordination_runner._allowed_card_ids(work_id, appt.get("observe_scope") or {})
        if any(source not in allowed for source in sources):
            continue
        plan_body = coordination_runner.plan_for_model(appt, allowed)
        items = [item for item in plan_body.get("items") or [] if isinstance(item, dict)]
        explicit_item = data.get("item_id")
        item = next((it for it in items if it.get("item_id") == explicit_item and explicit_item), None)
        if item is None:
            item = next((it for it in items if it.get("owner_card") == actor), None)
        item_id = item.get("item_id") if item else None
        report_refs = []
        if kind == "msg_report":
            refs = data.get("evidence_refs") or data.get("artifact_refs") or []
            if isinstance(refs, list):
                report_refs = [r.strip() for r in refs if isinstance(r, str) and r.strip()]
            if not report_refs:
                report_refs = re.findall(r"\b(?:evidence|artifacts)[/\\][^\s`\"'，。；）)]+",
                                         str(data.get("content") or ""))
            if not item_id or not report_refs:
                logger.debug("统筹忽略无关联新证据的汇报：%s/%s/%s", work_id, card_id, seq)
                continue
        coord = f"event:{work_id}:{card_id}:{seq}"
        summary = _summary_of(rec)
        change = {"coord": coord, "card_id": card_id, "source_cards": sources,
                  "kind": kind, "seq": seq, "summary": summary, "evidence": summary,
                  "item_id": item_id, "group": f"item:{item_id}" if item_id else f"card:{actor}"}
        hits = _node_hits(plan_body, actor, kind, data)
        if _stage_event(work_id, appt, change, hits, report_refs) and schedule:
            if hits:
                _spawn_strong_turn(work_id, plan_card, aid, "direct_node",
                                   f'{coord}:{hits[0]["node_id"]}')
            elif procs.is_primary_runner():
                _maybe_dispatch(work_id, plan_card, aid)


def _replay_committed(work_id: str, active: list[dict[str, Any]], *,
                      project_only: bool = False) -> None:
    from ..api import coordination_runner
    from ..storage import event_log

    card_ids: set[str] = set()
    for plan in active:
        appt = _current_appt(work_id, plan["plan_card_id"])
        if plan["plan_card_id"] == coordination_store.PROJECT_AGENT_ID:
            continue
        card_ids.update(coordination_runner._allowed_card_ids(work_id, appt.get("observe_scope") or {}))

    def _cursors(conn: sqlite3.Connection) -> dict[str, int]:
        _ensure(conn)
        return {r["card_id"]: r["seq"] for r in conn.execute("SELECT * FROM coordination_event_cursors")}

    after = card_db.write_sync(work_id, _cursors) or {}
    for card_id in sorted(card_ids):
        current = after.get(card_id, 0)
        if event_log.last_seq(work_id, card_id) <= current:
            continue
        events = event_log.read(work_id, card_id, from_seq=current + 1)
        for event in events:
            _feed_impl(work_id, card_id, event, schedule=False, project_only=project_only)
        last = max((event["seq"] for event in events if isinstance(event.get("seq"), int)),
                   default=current)

        def _advance(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO coordination_event_cursors(card_id,seq) VALUES (?,?)"
                         " ON CONFLICT(card_id) DO UPDATE SET seq=max(seq,excluded.seq)",
                         (card_id, last))

        card_db.write_sync(work_id, _advance)


def _current_appt(work_id: str, plan_card_id: str) -> dict[str, Any]:
    p = coordination_store.panel(work_id, plan_card_id)
    if p.get("state") not in ("active", "paused"):
        return {}
    g = p.get("grants") or {}
    p["observe_scope"] = g.get("observe") or {}
    p["instruct_scope"] = g.get("instruct") or {}
    return p


def _inspection_item(work_id: str, appt: dict, allowed: set[str], fact: dict) -> str:
    from ..storage import coordination_board, event_log

    if fact["type"] not in ("msg_report", "worker_done"):
        return ""
    ids = {item["item_id"] for item in coordination_board.visible_items(appt, allowed)}
    if not ids:
        return ""
    events, _ = event_log.timeline_events(work_id, fact["card"], [fact["seq"]])
    data = events[0].get("data") if events else {}
    if not isinstance(data, dict):
        return ""
    item = data.get("item_id")
    if isinstance(item, str) and item in ids:
        return item
    # 仅用于合批。报告中只有一个明确的计划板事项编号才归入该事项。
    matches = ids.intersection(re.findall(r"\bit_[a-zA-Z0-9_]+\b", str(data.get("content") or "")))
    return next(iter(matches)) if len(matches) == 1 else ""


def _inspection_group(change: dict) -> str:
    if change.get("item_id"):
        return f"item:{change['item_id']}"
    return "policy:" + ":".join(change.get("inspection_ids") or []) + ":" + str(change.get("policy_signature") or "")


def _same_inspection(first: dict, change: dict) -> bool:
    return _inspection_group(first) == _inspection_group(change)


def _belongs_to_handoff(first: dict, change: dict, item_id: str) -> bool:
    related = _same_inspection(first, change)
    changed_item = change.get("item_id")
    return bool(changed_item and (related or changed_item == item_id)) or (
        change["kind"] == "card_idle" and related)


def _active_handoff_batches(conn: sqlite3.Connection, aid: str, pending: list, control_seq: int) -> dict[str, str]:
    active = {row[0] for row in conn.execute(
        "SELECT run_id FROM coordination_runs WHERE assignment_id=? AND phase IN ('waiting','working')", (aid,))}
    return {entry["batch_id"]: str(entry.get("item_id") or "") for entry in pending
            if entry.get("kind") == "weak_wake" and entry.get("batch_id") and entry.get("run_id") in active
            and entry.get("control_seq") == control_seq}


async def enqueue_harness_inspection(work_id: str, target: str, policy_id: str, action_id: str) -> str | None:
    """Accept a native Harness action. Conditions have already been decided by Harness."""
    from . import coordination_policy, fact_store, intervention_store
    from ..api import coordination_runner

    if target != coordination_store.PROJECT_AGENT_ID or not action_id:
        return "inspect:stale"
    action = intervention_store.store().get_action(work_id, action_id) or {}
    if action.get("policy_id") != policy_id or action.get("mode") != coordination_policy.MODE:
        return "inspect:stale"
    spec = json.loads(action.get("spec") or "{}")
    basis = spec.get("basis") or {}
    appt = _current_appt(work_id, target)
    aid = appt.get("assignment_id")
    rules = {p["id"]: p["then"] for p in coordination_runner.project_inspection_rules(work_id, appt)}
    then = rules.get(policy_id, {})
    if (appt.get("state") != "active" or not then.get("signature")
            or then["signature"] != spec.get("signature")):
        return "inspect:stale"
    card, seq = basis.get("card"), basis.get("seq")
    allowed = coordination_runner._allowed_card_ids(work_id, appt.get("observe_scope") or {})
    if card not in allowed or type(seq) is not int or basis.get("type") != then.get("trigger"):
        return "inspect:stale"
    facts = fact_store.store().card_facts_after(work_id, card, seq - 1, types=[basis["type"]], limit=1)
    fact = next((row for row in facts if row.get("seq") == seq and row.get("ts") == basis.get("ts")), None)
    if (not fact or int((fact.get("_t") or 0) * 1000) < int(then.get("saved_at", 0) * 1000)
            or fact.get(then.get("subject", "card")) not in allowed):
        return "inspect:stale"
    subject = fact.get(then.get("subject", "card"))
    coord = f"event:{work_id}:{card}:{seq}"
    summary = coordination_store._dumps(fact)[:500]
    item_id = _inspection_item(work_id, appt, allowed, fact)
    change = {"coord": coord, "card_id": card, "source_cards": sorted({card, subject}),
              "kind": basis["type"], "seq": seq, "summary": summary, "evidence": summary,
              "item_id": item_id, "control_seq": appt["control_seq"],
              "inspection_ids": [policy_id], "policy_signature": then["signature"], "fact": fact}
    batch_id = f"harness_{action_id}"
    queued_at = _iso(now())

    def write(conn: sqlite3.Connection) -> str:
        coordination_store._ensure(conn)
        _ensure(conn)
        appointment = conn.execute("SELECT pending,control_seq FROM coordination_appointments"
                                   " WHERE assignment_id=? AND state='active'", (aid,)).fetchone()
        if not appointment or appointment["control_seq"] != appt["control_seq"]:
            return ""
        seen = conn.execute("INSERT OR IGNORE INTO coordination_seen VALUES (?,?,?,?,?,?)",
                            (work_id, target, aid, card, seq, queued_at)).rowcount
        if not seen:
            previous = conn.execute(
                "SELECT b.batch_id FROM coordination_batches b JOIN json_each(b.changes) c"
                " WHERE b.assignment_id=? AND b.status!='merged' AND json_extract(c.value,'$.coord')=?"
                " AND json_extract(c.value,'$.inspection_ids[0]')=? AND json_extract(c.value,'$.policy_signature')=?",
                (aid, coord, policy_id, then["signature"])).fetchone()
            if previous:
                return previous["batch_id"]
        handoffs = _active_handoff_batches(conn, aid, coordination_store._loads(appointment["pending"], []),
                                          appointment["control_seq"])
        for row in conn.execute("SELECT id,batch_id,changes,status FROM coordination_batches"
                                " WHERE assignment_id=? AND accepting=1 AND (status IN ('open','running')"
                                " OR batch_id IN (SELECT value FROM json_each(?)))"
                                " ORDER BY CASE status WHEN 'open' THEN 1 ELSE 0 END,id",
                                (aid, coordination_store._dumps(list(handoffs)))):
            active = row["batch_id"] in handoffs
            if row["status"] == "sent" and not active:
                continue
            changes = json.loads(row["changes"])
            if not changes or changes[0].get("control_seq", appt["control_seq"]) != appt["control_seq"]:
                continue
            if row["status"] != "open" and len(_native_changes(changes, rules)[0]) != len(changes):
                continue
            related = _same_inspection(changes[0], change)
            if active:
                related = _belongs_to_handoff(changes[0], change, handoffs[row["batch_id"]])
            if related:
                conn.execute("UPDATE coordination_batches SET changes=?,updated_at=? WHERE id=?",
                             (coordination_store._dumps([*changes, change]), queued_at, row["id"]))
                return row["batch_id"]
        conn.execute("INSERT OR IGNORE INTO coordination_batches"
                     " (work_id,plan_card_id,assignment_id,batch_id,changes,status,first_at,created_at,updated_at)"
                     " VALUES (?,?,?,?,?,'open',?,?,?)",
                     (work_id, target, aid, batch_id, coordination_store._dumps([change]),
                      queued_at, queued_at, queued_at))
        return batch_id

    queued = await card_db.run_write(work_id, write)
    if not queued:
        return "inspect:stale"
    _maybe_dispatch(work_id, target, aid)
    return queued


_weak_turns: set[tuple[str, str]] = set()
_weak_dispatching: set[tuple[str, str]] = set()


def _existing_weak_result(conn: sqlite3.Connection, plan_row: sqlite3.Row, batch_id: str) -> bool:
    pending = coordination_store._loads(plan_row["pending"], [])
    if any(p.get("batch_id") == batch_id and p.get("kind") in ("weak_wake", "unjudged_batch")
           for p in pending):
        return True
    return conn.execute(
        "SELECT 1 FROM coordination_observations WHERE assignment_id=? AND batch_id=?"
        " AND kind='weak_observation' AND status IN ('passed','unjudged','wake') LIMIT 1",
        (plan_row["assignment_id"], batch_id)).fetchone() is not None


def _native_changes(changes: list[dict], rules: dict) -> tuple[list[dict], dict]:
    from . import coordination_policy

    current, bindings = [], {}
    for change in changes:
        when = int(((change.get("fact") or {}).get("_t") or 0) * 1000)
        matches = [pid for pid in change.get("inspection_ids", []) if pid in rules
                   and change.get("policy_signature") == rules[pid].get("signature")
                   and when >= int(rules[pid].get("saved_at", 0) * 1000)]
        if matches:
            current.append({**change, "inspection_ids": matches})
            bindings.update({pid: coordination_policy.binding(rules[pid]) for pid in matches})
    return current, bindings


def native_batch_updates(work_id: str, aid: str, batch_id: str, count: int,
                         appt: dict) -> tuple[int, list[dict], dict]:
    from ..api import coordination_runner

    row = card_db.read_sync(work_id, lambda c: c.execute(
        "SELECT changes FROM coordination_batches WHERE assignment_id=? AND batch_id=?", (aid, batch_id)).fetchone(), None)
    if row is None:
        return count, [], {}
    changes = coordination_store._loads(row["changes"], [])
    if len(changes) <= count:
        return count, [], {}
    rules = {p["id"]: p["then"] for p in coordination_runner.project_inspection_rules(work_id, appt)}
    accepted, bindings = _native_changes(changes[count:], rules)
    return len(changes), accepted, bindings


async def seal_native_batch(work_id: str, aid: str, batch_id: str, count: int) -> bool:
    """与收件互斥。已有更新时让原会话读完再收口，新到事件继续归入本批。"""
    def write(conn):
        cursor = conn.execute("UPDATE coordination_batches SET accepting=0"
                              " WHERE assignment_id=? AND batch_id=? AND json_array_length(changes)=?",
                              (aid, batch_id, count))
        return bool(cursor.rowcount)

    return await card_db.run_write(work_id, write)


async def join_inspection_handoff(work_id: str, appt: dict, batch_id: str, item_id: str,
                                  conclusion: str, sources: set[str]) -> str:
    """原始消息没带事项编号时，采用巡检已查明的事项归属接续已有接手。"""
    from ..api import coordination_runner
    from ..storage import coordination_board

    aid = appt["assignment_id"]
    allowed = coordination_runner._allowed_card_ids(work_id, appt["observe_scope"])
    if item_id not in {item["item_id"] for item in coordination_board.visible_items(appt, allowed)}:
        return ""
    rules = {p["id"]: p["then"] for p in coordination_runner.project_inspection_rules(work_id, appt)}

    def write(conn):
        appointment = conn.execute("SELECT pending FROM coordination_appointments"
                                   " WHERE assignment_id=? AND state='active' AND control_seq=?",
                                   (aid, appt["control_seq"])).fetchone()
        if not appointment:
            return ""
        incoming = conn.execute("SELECT changes FROM coordination_batches WHERE assignment_id=? AND batch_id=?"
                                " AND status='running' AND accepting=1", (aid, batch_id)).fetchone()
        if not incoming:
            return ""
        changes = coordination_store._loads(incoming["changes"], [])
        if not changes:
            return ""
        pending = coordination_store._loads(appointment["pending"], [])
        for target, linked_item in _active_handoff_batches(conn, aid, pending, appt["control_seq"]).items():
            if target == batch_id or linked_item != item_id:
                continue
            row = conn.execute("SELECT changes FROM coordination_batches WHERE assignment_id=? AND batch_id=?"
                               " AND accepting=1", (aid, target)).fetchone()
            if not row:
                continue
            current = coordination_store._loads(row["changes"], [])
            if not current or len(_native_changes(current, rules)[0]) != len(current):
                continue
            changes[0] = {**changes[0], "inspection_note": conclusion,
                          "source_cards": sorted(sources | set(changes[0]["source_cards"]))}
            stamp = _iso(now())
            conn.execute("UPDATE coordination_batches SET changes=?,updated_at=? WHERE batch_id=?",
                         (coordination_store._dumps([*current, *changes]), stamp, target))
            conn.execute("UPDATE coordination_batches SET status='merged',accepting=0,sent_at=? WHERE batch_id=?",
                         (stamp, batch_id))
            return target
        return ""

    return await card_db.run_write(work_id, write)


async def keep_unseen_inspection_events(work_id: str, aid: str, batch_id: str, count: int) -> None:
    """失败照常留账。失败期间才收到的事实沿用原 Harness 证据，另行排队。"""
    from uuid import uuid4

    def write(conn):
        row = conn.execute("SELECT plan_card_id,changes FROM coordination_batches"
                           " WHERE assignment_id=? AND batch_id=? AND accepting=1", (aid, batch_id)).fetchone()
        if row is None:
            return
        conn.execute("UPDATE coordination_batches SET accepting=0 WHERE batch_id=?", (batch_id,))
        remaining = coordination_store._loads(row["changes"], [])[count:]
        if remaining:
            stamp = _iso(now())
            conn.execute("INSERT INTO coordination_batches"
                         " (work_id,plan_card_id,assignment_id,batch_id,changes,first_at,created_at,updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?)",
                         (work_id, row["plan_card_id"], aid, f"b_{uuid4().hex}",
                          coordination_store._dumps(remaining), stamp, stamp, stamp))

    await card_db.run_write(work_id, write)


async def run_owned_batch(work_id: str, plan_card_id: str, assignment_id: str,
                          batch_id: str) -> dict[str, Any]:
    from . import coordination_policy, procs
    from ..api import coordination_runner

    if not procs.runner_side() or (procs.is_runner() and
            procs.owner_index_of(work_id, plan_card_id) != procs.runner_index()):
        return {"status": "wrong_owner"}
    key = (work_id, plan_card_id)
    if key in _weak_turns:
        return {"status": "busy"}
    project_agent = plan_card_id == coordination_store.PROJECT_AGENT_ID
    if project_agent:
        from . import policy_runner
        if policy_runner._off():
            return {"status": "harness_paused"}
        appt = _current_appt(work_id, plan_card_id)
        rules = {p["id"]: p["then"] for p in coordination_runner.project_inspection_rules(work_id, appt)}
        if appt.get("state") != "active":
            return {"status": "waiting_inspection_plan"}

    def _take(conn: sqlite3.Connection) -> dict[str, Any]:
        if project_agent:
            _ensure(conn)
        plan = conn.execute("SELECT * FROM coordination_appointments WHERE work_id=?"
                            " AND plan_card_id=? AND assignment_id=? AND state='active'",
                            (work_id, plan_card_id, assignment_id)).fetchone()
        if plan is None:
            return {}
        if project_agent:
            control_seq = int(plan["control_seq"]) if "control_seq" in plan.keys() else 0
            if control_seq != appt["control_seq"]:
                return {}
        batch = conn.execute("SELECT * FROM coordination_batches WHERE assignment_id=?"
                             " AND batch_id=? AND status IN ('open','running')",
                             (assignment_id, batch_id)).fetchone()
        if batch is None:
            return {}
        if _existing_weak_result(conn, plan, batch_id):
            conn.execute("UPDATE coordination_batches SET status='sent' WHERE batch_id=?", (batch_id,))
            return {}
        changes = json.loads(batch["changes"] or "[]")
        bindings = {}
        if project_agent:
            if changes and batch["accepting"]:
                handoffs = _active_handoff_batches(conn, assignment_id,
                                                  coordination_store._loads(plan["pending"], []), control_seq)
                for active_id, item_id in handoffs.items():
                    if active_id == batch_id:
                        continue
                    active = conn.execute("SELECT changes FROM coordination_batches"
                                          " WHERE batch_id=? AND accepting=1", (active_id,)).fetchone()
                    if not active:
                        continue
                    ongoing = coordination_store._loads(active["changes"], [])
                    if (ongoing and len(_native_changes(ongoing, rules)[0]) == len(ongoing)
                            and all(_belongs_to_handoff(ongoing[0], change, item_id) for change in changes)):
                        conn.execute("UPDATE coordination_batches SET changes=?,updated_at=? WHERE batch_id=?",
                                     (coordination_store._dumps([*ongoing, *changes]), _iso(now()), active_id))
                        conn.execute("UPDATE coordination_batches SET status='merged',accepting=0 WHERE id=?",
                                     (batch["id"],))
                        return {"discarded": True}
                # 升级前仍排队的同组记录也纳入同一个子会话。
                waiting = conn.execute("SELECT id,changes FROM coordination_batches WHERE assignment_id=?"
                                       " AND batch_id!=? AND status='open' AND accepting=1 ORDER BY id",
                                       (assignment_id, batch_id)).fetchall()
                merged = []
                for row in waiting:
                    extra = coordination_store._loads(row["changes"], [])
                    if (extra and extra[0].get("control_seq", control_seq) == control_seq
                            and _same_inspection(changes[0], extra[0])):
                        changes.extend(extra)
                        merged.append(row["id"])
                if merged:
                    conn.executemany("UPDATE coordination_batches SET status='merged',accepting=0 WHERE id=?",
                                     [(key,) for key in merged])
                    conn.execute("UPDATE coordination_batches SET changes=? WHERE id=?",
                                 (coordination_store._dumps(changes), batch["id"]))
            count = len(changes)
            changes, bindings = _native_changes(changes, rules)
            if not changes:
                conn.execute("UPDATE coordination_batches SET status='sent',accepting=0 WHERE batch_id=?", (batch_id,))
                return {"discarded": True}
        conn.execute("UPDATE coordination_batches SET status='running' WHERE batch_id=?", (batch_id,))
        return {"batch_id": batch_id, "changes": changes, "policy_rev": int(plan["rev"]),
                "policy_bindings": bindings,
                **({"control_seq": appt["control_seq"], "native_count": count} if project_agent else {})}

    _weak_turns.add(key)
    try:
        batch = await card_db.run_write(work_id, _take)
        if not batch:
            return {"status": "skipped"}
        if batch.get("discarded"):
            return {"status": "filtered"}
        appt = _current_appt(work_id, plan_card_id)
        if appt.get("assignment_id") != assignment_id or appt.get("state") != "active":
            return {"status": "skipped"}
        batch["assignment_id"] = assignment_id
        result = await coordination_runner.run_weak_observation(work_id, plan_card_id, batch)
        if result["status"] == "unjudged" and result.get("native_count") is not None:
            await keep_unseen_inspection_events(work_id, assignment_id, batch_id, result["native_count"])
        if result.get("status") in ("passed", "unjudged", "wake", "skipped"):
            await card_db.run_write(work_id, lambda c: c.execute(
                "UPDATE coordination_batches SET status='sent', sent_at=?,"
                " accepting=CASE WHEN ? THEN accepting ELSE 0 END WHERE batch_id=?",
                (_iso(now()), int(result["status"] == "wake" and project_agent), batch_id)).rowcount)
        if result.get("status") == "wake" and result.get("run_id"):
            _spawn_strong_turn(work_id, plan_card_id, assignment_id, "weak_wake", batch_id)
        return result
    finally:
        _weak_turns.discard(key)


def _maybe_dispatch(work_id: str, plan_card_id: str, assignment_id: str) -> None:
    """到期先保留磁盘批次，再由属主独占一轮。重启可认领未结批。"""
    from . import policy_runner, procs
    from ..api import coordination_runner

    key = (work_id, plan_card_id)
    project_agent = plan_card_id == coordination_store.PROJECT_AGENT_ID
    if key in _weak_dispatching or (not project_agent and not procs.is_primary_runner()):
        return
    if project_agent and (policy_runner._off() or not coordination_runner.project_inspection_rules(
            work_id, _current_appt(work_id, plan_card_id))):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    window = None if project_agent else merge_window_sec(work_id, plan_card_id)

    def _ready(conn: sqlite3.Connection) -> str:
        for row in conn.execute("SELECT batch_id,first_at,status FROM coordination_batches"
                                " WHERE assignment_id=? AND status IN ('open','running') ORDER BY id",
                                (assignment_id,)):
            stamp = _parse_iso(row["first_at"])
            if window is None or row["status"] == "running" or (stamp is not None and stamp + window <= now()):
                return row["batch_id"]
        return ""

    batch_id = card_db.read_sync(work_id, _ready, "")
    if not batch_id:
        return

    async def _run() -> None:
        result = None
        try:
            if procs.is_runner() and procs.owner_index_of(work_id, plan_card_id) != procs.runner_index():
                result = await procs.forward_ctl(work_id, plan_card_id, "/ctl/coordination_turn", {
                    "work_id": work_id, "card_id": plan_card_id, "assignment_id": assignment_id,
                    "trigger": "weak_observation", "request_id": batch_id,
                })
            else:
                result = await run_owned_batch(work_id, plan_card_id, assignment_id, batch_id)
        except Exception as exc:
            logger.warning("[coordination_watcher] 弱模型批次失败 %s: %s", batch_id, exc)
        finally:
            _weak_dispatching.discard(key)
        if project_agent and isinstance(result, dict) \
                and result.get("status") in ("passed", "unjudged", "wake", "filtered", "joined"):
            _maybe_dispatch(work_id, plan_card_id, assignment_id)

    _weak_dispatching.add(key)
    _track(loop.create_task(_run()))


# ---------------------------------------------------------------- 到期与 tick
# 按计划卡加锁，任命、点名、节点、弱观察提请及到期共用属主入口。
_running_turns: set[tuple[str, str]] = set()
_dispatching: set[tuple[str, str]] = set()
_STRONG_REASONS = ("review_due", "appoint", "user_wake", "direct_node", "weak_wake")
_PROJECT_STRONG_REASONS = ("weak_wake", "user_wake")


def _track(task: asyncio.Task) -> None:
    from ..storage.event_log import _pending
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def run_owned_turn(work_id: str, plan_card_id: str, assignment_id: str,
                         trigger: str, request_id: str) -> dict[str, Any]:
    from . import procs
    from ..api import coordination_runner

    if not procs.runner_side() or (procs.is_runner() and
            procs.owner_index_of(work_id, plan_card_id) != procs.runner_index()):
        return {"status": "wrong_owner"}
    key = (work_id, plan_card_id)
    if key in _running_turns:
        return {"status": "busy"}
    appt = _current_appt(work_id, plan_card_id)
    reasons = (_PROJECT_STRONG_REASONS if plan_card_id == coordination_store.PROJECT_AGENT_ID
               else _STRONG_REASONS)
    if (appt.get("assignment_id") != assignment_id or appt.get("state") != "active"
            or trigger not in reasons or not request_id):
        return {"status": "skipped"}
    pending = next((p for p in appt.get("pending", [])
                    if p.get("kind") == trigger
                    and coordination_store.wake_key(p) == request_id), None)
    if pending is None:
        return {"status": "skipped"}
    reason = None
    if trigger in ("weak_wake", "direct_node"):
        if plan_card_id != coordination_store.PROJECT_AGENT_ID or pending.get("run_id"):
            reason = coordination_runner.authorized_wake_reason(work_id, appt, pending)
        if reason is None:
            await coordination_store.consume_wake(work_id, assignment_id, trigger, request_id)
            if pending.get("run_id"):
                run = coordination_store.get_run(work_id, pending["run_id"])
                if run.get("phase") in ("waiting", "working"):
                    await coordination_store.set_run_phase(
                        work_id, assignment_id, pending["run_id"],
                        phase="archived", trigger=trigger, previous=run["phase"])
            return {"status": "skipped"}
    elif trigger == "user_wake":
        reason = {"note": str(pending.get("note") or "")}
    _running_turns.add(key)
    try:
        kwargs = {"reason": reason} if reason else {}
        result = await coordination_runner.run_strong_first_turn(
            work_id, plan_card_id, assignment_id=assignment_id, trigger=trigger, **kwargs)
        if result["status"] == "error" and result.get("native_count") is not None:
            await keep_unseen_inspection_events(work_id, assignment_id, request_id, result["native_count"])
            _maybe_dispatch(work_id, plan_card_id, assignment_id)
        if trigger != "review_due" and (result.get("status") == "parsed"
                or (reason and reason.get("run_id") and result.get("status") in
                    ("unparsed", "error", "skipped"))):
            await coordination_store.consume_wake(work_id, assignment_id, trigger, request_id)
        return result
    finally:
        _running_turns.discard(key)


async def dispatch_turn(work_id: str, plan_card_id: str, assignment_id: str,
                        trigger: str, request_id: str) -> dict[str, Any]:
    from . import procs

    if procs.is_runner() and procs.owner_index_of(work_id, plan_card_id) != procs.runner_index():
        result = await procs.forward_ctl(work_id, plan_card_id, "/ctl/coordination_turn", {
            "work_id": work_id, "card_id": plan_card_id, "assignment_id": assignment_id,
            "trigger": trigger, "request_id": request_id,
        })
        return result if isinstance(result, dict) else {"status": "error"}
    return await run_owned_turn(work_id, plan_card_id, assignment_id, trigger, request_id)


def _spawn_strong_turn(work_id: str, plan_card_id: str, assignment_id: str,
                       trigger: str, request_id: str) -> bool:
    key = (work_id, plan_card_id)
    if key in _running_turns or key in _dispatching:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    async def _run() -> None:
        try:
            await dispatch_turn(work_id, plan_card_id, assignment_id, trigger, request_id)
        except Exception as exc:
            logger.warning("[coordination_watcher] 强模型轮失败 %s: %s", key, exc)
        finally:
            _dispatching.discard(key)

    _dispatching.add(key)
    _track(loop.create_task(_run()))
    return True


# ---------------------------------------------------------------- 服务接入

#: tick 周期（秒）：回看到期精度＝分钟级；到期值是绝对时间，早 tick 晚 tick
#: 只影响认领的先后，不损语义（60s 与 idle_sweeper 家族同量级偏小——统筹
#: 判断要赶在事件后第一时间，不是小时级归档）。
_TICK_SEC = 60.0

_TASK: "asyncio.Task | None" = None
_delivery_recovery: set[str] = set()


async def _recover_deliveries(work_id: str) -> None:
    from ..api import coordination_runner

    try:
        await coordination_runner.recover_deliveries(work_id, project_only=True)
    except Exception as exc:  # noqa: BLE001 下次 tick 继续补写
        logger.warning("[coordination_watcher] 收件恢复待重试 work=%s: %s", work_id, exc)
    finally:
        _delivery_recovery.discard(work_id)


def install() -> None:
    """装载统筹调度循环（tick 每 _TICK_SEC 扫全部 works）。

    [AGENTS.md] 驱动器/调度器/续跑全库一份只跑 runner-0——用
    ``procs.is_primary_runner()`` 自查闸（all 单进程或 runner-0）；
    web/condense 装载调用直接跳过。
    """
    global _TASK
    if _TASK is not None:
        return
    try:
        from . import procs
        if not procs.is_primary_runner():
            logger.info("[coordination] 非 primary runner，tick 不装载")
            return
    except Exception:  # noqa: BLE001 procs 读不到（测试）按 primary 处理
        pass

    async def _loop() -> None:
        while True:
            try:
                from ..storage import card_store
                root = card_store.WORKS_DIR
                if root.exists():
                    for d in sorted(root.iterdir()):
                        if d.is_dir() and (d / "work.json").is_file():
                            try:
                                tick(d.name, project_only=True)
                                if d.name not in _delivery_recovery:
                                    _delivery_recovery.add(d.name)
                                    _track(asyncio.create_task(_recover_deliveries(d.name)))
                            except Exception as exc:
                                logger.warning("[coordination_watcher] tick 失败 work=%s: %s", d.name, exc)
            except Exception as exc:  # noqa: BLE001 单轮失败不终止循环
                logger.warning("[coordination] tick 轮失败: %s", exc)
            await asyncio.sleep(_TICK_SEC)

    _TASK = asyncio.get_running_loop().create_task(_loop())
    logger.info("[coordination] 统筹调度循环已装载（%ss/tick）", _TICK_SEC)


def tick(work_id: str, *, project_only: bool = False) -> dict[str, Any]:
    """单进程或 runner-0 认领，欠办留到属主实际处理完成。"""
    from . import procs

    out = {"dispatched": 0, "review_marked": 0, "review_spawned": 0}
    if not procs.is_primary_runner():
        return out
    active = [p for p in coordination_store.list_plans(work_id, project_only=project_only)
              if p["state"] == "active"]
    if not active:
        return out
    try:
        _replay_committed(work_id, active, project_only=project_only)
    except Exception as exc:
        logger.warning("[coordination_watcher] 事件补读待重试 work=%s: %s", work_id, exc)
    for plan in active:
        try:
            _maybe_dispatch(work_id, plan["plan_card_id"], plan["assignment_id"])
        except sqlite3.Error as exc:
            logger.warning("[coordination_watcher] 弱批次待重试 work=%s: %s", work_id, exc)

    def _due(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
        jobs = []
        rows = conn.execute("SELECT * FROM coordination_appointments WHERE work_id=?"
                            " AND state='active' ORDER BY id", (work_id,))
        for row in rows:
            project_agent = row["plan_card_id"] == coordination_store.PROJECT_AGENT_ID
            if project_only and not project_agent:
                continue
            pending = json.loads(row["pending"] or "[]")
            due = row["next_review_at"]
            stamp = _parse_iso(due)
            overdue = not project_agent and stamp is not None and stamp <= now()
            if overdue and not any(p.get("kind") == "review_due" and p.get("due_at") == due
                                   for p in pending):
                pending = [p for p in pending if p.get("kind") != "review_due"]
                pending.append({"kind": "review_due", "due_at": due, "at": _iso(now())})
                conn.execute("UPDATE coordination_appointments SET pending=? WHERE id=?",
                             (coordination_store._dumps(pending), row["id"]))
                out["review_marked"] += 1
            reasons = _PROJECT_STRONG_REASONS if project_agent else _STRONG_REASONS
            for kind in reasons:
                request = next((p for p in pending if p.get("kind") == kind
                                and (kind != "review_due" or (overdue and p.get("due_at") == due))), None)
                if request is not None:
                    jobs.append((row["plan_card_id"], row["assignment_id"], kind,
                                 coordination_store.wake_key(request)))
                    break
        return jobs

    for card_id, assignment_id, trigger, request_id in card_db.write_sync(work_id, _due) or []:
        if _spawn_strong_turn(work_id, card_id, assignment_id, trigger, request_id):
            out["review_spawned" if trigger == "review_due" else "dispatched"] += 1
    return out
