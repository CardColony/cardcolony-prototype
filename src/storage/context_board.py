# -*- coding: utf-8 -*-
"""上下文缓存板（`context_board` 表）——「哪些历史在模型视野内」的物化答案。

[缓存板 2026-11-12] 对齐 dsh `projection-board.ts` 的**写时维护**思想：
板不是每轮现算的缓存，是「视野」这个问题的**唯一答案落点**——
装配层读板做机械投影，不再从全量原文 + 四层筛子现推。

## 板上是什么

**已过闸的可见块**（数据视图，不是渲染结果）：
- `enters_context` 为真——工具结果/调用壳/环节留痕**永不进板**；
- `seq >= 凝练 mark`——mark 推进**同事务**裁板，不可能错位（seq 比较，没有下标）；
- 评审流量 `is_review=1` 按 keep-N **写时逐出**，板上恒 ≤N 条。

## 为什么不再现算

旧路径「全量读→drop_stale_review→mark 切片→enters_context→heal」
四层筛子摞在读侧：mark 按下标切、先缩表再切 → 越界静默回退全量
（card_021 实案：mark=20236、过滤后 20215 → 2828 条全量上桌）。
写时维护后读侧**没有筛选动作**，这类 bug 在结构上不存在。

## 真相与自愈

`session_blocks`（原文账）永远是唯一事实，板是它的衍生视图——
钩子漏写/字段演进导致漂移 ⇒ ``rebuild_board`` 一键重算（幂等）。
懒确保：板空而原文有货 ⇒ 读侧就地重建一次（带 60s 节流，防全过滤卡空转）。

渲染语义变更（区头/映射规则）**不需要**迁移——板存块不存渲染物，
投影永远在装配层发生。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from . import card_db, predicates, review_scope

logger = logging.getLogger("storage.context_board")

#: 读侧开关：CONTEXT_BOARD=0 → 装配回退旧四层筛（回退通道，排障用）。
def enabled() -> bool:
    return os.environ.get("CONTEXT_BOARD", "1").strip() != "0"


# ---------------------------------------------------------------------------
# 写侧钩子（都在既有写事务内调用——同事务落账，板与原文/mark 永不脱节）
# ---------------------------------------------------------------------------

def upsert_blocks_tx(conn: Any, work_id: str, card_id: str,
                     blocks: list[dict[str, Any]]) -> int:
    """会话块落库**同事务**的进板钩（origin_store.write_blocks_sync 内调）。

    过闸 `enters_context` 才进板；幂等（PRIMARY KEY card_id+seq → OR REPLACE）。
    逐出规则同事务执行：
    - 本卡是评审型卡 ⇒ 每次追加按 seq 窗截尾（对齐 drop_stale_review 的
      `seq > 最新seq - span` 口径）；
    - 进板的是评审流量块 ⇒ keep-N 逐出（板上评审恒 ≤N 条）。
    """
    if not blocks:
        return 0
    ids = review_scope.review_card_ids(work_id)
    self_review = review_scope.is_review_card(work_id, card_id)
    rows: list[tuple] = []
    saw_review = False
    max_seq = -1
    for b in blocks:
        if not predicates.enters_context(b):
            continue
        seq = int(b.get("seq") or 0)
        is_rev = 1 if review_scope._is_review_block(b, ids) else 0
        saw_review = saw_review or bool(is_rev)
        max_seq = max(max_seq, seq)
        rows.append((
            card_id, seq,
            str(b.get("role") or ""), str(b.get("actor") or ""),
            str(b.get("content") or ""),
            str(b.get("reasoning_content") or "") or None,
            str(b.get("pair_id") or b.get("pair") or "") or None,
            is_rev,
        ))
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO context_board "
        "(card_id, seq, role, actor, content, reasoning_content, pair_id, is_review) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    if self_review:
        # 评审卡全流过程化：只留最近 span 个 seq（与读侧蒸发同口径）。
        floor = max_seq - review_scope._self_seq_span()
        conn.execute(
            "DELETE FROM context_board WHERE card_id=? AND seq<=?",
            (card_id, floor))
    elif saw_review:
        keep = review_scope._keep_n()
        conn.execute(
            "DELETE FROM context_board WHERE card_id=? AND is_review=1 "
            "AND seq NOT IN (SELECT seq FROM context_board "
            "              WHERE card_id=? AND is_review=1 "
            "              ORDER BY seq DESC LIMIT ?)",
            (card_id, card_id, keep))
    return len(rows)


def trim_below_tx(conn: Any, card_id: str, mark: int) -> int:
    """凝练 mark 推进**同事务**的裁板钩：板上不许有 seq < mark 的行。

    seq 比较（不是下标）——mark 语义错位的 bug 类在这里不可能存在。
    """
    cur = conn.execute(
        "DELETE FROM context_board WHERE card_id=? AND seq<?",
        (card_id, int(mark or 0)))
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# 读侧：读板（机械投影的原料），含懒重建自愈
# ---------------------------------------------------------------------------

def read_board(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """读板：按 seq 升序返回**块形态** dict（投影层复用同一套渲染）。

    先查新鲜度（板空+原文有货 / 板落后于原文 ⇒ 就地重建），再取行——
    既有卡免回填脚本自愈，钩子失败的漂移也在 60s 内补回。
    """
    _maybe_rebuild(work_id, card_id)
    return _select_board(work_id, card_id)


def _select_board(work_id: str, card_id: str) -> list[dict[str, Any]]:
    def _fn(conn: Any) -> list[dict[str, Any]]:
        return [{
            "seq": int(r["seq"]), "role": r["role"] or "",
            "actor": r["actor"] or "", "content": r["content"] or "",
            "reasoning_content": r["reasoning_content"],
            "pair_id": r["pair_id"], "is_review": int(r["is_review"] or 0),
        } for r in conn.execute(
            "SELECT seq, role, actor, content, reasoning_content, pair_id, is_review "
            "FROM context_board WHERE card_id=? ORDER BY seq", (card_id,))]
    try:
        return card_db.read_sync(work_id, _fn, []) or []
    except Exception:  # noqa: BLE001 表未建/库不在 → 空板，绝不阻断装配
        return []


#: 懒重建节流：board 合法为空（全过滤卡）时防每轮白扫——60s 内同卡只试一次。
_REBUILD_TRIED: dict[tuple[str, str], float] = {}


def _maybe_rebuild(work_id: str, card_id: str) -> None:
    key = (work_id, card_id)
    last = _REBUILD_TRIED.get(key, 0.0)
    if time.time() - last < 60:
        return

    def _state(conn: Any) -> tuple:
        bmax = conn.execute(
            "SELECT MAX(seq) FROM context_board WHERE card_id=?",
            (card_id,)).fetchone()[0]
        smax = conn.execute(
            "SELECT MAX(seq) FROM session_blocks WHERE card_id=?",
            (card_id,)).fetchone()[0]
        return bmax, smax
    try:
        bmax, smax = card_db.read_sync(work_id, _state, (None, None))
    except Exception:  # noqa: BLE001
        return
    if smax is None:
        return  # 原文无货，板空是合法态
    # 板落后原文 ⇒ 有新块没进板（钩子失败/部署前漂移）。尾部块可能全被
    # 过滤 ⇒ 重建零写入——一次 tail 读的成本，60s 节流兜底。
    if bmax is not None and int(bmax) >= int(smax):
        return
    _REBUILD_TRIED[key] = time.time()
    rebuild_board(work_id, card_id)


def rebuild_board(work_id: str, card_id: str) -> int:
    """整份重建：删板 → 从原文账按 mark/enters_context/评审政策重算 → 进板。

    幂等、可重跑；原文账（session_blocks）是唯一事实，板随时可由它派生。
    """
    try:
        from . import memory_store, origin_store  # 延迟：避免 storage 内部循环
        st = memory_store.load_memory(work_id, card_id)
        mark = int((st or {}).get("mark", 0) or 0)
        blocks = origin_store.read_blocks(work_id, card_id, mark)
        rows = [b for b in blocks if predicates.enters_context(b)]
        rows = review_scope.drop_stale_review(work_id, card_id, rows)
    except Exception as exc:  # noqa: BLE001 取数失败不破坏旧板
        logger.warning("[board] 重建取数失败（保留旧板）card=%s: %s", card_id, exc)
        return 0

    def _fn(conn: Any) -> int:
        conn.execute("DELETE FROM context_board WHERE card_id=?", (card_id,))
        return upsert_blocks_tx(conn, work_id, card_id, rows)

    try:
        n = int(card_db._write_sync(work_id, _fn) or 0)  # noqa: SLF001 同包写事务边界
    except Exception as exc:  # noqa: BLE001
        logger.error("[board] 重建落库失败 card=%s: %s", card_id, exc)
        return 0
    _REBUILD_TRIED[(work_id, card_id)] = time.time()
    logger.info("[board] 重建完成 card=%s mark=%s 行数=%s", card_id, mark, n)
    return n


def board_count(work_id: str, card_id: str) -> int:
    """板行数（观测/排障用）。"""
    def _fn(conn: Any) -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM context_board WHERE card_id=?",
            (card_id,)).fetchone()[0])
    try:
        return int(card_db.read_sync(work_id, _fn, 0) or 0)
    except Exception:  # noqa: BLE001
        return 0
