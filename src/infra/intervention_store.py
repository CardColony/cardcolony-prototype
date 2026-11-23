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
from contextlib import closing
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
    biz_key     TEXT NOT NULL DEFAULT '',
    step_json   TEXT NOT NULL DEFAULT '',
    claim_token TEXT NOT NULL DEFAULT '',
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
-- [动作按业务状态去重 2026-11-20 · 批次A返工] (policy,scope,target,biz_key)
-- 查口——biz_key 由 then.dedup_fields 声明的触发字段拼成（同一可运行
-- 步骤换 seq/ts ⇒ 同 biz_key ⇒ 沿用原动作身份）。
-- [并发登记兜底 2026-11-20 · 批次A第三轮返工] 业务键唯一性落实到
-- **部分唯一索引**（biz_key 非空才约束——空 biz 是未声明 dedup_fields
-- 的策略，同 policy/scope/target 的不同 fire_key 动作必须共存）：
-- 两连接同时查无行再各插 ⇒ 后者撞索引，record_action 读回同一编号。

CREATE TABLE IF NOT EXISTS hop_attempt_budget (
    card_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    logical_hop INTEGER NOT NULL,
    used INTEGER NOT NULL,
    PRIMARY KEY (card_id, session_id, pair_id, logical_hop)
);

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


