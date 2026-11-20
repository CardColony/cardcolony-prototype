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
            # [并发登记兜底] 存量重复 biz 行先收编（留最权威一行：
            # delivered > 进行中/待投 > stale），再建部分唯一索引——
            # 否则索引建不起来、约束落空。
            now = time.time()
            c.execute(
                "UPDATE actions SET state='stale', updated_ts=?,"
                " updated_iso=? WHERE biz_key != '' AND action_id IN ("
                "  SELECT action_id FROM ("
                "    SELECT action_id, ROW_NUMBER() OVER ("
                "      PARTITION BY policy_id, scope_key, target, biz_key"
                "      ORDER BY CASE state WHEN 'delivered' THEN 0"
                "        WHEN 'stale' THEN 2 ELSE 1 END,"
                "        created_ts, action_id) AS rn"
                "    FROM actions WHERE biz_key != '') WHERE rn > 1)",
                (now, _iso(now)))
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_biz"
                " ON actions(policy_id, scope_key, target, biz_key)"
                " WHERE biz_key != ''")
            c.commit()
        except sqlite3.Error as exc:  # noqa: BLE001 迁移失败不阻断读写
            logger.warning("[intervention] actions 迁移失败 work=%s: %s",
                           work_id, exc)
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
