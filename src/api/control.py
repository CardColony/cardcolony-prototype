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
import json
import logging
from typing import Any

from ..storage import session_facade, session_ops

logger = logging.getLogger("api.control")

STOP = "STOP"
CONTINUE = "CONTINUE"
DEFER_HUMAN = "DEFER_HUMAN"

# 人停置位（consume-once：check 返回 STOP 即清除，不影响下一回合）
_stop_requests: set[tuple[str, str]] = set()

#: [强制停止 2026-10-24 · 用户令「我需要的停止是强制性的」] 停止标记
#: (key=(work_id, card_id) → {reason, by, ts})。置位后该卡**拒绝一切内部唤醒**
#: (goal 续轮/收件箱调起/观点回合),只有两条路解除:用户直接发话、resume_child。
#: 与 goal pause 的分工:pause 治「自动续轮」(持久,事件日志权威);本标记治
#: 「被信件/汇聚再次叫醒」(进程内,goal pause 已持久兜底自动路径)。
_HALTED: dict[tuple[str, str], dict[str, Any]] = {}

#: [halt 事件化 2026-11-01 · 裁定《机制判断归harness》第一刀]
#: 「被停」此前只活在 _HALTED 进程内字典——重启即失忆，事件日志里查无
#: 此事（card_140/141 复盘的病根之一：停止意图不可持久、不可审计、
#: 不可派生）。现在 ``halt``/``unhalt`` 在置/摘标记的同时各落一条事件
#: （``card_halted``/``card_unhalted``，写目标卡事件日志），``_HALTED``
#: 降为它的进程内缓存——每条调用路径先 ``_rebuild_halted`` 按 work 惰性
#: 重放重建。「这卡是不是被停了」从此是一条可重放、可溯源的派生事实。
_HALTED_LOADED: set[str] = set()


def _emit_halt(work_id: str, card_id: str, ev_type: str,
               data: dict[str, Any]) -> None:
    """停止状态变更落事件（零阻断：落盘失败不拦停止/解除本身）。"""
    try:
        from ..storage import event_log as _ev
        _ev.append_sync(work_id, card_id, {"type": ev_type, "data": data})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[control] 停止事件落盘失败(不阻断) card=%s: %s",
                       card_id, exc)


def _rebuild_halted(work_id: str) -> None:
    """按 work 重放事件日志恢复停止标记（每 work 每进程一次）。

    判据：每张卡**最后**一条 ``card_halted``/``card_unhalted``——halted
    收尾即被停。逐行扫、只解析含 'halted' 的行（事件文件可上 MB，
    全量 json.loads 不值）。进程内已有的标记不清——重启语义是
    「磁盘补内存」，不是「磁盘覆盖内存」。
    """
    if work_id in _HALTED_LOADED:
        return
    _HALTED_LOADED.add(work_id)
    try:
        from ..storage import event_log as _ev
        evdir = _ev.events_dir(work_id)
        if not evdir.is_dir():
            return
        for f in evdir.glob("*.jsonl"):
            cid = f.stem
            last: dict[str, Any] | None = None
            try:
                with open(f, encoding="utf-8") as fh:
                    for ln in fh:
                        if "halted" not in ln:
                            continue
                        try:
                            rec = json.loads(ln)
                        except (ValueError, TypeError):
                            continue
                        if rec.get("type") in ("card_halted", "card_unhalted"):
                            last = rec
            except OSError:
                continue
            if last is None or last.get("type") != "card_halted":
                continue
            d = last.get("data") or {}
            _HALTED[(work_id, cid)] = {
                "reason": str(d.get("reason") or "")[:300],
                "by": str(d.get("by") or "")[:80],
                "ts": str(d.get("ts") or last.get("ts") or ""),
            }
    except Exception as exc:  # noqa: BLE001 重建失败不阻断（标记集可能不全）
        logger.warning("[control] 停止标记重建失败 work=%s: %s", work_id, exc)


