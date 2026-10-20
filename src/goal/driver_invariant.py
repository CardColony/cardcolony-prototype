"""续轮提示词的独立复核 —— 对应 ``goal-round-driver/src/invariant.ts``。

原版规则：一条被接纳的 goal 轮次消息，其内容必须与 ``renderGoalRoundPrompt``
逐字一致（用前置事件重放出当轮的 goal 视图再比对）。这样"提示词被谁偷偷改过"
在事件流层面就能被发现。
"""

from __future__ import annotations

from typing import Any

from .fold import fold_goal
from .prompt import render_goal_round_prompt
from .types import GoalView


class GoalRoundPromptFailure(Exception):
    """续轮提示词与渲染函数不一致。"""


def _view_before(events: list[dict[str, Any]]) -> tuple[Any, Any]:
    return fold_goal(events), None


def verify_round_prompt(events: list[dict[str, Any]]) -> None:
    """逐条校验 goal 轮次消息（原版 ``validateEvent``）。"""
    prior: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("type") == "user_message":
            data = ev.get("data") or {}
            source = data.get("source")
            if isinstance(source, dict) and source.get("kind") == "goal" and int(source.get("round") or 0) > 0:
                folded = fold_goal(prior)
                goal = folded.goal
                if (goal is None or goal.phase != "active"
                        or goal.id != source.get("goalId")
                        or goal.revision != source.get("revision")
                        or int(source.get("round") or 0) != folded.rounds_started + 1
                        or int(source.get("round") or 0) > goal.max_goal_rounds):
                    raise GoalRoundPromptFailure(
                        f"goal round {source.get('round')} cannot be reconstructed from the preceding durable goal state")
                view = GoalView(
                    id=goal.id, revision=goal.revision, objective=goal.objective, phase=goal.phase,
                    max_goal_rounds=goal.max_goal_rounds, rounds_started=folded.rounds_started,
                    created_at=float(folded.created_at or 0), updated_at=float(folded.updated_at or 0),
                    activation="armed", blocked_reason=goal.blocked_reason,  # type: ignore[arg-type]
                )
                expected = render_goal_round_prompt(view, int(source.get("round") or 0))
                if str(data.get("content") or "") != expected:
                    raise GoalRoundPromptFailure(
                        f"goal round {source.get('round')} content does not match the package-owned continuation prompt")
        prior.append(ev)
