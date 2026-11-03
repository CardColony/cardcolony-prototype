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
import logging
import sqlite3
import uuid
from typing import Any, Optional

from . import card_db

logger = logging.getLogger("storage.function_store")


def _emit_state(work_id: str, function_id: str, block: str, *,
                old: Any = None, new: Any = None,
                by: str = "", cause: str = "system") -> None:
    """[基础设施 2026-10-26 · 事件管理] 职能卡档案变更事件。

    改前 ``function_store`` **零事件**——职能卡只有行为事件
    （function_opinion/function_ledger），档案本身（管辖/订阅/发言契约/值班态/
    职责书）改了什么都不留痕。
    """
    try:
        from ..infra import event_hub
        event_hub.record_state_change(
            work_id, function_id, "card_block_edited",
            card_type="function", block=block,
            old=(str(old)[:2000] if old is not None else None),
            new=(str(new)[:2000] if new is not None else None),
            by=by, cause=cause)
    except Exception as exc:  # noqa: BLE001 事件失败不影响写档案
        logger.warning("[function_store] 状态事件失败（不阻断）card=%s: %s",
                       function_id, exc)


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
        # [职能实体化 A 案 2026-10-22] 职能在 cards 表里那一行的句柄：基础设施
        # （perms / build_ctx / _run_message / 建卡）全部按 card_id 走。
        # 老库可能还没补这列 ⇒ 取不到时给空串（迁移脚本会补）。
        "card_id": (row["card_id"] if "card_id" in row.keys() else "") or "",
        # [能力清单 2026-10-30] 这张卡挂了哪些能力（code 数组）。空＝没挂＝
        # 工具面按既有路径走。老库未补列时同样给空数组（fail-open 到旧行为）。
        "abilities": (_loads(row["abilities"], [])
                      if "abilities" in row.keys() else []),
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


async def get_abilities(work_id: str, function_id: str) -> list[str]:
    """这张职能卡挂了哪些能力（code 数组）。读不到 ⇒ 空数组（＝没挂）。"""
    prof = get_profile(work_id, function_id) or {}
    raw = prof.get("abilities")
    if not isinstance(raw, list):
        return []
    return [str(a) for a in raw if str(a).strip()]


async def set_abilities(work_id: str, function_id: str,
                        codes: list[str], *, kind: str = "function",
                        by: str = "", cause: str = "system") -> dict[str, Any]:
    """整组覆盖挂载。返回 ``{"abilities": [...], "rejected": [...]}``。

    校验规则（清单不可用时整体跳过——**fail-open 到"信调用方"**，
    否则清单文件坏一次就会把所有挂载清空）：
      · code 必须在清单里，且 ``applies_to`` 含 ``kind``；
      · 重复项去掉，保序。
    不合法的 code 不写库、放进 ``rejected``，由调用方决定怎么报。
    """
    want = [str(c).strip() for c in (codes or []) if str(c).strip()]
    rejected: list[str] = []
    keep: list[str] = []
    try:
        from ..infra import capability_registry as _cap
        if _cap.load().get("enabled"):
            keep = _cap.filter_mounts(want, kind)
            keep_set = set(keep)
            rejected = [c for c in want if c not in keep_set]
        else:
            keep = list(dict.fromkeys(want))
    except Exception as exc:  # noqa: BLE001 清单坏了 ⇒ 不校验
        logger.warning("[function] 挂载校验跳过（清单不可用）: %s", exc)
        keep = list(dict.fromkeys(want))

    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE function_profiles SET abilities=?, updated_at=?"
            " WHERE work_id=? AND function_id=?",
            (_dumps(keep), _now(), work_id, function_id))

    await card_db.run_write(work_id, _fn)
    _emit_state(work_id, function_id, "abilities", new=keep, by=by, cause=cause)
    return {"abilities": keep, "rejected": rejected}


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
            "  speak_contract, escalation, duty, charter, abilities,"
            "  created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (function_id, work_id, str(function_name or ""),
             _dumps(profile.get("jurisdiction") or {}),
             _dumps(profile.get("watch") or {}),
             _dumps(profile.get("speak_contract") or {}),
             _dumps(profile.get("escalation") or {}),
             _dumps(duty),
             _dumps(profile.get("charter") or {}),
             _dumps([str(a) for a in (profile.get("abilities") or []) if str(a).strip()]),
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
    _emit_state(work_id, function_id, "profile",
                new={k: v for k, v in patch.items() if k in allowed},
                cause="system")
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
    _emit_state(work_id, function_id, "duty", old=old, new=state, cause="system")
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


