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
from typing import Any, Optional

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


async def _zero_role_close_gate_block(work_id: str, current_id: str,
                                      cards: dict[str, Any]) -> Optional[dict[str, Any]]:
    """[5713 收口闸 · card_023] 零角色收口闸的机械判定。

    方向变更（card_004-799a7954）：删掉「【无人盯】写理由即可放行」出口——用户原话
    「强制它做一步拆分团队，不要规定它拆法，只是强制它拆」。唯一出路＝真的调
    create_role 建成至少一个**非空壳 active 角色**；空壳与不设同罪。

    判据（纯机械，同时满足才拦）：
      ① current_id == work_store.work_card_id_of(work_id)——只拦项目根卡
         （非根卡/编程卡一律不拦）；
      ② 该 work 工作卡 ≥ 2（card_type != "role"、parent != ""、不是根卡自己；
         影子卡 card_type="shadow" 天然排除）；
      ③ 无非空壳 active 职能（duty.state=="active" 才算在盯，suspended/archived
         不算；且 active 职能里至少一个 exclusive_key 非空、职责书非空）。

    [card_031 改接] 模型建的是**职能**（create_function，写 function_profiles）、
    不再是 role 卡——故判据 ③ 同时认两套：**function_profiles**（新路径，主）与
    role_profiles（旧路径，存量 role 卡仍在盯的也算）。出路文案把 create_function
    放在第一位；保留 create_role 字样以兼容 card_023 既有断言（旧路径仍可用）。

    返回：拦下 ⇒ 错误信封；放行 ⇒ None。闸只要求「拆」，不规定拆法（守 D02）。
    """
    try:
        from ..storage import function_store, role_store, work_store
        _root_id = work_store.work_card_id_of(work_id)
        _is_root = bool(_root_id) and current_id == _root_id
        _work_cards = [
            cid for cid, m in (cards or {}).items()
            if not str(cid or "").startswith("shadow_")
            and str((m or {}).get("card_type") or "") != "role"
            and str((m or {}).get("parent") or "") != ""
            and cid != _root_id
        ]
        # [card_031 改接] 判据 ③ 同时认两套「在盯的职能」：
        #   · function_profiles（新路径，模型经 create_function 建，主）；
        #   · role_profiles（旧路径，存量 role 卡仍在盯的也算）。
        # 两套各自判「非空壳 active」：duty.state==active 且 exclusive_key 非空
        # 且职责书非空。任一套命中即放行——闸的目的只是「真的有人盯」。
        _solid = False
        try:
            _fns = function_store.list_profiles(work_id) or []
            _solid = _solid or any(
                str((f.get("duty") or {}).get("state") or "") == "active"
                and str((f.get("jurisdiction") or {}).get("exclusive_key")
                        or (f.get("jurisdiction") or {}).get("domain") or "").strip()
                and str((f.get("charter") or {}).get("text") or "").strip()
                for f in _fns)
        except Exception:  # noqa: BLE001 新表读不到不阻断（还有旧路径可查）
            pass
        if not _solid:
            _roles = await role_store.list_profiles(work_id)
            _active_roles = [pr for pr in (_roles or [])
                             if str((pr.get("duty") or {}).get("state") or "") == "active"]
            _solid = any(
                str((pr.get("jurisdiction") or {}).get("exclusive_key") or "").strip()
                and str((pr.get("charter") or {}).get("text") or "").strip()
                for pr in _active_roles)
    except Exception as exc:  # noqa: BLE001 读不到职能/角色表不阻断主链（fail-open）
        logger.warning("[5713 收口闸] 查职能失败（放行）card=%s: %s", current_id, exc)
        _is_root, _work_cards, _solid = False, [], False
    if _is_root and len(_work_cards) >= 2 and not _solid:
        return _err("validation",
                    "收口被拒：本 work 有 "
                    f"{len(_work_cards)} 张工作卡"
                    f"（{', '.join(_work_cards)}），当前零个职能角色在盯"
                    "（active 职能数为 0，或均为空壳——空壳等于没人盯）。"
                    "出路：用 create_function 组建职能（设几个、设什么域、盯哪些卡，"
                    "由你判断；建好后需 duty.state=active 且职责书非空）；"
                    "旧 create_role 组阁路径仍可用；"
                    "或调 update_goal(blocked, reason=...) 说明卡在哪。")
    return None


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
    """给一张卡设目标：**写卡面 goal 区 + 开自动模式**，再让状态机跟上。

    [goal 变卡面区域 2026-10-28 · 用户令] 三处对齐：
      ① 目标内容的真值是**卡面 goal 区**（不是 goal 对象里的副本）⇒ 先写卡；
      ② `target` 默认自己，给了就是**父卡给子卡派任务**——这是比 direct 更明确的派活
         通道（direct 只说一句话，设目标会让它一直跑到声明完成）；
      ③ 去掉「必须直接人类回合」的限制：父卡在卡间消息回合里也要能派任务。
         权限改走 `policy.check(SWITCH_PLAN)` + 「必须是我的子卡」。
    """
    target = str(arguments.get("target") or "").strip() or current_id
    objective = str(arguments.get("objective") or "").strip()
    if not objective:
        return _err("validation", "objective 不能为空：写清「做到什么算完成」")
    try:
        source = authority.current_turn_source()
        authority.goal_tool_execution(source)
        if target != current_id:
            from ..permissions import policy
            policy.check(current_id, target, policy.SWITCH_PLAN, cards)
            meta = (cards or {}).get(target) or {}
            if str(meta.get("parent") or "") != current_id:
                return _err("denied", f"{target} 不是你的子卡，不能给它设目标")
        # ① 卡面（目标真值）
        from ..storage import card_store
        await card_store.update_card(
            work_id, target,
            {"goal": objective, "auto_mode": 1},
            f"[goal] {current_id} 设定目标")
        # ② 状态机（只记 phase/rounds，内容以卡面为准）
        svc = get_service()
        cur = await svc.get(work_id, target)
        if cur is None or cur.phase == "complete":
            view = await svc.create(work_id, target, CreateGoalRequest(
                objective=objective,
                max_goal_rounds=(int(arguments["max_goal_rounds"])
                                 if _has_round_cap(arguments.get("max_goal_rounds")) else None),
            ))
        else:
            view = await svc.edit(work_id, target, cur.ref(),
                                  EditGoalRequest(objective=objective))
    except GoalError as exc:
        return _err(exc.code, exc.message)
    except PermissionError as exc:
        return _err("denied", str(exc))
    except Exception as exc:  # noqa: BLE001 写卡失败如实回给模型
        return _err("internal", f"设定目标失败: {exc}")
    return _ok(_goal_value(view))


