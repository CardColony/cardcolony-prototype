# -*- coding: utf-8 -*-
"""思维链板 165 区渲染（迭代二 S4 原型；[思维链真实落地 2026-10-11] 判据化重写）。

给 ``context_assembler.build_context_regions`` 消费——形态照 t160 同构：
无来源→None（调用方跳过该区，「没有来源就不出现这个区」纪律）；有则返回
一条 region dict。

[笔 2] 区内容改用新板快照注入三段事实：
  ① 计划名／当前第几步；
  ② **已完成步的 result 清单**（跨回合不丢步成果——每回合装配都重看得到
     已推进步的成果原文）；
  ③ 当前步的 prompt／criteria／deliverable＋执行纪律（board.STEP_BRIEF_
     TEMPLATE 附录 C 模板全文）。
不含任何评价、结论、剩余步数或通过与否（事实纪律不变）。

与 t160 互斥（工具卡先到先得）：由调用方 else 分支实现——t160 在则本件
不被调用。

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
    """165 区：活板事实（计划名／步位／已完成步成果／本步判据与纪律）。
    无活板/恢复失败 → None。"""
    try:
        snap = board_mod.snapshot_sync(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 装配热路径绝不抛
        logger.warning("165 区取板异常 card=%s: %s", card_id, exc)
        return None
    if not snap:
        return None
    steps = snap.get("steps") or []
    cur = snap.get("current", 0)
    cur_step = steps[cur] if isinstance(cur, int) and 0 <= cur < len(steps) else None
    if cur_step is None:
        return None  # 全步推进完（收口中）/无当前步——区不出现

    lines = [
        f"当前计划: {snap.get('name', '')}",
        f"当前第 {cur + 1}/{snap.get('total', len(steps))} 步",
    ]
    done_steps = [s for s in steps if s.get("status") == "done" and s.get("id") != cur_step.get("id")]
    if done_steps:
        lines.append("已完成步成果（跨回合保留，供你衔接）:")
        for s in done_steps:
            result = (s.get("result") or "").strip()
            lines.append(f"- 步 {s['id']}: {s.get('prompt', '')}"
                         f" → 成果: {result[:800] if result else '（无成果记录）'}")
    lines.append(board_mod.step_brief(cur_step))
    return {
        "order": REGION_ORDER,
        "name": REGION_NAME,
        "channel": "system",
        "seats": ("main",),
        "text": "\n".join(lines),
        "entries": 1,
    }
