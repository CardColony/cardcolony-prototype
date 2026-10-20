"""goal 事件流的独立复核 —— 对应 ``goal/src/invariant.ts``。

原版在**每个事件发布之前**用一份独立 fold 复核，违反即 fail-loud。我们没有
事件总线，等价做法是：在每次 ``goal/change`` 提交后（以及测试里），用独立
fold 重放该卡的事件流；任何一条非法变更立刻抛错。

性能：事件文件可能十几万行（实测 card_009 11 万行），所以默认只在
``GOAL_INVARIANT=1`` 时开启；测试与排障时打开。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..storage import event_log
from .fold import empty_goal_fold_state, apply_goal_event

logger = logging.getLogger(__name__)


class GoalInvariantFailure(Exception):
    """事件流违反 goal 域不变量。"""


def enabled() -> bool:
    return os.environ.get("GOAL_INVARIANT", "0") == "1"


def verify_events(events: list[dict[str, Any]], label: str = "") -> None:
    """独立重放一遍；非法即抛 ``GoalInvariantFailure``（原版 fail-loud）。"""
    state = empty_goal_fold_state()
    for ev in events:
        try:
            apply_goal_event(state, ev)
        except Exception as exc:  # noqa: BLE001 转成不变量失败
            raise GoalInvariantFailure(
                f"session event {ev.get('seq')} violates the durable goal stream: {exc} {label}"
            ) from exc


def verify_card(work_id: str, card_id: str) -> None:
    """复核一张卡（仅在 ``GOAL_INVARIANT=1`` 时真正执行）。"""
    if not enabled():
        return
    try:
        events = event_log.read(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 读不到不阻断
        logger.warning("[goal-invariant] 读取事件失败 card=%s: %s", card_id, exc)
        return
    verify_events(events, label=f"card={card_id}")
