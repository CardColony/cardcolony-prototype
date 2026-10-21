"""续轮驱动 —— 对应 ``goal-round-driver/src/index.ts``（445 行，逐条移植）。

**这是"自动模式会不断唤醒"的机制本体。** 原版的核心不是"回合尾部的顺序调用"，
而是：

    ctx.on('agent/status', ({status}) => { if (status === 'idle') requestDrive(state) })

任何一次回到空闲（正常收口 / 出错 / 被取消）都会重新评估"要不要再起一轮"。
我们这里把"回合结束"映射成 ``notify_idle(work, card, outcome)``，由
``routes._run_message`` 在 **finally 里、互斥体释放之后** 调用——三条路径全覆盖。

逐条对应：
- ``drive``：active + armed + 有轮次预算 ⇒ 预留 round = roundsStarted+1
- 轮次耗尽 ⇒ ``block(code='round-limit')``
- 排队失败 ⇒ ``block(code='queue-failed')``
- 回合出错 ⇒ ``disarm``
- 回合被取消：attempt 已 claim/admitted ⇒ **pause**（不能自动重启），否则 disarm
- 人插话 ⇒ ``competing_queued``，自动轮让位；该回合 idle 时清掉
- 装载时不继承任何自动权威 ⇒ 全部 disarmed（activation 本来就不落盘）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .prompt import render_goal_round_prompt
from .runtime import GoalError
from .service import get_service
from .types import GoalMessageSource

logger = logging.getLogger(__name__)


@dataclass
class RoundAttempt:
    """一次已预留、可能还在队列里的续轮（原版 ``RoundAttempt``）。"""

    goal_id: str
    revision: int
    round: int
    content: str
    phase: str = "queued"       # queued | claimed | admitted
    cancelled: bool = False
    stale: bool = False


@dataclass
class DriverState:
    """单个 (work, card) 的进程内调度状态（原版 ``DriverState``）。"""

    attempt: Optional[RoundAttempt] = None
    competing_queued: bool = False
    needs_checkpoint: bool = False
    requested: bool = False
    run: Optional[asyncio.Task] = None
    stopping: bool = False


_states: dict[tuple[str, str], DriverState] = {}


def _state_for(work_id: str, card_id: str) -> DriverState:
    key = (work_id, card_id)
    st = _states.get(key)
    if st is None:
        st = DriverState()
        _states[key] = st
    return st


def state_of(work_id: str, card_id: str) -> DriverState:
    return _state_for(work_id, card_id)


# ---------------------------------------------------------------- 取消 / 出错
def disarm(work_id: str, card_id: str) -> None:
    """撤掉本卡的进程内续轮权威（原版 ``disarm``，不改 durable phase）。"""
    st = _state_for(work_id, card_id)
    st.attempt = None
    st.requested = False
    try:
        asyncio.get_running_loop().create_task(get_service().disarm(work_id, card_id))
    except RuntimeError:
        pass


def notify_human_input(work_id: str, card_id: str) -> None:
    """人类消息进入 ⇒ 自动轮让位（原版 ``agent/inbox/inserted`` 的 competingQueued）。"""
    st = _state_for(work_id, card_id)
    st.competing_queued = True
    if st.attempt is not None and st.attempt.phase == "queued":
        st.attempt.stale = True


# ---------------------------------------------------------------- 回合结束（idle 事件）
def notify_idle(work_id: str, card_id: str, outcome: str = "ok") -> None:
    """**回合结束事件**：成功/出错/取消三条路径都走这里。

    ``outcome``：``ok`` / ``error`` / ``aborted`` / ``max-tokens``。
    """
    st = _state_for(work_id, card_id)
    st.competing_queued = False
    if outcome in ("error", "max-tokens"):
        # 原版：agent/error 与 turn/end(max-tokens) 都 ⇒ disarm（异常结果不自动重试）
        logger.warning("[goal] 回合异常/截断，撤销续轮 card=%s outcome=%s", card_id, outcome)
        disarm(work_id, card_id)
        return
    if outcome == "aborted":
        attempt = st.attempt
        if attempt is not None and attempt.phase in ("claimed", "admitted"):
            attempt.cancelled = True
            asyncio.get_running_loop().create_task(_pause_cancelled(work_id, card_id))
        else:
            disarm(work_id, card_id)
        return
    request_drive(work_id, card_id)


async def _pause_cancelled(work_id: str, card_id: str) -> None:
    """取消的那一轮 ⇒ pause 目标，防止它被自动重启（原版 idle 检查点里的 pause）。"""
    st = _state_for(work_id, card_id)
    attempt = st.attempt
    st.attempt = None
    service = get_service()
    try:
        goal = await service.get(work_id, card_id)
        if goal is None or goal.phase != "active":
            return
        if attempt is not None and (goal.id != attempt.goal_id or goal.revision != attempt.revision):
            return
        await service.pause(work_id, card_id, goal.ref())
        logger.info("[goal] 取消后已 pause card=%s round=%s", card_id, attempt.round if attempt else "?")
    except GoalError as exc:
        logger.warning("[goal] 取消后 pause 失败（改为 disarm）card=%s: %s", card_id, exc)
        await service.disarm(work_id, card_id)


# ---------------------------------------------------------------- 驱动
def request_drive(work_id: str, card_id: str) -> None:
    """合并触发：同一卡同一时刻只跑一个驱动协程（原版 ``requestDrive``）。"""
    st = _state_for(work_id, card_id)
    if st.stopping:
        return
    st.requested = True
    if st.run is not None and not st.run.done():
        return
    try:
        st.run = asyncio.get_running_loop().create_task(_run_loop(work_id, card_id, st))
    except RuntimeError:
        st.run = None


async def _run_loop(work_id: str, card_id: str, st: DriverState) -> None:
    try:
        while st.requested and not st.stopping:
            st.requested = False
            try:
                await drive(work_id, card_id, st)
            except Exception as exc:  # noqa: BLE001 驱动失败 ⇒ disarm（原版同）
                logger.warning("[goal] 驱动异常，撤销续轮 card=%s: %s", card_id, exc)
                await get_service().disarm(work_id, card_id)
    finally:
        st.run = None
        if st.requested and not st.stopping:
            request_drive(work_id, card_id)


def _yield_to_children(work_id: str, card_id: str) -> bool:
    """**我们的层，不在原版 goal 包里**：有未终止子卡 ⇒ 父卡让位。

    原版用 subagent family 表达父子（子 agent 在一次工具调用里阻塞返回），
    我们的卡树是"建完子卡就结束回合、等回报"，所以必须在这里显式让位；
    否则父卡的 goal 是 active+armed，驱动会一直给它续轮，与子卡抢。
    """
    try:
        from ..api import auto_driver
        return auto_driver.has_active_child(work_id, card_id)
    except Exception:  # noqa: BLE001 判不出来就不让位（宁可按原版跑）
        return False


async def drive(work_id: str, card_id: str, st: Optional[DriverState] = None) -> None:
    """空闲检查点：有 active+armed 目标且还有预算 ⇒ 预留下一轮。"""
    st = st or _state_for(work_id, card_id)
    if st.stopping or st.competing_queued:
        return
    service = get_service()
    goal = await service.get(work_id, card_id)
    if goal is None or goal.phase != "active" or goal.activation != "armed":
        logger.debug("[goal] drive 跳过 card=%s goal=%s", card_id,
                     None if goal is None else (goal.phase, goal.activation))
        return
    if goal.rounds_started >= goal.max_goal_rounds:
        await service.block(work_id, card_id, goal.ref(), "round-limit",
                            f"Goal reached its configured limit of {goal.max_goal_rounds} rounds.")
        return
    if _yield_to_children(work_id, card_id):
        logger.debug("[goal] drive 让位给子卡 card=%s", card_id)
        return

    round_no = goal.rounds_started + 1
    content = render_goal_round_prompt(goal, round_no)
    source = GoalMessageSource(goal_id=goal.id, revision=goal.revision, round=round_no)
    st.attempt = RoundAttempt(goal_id=goal.id, revision=goal.revision, round=round_no,
                              content=content, phase="queued")
    try:
        await _spawn_goal_round(work_id, card_id, content, source)
    except Exception as exc:  # noqa: BLE001 排队失败 ⇒ block(queue-failed)
        st.attempt = None
        logger.warning("[goal] 续轮排队失败 card=%s round=%s: %s", card_id, round_no, exc)
        latest = await service.get(work_id, card_id)
        if latest is not None and latest.phase == "active" and latest.id == goal.id:
            await service.block(work_id, card_id, latest.ref(), "queue-failed",
                                f"Could not queue goal round {round_no}: {exc}")


async def _spawn_goal_round(work_id: str, card_id: str, content: str,
                            source: GoalMessageSource) -> None:
    """起一轮：与人类回合同一条链路，只多了 ``source``（原版 ``Agent.followup``）。"""
    from ..api import message_runner
    from ..api import routes as _routes
    from ..storage import event_log

    # [轮次脱钩 2026-10-20] 先把这一轮记进事件流（几十字节），再起轮。
    # 会话里**一个字都不写**：目标/目标态/已跑轮次已由 system prompt 的
    # 「自动模式·目标」区每轮渲染，塞进会话只会逐轮累积同一份文案
    # （实测 card_005 41 轮 = 30KB 重复目标）。
    await event_log.append(work_id, card_id, {
        "run_id": "", "round": 0, "type": "goal_round",
        "data": {"goalId": source.goal_id, "revision": source.revision,
                 "round": source.round},
    })
    message_runner._spawn_tracked(  # noqa: SLF001 与 auto_driver 同一条起轮路径
        _routes._run_message(work_id, card_id, content, None,
                             append_user=False, source=source.as_json())
    )
    print(f"[goal] 续轮已排队 card={card_id} round={source.round}", flush=True)


# ---------------------------------------------------------------- 回合入口校验
def validate_reservation(work_id: str, card_id: str,
                         source: dict[str, Any]) -> tuple[bool, str]:
    """回合**进入模型之前**校验：这一轮还是不是当前那一轮（原版 pre-step）。

    返回 ``(ok, reason)``；``ok=False`` 时调用方必须**不调模型**、直接结束回合。
    """
    st = _state_for(work_id, card_id)
    attempt = st.attempt
    if attempt is None:
        return False, "no reservation"
    if attempt.phase not in ("queued", "claimed"):
        return False, f"attempt phase {attempt.phase}"
    if attempt.stale or attempt.cancelled:
        return False, "stale or cancelled"
    if (source.get("goalId") != attempt.goal_id
            or source.get("revision") != attempt.revision
            or source.get("round") != attempt.round):
        return False, "reservation mismatch"
    return True, ""


def mark_admitted(work_id: str, card_id: str, source: dict[str, Any]) -> None:
    """消息被接纳（已落 user_message 事件）⇒ attempt 进入 admitted。"""
    st = _state_for(work_id, card_id)
    attempt = st.attempt
    if attempt is not None and source.get("round") == attempt.round:
        attempt.phase = "admitted"


def drop_attempt(work_id: str, card_id: str) -> None:
    st = _state_for(work_id, card_id)
    st.attempt = None


# ---------------------------------------------------------------- 父卡唤醒（我们的层）
def notify_parent_idle(work_id: str, card_id: str) -> None:
    """子卡**终止**（complete/blocked/paused）时，叫父卡重新评估。

    原版没有这一步——父子由 subagent family 表达（子 agent 在工具调用里阻塞返回，
    父回合自然继续）。我们的卡树是"建完就走"，所以子卡收口必须显式叫醒父卡，
    否则父卡会一直让位等一个已经终止的子卡。**这是我们的层，不在 goal 包里。**
    """
    try:
        asyncio.get_running_loop().create_task(_notify_parent(work_id, card_id))
    except RuntimeError:
        pass


async def _notify_parent(work_id: str, card_id: str) -> None:
    try:
        from ..storage import card_store
        meta = card_store._meta_of(work_id, card_id) or {}
        if not meta.get("auto_mode"):
            return
        goal = await get_service().get(work_id, card_id)
        if goal is not None and goal.phase == "active":
            return          # 本卡还没终止 ⇒ 父卡继续让位
        parent = str(meta.get("parent") or "")
        if not parent or parent == card_id:
            return
        parent_meta = card_store._meta_of(work_id, parent) or {}
        if not parent_meta.get("auto_mode"):
            return
        request_drive(work_id, parent)
    except Exception as exc:  # noqa: BLE001 叫醒失败不阻断
        logger.warning("[goal] 叫醒父卡失败 card=%s: %s", card_id, exc)


# ---------------------------------------------------------------- 装载
_INSTALLED = False


def _on_goal_changed(work_id: str, card_id: str, change: Any) -> None:
    """``goal/changed`` ⇒ 重新评估续轮（原版 driver 的 goal/changed 监听）。

    没有这一条，`/goal resume` 只把 activation 置成 armed，却没有任何东西去点火
    ——因为此刻没有回合在跑，也就没有 idle 事件可等。
    """
    request_drive(work_id, card_id)


def install() -> None:
    """装载驱动：订阅 goal/changed。进程启动时调一次（原版 plugin apply）。"""
    global _INSTALLED
    if _INSTALLED:
        return
    get_service().on_changed(_on_goal_changed)
    _INSTALLED = True
    print("[goal] 续轮驱动已装载（goal/changed 监听已挂）", flush=True)
