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

from ..storage import card_store, session_facade, session_ops

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

#: [6702 移植 2026-10-17 · 等子卡恢复] 「向上唤醒」判据的三种裁定（**不是闸**）。
#: ``wake_verdict`` 的返回值 —— 「本卡此刻该不该被向上唤醒、以哪种身份被唤醒」。
WAKE_NONE = ""            # 不唤醒（继续沿 parent 链上跳）
WAKE_ACTIVE = "active"    # 常规：goal_state 已是 active ⇒ 走原 ``will_continue`` 路径
WAKE_PAUSED = "paused"    # 本卡自己声明「等子卡」而挂起 ⇒ 先恢复 active 再点火

#: [等子卡活锁修复 2026-10-17] 「等子卡唤醒」死锁兜底阈值 —— **不是闸**。
#:
#: 【它是什么】``goal_await_wakes``（**连续「空等重挂」次数**：模型声明
#: ``pause(await_children=true)`` 而**那一刻并无未终止子卡**——即等待条件早已兑现
#: 却仍把自己挂起——则 +1；声明时确有子卡在跑、或换新目标生命周期，则归零）
#: **达到本值**、且 wake 判定**此刻确无未终止子卡**时，``wake_verdict`` 的
#: ``WAKE_PAUSED`` 分支不再恢复（保持 paused），只留 ``logger.warning`` ＋
#: ``ops_log("auto_await_resume_giveup")`` 痕迹，等人来处置。
#:
#: 【★ 计数点为什么在 pause 声明处，而不是任务书原拟的 ``_resume_awaiting``】
#:   两点理由，缺一不可：
#:   ① **对齐实测序列**：resume 时 +1 会让「第 1、2、3 次恢复都放行、第 4 次才拒」
#:      （r1→wakes1、r2→2、r3→3、第 4 判才 ≥3），而 work_017 的活锁形态与验收口径
#:      都是「第 3 次起不再恢复」。pause 点计数（p1→wakes1、p2→2、第 3 次 pause 后
#:      判定 sees 3）恰好第 3 次拒绝。
#:   ② **不误伤正常循环**：正常的「等→醒→干活→再等」循环里，每次 pause 声明时
#:      **确有子卡在跑**（模型刚派的新活）⇒ 走清零分支，计数永远攒不起来；resume 时
#:      +1 则会让一个完全合法的重复等待循环在第 4 次真等待时被永久卡死。
#:   判别点只能是「pause 声明那一刻有没有未终止子卡」——恢复动作发生时子卡必然
#:   已完工，无从区分真假等待。落地在 ``card_tools._h_update_goal`` 的 pause 分支
#:   （**收口闸一字未动**：complete 的未回应询问拒绝路径不经过此处）。
#:
#: 【它对付的是什么活锁】实测 work_017：两张子卡**早已全部 complete**，父卡却被
#: 子卡完工唤醒 → 恢复 active 起轮 → 模型读到自己上一轮的 reason「等子卡跑完再整合」
#: → **看不到任何变化** → 再次 ``pause(await_children=true)`` → 又被子卡完工唤醒 ⇒
#: 「唤醒-挂起」来回打转、持续烧额度。这不是模型能力问题，是**信息缺失**：
#: ``goal_prompt`` 通篇没告诉模型「你刚被唤醒、因为子卡已全部完工」。
#:
#: 【★ 它为什么不是闸 —— 逐条说清，勿改】
#:   1. **方向不同**：闸是「限制一张卡能跑多少／花多少」；本值只回答「**这一次唤醒动作**
#:      要不要发生」。它出现在 ``wake_verdict`` 里，而 ``wake_verdict`` 的返回值
#:      ``WAKE_*`` **只**被 ``after_turn`` 的「向上唤醒」分支读——**轮次续轮（active）
#:      根本不经过它**（那条路走 ``will_continue``，一个字都没改）。
#:   2. **与进度无关**：它不读任何轮次、时间或用量类阈值，也不看卡片类型与完成度。
#:      它读的唯一输入是「**同一个等待条件已被满足却反复重挂**」这一事实。
#:   3. **不拦任何正常推进**：三道反证 ——（a）本卡若是 ``active``，走
#:      ``WAKE_ACTIVE`` 分支，本值**压根不被读**；（b）**仍有未终止子卡**时判据不成立
#:      （``has_active_child`` 先拦，父卡让位照旧），照常唤醒；（c）计数到多大都不拦
#:      「正常等待-被唤醒」——首次等待、第二次等待（修法后模型通常已完成整合）全部照走。
#:   4. **它有出口**：兜底命中后卡保持 paused 且**留下一条 ops 痕迹**指向人和日志，
#:      不是无声吞掉。卡随时可由人恢复或由模型新的一次显式声明重新进入等待态
#:      （阈值只读不改写）。
#:   5. **与轮次闸的对照**：真正的轮次闸会写「本卡最多跑 N 轮」并因此**减少可跑的轮数**；
#:      本值不减任何一轮——它拦的是**打转的重复唤醒**，且只在「子卡已全完工」这个
#:      等待条件早已兑现之后才生效。
#:
#: 【为什么是 3】主修复（唤醒时带明确事实的激励文）足以让绝大多数模型在第一、二次
#:   唤醒就整合收口；阈值取 3＝「给足两次修法生效的机会，第三次仍原地打转才认定
#:   是结构性死锁」。它**不是**「最多唤醒 3 次」：判定里带着「此刻无未终止子卡」，
#:   有子卡在跑时这个值再大也不生效。
_AWAIT_RESUME_DEADLOCK_GIVEUP = 3


