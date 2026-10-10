# -*- coding: utf-8 -*-
"""思维链板 165 区渲染（迭代二 S4 / 方案 §2.2 t165 注入位）。

给 ``context_assembler.build_context_regions`` 消费——形态照 t160
（toolcard_step_prompt.build_region，:104-119）同构：无来源→None（调用方
跳过该区，「没有来源就不出现这个区」纪律）；有则返回一条 region dict。

区内容只含**事实三行**（任务书 S4 步骤 3 逐字：当前计划名／当前第几步／
本步要求原文）——不含任何评价、结论、剩余步数或通过与否（t160 同纪律）。

与 t160 互斥（工具卡先到先得）：由调用方 else 分支实现——t160 在则本件
不被调用（context_assembler 一处改动内完成，任务书红线 3）。

数据源＝board.snapshot_sync（同步读板：内存命中优先、盘只读恢复、
无板/坏盘 → None——装配热路径 fail-safe）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import board as board_mod

logger = logging.getLogger("thinkchain.step_region")

REGION_ORDER = 165
REGION_NAME = "思维链板"


def build_region(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """165 区：活板事实三行。无活板/恢复失败 → None。"""
    try:
        snap = board_mod.snapshot_sync(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 装配热路径绝不抛
        logger.warning("165 区取板异常 card=%s: %s", card_id, exc)
        return None
    if not snap:
        return None
    prompt = snap.get("current_prompt")
    if not prompt:
        return None  # 全步推进完（收口中）/无当前步——区不出现
    cur = snap.get("current", 0)
    text = (
        f"当前计划: {snap.get('name', '')}\n"
        f"当前第 {cur + 1}/{snap.get('total', len(snap['steps']))} 步\n"
        f"本步要求: {prompt}"
    )
    return {
        "order": REGION_ORDER,
        "name": REGION_NAME,
        "channel": "system",
        "seats": ("main",),
        "text": text,
        "entries": 1,
    }