def _dedup_actions(conn: sqlite3.Connection) -> int:
    """[存量收编 2026-11-20 · 批次A第四轮返工] actions 表按
    ``(policy_id, scope_key, target, biz_key)`` 收编重复行：

    - 保留最权威一行（``delivered`` > 进行中/待投 > ``stale``，
      再按 ``created_ts``、``action_id`` 定序）；
    - 败选行标 ``stale``、``receipt.biz_dedup`` 记审计（原 biz_key/
      保留行/时刻），并**把 ``biz_key`` 置空释放唯一索引占用**——
      只标 stale 不释键，``ux_actions_biz`` 部分索引照样撞键
      建不起来（实测缺陷）。

    须在调用方事务内执行；返回收编行数。"""
    rows = conn.execute(
        "SELECT action_id, policy_id, scope_key, target, biz_key,"
        " state, created_ts, receipt FROM actions"
        " WHERE biz_key != ''"
        " ORDER BY policy_id, scope_key, target, biz_key,"
        " CASE state WHEN 'delivered' THEN 0 WHEN 'stale' THEN 2"
        " ELSE 1 END, created_ts, action_id").fetchall()
    seen: dict[tuple, str] = {}
    n = 0
    now = time.time()
    iso = _iso(now)
    for r in rows:
        key = (str(r["policy_id"]), str(r["scope_key"]),
               str(r["target"]), str(r["biz_key"]))
        if key not in seen:
            seen[key] = str(r["action_id"])
            continue
        try:
            receipt = json.loads(r["receipt"] or "{}")
        except (TypeError, ValueError):
            receipt = {}
        if not isinstance(receipt, dict):
            receipt = {}
        receipt["biz_dedup"] = {"biz_key": str(r["biz_key"]),
                                "kept": seen[key], "ts": iso}
        conn.execute(
            "UPDATE actions SET state='stale', biz_key='', receipt=?,"
            " updated_ts=?, updated_iso=? WHERE action_id=?",
            (json.dumps(receipt, ensure_ascii=False), now, iso,
             str(r["action_id"])))
        n += 1
    return n


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
        # [库搬出施工目录 2026-11-07] harness.db 也放 works_works_data/{wid}/。
        return self._data_dir(work_id) / DB_NAME

    def _data_dir(self, work_id: str) -> Path:
        # 与 card_store.db_root 同规则：{works根名}_data。
        return self._root.parent / f"{self._root.name}_data" / str(work_id)

    def _conn(self, work_id: str) -> sqlite3.Connection:
        wid = str(work_id)
        c = self._conns.get(wid)
        if c is not None:
            return c
        # 旧位置还在 ⇒ 原子搬进 data/（_conns 单例串行）
        from ..storage import card_store as _cs
        path = _cs.migrate_db_file(self._root / wid / DB_NAME, self.db_path(wid))
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
            for col, ddl in (
                    ("spec", "TEXT NOT NULL DEFAULT ''"),
                    ("biz_key", "TEXT NOT NULL DEFAULT ''"),
                    # [步骤身份 2026-11-20 · 批次A第三轮返工] dedup_fields
                    # 解析值随动作落账——投递/恢复把 {batch,draft,next}
                    # 传给评审属主执行前复核，过期销账不执行。
                    ("step_json", "TEXT NOT NULL DEFAULT ''"),
                    # [领取凭据 2026-11-20 · 批次A第三轮返工] 发送者
                    # 身份——写回必须持本次领取的 token，防旧属主迟到
                    # 回执覆盖新属主的在途投递。
                    ("claim_token", "TEXT NOT NULL DEFAULT ''")):
                if col not in cols:
                    c.execute(f"ALTER TABLE actions ADD COLUMN {col} {ddl}")
            c.commit()
        except sqlite3.Error as exc:  # noqa: BLE001 迁移失败不阻断读写
            logger.warning("[intervention] actions 迁移失败 work=%s: %s",
                           work_id, exc)
        # [并发登记兜底·存量收编 2026-11-20 · 批次A第四轮返工]
        # 重复 biz 行与唯一索引在**同一事务**内处理：败选行释键
        # （biz_key=''）才真正退出部分索引；建索引后**核验真实存在**
        # 且无残留重复——核验不过 loud 告警，不再静默吞。
        try:
            _dedup_actions(c)
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_biz"
                " ON actions(policy_id, scope_key, target, biz_key)"
                " WHERE biz_key != ''")
            _idx = c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name='ux_actions_biz'").fetchone()
            if not _idx:
                raise sqlite3.OperationalError(
                    "ux_actions_biz 索引核验不存在")
            _dup = c.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM actions"
                " WHERE biz_key != '' GROUP BY policy_id, scope_key,"
                " target, biz_key HAVING COUNT(*)>1)").fetchone()
            if _dup and int(_dup[0] or 0):
                raise sqlite3.OperationalError(
                    f"ux_actions_biz 收编后仍有 {_dup[0]} 组重复 biz 键")
            c.commit()
        except sqlite3.Error as exc:  # noqa: BLE001
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            logger.error("[intervention] ux_actions_biz 迁移失败"
                         " work=%s: %s", work_id, exc)
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
                  "detail", "step_json"):
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
                      "detail", "step_json"):
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

    def update_matter_basis(self, work_id: str, matter_id: str, *,
                            basis: Optional[dict] = None) -> bool:
        """[超限合并 批五] 连续超限期内的后续超限编辑：刷新事项事实锚——
        最新 card/seq/ts 与实测字数进 basis，事项与动作身份不变。"""
        if not matter_id:
            return False
        now = time.time()
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "UPDATE matters SET basis=?, updated_ts=?, updated_iso=?"
                " WHERE matter_id=? AND state='open'",
                (json.dumps(basis or {}, ensure_ascii=False),
                 now, _iso(now), matter_id))
            conn.commit()
            return bool(cur.rowcount)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 刷事项事实锚失败 work=%s: %s",
                           work_id, exc)
            return False

    def refresh_action_message(self, work_id: str, action_id: str,
                               message: str) -> bool:
        """[超限合并 批五] 尚未受理的动作按最新事实重渲文案。

        只碰 ``pending/failed``——sending/sent/delivered 的动作已受理
        或入会话，历史提醒原样可查不被改写。"""
        if not action_id:
            return False
        now = time.time()
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "UPDATE actions SET message=?, updated_ts=?, updated_iso=?"
                " WHERE action_id=? AND state IN ('pending','failed')",
                (str(message or ""), now, _iso(now), action_id))
            conn.commit()
            return bool(cur.rowcount)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 刷动作材料失败 work=%s: %s",
                           work_id, exc)
            return False

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
                      spec: str = "", biz_key: str = "",
                      step: Optional[dict] = None) -> tuple[str, bool]:
        """登记一次投递；(fire_key, target) 已有行 ⇒ 返回既有 id, False（去重）。

        ``spec``：deploy 类动作的参数包（JSON）——函数/场型/退出规格，
        重启恢复时凭它重派，不靠当时 ctx 重算。

        ``biz_key``：业务状态键（策略 ``then.dedup_fields`` 声明的触发
        字段拼成）——同一业务状态换触发事实（seq/ts 不同）再来时，
        (policy,scope,target,biz_key) 命中既有行 ⇒ 沿用原动作身份，
        不造第二行（批次A返工：同一可运行步骤只投递一次）。

        ``step``：dedup_fields 的**结构化解析值**（如
        ``{"batch":..,"draft":..,"next":..}``）——投递/恢复把它传给
        评审属主执行前复核，过期销账（批次A第三轮返工）。

        [并发登记兜底] 唯一索引（ux_actions_fire / ux_actions_biz）是
        最终裁判：两个连接同时查无行再各插 ⇒ 后到者撞 IntegrityError
        ⇒ 读回先到者的 action_id——并发方拿到**同一个编号**，不会
        各发行一行。"""
        now = time.time()
        conn = self._conn(work_id)

        def _find() -> Optional[str]:
            old = conn.execute(
                "SELECT action_id FROM actions WHERE fire_key=? AND target=?",
                (fire_key, target)).fetchone()
            if not old and biz_key:
                old = conn.execute(
                    "SELECT action_id FROM actions WHERE policy_id=?"
                    " AND scope_key=? AND target=? AND biz_key=?",
                    (policy_id, scope_key, target, biz_key)).fetchone()
            return str(old["action_id"]) if old else None

        try:
            hit = _find()
            if hit:
                return hit, False
            aid = "act-" + uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO actions(action_id,matter_id,policy_id,scope_key,"
                "fire_key,biz_key,step_json,target,mode,wake,message,spec,"
                "state,created_ts,updated_ts,created_iso,updated_iso)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (aid, matter_id, policy_id, scope_key, fire_key,
                 str(biz_key or ""),
                 json.dumps(step or {}, ensure_ascii=False)
                 if step else "",
                 target, mode,
                 1 if wake else 0, message, str(spec or ""), now, now,
                 _iso(now), _iso(now)))
            conn.commit()
            return aid, True
        except sqlite3.IntegrityError:
            # 并发登记：另一连接已插入同身份行 ⇒ 读回同一编号
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            hit = _find()
            if hit:
                return hit, False
            logger.warning("[intervention] 登记撞键且读回落空 work=%s",
                           work_id)
            return "", False
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 登记动作失败 work=%s: %s", work_id, exc)
            return "", False

    def prepare_review_action(self, work_id: str, action_id: str,
                              step: dict[str, Any]) -> bool:
        """合并正卡请求的材料身份及点名意图，保持原动作号和领取者。"""
        try:
            # 单独的短事务，避免与其他槽环共用台账连接上的事务。
            with closing(sqlite3.connect(str(self.db_path(work_id)), timeout=10.0)) as conn, conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                row = self._row(conn.execute(
                    "SELECT * FROM actions WHERE action_id=?", (action_id,)))
                if not row:
                    return False
                state = row["state"]
                receipt = row.get("receipt") or {}
                # 同一步仍可执行且旧账缺启动事实，允许正卡恢复旧版误记的成功。
                legacy_seat_receipt = (state == "delivered"
                                       and str(step.get("next") or "").startswith("seat:")
                                       and "started" not in receipt)
                if state not in ("pending", "failed", "sent", "stale") and not legacy_seat_receipt:
                    return True
                merged = {**(row.get("step_json") or {}), **step}
                if state == "stale" or legacy_seat_receipt:
                    state = "pending"
                if legacy_seat_receipt:
                    receipt = {**receipt, "legacy_resume": True}
                conn.execute(
                    "UPDATE actions SET step_json=?, state=?, receipt=? WHERE action_id=?",
                    (json.dumps(merged, ensure_ascii=False), state,
                     json.dumps(receipt, ensure_ascii=False), action_id))
                return True
        except sqlite3.Error as exc:
            logger.warning("[intervention] 合并评审动作失败 work=%s aid=%s: %s",
                           work_id, action_id, exc)
            return False

    def claim_action(self, work_id: str, action_id: str, *,
                     dead_sending_before: float) -> Optional[str]:
        """[并发领取 2026-11-20 · 批次A返工3/第三轮返工4] 发出前**原子
        领取**动作：``pending/failed/sent`` ⇒ ``sending``；``sending``
        但 ``updated_ts < dead_sending_before``（租约过期＝属主确认
        退出/失联）⇒ 也可重领。

        成功返回**领取凭据** ``claim_token``——写回（mark_action/
        mark_receipt）必须持本次凭据，旧属主迟到回执被拒；失败返
        ``None``。实时投递与 resume 共用这道闸、同一租约口径
        （``now-CLAIM_STALE_S``）——boot 时刻不再是接管判据：新
        runner 启动**抢不走**仍在宽限内的在途投递。"""
        if not action_id:
            return None
        now = time.time()
        token = "clm-" + uuid.uuid4().hex[:12]
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "UPDATE actions SET state='sending', claim_token=?,"
                " updated_ts=?, updated_iso=? WHERE action_id=? AND"
                " (state IN ('pending','failed','sent') OR"
                "  (state='sending' AND updated_ts<?))",
                (token, now, _iso(now), action_id, dead_sending_before))
            conn.commit()
            return token if cur.rowcount else None
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 领取动作失败 work=%s: %s",
                           work_id, exc)
            return None

    def mark_action(self, work_id: str, action_id: str, state: str,
                    receipt: Optional[dict] = None, *,
                    claim_token: str = "") -> bool:
        """写回执。``claim_token`` 非空 ⇒ **领取凭据核对**：只允许
        当前 sending 属主写回（``claim_token`` 相符且仍 ``sending``），
        旧属主迟到回执/系统侧并发改写一律拒写。"""
        if not action_id:
            return False
        now = time.time()
        conn = self._conn(work_id)
        try:
            if claim_token:
                cur = conn.execute(
                    "UPDATE actions SET state=?, receipt=?, claim_token='',"
                    " updated_ts=?, updated_iso=? WHERE action_id=?"
                    " AND claim_token=? AND state='sending'",
                    (state, json.dumps(receipt or {}, ensure_ascii=False),
                     now, _iso(now), action_id, claim_token))
            else:
                cur = conn.execute(
                    "UPDATE actions SET state=?, receipt=?, updated_ts=?,"
                    " updated_iso=? WHERE action_id=?",
                    (state, json.dumps(receipt or {}, ensure_ascii=False),
                     now, _iso(now), action_id))
            conn.commit()
            return bool(cur.rowcount)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 写回执失败 work=%s: %s", work_id, exc)
            return False

    def touch_claim(self, work_id: str, action_id: str,
                    claim_token: str) -> Optional[bool]:
        """[在途续约 2026-11-20 · 批次A第四轮返工] 活发送者的心跳写回：
        ``sending`` 行 ``updated_ts`` 周期性刷新 ⇒ resume 的租约过期
        判据只在「发送者真的退出（心跳停了）」后成立——**超时不再
        等于可抢**。用**独立短连接**，不碰属主连接上的在途事务。

        三态返回（批次A第五轮返工——可重试错误与凭据失效分开）：

        - ``True`` ＝续约成功；
        - ``False``＝**凭据失效**（参数缺/领取易主/已不在 ``sending``
          ——UPDATE 零行命中），发送者应**停止**续约，不再替别人续；
        - ``None`` ＝**可重试错误**（``database is locked`` 等瞬时
          I/O、库文件暂不在）——本轮没续上，下轮再来；一次锁冲突
          不该把心跳打成死循环外。"""
        if not action_id or not claim_token:
            return False
        path = self.db_path(work_id)
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(str(path), timeout=10.0)
            try:
                now = time.time()
                cur = conn.execute(
                    "UPDATE actions SET updated_ts=?, updated_iso=?"
                    " WHERE action_id=? AND claim_token=?"
                    " AND state='sending'",
                    (now, _iso(now), action_id, claim_token))
                conn.commit()
                return bool(cur.rowcount)
            finally:
                conn.close()
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 续约失败 work=%s（下轮重试）"
                           ": %s", work_id, exc)
            return None

    def find_action_by_biz(self, work_id: str, policy_id: str,
                           scope_key: str, target: str,
                           biz_key: str) -> str:
        """按当前业务键查动作——与 ``record_action._find`` 同口径
        （**不限状态**：delivered/stale 也占位，同键即同账）。
        命中返回 action_id，未命中 ``""``。"""
        if not biz_key:
            return ""
        conn = self._conn(work_id)
        try:
            row = conn.execute(
                "SELECT action_id FROM actions WHERE policy_id=?"
                " AND scope_key=? AND target=? AND biz_key=?",
                (policy_id, scope_key, target, biz_key)).fetchone()
            return str(row["action_id"]) if row else ""
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 按业务键查动作失败 work=%s: %s",
                           work_id, exc)
            return ""

    def rekey_action(self, work_id: str, action_id: str, *,
                     biz_key: str, step: Optional[dict] = None) -> str:
        """[旧账归并 2026-11-20 · 批次A第五轮返工] 把刷新出的**现状
        业务身份**重锚到动作行（``biz_key``＋``step_json``）——刷新
        后的旧账按当前键登记，后续恢复/并发按现键查得到它。

        [租约分离 2026-11-20 · 批次A第六轮返工] **不碰
        ``updated_ts``/``updated_iso``**——那是发送者的租约心跳位：
        重锚业务身份把死 ``sending`` 行的租约"续"活，会导致同一次
        恢复里领取被拒、死账永远挂在 ``sending``。业务键更新与租约
        续期是两条账。

        返回 ``"rekeyed"``／``"conflict"``（另一动作已占该业务键
        ——唯一索引终审，调用方并入那一行）／``"error"``。"""
        if not action_id or not biz_key:
            return "error"
        conn = self._conn(work_id)
        try:
            conn.execute(
                "UPDATE actions SET biz_key=?, step_json=?"
                " WHERE action_id=?",
                (biz_key,
                 json.dumps(step or {}, ensure_ascii=False) if step else "",
                 action_id))
            conn.commit()
            return "rekeyed"
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            return "conflict"
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 重锚业务键失败 work=%s: %s",
                           work_id, exc)
            return "error"

    def get_action(self, work_id: str, action_id: str) -> Optional[dict[str, Any]]:
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,))
            return self._row(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读动作失败 work=%s: %s", work_id, exc)
            return None

    def latest_action(self, work_id: str, target: str, *,
                      mode: str = "") -> Optional[dict[str, Any]]:
        """某投递目标**最近一笔**动作（按 updated_ts）——状态读口展示
        「最近一次续跑的结果与动作编号」用。"""
        conn = self._conn(work_id)
        try:
            if mode:
                cur = conn.execute(
                    "SELECT * FROM actions WHERE target=? AND mode=?"
                    " ORDER BY updated_ts DESC LIMIT 1", (target, mode))
            else:
                cur = conn.execute(
                    "SELECT * FROM actions WHERE target=?"
                    " ORDER BY updated_ts DESC LIMIT 1", (target,))
            return self._row(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读最近动作失败 work=%s: %s",
                           work_id, exc)
            return None

    def pending_actions(self, work_id: str) -> list[dict[str, Any]]:
        """待送／上次失败／请求边界已过的动作（resume 重投的来源）。

        ``sent`` 只由 remediation 占位写（请求已发、回包未证）——恢复
        时必须看得见它对账（核销成 unverified/delivered），不能漏。
        ``sending`` 是投递在途占位——resume 凭 ``claim_action`` 的
        boot 时刻闸分辨死活：本进程启动前的 sending 属死进程可收，
        本进程内的在途投递不抢。"""
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM actions WHERE state IN"
                " ('pending','failed','sent','sending')"
                " ORDER BY created_ts")
            return self._rows(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读动作失败 work=%s: %s", work_id, exc)
            return []

    def matter_actions(self, work_id: str, matter_id: str) -> list[dict[str, Any]]:
        """某事项名下的全部动作行——业务状态→原动作身份的复用查口。

        [动作身份去重 2026-11-20 · 任务书批次A] 「新事实仍可能描述同一业务
        状态」：事项按 (policy,scope,matter_key) 去重，动作也要按事项复用——
        命中既有 open 事项时投递侧沿用这里查到的 action_id，不再造新行。"""
        conn = self._conn(work_id)
        try:
            cur = conn.execute(
                "SELECT * FROM actions WHERE matter_id=? ORDER BY created_ts",
                (str(matter_id),))
            return self._rows(cur)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 读事项动作失败 work=%s: %s",
                           work_id, exc)
            return []

    def claim_anomaly_decision(
            self, work_id: str, *,
            policy_id: str, scope_key: str, fire_key: str, target: str,
            mode: str, wake: bool, message: str, matter_id: str = "",
            step: dict,
    ) -> tuple[str, bool, str]:
        """按卡、会话、回合、跳次、异常与任务在同一事务内查并登记。"""
        pair, round_, anomaly = (
            str(step.get(k) or "").strip() for k in ("pair_id", "round", "anomaly"))
        if not (target and pair and round_ and anomaly):
            return "", False, ""
        sid = str(step.get("sid") or target)
        task = str(step.get("task_id") or "")
        biz_key = "anomaly:" + json.dumps(
            [sid, pair, round_, anomaly, task], ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        try:
            self._conn(work_id)
            with closing(sqlite3.connect(
                    str(self.db_path(work_id)), timeout=10.0)) as conn, conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                hit = conn.execute(
                    "SELECT action_id,policy_id FROM actions WHERE target=?"
                    " AND policy_id IN ('turn_remediation','pseudo_call_reinvoke')"
                    " AND biz_key=? ORDER BY created_ts LIMIT 1",
                    (target, biz_key)).fetchone()
                if hit is None and sid == target:
                    # 旧账只在能还原原跳时沿用。旧 runner 的 message.rounds
                    # 是触发跳，receipt.round 则是随后发出的补救跳。
                    rows = conn.execute(
                        "SELECT action_id,policy_id,message FROM actions WHERE target=?"
                        " AND ((policy_id='pseudo_call_reinvoke' AND biz_key=?)"
                        " OR (policy_id='turn_remediation' AND biz_key=''"
                        " AND instr(fire_key,?)=1)) ORDER BY created_ts",
                        (target, f"{pair}|{round_}|{anomaly}",
                         f"remediation|{target}|{pair}|"))
                    for row in rows:
                        if row["policy_id"] == "pseudo_call_reinvoke":
                            if task:
                                continue
                        else:
                            try:
                                previous = json.loads(row["message"] or "{}")
                            except (TypeError, ValueError):
                                continue
                            if not isinstance(previous, dict):
                                continue
                            bound = previous.get("bound")
                            old_task = (bound.get("task_id") if isinstance(bound, dict) else "")
                            if (str(previous.get("rounds") or "") != round_
                                    or str(old_task or "") != task):
                                continue
                        hit = row
                        break
                if hit:
                    return (str(hit["action_id"]), False,
                            str(hit["policy_id"] or ""))
                aid = "act-" + uuid.uuid4().hex[:12]
                # runner 的旧事实键只有回合，策略则保留原事件键，
                # 关闭试点后重放同一事件仍命中原动作。
                if policy_id == "turn_remediation":
                    fire_key = f"{fire_key}|{biz_key}"
                conn.execute(
                    "INSERT INTO actions(action_id,matter_id,policy_id,"
                    "scope_key,fire_key,biz_key,step_json,target,mode,wake,"
                    "message,spec,state,created_ts,updated_ts,created_iso,"
                    "updated_iso)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
                    (aid, matter_id, policy_id, scope_key, fire_key, biz_key,
                     json.dumps(step, ensure_ascii=False), target, mode,
                     1 if wake else 0, message, "",
                     now, now, _iso(now), _iso(now)))
                return aid, True, str(policy_id)
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("[intervention] 原子决定登记失败 work=%s: %s",
                           work_id, exc)
            return "", False, ""

    def reserve_hop_attempt(self, identity: dict, used: int) -> tuple[bool, Optional[int]]:
        """在实际出站前落已用额度。宿主内存与台账不一致时保留原记录。"""
        work_id = str(identity.get("work_id") or "")
        key = (str(identity.get("card_id") or ""),
               str(identity.get("session_id") or ""),
               str(identity.get("pair_id") or ""), int(identity.get("logical_hop") or 0))
        if not work_id or not all(key):
            return False, None
        try:
            self._conn(work_id)
            with closing(sqlite3.connect(str(self.db_path(work_id)), timeout=0.25)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT used FROM hop_attempt_budget WHERE card_id=?"
                    " AND session_id=? AND pair_id=? AND logical_hop=?", key).fetchone()
                prior = row[0] if row else 0
                if prior != used:
                    return False, prior
                conn.execute(
                    "INSERT INTO hop_attempt_budget VALUES(?,?,?,?,?)"
                    " ON CONFLICT(card_id,session_id,pair_id,logical_hop)"
                    " DO UPDATE SET used=excluded.used", (*key, used + 1))
                return True, used + 1
        except (OSError, sqlite3.Error) as exc:
            logger.warning("[intervention] 同跳尝试记账失败 work=%s: %s", work_id, exc)
            return False, None

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
            det = json.dumps(detail or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            det = json.dumps({"error": str(detail)},
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
