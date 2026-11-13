# -*- coding: utf-8 -*-
"""[对齐场 2026-11-11 · 用户令] 对齐卡（card_type=align）的场态存储。

对齐场＝一次性对话场：根卡拉卡树内 2-3 张卡（它开场说一句即退场）、
或子卡邀卡树内另外 1-2 张卡（发起方先发言），在场内轮流发言对齐消息；
每方各调一次 `align_end` 收场，全员调完场即退役（卡 state=completed），
同样几张卡再对齐须另开新场。

场态存 `cards.align_state`（JSON 列，照 review_state 先例）：

    {
      "topic":        str,            # 对齐话题（建场时定死）
      "participants": [card_id, ...], # 轮转成员（有序；发起方为首=先发言）
      "initiator":    card_id,        # 建场者
      "opener":       card_id | "",   # 开场席：根卡发起时=根卡（非轮转成员，
                                      #   说一次即退场）；子卡发起时=空（发起方
                                      #   自己是轮转首位）
      "ended":        [card_id, ...], # 已调 align_end 的成员（幂等去重）
      "status":       "open"|"closed",
      "turns":        int,            # 已跑的席位回合数
      "max_turns":    int,            # 轮数上限（防失控空烧）
      "created_at":   iso str,
      "closed_at":    iso str,
    }
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from . import card_db, card_store

logger = logging.getLogger(__name__)

ALIGN_TYPE = "align"
DEFAULT_MAX_TURNS = 60


def _now_iso() -> str:
    return card_store._now_iso()


async def init_state(work_id: str, align_id: str, *,
                     topic: str, participants: list[str],
                     initiator: str, opener: str = "",
                     max_turns: int = DEFAULT_MAX_TURNS) -> None:
    """建场即写（幂等——重复 init 不覆盖已有 ended/turns）。"""
    spec = {
        "topic": str(topic),
        "participants": [str(p) for p in participants],
        "initiator": str(initiator),
        "opener": str(opener or ""),
        "ended": [],
        "status": "open",
        "turns": 0,
        "max_turns": int(max_turns or DEFAULT_MAX_TURNS),
        "created_at": _now_iso(),
        "closed_at": "",
    }

    def _fn(conn: Any) -> None:
        row = conn.execute(
            "SELECT align_state FROM cards WHERE card_id=?",
            (align_id,)).fetchone()
        if row and row["align_state"]:
            return  # 已有态——建场只落一次
        conn.execute("UPDATE cards SET align_state=? WHERE card_id=?",
                     (json.dumps(spec, ensure_ascii=False), align_id))

    await card_db.run_write(work_id, _fn)


async def state_of(work_id: str, align_id: str) -> dict[str, Any]:
    """读场态；无列/无值/坏 JSON ⇒ {}。"""
    def _fn(conn: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT align_state FROM cards WHERE card_id=?",
            (align_id,)).fetchone()
        if not row or not row["align_state"]:
            return {}
        try:
            st = json.loads(row["align_state"])
        except (TypeError, ValueError):
            return {}
        return st if isinstance(st, dict) else {}

    try:
        return dict(card_db.read_sync(work_id, _fn, {}) or {})
    except Exception:  # noqa: BLE001
        return {}


async def is_open(work_id: str, align_id: str) -> bool:
    return str((await state_of(work_id, align_id)).get("status") or "") == "open"


# ---------------------------------------------------------------- 入口查询（nav/浮窗）
def _row_state(row: Any) -> dict[str, Any]:
    try:
        st = json.loads(row["align_state"] or "{}")
    except (TypeError, ValueError):
        st = {}
    return st if isinstance(st, dict) else {}


async def list_active(work_id: str) -> list[dict[str, Any]]:
    """本 work 里**开着**的对齐场（左栏临时出入口，同评审 list_active 口径：
    散场即消失，不留常驻入口）。"""
    def _fn(conn: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in conn.execute(
                "SELECT card_id, parent, align_state FROM cards"
                " WHERE work_id=? AND card_type=?", (work_id, ALIGN_TYPE)):
            st = _row_state(r)
            if str(st.get("status") or "") != "open":
                continue
            parts = [str(x) for x in (st.get("participants") or [])]
            ended = [str(x) for x in (st.get("ended") or [])]
            out.append({
                "align_card_id": str(r["card_id"]),
                "parent": str(r["parent"] or ""),
                "topic": str(st.get("topic") or ""),
                "participants": parts,
                "ended": ended,
                "initiator": str(st.get("initiator") or ""),
                "opener": str(st.get("opener") or ""),
                "turns": int(st.get("turns") or 0),
            })
        return out

    try:
        return list(card_db.read_sync(work_id, _fn, []) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[align] list_active 失败 work=%s: %s", work_id, exc)
        return []


async def active_of(work_id: str, member_id: str) -> dict[str, Any]:
    """这张卡当前作为**成员**参与的开着的对齐场（卡侧浮窗判据）。
    没有 ⇒ {}。同卡同时在多场里（理论可能）取先查到的那场。"""
    member_id = str(member_id)
    for it in await list_active(work_id):
        if member_id in (it.get("participants") or []):
            return it
    return {}


async def mark_ended(work_id: str, align_id: str, card_id: str,
                     conclusion: str = "") -> dict[str, Any]:
    """成员宣布说完：幂等并入 ended；可顺手留自己的结论（conclusions）。
    返回新场态。重复调用时新结论覆盖旧结论（后说为准）。"""
    card_id = str(card_id)
    conclusion = str(conclusion or "").strip()

    def _fn(conn: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT align_state FROM cards WHERE card_id=?",
            (align_id,)).fetchone()
        try:
            st = json.loads(row["align_state"]) if row and row["align_state"] else {}
        except (TypeError, ValueError):
            st = {}
        ended = st.setdefault("ended", [])
        if card_id not in ended:
            ended.append(card_id)
        if conclusion:
            st.setdefault("conclusions", {})[card_id] = conclusion
        conn.execute("UPDATE cards SET align_state=? WHERE card_id=?",
                     (json.dumps(st, ensure_ascii=False), align_id))
        return st

    try:
        return dict(await card_db.run_write(work_id, _fn) or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[align] mark_ended 失败 card=%s seat=%s: %s",
                       align_id, card_id, exc)
        return {}


async def bump_turn(work_id: str, align_id: str) -> int:
    """席位回合计数＋1；返回新值。"""
    def _fn(conn: Any) -> int:
        row = conn.execute(
            "SELECT align_state FROM cards WHERE card_id=?",
            (align_id,)).fetchone()
        try:
            st = json.loads(row["align_state"]) if row and row["align_state"] else {}
        except (TypeError, ValueError):
            st = {}
        st["turns"] = int(st.get("turns") or 0) + 1
        conn.execute("UPDATE cards SET align_state=? WHERE card_id=?",
                     (json.dumps(st, ensure_ascii=False), align_id))
        return int(st["turns"])

    try:
        return int(await card_db.run_write(work_id, _fn) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[align] bump_turn 失败 card=%s: %s", align_id, exc)
        return 0


async def claim_conclusion(work_id: str, align_id: str) -> bool:
    """原子占「结论已回投」位：第一个拿到 True 的调用方负责投块，
    其余拿 False——散场三路收口（all_ended/stalled/max_turns）＋
    align_end handler 都会抢着投，靠它保证只投一轮。"""
    def _fn(conn: Any) -> bool:
        row = conn.execute(
            "SELECT align_state FROM cards WHERE card_id=?",
            (align_id,)).fetchone()
        try:
            st = json.loads(row["align_state"]) if row and row["align_state"] else {}
        except (TypeError, ValueError):
            st = {}
        if st.get("conclusion_sent"):
            return False
        st["conclusion_sent"] = True
        conn.execute("UPDATE cards SET align_state=? WHERE card_id=?",
                     (json.dumps(st, ensure_ascii=False), align_id))
        return True

    try:
        return bool(await card_db.run_write(work_id, _fn))
    except Exception:  # noqa: BLE001 占位失败按"没抢到"处理——宁可不投，防重投
        return False


async def close(work_id: str, align_id: str, *, reason: str = "") -> bool:
    """散场：场态置 closed＋卡 state=completed（一次性场退役）。幂等。"""
    st = await state_of(work_id, align_id)
    if not st or str(st.get("status") or "") == "closed":
        return False

    def _fn(conn: Any) -> None:
        st["status"] = "closed"
        st["closed_at"] = _now_iso()
        if reason:
            st["close_reason"] = str(reason)
        conn.execute("UPDATE cards SET align_state=? WHERE card_id=?",
                     (json.dumps(st, ensure_ascii=False), align_id))

    try:
        await card_db.run_write(work_id, _fn)
        await card_store.update_card(
            work_id, align_id, {"state": "completed"},
            f"对齐场散场（{reason or '全员 align_end'}）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[align] 散场写库失败 card=%s: %s", align_id, exc)
        return False
