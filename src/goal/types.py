"""goal 域纯类型 —— 对应 ``goal/src/types.ts``。

字段名与 JSON 载荷保持原版的 camelCase（``maxGoalRounds`` / ``blockedReason`` /
``roundsStarted`` / ``createdAt`` / ``updatedAt`` / ``goalId``），这样落盘的事件
与原版会话日志可逐字对照；Python 内部属性用 snake_case。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

GoalPhase = Literal["active", "paused", "blocked", "complete"]
GoalActivation = Literal["armed", "disarmed"]

#: 原版 ``GoalPhase`` 的合法值集合。
PHASES: frozenset[str] = frozenset({"active", "paused", "blocked", "complete"})


@dataclass(frozen=True)
class GoalRef:
    """一次精确版本的比较并交换身份（原版 ``GoalRef``）。"""

    id: str
    revision: int

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}


@dataclass(frozen=True)
class GoalBlockReason:
    """blocked 的机器分类 ＋ 人读解释（原版 ``GoalBlockReason``）。"""

    code: str
    message: str

    def as_json(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class GoalSnapshot:
    """每次非 clear 变更写入的完整 durable 状态（原版 ``GoalSnapshot``）。"""

    id: str
    revision: int
    objective: str
    phase: GoalPhase
    max_goal_rounds: int
    blocked_reason: Optional[GoalBlockReason] = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "revision": self.revision,
            "objective": self.objective,
            "phase": self.phase,
            "maxGoalRounds": self.max_goal_rounds,
        }
        if self.phase == "blocked":
            # 原版：blockedReason 只在与 phase=blocked 同时出现（键集合精确匹配）。
            out["blockedReason"] = (
                self.blocked_reason.as_json() if self.blocked_reason else {"code": "", "message": ""}
            )
        return out

    def ref(self) -> GoalRef:
        return GoalRef(self.id, self.revision)


@dataclass(frozen=True)
class GoalView:
    """当前投影（含从日志推导的计数）＋ 进程内 activation（原版 ``GoalView``）。"""

    id: str
    revision: int
    objective: str
    phase: GoalPhase
    max_goal_rounds: int
    rounds_started: int
    created_at: float
    updated_at: float
    activation: GoalActivation
    blocked_reason: Optional[GoalBlockReason] = None

    def ref(self) -> GoalRef:
        return GoalRef(self.id, self.revision)

    def snapshot(self) -> GoalSnapshot:
        return GoalSnapshot(
            id=self.id, revision=self.revision, objective=self.objective,
            phase=self.phase, max_goal_rounds=self.max_goal_rounds,
            blocked_reason=self.blocked_reason,
        )


@dataclass(frozen=True)
class CreateGoalRequest:
    objective: str
    max_goal_rounds: Optional[int] = None


@dataclass(frozen=True)
class EditGoalRequest:
    objective: Optional[str] = None
    max_goal_rounds: Optional[int] = None


@dataclass(frozen=True)
class GoalMessageSource:
    """续轮消息的来源标记（原版 ``GoalMessageSource``）。"""

    goal_id: str
    revision: int
    round: int

    def as_json(self) -> dict[str, Any]:
        return {"kind": "goal", "goalId": self.goal_id, "revision": self.revision, "round": self.round}


@dataclass(frozen=True)
class GoalChanged:
    """一次 durable 变更提交后的活通知（原版 ``GoalChanged``）。"""

    operation: str
    ref: GoalRef
    goal: Optional[GoalView] = None
