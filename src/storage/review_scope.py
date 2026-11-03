# -*- coding: utf-8 -*-
"""评审流量的上下文治理（2026-11-02 · 用户裁定）。

裁定：评审过程消息是**过程噪音，不需要记忆**——评审的持久记录在
``review_store``（批次/决议/成员/稿次）和 origin 原文（可审计），会话里那份
派发快照/发言/催办在批次过去后一文不值。实案：card_154 被 card_155 的派单
快照灌了 142 万 token（每块 ~11 万，14 份同文档复印件），撞穿 1M 窗口；
同生态的 160/158/156/167 同病。凝练它们既烧调用又把记忆链灌满一次性噪音。

所以评审流量按「coder 卡过程噪音」同一原则治理：**不进凝练池、不进历史投影**
——新鲜时在场（被评卡靠当前派单干活），被更新的流量取代即蒸发。

机械判据：
- 块的 ``actor=card:<id>`` 且该卡 ``card_type='review'`` ⇒ 评审流量块；
- 本卡自己是评审型卡 ⇒ 全流过程化（评审卡的会话整体是过程）。

规则：
- 非评审卡：评审流量块只保留最新 ``REVIEW_CTX_KEEP``（env，缺省 3）条——
  当前批次＋近两轮在场，更早的直接判不进（投影/凝练两个池子同一份名单）。
  **不加体积闸**（2026-11-02 用户裁定）：巨块截断/蒸发会把上游失控盖住——
  病态该顶穿窗口报警，问题修在源头（``_batch_transcript`` 按席收窄）；
- 评审型卡：全流只保留最近 ``REVIEW_SELF_CTX_SEQ``（env，缺省 150）个 seq，
  且 ``condense_turn_end`` 对评审卡直接豁免。

判据收在本模块一处：``memory_store._read_origin``（凝练计量/窗口/排水同口径）、
``context_regions`` 历史投影、``card_draft`` 起草投影三处消费方共用。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_REVIEW_TYPE = "review"  # 对齐 review_store.REVIEW_TYPE（不 import：防 storage 内环）

#: 评审型卡 id 集合缓存：work_id → (expires_ts, ids)。评审卡是派生卡，
#: 新评审开场才增员——TTL 60s 足够跟得上。
_ids_cache: dict[str, tuple[float, frozenset[str]]] = {}
_IDS_TTL_SEC = 60.0


def review_card_ids(work_id: str) -> frozenset[str]:
    """该 work 里 ``card_type='review'`` 的卡 id 集合（60s 缓存；查库失败保守给空）。"""
    now = time.time()
    hit = _ids_cache.get(work_id)
    if hit and hit[0] > now:
        return hit[1]
    ids: frozenset[str] = frozenset()
    try:
        from . import card_db

        def _fn(conn: Any) -> list[str]:
            return [str(r["card_id"]) for r in conn.execute(
                "SELECT card_id FROM cards WHERE work_id=? AND card_type=?",
                (work_id, _REVIEW_TYPE))]

        rows = card_db.read_sync(work_id, _fn, [])
        ids = frozenset(x for x in (rows or []) if x)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review_scope] 评审卡集合查询失败（保守放行）work=%s: %s",
                       work_id, exc)
    _ids_cache[work_id] = (now + _IDS_TTL_SEC, ids)
    return ids


def is_review_card(work_id: str, card_id: str) -> bool:
    """本卡是不是评审型卡。"""
    return str(card_id) in review_card_ids(work_id)


def invalidate_cache(work_id: str | None = None) -> None:
    """评审卡集合缓存失效：新建/归档评审卡后调用（不给 work_id 则全清）。"""
    if work_id is None:
        _ids_cache.clear()
    else:
        _ids_cache.pop(work_id, None)


def _keep_n() -> int:
    try:
        return max(1, int(os.environ.get("REVIEW_CTX_KEEP", "3") or 3))
    except ValueError:
        return 3


def _self_seq_span() -> int:
    try:
        return max(1, int(os.environ.get("REVIEW_SELF_CTX_SEQ", "150") or 150))
    except ValueError:
        return 150


def _is_review_block(block: dict[str, Any], ids: frozenset[str]) -> bool:
    actor = str(block.get("actor") or "")
    return actor.startswith("card:") and actor[5:] in ids



def drop_stale_review(work_id: str, card_id: str,
                      blocks: list[dict[str, Any]],
                      keep: int | None = None) -> list[dict[str, Any]]:
    """评审流量治理过滤：返回「该留在上下文/凝练池里的」块列表（原列表不改）。

    ``keep`` 两个口径：
    - ``None``（缺省）＝**上下文口径**：评审流量保留最新 ``_keep_n()`` 条
      （当前批次在场，被评卡靠它干活；更早的蒸发）；
    - ``0``＝**凝练口径**：评审流量一条不留——评审内容不需要记忆，
      持久记录归 review_store（凝练侧 ``_read_origin`` 恒用此口径）。

    - 本卡是评审型卡 ⇒ 全流过程化：上下文口径只留最近 ``_self_seq_span()``
      个 seq；凝练口径全部蒸发（评审卡永不进凝练池）。
    原文永远在 origin_store，这里只管「进不进模型视野/凝练池」。
    """
    if not blocks:
        return blocks
    ids = review_card_ids(work_id)
    if is_review_card(work_id, card_id):
        if keep == 0:
            return []
        # 评审卡全流过程化：seq 窗口截尾。
        try:
            floor = int(blocks[-1].get("seq") or 0) - _self_seq_span()
        except (TypeError, ValueError):
            return blocks
        return [b for b in blocks
                if int(b.get("seq") or 0) > floor]
    if not ids:
        return blocks
    k = _keep_n() if keep is None else keep
    if k <= 0:
        return [b for b in blocks if not _is_review_block(b, ids)]
    rev_pos = [i for i, b in enumerate(blocks) if _is_review_block(b, ids)]
    if len(rev_pos) <= k:
        return blocks
    drop = set(rev_pos[:-k])
    return [b for i, b in enumerate(blocks) if i not in drop]
