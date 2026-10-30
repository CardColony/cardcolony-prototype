# -*- coding: utf-8 -*-
"""治理标记存储(card_governance):沉默/激活等**卡级管理状态**的落盘。

[沉默 2026-10-24 · 用户令「父卡/根卡可让完工的卡沉默,直到再次激活」]
- 沉默是**持久**管理状态(重启不失效),所以不进 control 的进程内标记,
  落库;cards 表加列不可行(card_db 禁改)⇒ 独立表,DDL 放本模块
  (function_directives 先例:card_db.run_write 是通用写门,建表 SQL
  归属各自的存储模块)。
- 语义:``silenced`` 的卡**拒绝一切内部唤醒**(K3 投递/deliver_now/goal 续轮),
  信留在收件箱 pending(解冻后按既有增量投递自然补上);用户直接发话
  (source_kind=human)仍可达(人是最高权威),但**不解除沉默**——解除只有
  一个口:silence_card(action=activate),由父卡/根卡执行。
与 control._HALTED(强制停止)的分工:halt 治「在跑的卡立刻停下」(进程内+
goal pause),silence 治「完工的卡别再吵它」(持久,只挡信路)。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import card_db

logger = logging.getLogger(__name__)

STATE_SILENCED = "silenced"
STATE_ACTIVE = "active"

_GOV_DDL = """CREATE TABLE IF NOT EXISTS card_governance (
    work_id     TEXT NOT NULL,
    card_id     TEXT NOT NULL,
    state       TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    by_id       TEXT DEFAULT '',
    ts          TEXT NOT NULL,
    PRIMARY KEY (work_id, card_id)
)"""


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_GOV_DDL)


async def set_state(work_id: str, card_id: str, state: str, reason: str = "",
                    by: str = "") -> dict[str, Any]:
    """置/清治理标记(覆盖写;ts/操作者留痕)。state: silenced|active。"""
    if state not in (STATE_SILENCED, STATE_ACTIVE):
        raise ValueError(f"非法治理状态: {state}")
    rec = {"work_id": work_id, "card_id": card_id, "state": state,
           "reason": str(reason or "")[:300], "by": str(by or "")[:80],
           "ts": _now()}

    def _fn(conn: sqlite3.Connection) -> None:
        _ensure(conn)
        conn.execute(
            "INSERT INTO card_governance"
            " (work_id, card_id, state, reason, by_id, ts)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(work_id, card_id) DO UPDATE SET"
            " state=excluded.state, reason=excluded.reason,"
            " by_id=excluded.by_id, ts=excluded.ts",
            (work_id, card_id, state, rec["reason"], rec["by"], rec["ts"]))

    await card_db.run_write(work_id, _fn)
    return rec


def get_state(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """读治理标记(无记录/表还没建过 ⇒ None——建表只发生在写路径,
    read_sync 是只读连接,里面跑 DDL 会被拒)。"""

    def _fn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        try:
            row = conn.execute(
                "SELECT state, reason, by_id, ts FROM card_governance"
                " WHERE work_id=? AND card_id=?",
                (work_id, card_id)).fetchone()
        except sqlite3.OperationalError:
            return None                      # 表不存在 ⇒ 未治理过
        if row is None:
            return None
        return {"state": row["state"], "reason": row["reason"] or "",
                "by": row["by_id"] or "", "ts": row["ts"] or ""}

    return card_db.read_sync(work_id, _fn, None)


def is_silenced(work_id: str, card_id: str) -> bool:
    """该卡是否处于沉默态(投递闸用;同步单查,cheap)。"""
    s = get_state(work_id, card_id)
    return bool(s and s.get("state") == STATE_SILENCED)
