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

import logging
from typing import Any

from ..storage import session_facade, session_ops

logger = logging.getLogger("api.control")

STOP = "STOP"
CONTINUE = "CONTINUE"
DEFER_HUMAN = "DEFER_HUMAN"

# 人停置位（consume-once：check 返回 STOP 即清除，不影响下一回合）
_stop_requests: set[tuple[str, str]] = set()


def request_stop(work_id: str, card_id: str) -> dict[str, Any]:
    """人停置位（stop 端点唯一入口——**不是**直接写 stop_requested，
    接口被消费才算数，T120）。无在途回合＝no-op 幂等（T123）：
    不报错、不造轮、返回当前状态。"""
    holder = session_facade.turn_holder(work_id, card_id)
    if not holder:
        return {"stopped": False, "reason": "no_inflight_turn",
                "idempotent": True, "holder": ""}
    _stop_requests.add((work_id, card_id))
    session_ops.ops_log("human_stop", work_id, card_id,
                        {"action": "requested", "holder": holder})
    logger.info("[control] 人停置位 work=%s card=%s holder=%s", work_id, card_id, holder)
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
