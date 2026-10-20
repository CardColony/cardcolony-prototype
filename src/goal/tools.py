"""模型面工具 —— 对应 ``tool-goal/src/index.ts``。

三件工具，与原版同名同参：

- ``get_goal()``
- ``create_goal(objective, max_goal_rounds?)`` —— 必须直接人类回合
- ``update_goal(goal_id, revision, action, objective?, max_goal_rounds?, blocked_reason?)``
  ``action ∈ {edit, pause, resume, complete, blocked}``

原版用 ``exec.deferContext`` 在 complete/blocked 之后注入收尾指令（``wrapup``）。
我们的回合是"工具循环"，没有 deferContext 这一步，所以把同一段文本放进**工具
返回的 data.wrapup**——模型在下一 step 读到它，效果一致（照样要写结语、不再调工具）。
"""

from __future__ import annotations

import logging
from typing import Any

from . import authority
from .runtime import DEFAULT_BLOCKED_AFTER_ROUNDS, GoalError
from .service import get_service
from .types import CreateGoalRequest, EditGoalRequest, GoalRef, GoalView
from .wrapup import render_wrapup_context

logger = logging.getLogger(__name__)

UPDATE_ACTIONS = ("edit", "pause", "resume", "complete", "blocked")


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}, "data": None}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "error": None, "data": data}


def _goal_value(view: GoalView | None) -> dict[str, Any]:
    """原版 ``goalValue``：紧凑输出，activation 是观测值不是重放状态。"""
    if view is None:
        return {"goal": None}
    out: dict[str, Any] = {
        "goal": {
            "id": view.id,
            "revision": view.revision,
            "objective": view.objective,
            "phase": view.phase,
            "roundsStarted": view.rounds_started,
            "maxGoalRounds": view.max_goal_rounds,
        },
        "activation": view.activation,
    }
    if view.blocked_reason is not None:
        out["goal"]["blockedReason"] = {
            "code": view.blocked_reason.code, "message": view.blocked_reason.message}
    return out


