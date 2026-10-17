# -*- coding: utf-8 -*-
"""自动模式 · 轮次驱动器（回灌 5700 任务书 §块0.3 / 迭代方案 §3.3）。

【业界】dsh 的 ``goal-round-driver``：agent 空闲 ＋ 目标 ``active`` ⇒ 自动起下一轮；
「停」由模型**显式声明**（完成/暂停/卡住）；异常结果一律不自动重试。

【本系统】映射到「一张卡」：
- **接线点**＝``routes._run_message`` 的回合收口，且**必须排在回合互斥体释放之后**
  （否则下一轮 ``turn_try_acquire`` 直接 409，驱动器等于空转——红线 5）。
- **规则**（全部读卡上字段，不猜）：``auto_mode=1`` ＋ ``goal_state=="active"`` ＋
  有 ``goal`` ＋ **无未终止子卡** ⇒ ``goal_rounds += 1`` ⇒ 起下一轮。
- **父卡让位**（方案 §3.8③）：父卡若有子卡目标未终止 ⇒ 父卡不起轮（否则父卡的
  驱动器会在子卡还在跑时不停给自己起轮——空转、烧额度）。子卡全部终止 ⇒ 父卡恢复。
- **不设任何上限**（U4/U5）：轮次 / 墙钟 / token 全不设。本模块唯一一个「上限」常量是
  ``_CARD_TREE_CYCLE_GUARD``——它是**卡树防环兜底**（环检测第二道，第一道在
  ``card_store`` 的 parent 防环），**不是闸**：不参与「能跑多少轮」的任何判定。
- **异常不自动重试**：``_run_message_inner`` 抛异常时本模块不被调到（异常沿原路径
  上抛），回合自然终止。

**分层**：本模块**只在函数内**延迟 import ``routes`` / ``message_runner``——
``card_tools`` 会 import 本模块，而 ``routes`` 会 import ``card_tools``，模块级
互拉会成环（沿用 ``message_runner`` 对 ``routes`` 的既有延迟引用手法）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..storage import card_store, session_facade

logger = logging.getLogger("api.auto_driver")

#: 目标态四值（照 dsh）。``active`` ＝推进中；其余三态＝**已终止**（驱动器不再起轮）。
GOAL_ACTIVE = "active"
GOAL_PAUSED = "paused"
GOAL_BLOCKED = "blocked"
GOAL_COMPLETE = "complete"

GOAL_STATES: tuple[str, ...] = (GOAL_ACTIVE, GOAL_PAUSED, GOAL_BLOCKED, GOAL_COMPLETE)

#: 卡树**防环**上限 —— **不是闸**（任务书 §四 要求把语义写死）。
#:
#: 它**是什么**：``after_turn`` 沿 parent 链向上唤醒父卡时的**环检测第二道**。
#:   第一道在存储层：``card_store.update_card`` 拒「parent 指向自身或后代」
#:   （``_is_descendant`` 防环），故**正常卡树不会触到本值**；本值只兜底
#:   「parent 链被绕过写出了环」这种不可能态，防止唤醒递归打转。
#:
#: 它**不是什么**（无闸铁律 · 红线 1）：**不是轮次闸、不是进度限制、不是时间闸、
#:   不是 token 上限、不是「最多跑 N 轮」**。它与「一张卡能跑多少轮」**毫无关系**
#:   ——跑多少轮由模型用 ``update_goal`` 说了算，本值一个字都不参与判定。
_CARD_TREE_CYCLE_GUARD = 32

# ============================================================
# [自动模式 2026-10-17 · 回灌 5700 阶段2.7 · TC3] 空转兜底（**止损，不是闸**）
#
# 用户愿景是「不设闸、观察模型自然行为」（U4），但「连续 N 轮一个工具都不调、
# 光说话」是明确的故障态（模型在原地打转/复读），继续烧额度无信息量。
# 故设一道**止损**：单卡连续 _IDLE_PAUSE_THRESHOLD 轮零工具调用 ⇒ 自动
# goal_state=paused ＋ 打点，交人工看。它**不是轮次/墙钟/token 闸**——不限制
# 「能跑多少轮」，只处理「空转」这一种病态（有工具调用的轮永不计入）。
#
# goal_rounds 告警线：超 _ROUNDS_WARN 只告警**不拦**（红线 1：不设轮次上限）。
# ============================================================
_IDLE_PAUSE_THRESHOLD = 3
_ROUNDS_WARN = 200

#: 进程内连续空转计数：(work_id, card_id) → 连续零工具轮数。
_IDLE_ROUNDS: dict[tuple[str, str], int] = {}


async def _idle_stop(work_id: str, card_id: str, tool_n: Optional[int]) -> bool:
    """空转兜底：连续零工具轮达阈值 ⇒ 自动 pause ＋ 打点。返回是否已暂停。

    ``tool_n=None`` ⇒ 无本轮信息（直接调 after_turn 的场景）⇒ 不判定、不清零
    （避免把「不知道」误算成空转）。
    """
    if tool_n is None:
        return False
    key = (work_id, card_id)
    if tool_n > 0:
        _IDLE_ROUNDS.pop(key, None)
        return False
    n = _IDLE_ROUNDS.get(key, 0) + 1
    _IDLE_ROUNDS[key] = n
    if n < _IDLE_PAUSE_THRESHOLD:
        return False
    _IDLE_ROUNDS.pop(key, None)
    try:
        await card_store.update_card(
            work_id, card_id, {"goal_state": GOAL_PAUSED},
            f"[自动模式] 空转兜底：连续 {n} 轮无工具调用，已自动暂停",
        )
    except Exception as exc:  # noqa: BLE001 兜底失败不阻断（只是没停住）
        logger.warning("[auto] 空转暂停失败 work=%s card=%s: %s", work_id, card_id, exc)
        return False
    logger.warning("[auto] 空转兜底触发 work=%s card=%s 连续 %d 轮零工具",
                   work_id, card_id, n)
    return True


def _meta(work_id: str, card_id: str) -> dict[str, Any]:
    try:
        return card_store._meta_of(work_id, card_id) or {}
    except Exception:  # noqa: BLE001 读不出 meta ⇒ 当作「没有」，不起轮
        return {}


def is_auto(meta: dict[str, Any]) -> bool:
    return bool((meta or {}).get("auto_mode"))


def has_active_child(work_id: str, card_id: str) -> bool:
    """本卡是否有**未终止子卡**（直接子卡中 ``goal_state == "active"`` 者）。

    【口径】「未终止」＝``goal_state ∈ {active}``；complete / blocked /
    paused 视为已终止。「有子卡但子卡无目标」（goal_state 空）**不计入**——
    本任务按此口径实现（若需扩口径，另行裁定）。

    取数走 ``iter_card_metas``（**已加载的卡 meta**，一次查询）——**不逐卡开连接**
    （迭代方案 §8 明令）。
    """
    try:
        metas = card_store.iter_card_metas(work_id)
    except Exception:  # noqa: BLE001 读不出 ⇒ 不阻塞（宁可起轮，不可空转）
        return False
    for cid, m in metas.items():
        if cid == card_id:
            continue
        if str(m.get("parent") or "") != card_id:
            continue
        if str(m.get("goal_state") or "") == GOAL_ACTIVE:
            return True
    return False


def will_continue(work_id: str, card_id: str) -> bool:
    """本卡此刻是否应当再起一轮（纯判定，无副作用）。**驱动器与执行锁共用同一判据**。"""
    meta = _meta(work_id, card_id)
    if not is_auto(meta):
        return False
    if str(meta.get("goal_state") or "") != GOAL_ACTIVE:
        return False
    if not str(meta.get("goal") or "").strip():
        return False
    if has_active_child(work_id, card_id):
        return False   # ★ 父卡让位
    return True


def goal_prompt(meta: dict[str, Any], goal: str) -> str:
    """下一轮的激励文（只走当轮 user_prompt，``append_user=False`` 不落档案）。"""
    try:
        rounds = int((meta or {}).get("goal_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    return (
        f"（自动模式 · 继续推进）你的目标：{goal}\n"
        f"当前目标状态：active（已跑 {rounds} 轮）。\n"
        "继续做下去。**完成时你必须显式调用 update_goal(complete) 声明完成**——"
        "不声明，系统会自动再起一轮。无法继续时调用 update_goal(blocked, 原因)；"
        "需要停下时调用 update_goal(pause)。"
    )


async def _bump_rounds(work_id: str, card_id: str) -> int:
    """``goal_rounds += 1``（**只做可见性，不参与任何判定、不构成闸**）。"""
    meta = _meta(work_id, card_id)
    try:
        cur = int(meta.get("goal_rounds") or 0)
    except (TypeError, ValueError):
        cur = 0
    nxt = cur + 1
    try:
        await card_store.update_card(
            work_id, card_id, {"goal_rounds": nxt}, f"[自动模式] 第 {nxt} 轮",
        )
    except Exception as exc:  # noqa: BLE001 计轮失败不阻断起轮
        logger.warning("[auto] 计轮失败 work=%s card=%s: %s", work_id, card_id, exc)
    # [阶段2.7 · TC3] 轮次告警线：超阈值**只告警不拦**（红线 1：不设轮次上限）。
    # 每 50 轮告警一次防刷屏，便于人工介入看这卡是不是在长跑。
    if nxt > _ROUNDS_WARN and nxt % 50 == 0:
        logger.warning("[auto] 轮次告警（不拦）work=%s card=%s goal_rounds=%d 超 %d",
                       work_id, card_id, nxt, _ROUNDS_WARN)
    return nxt


def kick(work_id: str, card_id: str) -> Optional["Any"]:
    """点火：起一轮（``append_user=False``——激励文只走当轮 user_prompt，不落档案）。

    前置判据与 ``will_continue`` 同源；**已有在途回合 ⇒ 不起**（防双起轮——与
    既有卡间调起共用同一互斥体语义）。
    """
    meta = _meta(work_id, card_id)
    if not is_auto(meta):
        return None
    if str(meta.get("goal_state") or "") != GOAL_ACTIVE:
        return None
    goal = str(meta.get("goal") or "").strip()
    if not goal:
        return None
    try:
        if session_facade.turn_holder(work_id, card_id):
            logger.info("[auto] 已有在途回合、不起轮 work=%s card=%s", work_id, card_id)
            return None
    except Exception:  # noqa: BLE001 查不出在途 ⇒ 照起（互斥体兜底）
        pass

    from . import message_runner
    from . import routes as _routes

    content = goal_prompt(meta, goal)
    task = message_runner._spawn_tracked(
        _routes._run_message(work_id, card_id, content, None, append_user=False)
    )
    logger.info("[auto] 起轮 work=%s card=%s goal_rounds=%s",
                work_id, card_id, meta.get("goal_rounds"))
    return task


async def after_turn(work_id: str, card_id: str, *, _hops: int = 0,
                     tool_n: Optional[int] = None) -> None:
    """回合收口后的驱动检查（**必须由调用方排在互斥体释放之后**）。

    能继续 ⇒ ``goal_rounds += 1`` ＋ 起下一轮；不能继续 ⇒ 向上唤醒父卡
    （父卡可能因「让位」而停着，子卡全终止后应恢复）。

    ``_hops``：沿 parent 链已上跳层数，**只服务 ``_CARD_TREE_CYCLE_GUARD`` 的
    卡树防环兜底**——不是轮次/进度/时间闸（见该常量）。

    ``tool_n``：**本轮**工具调用次数（来自 finish 响应体的 tool_events 长度）。
    只服务空转兜底（阶段2.7/TC3）：连续零工具轮达阈值 ⇒ 自动 pause。
    None ⇒ 无信息不判定（直接调本函数的场景，行为与 2.7 前逐字一致）。
    """
    meta = _meta(work_id, card_id)
    if not is_auto(meta):
        return
    # [阶段2.7 · TC3] 空转止损：连续零工具轮 ⇒ 暂停（不是闸，见 _idle_stop）
    try:
        if await _idle_stop(work_id, card_id, tool_n):
            return
    except Exception as exc:  # noqa: BLE001 兜底失败不阻断正常驱动
        logger.warning("[auto] 空转兜底异常 work=%s card=%s: %s",
                       work_id, card_id, exc)
    if will_continue(work_id, card_id):
        await _bump_rounds(work_id, card_id)
        kick(work_id, card_id)
        return
    # —— 本卡不再起轮：向上唤醒父卡（只对 auto 树做；非 auto 卡不触发）——
    # _hops ＝ 沿 parent 链已经上跳了几层，仅用于 `_CARD_TREE_CYCLE_GUARD`
    # 的**防环**兜底（**不是轮次/进度/时间闸**，见该常量处说明）。
    if _hops >= _CARD_TREE_CYCLE_GUARD:
        logger.warning("[auto] 卡树防环兜底命中（parent 链疑似成环），停止向上 work=%s card=%s",
                       work_id, card_id)
        return
    parent = str(meta.get("parent") or "")
    if parent and parent != card_id:
        await after_turn(work_id, parent, _hops=_hops + 1)


async def ensure_goal_from_message(work_id: str, card_id: str, content: str) -> bool:
    """auto 卡收到**首条消息** ⇒ 该消息成为 ``goal``、``goal_state=active``（D2 起跑）。

    已有目标（``goal`` 非空）⇒ **不动**——你再发消息是**中途引导**，目标不变
    （迭代方案 §3.4）。返回是否本次设定了目标。
    """
    meta = _meta(work_id, card_id)
    if not is_auto(meta):
        return False
    if str(meta.get("goal") or "").strip():
        return False
    goal = str(content or "").strip()
    if not goal:
        return False
    try:
        await card_store.update_card(
            work_id, card_id, {"goal": goal, "goal_state": GOAL_ACTIVE},
            "[自动模式] 目标设定（首条消息）",
        )
    except Exception as exc:  # noqa: BLE001 设定失败 ⇒ 不起跑，不阻断消息本身
        logger.warning("[auto] 目标设定失败 work=%s card=%s: %s", work_id, card_id, exc)
        return False
    return True


async def kick_if_ready(work_id: str, card_id: str) -> Optional["Any"]:
    """建卡即起跑（D2）：卡是 auto 且有目标 ⇒ 点火。**建卡调用方**在建成后调它。"""
    return kick(work_id, card_id)