# ============================================================
# [指令台账 2026-10-22] function_directives —— 职能下指令/验收的存储
# ============================================================
# 为什么单独补：`issue_directive` / `verify_directive` 原来只做了工具层，
# 职能（fn_）一调就返回 not_implemented（"function_directives 表尚不存在"）。
# 语义与 role_directives 逐字同构（纯改名 role_id → function_id），不改任何规则：
#   issued →（对方执行）→ complied / rejected（必带 evidence）/ cancelled。
# 表用 CREATE IF NOT EXISTS 幂等建（card_db 是禁改文件，DDL 放本层）。

_DIRECTIVE_DDL = """CREATE TABLE IF NOT EXISTS function_directives (
    id              TEXT PRIMARY KEY,
    work_id         TEXT NOT NULL,
    function_id     TEXT NOT NULL,
    target_card_id  TEXT NOT NULL,
    instruction     TEXT,
    acceptance      TEXT,
    msg_id          TEXT DEFAULT '',
    status          TEXT DEFAULT 'issued',
    evidence        TEXT DEFAULT '',
    created_at      TEXT,
    updated_at      TEXT,
    verified_at     TEXT DEFAULT '',
    due_at          TEXT DEFAULT '',
    parent_id       TEXT DEFAULT '',
    attempt         INTEGER DEFAULT 1
)"""

_DIRECTIVE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_function_directives_fn "
    "ON function_directives(function_id, status)")

_DIRECTIVE_STATUSES = ("issued", "complied", "rejected", "cancelled")


def _ensure_directives(conn: sqlite3.Connection) -> None:
    conn.execute(_DIRECTIVE_DDL)
    conn.execute(_DIRECTIVE_INDEX)


def _directive_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "function_id": r["function_id"],
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


async def add_directive(work_id: str, function_id: str, target_card_id: str,
                        instruction: str, acceptance: str,
                        msg_id: str = "", due_at: str = "",
                        parent_id: str = "", attempt: int = 1) -> dict[str, Any]:
    """职能下一条指令（status=issued）。"""
    rec = {
        "id": uuid.uuid4().hex[:12],
        "function_id": function_id,
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
        _ensure_directives(conn)
        conn.execute(
            "INSERT INTO function_directives"
            " (id, work_id, function_id, target_card_id, instruction, acceptance,"
            "  msg_id, status, evidence, created_at, updated_at, verified_at,"
            "  due_at, parent_id, attempt)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], work_id, function_id, target_card_id, instruction,
             acceptance, msg_id, "issued", "", rec["created_at"],
             rec["updated_at"], "", due_at, parent_id, rec["attempt"]))

    await card_db.run_write(work_id, _fn)
    return rec