#: [等子卡活锁修复 2026-10-17 · 一次性标记] 恢复轮激励文里的「唤醒事实」暂存位。
#:
#: 【为什么用一次性标记，而不是给 ``kick`` 加参数】
#:   ``kick`` 是**对外**接口：``routes`` 与 ``kick_if_ready``（建卡即起跑）都调它，
#:   给它加形参就把「本模块内部的『恢复轮 vs 常规轮』上下文」泄漏成了所有调用方
#:   都要关心的参数，且每个调用点都得记得传/不传（漏传＝静默退化成旧行为）。改用
#:   模块内一次性标记：``after_turn`` 在恢复成功后置位，``kick`` **consume-once** 取走，
#:   ``kick`` 的对外签名**逐字不变**（向后兼容，零调用点改动）。
#:   消费纪律（同 ``control.check`` 的 consume-once 口径）：``kick`` 一进来就取走，
#:   即使随后因前置判据返回 None 也不留存 ⇒ 绝不会把「上一轮的唤醒事实」带进下一轮。
_PENDING_RESUME_NOTE: Optional[str] = None


def _set_resume_note(note: str) -> None:
    """置位「本轮是等子卡恢复轮」的一次性标记（见 ``_PENDING_RESUME_NOTE``）。"""
    global _PENDING_RESUME_NOTE
    _PENDING_RESUME_NOTE = note


def _take_resume_note() -> str:
    """consume-once 取走唤醒事实标记（取走即清空，失败/早退也不残留）。"""
    global _PENDING_RESUME_NOTE
    note = _PENDING_RESUME_NOTE or ""
    _PENDING_RESUME_NOTE = None
    return note


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


def _await_children_flag(meta: dict[str, Any]) -> bool:
    """读 ``goal_await_children`` 标志位（0/1；**机械读列，不猜**）。

    读侧容 ``1`` / ``"1"`` / ``True``：SQLite INTEGER 列经 JSON/前端往返可能成
    字符串或布尔。全归一成 bool，任一非空非零真值即为「在等子卡」。
    """
    raw = (meta or {}).get("goal_await_children")
    if raw is None or raw == "":
        return False
    if isinstance(raw, str):
        return raw.strip() not in ("", "0", "False", "false", "None")
    try:
        return int(raw) != 0
    except (TypeError, ValueError):
        return bool(raw)


