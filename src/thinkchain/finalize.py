# -*- coding: utf-8 -*-
"""收口三件＋终态清理（迭代二 S3 原型；[思维链真实落地 2026-10-11 笔 3] 重写）。

末步 step_complete 自动触发（**钉死自动**——不设模型手动工具）。收口三件序：

  ①final 块落档（behavior="final"）：收口终态＋计划名＋各步成果与判据
    满足摘要（全部步的 criteria/deliverable/result）——提交轮三席质询的
    档案面；
  ②触发终态标记（board.finalize_board：ops plan_finalize＋卡面
    thinkchain_final 终态键）；
  ③清理（board.cleanup_board：删 plan 文件＋.tmp 残留＋内存条目）。

[笔 3 变更] 旧四件序中的「结论回写卡面 status_block」挪出收口——回写时机
改为提交轮统合完成后（message_runner.run_seats._write_chain_summary），
回写内容＝统合稿（提交轮统合输出才是收口结论，任务书笔 3 ①「收口结论
回写卡面并入提交轮统合输出」）。产物核对位随 has_artifact 字段退役——
步产出物以 deliverable 声明＋result 成果为准，真文件产物由模型经 write_file
落盘（file_tools 通道），收口不再核对板内产物位。

形态照 toolcard/finalize.py:57-99（永不抛、try/except 留痕、result dict）。
被 plan_tools._trigger_finalize 延迟导入消费。
"""
from __future__ import annotations

import logging
from typing import Any

from ..storage import session_facade
from . import board as board_mod

logger = logging.getLogger("thinkchain.finalize")

BEHAVIOR_FINAL = "final"


def _steps_digest(steps: list[dict[str, Any]]) -> str:
    """全部步成果×判据摘要（final 块与提交轮共用口径）：每步一行要求、
    一行判据、一行成果——跨回合不丢步成果的档案面。"""
    lines: list[str] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        lines.append(
            f"- 步 {s.get('id', '')} [{s.get('status', '')}] {s.get('prompt', '')}\n"
            f"  判据: {s.get('criteria', '')}\n"
            f"  成果: {(s.get('result') or '').strip() or '（无成果记录）'}")
    return "\n".join(lines) if lines else "（无步记录）"


async def run_finalize(work_id: str, card_id: str, conclusion: str = "") -> str:
    """收口三件序。返回一行摘要（供 step_complete 结果拼接）；**永不抛**。"""
    try:
        b = await board_mod.get_board(work_id, card_id)
    except board_mod.BoardRestoreError as exc:
        logger.warning("收口取板失败 card=%s: %s", card_id, exc)
        return "收口未执行（板不可读）"
    if b is None:
        logger.info("收口时板已不在 card=%s（可能已清理）", card_id)
        return "收口未执行（板已不在）"
    snap = b.to_snapshot()
    plan_name = snap.get("name", "")
    steps = snap["steps"]
    digest = _steps_digest(steps)

    # ① final 块落档（收口终态＋计划名＋各步成果×判据摘要）
    conclusion = (conclusion or "").strip() or "（模型未交结论原文）"
    try:
        await session_facade.append(work_id, card_id, {
            "content": (
                f"收口终态: 已收口\n"
                f"计划名: {plan_name}\n"
                f"末步结论: {conclusion}\n"
                f"步成果×判据:\n{digest}"),
            "actor": "tool",
            "end": "",
            "behavior": BEHAVIOR_FINAL,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("final 块落盘失败 card=%s: %s", card_id, exc)

    # ② 终态标记＋ ③ 清理（fail-open 逐件）
    try:
        await board_mod.finalize_board(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_finalize 留痕异常 card=%s: %s", card_id, exc)
    try:
        cr = await b.cleanup_board()
        if not cr.get("ok"):
            logger.warning("板清理未成 card=%s: %s（启动扫描兜底）", card_id, cr.get("error"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("板清理异常 card=%s: %s（启动扫描兜底）", card_id, exc)

    return (f"收口完成：{len(steps)} 步推进，各步成果与判据已落档。"
            "随后进入提交轮：三席将核对本步成果是否满足判据，统合后交付。")
