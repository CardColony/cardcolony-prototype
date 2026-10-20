"""严格重放 fold 与载荷解码 —— 对应 ``goal/src/fold.ts``（349 行，逐条移植）。

原版设计：**会话日志是唯一权威**。任何非法变更都 fail-loud 抛错，不猜测、不修补。
这里把 12 条硬规则逐条搬过来，一条都不许放宽：

1. 每次变更 ``revision`` 必须 = 当前 + 1，``id`` 不变。
2. 除 ``edit`` 外，任何操作改 ``objective`` / ``maxGoalRounds`` 一律拒。
3. 每次变更必须保留 ``createdAt`` 与 ``roundsStarted``；``updatedAt`` 不得回退。
4. ``pause`` 只能 ``active → paused``。
5. ``block`` 只能 ``active → blocked``。
6. ``resume`` 只能 ``active|paused|blocked → active``，且 ``roundsStarted < maxGoalRounds``。
7. ``complete`` 不能从 ``complete`` 再来一次。
8. ``create`` 必须 ``revision=1`` / ``phase=active`` / ``roundsStarted=0`` /
   ``id`` 从未出现过；且当前 goal 必须是 ``complete`` 才能新建。
9. ``clear`` 必须 ``revision+1``，且 ``clearedAt >= updatedAt``。
10. 每个字段的键集合必须**精确匹配**（多一个少一个都拒）。
11. ``objective`` / ``blockedReason.message`` 必须已 trim 且非空；``code`` 必须 lower-kebab-case。
12. 续轮 ``user_message`` 必须 ``round == roundsStarted+1``、``round <= maxGoalRounds``、
    ``goalId``/``revision`` 与当前一致、``phase == active`` —— **这是 roundsStarted 的唯一推进口**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .runtime import (GOAL_CHANGE_EVENT, GOAL_CHANGE_VERSION, GOAL_ROUND_EVENT,
                      USER_MESSAGE_EVENT, GoalStreamError)
from .types import PHASES, GoalBlockReason, GoalRef, GoalSnapshot

_SNAPSHOT_OPERATIONS = frozenset({"create", "edit", "pause", "resume", "complete", "block"})
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

_CLEAR_KEYS = frozenset({"cleared", "clearedAt", "kind", "operation", "version"})
_SNAPSHOT_KEYS = frozenset(
    {"createdAt", "goal", "kind", "operation", "roundsStarted", "updatedAt", "version"}
)
_GOAL_KEYS_BLOCKED = frozenset(
    {"blockedReason", "id", "maxGoalRounds", "objective", "phase", "revision"}
)
_GOAL_KEYS_PLAIN = frozenset({"id", "maxGoalRounds", "objective", "phase", "revision"})
_REF_KEYS = frozenset({"id", "revision"})
_REASON_KEYS = frozenset({"code", "message"})


@dataclass
class GoalFoldState:
    """纯 fold 累加器（原版 ``GoalFoldState``）。"""

    goal: Optional[GoalSnapshot] = None
    rounds_started: int = 0
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    last_ref: Optional[GoalRef] = None
    seen_goal_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class FoldedGoal:
    """fold 结果（原版 ``FoldedGoal``）——**故意不含 activation**。"""

    goal: Optional[GoalSnapshot] = None
    rounds_started: int = 0
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    last_ref: Optional[GoalRef] = None


def empty_goal_fold_state() -> GoalFoldState:
    return GoalFoldState()


# ---------------------------------------------------------------- 解码
def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GoalStreamError(f"goal change {field_name} must be a positive safe integer")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GoalStreamError(f"goal change {field_name} must be a non-negative safe integer")
    return value


def _non_negative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise GoalStreamError(f"goal change {field_name} must be a non-negative number")
    return float(value)


def _exact_keys(value: dict[str, Any], expected: frozenset[str], what: str) -> None:
    if frozenset(value.keys()) != expected:
        raise GoalStreamError(
            f"{what} must have exactly {','.join(sorted(expected))} fields, got {','.join(sorted(value.keys()))}"
        )


def _decode_block_reason(value: Any) -> GoalBlockReason:
    if not _is_record(value):
        raise GoalStreamError("goal change goal.blockedReason must be a record")
    _exact_keys(value, _REASON_KEYS, "goal change goal.blockedReason")
    code = value["code"]
    message = value["message"]
    if not isinstance(code, str) or not _KEBAB.match(code):
        raise GoalStreamError("goal change goal.blockedReason.code must be lower-kebab-case")
    if not isinstance(message, str) or not message.strip() or message != message.strip():
        raise GoalStreamError("goal change goal.blockedReason.message must be non-empty and normalized")
    return GoalBlockReason(code=code, message=message)


def _decode_snapshot(value: Any) -> GoalSnapshot:
    if not _is_record(value):
        raise GoalStreamError("goal change goal must be a record")
    gid = value.get("id")
    if not isinstance(gid, str) or not gid:
        raise GoalStreamError("goal change goal.id must be a non-empty string")
    objective = value.get("objective")
    if not isinstance(objective, str) or not objective.strip() or objective != objective.strip():
        raise GoalStreamError("goal change goal.objective must be non-empty and normalized")
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in PHASES:
        raise GoalStreamError("goal change goal.phase is invalid")
    _exact_keys(value, _GOAL_KEYS_BLOCKED if phase == "blocked" else _GOAL_KEYS_PLAIN, f"goal change goal for phase {phase}")
    reason = _decode_block_reason(value["blockedReason"]) if phase == "blocked" else None
    return GoalSnapshot(
        id=gid,
        revision=_positive_int(value.get("revision"), "goal.revision"),
        objective=objective,
        phase=phase,  # type: ignore[arg-type]
        max_goal_rounds=_positive_int(value.get("maxGoalRounds"), "goal.maxGoalRounds"),
        blocked_reason=reason,
    )


def _decode_ref(value: Any) -> GoalRef:
    if not _is_record(value):
        raise GoalStreamError("goal clear tombstone must be a record")
    _exact_keys(value, _REF_KEYS, "goal clear tombstone")
    gid = value.get("id")
    if not isinstance(gid, str) or not gid:
        raise GoalStreamError("goal clear tombstone id must be a non-empty string")
    return GoalRef(id=gid, revision=_positive_int(value.get("revision"), "cleared.revision"))


def decode_goal_change(value: Any) -> Optional[dict[str, Any]]:
    """解码一条自称 goal change 的载荷；无关值返 None，畸形则 fail-loud。"""
    if not _is_record(value) or value.get("kind") != GOAL_CHANGE_EVENT:
        return None
    if value.get("version") != GOAL_CHANGE_VERSION:
        raise GoalStreamError(f"unsupported goal change version {value.get('version')!r}")
    operation = value.get("operation")
    if operation == "clear":
        _exact_keys(value, _CLEAR_KEYS, "goal clear change")
        return {
            "kind": GOAL_CHANGE_EVENT,
            "version": GOAL_CHANGE_VERSION,
            "operation": "clear",
            "cleared": _decode_ref(value["cleared"]),
            "clearedAt": _non_negative_number(value.get("clearedAt"), "clearedAt"),
        }
    if not isinstance(operation, str) or operation not in _SNAPSHOT_OPERATIONS:
        raise GoalStreamError("goal change operation is invalid")
    _exact_keys(value, _SNAPSHOT_KEYS, "goal snapshot change")
    created_at = _non_negative_number(value.get("createdAt"), "createdAt")
    updated_at = _non_negative_number(value.get("updatedAt"), "updatedAt")
    if updated_at < created_at:
        raise GoalStreamError("goal change updatedAt cannot precede createdAt")
    return {
        "kind": GOAL_CHANGE_EVENT,
        "version": GOAL_CHANGE_VERSION,
        "operation": operation,
        "goal": _decode_snapshot(value["goal"]),
        "roundsStarted": _non_negative_int(value.get("roundsStarted"), "roundsStarted"),
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


# ---------------------------------------------------------------- 应用
def goal_change_ref(change: dict[str, Any]) -> GoalRef:
    """变更携带的版本身份（原版 ``goalChangeRef``）。"""
    if change["operation"] == "clear":
        return change["cleared"]
    goal: GoalSnapshot = change["goal"]
    return goal.ref()


def _require_same_definition(current: GoalSnapshot, nxt: GoalSnapshot, operation: str) -> None:
    if nxt.objective != current.objective or nxt.max_goal_rounds != current.max_goal_rounds:
        raise GoalStreamError(f"goal {operation} cannot change objective or maxGoalRounds")


def _require_next_revision(current: GoalSnapshot, nxt: GoalRef, operation: str) -> None:
    if nxt.id != current.id or nxt.revision != current.revision + 1:
        raise GoalStreamError(f"goal {operation} must advance the current goal by one revision")


def _validate_snapshot_transition(state: GoalFoldState, change: dict[str, Any], current: GoalSnapshot) -> None:
    nxt: GoalSnapshot = change["goal"]
    operation = change["operation"]
    _require_next_revision(current, nxt.ref(), operation)
    if state.updated_at is None:
        raise GoalStreamError("current goal fold lacks updatedAt")
    if (change["createdAt"] != state.created_at
            or change["updatedAt"] < state.updated_at
            or change["roundsStarted"] != state.rounds_started):
        raise GoalStreamError(f"goal {operation} does not preserve the current counters and timestamps")
    if operation == "edit":
        if nxt.phase != current.phase or nxt.blocked_reason != current.blocked_reason:
            raise GoalStreamError("goal edit cannot change phase or blocked reason")
        return
    _require_same_definition(current, nxt, operation)
    if operation == "pause":
        if current.phase != "active" or nxt.phase != "paused":
            raise GoalStreamError("goal pause has an invalid phase transition")
    elif operation == "resume":
        if current.phase not in ("active", "paused", "blocked") or nxt.phase != "active":
            raise GoalStreamError("goal resume has an invalid phase transition or exhausted round budget")
        if state.rounds_started >= nxt.max_goal_rounds:
            raise GoalStreamError("goal resume has an invalid phase transition or exhausted round budget")
    elif operation == "complete":
        if current.phase == "complete" or nxt.phase != "complete":
            raise GoalStreamError("goal complete has an invalid phase transition")
    elif operation == "block":
        if current.phase != "active" or nxt.phase != "blocked":
            raise GoalStreamError("goal block has an invalid phase transition")
    else:  # create 由调用方排除；闭集兜底
        raise GoalStreamError("unknown goal snapshot operation")


def apply_goal_change(state: GoalFoldState, change: dict[str, Any]) -> None:
    """把一条已解码变更应用到累加器（原版 ``applyGoalChange``）。"""
    ref = goal_change_ref(change)
    if change["operation"] == "clear":
        current = state.goal
        if current is None:
            raise GoalStreamError("goal clear requires a current goal")
        _require_next_revision(current, change["cleared"], "clear")
        if state.updated_at is None:
            raise GoalStreamError("current goal fold lacks updatedAt")
        if change["clearedAt"] < state.updated_at:
            raise GoalStreamError("goal clear timestamp cannot precede the current goal update")
        state.goal = None
        state.rounds_started = 0
        state.created_at = None
        state.updated_at = None
        state.last_ref = ref
        return
    goal: GoalSnapshot = change["goal"]
    if change["operation"] == "create":
        if (goal.revision != 1 or goal.phase != "active" or change["roundsStarted"] != 0
                or (state.goal is not None and state.goal.phase != "complete")
                or goal.id in state.seen_goal_ids):
            raise GoalStreamError("goal create requires a fresh active revision-one goal with zero rounds")
        state.seen_goal_ids.add(goal.id)
    else:
        current = state.goal
        if current is None:
            raise GoalStreamError(f"goal {change['operation']} requires a current goal")
        _validate_snapshot_transition(state, change, current)
    state.goal = goal
    state.rounds_started = change["roundsStarted"]
    state.created_at = change["createdAt"]
    state.updated_at = change["updatedAt"]
    state.last_ref = ref


def _goal_source(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从 user_message 事件里取 goal 续轮来源；非 goal 来源返 None。"""
    data = event.get("data") or {}
    source = data.get("source")
    if not _is_record(source) or source.get("kind") != "goal":
        return None
    gid = source.get("goalId")
    revision = source.get("revision")
    rnd = source.get("round")
    if (not isinstance(gid, str) or not gid
            or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
            or not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1):
        raise GoalStreamError("goal message source is invalid")
    return source