def _await_wakes(meta: dict[str, Any]) -> int:
    """读 ``goal_await_wakes`` 计数（机械读列，不猜；容 None/字符串/异常 → 0）。"""
    raw = (meta or {}).get("goal_await_wakes")
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _giveup_await_resume(work_id: str, card_id: str, meta: dict[str, Any]) -> None:
    """「等子卡唤醒」死锁兜底命中时的**留痕**（不改任何状态，纯可见性）。

    卡保持 ``paused`` —— 本函数只打两条痕迹（``logger.warning`` ＋
    ``ops_log("auto_await_resume_giveup")``），把「为什么没被叫醒」落到日志与
    ops 档案里等人处置。**不改 goal_state、不改计数、不清 await 标志**。
    """
    wakes = _await_wakes(meta)
    reason = str(meta.get("goal") or "")[:200]
    logger.warning(
        "[auto] 等子卡唤醒死锁兜底：不再恢复 work=%s card=%s wakes=%d >= %d 且此刻无未终止子卡"
        "（模型恢复后重复挂起、来回打转；卡片保持 paused，请人工处置）",
        work_id, card_id, wakes, _AWAIT_RESUME_DEADLOCK_GIVEUP)
    try:
        session_ops.ops_log("auto_await_resume_giveup", work_id, card_id, {
            "goal_await_wakes": wakes,
            "threshold": _AWAIT_RESUME_DEADLOCK_GIVEUP,
            "has_active_child": False,
            "goal": reason,
            "action": "kept_paused",
        })
    except Exception as exc:  # noqa: BLE001 留痕失败不阻断（不得吃掉 after_turn 的链）
        logger.warning("[auto] 死锁兜底留痕失败 work=%s card=%s: %s", work_id, card_id, exc)


def wake_verdict(work_id: str, card_id: str) -> str:
    """**向上唤醒专用**判据（纯判定，无副作用）——返回 :data:`WAKE_*` 三值之一。

    【为什么不用 ``will_continue`` 直接判】
    ``will_continue`` 仍**只认 active**，一个字不改——它被 ``routes._run_message``
    的回合末执行锁持有判定（``keep = will_continue(...)``）共用，改它会波及串行
    队列的锁语义。本判据**只**服务 ``after_turn`` 沿 parent 链的「唤醒」这一个动作。

    【缺口背景】父卡 ``update_goal(pause)`` 写「等子卡跑完回报」后恒为 paused；
    ``will_continue`` 判否 ⇒ ``after_turn`` 既不起它、也不再上跳 ⇒ 父卡**永久挂死**
    （实测 work_008：五张子卡全 complete，父卡 rounds 恒 0）。恢复看似"发生过"只是
    因为子卡**主动发了 msg_notify**——**模型行为掩盖了机制缺口**。

    判据＝``auto_mode=1`` ＋ 有 ``goal`` ＋ （``goal_state=="active"``
    **或**（``paused`` 且 ``goal_await_children==1``）） ＋ **无未终止直接子卡**。
    """
    meta = _meta(work_id, card_id)
    if not is_auto(meta):
        return WAKE_NONE
    if not str(meta.get("goal") or "").strip():
        return WAKE_NONE
    state = str(meta.get("goal_state") or "")
    if state == GOAL_ACTIVE:
        verdict = WAKE_ACTIVE
    elif state == GOAL_PAUSED and _await_children_flag(meta):
        verdict = WAKE_PAUSED
    else:
        return WAKE_NONE
    if has_active_child(work_id, card_id):
        return WAKE_NONE   # ★ 父卡让位（子卡还在跑 ⇒ 轮不到父卡）

    # [等子卡活锁修复 2026-10-17 · 机械兜底] 走到这里说明**确无未终止子卡** ——
    # 等待条件早已被满足。此时若本卡已被「等子卡」唤醒过
    # ``_AWAIT_RESUME_DEADLOCK_GIVEUP`` 次以上，说明它**每次被唤醒都重复挂起**、
    # 从不整合（实测 work_017 的活锁）⇒ 不再恢复，保持 paused，等人处置。
    #
    # ★ 它**只**拦这一个动作（WAKE_PAUSED 的恢复），不拦任何正常推进：
    #   · ``WAKE_ACTIVE`` 分支（常规续轮）**在此之前**就 return 了 ⇒ 本判据压根不参与；
    #   · 有未终止子卡时**在上一行**就被父卡让位拦掉 ⇒ 判据不成立，照常让位；
    #   · 它不看轮次/墙钟/token/进度 —— 见常量处的「它为什么不是闸」五条。
    if verdict == WAKE_PAUSED and _await_wakes(meta) >= _AWAIT_RESUME_DEADLOCK_GIVEUP:
        _giveup_await_resume(work_id, card_id, meta)
        return WAKE_NONE   # 保持 paused：不恢复、不起轮（本卡已是终止态，链到此为止）
    return verdict


