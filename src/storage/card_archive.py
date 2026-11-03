# -*- coding: utf-8 -*-
"""卡片归档存储（card_archive）：**任何卡**折叠成一条记录，卡面从树上消失。

[2026-10-25 · 用户令] 「让所有卡都可以归档」——不加任何前置判断：
不看卡类型、不看 state、不看有没有子卡。理由（用户原话）：任务进行到一定
程度，**过程有价值但不需要继续**的卡，不该一直挂在树上；这类处置是「归档」，
不是「删除」，也不是「状态」。

与 coder_archive 的关系：
- `coder_archive` 是 2026-10-25 早先为**编程卡**做的折叠（字段是 task/final_report）；
- 本表是**通用**归档：保留三块正文（使命／现状／第三块）、修改历史、影子三节、
  会话引用——即"过程价值"的完整快照，而不只是回执。
- 两者并存：老 coder 归档记录仍可读（`read_archive` 先查本表、再回落 coder_archive）。

物理事实（与 coder 归档同口径）：会话（`sessions/*.md`）与事件流
（`events/*.jsonl`）是**按卡号寻址的独立文件**，归档**不动它们**——它们就是
剩下的过程记录；本表存的是卡面快照，让卡能被读懂、能被恢复。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import card_db

logger = logging.getLogger(__name__)

_ARCHIVE_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS card_archive (
    card_id      TEXT PRIMARY KEY,
    work_id      TEXT NOT NULL,
    card_type    TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    parent       TEXT DEFAULT '',
    mission      TEXT DEFAULT '',
    status_block TEXT DEFAULT '',
    requirements      TEXT DEFAULT '',
    shadow       TEXT DEFAULT '{}',
    history      TEXT DEFAULT '[]',
    session_ref  TEXT DEFAULT '',
    archived_at  TEXT DEFAULT '',
    reason       TEXT DEFAULT '',
    archived_by  TEXT DEFAULT '',
    meta         TEXT DEFAULT '{}'
)""",
    "CREATE INDEX IF NOT EXISTS idx_carchive_work ON card_archive(work_id, archived_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_carchive_parent ON card_archive(work_id, parent)",
)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _ensure(conn: sqlite3.Connection) -> None:
    for ddl in _ARCHIVE_DDL:
        conn.execute(ddl)
    # [库结构升级 2026-11-02] 老库第三块列名还是 rulings——
    # ``CREATE TABLE IF NOT EXISTS`` 对既有表不改结构，须显式迁移：
    # 有旧列无新列 → RENAME（数据原样保住）；两列皆无 → ADD。
    # 判据与 card_db._rename_columns 同口径，幂等。
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(card_archive)")}
    if "rulings" in cols and "requirements" not in cols:
        conn.execute("ALTER TABLE card_archive RENAME COLUMN rulings TO requirements")
    elif "requirements" not in cols:
        conn.execute(
            "ALTER TABLE card_archive ADD COLUMN requirements TEXT DEFAULT ''")


def _row_to_rec(row: sqlite3.Row) -> dict[str, Any]:
    def _load(raw: Any, default: Any) -> Any:
        try:
            return json.loads(raw) if isinstance(raw, str) and raw else default
        except (TypeError, ValueError):
            return default

    # 未迁移老表第三块还叫 rulings（读路径容错）：按 requirements 语义返回。
    names = set(row.keys())
    third = (row["requirements"] if "requirements" in names
             else row["rulings"] if "rulings" in names else "")
    return {
        "card_id": row["card_id"],
        "card_type": row["card_type"] or "",
        "title": row["title"] or "",
        "parent": row["parent"] or "",
        "mission": row["mission"] or "",
        "status_block": row["status_block"] or "",
        "requirements": third or "",
        "shadow": _load(row["shadow"], {}),
        "history": _load(row["history"], []),
        "session_ref": row["session_ref"] or "",
        "archived_at": row["archived_at"] or "",
        "reason": row["reason"] or "",
        "archived_by": row["archived_by"] or "",
        # 整行 meta 原样存/取——恢复要无损（perms/links/state/时间戳都在里面）
        "meta": _load(row["meta"], {}),
    }


def get_record(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """读一条归档记录（无记录/表未建 ⇒ None）。"""

    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        try:
            row = conn.execute(
                "SELECT * FROM card_archive WHERE work_id=? AND card_id=?",
                (work_id, card_id)).fetchone()
        except sqlite3.OperationalError:
            return None
        return _row_to_rec(row) if row is not None else None

    return card_db.read_sync(work_id, _fn, None)


def list_records(work_id: str, parent: str = "") -> list[dict[str, Any]]:
    """列归档记录（parent 空＝全部；按归档时间倒序）。"""

    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            if parent:
                rows = conn.execute(
                    "SELECT * FROM card_archive WHERE work_id=? AND parent=?"
                    " ORDER BY archived_at DESC", (work_id, parent)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM card_archive WHERE work_id=?"
                    " ORDER BY archived_at DESC", (work_id,)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [_row_to_rec(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


async def archive(work_id: str, card_id: str, rec: dict[str, Any]) -> bool:
    """写归档记录（幂等：同卡号重归档以最新为准）。"""
    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        conn.execute(
            "INSERT INTO card_archive"
            " (card_id, work_id, card_type, title, parent, mission, status_block,"
            "  requirements, shadow, history, session_ref, archived_at, reason,"
            "  archived_by, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(card_id) DO UPDATE SET"
            "  card_type=excluded.card_type, title=excluded.title,"
            "  parent=excluded.parent, mission=excluded.mission,"
            "  status_block=excluded.status_block, requirements=excluded.requirements,"
            "  shadow=excluded.shadow, history=excluded.history,"
            "  session_ref=excluded.session_ref, archived_at=excluded.archived_at,"
            "  reason=excluded.reason, archived_by=excluded.archived_by,"
            "  meta=excluded.meta",
            (card_id, work_id,
             str(rec.get("card_type") or ""), str(rec.get("title") or ""),
             str(rec.get("parent") or ""), str(rec.get("mission") or ""),
             str(rec.get("status_block") or ""), str(rec.get("requirements") or ""),
             json.dumps(rec.get("shadow") or {}, ensure_ascii=False),
             json.dumps(rec.get("history") or [], ensure_ascii=False),
             str(rec.get("session_ref") or ""), _now(),
             str(rec.get("reason") or ""), str(rec.get("archived_by") or ""),
             json.dumps(rec.get("meta") or {}, ensure_ascii=False)))

    await card_db.run_write(work_id, _fn)
    return True


async def remove(work_id: str, card_id: str) -> bool:
    """删归档记录（复活路径用——cards 行由调用方重建）。"""

    def _fn(conn: sqlite3.Connection) -> bool:
        _ensure(conn)
        cur = conn.execute(
            "DELETE FROM card_archive WHERE work_id=? AND card_id=?",
            (work_id, card_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))


def archived_children(work_id: str, parent: str) -> int:
    """某父卡名下的归档子卡数（阶段收口判定用——归档＝天然已完成）。"""

    def _fn(conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM card_archive WHERE work_id=? AND parent=?",
                (work_id, parent)).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    return int(card_db.read_sync(work_id, _fn, 0) or 0)
