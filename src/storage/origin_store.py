# -*- coding: utf-8 -*-
"""原文取数（照抄 dsh `gen3/platform/origin-contract.ts` ＋ `origin-selfread.ts`）。

## dsh 的形状（原样）

```ts
interface OriginEvent { seq: number; type: string; time?: number; data?: unknown;
                        surfaceOp?: unknown; sourceEventSeqs?: number[] }
interface OriginSpan  { events: OriginEvent[]; from: number; throughSeq: number | null }
readOriginSpan(sid, fromSeq, { throughSeq? })   // 半开起点（含）、按 seq 升序
```

## 本项目的映射

dsh 的"原文"是 **session 事件流**；本项目的原文是 **会话块**（`session_facade` 落的块）。
⇒ 一条块 = 一个 `OriginEvent`：

| OriginEvent | 本项目 |
|---|---|
| `seq` | 块的 `seq`（块序号，与凝练水位 `mark` 同尺） |
| `type` | 块的事件型（`user_message` / `assistant_text` / `tool_result` / `tool_call` / 空串） |
| `time` | 块的 `ts`（ISO 串 → epoch ms） |
| `data` | **整块原样**（actor/turn/pair/model/behavior/end/content/tool_calls/tool_call_id/…） |
| `surface_op` | 预留（当前恒 None，与 dsh 同字段位） |
| `source_event_seqs` | 预留（当前恒 None；dsh 用它记"本事件由哪些事件产生"） |

## 四条纪律（照抄 dsh 契约，逐字对应）

1. **`from_seq` 必填、禁全量**（dsh 原注：主会话 623 万行）。
2. **不做缓存**——每次真读；缓存会引入两份数据不一致。
3. **不做过滤、不做口径换算**（`estimateTokens` 之类归消费者）。
4. **起点 = 消费者自带的 mark**（凝练侧 `condenseMark`）——本模块**不实现任何 mark 逻辑**，
   只负责"从某个 seq 起把原文取出来"。

## 与 md 的关系

md 仍是当前写路径的真相源；本模块的库表是**同一批块的第二份可查副本**（双写）。
⇒ 追溯（记忆条目 → 原文）走库；md 保留原样，供人读与既有读路径使用。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from . import card_db

logger = logging.getLogger("storage.origin")


# ===========================================================================
# 事件型映射（块 → 事件 type）
# ===========================================================================

def event_type_of(block: dict[str, Any]) -> str:
    """块 → 事件型。与 `session_facade._block_event_type` 同口径，但**不吞工具块**：

    - `actor=human` → `user_message`
    - `actor=assistant` 带 `tool_calls` → `tool_call`（dsh 的 `tool/call`）
    - `actor=assistant` → `assistant_text`
    - `actor=tool` → `tool_result`（dsh 的 `tool/result`）
    - 其余 → 空串

    注意：`session_facade._block_event_type` 对工具块返回空串是**为了不发事件**（避免与
    过程通道双份）；那是"事件通道"的口径。**原文存的是块**，工具块必须留型，否则追溯
    时看不出"这一条是工具调用/结果"。两处口径不同是有意的，不要合并。
    """
    actor = str(block.get("actor") or "")
    if actor == "human":
        return "user_message"
    if actor == "assistant":
        return "tool_call" if block.get("tool_calls") else "assistant_text"
    if actor == "tool":
        return "tool_result"
    return ""


def _to_epoch_ms(ts: Any) -> Optional[int]:
    """ISO 时间串 → epoch ms；解析不了返回 None（不抛——原文取数不该因时间坏掉而失败）。"""
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except (ValueError, TypeError, OSError):
        return None


# ===========================================================================
# 写：块 → 原文表（由 session_facade 在块落盘后调用）
# ===========================================================================

def _row_of(card_id: str, block: dict[str, Any]) -> tuple:
    seq = int(block.get("seq") or 0)
    payload = {k: v for k, v in block.items() if k not in ("seq", "content")}
    return (
        card_id,
        seq,
        event_type_of(block),
        _to_epoch_ms(block.get("ts")),
        str(block.get("content") or ""),
        json.dumps(payload, ensure_ascii=False),
    )


def write_blocks_sync(work_id: str, card_id: str,
                      blocks: list[dict[str, Any]]) -> int:
    """把一批块写进原文表（幂等：同 `(card_id, seq)` 覆盖）。

    由 `session_facade` 在**md 落盘成功之后**调用（md 是写路径的真相源，库是副本）。
    返回写入行数；失败只记日志、**不抛**——库写失败不得让一轮对话失败
    （与 dsh「原文取数不阻断业务」同精神；缺行由 `backfill` 补）。
    """
    if not blocks:
        return 0
    rows = [_row_of(card_id, b) for b in blocks]
    sql = ("INSERT OR REPLACE INTO session_blocks "
           "(card_id, seq, type, time, content, data) VALUES (?,?,?,?,?,?)")

    def _fn(conn: Any) -> int:
        conn.executemany(sql, rows)
        return len(rows)

    try:
        # 写路径必须走 card_db 的**写**通道：`read_sync` 会拒绝写操作
        # （"只读路径出现写操作（run_read 的 fn 必须纯查询）"）。
        return int(card_db._write_sync(work_id, _fn) or 0)  # noqa: SLF001 同包内复用写事务边界
    except Exception as exc:  # noqa: BLE001 库写失败不阻断业务
        logger.warning("[origin] 原文入库失败（md 已落，待 backfill）card=%s n=%d: %s",
                       card_id, len(rows), exc)
        return 0


async def write_blocks(work_id: str, card_id: str,
                       blocks: list[dict[str, Any]]) -> int:
    """异步薄包装（库操作走线程池）。"""
    import asyncio
    return await asyncio.to_thread(write_blocks_sync, work_id, card_id, blocks)


# ===========================================================================
# 读：原文区间（dsh `readOriginSpan` 的同构实现）
# ===========================================================================

def read_origin_span(work_id: str, card_id: str, from_seq: int,
                     through_seq: Optional[int] = None) -> dict[str, Any]:
    """读一段原文。**半开起点（`from_seq` 含）、按 seq 升序、不加工。**

    - `from_seq` **必填**（禁全量）；
    - `through_seq` 给定时为**闭区间上界**（含）；不给则读到末尾；
    - 返回 `{"events": [...], "from": from_seq, "throughSeq": 末条 seq 或 None}`，
      形状与 dsh `OriginSpan` 一致（键名按 Python 习惯，保留 `throughSeq` 原名便于对照）。

    取不到（库不存在/未迁移）→ `events=[]`、`throughSeq=None`，不抛。
    """
    start = int(from_seq or 0)

    def _fn(conn: Any) -> list[dict[str, Any]]:
        if through_seq is None:
            cur = conn.execute(
                "SELECT seq, type, time, content, data FROM session_blocks "
                "WHERE card_id = ? AND seq >= ? ORDER BY seq",
                (card_id, start))
        else:
            cur = conn.execute(
                "SELECT seq, type, time, content, data FROM session_blocks "
                "WHERE card_id = ? AND seq >= ? AND seq <= ? ORDER BY seq",
                (card_id, start, int(through_seq)))
        return [dict(r) for r in cur.fetchall()]

    try:
        rows = card_db.read_sync(work_id, _fn, []) or []
    except Exception as exc:  # noqa: BLE001 读不到＝没有（不阻断）
        logger.warning("[origin] 原文取数失败（视同空）card=%s from=%s: %s",
                       card_id, start, exc)
        rows = []

    events: list[dict[str, Any]] = []
    for r in rows:
        ev: dict[str, Any] = {"seq": int(r["seq"]), "type": str(r.get("type") or "")}
        if r.get("time") is not None:
            ev["time"] = int(r["time"])
        if r.get("content"):
            ev["content"] = r["content"]
        raw = r.get("data") or ""
        if raw:
            try:
                ev["data"] = json.loads(raw)
            except (ValueError, TypeError):
                # 与 dsh 一致：解析失败显式记日志，但**不丢 seq/type**（原文取数要能继续）
                logger.warning("[origin] data 列解析失败 seq=%s card=%s", r.get("seq"), card_id)
        events.append(ev)

    return {
        "events": events,
        "from": start,
        "throughSeq": events[-1]["seq"] if events else None,
    }


def trace_entry(work_id: str, card_id: str, seq_from: int,
                seq_to: Optional[int] = None) -> dict[str, Any]:
    """**追溯**：给记忆条目的覆盖范围，取回它压自的原文块。

    这是"记忆 → 原文"的那一条链：`条目.seq_from/seq_to` → `read_origin_span`。
    范围缺 `seq_to` 时按"到末尾"读（与凝练窗口同口径）。
    """
    return read_origin_span(work_id, card_id, seq_from, seq_to)


# ===========================================================================
# 补齐：把 md 里已有、库里没有的块补进来（存量会话追溯）
# ===========================================================================

def existing_seqs(work_id: str, card_id: str) -> set[int]:
    """库里已有的块序号集合（供 backfill 判断缺哪些）。"""
    def _fn(conn: Any) -> list[int]:
        return [int(r["seq"]) for r in conn.execute(
            "SELECT seq FROM session_blocks WHERE card_id = ? ORDER BY seq", (card_id,))]

    try:
        return set(card_db.read_sync(work_id, _fn, []) or [])
    except Exception:  # noqa: BLE001
        return set()
