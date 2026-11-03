# -*- coding: utf-8 -*-
"""coder 卡归档存储(coder_archive):完工编程卡从「完整卡片形态」折叠成「仅剩记录」。

[2026-10-25 · 用户令「完工静默的 coder 卡可折叠——视觉到数据;数据上变成
归档记录,不再是完整卡片形态,仅剩记录,模型仍可查」]

物理事实(设计依据):coder 卡的**过程记录本来就不在 cards 行里**——会话
(``sessions/*.md``)与事件流(``events/*.jsonl``)是按卡号寻址的独立文件,
cards 行只是「卡面/权限/位置/goal」这些活体属性。归档=把 cards 行搬进
档案表(活体属性消失),会话/事件**原位不动**——它们就是剩下的记录。

DDL 放本模块(card_db 是禁改文件,独立表归各自存储模块——
function_directives/governance_store 先例)。影子行随归档提取产物清单后
删除(影子是本卡私有过程区,归档后不再是卡,无影子可言)。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import card_db

logger = logging.getLogger(__name__)

_ARCHIVE_DDL = """CREATE TABLE IF NOT EXISTS coder_archive (
    card_id       TEXT PRIMARY KEY,
    work_id       TEXT NOT NULL,
    title         TEXT DEFAULT '',
    parent        TEXT DEFAULT '',
    task          TEXT DEFAULT '',
    final_report  TEXT DEFAULT '',
    artifacts     TEXT DEFAULT '[]',
    completed_at  TEXT DEFAULT ''
)"""


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_ARCHIVE_DDL)


def get_record(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """读一条归档记录(模型可查的数据面;无记录/表未建 ⇒ None)。"""

    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        try:
            row = conn.execute(
                "SELECT * FROM coder_archive WHERE work_id=? AND card_id=?",
                (work_id, card_id)).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        import json as _json
        return {
            "card_id": row["card_id"], "title": row["title"] or "",
            "parent": row["parent"] or "", "task": row["task"] or "",
            "final_report": row["final_report"] or "",
            "artifacts": _json.loads(row["artifacts"] or "[]"),
            "completed_at": row["completed_at"] or "",
        }

    return card_db.read_sync(work_id, _fn, None)


def list_records(work_id: str, parent: str = "") -> list[dict[str, Any]]:
    """列归档记录(parent 空=全部;按完成时间倒序)。"""

    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        import json as _json
        try:
            if parent:
                rows = conn.execute(
                    "SELECT * FROM coder_archive WHERE work_id=? AND parent=?"
                    " ORDER BY completed_at DESC",
                    (work_id, parent)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM coder_archive WHERE work_id=?"
                    " ORDER BY completed_at DESC", (work_id,)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for row in rows:
            out.append({
                "card_id": row["card_id"], "title": row["title"] or "",
                "parent": row["parent"] or "", "task": row["task"] or "",
                "final_report": row["final_report"] or "",
                "artifacts": _json.loads(row["artifacts"] or "[]"),
                "completed_at": row["completed_at"] or "",
            })
        return out

    return card_db.read_sync(work_id, _fn, []) or []


async def archive(work_id: str, card_id: str, *, task: str = "",
                  final_report: str = "", title: str = "", parent: str = "",
                  artifacts: Optional[list] = None) -> bool:
    """写档案行(幂等:覆盖写——同卡号重归档以最新为准)。"""
    import json as _json

    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        conn.execute(
            "INSERT INTO coder_archive"
            " (card_id, work_id, title, parent, task, final_report,"
            "  artifacts, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(card_id) DO UPDATE SET"
            " title=excluded.title, parent=excluded.parent,"
            " task=excluded.task, final_report=excluded.final_report,"
            " artifacts=excluded.artifacts, completed_at=excluded.completed_at",
            (card_id, work_id, title, parent, task, final_report,
             _json.dumps(list(artifacts or [])), _now()))

    await card_db.run_write(work_id, _fn)
    return True


async def remove(work_id: str, card_id: str) -> bool:
    """解档:删档案行(复活路径用——cards 行由调用方重建)。"""

    def _fn(conn: sqlite3.Connection) -> bool:
        _ensure(conn)
        cur = conn.execute(
            "DELETE FROM coder_archive WHERE work_id=? AND card_id=?",
            (work_id, card_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))


def archived_children(work_id: str, parent: str) -> int:
    """某父卡名下的归档子卡数(阶段收口判定用——归档=天然 completed)。"""

    def _fn(conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM coder_archive"
                " WHERE work_id=? AND parent=?", (work_id, parent)).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    return int(card_db.read_sync(work_id, _fn, 0) or 0)
