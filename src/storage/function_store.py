# -*- coding: utf-8 -*-
"""职能（function）的结构化存储（5713 · function 实体化第一步 · card_028）。

职能＝「角色」剥掉卡的皮之后的独立实体：身份用**独立号段** ``fn_001``／
``fn_002``…（不再是 card_NNN），**不写 cards 表**——因此不出现在画布、
卡片清单、卡树、以及任何按卡遍历的地方。

五张表（``card_db._FUNCTION_DDL``，幂等 CREATE，存量 work 首次连接即补建）：

- ``function_profiles``：职能档案（``function_name`` 由调用方自由填——模型定的
  职能名如「质量管理」，本层只存不管内容、不设白名单；``charter`` 职责书）
- ``function_verdicts``：判断簿（decision/reason/sources/targets/evidence/被取代）
- ``function_alarms``：警报簿（severity/status/ack/更正链）
- ``function_sync``：待巡检同步队列（来源、seq 区间、命中信号、pending/consumed）
- ``function_charter_history``：职责书版本史

列与语义照搬 role_* 五张表（纯改名迁移：card_id/role_id → function_id），
没有顺手改任何语义；``function_profiles`` 按任务书额外带 ``function_name`` 列
——职能没有 cards 行，职能名只能自己扛，不再借 cards.title。

发号先例（照 cards）：``meta.fn_seq`` 独立号段，**发号与建行同一写事务**
（``BEGIN IMMEDIATE`` 内取号 → INSERT → 写回 fn_seq），并发下不重号。

本模块是纯存储：只 import ``card_db``，不 import card_store / api，避免环
（与 role_store 同一纪律）。存量 role_* 路径本步不删不改——新旧两套并存，
旧路径行为逐字不变。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from . import card_db


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat()


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        v = json.loads(str(raw or ""))
        return v if isinstance(v, type(default)) else default
    except Exception:  # noqa: BLE001
        return default


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


# ============================================================
# 职能档案
# ============================================================

def _row_to_profile(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "function_id": row["function_id"],
        "function_name": row["function_name"] or "",
        "work_id": row["work_id"],
        "jurisdiction": _loads(row["jurisdiction"], {}),
        "watch": _loads(row["watch"], {}),
        "speak_contract": _loads(row["speak_contract"], {}),
        "escalation": _loads(row["escalation"], {}),
        "duty": _loads(row["duty"], {}),
        "charter": _loads(row["charter"], {}),
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def get_profile(work_id: str, function_id: str) -> Optional[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM function_profiles WHERE work_id=? AND function_id=?",
            (work_id, function_id)).fetchone()
        return _row_to_profile(row) if row is not None else None

    return card_db.read_sync(work_id, _fn, None)


def function_exists(work_id: str, function_id: str) -> bool:
    return get_profile(work_id, function_id) is not None


def list_profiles(work_id: str) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM function_profiles WHERE work_id=? ORDER BY function_id",
            (work_id,)).fetchall()
        return [_row_to_profile(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


async def create_function(work_id: str, function_name: str,
                          profile: dict[str, Any] | None = None,
                          ) -> dict[str, Any]:
    """建一个职能：**发号（meta.fn_seq）与建行在同一写事务**（照 cards 的
    卡号发号先例——BEGIN IMMEDIATE 内取号 → INSERT → 写回，并发下不重号）。

    **不写 cards 表**——这是本步的核心契约：职能不进卡片世界，因此天然
    不出现在画布/卡片清单/卡树/任何按卡遍历。

    ``function_name``：调用方（模型）自填的职能名，本层只存不管内容、
    不设白名单。``profile`` 可携带 jurisdiction/watch/speak_contract/
    escalation/duty/charter（语义与 role_profiles 一致）。
    """
    profile = dict(profile or {})
    now = _now()

    def _fn(conn: sqlite3.Connection) -> str:
        seq = card_db.next_fn_seq(conn)
        function_id = f"fn_{seq:03d}"
        duty = profile.get("duty") or {"state": "active", "since": now,
                                       "last_wake_at": "", "pending_sync": 0}
        conn.execute(
            "INSERT INTO function_profiles"
            " (function_id, work_id, function_name, jurisdiction, watch,"
            "  speak_contract, escalation, duty, charter, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (function_id, work_id, str(function_name or ""),
             _dumps(profile.get("jurisdiction") or {}),
             _dumps(profile.get("watch") or {}),
             _dumps(profile.get("speak_contract") or {}),
             _dumps(profile.get("escalation") or {}),
             _dumps(duty),
             _dumps(profile.get("charter") or {}),
             now, now),
        )
        charter = profile.get("charter") or {}
        if str(charter.get("text") or "").strip():
            conn.execute(
                "INSERT INTO function_charter_history"
                " (work_id, function_id, version, text, ratified_by, ratified_at, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (work_id, function_id, int(charter.get("version") or 1),
                 str(charter.get("text") or ""),
                 str(charter.get("ratified_by") or ""),
                 str(charter.get("ratified_at") or now), "职能立项"),
            )
        card_db.set_meta_value(conn, "fn_seq", str(seq))
        card_db.bump_rev(conn)
        return function_id

    function_id = await card_db.run_write(work_id, _fn)
    return get_profile(work_id, function_id) or {}


async def ensure_profile(work_id: str, function_id: str,
                         profile: dict[str, Any]) -> dict[str, Any]:
    """不存在则插入（不动 fn 号段——只给既有 fn 行补档案用）；存在则原样返回。"""
    existing = get_profile(work_id, function_id)
    if existing is not None:
        return existing
    now = _now()
    function_id = str(function_id)

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO function_profiles"
            " (function_id, work_id, function_name, jurisdiction, watch,"
            "  speak_contract, escalation, duty, charter, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (function_id, work_id, str(profile.get("function_name") or ""),
             _dumps(profile.get("jurisdiction") or {}),
             _dumps(profile.get("watch") or {}),
             _dumps(profile.get("speak_contract") or {}),
             _dumps(profile.get("escalation") or {}),
             _dumps(profile.get("duty") or {"state": "active", "since": now,
                                            "last_wake_at": "",
                                            "pending_sync": 0}),
             _dumps(profile.get("charter") or {}),
             now, now),
        )
        charter = profile.get("charter") or {}
        if str(charter.get("text") or "").strip():
            conn.execute(
                "INSERT INTO function_charter_history"
                " (work_id, function_id, version, text, ratified_by, ratified_at, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (work_id, function_id, int(charter.get("version") or 1),
                 str(charter.get("text") or ""),
                 str(charter.get("ratified_by") or ""),
                 str(charter.get("ratified_at") or now), "组阁立项"),
            )

    await card_db.run_write(work_id, _fn)
    return get_profile(work_id, function_id) or profile


async def update_profile(work_id: str, function_id: str,
                         patch: dict[str, Any]) -> dict[str, Any]:
    """局部更新档案字段（function_name/jurisdiction/watch/speak_contract/
    escalation/duty/charter）。"""
    allowed = ("function_name", "jurisdiction", "watch", "speak_contract",
               "escalation", "duty", "charter")
    sets: list[str] = []
    vals: list[Any] = []
    for k in allowed:
        if k not in patch:
            continue
        if k == "function_name":
            sets.append("function_name=?")
            vals.append(str(patch[k] or ""))
        else:
            sets.append(f"{k}=?")
            vals.append(_dumps(patch[k]))
    if not sets:
        return get_profile(work_id, function_id) or {}
    vals.extend([_now(), work_id, function_id])

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"UPDATE function_profiles SET {', '.join(sets)}, updated_at=?"
            " WHERE work_id=? AND function_id=?", tuple(vals))

    await card_db.run_write(work_id, _fn)
    return get_profile(work_id, function_id) or {}


async def set_duty_state(work_id: str, function_id: str,
                         state: str, note: str = "") -> dict[str, Any]:
    """值班态变更。读-改-写在一个写事务内（照 role_store.set_duty_state 的
    并发教训——旧快照整字典写回会丢并发更新）。"""
    def _fn(conn: sqlite3.Connection) -> tuple[dict[str, Any], str]:
        row = conn.execute(
            "SELECT duty FROM function_profiles WHERE work_id=? AND function_id=?",
            (work_id, function_id)).fetchone()
        duty = dict(_loads(row["duty"], {})) if row is not None else {}
        old = str(duty.get("state") or "active")
        duty["state"] = state
        duty["since"] = _now()
        if note:
            duty["note"] = note
        duty.setdefault("last_wake_at", "")
        duty.setdefault("pending_sync", 0)
        conn.execute(
            "UPDATE function_profiles SET duty=?, updated_at=?"
            " WHERE work_id=? AND function_id=?",
            (_dumps(duty), _now(), work_id, function_id))
        return duty, old

    duty, old = await card_db.run_write(work_id, _fn)
    duty["previous"] = old
    return duty


async def touch_duty(work_id: str, function_id: str, *,
                     last_wake_at: str | None = None,
                     pending_sync: int | None = None) -> dict[str, Any]:
    """只合并 duty 的巡检记账字段（last_wake_at/pending_sync），绝不碰 state。"""
    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute(
            "SELECT duty FROM function_profiles WHERE work_id=? AND function_id=?",
            (work_id, function_id)).fetchone()
        duty = dict(_loads(row["duty"], {})) if row is not None else {}
        if last_wake_at is not None:
            duty["last_wake_at"] = last_wake_at
        if pending_sync is not None:
            duty["pending_sync"] = int(pending_sync)
        duty.setdefault("state", "active")
        conn.execute(
            "UPDATE function_profiles SET duty=?, updated_at=?"
            " WHERE work_id=? AND function_id=?",
            (_dumps(duty), _now(), work_id, function_id))
        return duty

    return await card_db.run_write(work_id, _fn)


async def append_charter(work_id: str, function_id: str, text: str,
                         ratified_by: str, note: str = "") -> dict[str, Any]:
    prof = get_profile(work_id, function_id) or {}
    charter = dict(prof.get("charter") or {})
    version = int(charter.get("version") or 0) + 1
    now = _now()
    charter.update({"version": version, "text": text,
                    "ratified_by": ratified_by, "ratified_at": now})

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO function_charter_history"
            " (work_id, function_id, version, text, ratified_by, ratified_at, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (work_id, function_id, version, text, ratified_by, now, note))

    await card_db.run_write(work_id, _fn)
    await update_profile(work_id, function_id, {"charter": charter})
    return charter


async def charter_history(work_id: str, function_id: str,
                          limit: int = 20) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT version, text, ratified_by, ratified_at, note"
            " FROM function_charter_history WHERE work_id=? AND function_id=?"
            " ORDER BY version DESC LIMIT ?",
            (work_id, function_id, limit)).fetchall()
        return [dict(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


# ============================================================
# 同步队列
# ============================================================

async def add_sync(work_id: str, function_id: str, source: str,
                   from_seq: int, to_seq: int, signals: list[str]) -> bool:
    """插入一条待巡检区间；同源已有更大的 pending to_seq 则跳过（幂等）。"""
    def _fn(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT MAX(to_seq) AS m FROM function_sync"
            " WHERE work_id=? AND function_id=? AND source=? AND status='pending'",
            (work_id, function_id, source)).fetchone()
        if row is not None and row["m"] is not None and int(row["m"]) >= int(to_seq):
            return False
        conn.execute(
            "INSERT INTO function_sync"
            " (work_id, function_id, source, from_seq, to_seq, signals,"
            "  status, created_at)"
            " VALUES (?,?,?,?,?,?, 'pending', ?)",
            (work_id, function_id, source, int(from_seq), int(to_seq),
             _dumps(list(signals or [])), _now()))
        return True

    return bool(await card_db.run_write(work_id, _fn))


def pending_syncs(work_id: str, function_id: str) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM function_sync WHERE work_id=? AND function_id=?"
            " AND status='pending' ORDER BY seq",
            (work_id, function_id)).fetchall()
        return [{
            "seq": int(r["seq"]),
            "source": r["source"] or "",
            "from_seq": int(r["from_seq"] or 0),
            "to_seq": int(r["to_seq"] or 0),
            "signals": _loads(r["signals"], []),
        } for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


def last_consumed_seq(work_id: str, function_id: str, source: str) -> int:
    """该来源已消费到的最大 to_seq；没有则 -1。"""
    def _fn(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT MAX(to_seq) AS m FROM function_sync"
            " WHERE work_id=? AND function_id=? AND source=? AND status='consumed'",
            (work_id, function_id, source)).fetchone()
        if row is None or row["m"] is None:
            return -1
        return int(row["m"])

    raw = card_db.read_sync(work_id, _fn, -1)
    return -1 if raw is None else int(raw)


async def consume_syncs(work_id: str, ids: list[int]) -> None:
    if not ids:
        return
    marks = ",".join("?" for _ in ids)

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"UPDATE function_sync SET status='consumed', consumed_at=?"
            f" WHERE seq IN ({marks})",
            tuple([_now()] + [int(i) for i in ids]))

    await card_db.run_write(work_id, _fn)


# ============================================================
# 判断簿
# ============================================================

def _verdict_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"],
        "decision": r["decision"],
        "reason": r["reason"] or "",
        "sources": _loads(r["sources"], []),
        "targets": _loads(r["targets"], []),
        "evidence": _loads(r["evidence"], []),
        "auto": bool(r["auto"]),
        "superseded_by": r["superseded_by"] or "",
    }


async def add_verdict(work_id: str, function_id: str, decision: str, reason: str,
                      sources: list[str], targets: list[str],
                      evidence: list[str], auto: bool = False) -> dict[str, Any]:
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "decision": decision,
        "reason": reason,
        "sources": list(sources or []),
        "targets": list(targets or []),
        "evidence": list(evidence or []),
        "auto": bool(auto),
        "superseded_by": "",
    }

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO function_verdicts"
            " (id, work_id, function_id, ts, decision, reason, sources, targets,"
            "  evidence, auto, superseded_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], work_id, function_id, rec["ts"], decision, reason,
             _dumps(rec["sources"]), _dumps(rec["targets"]),
             _dumps(rec["evidence"]), 1 if auto else 0, ""))

    await card_db.run_write(work_id, _fn)
    return rec


def list_verdicts(work_id: str, function_id: str,
                  limit: int = 30) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM function_verdicts WHERE work_id=? AND function_id=?"
            " ORDER BY ts DESC LIMIT ?",
            (work_id, function_id, limit)).fetchall()
        return [_verdict_row(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


async def supersede_verdict(work_id: str, old_id: str, new_id: str) -> None:
    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE function_verdicts SET superseded_by=?"
            " WHERE work_id=? AND id=?",
            (new_id, work_id, old_id))

    await card_db.run_write(work_id, _fn)


# ============================================================
# 警报簿
# ============================================================

def _alarm_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"],
        "function_id": r["function_id"],
        "severity": r["severity"] or "warn",
        "title": r["title"] or "",
        "detail": r["detail"] or "",
        "status": r["status"] or "open",
        "acked_at": r["acked_at"] or "",
        "corrected_by": r["corrected_by"] or "",
        "supersedes": r["supersedes"] or "",
        "question": r["question"] or "",
        "options": _loads(r["options"], []),
        "recommendation": r["recommendation"] or "",
        "default_action": r["default_action"] or "",
        "deadline": r["deadline"] or "",
    }


async def add_alarm(work_id: str, function_id: str, severity: str, title: str,
                    detail: str, supersedes: str = "",
                    question: str = "", options: list[str] | None = None,
                    recommendation: str = "", default_action: str = "",
                    deadline: str = "") -> dict[str, Any]:
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "function_id": function_id,
        "severity": severity,
        "title": title,
        "detail": detail,
        "status": "open",
        "acked_at": "",
        "corrected_by": "",
        "supersedes": supersedes,
        "question": question,
        "options": list(options or []),
        "recommendation": recommendation,
        "default_action": default_action,
        "deadline": deadline,
    }

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO function_alarms"
            " (id, work_id, function_id, ts, severity, title, detail, status,"
            "  acked_at, corrected_by, supersedes, question, options,"
            "  recommendation, default_action, deadline)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], work_id, function_id, rec["ts"], severity, title, detail,
             "open", "", "", supersedes, question, _dumps(rec["options"]),
             recommendation, default_action, deadline))
        if supersedes:
            conn.execute(
                "UPDATE function_alarms SET status='corrected', corrected_by=?"
                " WHERE work_id=? AND id=?",
                (rec["id"], work_id, supersedes))

    await card_db.run_write(work_id, _fn)
    return rec


def list_alarms(work_id: str, status: str = "open",
                function_id: str = "") -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        sql = "SELECT * FROM function_alarms WHERE work_id=?"
        args: list[Any] = [work_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        if function_id:
            sql += " AND function_id=?"
            args.append(function_id)
        sql += " ORDER BY ts DESC"
        return [_alarm_row(r) for r in conn.execute(sql, tuple(args)).fetchall()]

    return card_db.read_sync(work_id, _fn, []) or []


async def ack_alarm(work_id: str, alarm_id: str) -> bool:
    def _fn(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE function_alarms SET status='acked', acked_at=?"
            " WHERE work_id=? AND id=? AND status='open'",
            (_now(), work_id, alarm_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))


async def correct_alarm(work_id: str, alarm_id: str,
                        corrected_by: str) -> bool:
    def _fn(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE function_alarms SET status='corrected', corrected_by=?"
            " WHERE work_id=? AND id=? AND status IN ('open','acked')",
            (corrected_by, work_id, alarm_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))