def list_directives(work_id: str, function_id: str,
                    status: str = "") -> list[dict[str, Any]]:
    """列该职能的指令台账（status 空＝全部，按创建时间倒序）。"""

    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        _ensure_directives(conn)
        sql = "SELECT * FROM function_directives WHERE work_id=? AND function_id=?"
        args: list[Any] = [work_id, function_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC"
        return [_directive_row(r) for r in conn.execute(sql, tuple(args)).fetchall()]

    return card_db.read_sync(work_id, _fn, []) or []


def get_directive(work_id: str, directive_id: str) -> Optional[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        _ensure_directives(conn)
        row = conn.execute(
            "SELECT * FROM function_directives WHERE work_id=? AND id=?",
            (work_id, directive_id)).fetchone()
        return _directive_row(row) if row is not None else None

    return card_db.read_sync(work_id, _fn, None)


async def verify_directive(work_id: str, function_id: str, directive_id: str,
                           status: str, evidence: str = "") -> bool:
    """验收一条指令：complied / rejected / cancelled。只认自己下的指令。"""
    if status not in _DIRECTIVE_STATUSES or status == "issued":
        return False
    done = {"ok": False}

    def _fn(conn: sqlite3.Connection) -> None:
        _ensure_directives(conn)
        cur = conn.execute(
            "UPDATE function_directives"
            " SET status=?, evidence=?, updated_at=?, verified_at=?"
            " WHERE work_id=? AND id=? AND function_id=? AND status='issued'",
            (status, evidence, _now(), _now(), work_id, directive_id, function_id))
        done["ok"] = cur.rowcount > 0

    await card_db.run_write(work_id, _fn)
    return bool(done["ok"])


# ============================================================
# [职能卡面定制 2026-10-24 · P1 收尾] 专业台账存储(function_ledgers)
#
# 每个职能的面不只是「观点簿/警报簿」——目录给每个职能定义了**专业台账**
# (PM=前提台账/CTO=技术裁定簿/QA=判据与验收台账/涌现守门=判据登记与体检/
#  总裁=决策簿·汇报簿·分歧待裁)。这些是「卡面=全局权威事实源」在职能侧的
# 物理落点:职能写自己的台账,别人经面读到。
# DDL 落本层(function_directives 同法)——card_db 是禁改文件。
# ============================================================

# [两源模型 2026-11-01 · 用户令「卡面是唯一权威事实源，不需要台账」]
# function_ledgers 读写全退役：台账是「没人裁过的主张」，正是本系统要
# 消灭的第二事实源——准则归 rules.md（人改）、产出归场卡面、分歧归
# 票+sync。存量库的表留着（历史数据不删），代码不再碰它。


# ============================================================
# [差事对象 2026-10-31] function_deployments —— 「哪里需要去哪里」的账本
#
# 一次差事＝一个职能被派进一个场（评审席/派驻/执笔）：
# 谁派的（delegator）、干什么（mandate）、授了什么工具（tools）、
# 怎么算完（exit 判据）、结果（result）。
#
# 「何时派」由调用方/harness 决定——本层只负责：记账、幂等、
# 惰性核退出、显式关账。**没有核对器的 exit_kind 只能显式关**；
# 核对器只做读判定不改写 DB（effective_status 是读出来的）。
# DDL 落本层（function_ledgers 同法）——card_db 是禁改文件。
# ============================================================

_DEPLOY_DDL = """CREATE TABLE IF NOT EXISTS function_deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    function_id TEXT NOT NULL,
    venue_kind TEXT NOT NULL,
    venue_ref TEXT NOT NULL,
    venue_label TEXT NOT NULL DEFAULT '',
    mandate TEXT NOT NULL DEFAULT '',
    delegator TEXT NOT NULL DEFAULT '',
    batch_id TEXT NOT NULL DEFAULT '',
    target_card TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',
    exit_kind TEXT NOT NULL DEFAULT 'explicit',
    exit_spec TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    result TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT ''
)"""

_DEPLOY_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_fn_deploy_fn "
    "ON function_deployments(work_id, function_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_fn_deploy_venue "
    "ON function_deployments(work_id, venue_kind, venue_ref)")

# [两源模型 2026-11-01 · 用户令「会诊/治理都删了，需要时再设计再加」]
# consult 场退役：私问职能产的是没人裁过的主张，与「卡面是唯一权威事实源」
# 冲突；governance 场退役：零生产者的哑件，授面最宽（curate/instruct/
# report_to_human）却没有判据管收口，留着只会被当「已支持」拖进重构。
# 要叫职能只走评审/派驻；将来真出管理类差事场景，按当时的语义重新设计。
VENUE_KINDS = ("review", "deploy", "author")
DEPLOY_STATUSES = ("created", "active", "done", "withdrawn", "escalated")
DEPLOY_ACTIVE = ("created", "active")
EXIT_KINDS = ("verdict", "answer", "deliverable", "signoff", "explicit")


def _ensure_deployments(conn: sqlite3.Connection) -> None:
    # 注意：不能用 executescript——它隐式提交事务，会把 run_write 的
    # BEGIN IMMEDIATE 提前 COMMIT（「no transaction is active」坑）。
    conn.execute(_DEPLOY_DDL)
    for idx in _DEPLOY_IDX:
        conn.execute(idx)


def _dep_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "work_id": row["work_id"],
        "function_id": row["function_id"] or "",
        "venue_kind": row["venue_kind"] or "",
        "venue_ref": row["venue_ref"] or "",
        "venue_label": row["venue_label"] or "",
        "mandate": row["mandate"] or "",
        "delegator": row["delegator"] or "",
        "batch_id": row["batch_id"] or "",
        "target_card": row["target_card"] or "",
        "tools": _loads(row["tools"], []),
        "exit_kind": row["exit_kind"] or "explicit",
        "exit_spec": row["exit_spec"] or "",
        "status": row["status"] or "",
        "result": row["result"] or "",
        "meta": _loads(row["meta"], {}),
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "closed_at": row["closed_at"] or "",
    }


