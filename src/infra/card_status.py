# -*- coding: utf-8 -*-
"""卡的合成状态视图（裁定《机制判断归harness》第 2 步）。

## 为什么有它

card_140/141 复盘的病根：「这张卡什么状态」在系统里有四个互不相干的
答案——生命周期列 ``state``、目标相位 ``goal_state``、进程内 halt 标记、
回合占用。每张门各验一个，停在半状态的卡哪扇都过不去。

本模块不新造第五个字段，而是给**唯一的合成查询点**：四个维度分别从
各自的权威源现推，合成本卡此刻的完整状态。读的人不再挑字段猜。

## 四个维度与各自权威源

| 维度 | 权威源 | 说明 |
|---|---|---|
| ``delivery`` | **事件流派生**（最后一条 ``card_block_edited`` 且 ``block=state`` 的 ``new``；无则 ``active``） | 交付工序位置：active=在岗／completed=已交工 |
| ``halted`` | ``control.is_halted``（halt 事件化的缓存，权威是事件日志） | 被强制停止 |
| ``turn`` | ``session_facade.turn_holder`` | 真实在途回合——「在不在干活」的唯一可信判据 |
| ``goal_phase`` | ``goal.service.get``（异步，仅 ``derive_async`` 提供） | 目标相位（事件投影，非投影列） |

## 合成词（operational）

``running`` 有在途回合 ＞ ``halted`` 被停 ＞ ``completed`` 已交工 ＞ ``idle`` 其余。

``idle`` 且 ``delivery=active`` 即「派了活、没在干、也没被停」——可唤醒；
140/141 死掉的半状态在视图上直接可读：``halted``。

## 与 ``state`` 列的关系

``delivery`` 是从事件推的真值；``state`` 列是手写投影。``reconcile_work``
把两者全库对账——不一致即列漂移（列撒谎的证据）。闸门迁移（第 3 步）
完成前，``state`` 列仍是闸门读的那列；视图只答「真相是什么」。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("infra.card_status")


def _delivery_from_events(work_id: str, card_id: str) -> tuple[str, bool]:
    """交付工序位置：重放该卡事件日志，取最后一条 state 块变更的 new。

    返回 ``(值, 有无事件覆盖)``：``card_block_edited block=state`` 是后来
    才接线的写事件——**早于接线的完工没有事件**（实测 work_027 两张
    10 月底完工的卡，列=completed 而事件面零条）。无覆盖时调用方回落
    ``state`` 列——那列是这段史前史的唯一记录，不能用默认 active 假装
    知道真相。
    """
    from ..storage import event_log as _ev
    path = _ev.event_path(work_id, card_id)
    if not path.is_file():
        return "", False
    last: Optional[str] = None
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                if '"block": "state"' not in ln and '"block":"state"' not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except (ValueError, TypeError):
                    continue
                if rec.get("type") != "card_block_edited":
                    continue
                d = rec.get("data") or {}
                if d.get("block") == "state":
                    last = str(d.get("new") or "")
    except OSError:
        return "", False
    if last:
        return last, True
    return "", False


def derive(work_id: str, card_id: str) -> dict[str, Any]:
    """同步版：交付位置（事件派生）＋ 被停 ＋ 在途回合 ＋ 合成词。

    goal_phase 不进同步版——权威是 goal 服务的异步折叠，闸门同步路径
    不该为它换协程；需要相位用 ``derive_async``。
    """
    from ..api import control
    from ..storage import card_store, session_facade

    delivery, covered = _delivery_from_events(work_id, card_id)
    if not covered:
        meta = card_store._meta_of(work_id, card_id) or {}
        delivery = str(meta.get("state") or "active")   # 史前史：列是唯一记录
    halted = control.is_halted(work_id, card_id)
    holder = session_facade.turn_holder(work_id, card_id) or ""
    if holder:
        op = "running"
    elif halted is not None:
        op = "halted"
    elif delivery == "completed":
        op = "completed"
    else:
        op = "idle"
    return {
        "card_id": card_id,
        "delivery": delivery,
        "delivery_from_events": covered,
        "halted": halted,
        "turn_holder": holder,
        "operational": op,
    }


async def derive_async(work_id: str, card_id: str) -> dict[str, Any]:
    """完整版：再加 goal 相位（goal 服务折叠，权威非投影列）。"""
    out = derive(work_id, card_id)
    try:
        from ..goal.service import get_service
        goal = await get_service().get(work_id, card_id)
        out["goal_phase"] = str(getattr(goal, "phase", "") or "")
        out["goal_id"] = str(getattr(goal, "id", "") or "")
    except Exception:  # noqa: BLE001 无目标／服务异常 ⇒ 相位记空
        out["goal_phase"] = ""
        out["goal_id"] = ""
    return out


def reconcile_work(work_id: str) -> list[dict[str, Any]]:
    """全库对账：事件派生 ``delivery`` vs 手写 ``state`` 列。

    两种结果分开报：
      - ``kind=conflict``：有事件覆盖且与列打架——列在撒谎，真漂移；
      - ``kind=no_event_coverage``：列有值但无事件覆盖——完工早于
        state 事件接线，属遗留缺口（信息性，不是谎言）。
    """
    from ..storage import card_store

    out: list[dict[str, Any]] = []
    try:
        metas = card_store.iter_card_metas(work_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[card_status] 读卡失败 work=%s: %s", work_id, exc)
        return out
    for cid, m in metas.items():
        column = str(m.get("state") or "")
        if not column:
            continue                                   # 非生命周期卡种无此列
        derived, covered = _delivery_from_events(work_id, cid)
        if not covered:
            # 出生默认 active：无变更事件与「从未变过」一致，不算缺口；
            # 只报「列声称非 active、事件却零覆盖」的史前完工类。
            if column != "active":
                out.append({"card_id": cid, "kind": "no_event_coverage",
                            "column": column})
        elif derived != column:
            out.append({"card_id": cid, "kind": "conflict",
                        "derived": derived, "column": column})
    return out
