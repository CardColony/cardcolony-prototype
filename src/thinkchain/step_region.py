# -*- coding: utf-8 -*-
"""思维链 165 来源 → 当前轮注入事件（[当前轮注入消息事件 2026-10-14] 消息事件化）。

**改造前**：本模块产一条 165 system 区（计划名／步位／已完成步成果／本步判据
与纪律），由 ``context_assembler.build_context_regions`` 拼进 system_prompt。
**改造后**（用户 2026-10-14 任务书）：160/165 不再是常驻 system 文本，改为
**当前轮消息事件**——本模块只产**结构化数据**（不再产区、不再拼文本块）：

- ``build_preamble_data``  → ``context_preamble`` 事件 data（固定总领＝附录 C
  模板的固定部分，来自 board.STEP_BRIEF_TEMPLATE）；
- ``build_injection_data`` → ``context_injection`` 事件 data（计划名／步位／
  已完成步成果／当前步字段，全部读 board.snapshot_sync 快照）。

事件本体（七键外壳、type、落盘）由 ``api/current_turn.py`` 组装与投影；
本模块做**数据 + 投影渲染**，不做 IO 之外的判断，不含评价/结论/剩余步数/通过与否
（事实纪律不变，与改造前逐字同）。
[当前轮注入改assistant自述 2026-10-14] ``render_preamble``/``render_injection``
改为第一人称自述（「工作规范」/「当前流程」）；字段与来源不变，只改人称与标签。

无活板／快照不可读／无当前步 → 两个 builder 均返回 None（「没有来源就不发
注入事件」——与改造前「区不出现」等价）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import board as board_mod

logger = logging.getLogger("thinkchain.step_region")

# 固定总领的来源引用（可回溯到唯一模板）
PREAMBLE_SOURCE_REF = "board.STEP_BRIEF_TEMPLATE"

# 已完成步 result 的**展示**长度口径——沿用改造前 165 区（800 字符）。
# 事件 data 里存**全文**（忠实记录），只在 render_injection 的模型可见投影里截断。
RESULT_DISPLAY_LIMIT = 800

# [当前轮注入改assistant自述 2026-10-14] 固定总领的**自述投影**（第一人称「工作规范」）。
# 规则实质仍来自 board.STEP_BRIEF_TEMPLATE（唯一来源）；本常量是它在投影层的自述改写，
# 条目与模板**一一对应、无增无删**（任务书 v2 乙1/乙7）。映射（模板 → 自述）：
#   【步骤执行】你现在执行计划的一步，只做本步的事。           → 我按这套规范推进当前任务：
#   执行纪律：                                                → 我的执行纪律：
#   1. 只做本步的事，不跨步、不加戏。                          → 1. 只做当前步的事，不跨步、不加戏。
#   2. 需要取证就调工具（read_file/search_files/read_card），不凭想象。
#                                                             → 2. 需要取证就调工具，不凭想象。
#   3. 完成判据不满足，就说"没完成"，不许谎报"已完成"。        → 3. 完成判据不满足，就说不满足、不谎报完成。
#   4. 做完立刻调 step_complete：result 写本步成果，criteria_met 写"判据如何满足"。
#                                                             → 4. 做完立刻调 step_complete，提交本步成果与判据满足情况。
PREAMBLE_SELF_NARRATIVE = (
    "我按这套规范推进当前任务：\n"
    "我的执行纪律：\n"
    "1. 只做当前步的事，不跨步、不加戏。\n"
    "2. 需要取证就调工具，不凭想象。\n"
    "3. 完成判据不满足，就说不满足、不谎报完成。\n"
    "4. 做完立刻调 step_complete，提交本步成果与判据满足情况。"
)


def _snapshot(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """同步读板快照（热路径 fail-safe：异常/无板 → None，绝不抛）。"""
    try:
        return board_mod.snapshot_sync(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 装配热路径绝不抛
        logger.warning("取板异常 card=%s: %s", card_id, exc)
        return None


def build_preamble_data(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """固定总领事件 data（思维链）。无活板 → None（不发空壳事件）。"""
    snap = _snapshot(work_id, card_id)
    if not snap:
        return None
    content = board_mod.step_brief_fixed()
    if not content:
        return None
    return {
        "source_kind": "thinkchain",
        "source_ref": PREAMBLE_SOURCE_REF,
        "run_context": {"plan_name": snap.get("name", "")},
        "content": content,
    }


def build_injection_data(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """动态步骤注入事件 data（思维链）。无活板／无当前步 → None。"""
    snap = _snapshot(work_id, card_id)
    if not snap:
        return None
    steps = snap.get("steps") or []
    cur = snap.get("current", 0)
    if not (isinstance(cur, int) and 0 <= cur < len(steps)):
        return None  # 全步推进完（收口中）/无当前步——不发注入
    cur_step = steps[cur]
    if not isinstance(cur_step, dict):
        return None
    done_steps = [
        s for s in steps
        if isinstance(s, dict) and s.get("status") == "done"
        and s.get("id") != cur_step.get("id")
    ]
    return {
        "source_kind": "thinkchain",
        "source_ref": {"plan_name": snap.get("name", ""),
                       "step_id": cur_step.get("id", "")},
        "plan_name": snap.get("name", ""),
        "step_id": cur_step.get("id", ""),
        "index": cur + 1,
        "total": len(steps),
        "completed_steps": [
            {"step_id": s.get("id", ""), "prompt": s.get("prompt", ""),
             "result": s.get("result") or ""}
            for s in done_steps
        ],
        "current_step": {
            "prompt": cur_step.get("prompt", ""),
            "criteria": cur_step.get("criteria") or "",
            "deliverable": cur_step.get("deliverable") or "",
            "type": cur_step.get("type") or "",
        },
    }


def render_preamble(data: dict[str, Any]) -> str:
    """固定总领 data → 模型可见文本（第一人称自述「工作规范」）。

    [当前轮注入改assistant自述 2026-10-14] 投影层自述改写：正文取
    ``PREAMBLE_SELF_NARRATIVE``（规则实质仍来自 board.STEP_BRIEF_TEMPLATE，
    条目一一对应、无增无删）。**事件 data 不变**——``data['content']`` 仍是
    模板固定部分原文（供审计/落盘），投影不再直接透传它。
    """
    return PREAMBLE_SELF_NARRATIVE


def render_injection(data: dict[str, Any]) -> str:
    """动态步骤 data → 模型可见文本（第一人称自述「当前流程」）。

    [当前轮注入改assistant自述 2026-10-14] 字段与来源**不变**（board 快照），
    只改人称与标签：计划名／当前第 N/M 步／已完成步成果（result 展示截断 800，
    无已完成步则整行跳过）／本步要求·完成判据·产出物（仍由
    board.step_brief_dynamic 单一模板渲染）。
    """
    lines = [
        f"我正在执行「{data.get('plan_name', '')}」的第 "
        f"{data.get('index')}/{data.get('total')} 步。",
    ]
    done_steps = data.get("completed_steps") or []
    if done_steps:
        lines.append("我已经完成：")
        for s in done_steps:
            result = (s.get("result") or "").strip()
            shown = result[:RESULT_DISPLAY_LIMIT] if result else "（无成果记录）"
            lines.append(f"- 步 {s.get('step_id', '')}：{s.get('prompt', '')}"
                         f" → 成果：{shown}")
    lines.append(board_mod.step_brief_dynamic(data.get("current_step") or {}))
    return "\n".join(lines)
