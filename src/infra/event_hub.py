# -*- coding: utf-8 -*-
"""事件枢纽（event_hub）：系统内**所有事件**的统一登记与汇总面。

[基础设施 2026-10-26 · 用户裁定「事件管理、日志连同后续的 harness 消息层，是一个
模块，是整个项目的基础设施，而不是散在各处的单纯功能实现」]

## 它解决什么

改前事件散在三处、互不汇总：

- ``event_stream.emit``   → 内存 journal（刷新即失）＋ ``event_log`` 每卡一个 jsonl
- ``event_stream.emit_work`` → **只广播、不落盘**（card_changed / card_running 零持久记录）
- 四个 store 的写卡路径 → **零事件**（shadow_store / role_store / function_store 完全沉默）

于是「这个 work 里发生过什么」只能逐卡读 jsonl，且卡面编辑、影子卡生命周期根本没有记录。

## 本模块的三件事

1. **登记**：``config/event_catalog.json`` 是事件类型的唯一登记处。未登记的类型
   在 ``record`` 时 fail-loud（开发期抛 ``UnknownEventType``、生产期 ERROR 日志但
   仍记录——事件是观测通道，不能因为漏登记就丢事实）。
2. **汇总**：所有事件按 ``(work_id, card_id, seq)`` 落到 ``work_events`` 表，
   **按卡分开、所有卡类型都收**。一次 SQL 即可跨卡统计，同时保留单卡维度。
3. **状态变更**：``record_state_change`` 给四个 store 用，载荷带
   ``block / old / new / by / cause``，主动与被动的编辑分得开。

## 性能

事件是高频通道（reasoning 逐帧、tool_call 每轮数条），逐条写表会被 SQLite
写事务拖慢主链。故 ``record`` 只做**同步入缓冲**（微秒级），真正的落表由
``maybe_flush`` 批量完成（条数阈值 / 时间阈值双触发），由 ``event_log.append``
（已在异步链上）驱动。``append_sync`` 无事件循环，只入缓冲，随下一次异步
flush 一起落表。

铁则与 event_log 同族：**零阻断**——任何异常只记 warning，绝不影响主流程。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("infra.event_hub")

#: 登记表路径（可被测试/沙箱用环境变量重定向）
ENV_CATALOG_PATH = "ECO_EVENT_CATALOG_PATH"
_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "event_catalog.json")

_catalog_cache: dict[str, dict[str, Any]] | None = None
_catalog_mtime: float = 0.0

#: 缓冲：work_id -> [row, ...]
_buf: dict[str, list[dict[str, Any]]] = {}
_buf_lock = threading.Lock()

#: 落表阈值
FLUSH_MAX_ROWS = 40          # 单 work 缓冲达到该条数即落表
FLUSH_MAX_SECONDS = 1.5      # 距上次落表超过该秒数即落表（事件稀疏时也不至于长期滞留）
_last_flush: dict[str, float] = {}

#: 未登记类型只在进程内吵一次，避免刷屏（但每条仍会记录）
_warned: set[str] = set()


def _disabled() -> bool:
    """全局开关（排查/极端降级用）：置 1 则只登记不汇总，不动 DB。"""
    return os.environ.get("ECO_EVENT_HUB_OFF") == "1"


def drop_work(work_id: str) -> None:
    """丢弃某 work 的全部缓冲（work 被删除/迁移前调用）。

    必要性：缓冲落表会打开 cards.db；Windows 下句柄未释放时删除目录会
    ``WinError 32``，删除方只能降级成标记删除。删除前先丢缓冲，后续 flush
    就不会再碰这个库。
    """
    with _buf_lock:
        _buf.pop(str(work_id), None)
        _last_flush.pop(str(work_id), None)


class UnknownEventType(RuntimeError):
    """事件类型未在 ``config/event_catalog.json`` 登记。"""


# ---------------------------------------------------------------- 登记表
def load_catalog() -> dict[str, dict[str, Any]]:
    """读事件类型登记表（每次现读，文件变更即时生效；坏表 → 空表并告警）。"""
    global _catalog_cache, _catalog_mtime
    path = os.environ.get(ENV_CATALOG_PATH) or _DEFAULT_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        logger.warning("[event_hub] 登记表不存在: %s（视为空表）", path)
        return {}
    if _catalog_cache is not None and mtime == _catalog_mtime:
        return _catalog_cache
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        types = raw.get("types") or {}
        if not isinstance(types, dict):
            raise ValueError("types 不是对象")
        _catalog_cache = types
        _catalog_mtime = mtime
        return types
    except Exception as exc:  # noqa: BLE001 坏表 ⇒ fail-open（调用方自行决定）
        logger.warning("[event_hub] 登记表读取失败 %s: %s", path, exc)
        return {}


def reset_cache() -> None:
    """清登记表缓存（测试/热加载用）。"""
    global _catalog_cache, _catalog_mtime
    _catalog_cache = None
    _catalog_mtime = 0.0


def reset_state() -> None:
    """清登记表缓存 + 全部缓冲（测试隔离用）。"""
    reset_cache()
    with _buf_lock:
        _buf.clear()
        _last_flush.clear()
    _warned.clear()


def is_registered(ev_type: str) -> bool:
    return str(ev_type) in load_catalog()


def is_triggerable(ev_type: str) -> bool:
    """该类型是否允许作为 harness 规则触发源（未登记 ⇒ False）。"""
    entry = load_catalog().get(str(ev_type)) or {}
    return bool(entry.get("triggerable"))


# ---------------------------------------------------------------- 字段契约
# [harness 第 1 步 2026-10-28 · 用户口径「能当判断依据的就收」]
#
# 报备的实质不是「登记一个类型名」，而是「登记这个类型必须带哪些零件」。
# 所以 catalog 每条多一个 fields 块，把 data 里的键分成三类：
#
#   part     必须带的零件——短、可枚举、可比较，**harness 条件的唯一取数面**。
#            缺一个就是事实缺失，测试红。
#   optional 允许缺的零件（错误路径天然没有某些字段）。
#   payload  原文全文，仅供人看/事后分析；**不进条件面**，按保留策略先清。
#
# 判据来源：能当判断依据的进 part，只能当原文的进 payload。两者寿命不同。
def fields_of(ev_type: str) -> dict[str, list[str]]:
    """取某类型的字段契约（未登记 ⇒ 空契约）。"""
    entry = load_catalog().get(str(ev_type)) or {}
    f = entry.get("fields")
    if not isinstance(f, dict):
        return {"part": [], "optional": [], "payload": []}
    return {
        "part": [str(x) for x in (f.get("part") or [])],
        "optional": [str(x) for x in (f.get("optional") or [])],
        "payload": [str(x) for x in (f.get("payload") or [])],
    }


def missing_parts(ev_type: str, data: Any) -> list[str]:
    """这条事件的 data 缺了哪些**必须带**的零件。

    只判「键在不在」，不判值真假——空串是合法的零件值（"没带 kind"本身
    就是一条可判断的事实），键不在才是事实缺失。
    """
    d = data if isinstance(data, dict) else {}
    return [k for k in fields_of(ev_type)["part"] if k not in d]


def is_deprecated(ev_type: str) -> bool:
    """该类型是否已停用（历史数据仍可解释，只是不再新写）。"""
    entry = load_catalog().get(str(ev_type)) or {}
    return bool(entry.get("deprecated"))


def is_write_blocked(ev_type: str) -> bool:
    """该类型是否禁止新写入。

    [清退机制 2026-10-28] 清退 ≠ 删行：历史 jsonl 里躺着旧类型的记录，
    删了登记表那些历史就变成无法解释的孤儿。故照
    ``card_type_registry.legacy_types`` 的手法——标 ``deprecated`` 让旧数据
    可解释，标 ``write_blocked`` 让新写入被拒。
    """
    entry = load_catalog().get(str(ev_type)) or {}
    return bool(entry.get("write_blocked"))


def validate_type(ev_type: str, *, strict: bool = False) -> None:
    """校验事件类型已登记、且未被清退。

    ``strict=True``（开发/测试）⇒ 未登记直接抛 ``UnknownEventType``；
    缺省（生产）⇒ 只记一次 ERROR 日志，仍放行记录——事件是观测通道，
    漏登记不该让事实消失，但必须吵得足够响。

    已被 ``write_blocked`` 的类型：同样只吵不拦（观测通道不丢事实），
    但日志级别更高，便于在清退期发现"还有谁在写"。
    """
    t = str(ev_type or "")
    if not t:
        return
    if is_registered(t):
        if is_write_blocked(t) and t not in _warned:
            _warned.add(t)
            logger.error("[event_hub] 已清退的事件类型 %r 仍有写入——"
                         "该类型已标 write_blocked，请移除产生点", t)
        return
    if strict:
        raise UnknownEventType(
            f"事件类型未登记: {t!r}（请加到 config/event_catalog.json 的 types 下）")
    if t not in _warned:
        _warned.add(t)
        logger.error("[event_hub] 未登记的事件类型 %r —— 请补进 "
                     "config/event_catalog.json（本条只吵一次）", t)


# ---------------------------------------------------------------- 记录
def _row_of(work_id: str, card_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    data = rec.get("data")
    try:
        data_json = json.dumps(data if isinstance(data, dict) else {"value": data},
                               ensure_ascii=False)
    except (TypeError, ValueError):
        data_json = '{"_note": "unserializable"}'
    return {
        "work_id": str(work_id),
        "card_id": str(rec.get("card_id") or card_id or ""),
        "seq": int(rec.get("seq") or 0),
        "type": str(rec.get("type") or ""),
        "run_id": str(rec.get("run_id") or ""),
        "round": int(rec.get("round") or 0),
        "ts": str(rec.get("ts") or ""),
        "data": data_json,
    }


def record(work_id: str, card_id: str, rec: dict[str, Any]) -> None:
    """把一条**已落 jsonl** 的事件收进汇总缓冲（同步、微秒级、零阻断）。

    调用点：``event_log._append_locked`` 落盘成功后（唯一真正的写点），
    因此 jsonl 与 work_events 天然同 seq、同刻、同载荷，不会错位。
    """
    try:
        if _disabled():
            return
        validate_type(str(rec.get("type") or ""))
        row = _row_of(work_id, card_id, rec)
        if not row["card_id"]:
            return
        with _buf_lock:
            _buf.setdefault(str(work_id), []).append(row)
    except Exception as exc:  # noqa: BLE001 汇总失败不阻断事件落盘
        logger.warning("[event_hub] record 失败 work=%s card=%s: %s",
                       work_id, card_id, exc)


async def maybe_flush(work_id: str, *, force: bool = False) -> int:
    """按阈值把缓冲落进 work_events 表，返回本次落表行数。

    阈值：条数 ≥ ``FLUSH_MAX_ROWS`` 或距上次落表 ≥ ``FLUSH_MAX_SECONDS``。
    ``force=True`` 无条件落表（测试 drain / 关停用）。
    """
    rows = _take_rows(str(work_id), force=force)
    if not rows:
        return 0
    try:
        from ..storage import card_db
        await card_db.run_write(str(work_id), lambda conn: _insert_rows(conn, rows))
        return len(rows)
    except Exception as exc:  # noqa: BLE001 落表失败：把行放回缓冲，下次再试
        logger.warning("[event_hub] 汇总落表失败 work=%s n=%d: %s",
                       work_id, len(rows), exc)
        with _buf_lock:
            _buf.setdefault(str(work_id), []).extend(rows)
        return 0


def _take_rows(work_id: str, *, force: bool) -> list[dict[str, Any]]:
    """从缓冲取走本次要落表的行（取走即出缓冲，失败由调用方放回）。"""
    now = time.monotonic()
    with _buf_lock:
        rows = _buf.get(work_id) or []
        if not rows:
            return []
        due = (force
               or len(rows) >= FLUSH_MAX_ROWS
               or now - _last_flush.get(work_id, 0.0) >= FLUSH_MAX_SECONDS)
        if not due:
            return []
        taken = rows[:]
        _buf[work_id] = []
        _last_flush[work_id] = now
        return taken


_DDL = """CREATE TABLE IF NOT EXISTS work_events (
    work_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    type    TEXT NOT NULL,
    run_id  TEXT DEFAULT '',
    round   INTEGER DEFAULT 0,
    ts      TEXT DEFAULT '',
    data    TEXT DEFAULT '{}',
    PRIMARY KEY (work_id, card_id, seq)
)"""


def _insert_rows(conn: Any, rows: list[dict[str, Any]]) -> None:
    conn.execute(_DDL)
    conn.executemany(
        "INSERT OR IGNORE INTO work_events"
        " (work_id, card_id, seq, type, run_id, round, ts, data)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(r["work_id"], r["card_id"], r["seq"], r["type"],
          r["run_id"], r["round"], r["ts"], r["data"]) for r in rows],
    )


async def flush_all() -> int:
    """把全部 work 的缓冲落表（测试 drain / 关停用）。"""
    with _buf_lock:
        works = list(_buf.keys())
    total = 0
    for w in works:
        total += await maybe_flush(w, force=True)
    return total


# ---------------------------------------------------------------- 状态变更
def record_state_change(work_id: str, card_id: str, ev_type: str, *,
                        card_type: str = "", block: str = "",
                        old: Any = None, new: Any = None,
                        by: str = "", cause: str = "",
                        extra: Optional[dict[str, Any]] = None) -> None:
    """记录一次卡面状态变更（四个 store 共用）。

    ``cause`` 区分主动/被动/系统：
      - ``self``   卡自己改自己
      - ``parent`` 父卡/上级改下级（含 curate_face、指令）
      - ``tool``   工具通道改（模型调工具）
      - ``system`` 系统改（归档/恢复/迁移）
    与 ``by``（操作者卡号）配合，才能回答「谁在什么时候改了哪块、从什么变成什么」。
    """
    payload: dict[str, Any] = {
        "card_type": card_type,
        "block": block,
        "by": by,
        "cause": cause,
    }
    if old is not None:
        payload["old"] = old
    if new is not None:
        payload["new"] = new
    if extra:
        payload.update(extra)
    try:
        from ..storage import event_log
        event_log.append_bg(work_id, card_id, {
            "run_id": "", "round": 0, "type": ev_type, "data": payload,
        })
    except Exception as exc:  # noqa: BLE001 状态事件失败不影响写卡
        logger.warning("[event_hub] 状态事件失败 work=%s card=%s type=%s: %s",
                       work_id, card_id, ev_type, exc)


# ---------------------------------------------------------------- 查询
def count_by_card(work_id: str, *, ev_type: str = "") -> list[dict[str, Any]]:
    """按卡 × 类型统计（同步读，一处查询）。"""
    from ..storage import card_db

    def _fn(conn: Any) -> list[dict[str, Any]]:
        try:
            if ev_type:
                rows = conn.execute(
                    "SELECT card_id, type, COUNT(*) AS n FROM work_events"
                    " WHERE work_id=? AND type=? GROUP BY card_id, type"
                    " ORDER BY card_id", (work_id, ev_type)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT card_id, type, COUNT(*) AS n FROM work_events"
                    " WHERE work_id=? GROUP BY card_id, type"
                    " ORDER BY card_id, type", (work_id,)).fetchall()
        except Exception:  # noqa: BLE001 表未建/库不存在 ⇒ 空
            return []
        return [dict(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []


def list_events(work_id: str, card_id: str = "", *, limit: int = 200,
                ev_type: str = "") -> list[dict[str, Any]]:
    """按卡列最近事件（同步读；``card_id`` 空 ⇒ 整个 work）。"""
    from ..storage import card_db

    def _fn(conn: Any) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT * FROM work_events WHERE work_id=?")
            args: list[Any] = [work_id]
            if card_id:
                sql += " AND card_id=?"
                args.append(card_id)
            if ev_type:
                sql += " AND type=?"
                args.append(ev_type)
            sql += " ORDER BY ts DESC LIMIT ?"
            args.append(int(limit))
            rows = conn.execute(sql, args).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [dict(r) for r in rows]

    return card_db.read_sync(work_id, _fn, []) or []
