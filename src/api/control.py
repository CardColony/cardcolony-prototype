# -*- coding: utf-8 -*-
"""控制三态接口（迭代一·会话底座 S7-R3 / 4.7＋4.9，T106-T108，T119-T124）。

`check(work_id, card_id) -> STOP | CONTINUE | DEFER_HUMAN`

三态语义：
- STOP：本回合停止（迭代一唯一真实来源＝人的手动停，T124/X4——
  停续策略判据内容一件不写）；
- CONTINUE：继续；
- DEFER_HUMAN：「判不了交人」本身就是输出（T108）——由类型注册表
  （R1 behavior_registry）判据产生的合法返回值。迭代三注册表已填语义
  （execute/think 两条目）、判据位仍空 ⇒ check 仍恒 CONTINUE；消费侧
  路由保留（轮末 end=policy_stop 闭合），接口非空置。

**与工具卡的共存禁令（任务书 §5-10，防隐性闸门）**：本模块只答「该停吗」，
**不得**以 `toolcard_driver.on_step_boundary` 的返回值为停的依据——
「看到了进度」≠「按进度判停」。两调用在 `message_runner.check_control`
内串行执行、各司其职、互不解释。停的依据只能是：人的手动停（迭代一），
或迭代二注册表里类型自带的完成判据。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..storage import session_facade, session_ops

logger = logging.getLogger("api.control")

STOP = "STOP"
CONTINUE = "CONTINUE"
DEFER_HUMAN = "DEFER_HUMAN"

# 人停置位（consume-once：check 返回 STOP 即清除，不影响下一回合）
_stop_requests: set[tuple[str, str]] = set()

#: 在途回合的协程任务（key=(work_id, card_id)）。
#:
#: [立刻停止 2026-10-21 · 用户令] 改前「停止」只往 `_stop_requests` 放个标记，
#: 靠 `check()` 在**步边界**消费。而一轮模型调用可能跑几分钟——步边界恰恰在
#: 最需要停的时候等不到。于是「明明点了停止，后端还在跑，发消息 409」。
#: 行业做法（OpenAI 的 cancel run、Claude Code 的 Esc、HTTP 的 abort）都是
#: **立刻中断正在进行的 I/O**。本机一直有这条通道：取消协程即可。
#: 所以停止端点直接 cancel 这个 task，`_run_message` 的 finally 照常释放互斥体。
_turn_tasks: dict[tuple[str, str], Any] = {}


def bind_turn_task(work_id: str, card_id: str, task: Any) -> None:
    """登记在途回合的协程（`_run_message` 入口调用）。"""
    _turn_tasks[(work_id, card_id)] = task


def unbind_turn_task(work_id: str, card_id: str, task: Any = None) -> None:
    """解登记（回合 finally 调用）。传 task 时只解自己那一个，防误删后续回合。"""
    key = (work_id, card_id)
    if task is None or _turn_tasks.get(key) is task:
        _turn_tasks.pop(key, None)


def cancel_turn(work_id: str, card_id: str) -> bool:
    """立刻掐断在途回合（返回是否真的取消了）。

    同一个协程里调（内部调用 / 单测直接在回合 task 里调）**不能 cancel 自己**——
    那等于自杀，回合连 finally 都走不完。这种情况返回 False，退回「置位等步边界」。
    生产里停止端点跑在**另一个**请求 task 里，所以永远走得到 cancel。
    """
    task = _turn_tasks.get((work_id, card_id))
    if task is None or task.done():
        return False
    if task is asyncio.current_task():
        logger.debug("[control] 停止请求与回合同协程，退回置位 work=%s card=%s",
                     work_id, card_id)
        return False
    task.cancel()
    logger.info("[control] 立刻停止（cancel 协程）work=%s card=%s", work_id, card_id)
    return True


#: 软停等不到边界时，多久之后硬取消（秒）。
#: 软停是「下一个 chunk / 步边界就断」，正常吐字时毫秒级生效、还能保住已产出的字；
#: 但模型**一个字都不吐**卡在那里时，边界永远不会来 —— 这时候必须硬掐。
STOP_HARD_CANCEL_DELAY = 1.5


async def _hard_cancel_after(work_id: str, card_id: str, holder: str,
                             delay: float | None = None) -> None:
    """软停兜底：``delay`` 秒后这一轮还没收口 ⇒ 直接 cancel 协程。

    比对 ``holder`` 再动手：期间若已换了一轮（或已释放），绝不误杀。
    ``delay`` 缺省时**现读**模块常量（测试可 monkeypatch 缩短）。
    """
    delay = STOP_HARD_CANCEL_DELAY if delay is None else float(delay)
    await asyncio.sleep(delay)
    if session_facade.turn_holder(work_id, card_id) != holder:
        return
    if cancel_turn(work_id, card_id):
        logger.warning("[control] 软停 %.1fs 未收口 ⇒ 硬取消 work=%s card=%s",
                       delay, work_id, card_id)


def request_stop(work_id: str, card_id: str) -> dict[str, Any]:
    """人停（stop 端点唯一入口）。

    [立刻停止 2026-10-21 · 用户令] 两条腿并行，不再只靠那条等不到的：
      ① **软停**：置位，下一个 chunk / 步边界即断（毫秒级，且保住已产出的字——
         2026-10-15 裁定「不丢已产出的字」不推翻）；
      ② **硬停兜底**：1.5 秒还没收口 ⇒ cancel 协程。模型一个字都不吐、卡在
         流里时，软停永远等不到边界（实测 card_031 卡 9 分钟，停止无效、409 发不出）。
    """
    holder = session_facade.turn_holder(work_id, card_id)
    if not holder:
        return {"stopped": False, "reason": "no_inflight_turn",
                "idempotent": True, "holder": ""}
    _stop_requests.add((work_id, card_id))
    try:
        asyncio.get_running_loop().create_task(
            _hard_cancel_after(work_id, card_id, holder))
    except RuntimeError:
        # 没有事件循环（同步上下文调用）⇒ 只能立刻硬取消
        cancel_turn(work_id, card_id)
    session_ops.ops_log("human_stop", work_id, card_id,
                        {"action": "requested", "holder": holder})
    logger.info("[control] 人停 work=%s card=%s holder=%s（软停＋1.5s 硬停兜底）",
                work_id, card_id, holder)
    return {"stopped": True, "reason": "stop_requested", "holder": holder}


def check(work_id: str, card_id: str) -> str:
    """步间控制判定（message_runner.check_control 内串行消费）。
    消费即清（consume-once）：STOP 只终止当前回合，下一回合不受影响。"""
    if (work_id, card_id) in _stop_requests:
        _stop_requests.discard((work_id, card_id))
        logger.info("[control] STOP 消费 work=%s card=%s", work_id, card_id)
        return STOP
    # 迭代三：注册表（R1）已填语义（execute/think）、判据位仍空 → 无
    # DEFER_HUMAN 判据来源 → 恒 CONTINUE
    return CONTINUE


def discard_stop(work_id: str, card_id: str) -> None:
    """[A8 清扫位] 清掉未被步边界消费的残留停止置位。

    置位只在 step 边界被消费；若置位发生在回合**再无步边界**的区段
    （典型：模型调用进行中置位、调用返回后无工具直接交付——入口消费点
    已过、出口消费点不经过），残留会被下一回合的入口 check 误消费，
    违背 T124「consume-once 不影响下一回合」。故回合收口（互斥体释放位）
    统一清扫：置位在回合窗口内有效、回合结束即作废。
    """
    _stop_requests.discard((work_id, card_id))


def pending_stop(work_id: str, card_id: str) -> bool:
    """诊断用：当前是否有未消费的停止置位（不改状态）。"""
    return (work_id, card_id) in _stop_requests


# ===========================================================================
# [自动模式 2026-10-17 · 回灌 5700 块0.3 · 方案 §3.7 / 任务书 §块3]
# 停止扩为**三件**（缺一即失效）
#
#   ① 停当前这一轮   —— 现有 ``request_stop``（consume-once 置位，步边界消费）
#   ② 关自动继续     —— ``goal_state → paused``；否则驱动器立刻又起一轮，等于没停
#   ③ 级联停自动子卡 —— 否则你按了停，子卡还在后台转
#   （附带）释放执行锁 —— 否则串行队列后面全部饿死（方案 §8）
#
# 三件由 ``routes.post_work_stop`` 一次调齐：``request_stop`` ＋ ``stop_auto``。
# ===========================================================================

def _auto_subtree(work_id: str, card_id: str) -> list[str]:
    """本卡子树内**全部 auto 卡**的 card_id（含自身）。走已加载的卡 meta，零逐卡连接。"""
    from ..storage import card_store

    try:
        metas = card_store.iter_card_metas(work_id)
    except Exception:  # noqa: BLE001 读不出 ⇒ 只处理自身
        metas = {}
    # 沿 parent 链收集后代（visited 防环）
    children: dict[str, list[str]] = {}
    for cid, m in metas.items():
        children.setdefault(str(m.get("parent") or ""), []).append(cid)
    out: list[str] = []
    stack = [card_id]
    seen: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        m = metas.get(cur)
        if cur == card_id or (m is not None and bool(m.get("auto_mode"))):
            out.append(cur)
        stack.extend(children.get(cur, []))
    return out


async def stop_auto(work_id: str, card_id: str) -> dict[str, Any]:
    """自动模式停止的 ②③ 两件 ＋ 释放执行锁。**幂等**（无 auto 卡 ⇒ 全 no-op）。

    - ② 本卡（若 auto）``goal_state → paused``；
    - ③ 子树内**自动子卡**一并 ``goal_state → paused``（级联）；
    - 释放该 work 的执行锁（串行模式下队列不饿死）。
    """
    from ..storage import card_store
    from . import serial_gate

    paused: list[str] = []
    cleared_await: list[str] = []
    for cid in _auto_subtree(work_id, card_id):
        m = card_store._meta_of(work_id, cid) or {}
        if not bool(m.get("auto_mode")):
            continue
        was_active = str(m.get("goal_state") or "") == "active"
        raw_await = m.get("goal_await_children")
        try:
            has_await = int(raw_await or 0) != 0
        except (TypeError, ValueError):
            has_await = bool(raw_await)
        if not was_active and not has_await:
            continue        # 既不用置 paused、也没等待意图 ⇒ 逐字维持原 no-op
        # [6702 移植 · 红线] 用户按停优先：``goal_await_children`` 是模型自己声明的
        # 「我在等子卡」意图，用户一按停就作废——之后子卡再完工也绝不唤醒本卡。
        # ★ 不能只清「被本次置 paused 的卡」：按停时可能已有一张 paused 且标志位=1
        #   的卡（模型先 pause 等子卡，用户随后按停），沿用原跳过分支它的等待意图
        #   就永远留着，子卡一完工便复活——那才是真红线破口。
        fields: dict[str, Any] = {}
        note = ""
        if was_active:
            fields["goal_state"] = "paused"
            note = "[自动模式] 停止：关自动继续"
        if has_await:
            fields["goal_await_children"] = 0
            note = (note + " ｜ " if note else "") + "取消「等子卡」等待意图（用户按停优先）"
        try:
            await card_store.update_card(work_id, cid, fields, note)
            if was_active:
                paused.append(cid)
            if has_await:
                cleared_await.append(cid)      # 成功后才记（失败不谎报已清）
        except Exception as exc:  # noqa: BLE001 停不动不抛（停止路径必须走完）
            logger.warning("[control] 关自动继续失败 work=%s card=%s: %s",
                           work_id, cid, exc)
    released = serial_gate.release_work(work_id)
    if paused or cleared_await or released:
        session_ops.ops_log("auto_stop", work_id, card_id,
                            {"paused": paused, "lock_released": released,
                             "cleared_await": cleared_await})
    logger.info("[control] 自动模式停止 work=%s card=%s paused=%s released=%s cleared_await=%s",
                work_id, card_id, paused, released, cleared_await)
    return {"paused": paused, "lock_released": released}