def _contains_from(obj: Any, want: str, _depth: int = 0) -> bool:
    """事件记录里递归找 from==want（记录形状不钉死，宽容扫）。"""
    if _depth > 4 or not want:
        return False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) == "from" and str(v) == want:
                return True
            if isinstance(v, (dict, list)) and _contains_from(v, want, _depth + 1):
                return True
    elif isinstance(obj, list):
        return any(_contains_from(x, want, _depth + 1) for x in obj)
    return False


def _verdict_exit(work_id: str, dep: dict[str, Any]) -> bool:
    """评审差事的退出：该席位在本批次有一张**立着的票**（revoked=0）。

    approve/reject 都算——票立起来＝差事办完；revoke 把票撤回 ⇒
    差事在惰性核对里自动回到在派（判据是读出来的，不改写）。
    """
    bid = str(dep.get("batch_id") or "")
    if not bid:
        return False

    def _fn(conn: sqlite3.Connection) -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM review_approvals"
                " WHERE work_id=? AND batch_id=? AND role_id=?"
                " AND revoked=0 LIMIT 1",
                (work_id, bid, str(dep.get("function_id") or ""))
            ).fetchone() is not None
        except sqlite3.OperationalError:  # 老库还没有 review_approvals
            return False

    try:
        return bool(card_db.read_sync(work_id, _fn, False))
    except Exception:  # noqa: BLE001 核对失败一律当没办完
        return False


def _answer_exit(work_id: str, dep: dict[str, Any]) -> bool:
    """answer 判据的退出：职能把答案**送回了派遣方**——派遣方（venue_ref）
    的卡事件流里出现一条 from=该职能的消息事件（msg_sync 等）。
    """
    venue = str(dep.get("venue_ref") or "")
    fn = str(dep.get("function_id") or "")
    if not venue or not fn:
        return False
    try:
        from . import event_log
        recs = event_log.read(work_id, venue) or []
    except Exception:  # noqa: BLE001 事件流读不到 ⇒ 当没办完
        return False
    created = str(dep.get("created_at") or "")
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("type") or "") not in (
                "msg_sync", "msg_answer", "msg_directive", "msg_report"):
            continue
        ts = str(rec.get("ts") or "")
        if created and ts and ts < created:
            continue
        if _contains_from(rec, fn):
            return True
    return False


def _deliverable_exit(work_id: str, dep: dict[str, Any]) -> bool:
    """派驻/执笔差事的退出：职能**把产出落到了卡的面上**——差事创建后，
    承载事实的那张卡的事件流里出现一条它名下的产出事实。

    [两源模型 2026-11-01 · 用户令「卡面是唯一权威事实源，不需要台账」]
    [差事卡 2026-11-01 · 落点修正] 场卡（``venue_ref``）是职能干活的
    **场**（验收卡/执笔卡），事实落在别处：

    - ``msg`` / ``report`` ：结论发给**派遣方**（``delegator``）——它的事件
                           流里出现 from=该职能的消息（``report`` 只看
                           msg_report；``msg`` 四种消息类型都算）。派遣方不是
                           卡时回落 target_card → venue（旧场即对象模型）。
    - ``cardwrite[:<块>]`` ：草稿写在**对象卡**（``target_card``）面上——
                           它的事件流里出现 ``card_block_edited`` 且
                           ``by=该职能``。无对象卡时回落 venue。
    """
    spec = str(dep.get("exit_spec") or "")
    venue = str(dep.get("venue_ref") or "")
    fn = str(dep.get("function_id") or "")
    created = str(dep.get("created_at") or "")
    if not fn:
        return False

    def _stream(card_id: str) -> list[dict[str, Any]]:
        if not card_id:
            return []
        try:
            from . import event_log
            return event_log.read(work_id, card_id) or []
        except Exception:  # noqa: BLE001 事件流读不到 ⇒ 当没办完
            return []

    if spec in ("msg", "report"):
        want_types = (("msg_report",) if spec == "report"
                      else ("msg_sync", "msg_answer", "msg_directive",
                            "msg_report"))
        # 回执落派遣方流；派遣方不是卡（harness:xxx）时回落对象卡→场卡。
        streams = [str(dep.get("delegator") or ""),
                   str(dep.get("target_card") or ""), venue]
        seen: set[str] = set()
        for cid in streams:
            if not cid or cid in seen or ":" in cid:
                continue
            seen.add(cid)
            for rec in _stream(cid):
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("type") or "") not in want_types:
                    continue
                if created and str(rec.get("ts") or "") < created:
                    continue
                if _contains_from(rec, fn):
                    return True
        return False
    block = (spec.split(":", 1)[1].strip()
             if spec.startswith("cardwrite:") else "")
    recs = _stream(str(dep.get("target_card") or "") or venue)
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("type") or "") != "card_block_edited":
            continue
        if created and str(rec.get("ts") or "") < created:
            continue
        # by/block 在 data 载荷里（record_state_change 的落盘形状），不在顶层。
        payload = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        if str(payload.get("by") or "") != fn:
            continue
        if block and str(payload.get("block") or "") != block:
            continue
        return True
    return False


