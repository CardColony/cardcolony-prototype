# -*- coding: utf-8 -*-
"""轮内插话通道（用户消息投递进**正在跑**的这一轮）。

## 为什么要有

对话页一直有"插话"这个动作（Cmd/Ctrl+Enter、``T.steerQueue`` 文案），
但**实现是空的**：

- ``workspace.js`` 的 ``steerQueue()`` 在运行中只弹一句
  「本轮进行中，排队消息将在轮结束后依序发出」——**什么都不做**；
- ``queue-store.js`` 的 ``steer`` 动作只是「提到队首」，仍是排队语义。

⇒ UI 上写着"插话"，实际语义是"排队"，用户等的是**整轮跑完**（1–8 分钟）。
**这是假功能。**

## 本模块做什么

给一条**真的**插话通道：用户在模型跑着的时候把话打进去，**下一轮模型调用就能看到**
（不必等整轮收口）。照 ``control.py`` 的既有范式做（三件套：置位／消费／清扫）：

- 置位：``request_steer``（HTTP 端点唯一入口）
- 消费：``take_steer``（``message_runner`` 每次模型调用前取走，插进轮槽）
- 清扫：``discard_steer``（回合收口统一清残留，避免下一回合误消费）

## 边界（据实说清，不含糊）

- **"下一轮可见"不是"这一轮立刻打断"**：模型调用一旦发出，本机没有中断它的通道
  （中止只有人工 STOP 一条路，见 ``control.request_stop``）。插话在**下一次**模型
  调用生效——工具轮通常几秒到几十秒，体感是「很快看到了」，而不是「等 1–8 分钟」。
- **插话优先于收口**：模型正要收口（无工具、正文已定）时用户插了话 ⇒ 不收口，
  把插话插进轮槽、继续下一轮（否则那句话会被丢掉）。
- **不写会话块**：插话只进**轮槽**（本次回合的模型上下文）与**事件日志**（界面可见），
  不进会话文件 ⇒ 不重复喂给模型、不污染会话历史。
- **无在途回合 ⇒ 不投递**：``request_steer`` 查在途互斥体，无在途回合就返回
  ``not_applied``，前端回退成普通发送（不假装成功）。
"""
from __future__ import annotations

import logging
from typing import Any

from ..storage import session_facade, session_ops

logger = logging.getLogger("api.steer")

#: 插话消息的前置标记（与 current_turn 的〔工作规范〕〔当前流程〕同族）：
#: 让模型一眼认出「这是用户在跑的过程中插进来的话」，不是系统注入、也不是新任务。
STEER_MARK = "用户插话："

#: 待投递插话：(work_id, card_id) → [文本, ...]（进程内；回合收口清扫）
_pending: dict[tuple[str, str], list[str]] = {}


async def request_steer(work_id: str, card_id: str, text: str, *,
                        mark: str = STEER_MARK,
                        emit_event: bool = True) -> dict[str, Any]:
    """投递一条插话（HTTP 端点唯一入口）。

    无在途回合 ⇒ **不投递**（``not_applied``）：宁可让前端回退成普通发送，
    也不假装插话成功——用户要的是「说给正在跑的那一轮听」。

    [2026-11-08 · 卡间消息复用插话通道] ``mark``/``emit_event``：用户插话
    走默认 〔用户插话〕 标＋user_message 事件；卡间消息/汇报经
    ``deliver_now`` 忙时复用本通道时传自己的标、不发事件——其 msg_* 事件
    在落会话块时已记，再发一条 user_message 会让前端把卡的话当用户的话。
    队列里存的是 ``mark+body`` 成品，``_apply_steer`` 取走即用不再拼。
    """
    body = str(text or "").strip()
    if not body:
        return {"applied": False, "reason": "empty_text", "idempotent": True}
    holder = session_facade.turn_holder(work_id, card_id)
    if not holder:
        return {"applied": False, "reason": "no_inflight_turn",
                "idempotent": True, "holder": ""}
    key = (work_id, card_id)
    item = str(mark or "") + body
    _pending.setdefault(key, []).append(item)
    # 界面可见：插话落一条 user_message **事件**（不写会话块 ⇒ 不重复喂给模型、
    # 不污染会话历史；事件流是前端唯一数据源 ⇒ 刷新后也看得见这条插话）。
    # 落点＝用户按下的那一刻（不是被消费时）⇒ 事件时序与用户操作顺序一致。
    if emit_event:
        try:
            await session_facade.emit_message_event(
                work_id, card_id, "user_message", item)
        except Exception as exc:  # noqa: BLE001 事件落盘零阻断
            logger.warning("[steer] 插话事件落盘失败（不阻断）card=%s: %s", card_id, exc)
    session_ops.ops_log("steer", work_id, card_id,
                        {"action": "requested", "holder": holder,
                         "chars": len(item)})
    logger.info("[steer] 插话置位 work=%s card=%s holder=%s 待投=%d",
                work_id, card_id, holder, len(_pending[key]))
    return {"applied": True, "reason": "steered", "holder": holder,
            "pending": len(_pending[key])}


def take_steer(work_id: str, card_id: str) -> list[str]:
    """消费：取走本卡全部待投插话（取走即清——投递是一次性的，不重放）。"""
    key = (work_id, card_id)
    items = _pending.pop(key, None)
    if items:
        logger.info("[steer] 插话消费 work=%s card=%s 条数=%d", work_id, card_id, len(items))
    return items or []


def pending_steer(work_id: str, card_id: str) -> int:
    """诊断用：当前有多少条待投插话（不改状态）。"""
    return len(_pending.get((work_id, card_id), []))


def discard_steer(work_id: str, card_id: str) -> list[str]:
    """回合收口清扫（对齐 ``control.discard_stop`` 的纪律）。返回被清件。

    置位只在回合窗口内有效：回合收口时未消费的插话（模型已收口、用户那句话
    来不及了）一律作废——**不能留给下一回合**，否则那句话会凭空出现在下一轮。

    [2026-11-08] 但「作废」不等于「吞掉」：返回残留件（已按 mark 拼好的
    成品），收口方可以决定摘牌后补投一轮——否则 applied=True 的那句话
    模型从没见过，发话方却以为送达了。
    """
    key = (work_id, card_id)
    items = _pending.pop(key, []) or []
    if items:
        logger.info("[steer] 插话残留清扫 work=%s card=%s 条数=%d", work_id, card_id, len(items))
    return items
