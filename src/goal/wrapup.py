"""收尾指令 —— 对应 ``tool-goal/src/wrapup.ts``（逐字移植）。

自动续轮里报 complete / blocked 之后注入，替换掉"回合硬停"，让模型仍然对用户
说一次话再结束。
"""

from __future__ import annotations

import json

_GROUNDING = (
    "Report only what earlier rounds and tool results in this session actually establish; "
    "when a detail is not in the session, say so instead of inventing it. "
)


def render_wrapup_context(objective: str, blocked_reason: str | None = None) -> str:
    """渲染 ``<goal_complete>`` 或 ``<goal_blocked>`` 收尾指令。"""
    heading = f"Objective: {json.dumps(objective, ensure_ascii=False)}\n"
    if blocked_reason is None:
        return (
            "<goal_complete>\n" + heading
            + "The goal is marked complete and this autonomous run is ending. Write the closing "
            + "message to the user now: state the outcome, summarize what was done and how it was "
            + "verified, and point to the concrete results (files, commits, or other artifacts). "
            + _GROUNDING
            + "Note anything the user should review or do next. Address the user directly. Do not "
            + "call any more tools in this run; further work waits for the user's next instruction.\n"
            + "</goal_complete>"
        )
    return (
        "<goal_blocked>\n" + heading
        + f"Blocked: {json.dumps(blocked_reason, ensure_ascii=False)}\n"
        + "The goal is marked blocked and this autonomous run is ending. Write the closing "
        + "message to the user now: state what has been completed so far, describe the concrete "
        + "blocking condition and what you tried, and say exactly what you need from the user to "
        + "continue. "
        + _GROUNDING
        + "Address the user directly. Do not call any more tools in this run; further work "
        + "waits for the user's next instruction.\n"
        + "</goal_blocked>"
    )