# 退出判据 → 惰性核对器。读层凭它给出「这张差事实际办完没有」，
# 不写库；持久关账仍走 close_deployment（显式）或事件通道。
# **没有核对器的判据（signoff/explicit）只能靠显式关**——signoff 是
# 派遣方/用户的事后确认，机器没有可判的事实。
exit_checkers: dict[str, Any] = {
    "verdict": _verdict_exit,
    "answer": _answer_exit,
    "deliverable": _deliverable_exit,
}


def effective_status(work_id: str, dep: dict[str, Any]) -> str:
    """有效状态＝库状态 ⊕ 惰性核对。终态行直接返回库状态。"""
    st = str(dep.get("status") or "")
    if st not in DEPLOY_ACTIVE:
        return st
    chk = exit_checkers.get(str(dep.get("exit_kind") or ""))
    if chk is None:
        return st
    try:
        return "done" if chk(work_id, dep) else st
    except Exception:  # noqa: BLE001 核对器坏 ⇒ 不信它
        return st


def _with_effective(work_id: str, dep: dict[str, Any]) -> dict[str, Any]:
    d = dict(dep)
    d["effective_status"] = effective_status(work_id, dep)
    return d


def _emit_deploy(work_id: str, dep: dict[str, Any], etype: str) -> None:
    """差事生命周期事件（发在职能自己的卡键上）。"""
    try:
        from ..api import event_stream
        event_stream.emit(work_id, str(dep.get("function_id") or ""), etype, {
            "deployment": int(dep.get("id") or 0),
            "function_id": str(dep.get("function_id") or ""),
            "venue_kind": str(dep.get("venue_kind") or ""),
            "venue_ref": str(dep.get("venue_ref") or ""),
            "delegator": str(dep.get("delegator") or ""),
            "batch_id": str(dep.get("batch_id") or ""),
            "target_card": str(dep.get("target_card") or ""),
            "status": str(dep.get("status") or ""),
            "result": str(dep.get("result") or ""),
        })
    except Exception as exc:  # noqa: BLE001 事件失败不影响建账
        logger.warning("[function_store] 差事事件失败（不阻断）dep=%s: %s",
                       dep.get("id"), exc)


