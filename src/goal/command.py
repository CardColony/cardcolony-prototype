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


#: 解析结果对外可见（路由要按 kind 决定「改完目标要不要立刻起跑」）。
parse = _parse


async def _arm_auto_mode(work_id: str, card_id: str) -> None:
    """`/goal` 建/改目标 ⇒ **同时打开自动模式**（用户 2026-10-28 口径）。

    改前 `/goal xxx` 只建目标、不动 ``auto_mode``，而 ``update_goal`` 的第一道闸
    就是「本卡不是自动模式卡 ⇒ denied」⇒ 目标建出来了、模型却没法标完成，
    自动模式也不会续轮。这一处补上，「/goal ＝ 开自动模式 ＋ 填目标」才成立。

    失败不阻断命令本身（目标已经写进去了，开关下一轮还能再开）。
    """
    try:
        from ..storage import card_store
        await card_store.update_card(
            work_id, card_id, {"auto_mode": 1}, "[goal] /goal 开启自动模式")
    except Exception:  # noqa: BLE001 开关失败不影响目标本身
        pass


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
            await _arm_auto_mode(work_id, card_id)
            return {"ok": True, "text": render_goal("Goal created", view)}

        if current is None:
            return {"ok": False, "text": f"No goal is currently set; /goal {cmd['kind']} requires one. {USAGE}"}

        if cmd["kind"] == "edit":
            if current.phase == "complete":
                # 完成态再 edit ＝ 开新生命周期；**不动 auto_mode**（见下条注释）
                view = await service.create(work_id, card_id, CreateGoalRequest(objective=cmd["objective"]))
                return {"ok": True, "text": render_goal("Goal created", view)}
            # 执行中改目标 ⇒ **不动 auto_mode**：它已经在跑，改的是目标内容不是开关
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
