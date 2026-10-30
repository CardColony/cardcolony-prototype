"""续轮提示词 —— 对应 ``goal-round-driver/src/prompt.ts``（逐字移植）。

原版用 ``JSON.stringify(objective)`` 保证多行/类标签文本被当作数据；
Python 侧用 ``json.dumps(ensure_ascii=False)`` 等价。
"""

from __future__ import annotations

import json

from .types import GoalView

_TEMPLATE = (
    "<goal_round>\n"
    "Objective: {objective}\n"
    "Round: {round}/{max_rounds}\n\n"
    "Continue working toward the objective in this same session. Treat the current workspace, "
    "tool results, and durable session state as authoritative; inspect them instead of assuming "
    "earlier narration is still current. Make concrete progress and verify the result. Before "
    "claiming completion, gather evidence that the whole objective is achieved, read the current "
    "goal, and mark it complete. If work remains, leave the goal active for the next round. Follow "
    "the configured goal-tool policy before reporting a blocker.\n"
    "</goal_round>"
)


def render_goal_round_prompt(goal: GoalView, round_no: int,
                             objective: str | None = None) -> str:
    """渲染一轮完整的续轮指令（原版 ``renderGoalRoundPrompt``）。

    ``objective`` 非 None 时用它替代 ``goal.objective``——
    [goal 变卡面区域 2026-10-28 · 用户令] **只认卡面上那段文字为目标**：
    驱动从卡的 ``goal`` 字段取内容（它就是卡面 goal 区），不再用 goal 域里
    事件溯源出来的副本。两份并存时以卡面为准，避免"改了卡面、续轮还在念旧目标"。
    """
    return _TEMPLATE.format(
        objective=json.dumps(objective if objective is not None else goal.objective,
                             ensure_ascii=False),
        round=round_no,
        max_rounds=goal.max_goal_rounds,
    )