def halt(work_id: str, card_id: str, reason: str = "", by: str = "") -> dict[str, Any]:
    """强制停止:置标记 + 落 card_halted 事件 + 杀当轮(request_stop 的全部
    机制:流内信号赛跑保字/流外直接取消协程)。goal pause 由调用方按需叠加
    (control.stop_auto)。"""
    from datetime import datetime, timezone, timedelta
    _rebuild_halted(work_id)
    _HALTED[(work_id, card_id)] = {
        "reason": str(reason or "")[:300],
        "by": str(by or "")[:80],
        "ts": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    }
    _emit_halt(work_id, card_id, "card_halted", dict(_HALTED[(work_id, card_id)]))
    return request_stop(work_id, card_id)


def unhalt(work_id: str, card_id: str, by: str = "", via: str = "") -> bool:
    """解除停止标记(用户直接发话/resume_child/续派时)。返回是否原本在停。
    真在停才落 card_unhalted——对本来没停的卡不造「解除」假象。"""
    _rebuild_halted(work_id)
    entry = _HALTED.pop((work_id, card_id), None)
    if entry is not None:
        _emit_halt(work_id, card_id, "card_unhalted", {
            "by": str(by or "")[:80], "via": str(via or "")[:80],
            "was_by": str(entry.get("by") or "")[:80],
            "was_reason": str(entry.get("reason") or "")[:300],
        })
    return entry is not None