async def h_update_goal(work_id: str, current_id: str,
                        arguments: dict[str, Any], cards: dict[str, Any]) -> dict[str, Any]:
    action = str(arguments.get("action") or "").strip()
    # [goal 变卡面区域 2026-10-28 · 用户令] 加 target 与「只改内容」分支：
    #   · target 默认自己，给了就是**父卡改子卡目标**；
    #   · 只给 objective 不给 action ⇒ 纯改内容，**不需要 goal_id/revision**
    #     （内容真值是卡面，改卡面就行）；状态机有就跟一步，没有就建一个。
    target = str(arguments.get("target") or "").strip() or current_id
    _objective = str(arguments.get("objective") or "").strip()
    if _objective and not action:
        try:
            source = authority.current_turn_source()
            authority.goal_tool_execution(source)
            if target != current_id:
                from ..permissions import policy
                policy.check(current_id, target, policy.SWITCH_PLAN, cards)
                meta = (cards or {}).get(target) or {}
                if str(meta.get("parent") or "") != current_id:
                    return _err("denied", f"{target} 不是你的子卡，不能改它的目标")
            from ..storage import card_store
            await card_store.update_card(
                work_id, target, {"goal": _objective},
                f"[goal] {current_id} 更新目标")
            svc = get_service()
            cur = await svc.get(work_id, target)
            if cur is None or cur.phase == "complete":
                view = await svc.create(work_id, target,
                                        CreateGoalRequest(objective=_objective))
            else:
                view = await svc.edit(work_id, target, cur.ref(),
                                      EditGoalRequest(objective=_objective))
        except GoalError as exc:
            return _err(exc.code, exc.message)
        except PermissionError as exc:
            return _err("denied", str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err("internal", f"更新目标失败: {exc}")
        return _ok(_goal_value(view))
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
                f"{d.get('id')}: {str(d.get('instruction') or '')}"
                for d in _blockers[:3])
            return _err("ROLE_DIRECTIVE_BLOCKING",
                        f"收口被拒：本卡还有 {len(_blockers)} 条未验收的角色指令"
                        f"（{_desc}）。先按指令做完，让角色 verify_directive(complied)，"
                        "或 update_goal(blocked) 说明卡在哪。")

        # [5713 收口闸 · card_023] 零角色收口闸：项目根卡收口时若多张工作卡
        # 零人盯，收口被拒（强制做一步拆分团队，不规定拆法）。放在 authority
        # 之前：即使调用方不在开放回合，也先如实告诉它卡在哪（同指令闸手法）。
        _role_gate = await _zero_role_close_gate_block(work_id, current_id, cards)
        if _role_gate is not None:
            return _role_gate

        # [子卡未完成闸 2026-10-28 · 用户实测「goal 形同虚设」] 有子卡还在跑 ⇒ 不许收口。
        #
        # 为什么必须加：`drive()` 续轮时有 `_yield_to_children`（有子卡就让位），
        # 收口却完全不看子卡 ⇒ 卡能在子卡还在跑的时候把自己标 complete，然后
        # 目标 disarmed、自动模式再也不驱动它。实测 card_061 就是这样：收口时
        # card_066/card_067 两张子卡还在跑。
        # 同一条纪律的两端：**续轮让位给子卡，收口也得等子卡**。
        try:
            from ..api import auto_driver
            _kids = auto_driver.has_active_child(work_id, current_id)
        except Exception:  # noqa: BLE001 判不出来就放行（宁可放过，不误拦）
            _kids = False
        if _kids:
            return _err(
                "CHILD_PENDING",
                "收口被拒：本卡还有子卡在跑。目标没真正做完就收口，等于把这条线"
                "半路关掉（自动模式不会再驱动它）。出路二选一：等子卡收口后再 "
                "update_goal(complete)；或 update_goal(blocked) 说明卡在哪一步、"
                "在等哪张子卡。")
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
        # [阶段收敛 2026-10-25 · 用户令「职能只在阶段完工介入,工作细节域内
        # 自决」] 逐卡两段式收口闸撤除——逐卡评审=逐卡叫醒职能=无限上报。
        # 完工即 complete;质量与方向在**阶段收口**由职能统一表态
        # (全部直接子卡 completed ⇒ 父卡 phase_arrived ⇒ 汇聚)。

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
    把子卡的目标 resume（写 goal/change + arm）并立刻点火。

    边界（都是硬判据，不靠模型自觉）：
      · 只能是自己**子树内**的卡（``policy._is_in_subtree``）；
      · [唤醒不看模式 2026-11-01 · 用户令「唤醒子卡不需要自动」] **不再要求**
        ``auto_mode=1``——「把它拉起来干活」与「它要不要自动续轮」是两回事：
        自动卡 resume 后走续轮驱动（request_drive）；手动卡没有驱动，恢复
        相位后亲手点一轮（在跑则不打断，它本轮收口会自查）。
      · ``complete`` 也能恢复：用同一目标开**新生命周期**（轮次归零），
        不再要求父卡新建子卡（[2026-10-21 · 用户令]）；
      · 自动卡已在 active+armed ⇒ 幂等返回，不重复点火；手动卡的忙闲看
        「有没有在途回合」，不看 goal 相位（它没有驱动，相位与实跑解耦）。
    """
    from ..permissions import policy

    target = str(arguments.get("card_id") or "").strip()
    if not target:
        return _err("validation", "card_id 不能为空")
    meta = (cards or {}).get(target) or {}
    if not meta:
        return _err("not_found", f"卡片不存在: {target}")
    # [强制停止 2026-10-24] 恢复 = 同时解除停止标记(否则解了 goal 也进不了回合)
    try:
        from ..api import control as _control
        _control.unhalt(work_id, target, by=current_id, via="resume_child")
    except Exception:  # noqa: BLE001
        pass
    try:
        if not policy._is_in_subtree(current_id, target, cards):
            return _err("denied", f"越权：{target} 不在你的子树内，不能替它恢复目标")
    except Exception as exc:  # noqa: BLE001 判定失败一律拒（宁可不动）
        return _err("denied", f"子树判定失败，已拒绝：{exc}")

    is_auto = bool(meta.get("auto_mode"))
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
        elif goal.phase == "active" and goal.activation == "armed" and is_auto:
            return _ok({"resumed": False, "reason": "已在推进中", **_goal_value(goal)})
        elif goal.phase == "active":
            # 手动卡的相位与实跑解耦（没有驱动），active 无需再动相位，
            # 直接到下面点火；resume(active+armed) 反而会被状态机拒掉。
            view = goal
        else:
            view = await service.resume(work_id, target, goal.ref())
    except GoalError as exc:
        return _err(exc.code, exc.message)

    ignited = False
    if is_auto:
        # 自动卡：resume 本身会触发 goal/changed 监听，这里再叫一次是幂等的
        # （request_drive 有 requested 合并）——驱动器接管后续续轮。
        try:
            from . import driver as _driver
            _driver.request_drive(work_id, target)
            ignited = True
        except Exception as exc:  # noqa: BLE001 点火失败不回滚 resume，只记日志
            logger.warning("[goal] resume_child 点火失败 card=%s: %s", target, exc)
    else:
        # 手动卡没有续轮驱动：resume 只把目标相位摆正，还得亲手点一轮。
        try:
            from ..storage import session_facade as _sf
            busy = bool(_sf.turn_holder(work_id, target))
        except Exception:  # noqa: BLE001 判不出来按空闲处理（点火撞互斥也如实回落）
            busy = False
        if busy:
            data = _goal_value(view)
            data["resumed"] = True
            data["ignited"] = False
            data["reason"] = "目标已恢复；它正在跑，不打断，本轮收口后会自查"
            return _ok(data)
        try:
            from ..api import message_runner as _mr
            from ..api import routes as _routes
            prompt = (
                "## 上级唤醒\n\n"
                f"你的目标已由 `{current_id}` 恢复推进（此前停在暂停/卡住/完工）。"
                "请接着这个目标继续干活；觉得做完了就按卡的完工/收口通道声明，"
                "这轮也可以选择不做事直接结束。"
            )
            task = _mr._spawn_tracked(_routes._run_message(
                work_id, target, prompt, None, append_user=False,
                source={"kind": "resume_child", "from": current_id}))
            task._card_msg_target = target  # type: ignore[attr-defined]
            ignited = True
        except Exception as exc:  # noqa: BLE001 点火失败不回滚 resume，只记日志
            logger.warning("[goal] resume_child 手动卡点火失败 card=%s: %s",
                           target, exc)

    data = _goal_value(view)
    data["resumed"] = True
    data["ignited"] = ignited
    return _ok(data)


# ---------------------------------------------------------------- schemas
GET_GOAL_SCHEMA: dict[str, Any] = {
    "name": "get_goal",
    "description": (
        "读取当前会话的目标：精确 id/revision、objective、phase、已跑轮次、轮次上限、"
        "blocked 原因，以及本目标是否还参与自动续轮（activation）。**调用 update_goal 之前必须先读它**，"
        "并把 id 与 revision 原样抄过去。"
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

CREATE_GOAL_SCHEMA: dict[str, Any] = {
    "name": "create_goal",
    "description": (
        "给一张卡设定目标（写入它的**卡面 goal 区**，并打开自动模式）。"
        "不给 target 就是给自己设；给了 target 就是给子卡派任务——"
        "这是父卡派活的正规通道，比 direct 更明确：目标会一直驱动它跑到声明完成。"
        "目标要可验收，写清「做到什么算完成」。单轮就能做完的琐事不要设目标。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "要达成的、可验收的目标。"},
            "target": {"type": "string", "description": "可选：目标卡 id（默认＝自己）。给子卡设目标时填它。"},
            "max_goal_rounds": {"type": "integer", "description": "可选：自动续轮的轮次上限。"},
        },
        "required": ["objective"],
    },
}

UPDATE_GOAL_SCHEMA: dict[str, Any] = {
    "name": "update_goal",
    "description": (
        "改一张卡的目标，或改它的目标状态。"
        "**objective**：改目标内容，直接写它的卡面 goal 区（不需要 goal_id；"
        "自动续轮里也能用——做完了但还有后续就改目标继续，不要新开一张卡）。"
        "**action**：声明目标态（complete 收口 / pause 暂停 / resume 复活 / blocked 卡住），"
        "这时才需要 goal_id 与 revision（先用 get_goal 取）。"
        "target 不给＝改自己；给了＝改子卡（父卡改子卡目标）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal_id": {"type": "string", "description": "get_goal 返回的精确 id。"},
            "revision": {"type": "integer", "description": "get_goal 返回的精确 revision。"},
            "target": {"type": "string", "description": "可选：目标卡 id（默认＝自己）。父卡改子卡目标时填它。"},
            "action": {"type": "string", "enum": list(UPDATE_ACTIONS),
                       "description": "edit | pause | resume | complete | blocked（只改内容时不用给）"},
            "objective": {"type": "string", "description": "新目标内容：直接写卡面 goal 区。"},
            "max_goal_rounds": {"type": "integer", "description": "替换轮次上限，仅 action=edit。"},
            "blocked_reason": {"type": "string", "description": "具体阻塞条件，仅 action=blocked 必填。"},
        },
        "required": [],
    },
}


RESUME_CHILD_SCHEMA: dict[str, Any] = {
    "name": "resume_child",
    "description": (
        "让一张**停下的子卡重新开始自主推进**：解除它的停止标记、把目标从 "
        "paused/blocked 恢复成 active，并立刻点火让它接着跑（不用你再发消息催）。"
        "只能用于自己子树内的卡；自动卡恢复后走自动续轮，手动卡点一轮让它干活——"
        "不看 auto_mode。已完成（complete）的卡也能恢复：用同一目标开新生命周期，"
        "不必新建子卡。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {"type": "string", "description": "要恢复的子卡 id"},
        },
        "required": ["card_id"],
    },
}
