# -*- coding: utf-8 -*-
"""收口三件＋终态清理（迭代二 S3 / 方案 §4.5）。

末步 step_complete 自动触发（**钉死自动**——不设模型手动工具；plan_finalize
如需暴露留待下期按需加，任务书 S3 步骤 2 原文）。四件序：

  ①结论回写卡面一次（走 card_store 写闸——「计划名＋终态＋结论＋产物
    指针」四样拼一段写进卡面现状块 status_block，update_card 支持的正文
    三块键之一，docstring :923 在案）；
  ②产物核对落库（has_artifact=true 的步，位置
    works/{work_id}/thinkchain/{card_id}/{seq}-{step_id}.md；
    **未声明的不强造**——has_artifact=False 或文件缺失都如实记录，绝不补造）；
  ③final 块落档（结论＋产物清单，behavior="final"）；
  ④触发 S1 清理（board.finalize_board：ops plan_finalize＋卡面
    thinkchain_final 终态键 → board.cleanup_board：删 plan 文件＋.tmp 残留
    ＋内存条目）。

形态照 toolcard/finalize.py:57-99（永不抛、try/except 留痕、result dict）。
被 S2 plan_tools._trigger_finalize 延迟导入消费。
"""
from __future__ import annotations

import logging
from typing import Any

from ..storage import card_store, session_facade
from . import board as board_mod

logger = logging.getLogger("thinkchain.finalize")

BEHAVIOR_FINAL = "final"

_FINAL_FM_NOTE = "思维链板收口结论回写"


def _artifact_line(work_id: str, card_id: str, seq: int, step_id: str,
                   exists: bool) -> str:
    p = board_mod.artifact_path(work_id, card_id, seq, step_id)
    mark = "在库" if exists else "缺失（未声明产物不强造）"
    return f"- 步 {step_id}: {p}（{mark}）"


async def run_finalize(work_id: str, card_id: str, conclusion: str = "") -> str:
    """收口四件序。返回一行摘要（供 step_complete 结果拼接）；**永不抛**。"""
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

    # ② 产物核对（先于回写——清单进卡面与 final 块）
    artifacts: list[str] = []
    for i, st in enumerate(steps):
        if st.get("has_artifact"):
            seq = i + 1
            exists = board_mod.artifact_path(work_id, card_id, seq, st["id"]).exists()
            artifacts.append(_artifact_line(work_id, card_id, seq, st["id"], exists))
    artifact_block = "\n".join(artifacts) if artifacts else "（无声明产物的步）"

    # ① 结论回写卡面一次（四样：计划名＋终态＋结论＋产物指针）
    final_verdict = "已收口"
    conclusion = (conclusion or "").strip() or "（模型未交结论原文）"
    status_text = (
        f"【思维链收口】\n"
        f"计划名: {plan_name}\n"
        f"终态: {final_verdict}\n"
        f"结论: {conclusion}\n"
        f"产物:\n{artifact_block}"
    )
    card_marked = False
    try:
        result = await card_store.update_card(
            work_id, card_id, {"status_block": status_text}, _FINAL_FM_NOTE,
        )
        card_marked = not (isinstance(result, dict) and "error" in result)
        if not card_marked:
            logger.warning("收口结论回写被拒 card=%s: %s", card_id, result.get("error"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("收口结论回写异常 card=%s: %s", card_id, exc)

    # ③ final 块落档（结论＋产物清单）
    try:
        await session_facade.append(work_id, card_id, {
            "content": f"收口终态: {final_verdict}\n结论: {conclusion}\n产物清单:\n{artifact_block}",
            "actor": "tool",
            "end": "",
            "behavior": BEHAVIOR_FINAL,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("final 块落盘失败 card=%s: %s", card_id, exc)

    # ④ S1 终态标记＋清理（fail-open 逐件）
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

    n_art = len(artifacts)
    return (f"收口完成：{len(steps)} 步推进，产物 {n_art} 件核对"
            f"（{'卡面已回写' if card_marked else '卡面回写失败已留痕'}）。"
            "请把收口结论整理成最终回复交给用户。")
