"""人面 goal 命令 —— 对应 ``command-goal/src/index.ts``。

原版是 ``/goal [<objective>|clear|edit <objective>|pause|resume]``。我们把它挂到
HTTP：``POST /api/works/{w}/cards/{c}/goal``，body ``{"input": "..."}``。
语法、提示语、状态标签逐条照搬。
"""

from __future__ import annotations

from typing import Any

from .runtime import GoalError
from .service import get_service
from .types import CreateGoalRequest, EditGoalRequest, GoalView

USAGE = "Usage: /goal [<objective>|clear|edit <objective>|pause|resume]"


def _phase_label(phase: str) -> str:
    return phase if phase in ("active", "paused", "blocked", "complete") else phase


def _command_hint(goal: GoalView) -> str:
    if goal.phase == "active":
        return ("/goal edit <objective>, /goal pause, /goal clear" if goal.activation == "armed"
                else "/goal edit <objective>, /goal resume, /goal clear")
    if goal.phase in ("paused", "blocked"):
        return "/goal edit <objective>, /goal resume, /goal clear"
    return "/goal <objective>, /goal clear"


def render_goal(title: str, goal: GoalView) -> str:
    lines = [title, f"Status: {_phase_label(goal.phase)}"]
    if goal.phase == "blocked" and goal.blocked_reason is not None:
        lines.append(f"Blocker: {goal.blocked_reason.code}: {goal.blocked_reason.message}")
    lines += [
        f"Objective: {goal.objective}",
        f"Rounds: {goal.rounds_started}/{goal.max_goal_rounds}",
        f"Activation: {goal.activation}",
        "",
        f"Commands: {_command_hint(goal)}",
    ]
    return "\n".join(lines)


def _parse(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {"kind": "show"}
    control = text.lower()
    if control == "clear":
        return {"kind": "clear"}
    if control == "pause":
        return {"kind": "pause"}
    if control == "resume":
        return {"kind": "resume"}
    if control == "edit":
        return {"kind": "invalid-edit"}
    if control.startswith("edit ") or control.startswith("edit\t"):
        return {"kind": "edit", "objective": text[4:].strip()}
    return {"kind": "create", "objective": text}


async def execute_command(work_id: str, card_id: str, raw_input: str) -> dict[str, Any]:
    """执行一条人面命令（原版 ``executeGoalCommand``）。"""
    cmd = _parse(raw_input)
    service = get_service()
    try:
        current = await service.get(work_id, card_id)
        if cmd["kind"] == "show":
            if current is None:
                return {"ok": True, "text": f"No goal is currently set.\n{USAGE}"}
            return {"ok": True, "text": render_goal("Goal", current)}

        if cmd["kind"] == "invalid-edit":
            return {"ok": False, "text": f"Goal editing requires a replacement objective.\n{USAGE}"}

        if cmd["kind"] == "create":
            if current is not None and current.phase != "complete":
                return {"ok": False, "text": (
                    f"A goal is already {_phase_label(current.phase)}. Use /goal edit <objective> "
                    "to change it or /goal clear before replacing it.")}
            view = await service.create(work_id, card_id, CreateGoalRequest(objective=cmd["objective"]))
            return {"ok": True, "text": render_goal("Goal created", view)}

        if current is None:
            return {"ok": False, "text": f"No goal is currently set; /goal {cmd['kind']} requires one. {USAGE}"}

        if cmd["kind"] == "edit":
            if current.phase == "complete":
                view = await service.create(work_id, card_id, CreateGoalRequest(objective=cmd["objective"]))
                return {"ok": True, "text": render_goal("Goal created", view)}
            view = await service.edit(work_id, card_id, current.ref(),
                                      EditGoalRequest(objective=cmd["objective"]))
            return {"ok": True, "text": render_goal("Goal updated", view)}

        if cmd["kind"] == "pause":
            return {"ok": True, "text": render_goal("Goal paused", await service.pause(work_id, card_id, current.ref()))}

        if cmd["kind"] == "resume":
            return {"ok": True, "text": render_goal("Goal resumed", await service.resume(work_id, card_id, current.ref()))}

        if cmd["kind"] == "clear":
            await service.clear(work_id, card_id, current.ref())
            return {"ok": True, "text": "Goal cleared."}

        return {"ok": False, "text": USAGE}
    except GoalError:
        return {"ok": False, "text": (
            "The goal command is not valid for the current state. Run /goal to view available commands.")}
