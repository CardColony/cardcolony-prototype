# -*- coding: utf-8 -*-
"""角色卡的结构化存储（5710 v2）。

角色卡不再复用普通卡的三块正文来承载职责/判词/警报，而是有独立的
``role_profiles`` ＋ 三本台账（判断簿／警报簿／同步队列）＋ 职责书版本史：

- ``role_profiles``：管辖域、关注与信号、发言契约、升级线、值班态、职责书
- ``role_verdicts``：每次巡检的判断（speak/silent、依据、来源、证据、被取代）
- ``role_alarms``：警报（severity/status/ack/更正链）
- ``role_sync``：待巡检同步队列（来源、seq 区间、命中信号、pending/consumed）
- ``role_charter_history``：职责书版本史

所有表加进 ``card_db._ensure_tables``（幂等 CREATE），既有 work 首次连接即补建。
本模块是纯存储，不 import card_store / api，避免环。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Optional

from . import card_db

logger = logging.getLogger("storage.role_store")


def _emit_state(work_id: str, role_id: str, block: str, *,
                old: Any = None, new: Any = None,
                by: str = "", cause: str = "system") -> None:
    """[基础设施 2026-10-26 · 事件管理] 角色卡档案变更事件。

    改前 ``role_store`` **零事件**——角色卡只有行为事件（role_alarm/role_judged），
    档案本身（管辖/订阅/发言契约/值班态/职责书）改了什么都不留痕。
    「所有类型的卡片都要记录」这条对 role 卡就是靠这里兑现的。
    """
    try:
        from ..infra import event_hub
        event_hub.record_state_change(
            work_id, role_id, "card_block_edited",
            card_type="role", block=block,
            old=(str(old) if old is not None else None),
            new=(str(new) if new is not None else None),
            by=by, cause=cause)
    except Exception as exc:  # noqa: BLE001 事件失败不影响写档案
        logger.warning("[role_store] 状态事件失败（不阻断）card=%s: %s", role_id, exc)


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
# 角色档案
# ============================================================

def _row_to_profile(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "role_id": row["card_id"],
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


def get_profile(work_id: str, role_id: str) -> Optional[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM role_profiles WHERE work_id=? AND card_id=?",
            (work_id, role_id)).fetchone()
        return _row_to_profile(row) if row is not None else None

    return card_db.read_sync(work_id, _fn, None)


async def ensure_profile(work_id: str, role_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    """不存在则插入；存在则原样返回（不覆盖）。返回最终档案。"""
    existing = get_profile(work_id, role_id)
    if existing is not None:
        return existing
    now = _now()
    role_id = str(role_id)

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO role_profiles"
            " (card_id, work_id, jurisdiction, watch, speak_contract,"
            "  escalation, duty, charter, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (role_id, work_id,
             _dumps(profile.get("jurisdiction") or {}),
             _dumps(profile.get("watch") or {}),
             _dumps(profile.get("speak_contract") or {}),
             _dumps(profile.get("escalation") or {}),
             _dumps(profile.get("duty") or {"state": "active",
                                            "since": now,
                                            "last_wake_at": "",
                                            "pending_sync": 0}),
             _dumps(profile.get("charter") or {}),
             now, now),
        )
        charter = profile.get("charter") or {}
        if str(charter.get("text") or "").strip():
            conn.execute(
                "INSERT INTO role_charter_history"
                " (work_id, role_id, version, text, ratified_by, ratified_at, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (work_id, role_id, int(charter.get("version") or 1),
                 str(charter.get("text") or ""),
                 str(charter.get("ratified_by") or ""),
                 str(charter.get("ratified_at") or now), "组阁立项"),
            )

    await card_db.run_write(work_id, _fn)
    return get_profile(work_id, role_id) or profile


async def update_profile(work_id: str, role_id: str,
                         patch: dict[str, Any]) -> dict[str, Any]:
    """局部更新档案字段（jurisdiction/watch/speak_contract/escalation/duty/charter）。"""
    allowed = ("jurisdiction", "watch", "speak_contract",
               "escalation", "duty", "charter")
    sets = [f"{k}=?" for k in allowed if k in patch]
    vals = [_dumps(patch[k]) for k in allowed if k in patch]
    if not sets:
        return get_profile(work_id, role_id) or {}
    vals.extend([_now(), work_id, role_id])

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"UPDATE role_profiles SET {', '.join(sets)}, updated_at=?"
            " WHERE work_id=? AND card_id=?", tuple(vals))

    await card_db.run_write(work_id, _fn)
    _emit_state(work_id, role_id, "profile",
                new={k: v for k, v in patch.items() if k in allowed},
                cause="system")
    return get_profile(work_id, role_id) or {}


async def set_duty_state(work_id: str, role_id: str,
                         state: str, note: str = "") -> dict[str, Any]:
    # [role-sync 2026-10-20 · card_007] 读-改-写必须在一个写事务内完成：
    # 旧写法先 get_profile 再整字典 update_profile，与并发中的 duty 写互踩
    #（dynamic_cabinet D 段实测：复职写入被上轮收尾的旧快照覆盖回 suspended）。
    def _fn(conn: sqlite3.Connection) -> tuple[dict[str, Any], str]:
        row = conn.execute(
            "SELECT duty FROM role_profiles WHERE work_id=? AND card_id=?", 
            (work_id, role_id)).fetchone()
        duty = dict(_loads(row["duty"], {})) if row is not None else {}
        old = str(duty.get("state") or "active")
        duty["state"] = state
        duty["since"] = _now()
        if note:
            duty["note"] = note
        duty.setdefault("last_wake_at", "")
        duty.setdefault("pending_sync", 0)
        conn.execute(
            "UPDATE role_profiles SET duty=?, updated_at=?"
            " WHERE work_id=? AND card_id=?", 
            (_dumps(duty), _now(), work_id, role_id))
        return duty, old

    duty, old = await card_db.run_write(work_id, _fn)
    duty["previous"] = old
    _emit_state(work_id, role_id, "duty", old=old, new=state, cause="system")
    return duty


async def touch_duty(work_id: str, role_id: str, *,
                     last_wake_at: str | None = None,
                     pending_sync: int | None = None) -> dict[str, Any]:
    """只合并 duty 的巡检记账字段（last_wake_at/pending_sync），绝不碰 state。

    为什么不整字典写回：_finalize 收尾与 set_duty 并发时（复职恰逢上轮巡检
    收尾的几百毫秒窗口），旧读快照会把刚写入的 state=active 冲回 suspended
    （丢更新）——spawn 的值班闸随即静默拒起，复职补巡检永远不来。
    """
    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute(
            "SELECT duty FROM role_profiles WHERE work_id=? AND card_id=?", 
            (work_id, role_id)).fetchone()
        duty = dict(_loads(row["duty"], {})) if row is not None else {}
        if last_wake_at is not None:
            duty["last_wake_at"] = last_wake_at
        if pending_sync is not None:
            duty["pending_sync"] = int(pending_sync)
        duty.setdefault("state", "active")
        conn.execute(
            "UPDATE role_profiles SET duty=?, updated_at=?"
            " WHERE work_id=? AND card_id=?", 
            (_dumps(duty), _now(), work_id, role_id))
        return duty

    return await card_db.run_write(work_id, _fn)


async def list_profiles(work_id: str) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM role_profiles WHERE work_id=? ORDER BY card_id",
            (work_id,)).fetchall()
        return [_row_to_profile(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


async def append_charter(work_id: str, role_id: str, text: str,
                         ratified_by: str, note: str = "") -> dict[str, Any]:
    prof = get_profile(work_id, role_id) or {}
    charter = dict(prof.get("charter") or {})
    version = int(charter.get("version") or 0) + 1
    now = _now()
    charter.update({"version": version, "text": text,
                    "ratified_by": ratified_by, "ratified_at": now})

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO role_charter_history"
            " (work_id, role_id, version, text, ratified_by, ratified_at, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (work_id, role_id, version, text, ratified_by, now, note))

    await card_db.run_write(work_id, _fn)
    await update_profile(work_id, role_id, {"charter": charter})
    return charter


async def charter_history(work_id: str, role_id: str,
                          limit: int = 20) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT version, text, ratified_by, ratified_at, note"
            " FROM role_charter_history WHERE work_id=? AND role_id=?"
            " ORDER BY version DESC LIMIT ?",
            (work_id, role_id, limit)).fetchall()
        return [dict(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


# ============================================================
# 同步队列
# ============================================================

async def add_sync(work_id: str, role_id: str, source: str,
                   from_seq: int, to_seq: int, signals: list[str]) -> bool:
    """插入一条待巡检区间；同源已有更大的 pending to_seq 则跳过（幂等）。"""
    def _fn(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT MAX(to_seq) AS m FROM role_sync"
            " WHERE work_id=? AND role_id=? AND source=? AND status='pending'",
            (work_id, role_id, source)).fetchone()
        if row is not None and row["m"] is not None and int(row["m"]) >= int(to_seq):
            return False
        conn.execute(
            "INSERT INTO role_sync"
            " (work_id, role_id, source, from_seq, to_seq, signals,"
            "  status, created_at)"
            " VALUES (?,?,?,?,?,?, 'pending', ?)",
            (work_id, role_id, source, int(from_seq), int(to_seq),
             _dumps(list(signals or [])), _now()))
        return True

    return bool(await card_db.run_write(work_id, _fn))


def pending_syncs(work_id: str, role_id: str) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM role_sync WHERE work_id=? AND role_id=?"
            " AND status='pending' ORDER BY seq",
            (work_id, role_id)).fetchall()
        return [{
            "seq": int(r["seq"]),
            "source": r["source"] or "",
            "from_seq": int(r["from_seq"] or 0),
            "to_seq": int(r["to_seq"] or 0),
            "signals": _loads(r["signals"], []),
        } for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


def last_consumed_seq(work_id: str, role_id: str, source: str) -> int:
    """该来源已消费到的最大 to_seq；没有则 -1。"""
    def _fn(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT MAX(to_seq) AS m FROM role_sync"
            " WHERE work_id=? AND role_id=? AND source=? AND status='consumed'",
            (work_id, role_id, source)).fetchone()
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
            f"UPDATE role_sync SET status='consumed', consumed_at=?"
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


async def add_verdict(work_id: str, role_id: str, decision: str, reason: str,
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
            "INSERT INTO role_verdicts"
            " (id, work_id, role_id, ts, decision, reason, sources, targets,"
            "  evidence, auto, superseded_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], work_id, role_id, rec["ts"], decision, reason,
             _dumps(rec["sources"]), _dumps(rec["targets"]),
             _dumps(rec["evidence"]), 1 if auto else 0, ""))

    await card_db.run_write(work_id, _fn)
    return rec


def list_verdicts(work_id: str, role_id: str,
                  limit: int = 30) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM role_verdicts WHERE work_id=? AND role_id=?"
            " ORDER BY ts DESC LIMIT ?",
            (work_id, role_id, limit)).fetchall()
        return [_verdict_row(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


async def supersede_verdict(work_id: str, old_id: str, new_id: str) -> None:
    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE role_verdicts SET superseded_by=?"
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
        "role_id": r["role_id"],
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


async def add_alarm(work_id: str, role_id: str, severity: str, title: str,
                    detail: str, supersedes: str = "",
                    question: str = "", options: list[str] | None = None,
                    recommendation: str = "", default_action: str = "",
                    deadline: str = "") -> dict[str, Any]:
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "role_id": role_id,
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
            "INSERT INTO role_alarms"
            " (id, work_id, role_id, ts, severity, title, detail, status,"
            "  acked_at, corrected_by, supersedes, question, options,"
            "  recommendation, default_action, deadline)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], work_id, role_id, rec["ts"], severity, title, detail,
             "open", "", "", supersedes, question, _dumps(rec["options"]),
             recommendation, default_action, deadline))
        if supersedes:
            conn.execute(
                "UPDATE role_alarms SET status='corrected', corrected_by=?"
                " WHERE work_id=? AND id=?",
                (rec["id"], work_id, supersedes))

    await card_db.run_write(work_id, _fn)
    return rec


def list_alarms(work_id: str, status: str = "open",
                role_id: str = "") -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        sql = "SELECT * FROM role_alarms WHERE work_id=?"
        args: list[Any] = [work_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        if role_id:
            sql += " AND role_id=?"
            args.append(role_id)
        sql += " ORDER BY ts DESC"
        return [_alarm_row(r) for r in conn.execute(sql, tuple(args)).fetchall()]

    return card_db.read_sync(work_id, _fn, []) or []


async def ack_alarm(work_id: str, alarm_id: str) -> bool:
    def _fn(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE role_alarms SET status='acked', acked_at=?"
            " WHERE work_id=? AND id=? AND status='open'",
            (_now(), work_id, alarm_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))


async def correct_alarm(work_id: str, alarm_id: str,
                        corrected_by: str) -> bool:
    def _fn(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE role_alarms SET status='corrected', corrected_by=?"
            " WHERE work_id=? AND id=? AND status IN ('open','acked')",
            (corrected_by, work_id, alarm_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))


# ============================================================
# 旧原型 JSONL 迁移（5710 v1 -> v2）
# ============================================================

async def import_verdict(work_id: str, role_id: str, rec: dict[str, Any]) -> None:
    """把旧 verdicts.jsonl 的一条原样导入（id/ts 保留；已存在则跳过）。"""
    vid = str(rec.get("id") or "") or uuid.uuid4().hex[:12]

    def _fn(conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM role_verdicts WHERE id=?", (vid,)).fetchone():
            return
        conn.execute(
            "INSERT INTO role_verdicts"
            " (id, work_id, role_id, ts, decision, reason, sources, targets,"
            "  evidence, auto, superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vid, work_id, role_id, str(rec.get("ts") or _now()),
             str(rec.get("decision") or "silent"), str(rec.get("reason") or ""),
             _dumps(list(rec.get("sources") or [])),
             _dumps(list(rec.get("targets") or [])),
             _dumps(list(rec.get("evidence") or [])),
             1 if rec.get("auto") else 0,
             str(rec.get("superseded_by") or "")))

    await card_db.run_write(work_id, _fn)


async def import_alarm(work_id: str, role_id: str, rec: dict[str, Any]) -> None:
    """把旧 alarms.jsonl 的一条原样导入（状态/更正链保留；已存在则跳过）。"""
    aid = str(rec.get("id") or "") or uuid.uuid4().hex[:12]

    def _fn(conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM role_alarms WHERE id=?", (aid,)).fetchone():
            return
        conn.execute(
            "INSERT INTO role_alarms"
            " (id, work_id, role_id, ts, severity, title, detail, status,"
            "  acked_at, corrected_by, supersedes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (aid, work_id, role_id, str(rec.get("ts") or _now()),
             str(rec.get("severity") or "warn"), str(rec.get("title") or ""),
             str(rec.get("detail") or ""), str(rec.get("status") or "open"),
             str(rec.get("acked_at") or ""), str(rec.get("corrected_by") or ""),
             str(rec.get("supersedes") or "")))

    await card_db.run_write(work_id, _fn)


# ============================================================
# 指令簿（角色 -> 关注卡，可验收）
# ============================================================

def _directive_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "role_id": r["role_id"],
        "target_card_id": r["target_card_id"],
        "instruction": r["instruction"] or "",
        "acceptance": r["acceptance"] or "",
        "msg_id": r["msg_id"] or "",
        "status": r["status"] or "issued",
        "evidence": r["evidence"] or "",
        "created_at": r["created_at"] or "",
        "updated_at": r["updated_at"] or "",
        "verified_at": r["verified_at"] or "",
        "due_at": r["due_at"] or "",
        "parent_id": r["parent_id"] or "",
        "attempt": int(r["attempt"] or 1),
    }


async def add_directive(work_id: str, role_id: str, target_card_id: str,
                        instruction: str, acceptance: str,
                        msg_id: str = "", due_at: str = "",
                        parent_id: str = "", attempt: int = 1) -> dict[str, Any]:
    import uuid
    rec = {
        "id": uuid.uuid4().hex[:12],
        "role_id": role_id,
        "target_card_id": target_card_id,
        "instruction": instruction,
        "acceptance": acceptance,
        "msg_id": msg_id,
        "status": "issued",
        "evidence": "",
        "created_at": _now(),
        "updated_at": _now(),
        "verified_at": "",
        "due_at": due_at,
        "parent_id": parent_id,
        "attempt": max(1, int(attempt or 1)),
    }

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO role_directives"
            " (id, work_id, role_id, target_card_id, instruction, acceptance,"
            "  msg_id, status, evidence, created_at, updated_at, verified_at,"
            "  due_at, parent_id, attempt)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], work_id, role_id, target_card_id, instruction, acceptance,
             msg_id, "issued", "", rec["created_at"], rec["updated_at"], "",
             due_at, parent_id, rec["attempt"]))

    await card_db.run_write(work_id, _fn)
    return rec


def list_directives(work_id: str, role_id: str,
                    status: str = "") -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        sql = "SELECT * FROM role_directives WHERE work_id=? AND role_id=?"
        args: list[Any] = [work_id, role_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC"
        return [_directive_row(r) for r in conn.execute(sql, tuple(args)).fetchall()]

    return card_db.read_sync(work_id, _fn, []) or []


def get_directive(work_id: str, directive_id: str) -> Optional[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM role_directives WHERE work_id=? AND id=?",
            (work_id, directive_id)).fetchone()
        return _directive_row(row) if row is not None else None

    return card_db.read_sync(work_id, _fn, None)


async def verify_directive(work_id: str, directive_id: str, status: str,
                           evidence: str = "") -> bool:
    if status not in ("complied", "rejected", "cancelled"):
        return False

    def _fn(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE role_directives SET status=?, evidence=?, verified_at=?,"
            " updated_at=? WHERE work_id=? AND id=?",
            (status, evidence, _now(), _now(), work_id, directive_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))


def list_open_directives(work_id: str, target_card_id: str) -> list[dict[str, Any]]:
    """阻塞目标卡收口的指令：issued/rejected/overdue。"""
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM role_directives WHERE work_id=? AND target_card_id=?"
            " AND status IN ('issued','rejected','overdue')"
            " ORDER BY created_at", (work_id, target_card_id)).fetchall()
        return [_directive_row(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


async def refresh_overdue(work_id: str, role_id: str = "") -> int:
    """把已过 due_at 的 issued 指令置 overdue（幂等；到点后阻塞收口）。"""
    now = _now()

    def _fn(conn: sqlite3.Connection) -> int:
        sql = ("UPDATE role_directives SET status='overdue', updated_at=?"
               " WHERE work_id=? AND status='issued' AND due_at<>'' AND due_at<?")
        args: list[Any] = [now, work_id, now]
        if role_id:
            sql += " AND role_id=?"
            args.append(role_id)
        cur = conn.execute(sql, tuple(args))
        return int(cur.rowcount or 0)

    return int(await card_db.run_write(work_id, _fn) or 0)


async def mark_directive_rework(work_id: str, old_id: str, new_id: str) -> bool:
    """旧指令已由返工指令接续：置 rework（不再阻塞），保留原证据。"""
    def _fn(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE role_directives SET status='rework',"
            " evidence = CASE WHEN evidence='' THEN ? ELSE evidence || '；' || ? END,"
            " updated_at=? WHERE work_id=? AND id=? AND status='rejected'",
            (f"→ 返工指令 {new_id}", f"→ 返工指令 {new_id}", _now(),
             work_id, old_id))
        return cur.rowcount > 0

    return bool(await card_db.run_write(work_id, _fn))
