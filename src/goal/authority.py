"""goal 工具的权威校验 —— 对应 ``tool-goal/src/authority.ts``。

原版三条：
1. ``goalToolExecution``：必须是**当前活跃的驱动回合内**调用（有 open turn）。
2. ``requireDirectHuman``：``create/edit/pause/resume`` 必须来自**直接人类回合**。
3. ``completionAuthority``：``complete/blocked`` 允许**直接人类回合**或**当前那一轮
   goal round**。

映射说明（我们与原版的唯一差别，已在此声明，不藏在代码里）：
原版 ``hasDirectHumanInput`` 还要求 ``ctx.agents.roots().includes(agent)``——
即只有**顶层 agent** 能授予人类权威，子 agent 不行。我们的产品里人可以直接对
任意一张卡说话（画布上每张卡都有输入框），所以这一条映射为"**本回合由人类消息
发起**"，不再限制卡在树里的层级。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from .runtime import GoalError
from .types import GoalMessageSource, GoalView


class GoalToolAuthorityError(GoalError):
    """权威不足（原版 ``HarnessError`` with GOAL_TOOL_* codes）。"""


#: 本回合的来源。``{"kind": "user"}`` / ``{"kind": "goal", "goalId", "revision", "round"}``。
#: 由 ``routes._run_message`` 在回合入口设置；``asyncio.create_task`` 会复制上下文，
#: 所以驱动起的回合里读到的是驱动写的那个来源。
TURN_SOURCE: ContextVar[Optional[dict[str, Any]]] = ContextVar("goal_turn_source", default=None)


def set_turn_source(source: Optional[dict[str, Any]]) -> Any:
    return TURN_SOURCE.set(source)


def reset_turn_source(token: Any) -> None:
    TURN_SOURCE.reset(token)


def current_turn_source() -> Optional[dict[str, Any]]:
    return TURN_SOURCE.get()


def goal_source_of(source: Optional[dict[str, Any]]) -> Optional[GoalMessageSource]:
    """把回合来源里的 goal 段解析成 ``GoalMessageSource``；非 goal 来源返 None。"""
    if not source or source.get("kind") != "goal":
        return None
    return GoalMessageSource(
        goal_id=str(source.get("goalId") or ""),
        revision=int(source.get("revision") or 0),
        round=int(source.get("round") or 0),
    )


def goal_tool_execution(source: Optional[dict[str, Any]]) -> dict[str, Any]:
    """要求"在某个回合内"。没有来源 ⇒ 拒绝（原版 GOAL_TOOL_DRIVER_REQUIRED）。"""
    if source is None:
        raise GoalToolAuthorityError(
            "goal tools require an open model turn", "GOAL_TOOL_DRIVER_REQUIRED")
    return {"source": source}


def has_direct_human_input(source: Optional[dict[str, Any]]) -> bool:
    return bool(source) and source.get("kind") == "user"


def require_direct_human(source: Optional[dict[str, Any]]) -> None:
    if has_direct_human_input(source):
        return
    raise GoalToolAuthorityError(
        "this goal operation requires a direct human turn", "GOAL_TOOL_AUTHORITY_REQUIRED")


def is_matching_goal_round(source: Optional[dict[str, Any]], goal: GoalView) -> bool:
    """本回合是否就是当前 goal 的**那一轮**（原版 ``isMatchingGoalRound``）。

    注意：``roundsStarted`` 在消息被接纳时已推进到本轮，所以比的是
    ``source.round == goal.rounds_started``。
    """
    src = goal_source_of(source)
    if src is None:
        return False
    return (src.goal_id == goal.id and src.revision == goal.revision
            and src.round == goal.rounds_started)


def completion_authority(source: Optional[dict[str, Any]],
                         goal: Optional[GoalView]) -> dict[str, Any]:
    """``complete`` / ``blocked`` 的权威来源（原版 ``completionAuthority``）。"""
    if has_direct_human_input(source):
        return {"kind": "direct-human"}
    if goal is not None and is_matching_goal_round(source, goal):
        return {"kind": "goal-round", "goal": goal}
    raise GoalToolAuthorityError(
        "complete and blocked require a direct human turn or the current goal round",
        "GOAL_TOOL_AUTHORITY_REQUIRED")
