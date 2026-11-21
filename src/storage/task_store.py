# -*- coding: utf-8 -*-
"""任务台账（B 批 · 执行卡开工依据与验收核证）。

一份任务一条账，绑定**原派工编号**：责任卡、需求版本与条款、当前允许步骤、
暂缓事项、验收判据、注入水位（pending→injected，一次性）与交付回执。

- 注入水位是**按任务**的：同一责任卡上别的任务取不走这份依据（区别于
  ``cards.pending_inject`` 的卡级共享队列——那个谁先到谁先拿）。
- 装配失败不消费水位：``basis_text`` 组装抛错时调用方不得调用
  ``mark_injected``——任务留在 pending，下一轮重试（可恢复）。
- 需求换版：``req_fp``/``clause_map`` 记录登记时的版本指纹与条款定位；
  改版后由 ``task_basis.realign_check`` 只把**受影响**任务打回
  ``status='realign' + inject_state='pending'``，不受影响任务只更新指纹。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Optional

from . import card_db

_TASK_DDL = """CREATE TABLE IF NOT EXISTS task_ledger (
    task_id      TEXT PRIMARY KEY,
    work_id      TEXT NOT NULL,
    owner_card   TEXT NOT NULL,
    dispatch_id  TEXT DEFAULT '',
    source_msg_id TEXT DEFAULT '',
    req_card     TEXT DEFAULT '',
    req_fp       TEXT DEFAULT '',
    clause_map   TEXT DEFAULT '{}',
    clauses      TEXT DEFAULT '[]',
    allowed      TEXT DEFAULT '[]',
    deferred     TEXT DEFAULT '[]',
    accept       TEXT DEFAULT '',
    criteria     TEXT DEFAULT '[]',
    target_card  TEXT DEFAULT '',
    status       TEXT DEFAULT 'open',
    inject_state TEXT DEFAULT 'pending',
    created_at   TEXT,
    updated_at   TEXT
)"""

_DELIVERY_DDL = """CREATE TABLE IF NOT EXISTS task_deliveries (
    delivery_id TEXT PRIMARY KEY,
    work_id     TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    card_id     TEXT NOT NULL,
    claims      TEXT DEFAULT '[]',
    verdicts    TEXT DEFAULT '[]',
    status      TEXT DEFAULT '',
    created_at  TEXT,
    req_fp      TEXT DEFAULT ''
)"""

_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_task_owner "
    "ON task_ledger(work_id, owner_card, status)",
    "CREATE INDEX IF NOT EXISTS idx_task_delivery "
    "ON task_deliveries(work_id, task_id)",
)

_STATUSES = ("open", "realign", "closed")
_INJECT_STATES = ("pending", "injected")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_TASK_DDL)
    conn.execute(_DELIVERY_DDL)
    for sql in _INDEX:
        conn.execute(sql)
    if not any(r[1] == "req_fp" for r in conn.execute("PRAGMA table_info(task_deliveries)")):
        conn.execute("ALTER TABLE task_deliveries ADD COLUMN req_fp TEXT DEFAULT ''")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """读侧探测（``read_sync`` 是只读连接——DDL 只允许在写事务里跑）。"""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _task_row(r: sqlite3.Row) -> dict[str, Any]:
    def _jl(v: str, default: Any) -> Any:
        try:
            return json.loads(v or "") or default
        except Exception:  # noqa: BLE001 账上 JSON 坏＝按空处理，不炸读取
            return default

    return {
        "task_id": r["task_id"],
        "owner_card": r["owner_card"],
        "dispatch_id": r["dispatch_id"] or "",
        "source_msg_id": r["source_msg_id"] or "",
        "req_card": r["req_card"] or "",
        "req_fp": r["req_fp"] or "",
        "clause_map": _jl(r["clause_map"], {}),
        "clauses": _jl(r["clauses"], []),
        "allowed": _jl(r["allowed"], []),
        "deferred": _jl(r["deferred"], []),
        "accept": r["accept"] or "",
        "criteria": _jl(r["criteria"], []),
        "target_card": r["target_card"] or "",
        "status": r["status"] or "open",
        "inject_state": r["inject_state"] or "pending",
        "created_at": r["created_at"] or "",
        "updated_at": r["updated_at"] or "",
    }


def register_task(work_id: str, *, owner_card: str, task_id: str = "",
                  dispatch_id: str = "", source_msg_id: str = "",
                  req_card: str = "", req_fp: str = "",
                  clause_map: Optional[dict[str, Any]] = None,
                  clauses: Optional[list[str]] = None,
                  allowed: Optional[list[str]] = None,
                  deferred: Optional[list[str]] = None,
                  accept: str = "",
                  criteria: Optional[list[Any]] = None,
                  target_card: str = "") -> dict[str, Any]:
    """登记一份任务（幂等：同 task_id 重复登记返回既有账）。"""
    tid = str(task_id or "").strip() or uuid.uuid4().hex[:12]
    rec = {
        "task_id": tid,
        "owner_card": owner_card,
        "dispatch_id": dispatch_id,
        "source_msg_id": source_msg_id,
        "req_card": req_card,
        "req_fp": req_fp,
        "clause_map": dict(clause_map or {}),
        "clauses": list(clauses or []),
        "allowed": list(allowed or []),
        "deferred": list(deferred or []),
        "accept": accept,
        "criteria": list(criteria or []),
        "target_card": target_card,
        "status": "open",
        "inject_state": "pending",
        "created_at": _now(),
        "updated_at": _now(),
    }

    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        conn.execute(
            "INSERT OR IGNORE INTO task_ledger"
            " (task_id, work_id, owner_card, dispatch_id, source_msg_id,"
            "  req_card, req_fp, clause_map, clauses, allowed, deferred,"
            "  accept, criteria, target_card, status, inject_state,"
            "  created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, work_id, owner_card, dispatch_id, source_msg_id,
             req_card, req_fp, json.dumps(rec["clause_map"]),
             json.dumps(rec["clauses"]), json.dumps(rec["allowed"]),
             json.dumps(rec["deferred"]), accept,
             json.dumps(rec["criteria"]), target_card,
             "open", "pending", rec["created_at"], rec["updated_at"]))

    card_db.write_sync(work_id, _fn)
    saved = get_task(work_id, tid)
    if saved is None:
        raise RuntimeError("未读回派工任务登记")
    return saved


def get_task(work_id: str, task_id: str) -> Optional[dict[str, Any]]:
    """沿原派工编号核查——恢复、验收、不明时对账都走这一个入口。"""
    tid = str(task_id or "").strip()
    if not tid:
        return None

    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        if not _has_table(conn, "task_ledger"):
            return None
        row = conn.execute(
            "SELECT * FROM task_ledger WHERE work_id=? AND task_id=?",
            (work_id, tid)).fetchone()
        return _task_row(row) if row is not None else None

    return card_db.read_sync(work_id, _fn, None)


def by_dispatch(work_id: str, dispatch_id: str) -> Optional[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        if not _has_table(conn, "task_ledger"):
            return None
        rows = conn.execute(
            "SELECT * FROM task_ledger WHERE work_id=?"
            " AND (dispatch_id=? OR source_msg_id=?) LIMIT 2",
            (work_id, dispatch_id, dispatch_id)).fetchall()
        return _task_row(rows[0]) if len(rows) == 1 else None

    return card_db.read_sync(work_id, _fn, None) if dispatch_id else None


def open_tasks(work_id: str) -> list[dict[str, Any]]:
    """open + realign 两态都算活着的任务（改版重排队靠这张单）。"""

    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if not _has_table(conn, "task_ledger"):
            return []
        return [_task_row(r) for r in conn.execute(
            "SELECT * FROM task_ledger WHERE work_id=? AND status != 'closed'",
            (work_id,)).fetchall()]

    return card_db.read_sync(work_id, _fn, []) or []


def tasks_for_card(work_id: str, owner_card: str) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if not _has_table(conn, "task_ledger"):
            return []
        return [_task_row(r) for r in conn.execute(
            "SELECT * FROM task_ledger WHERE work_id=? AND owner_card=?",
            (work_id, owner_card)).fetchall()]

    return card_db.read_sync(work_id, _fn, []) or []


def mark_injected(work_id: str, task_id: str, owner_card: str,
                  req_fp: str) -> bool:
    """注入水位 pending→injected（一次性）。**责任卡不符/已注入 ⇒ False**。"""
    done = {"ok": False}

    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        cur = conn.execute(
            "UPDATE task_ledger SET inject_state='injected', updated_at=?"
            " WHERE work_id=? AND task_id=? AND owner_card=?"
            "   AND inject_state='pending' AND req_fp=? AND status!='closed'",
            (_now(), work_id, str(task_id), owner_card, req_fp))
        done["ok"] = cur.rowcount > 0

    card_db.write_sync(work_id, _fn)
    return bool(done["ok"])


def set_task_state(work_id: str, task_id: str, *, status: str = "",
                   inject_state: str = "", req_fp: str = "",
                   clause_map: Optional[dict[str, Any]] = None,
                   expect_fp: Optional[str] = None) -> bool:
    """改版重对齐/收口的状态推进（空参数不动对应字段）。"""
    done = {"ok": False}

    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        sets, args = ["updated_at=?"], [_now()]
        if status:
            sets.append("status=?")
            args.append(status if status in _STATUSES else "open")
        if inject_state:
            sets.append("inject_state=?")
            args.append(inject_state
                        if inject_state in _INJECT_STATES else "pending")
        if req_fp:
            sets.append("req_fp=?")
            args.append(req_fp)
        if clause_map is not None:
            sets.append("clause_map=?")
            args.append(json.dumps(clause_map))
        args += [work_id, str(task_id)]
        predicate = ""
        if expect_fp is not None:
            predicate = " AND req_fp=? AND status!='closed'"
            args.append(expect_fp)
        cur = conn.execute(
            f"UPDATE task_ledger SET {', '.join(sets)}"
            " WHERE work_id=? AND task_id=?" + predicate, tuple(args))
        done["ok"] = cur.rowcount > 0

    card_db.write_sync(work_id, _fn)
    return bool(done["ok"])


def record_delivery(work_id: str, *, task_id: str, card_id: str,
                    claims: list[dict[str, Any]],
                    verdicts: list[dict[str, Any]],
                    status: str = "", req_fp: str = "") -> Optional[dict[str, Any]]:
    """交付回执落账——claims 是执行卡声明的证据锚，verdicts 是逐条机械判定。"""
    import hashlib

    identity = json.dumps([work_id, task_id, card_id, req_fp, verdicts],
                          sort_keys=True, ensure_ascii=False)
    did = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    rec = {
        "delivery_id": did,
        "task_id": str(task_id),
        "card_id": card_id,
        "claims": list(claims or []),
        "verdicts": list(verdicts or []),
        "status": status,
        "req_fp": req_fp,
        "created_at": _now(),
    }

    def _fn(conn: sqlite3.Connection) -> bool:
        _ensure(conn)
        row = conn.execute(
            "SELECT t.req_fp, c.requirements FROM task_ledger t"
            " LEFT JOIN cards c ON c.card_id=t.req_card"
            " WHERE t.work_id=? AND t.task_id=? AND t.owner_card=? AND t.status!='closed'",
            (work_id, task_id, card_id)).fetchone()
        if not row or row["req_fp"] != req_fp:
            return False
        if status == "passed":
            from ..api.doc_locator import doc_fingerprint
            if not row["requirements"] or doc_fingerprint(row["requirements"]) != req_fp:
                return False
        conn.execute(
            "INSERT OR IGNORE INTO task_deliveries"
            " (delivery_id, work_id, task_id, card_id, claims, verdicts,"
            "  status, created_at, req_fp) VALUES (?,?,?,?,?,?,?,?,?)",
            (did, work_id, str(task_id), card_id, json.dumps(rec["claims"]),
             json.dumps(rec["verdicts"]), status, rec["created_at"], req_fp))
        return True

    return rec if card_db.write_sync(work_id, _fn) else None


def get_delivery(work_id: str, task_id: str) -> Optional[dict[str, Any]]:
    """最近一次交付回执（投递结果不明时沿原编号取这份核）。"""

    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        if not _has_table(conn, "task_deliveries"):
            return None
        row = conn.execute(
            "SELECT * FROM task_deliveries WHERE work_id=? AND task_id=?"
            " ORDER BY rowid DESC LIMIT 1",
            (work_id, str(task_id))).fetchone()
        if row is None:
            return None
        try:
            claims = json.loads(row["claims"] or "[]")
            verdicts = json.loads(row["verdicts"] or "[]")
        except Exception:  # noqa: BLE001
            claims, verdicts = [], []
        return {"delivery_id": row["delivery_id"], "task_id": row["task_id"],
                "card_id": row["card_id"], "claims": claims,
                "verdicts": verdicts, "status": row["status"] or "",
                "req_fp": row["req_fp"] if "req_fp" in row.keys() else "",
                "created_at": row["created_at"] or ""}

    return card_db.read_sync(work_id, _fn, None)