async def _resume_awaiting(work_id: str, card_id: str) -> bool:
    """恢复「等子卡」挂起中的父卡：``goal_state → active`` 且**清**等待标志位。

    **一次 ``update_card``** 落两个字段（不留中间态）。清标志位的意义：它是**一次性
    意图**——「这次挂起是等子卡」用过即销，不跨目标生命周期留存。

    返回是否恢复成功（失败只留痕不抛——不得吃掉 ``after_turn`` 的整条链）。
    """
    # [等子卡活锁兜底 2026-10-17 · 口径落点说明] 计数 ``goal_await_wakes`` **不在**
    # 恢复时 +1 —— 它在**空等重挂的 pause 声明点**计数（见 ``card_tools._h_update_goal``
    # 的 pause 分支与 ``_AWAIT_RESUME_DEADLOCK_GIVEUP`` 的口径段）。理由：恢复动作本身
    # 无从区分「这次等待是真是假」；而 pause 声明那一刻「有没有未终止子卡」恰是判别点。
    # 本函数只管恢复（goal_state→active ＋ 清 await 意图位），对计数**一字不碰**。
    try:
        await card_store.update_card(
            work_id, card_id,
            {"goal_state": GOAL_ACTIVE, "goal_await_children": 0},
            "[自动模式] 子卡完工，恢复等待中的父卡",
        )
    except Exception as exc:  # noqa: BLE001 恢复失败 ⇒ 不点火（否则状态自相矛盾）
        logger.warning("[auto] 等待中父卡恢复失败 work=%s card=%s: %s",
                       work_id, card_id, exc)
        return False
    return True


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


#: [等子卡活锁修复 2026-10-17] 恢复轮激励文的「唤醒事实」段。抽成常量便于测试按片段
#: 断言，也便于以后改口径时只改一处。**只在校准的恢复轮追加**，常规续轮逐字不变。
_AWAKE_FROM_AWAIT_NOTE = (
    "（唤醒说明）你刚才声明「等子卡跑完」并挂起了；**此刻你等的子卡已全部完工，"
    "你已被唤醒**。\n"
    "请**立刻** read_card 读这些子卡的现状、把它们的结果整合进本卡的现状，然后继续推进。\n"
    "若确有**新的**外部条件要等，改用 `update_goal(pause)`，且**不要**再传 "
    "`await_children=true`——子卡已经全部完工，再传只会把你重新挂起，却等不到任何新东西。"
)


