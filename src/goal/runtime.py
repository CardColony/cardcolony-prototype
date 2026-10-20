"""运行时构造与协议常量 —— 对应 ``goal/src/runtime.ts``。"""

from __future__ import annotations

from typing import Final

#: ``goal/change`` 载荷版本（与原版 GOAL_CHANGE_VERSION 同值）。
GOAL_CHANGE_VERSION: Final[int] = 1

#: 会话事件类型名（原版 session event ``goal/change``）。
GOAL_CHANGE_EVENT: Final[str] = "goal/change"

#: 续轮消息在会话日志里的事件类型（原版 ``user/message``）。
USER_MESSAGE_EVENT: Final[str] = "user_message"

#: 部署缺省轮次上限（原版 ``defaultMaxGoalRounds: 256``）。
DEFAULT_MAX_GOAL_ROUNDS: Final[int] = 256

#: blocked 阈值缺省（原版 ``blockedAfterConsecutiveRounds: 3``）。
DEFAULT_BLOCKED_AFTER_ROUNDS: Final[int] = 3

#: 原版 GoalErrorCode 全集。
GOAL_ERROR_CODES: Final[frozenset[str]] = frozenset({
    "GOAL_AGENT_NOT_LIVE",
    "GOAL_NOT_FOUND",
    "GOAL_ALREADY_EXISTS",
    "GOAL_STALE_REVISION",
    "GOAL_INVALID_OBJECTIVE",
    "GOAL_INVALID_MAX_ROUNDS",
    "GOAL_INVALID_BLOCK_REASON",
    "GOAL_INVALID_EDIT",
    "GOAL_INVALID_TRANSITION",
})


class GoalError(Exception):
    """goal 域边界错误（原版 ``GoalError extends HarnessError``）。

    ``code`` 是稳定的机器可路由分类，调用方（工具/命令）据此决定怎么回话。
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class GoalStreamError(Exception):
    """重放校验失败（原版 fold 抛裸 ``Error``，此处单列以免与域错误混淆）。"""


def goal_id(value: str) -> str:
    """原版 ``GoalId`` 是编译期 brand；Python 侧只在边界校验非空。"""
    if not isinstance(value, str) or not value:
        raise GoalError("goal id must be a non-empty string", "GOAL_NOT_FOUND")
    return value
