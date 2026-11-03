# -*- coding: utf-8 -*-
"""干预台账（迭代五 · S1 落地）。把「还有事没送、还没解决」持久化到
``works/<work>/harness.db``，跨重启可查。

设计口径（docs/harness-智能干预迭代方案-v1-20261031.md §4.1/§4.3）：

- **matters（干预事项）**＝一个未解决的业务问题。绑定策略、作用域、业务对象
  （``matter_key``），状态 open / resolved / cancelled / superseded。
  关闭必须有解除依据（``evidence``）——送达、起轮、回话都只是执行事实，
  不直接关事项。
- **actions（投递动作）**＝一次具体的干预尝试。每次策略命中按目标卡落一行，
  ``fire_key``＝``policy|scope|触发事件身份``，唯一索引兜底重启重演去重
  （同一触发事实在回放下重复命中 ⇒ 同一行，不重复投递）。
- **errors（错误台账）**＝干预链路/harness 机制自身出错的收集面。
  一行一个错误签名（stage+kind+归属），去重累计：反复发生的同一错
  只刷 ``count``/``last_ts``/最新凭据，不刷行数。
- **运行责任不进七天保留**：本库是 harness 运行层，不是事实缓存，
  未结事项与回执必须跨重启保留；原始事件与会话原文不在此复制。

零阻塞：落库失败只告警、不拖垮投递与回合（与 fact_store 同一先例）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("infra.intervention_store")

DB_NAME = "harness.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matters (
    matter_id   TEXT PRIMARY KEY,
    policy_id   TEXT NOT NULL,
    scope_key   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    matter_key  TEXT NOT NULL DEFAULT '',
    objects     TEXT NOT NULL DEFAULT '{}',
    check_on    TEXT NOT NULL DEFAULT '[]',
    state       TEXT NOT NULL DEFAULT 'open',
    basis       TEXT NOT NULL DEFAULT '{}',
    evidence    TEXT NOT NULL DEFAULT '{}',
    created_ts  REAL,
    updated_ts  REAL,
    created_iso TEXT NOT NULL DEFAULT '',
    updated_iso TEXT NOT NULL DEFAULT ''
);
-- 同一（policy, scope, matter_key）只允许一个 open 事项：重复命中不重复立案。
CREATE UNIQUE INDEX IF NOT EXISTS ux_matters_open
    ON matters(policy_id, scope_key, matter_key) WHERE state='open';
CREATE INDEX IF NOT EXISTS ix_matters_state ON matters(state);

CREATE TABLE IF NOT EXISTS actions (
    action_id   TEXT PRIMARY KEY,
    matter_id   TEXT NOT NULL DEFAULT '',
    policy_id   TEXT NOT NULL,
    scope_key   TEXT NOT NULL DEFAULT '',
    fire_key    TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    mode        TEXT NOT NULL DEFAULT 'message',
    wake        INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    spec        TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'pending',
    receipt     TEXT NOT NULL DEFAULT '{}',
    created_ts  REAL,
    updated_ts  REAL,
    created_iso TEXT NOT NULL DEFAULT '', updated_iso TEXT NOT NULL DEFAULT ''
);
-- (fire_key, target) 唯一：同一触发事实对同一目标的投递只存在一行。
CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_fire ON actions(fire_key, target);
CREATE INDEX IF NOT EXISTS ix_actions_state  ON actions(state);
CREATE INDEX IF NOT EXISTS ix_actions_matter ON actions(matter_id);

CREATE TABLE IF NOT EXISTS errors (
    err_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT NOT NULL UNIQUE,
    stage       TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    policy_id   TEXT NOT NULL DEFAULT '',
    action_id   TEXT NOT NULL DEFAULT '',
    matter_id   TEXT NOT NULL DEFAULT '',
    card_id     TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '{}',
    count       INTEGER NOT NULL DEFAULT 1,
    first_ts    REAL, last_ts REAL,
    first_iso   TEXT NOT NULL DEFAULT '', last_iso TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_errors_stage ON errors(stage);
"""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class InterventionStore:
    """按 work 分区的干预台账（每个 work 一个 ``harness.db``）。"""

    def __init__(self, works_dir: Any = None) -> None:
        if works_dir is None:
            from ..storage import card_store
            works_dir = card_store.WORKS_DIR
        self._root = Path(works_dir)
        self._conns: dict[str, sqlite3.Connection] = {}

    # ------------------------------------------------------------ 连接
    def db_path(self, work_id: str) -> Path:
        return self._root / str(work_id) / DB_NAME

    def _conn(self, work_id: str) -> sqlite3.Connection:
        wid = str(work_id)
        c = self._conns.get(wid)
        if c is not None:
            return c
        path = self.db_path(wid)
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(_SCHEMA)
        # [deploy 2026-11-01] actions.spec 列：存量库按列头判存补列，
        # 新库由 _SCHEMA 直接带上。
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(actions)")}
            if "spec" not in cols:
                c.execute(
                    "ALTER TABLE actions ADD COLUMN spec "
                    "TEXT NOT NULL DEFAULT ''")
                c.commit()
        except sqlite3.Error:
            pass
        self._conns[wid] = c
        return c

    def close(self, work_id: Optional[str] = None) -> None:
        if work_id is None:
            for c in self._conns.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conns.clear()
            return
        c = self._conns.pop(str(work_id), None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _row(cur: Any) -> Optional[dict[str, Any]]:
        r = cur.fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("objects", "check_on", "basis", "evidence", "receipt",
                  "detail"):
            if k in d:
                try:
                    d[k] = json.loads(d[k] or "{}") if d[k] else (
                        [] if k == "check_on" else {})
                except (TypeError, ValueError):
                    d[k] = [] if k == "check_on" else {}
        if "wake" in d:
            d["wake"] = bool(d["wake"])
        return d

    @classmethod
    def _rows(cls, cur: Any) -> list[dict[str, Any]]:
        out = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("objects", "check_on", "basis", "evidence", "receipt",
                      "detail"):
                if k in d:
                    try:
                        d[k] = json.loads(d[k] or "{}") if d[k] else (
                            [] if k == "check_on" else {})
                    except (TypeError, ValueError):
                        d[k] = [] if k == "check_on" else {}
            if "wake" in d:
                d["wake"] = bool(d["wake"])
            out.append(d)
        return out

    # ------------------------------------------------------------ 事项
    def open_matter(self, work_id: str, *, policy_id: str, scope_key: str,
                    kind: str, matter_key: str,
                    objects: Optional[dict] = None,
                    check_on: Optional[list] = None,
                    basis: Optional[dict] = None) -> tuple[str, bool]:
        """立案；同 (policy, scope, key) 已有 open 事项 ⇒ 返回既有 id, False。"""
        now = time.time()
        conn = self._conn(work_id)
        try:
            old = conn.execute(
                "SELECT matter_id FROM matters WHERE policy_id=? AND scope_key=?"
                " AND matter_key=? AND state='open'",
                (policy_id, scope_key, matter_key)).fetchone()
            if old:
                return str(old["matter_id"]), False
            mid = "mat-" + uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO matters(matter_id,policy_id,scope_key,kind,matter_key,"
                "objects,check_on,state,basis,created_ts,updated_ts,created_iso,updated_iso)"
                " VALUES(?,?,?,?,?,?,?,'open',?,?,?,?,?)",
                (mid, policy_id, scope_key, kind, matter_key,
                 json.dumps(objects or {}, ensure_ascii=False),
                 json.dumps(check_on or [], ensure_ascii=False),
                 json.dumps(basis or {}, ensure_ascii=False),
                 now, now, _iso(now), _iso(now)))
            conn.commit()
            return mid, True
        except sqlite3.Error as exc:  # noqa: BLE001 立案失败不阻塞投递
            logger.warning("[intervention] 立案失败 work=%s: %s", work_id, exc)
            return "", False

    def close_matter(self, work_id: str, matter_id: str, *,
                     state: str = "resolved",
                     evidence: Optional[dict] = None) -> bool:
        """关事项；其上仍 pending/failed 的动作一并标 stale。"""
        if not matter_id:
            return False
        now = time.time()
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "UPDATE matters SET state=?, evidence=?, updated_ts=?, updated_iso=?"
                " WHERE matter_id=? AND state='open'",
                (state, json.dumps(evidence or {}, ensure_ascii=False),
                 now, _iso(now), matter_id))
            if not cur.rowcount:
                return False
            conn.execute(
                "UPDATE actions SET state='stale', updated_ts=?, updated_iso=?"
                " WHERE matter_id=? AND state IN ('pending','failed')",
                (now, _iso(now), matter_id))
            conn.commit()
            return True
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 关事项失败 work=%s: %s", work_id, exc)
            return False

    def open_matters(self, work_id: str) -> list[dict[str, Any]]:
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM matters WHERE state='open' ORDER BY created_ts")
            return self._rows(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读事项失败 work=%s: %s", work_id, exc)
            return []

    def get_matter(self, work_id: str, matter_id: str) -> Optional[dict[str, Any]]:
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM matters WHERE matter_id=?", (matter_id,))
            return self._row(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读事项失败 work=%s: %s", work_id, exc)
            return None

    # ------------------------------------------------------------ 动作
    def record_action(self, work_id: str, *, policy_id: str, scope_key: str,
                      fire_key: str, target: str, mode: str, wake: bool,
                      message: str, matter_id: str = "",
                      spec: str = "") -> tuple[str, bool]:
        """登记一次投递；(fire_key, target) 已有行 ⇒ 返回既有 id, False（去重）。

        ``spec``：deploy 类动作的参数包（JSON）——函数/场型/退出规格，
        重启恢复时凭它重派，不靠当时 ctx 重算。"""
        now = time.time()
        conn = self._conn(work_id)
        try:
            old = conn.execute(
                "SELECT action_id, state FROM actions WHERE fire_key=? AND target=?",
                (fire_key, target)).fetchone()
            if old:
                return str(old["action_id"]), False
            aid = "act-" + uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO actions(action_id,matter_id,policy_id,scope_key,"
                "fire_key,target,mode,wake,message,spec,state,created_ts,updated_ts,"
                "created_iso,updated_iso) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (aid, matter_id, policy_id, scope_key, fire_key, target, mode,
                 1 if wake else 0, message, str(spec or ""), now, now,
                 _iso(now), _iso(now)))
            conn.commit()
            return aid, True
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 登记动作失败 work=%s: %s", work_id, exc)
            return "", False

    def mark_action(self, work_id: str, action_id: str, state: str,
                    receipt: Optional[dict] = None) -> bool:
        if not action_id:
            return False
        now = time.time()
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "UPDATE actions SET state=?, receipt=?, updated_ts=?, updated_iso=?"
                " WHERE action_id=?",
                (state, json.dumps(receipt or {}, ensure_ascii=False),
                 now, _iso(now), action_id))
            conn.commit()
            return bool(cur.rowcount)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 写回执失败 work=%s: %s", work_id, exc)
            return False

    def get_action(self, work_id: str, action_id: str) -> Optional[dict[str, Any]]:
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,))
            return self._row(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读动作失败 work=%s: %s", work_id, exc)
            return None

    def pending_actions(self, work_id: str) -> list[dict[str, Any]]:
        """待送／上次失败的动作（resume 重投的来源）。"""
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM actions WHERE state IN ('pending','failed')"
                " ORDER BY created_ts")
            return self._rows(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读动作失败 work=%s: %s", work_id, exc)
            return []

    def count_open(self, work_id: str) -> int:
        conn = self._conn(work_id)
        try:
            r = conn.execute(
                "SELECT COUNT(*) AS n FROM matters WHERE state='open'").fetchone()
            return int(r["n"] if r else 0)
        except sqlite3.Error:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------ 错误
    def record_error(self, work_id: str, *, stage: str, kind: str,
                     policy_id: str = "", action_id: str = "",
                     matter_id: str = "", card_id: str = "",
                     key: str = "", detail: Any = None) -> bool:
        """记一次干预链路/harness 机制的错误。**同一签名去重累计**——
        反复发生的同一错只刷 ``count``/``last_ts``/最新凭据，不刷行数。

        ``stage``：环节（resolve/record/deliver/check/ingest/dispatch/
        resume/config/bootstrap）；``kind``：类别（异常类名或语义码）。
        ``key``：追加进签名的自由料（如配置错误的原文——同类不同错不
        并账）。永不抛错：台账自身写不进就只剩日志（库坏时随之降级）。
        """
        stage = str(stage or "")
        kind = str(kind or "")
        dedup = "|".join([stage, kind, str(policy_id or ""),
                          str(action_id or ""), str(matter_id or ""),
                          str(card_id or ""), str(key or "")])
        if isinstance(detail, str):
            detail = {"error": detail}
        try:
            det = json.dumps(detail or {}, ensure_ascii=False)[:2000]
        except (TypeError, ValueError):
            det = json.dumps({"error": str(detail)[:500]},
                             ensure_ascii=False)
        now = time.time()
        try:
            conn = self._conn(work_id)
            conn.execute(
                "INSERT INTO errors(dedup_key,stage,kind,policy_id,action_id,"
                "matter_id,card_id,detail,count,first_ts,last_ts,first_iso,"
                "last_iso) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)"
                " ON CONFLICT(dedup_key) DO UPDATE SET"
                " last_ts=excluded.last_ts, last_iso=excluded.last_iso,"
                " count=count+1, detail=excluded.detail",
                (dedup, stage, kind, str(policy_id or ""),
                 str(action_id or ""), str(matter_id or ""),
                 str(card_id or ""), det, now, now, _iso(now), _iso(now)))
            conn.commit()
            return True
        except sqlite3.Error as exc:  # noqa: BLE001 台账坏了 ⇒ 只剩日志
            logger.warning("[intervention] 记错误失败 work=%s stage=%s: %s",
                           work_id, stage, exc)
            return False

    def list_errors(self, work_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """错误台账（最近在前）。"""
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM errors ORDER BY last_ts DESC LIMIT ?",
                (int(limit),))
            return self._rows(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读错误失败 work=%s: %s",
                           work_id, exc)
            return []


# ---------------------------------------------------------------- 单例
_STORE: Optional[InterventionStore] = None


def store() -> InterventionStore:
    global _STORE
    if _STORE is None:
        _STORE = InterventionStore()
    return _STORE


def reset(store_: Optional[InterventionStore] = None) -> InterventionStore:
    """测试换沙箱：清连接缓存（换目录后同 work_id 不串库）。"""
    global _STORE
    if _STORE is not None:
        _STORE.close()
    _STORE = store_ or InterventionStore()
    return _STORE