async def create_deployment(work_id: str, function_id: str,
                            *, venue_kind: str, venue_ref: str,
                            mandate: str = "", delegator: str = "",
                            batch_id: str = "", target_card: str = "",
                            tools: Optional[list[str]] = None,
                            exit_kind: str = "explicit",
                            exit_spec: str = "", venue_label: str = "",
                            status: str = "created",
                            meta: Optional[dict[str, Any]] = None
                            ) -> dict[str, Any]:
    """开一张差事。**幂等**：同 (function, venue_kind, venue_ref, batch_id)
    同 mandate 的在派差事已存在 ⇒ 返回既有行（补种/重放不双开）。"""
    venue_kind = str(venue_kind or "").strip()
    if venue_kind not in VENUE_KINDS:
        raise ValueError(f"venue_kind 非法: {venue_kind!r}（{VENUE_KINDS}）")
    status = str(status or "created").strip()
    if status not in DEPLOY_STATUSES:
        raise ValueError(f"status 非法: {status!r}（{DEPLOY_STATUSES}）")
    exit_kind = str(exit_kind or "explicit").strip()
    if exit_kind not in EXIT_KINDS:
        raise ValueError(f"exit_kind 非法: {exit_kind!r}（{EXIT_KINDS}）")
    function_id = str(function_id or "").strip()
    venue_ref = str(venue_ref or "").strip()
    if not function_id or not venue_ref:
        raise ValueError("function_id / venue_ref 不能为空")
    mandate = str(mandate or "").strip()
    tools = [str(t) for t in (tools or [])]
    now = _now()

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        _ensure_deployments(conn)
        row = conn.execute(
            "SELECT * FROM function_deployments"
            " WHERE work_id=? AND function_id=? AND venue_kind=?"
            " AND venue_ref=? AND batch_id=? AND mandate=?"
            " AND status IN ('created','active') LIMIT 1",
            (work_id, function_id, venue_kind, venue_ref,
             str(batch_id or ""), mandate)).fetchone()
        if row is not None:
            return _dep_row(row)
        cur = conn.execute(
            "INSERT INTO function_deployments"
            " (work_id,function_id,venue_kind,venue_ref,venue_label,"
            "  mandate,delegator,batch_id,target_card,tools,"
            "  exit_kind,exit_spec,status,meta,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_id, function_id, venue_kind, venue_ref,
             str(venue_label or ""), mandate, str(delegator or ""),
             str(batch_id or ""), str(target_card or ""), _dumps(tools),
             exit_kind, str(exit_spec or ""), status,
             _dumps(meta or {}), now, now))
        return _dep_row(conn.execute(
            "SELECT * FROM function_deployments WHERE id=?",
            (cur.lastrowid,)).fetchone())

    dep = await card_db.run_write(work_id, _fn)
    _emit_deploy(work_id, dep, "deployment_created")
    try:
        # 顺手一扫：票/台账先于差事就位（重演、补建）的态直接判完，
        # 不留"开出来就已过期"的假在派。
        await sweep_deployments(work_id)
    except Exception as exc:  # noqa: BLE001 扫不动不拦建账
        logger.warning("[function_store] 建账后扫场失败: %s", exc)
    return dep


