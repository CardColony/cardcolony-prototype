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
    # [角色机制 5710] 指令闸：目标卡还有未验收的角色指令时不许 complete。
    # 放在 authority 之前：即使调用方不在开放回合，也先如实告诉它“先完成指令”。
    if action == "complete":
        try:
            from ..api import role_room
            _blockers = role_room.open_directives_for(work_id, current_id)
        except Exception:  # noqa: BLE001 读不到闸表不阻断主链（fail-open）
            _blockers = []
        if _blockers:
            _desc = "；".join(
                f"{d.get('id')}: {str(d.get('instruction') or '')[:60]}"
                for d in _blockers[:3])
            return _err("ROLE_DIRECTIVE_BLOCKING",
                        f"收口被拒：本卡还有 {len(_blockers)} 条未验收的角色指令"
                        f"（{_desc}）。先按指令做完，让角色 verify_directive(complied)，"
                        "或 update_goal(blocked) 说明卡在哪。")
    try:
        source = authority.current_turn_source()
        authority.goal_tool_execution(source)
        ref = _goal_ref(arguments.get("goal_id"), arguments.get("revision"))
        service = get_service()
        view = await service.get(work_id, current_id)

        if action == "edit":
            # [自动续跑可改目标 2026-10-21 · 用户令] 原来 edit 要求**直接人类回合**，
            # 于是自动续轮里模型发现「这条线还有后续」也改不了目标，只能新开一张卡
            # 从头再来——同一件事被拆成两张卡，上下文和留痕全断。
            # 现在放开：模型在自己的续轮里就能改目标并继续（complete 也一样）。
            if _has_text(arguments.get("blocked_reason")):
                raise GoalError("blocked_reason is valid only with action blocked", "GOAL_TOOL_INVALID_UPDATE")
            _objective = (str(arguments["objective"])
                          if _has_text(arguments.get("objective")) else None)
            _rounds = (int(arguments["max_goal_rounds"])
                       if _has_round_cap(arguments.get("max_goal_rounds")) else None)
            if view is not None and view.phase == "complete":
                # complete 在状态机里不能 edit 回 active（edit 保持 phase）⇒
                # 用新 objective 开一轮新生命周期（create 是唯一合法入口）。
                view = await service.create(work_id, current_id, CreateGoalRequest(
                    objective=_objective or view.objective, max_goal_rounds=_rounds))
            else:
                view = await service.edit(work_id, current_id, ref, EditGoalRequest(
                    objective=_objective, max_goal_rounds=_rounds))
                if view.phase in ("paused", "blocked"):
                    # 改完目标顺手激活：只改不激活＝改了也不跑，用户会以为没生效。
                    view = await service.resume(work_id, current_id, view.ref())
            return _ok(_goal_value(view))

        if action in ("pause", "resume"):
            # pause 仍要求直接人类回合（停是人的决定）；resume 放开——
            # 模型判断「还有后续要干」时自己就能重新激活，不必等人来点。
            if action == "pause":
                authority.require_direct_human(source)
            if (_has_text(arguments.get("objective")) or _has_round_cap(arguments.get("max_goal_rounds"))
                    or _has_text(arguments.get("blocked_reason"))):
                raise GoalError(
                    "objective and max_goal_rounds are valid only with action edit; "
                    "blocked_reason is valid only with action blocked",
                    "GOAL_TOOL_INVALID_UPDATE")
            if action == "pause":
                view = await service.pause(work_id, current_id, ref)
            elif view is not None and view.phase == "complete":
                # [重新激活 2026-10-21 · 用户令] complete 不能 resume（状态机不允许）
                # ⇒ 用同一目标开新生命周期。这就是「重新激活这张卡」。
                view = await service.create(work_id, current_id, CreateGoalRequest(
                    objective=view.objective))
            else:
                view = await service.resume(work_id, current_id, ref)
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


