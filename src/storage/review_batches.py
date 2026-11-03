# -*- coding: utf-8 -*-
"""评审批次存储(review_batches):收集齐才唤醒总裁——全状态判定,零墙钟。

[2026-10-25 · 用户裁定「评审在收集齐之后才回发唤醒」+「不要设墙钟,
全部都用状态判断」]

模型:
- review_request 开一个批次(记应到名单=当时全部在职职能);
- 每份 submit_review 落到批次上(提交即记账);
- 纯状态判定:应到名单里每个职能都有结论 ⇒ 收齐 ⇒ 唤醒总裁一次(最后
  一人提交时状态翻转,即刻触发,无等待窗口);
- 停职/失能者在判定时被剔除(duty 状态);全部失能 ⇒ 批次立刻作废并
  回执发起方——这是当次判定的结果,不是超时。

DDL 放本模块(card_db 是禁改文件,独立表归各自存储模块)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import card_db, function_store

logger = logging.getLogger(__name__)

_BATCH_DDL = """CREATE TABLE IF NOT EXISTS review_batches (
    batch_id    TEXT PRIMARY KEY,
    work_id     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    target_card TEXT DEFAULT '',
    focus       TEXT DEFAULT '',
    expected    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'open'
)"""

_SUBMISSION_DDL = """CREATE TABLE IF NOT EXISTS review_submissions (
    batch_id     TEXT NOT NULL,
    function_id  TEXT NOT NULL,
    ts           TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    concerns     TEXT DEFAULT '',
    divergence   INTEGER DEFAULT 0,
    PRIMARY KEY (batch_id, function_id)
)"""


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_BATCH_DDL)
    conn.execute(_SUBMISSION_DDL)


async def open_batch(work_id: str, subject: str, target_card: str = "",
                     focus: str = "",
                     expected: list[str] | None = None) -> str:
    """开一个评审批次。expected=应到职能名单(调用方现算,通常=全部在职)。"""
    batch_id = f"rv_{uuid.uuid4().hex[:12]}"

    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        conn.execute(
            "INSERT INTO review_batches"
            " (batch_id, work_id, subject, target_card, focus, expected,"
            "  created_at, status)"
            " VALUES (?,?,?,?,?,?,?, 'open')",
            (batch_id, work_id, subject, target_card, focus,
             json.dumps(list(expected or [])), _now()))

    await card_db.run_write(work_id, _fn)
    return batch_id


def get_batch(work_id: str, batch_id: str) -> Optional[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        try:
            row = conn.execute(
                "SELECT * FROM review_batches"
                " WHERE work_id=? AND batch_id=?",
                (work_id, batch_id)).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        d = dict(row)
        d["expected"] = json.loads(d.get("expected") or "[]")
        subs = conn.execute(
            "SELECT * FROM review_submissions WHERE batch_id=?"
            " ORDER BY ts", (batch_id,)).fetchall()
        d["submissions"] = [dict(s) for s in subs]
        return d

    return card_db.read_sync(work_id, _fn, None)


def open_batch_for(work_id: str, target_card: str) -> Optional[dict[str, Any]]:
    """目标卡当前未收口的评审批次(发起评审去重用)。"""

    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        try:
            row = conn.execute(
                "SELECT * FROM review_batches"
                " WHERE work_id=? AND target_card=? AND status='open'"
                " ORDER BY created_at DESC LIMIT 1",
                (work_id, target_card)).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        d = dict(row)
        d["expected"] = json.loads(d.get("expected") or "[]")
        return d

    return card_db.read_sync(work_id, _fn, None)


async def submit(work_id: str, batch_id: str, function_id: str,
                 verdict: str, concerns: str = "",
                 divergence: bool = False) -> dict[str, Any]:
    """把一份评审结论落到批次上,**写与收齐判定在同一写事务**(防并发竞态)。

    [2026-10-25 · 用户质询「工具并发了会怎么样」] 改前 submit(写)与
    completeness(读)分两个事务——最后两人同时提交时:各自的判定都读不到
    对方刚写的行 ⇒ 双方都判"未收齐" ⇒ **批次永不唤醒总裁**(check-then-act
    竞态)。现在写+重读收进同一个 BEGIN IMMEDIATE 事务:SQLite 写事务串行
    ⇒ 恰好一个提交者看到 collected=True,那个人负责唤醒(恰好一次)。
    """
    from . import function_store as _fs

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure(conn)
        conn.execute(
            "INSERT INTO review_submissions"
            " (batch_id, function_id, ts, verdict, concerns, divergence)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(batch_id, function_id) DO UPDATE SET"
            " ts=excluded.ts, verdict=excluded.verdict,"
            " concerns=excluded.concerns, divergence=excluded.divergence",
            (batch_id, function_id, _now(), verdict,
             concerns, 1 if divergence else 0))
        # 事务内重读(此时别人的写被串行化挡在外面,看到的是全量)
        brow = conn.execute(
            "SELECT expected FROM review_batches"
            " WHERE work_id=? AND batch_id=?",
            (work_id, batch_id)).fetchone()
        expected = json.loads(brow["expected"]) if brow else []
        # 在职判定(duty)也在此读——同事务一致性
        duty = {p["function_id"]: (p.get("duty") or {}).get("state")
                for p in _fs.list_profiles(work_id)}
        active = [f for f in expected
                  if str(duty.get(f) or "active") == "active"]
        subs = {r["function_id"] for r in conn.execute(
            "SELECT function_id FROM review_submissions WHERE batch_id=?",
            (batch_id,)).fetchall()}
        missing = [f for f in active if f not in subs]
        return {
            "collected": sorted(subs & set(active)),
            "expected": active,
            "missing": missing,
            "complete": bool(active) and not missing,
            "void": not active,
        }

    return await card_db.run_write(work_id, _fn)


def active_expected(work_id: str, expected: list[str]) -> list[str]:
    """应到名单里**仍在职**的(停职/失能者剔除——纯 duty 状态判定)。"""
    out = []
    try:
        duty = {p.get("function_id"): (p.get("duty") or {}).get("state")
                for p in function_store.list_profiles(work_id)}
    except Exception:  # noqa: BLE001 判不出 ⇒ 按原名单(宁可多等一次人工处置)
        return list(expected or [])
    for fid in (expected or []):
        if str(duty.get(fid) or "active") == "active":
            out.append(str(fid))
    return out


def completeness(work_id: str, batch_id: str) -> dict[str, Any]:
    """纯状态判定:应到(在职)名单 vs 已提交名单。

    返回 {collected, expected, missing, complete, void}:
    - complete=True ⇒ 收齐(调用方即刻唤醒总裁,无等待);
    - void=True ⇒ 应到者全部失能 ⇒ 批次作废(回执发起方);
    - 二者皆假 ⇒ 还没齐,继续收集(谁都不唤醒)。
    """
    b = get_batch(work_id, batch_id)
    if b is None:
        return {"collected": [], "expected": [], "missing": [],
                "complete": False, "void": False}
    expected = active_expected(work_id, b.get("expected") or [])
    submitted = {str(s.get("function_id")) for s in (b.get("submissions") or [])}
    missing = [f for f in expected if f not in submitted]
    return {
        "collected": sorted(submitted & set(expected)),
        "expected": expected,
        "missing": missing,
        "complete": bool(expected) and not missing,
        "void": not expected,
    }


async def close_batch(work_id: str, batch_id: str,
                       status: str = "collected") -> None:
    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        conn.execute(
            "UPDATE review_batches SET status=?"
            " WHERE work_id=? AND batch_id=?",
            (status, work_id, batch_id))

    await card_db.run_write(work_id, _fn)


def compose_summary(work_id: str, batch_id: str) -> str:
    """汇总文本(收齐后投给总裁):各职能结论+分歧标记。"""
    b = get_batch(work_id, batch_id)
    if b is None:
        return ""
    names = {str(p.get("function_id")): str(p.get("function_name")
                                             or p.get("function_id"))
             for p in function_store.list_profiles(work_id)}
    lines = [f"〔评审收齐·{b.get('subject') or batch_id}〕"]
    if b.get("target_card"):
        lines.append(f"评审对象:{b['target_card']}")
    if b.get("focus"):
        lines.append(f"评审焦点:{b['focus']}")
    lines.append("")
    for s in (b.get("submissions") or []):
        fid = str(s.get("function_id"))
        flag = "〔分歧·优先看〕" if s.get("divergence") else ""
        lines.append(f"- {names.get(fid, fid)}{flag}:"
                     f"{str(s.get('verdict') or '')[:500]}")
        c = str(s.get("concerns") or "").strip()
        if c:
            lines.append(f"  关注点:{c[:300]}")
    return "\n".join(lines)