def is_halted(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """该卡是否处于强制停止态(返回标记详情;None=未停)。"""
    _rebuild_halted(work_id)
    return _HALTED.get((work_id, card_id))

#: 停止信号的**可等待**形态（key=(work_id, card_id)）。
#:
#: [取消墙钟 2026-10-23 · 用户令「整个机制全部通过信息反馈」]
#: 原来停止靠两条腿：置位（在下一帧边界消费）＋ `_hard_cancel_after` 的 1.5 秒
#: 硬取消兜底。那个 1.5 秒就是墙钟——而它要解决的「软停等不到边界」其实有
#: 直接的信息通道：**把停止做成一个 event，和「下一个 chunk」一起等，谁先到算谁**。
#: ⇒ 停止立即生效（不等帧、不等钟），已产出的字照旧保住（走同一 `_stopped` 收口）。
_stop_events: dict[tuple[str, str], asyncio.Event] = {}

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


def request_stop(work_id: str, card_id: str) -> dict[str, Any]:
    """人停（stop 端点唯一入口）。

    按**状态**分两条路，都不靠时间：

      ① **在模型流里**（``model_service.in_stream`` 为真）：置停止信号。
         流把它和「下一个 chunk」一起 await，谁先到算谁 ⇒ 立刻收口，
         且已产出的字保住（2026-10-15 裁定「不丢已产出的字」不推翻）。
      ② **不在流里**（卡在工具执行 / 上下文装配）：信号没人等 ⇒ 直接 cancel
         回合协程，否则回合永不收口、互斥体永不释放（实测 card_031 卡 9 分钟、
         再发消息 409）。

    [取消墙钟 2026-10-23] 原文档写的「② 硬停兜底：1.5 秒还没收口 ⇒ cancel」
    已废——那个 1.5 秒是墙钟，已删除；现在的判据是**在不在流里**这个状态。
    """
    holder = session_facade.turn_holder(work_id, card_id)
    if not holder:
        return {"stopped": False, "reason": "no_inflight_turn",
                "idempotent": True, "holder": ""}
    _stop_requests.add((work_id, card_id))
    # 停止＝把信号直接交给在等的流（它和「下一个 chunk」赛跑，谁先到算谁）：
    # 流里立刻收口、保住已产出的字，**不需要任何定时兜底**。
    stop_event_for(work_id, card_id).set()
    # 但若这张卡此刻**不在**模型流里（卡在工具执行/上下文装配），信号没人等，
    # 回合就永远不会收口、互斥体永不释放（实测 card_031 卡 9 分钟、再发消息 409）。
    # ⇒ 这种情况直接 cancel 协程。判据是**状态**（在不在流里），不是时间。
    try:
        from . import model_service as _ms
        _waiting_on_stream = _ms.in_stream(work_id, card_id)
    except Exception:  # noqa: BLE001 判不出来按「不在流里」处理：宁可立刻停
        _waiting_on_stream = False
    if not _waiting_on_stream:
        cancel_turn(work_id, card_id)
    session_ops.ops_log("human_stop", work_id, card_id,
                        {"action": "requested", "holder": holder,
                         "path": "signal" if _waiting_on_stream else "cancel"})
    logger.info("[control] 人停 work=%s card=%s holder=%s 路径=%s",
                work_id, card_id, holder,
                "信号（在流里，立刻收口）" if _waiting_on_stream
                else "取消协程（不在流里，信号没人等）")
    return {"stopped": True, "reason": "stop_requested", "holder": holder,
            "path": "signal" if _waiting_on_stream else "cancel"}


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
    # 信号也一起清：否则下一回合的流一上来就看见「已置位」，立刻自我收口。
    ev = _stop_events.pop((work_id, card_id), None)
    if ev is not None:
        ev.clear()


def pending_stop(work_id: str, card_id: str) -> bool:
    """诊断用：当前是否有未消费的停止置位（不改状态）。"""
    return (work_id, card_id) in _stop_requests


def stop_event_for(work_id: str, card_id: str) -> asyncio.Event:
    """取该卡的停止信号，**没有就建**——模型流启动时必须调它。

    [停止失效修复 2026-10-23] 改前流启动用的是 ``stop_event()``（只取不建）：
    而事件只在**点停止时**才被创建 ⇒ 流启动时永远是 None ⇒ 流根本不和停止
    信号赛跑 ⇒ 点停止时 ``request_stop`` 又因 ``in_stream()`` 为真而不 cancel
    ⇒ **停止彻底无效**（用户实测「说停就停不下来」）。
    事件生命周期由回合界定：流里建、``discard_stop`` 在回合收口时清。
    """
    ev = _stop_events.get((work_id, card_id))
    if ev is None:
        ev = asyncio.Event()
        _stop_events[(work_id, card_id)] = ev
    return ev


def has_stop_signal(work_id: str, card_id: str) -> bool:
    """诊断用：该卡此刻是否有**已置位**的停止信号（不新建、不暴露事件对象）。

    [接口收敛 2026-10-23] 原来这里还有一个 ``stop_event()``（只取不建、取不到返回
    None），和 ``stop_event_for()``（取不到就建）**同名不同行为**——流启动时选错
    那个就返回 None，于是流不与信号赛跑、停止彻底失效（用户实测「停不下来」）。
    两个入口留着，下一个人还会选错。故**只保留一个「取用」入口**
    （``stop_event_for``，恒有返回值），诊断需求走本函数返回 bool。
    """
    ev = _stop_events.get((work_id, card_id))
    return ev is not None and ev.is_set()


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
    """停止：**暂停目标** ＋ 清「等子卡」意图 ＋ 释放执行锁。**幂等**（无目标 ⇒ 全 no-op）。

    - ② 本卡目标 ``active → paused``（**经 goal 服务**，权威在事件日志）；
    - ③ 子树内自动卡一并停（级联）；
    - 释放该 work 的执行锁（串行模式下队列不饿死）。

    [停止不看模式 2026-10-29 · 用户令「停止为什么要分自动还是手动」]
    改前循环第一行是 ``if not auto_mode: continue`` —— **手动卡按下停止时，它的
    目标根本不会被暂停**。实测 work_027/card_061（auto_mode=0）连按三次停止，
    goal 域**零 pause 事件**，``goal_state`` 仍是 active，界面一直显示「推进中」，
    用户感受就是「不论如何也停不下来」。
    ⇒ ``auto_mode`` 只该决定「要不要自动续轮」，**不该决定「能不能被停」**。
      停止是用户的强制动作，对自动卡与手动卡一视同仁。

    [停止失效修复 2026-10-24 · 用户实测「开了自动模式就停不下来」]
    改前这里直接 ``update_card({"goal_state": "paused"})``——写的是**投影列**，
    而权威是事件日志 fold 出来的 ``goal.phase``。运行中的进程缓存仍是 active，
    ``goal.driver.drive`` 照旧续轮，下一轮 ``_project`` 又把 ``goal_state`` 写回
    active ⇒ **点了停止也停不下来**（实测 work_001/card_001：停一次立刻又起一轮，
    571 字符的续轮 prompt 一模一样，连停 5 次停不住）。

    现在走 ``goal.service.pause()``：写 goal/change 事件 + 重投影，**跨重启有效**，
    且不会被下一次投影覆盖。
    """
    from ..storage import card_store
    from . import serial_gate

    paused: list[str] = []
    cleared_await: list[str] = []
    goal_error: list[str] = []
    for cid in _auto_subtree(work_id, card_id):
        m = card_store._meta_of(work_id, cid) or {}
        was_active = str(m.get("goal_state") or "") == "active"
        # ② 目标暂停走 **goal 服务的正规命令**（写事件 + 重投影），不是写投影列。
        # ★ 判据取 **goal 域自己的 phase**，不看 auto_mode，也不只看投影列
        #   （投影可能落后于事件日志：卡停在 paused 但权威仍是 active 的情形，
        #    正是「停不下来」的一半成因）。
        paused_here = False
        try:
            from ..goal import service as goal_service
            gs = goal_service.get_service()
            goal = await gs.get(work_id, cid)
            if goal is not None and goal.phase == "active":
                await gs.pause(work_id, cid, goal.ref())
                paused.append(cid)
                paused_here = True
        except Exception as exc:  # noqa: BLE001 停不动不抛（停止路径必须走完）
            goal_error.append(f"{cid}:{exc}")
            logger.warning("[control] 目标暂停失败 work=%s card=%s: %s",
                           work_id, cid, exc)
        if not paused_here and was_active:
            # 没有 goal 事件（纯 legacy 卡）⇒ 退回写投影列，保持旧行为可用
            try:
                await card_store.update_card(
                    work_id, cid, {"goal_state": "paused"},
                    "[自动模式] 停止：关自动继续")
                paused.append(cid)
            except Exception as exc:  # noqa: BLE001
                goal_error.append(f"{cid}:{exc}")
                logger.warning("[control] 关自动继续失败 work=%s card=%s: %s",
                               work_id, cid, exc)
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
        if has_await:
            fields["goal_await_children"] = 0
            note = "取消「等子卡」等待意图（用户按停优先）"
        if not fields:
            continue
        try:
            await card_store.update_card(work_id, cid, fields, note)
            if has_await:
                cleared_await.append(cid)      # 成功后才记（失败不谎报已清）
        except Exception as exc:  # noqa: BLE001 停不动不抛（停止路径必须走完）
            logger.warning("[control] 关自动继续失败 work=%s card=%s: %s",
                           work_id, cid, exc)
    released = serial_gate.release_work(work_id)
    if paused or cleared_await or released or goal_error:
        session_ops.ops_log("auto_stop", work_id, card_id,
                            {"paused": paused, "lock_released": released,
                             "cleared_await": cleared_await,
                             "goal_errors": goal_error})
    logger.info("[control] 自动模式停止 work=%s card=%s paused=%s released=%s cleared_await=%s",
                work_id, card_id, paused, released, cleared_await)
    # ★ 返回形状**逐字不变**（``{"paused", "lock_released"}`` 是既有调用方与
    #   测试的契约）。目标暂停的失败只进 ops 留痕，不往返回值里加键。
    return {"paused": paused, "lock_released": released}