async def h_resume_child(work_id: str, current_id: str,
                         arguments: dict[str, Any], cards: dict[str, Any]) -> dict[str, Any]:
    """让**自己子树内**一张停下的卡重新开始自主推进。

    为什么需要：goal 域里 ``update_goal`` 只能改**自己的**目标（原版 dsh 同此），
    于是父卡看见子卡 paused/blocked 时只能发 msg_ask 把它叫醒一次，叫醒之后它
    目标仍是 paused，驱动不会再续 —— 「叫得醒、续不上」。这条工具补的就是那一步：
    把子卡的目标 resume（写 goal/change + arm）并立刻 request_drive。

    边界（都是硬判据，不靠模型自觉）：
      · 只能是自己**子树内**的卡（``policy._is_in_subtree``）；
      · 目标卡必须是 ``auto_mode=1``；
      · ``complete`` 也能恢复：用同一目标开**新生命周期**（轮次归零），
        不再要求父卡新建子卡（[2026-10-21 · 用户令]）；
      · 已在 active+armed ⇒ 幂等返回，不重复点火。
    """
    from ..permissions import policy

    target = str(arguments.get("card_id") or "").strip()
    if not target:
        return _err("validation", "card_id 不能为空")
    meta = (cards or {}).get(target) or {}
    if not meta:
        return _err("not_found", f"卡片不存在: {target}")
    if not bool(meta.get("auto_mode")):
        return _err("denied", f"{target} 不是自动模式卡，没有目标可恢复")
    try:
        if not policy._is_in_subtree(current_id, target, cards):
            return _err("denied", f"越权：{target} 不在你的子树内，不能替它恢复目标")
    except Exception as exc:  # noqa: BLE001 判定失败一律拒（宁可不动）
        return _err("denied", f"子树判定失败，已拒绝：{exc}")

    service = get_service()
    try:
        goal = await service.get(work_id, target)
        if goal is None:
            return _err("validation", f"{target} 没有目标；要它干活请给它发消息或建子卡")
        if goal.phase == "complete":
            # [重新激活子卡 2026-10-21 · 用户令] 父卡给一张**已完成**的子卡派新活时，
            # 不必再新建一张子卡：用同一目标开新生命周期（create 是 complete 之后
            # 唯一合法入口），上下文/留痕都留在原来那张卡上。
            view = await service.create(
                work_id, target, CreateGoalRequest(objective=goal.objective))
        elif goal.phase == "active" and goal.activation == "armed":
            return _ok({"resumed": False, "reason": "已在推进中", **_goal_value(goal)})
        else:
            view = await service.resume(work_id, target, goal.ref())
    except GoalError as exc:
        return _err(exc.code, exc.message)

    # 立刻评估点火（不等下一轮 idle）——resume 本身也会触发 goal/changed 监听，
    # 这里再叫一次是幂等的（request_drive 有 requested 合并）。
    try:
        from . import driver as _driver
        _driver.request_drive(work_id, target)
    except Exception as exc:  # noqa: BLE001 点火失败不回滚 resume，只记日志
        logger.warning("[goal] resume_child 点火失败 card=%s: %s", target, exc)

    data = _goal_value(view)
    data["resumed"] = True
    return _ok(data)


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
        "action=edit 改目标：**自动续轮里也能用**——任务做完了但还有后续，改目标继续，"
        "不要新开一张卡（complete 状态下 edit 会开一轮新生命周期并立刻继续）；"
        "action=resume 重新激活：paused/blocked/complete 都能拉回 active 继续跑。"
        "action=pause 需要直接人类回合（停是人的决定）。"
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


RESUME_CHILD_SCHEMA: dict[str, Any] = {
    "name": "resume_child",
    "description": (
        "让一张**停下的子卡重新开始自主推进**：把它的目标从 paused/blocked 恢复成 active，"
        "并立刻点火让它接着跑（不用你再发消息催）。只能用于自己子树内、自动模式卡；"
        "已完成（complete）的卡也能恢复：用同一目标开新生命周期，不必新建子卡。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {"type": "string", "description": "要恢复的子卡 id"},
        },
        "required": ["card_id"],
    },
}