def get_deployment(work_id: str, deployment_id: int) -> Optional[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        try:
            row = conn.execute(
                "SELECT * FROM function_deployments"
                " WHERE work_id=? AND id=?",
                (work_id, int(deployment_id))).fetchone()
        except sqlite3.OperationalError:
            # 只读连接上不能做 DDL——表还没建（尚无差事）就当不存在，
            # 与 _query_deployments/has_prior 同一口径。
            return None
        return _dep_row(row) if row is not None else None

    return card_db.read_sync(work_id, _fn, None)


async def _set_status(work_id: str, deployment_id: int, status: str,
                      *, result: str = "") -> Optional[dict[str, Any]]:
    if status not in DEPLOY_STATUSES:
        raise ValueError(f"status 非法: {status!r}（{DEPLOY_STATUSES}）")
    now = _now()
    terminal = status in ("done", "withdrawn", "escalated")

    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        _ensure_deployments(conn)
        conn.execute(
            "UPDATE function_deployments SET status=?, result=?,"
            " updated_at=?, closed_at=?"
            " WHERE work_id=? AND id=?",
            (status, str(result or "") if terminal else "", now,
             now if terminal else "",
             work_id, int(deployment_id)))
        row = conn.execute(
            "SELECT * FROM function_deployments"
            " WHERE work_id=? AND id=?",
            (work_id, int(deployment_id))).fetchone()
        return _dep_row(row) if row is not None else None

    return await card_db.run_write(work_id, _fn)


async def activate_deployment(work_id: str, deployment_id: int) -> dict[str, Any]:
    return await _set_status(work_id, deployment_id, "active") or {}


async def close_deployment(work_id: str, deployment_id: int, *,
                           status: str = "done",
                           result: str = "") -> dict[str, Any]:
    """显式关账（派遣方撤回/升级/确认完成）。终态才带 closed_at。"""
    if status not in ("done", "withdrawn", "escalated"):
        raise ValueError("close 只接受 done/withdrawn/escalated")
    dep = await _set_status(work_id, deployment_id, status, result=result) or {}
    if dep:
        _emit_deploy(work_id, dep, "deployment_closed")
    return dep


async def sweep_deployments(work_id: str) -> int:
    """扫在派差事：惰性核对判完的**持久关账**——status=done + closed_at +
    `deployment_closed` 事件（策略可监听）。返回关掉的条数。

    调用点（本线的写路径，幂等随便跑）：
      · `create_deployment` 落账后顺手一扫（票/台账先于差事就位的态）；
      · `card_tools.execute_tool` 工具落完后（deliverable 的场卡产出
        事实可能就此翻完——单点覆盖所有写通道）；
      · harness 若要主动收口，可在策略触发后调它——判据全在库内。
    """
    rows = _query_deployments(
        work_id, "AND status IN ('created','active')", (), limit=500)
    closed = 0
    for r in rows:
        if effective_status(work_id, r) != "done":
            continue
        dep = await _set_status(work_id, int(r["id"]), "done",
                                result=r.get("result") or "")
        if dep:
            _emit_deploy(work_id, dep, "deployment_closed")
            closed += 1
    return closed


def _query_deployments(work_id: str, where: str, args: tuple,
                       *, limit: int = 200) -> list[dict[str, Any]]:
    def _fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            rows = conn.execute(
                "SELECT * FROM function_deployments WHERE work_id=? "
                + where + " ORDER BY id DESC LIMIT ?",
                (work_id,) + args + (int(limit),)).fetchall()
        except sqlite3.OperationalError:  # 老库还没这张表
            return []
        return [_dep_row(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


def active_for_function(work_id: str, function_id: str,
                        venue_kind: str = "") -> list[dict[str, Any]]:
    """该职能**当前有效在派**的差事（惰性核对后仍 active 才算）。"""
    where = "AND function_id=? AND status IN ('created','active')"
    args: tuple = (str(function_id),)
    if venue_kind:
        where += " AND venue_kind=?"
        args += (str(venue_kind),)
    rows = _query_deployments(work_id, where, args)
    return [d for d in (_with_effective(work_id, r) for r in rows)
            if d["effective_status"] in DEPLOY_ACTIVE]


def active_for_venue(work_id: str, venue_ref: str,
                     venue_kind: str = "") -> list[dict[str, Any]]:
    """这个场里现在挂着哪些职能的差事。"""
    where = "AND venue_ref=? AND status IN ('created','active')"
    args: tuple = (str(venue_ref),)
    if venue_kind:
        where += " AND venue_kind=?"
        args += (str(venue_kind),)
    rows = _query_deployments(work_id, where, args)
    return [d for d in (_with_effective(work_id, r) for r in rows)
            if d["effective_status"] in DEPLOY_ACTIVE]


def deployments_for_function(work_id: str, function_id: str,
                             limit: int = 200) -> list[dict[str, Any]]:
    """该职能的全部差事（含终态），带 effective_status。"""
    rows = _query_deployments(
        work_id, "AND function_id=?", (str(function_id),), limit=limit)
    return [_with_effective(work_id, r) for r in rows]


def deployment_history(work_id: str, function_id: str = "",
                       limit: int = 50) -> list[dict[str, Any]]:
    """参与记录＝**有效状态已终**的差事（库终态或惰性核对判终）。"""
    if function_id:
        rows = _query_deployments(
            work_id, "AND function_id=?", (str(function_id),), limit=500)
    else:
        rows = _query_deployments(work_id, "", (), limit=500)
    done = [d for d in (_with_effective(work_id, r) for r in rows)
            if d["effective_status"] not in DEPLOY_ACTIVE]
    return done[:int(limit)]


def has_prior(work_id: str, function_id: str, venue_ref: str,
              venue_kinds: tuple = ("deploy", "author")) -> bool:
    """该职能在 venue_ref（通常一张目标卡）上有过某类差事——
    「作者不评自己」的判据：写过稿的职能只能答辩不能当评委。
    withdrawn 不算（差事撤了＝没真执笔过）。
    """
    kinds = tuple(str(k) for k in venue_kinds if str(k).strip())
    if not kinds:
        return False
    ph = ",".join("?" for _ in kinds)

    def _fn(conn: sqlite3.Connection) -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM function_deployments"
                " WHERE work_id=? AND function_id=? AND venue_ref=?"
                f" AND venue_kind IN ({ph})"
                " AND status != 'withdrawn' LIMIT 1",
                (work_id, str(function_id), str(venue_ref), *kinds)
            ).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    return bool(card_db.read_sync(work_id, _fn, False))