def apply_goal_event(state: GoalFoldState, event: dict[str, Any]) -> None:
    """把一个会话事件应用到严格 fold（原版 ``applyGoalEvent``）。"""
    etype = event.get("type")
    if etype == GOAL_CHANGE_EVENT:
        change = decode_goal_change(event.get("data"))
        if change is None:
            raise GoalStreamError(f"goal change at session event {event.get('seq')} has an invalid kind")
        apply_goal_change(state, change)
        return
    if etype == GOAL_ROUND_EVENT:
        # [轮次脱钩 2026-10-20] 续轮不再往会话塞 user 消息（那会让上下文逐轮累积
        # 同一份目标），改由 driver 记一条几十字节的 goal_round 事件来推进轮次。
        # 校验与旧口径一致：必须是当前 active 目标的下一轮、且不超上限。
        data = event.get("data") or {}
        try:
            gid = str(data["goalId"])
            rev = int(data["revision"])
            rnd = int(data["round"])
        except (KeyError, TypeError, ValueError):
            raise GoalStreamError(
                f"goal round at session event {event.get('seq')} has an invalid kind")
        current = state.goal
        if (current is None or current.phase != "active"
                or gid != current.id or rev != current.revision
                or rnd != state.rounds_started + 1
                or rnd > current.max_goal_rounds):
            raise GoalStreamError(
                f"goal round at session event {event.get('seq')} is not the next admitted round of the active goal")
        state.rounds_started = rnd
        return
    if etype == USER_MESSAGE_EVENT:
        source = _goal_source(event)
        if source is None:
            return
        current = state.goal
        if (current is None or current.phase != "active"
                or source["goalId"] != current.id
                or source["revision"] != current.revision
                or source["round"] != state.rounds_started + 1
                or source["round"] > current.max_goal_rounds):
            raise GoalStreamError(
                f"goal round at session event {event.get('seq')} is not the next admitted round of the active goal"
            )
        state.rounds_started = int(source["round"])


def fold_goal(events: list[dict[str, Any]]) -> FoldedGoal:
    """从连续会话日志重放（原版 ``foldGoal``）。"""
    state = empty_goal_fold_state()
    for event in events:
        apply_goal_event(state, event)
    return FoldedGoal(
        goal=state.goal,
        rounds_started=state.rounds_started,
        created_at=state.created_at,
        updated_at=state.updated_at,
        last_ref=state.last_ref,
    )