def _goal_ref(goal_id: Any, revision: Any) -> GoalRef:
    if not isinstance(goal_id, str) or not goal_id or goal_id != goal_id.strip():
        raise GoalError("goal_id must be non-empty", "GOAL_TOOL_INVALID_UPDATE")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise GoalError("revision must be a positive safe integer", "GOAL_TOOL_INVALID_UPDATE")
    return GoalRef(goal_id, revision)


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _has_round_cap(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value != 0


# ---------------------------------------------------------------- handlers
async def h_get_goal(work_id: str, current_id: str,
                     arguments: dict[str, Any], cards: dict[str, Any]) -> dict[str, Any]:
    try:
        authority.goal_tool_execution(authority.current_turn_source())
        view = await get_service().get(work_id, current_id)
    except GoalError as exc:
        return _err(exc.code, exc.message)
    return _ok(_goal_value(view))


async def h_create_goal(work_id: str, current_id: str,
                        arguments: dict[str, Any], cards: dict[str, Any]) -> dict[str, Any]:
    try:
        source = authority.current_turn_source()
        authority.goal_tool_execution(source)
        authority.require_direct_human(source)
        view = await get_service().create(work_id, current_id, CreateGoalRequest(
            objective=str(arguments.get("objective") or ""),
            max_goal_rounds=(int(arguments["max_goal_rounds"])
                             if _has_round_cap(arguments.get("max_goal_rounds")) else None),
        ))
    except GoalError as exc:
        return _err(exc.code, exc.message)
    return _ok(_goal_value(view))


async def h_update_goal(work_id: str, current_id: str,
                        arguments: dict[str, Any], cards: dict[str, Any]) -> dict[str, Any]:
    action = str(arguments.get("action") or "").strip()
    try:
        source = authority.current_turn_source()
        authority.goal_tool_execution(source)
        ref = _goal_ref(arguments.get("goal_id"), arguments.get("revision"))
        service = get_service()
        view = await service.get(work_id, current_id)

        if action == "edit":
            authority.require_direct_human(source)
            if _has_text(arguments.get("blocked_reason")):
                raise GoalError("blocked_reason is valid only with action blocked", "GOAL_TOOL_INVALID_UPDATE")
            view = await service.edit(work_id, current_id, ref, EditGoalRequest(
                objective=(str(arguments["objective"]) if _has_text(arguments.get("objective")) else None),
                max_goal_rounds=(int(arguments["max_goal_rounds"])
                                 if _has_round_cap(arguments.get("max_goal_rounds")) else None),
            ))
            return _ok(_goal_value(view))

        if action in ("pause", "resume"):
            authority.require_direct_human(source)
            if (_has_text(arguments.get("objective")) or _has_round_cap(arguments.get("max_goal_rounds"))
                    or _has_text(arguments.get("blocked_reason"))):
                raise GoalError(
                    "objective and max_goal_rounds are valid only with action edit; "
                    "blocked_reason is valid only with action blocked",
                    "GOAL_TOOL_INVALID_UPDATE")
            view = (await service.pause(work_id, current_id, ref) if action == "pause"
                    else await service.resume(work_id, current_id, ref))
            return _ok(_goal_value(view))

        if action not in ("complete", "blocked"):
            raise GoalError(f"action 非法: {action or '(空)'}", "GOAL_TOOL_INVALID_UPDATE")

        granted = authority.completion_authority(source, view)
        if _has_text(arguments.get("objective")) or _has_round_cap(arguments.get("max_goal_rounds")):
            raise GoalError("objective and max_goal_rounds are valid only with action edit",
                            "GOAL_TOOL_INVALID_UPDATE")
        if action == "complete" and _has_text(arguments.get("blocked_reason")):
            raise GoalError("blocked_reason is valid only with action blocked", "GOAL_TOOL_INVALID_UPDATE")
        reason = arguments.get("blocked_reason")
        if action == "blocked" and not _has_text(reason):
            raise GoalError("blocked_reason is required with action blocked", "GOAL_TOOL_INVALID_UPDATE")
        if (action == "blocked" and granted["kind"] == "goal-round"
                and granted["goal"].rounds_started < DEFAULT_BLOCKED_AFTER_ROUNDS):
            raise GoalError(
                f"blocked requires at least {DEFAULT_BLOCKED_AFTER_ROUNDS} consecutive goal rounds; "
                f"current round is {granted['goal'].rounds_started}",
                "GOAL_TOOL_BLOCK_THRESHOLD")

        view = (await service.complete(work_id, current_id, ref) if action == "complete"
                else await service.block(work_id, current_id, ref, "model-reported", str(reason)))
        data = _goal_value(view)
        if granted["kind"] == "goal-round":
            # 原版 deferContext(renderWrapupContext(...))；我们随工具结果下发。
            data["wrapup"] = render_wrapup_context(
                view.objective, str(reason) if action == "blocked" else None)
        return _ok(data)
    except GoalError as exc:
        return _err(exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 工具面不许抛裸异常
        logger.exception("[goal] update_goal 异常 card=%s", current_id)
        return _err("GOAL_TOOL_INVALID_UPDATE", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- schemas
GET_GOAL_SCHEMA: dict[str, Any] = {
    "name": "get_goal",
    "description": (
        "读取当前会话的目标：精确 id/revision、objective、phase、已跑轮次、轮次上限、"
        "blocked 原因，以及是否还允许自动续轮（activation）。**调用 update_goal 之前必须先读它**，"
        "并把 id 与 revision 原样抄过去。"
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

CREATE_GOAL_SCHEMA: dict[str, Any] = {
    "name": "create_goal",
    "description": (
        "当**当前这条人类请求**是一个需要跨多轮自主推进的长目标时，创建一个持久目标。"
        "可以从人类意图推断，不必等他说“建个目标”。单轮就能做完的琐事不要建。"
        "只有直接人类回合有权创建。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "从人类请求推断出的、可验收的完成目标。"},
            "max_goal_rounds": {"type": "integer", "description": "可选：自动续轮的轮次上限。"},
        },
        "required": ["objective"],
    },
}

UPDATE_GOAL_SCHEMA: dict[str, Any] = {
    "name": "update_goal",
    "description": (
        "更新当前目标的精确版本（先用 get_goal 取 goal_id 与 revision）。"
        "action：edit/pause/resume 需要**直接人类回合**；complete/blocked 在自动续轮里也允许。"
        "blocked 在未达最少轮次前会被拒；模型负责判断“同一个阻塞条件确实连续存在”，并写进 blocked_reason。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal_id": {"type": "string", "description": "get_goal 返回的精确 id。"},
            "revision": {"type": "integer", "description": "get_goal 返回的精确 revision。"},
            "action": {"type": "string", "enum": list(UPDATE_ACTIONS),
                       "description": "edit | pause | resume | complete | blocked"},
            "objective": {"type": "string", "description": "替换目标，仅 action=edit。"},
            "max_goal_rounds": {"type": "integer", "description": "替换轮次上限，仅 action=edit。"},
            "blocked_reason": {"type": "string", "description": "具体阻塞条件，仅 action=blocked 必填。"},
        },
        "required": ["goal_id", "revision", "action"],
    },
}
