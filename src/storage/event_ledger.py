# -*- coding: utf-8 -*-
"""事件台账（照抄 dsh `gen3/condense/event-ledger.ts` 的机械层：E1–E4）。

**纯函数、零 LLM、零 domain IO**——与 `memory_domain.check_invariants`（I1–I5）同一哲学：
**I1–I5 管条目空间，E1–E4 管事件空间**，互不覆盖。

## 四件（dsh 逐条）

- `new_event_id()`：`"evt-" + 21 位 nanoid 同格式`（dsh 用 `node:crypto` 实现，零新增依赖；
  这里用 `secrets`，同样零新增依赖）。
- `ensure_misc_event()`：幂等建 `evt-misc`「杂项」保留行（title 是常量、summary 空串——
  **机械层永不产摘要文本**）。
- `validate_event_invariants(chains, events, …)`：
  - **E1 悬空 eventRef**：节点的 `eventRef` 指向不存在的事件（同批立案台账豁免；**进违例清单**）
  - **E2 孤儿**：`open/active` 且**零成员**且超 N 天 ⇒ 告警（**不自动删**；`evt-misc` 豁免）
  - **E3 指向 closed**：新节点引用 closed 事件 ⇒ **不拒绝**，产出 `reopen` 路由指令
  - **E4 同指纹多 open**：`subject + objectKey` 相同且多条 `open` ⇒ 告警
- 边界（dsh 原注）：**E1 进违例清单（拒写口径）；E2–E4 只告警、不拒写。**

## 本项目映射

| dsh | 本项目 |
|---|---|
| `chains: CausalNode[][]` | 各卡的因果链（`memory_domain.load_chain`） |
| `events: EventEntry[]` | `events` 表行（`card_db` 新表） |
| `n.eventRef` | 节点上的 `eventRef` 字段（已在 `CausalNode` 模型里） |
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from typing import Any, Iterable, Optional

logger = logging.getLogger("storage.event_ledger")

#: 杂项保留事件（常量钉死；E2 孤儿判定豁免）
MISC_EVENT_ID = "evt-misc"
MISC_EVENT_TITLE = "杂项"

#: E2 孤儿告警天数缺省（dsh：N=配置，默认 7 天；文件级 const）
EVENT_ORPHAN_DAYS_DEFAULT = 7

#: nanoid 缺省字母表/长度（dsh 同值）
_NANO_ALPHABET = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLFGQZbfghjklqvwyzrict"
_NANO_LEN = 21

#: 事件状态（dsh 同集合）
STATUSES: tuple[str, ...] = ("open", "active", "closed")


def new_event_id() -> str:
    """`"evt-" + 21 位`（dsh nanoid 同格式；`secrets` 实现，零新增依赖）。"""
    return "evt-" + "".join(secrets.choice(_NANO_ALPHABET) for _ in range(_NANO_LEN))


def is_misc_event_id(event_id: Any) -> bool:
    return event_id == MISC_EVENT_ID


def _ms(now: Optional[int]) -> int:
    return int(now) if now is not None else int(datetime.now().timestamp() * 1000)


# ---------------------------------------------------------------------------
# 落库（events 表）
# ---------------------------------------------------------------------------

def ensure_misc_event(work_id: str, card_id: str) -> bool:
    """幂等建 `evt-misc` 保留行（已存在不重复建）。失败只记日志。"""
    from . import card_db
    now = _ms(None)

    def _fn(conn: Any) -> int:
        row = conn.execute(
            "SELECT event_id FROM events WHERE card_id = ? AND event_id = ?",
            (card_id, MISC_EVENT_ID)).fetchone()
        if row is not None:
            return 0
        conn.execute(
            "INSERT INTO events (card_id, event_id, title, status, summary, subject, "
            "object_key, seq_from, seq_to, salience, confidence, version_no, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (card_id, MISC_EVENT_ID, MISC_EVENT_TITLE, "open", "", "system", "misc",
             0, 0, 0, "high", 1, now, now))
        return 1

    try:
        return bool(card_db._write_sync(work_id, _fn))  # noqa: SLF001 同包写事务边界
    except Exception as exc:  # noqa: BLE001
        logger.warning("[event_ledger] 建 misc 事件失败 card=%s: %s", card_id, exc)
        return False


def load_events(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """读该卡全部事件行（读不到＝空表，不抛）。"""
    from . import card_db

    def _fn(conn: Any) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT event_id, title, status, summary, subject, object_key, seq_from, "
            "seq_to, salience, confidence, version_no, created_at, updated_at "
            "FROM events WHERE card_id = ? ORDER BY created_at", (card_id,)).fetchall()
        return [dict(r) for r in rows]

    try:
        return card_db.read_sync(work_id, _fn, []) or []
    except Exception:  # noqa: BLE001
        return []


def save_events(work_id: str, card_id: str, events: Iterable[dict[str, Any]]) -> int:
    """upsert 一批事件（幂等）。失败只记日志、不抛。"""
    from . import card_db
    now = _ms(None)
    rows = []
    for e in events or []:
        if e.get("event_id") == MISC_EVENT_ID:
            continue  # misc 由 ensure_misc_event 管
        rows.append((
            card_id, str(e["event_id"]), str(e.get("title") or ""),
            str(e.get("status") or "open"), str(e.get("summary") or ""),
            str(e.get("subject") or ""), str(e.get("object_key") or ""),
            int(e.get("seq_from") or 0), int(e.get("seq_to") or 0),
            int(e.get("salience") or 0), str(e.get("confidence") or "high"),
            int(e.get("version_no") or 1), now, now))
    if not rows:
        return 0

    def _fn(conn: Any) -> int:
        conn.executemany(
            "INSERT INTO events (card_id, event_id, title, status, summary, subject, "
            "object_key, seq_from, seq_to, salience, confidence, version_no, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(card_id, event_id) DO UPDATE SET "
            "title=excluded.title, status=excluded.status, summary=excluded.summary, "
            "subject=excluded.subject, object_key=excluded.object_key, "
            "seq_from=excluded.seq_from, seq_to=excluded.seq_to, "
            "salience=excluded.salience, confidence=excluded.confidence, "
            "version_no=events.version_no + 1, updated_at=excluded.updated_at",
            rows)
        return len(rows)

    try:
        return int(card_db._write_sync(work_id, _fn) or 0)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("[event_ledger] 事件落库失败 card=%s n=%d: %s", card_id, len(rows), exc)
        return 0


# ---------------------------------------------------------------------------
# E1–E4
# ---------------------------------------------------------------------------

def validate_event_invariants(
    chains: list[list[dict[str, Any]]],
    events: list[dict[str, Any]],
    *,
    now: Optional[int] = None,
    orphan_days: int = EVENT_ORPHAN_DAYS_DEFAULT,
    new_nodes: Optional[list[dict[str, Any]]] = None,
    same_batch_event_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """E1–E4 校验（纯函数：链/事件快照入参，零 IO、零 LLM）。

    返回：`{"violations": [E1 文本], "e1Dangling": [...], "e2Orphans": [...],
            "e3ReopenIds": [...], "e4Dupes": [...]}`

    - `new_nodes` 给定时 E1/E3 按**批提交语义**（只看新节点）；缺省时 E1 按**全量语义**。
    - `same_batch_event_ids`＝同批立案台账（E1 豁免集：本 put 内先写后引不算悬空）。
    """
    now_ms = _ms(now)
    event_ids = {str(e.get("event_id")) for e in events or []}
    ledger = same_batch_event_ids or set()

    flat: list[dict[str, Any]] = [n for chain in chains or [] for n in chain or []]

    # ── E1：eventRef 悬空（新节点优先口径；否则全量口径）──
    e1_source = new_nodes if new_nodes is not None else flat
    e1_dangling: list[dict[str, Any]] = []
    violations: list[str] = []
    for n in e1_source:
        ref = n.get("eventRef")
        if not isinstance(ref, str) or ref == "":
            continue
        if ref not in event_ids and ref not in ledger:
            seq = int((n.get("anchor") or {}).get("seq", -1))
            e1_dangling.append({"nodeSeq": seq, "eventRef": ref})
            violations.append(f"E1: 节点 seq={seq} 的 eventRef={ref} 悬空（事件不存在）")

    # ── E2：open/active 且零成员且超 N 天 ──
    member: set[str] = set()
    for n in flat:
        ref = n.get("eventRef")
        if isinstance(ref, str) and ref:
            member.add(ref)
    for n in (new_nodes or []):
        ref = n.get("eventRef")
        if isinstance(ref, str) and ref:
            member.add(ref)
    e2_orphans: list[dict[str, Any]] = []
    for e in events or []:
        eid = str(e.get("event_id"))
        if eid == MISC_EVENT_ID:
            continue                                  # misc 常驻，不参与孤儿判定
        if str(e.get("status")) not in ("open", "active"):
            continue
        if eid in member:
            continue
        ts = e.get("updated_at") or e.get("created_at") or 0
        age_days = (now_ms - int(ts)) / 86_400_000
        if age_days > orphan_days:
            e2_orphans.append({"eventId": eid, "ageDays": int(age_days)})

    # ── E3：新节点指向 closed 事件（不拒绝；产出 reopen 指令）──
    e3_reopen: list[str] = []
    if new_nodes is not None:
        by_id = {str(e.get("event_id")): e for e in events or []}
        for n in new_nodes:
            ref = n.get("eventRef")
            if not isinstance(ref, str) or ref == "":
                continue
            hit = by_id.get(ref)
            if hit is not None and str(hit.get("status")) == "closed" and ref not in e3_reopen:
                e3_reopen.append(ref)

    # ── E4：同指纹（subject + objectKey）多条 open ──
    groups: dict[str, list[str]] = {}
    for e in events or []:
        if str(e.get("status")) != "open":
            continue
        eid = str(e.get("event_id"))
        if eid == MISC_EVENT_ID:
            continue
        fp = f"{e.get('subject') or ''}\u0000{e.get('object_key') or ''}"
        groups.setdefault(fp, []).append(eid)
    e4_dupes = [{"fingerprint": fp.replace("\u0000", "|"), "eventIds": ids}
                for fp, ids in groups.items() if len(ids) > 1]

    return {
        "violations": violations,
        "e1Dangling": e1_dangling,
        "e2Orphans": e2_orphans,
        "e3ReopenIds": e3_reopen,
        "e4Dupes": e4_dupes,
    }
