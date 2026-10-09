# -*- coding: utf-8 -*-
"""T6 · 步骤指令组装（EC-048 / EC-049 / EC-050 / EC-051 / EC-052 / EC-053）。

挂点：``context_assembler.build_context_regions``（L55）与
``message_runner.build_context``（L162）——在 150 区（当前卡）之后插一个
160 区，只在**该卡确实有工具卡运行时**时出现；没有就不出现（没有来源就不
出现这个区，与 150/200 区同纪律）。

注入文本形态（EC-049 逐字）::

    【当前剧本】{name}
    【当前步】{step_id}
    【本步要求】{提示词全文}
    【上一步产物】{代码自己落盘时的文件路径}

**可说与不可说（这是本模块存在的全部理由）**

+--------------------------------------------------+---------------------------+
| 可以说（EC-050）                                  | 不可以说（EC-051/052/053）|
+==================================================+===========================+
| 「上一步我落盘的文件是                            | 「该文件存在：是/否」       |
|  ``04-p2-骨架草稿-01.md``」                       | 「该文件非空：是/否」       |
| ——**代码自己做过的事的记录**                      | 「上一步通过/未通过」       |
|                                                  | 「还剩 N 步、已用 M 轮」   |
+--------------------------------------------------+---------------------------+

不可说那一栏的共同点：**都是在替模型下结论**。文件在不在、空不空是 IO 事实
（代码能 stat，但它一 stat 就等于在做「这一步做得下去」的判断）；
「通过/未通过」与「还剩几步」是评价与全局视角，**只有模型能说**。

故本模块：拼提示词全文（原文，一个字不改）、列**代码自己登记过的文件名**，
**不 stat、不 open、不数行、不算剩余步数、不查轮数**。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import artifact_store, driver, script_loader

logger = logging.getLogger("toolcard.step_prompt")

REGION_ORDER = 160
REGION_NAME = "工具卡步骤指令"
SECTION_PROGRESS = "## 进度"


def _previous_artifact_files(card_id: str, current_step_id: str) -> list[str]:
    """列出**当前步之外**、代码自己登记过的产物文件名（机械过滤，非判断）。

    过滤依据只是「登记的 stepId 等不等于当前 step」——字符串相等与否，
    与内容、存在性、空满、好坏**统统无关**。
    """
    return [
        e.get("文件", "") for e in artifact_store.manifest_entries(card_id)
        if e.get("stepId") != current_step_id
    ]


def build_instruction(work_id: str, card_id: str) -> Optional[str]:
    """拼当前步指令文本；该卡无工具卡运行时 → None（**没有来源就不出现**）。

    只拼事实与原文：剧本名、当前步指针、提示词全文、代码自己落过的文件名。
    **不产出任何评价、结论、进度百分比、剩余步数。**
    """
    rt = driver._runtimes.get(card_id)
    if rt is None:
        return None
    prompt_text = ""
    try:
        script = script_loader.load_script(rt.script_name)
        sd = script.find_step(rt.step_id)
        if sd is not None and sd.prompt_ref:
            prompt_text = script_loader.load_prompt(rt.script_name, sd.prompt_ref)
    except script_loader.ScriptError as exc:
        # 剧本/提示词读不到＝IO 事实。如实降级为空提示词并 warn，
        # **不判剧本写得对不对**，也不因此拦推进。
        logger.warning("步骤指令取提示词失败 card=%s: %s", card_id, exc)

    prev = _previous_artifact_files(card_id, rt.step_id)
    parts = [
        f"【当前剧本】{rt.script_name}",
        f"【当前步】{rt.step_id}",
        f"【本步要求】{prompt_text}" if prompt_text else "【本步要求】（本步未配提示词文件）",
    ]
    if prev:
        # EC-050 允许的形态：只报「我落盘的文件是 X」——代码自己做过的事。
        # 不查它在不在、空不满、好不好（EC-051）。
        parts.append("【上一步产物】" + "、".join(prev))
    return "\n".join(parts)


def build_progress_note(card_id: str) -> str:
    """影子卡 ``## 进度`` 节的展示行（**展示用第二载体**，非真值源）。

    同样只写位置与计数，不写「完成/通过」（EC-038）。
    """
    rt = driver._runtimes.get(card_id)
    if rt is None:
        return ""
    return f"- 剧本 {rt.script_name}｜当前步 {rt.step_id}｜第 {rt.round} 次"


def build_region(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """给 ``context_assembler.build_context_regions`` 用的 160 区。

    无运行时 → None（调用方跳过该区）。有则返回一条 region dict。
    """
    text = build_instruction(work_id, card_id)
    if not text:
        return None
    return {
        "order": REGION_ORDER,
        "name": REGION_NAME,
        "channel": "system",
        "seats": ("main",),
        "text": text,
        "entries": 1,
    }