def goal_prompt(meta: dict[str, Any], goal: str,
                resume_note: str = "") -> str:
    """下一轮的激励文（只走当轮 user_prompt，``append_user=False`` 不落档案）。

    [等子卡活锁修复 2026-10-17] ``resume_note``：**可选**的唤醒上下文（缺省空串 ⇒
    输出与改造前**逐字相同**）。命中时在末尾追加一段**明确事实**——「你刚被唤醒、
    因为子卡已全部完工」。

    【为什么必须在这里说这件事（根因，实测 work_017）】
    ``kick`` 用 ``append_user=False``：**激励文不落会话块**，只进当轮 ``user_prompt``。
    所以模型在会话历史里**看不到**任何「你已被唤醒」的痕迹，它读到的仍是上一轮
    自己写的 reason「等子卡跑完再整合」——那个等待条件**早已不成立**了，但模型无从
    得知 ⇒ 再次 ``pause(await_children=true)`` ⇒ 被再次唤醒 ⇒ **活锁打转烧额度**。
    这段文字是「环境状态变化」的唯一传达点，缺它，机械兜底就成了唯一防线。
    """
    try:
        rounds = int((meta or {}).get("goal_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    base = (
        f"（自动模式 · 继续推进）你的目标：{goal}\n"
        f"当前目标状态：active（已跑 {rounds} 轮）。\n"
        "继续做下去。**完成时你必须显式调用 update_goal(complete) 声明完成**——"
        "不声明，系统会自动再起一轮。无法继续时调用 update_goal(blocked, 原因)；"
        "需要停下时调用 update_goal(pause)。"
    )
    return base + ("\n" + resume_note if resume_note else "")


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

    [等子卡活锁修复 2026-10-17] **对外签名逐字不变**（``work_id, card_id``）——
    恢复轮要带的「唤醒事实」由模块内一次性标记传入（见 ``_PENDING_RESUME_NOTE``
    的理由段）：``after_turn`` 置位、本函数**consume-once** 取走。一进来就取走，
    即使随后因任一前置判据早退也不残留 ⇒ 绝不把上一轮的唤醒事实带进下一轮。
    """
    # consume-once：先取走，再走任何前置判据（早退也不残留）
    resume_note = _take_resume_note()
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

    content = goal_prompt(meta, goal, resume_note=resume_note)
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
    # [6702 移植 2026-10-17 · 等子卡恢复] 本卡能起轮但**不该起**（父卡让位），
    # 或本卡已终止——先判本卡是否属「等子卡挂起中」：是 ⇒ 恢复 active **再**点火。
    # ★ 顺序不可反：``kick`` 自己判 ``goal_state != active → 不起``，先 kick 会被
    #   自己的前置判据挡掉；故必须「先 update_card 改回 active，再 kick」。
    if wake_verdict(work_id, card_id) == WAKE_PAUSED:
        if not await _resume_awaiting(work_id, card_id):
            return
        meta = _meta(work_id, card_id)     # 重读：计轮与起轮都按恢复后的新 meta 走
        await _bump_rounds(work_id, card_id)
        # [等子卡活锁修复 2026-10-17] 恢复成功 ⇒ 置位一次性标记，让下面这次 kick
        # 带**唤醒事实**（见 _PENDING_RESUME_NOTE 的理由段：为什么不改 kick 签名）。
        # ★ 顺序不可反：``_resume_awaiting`` 必须先于置位与 kick（kick 自己判
        #   ``goal_state != active → 不起``；且只有真正恢复成功的轮次才配说「你被唤醒」）。
        _set_resume_note(_AWAKE_FROM_AWAIT_NOTE)
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
        # [等子卡活锁兜底 2026-10-17] 新目标＝新生命周期 ⇒ 空等重挂计数**归零**。
        # 语义口径（见 _AWAIT_RESUME_DEADLOCK_GIVEUP）：计数是「连续空等重挂」次数，
        # 换目标意味着旧链断开，上一段生命的死锁证据不应连坐新目标。
        await card_store.update_card(
            work_id, card_id,
            {"goal": goal, "goal_state": GOAL_ACTIVE, "goal_await_wakes": 0},
            "[自动模式] 目标设定（首条消息）",
        )
    except Exception as exc:  # noqa: BLE001 设定失败 ⇒ 不起跑，不阻断消息本身
        logger.warning("[auto] 目标设定失败 work=%s card=%s: %s", work_id, card_id, exc)
        return False
    return True


async def kick_if_ready(work_id: str, card_id: str) -> Optional["Any"]:
    """建卡即起跑（D2）：卡是 auto 且有目标 ⇒ 点火。**建卡调用方**在建成后调它。"""
    return kick(work_id, card_id)
