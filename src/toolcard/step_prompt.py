# -*- coding: utf-8 -*-
"""T6 · 步骤指令组装（EC-048 / EC-049 / EC-050 / EC-051 / EC-052 / EC-053）。

[当前轮注入消息事件 2026-10-14] **160 区退役**：本模块不再产 system 区，
改为产 ``context_injection`` 事件的结构化数据（``build_injection_data``）与
模型可见投影（``render_injection``）；事件本体与落盘由 ``api/current_turn.py``
统一组装。只在**该卡确实有工具卡运行时**时有数据；没有就返回 None。

**两路分家**（[当前轮注入改assistant自述 2026-10-14 · v2.1 §0.1]）：
- ``render_injection``：供**注入投影**用，第一人称自述；
- ``render_instruction`` / ``build_instruction``：供 **run_tool_card 工具结果**的
  ``instruction`` 字段用，**第三人称标签原样**（与改造前逐字一致）——
  工具结果不是注入投影，**不得**改成第一人称；``build_instruction`` **不得**
  复用 ``render_injection``（防再次连坐）。

注入文本形态（``render_injection`` · 自述）::

    我正在按「{name}」推进，当前在 {step_id}。
    本步要求：{提示词全文}
    我已落盘的文件：{代码自己落盘时的文件名}

工具结果文本形态（``render_instruction`` · 第三人称原样）::

    【当前剧本】{name}
    【当前步】{step_id}
    【本步要求】{提示词全文}
    【上一步产物】{代码自己落盘时的文件名}

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


def build_injection_data(work_id: str, card_id: str) -> Optional[dict[str, Any]]:
    """``context_injection``（工具卡）事件 data；无运行时 → None（没有来源不发）。

    字段全部原样取自运行态来源：剧本名／当前步（driver runtime）、本步提示词
    **原文**（剧本 JSON 的 prompt_ref → 提示词文件）、代码自己登记过的产物
    **文件名**（artifact manifest）。**不产出任何评价、结论、剩余步数。**
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
    return {
        "source_kind": "toolcard",
        "source_ref": {"script_name": rt.script_name, "step_id": rt.step_id},
        "step_id": rt.step_id,
        "content": {"prompt": prompt_text, "artifacts": prev},
    }


def render_injection(data: dict[str, Any]) -> str:
    """工具卡注入 data → 模型可见文本（第一人称自述「当前流程」）。

    [当前轮注入改assistant自述 2026-10-14] 字段与来源**不变**（driver runtime／
    剧本 JSON／prompt 原文／artifact manifest），只改人称与标签：旧
    ``【当前剧本】/【当前步】/【本步要求】/【上一步产物】`` 是第三视角标签，
    改为自述（``我已落盘的文件：``）。``artifacts`` 为空时整行跳过（行为不变）。
    """
    ref = data.get("source_ref") or {}
    content = data.get("content") or {}
    prompt_text = content.get("prompt") or ""
    parts = [
        f"我正在按「{ref.get('script_name', '')}」推进，当前在 {data.get('step_id', '')}。",
        f"本步要求：{prompt_text}" if prompt_text else "本步要求：（本步未配提示词文件）",
    ]
    artifacts = content.get("artifacts") or []
    if artifacts:
        # EC-050 允许的形态：只报「我落盘的文件是 X」——代码自己做过的事。
        # 不查它在不在、空不满、好不好（EC-051）。
        parts.append("我已落盘的文件：" + "、".join(artifacts))
    return "\n".join(parts)


def render_instruction(data: dict[str, Any]) -> str:
    """工具卡注入 data → **工具结果** instruction 文本（第三人称标签，与改造前逐字一致）。

    [当前轮注入改assistant自述 2026-10-14 · v2.1 §0.1] 与 ``render_injection``
    分家：本函数供 ``run_tool_card`` 工具结果的 ``instruction`` 字段用，**保持
    第三人称原样**（``【当前剧本】/【当前步】/【本步要求】/【上一步产物】``），
    不得改成第一人称。字段与来源同 ``render_injection``（不变）。
    """
    ref = data.get("source_ref") or {}
    content = data.get("content") or {}
    prompt_text = content.get("prompt") or ""
    parts = [
        f"【当前剧本】{ref.get('script_name', '')}",
        f"【当前步】{data.get('step_id', '')}",
        f"【本步要求】{prompt_text}" if prompt_text else "【本步要求】（本步未配提示词文件）",
    ]
    artifacts = content.get("artifacts") or []
    if artifacts:
        # EC-050 允许的形态：只报「代码落盘的文件是 X」——代码自己做过的事。
        parts.append("【上一步产物】" + "、".join(artifacts))
    return "\n".join(parts)


def build_instruction(work_id: str, card_id: str) -> Optional[str]:
    """拼当前步指令文本（**工具结果用·第三人称原样**）；无运行时 → None。

    [当前轮注入改assistant自述 2026-10-14 · v2.1 §0.1] **不得**复用
    ``render_injection``（注入投影·第一人称）——两路分家，防工具结果被连坐
    成第一人称。``build_injection_data`` → ``render_instruction``，输出与改造前
    逐字一致（run_tool_card 工具结果的 ``instruction`` 字段走这里）。
    """
    data = build_injection_data(work_id, card_id)
    return render_instruction(data) if data is not None else None


def build_progress_note(card_id: str) -> str:
    """影子卡 ``## 进度`` 节的展示行（**展示用第二载体**，非真值源）。

    同样只写位置与计数，不写「完成/通过」（EC-038）。
    """
    rt = driver._runtimes.get(card_id)
    if rt is None:
        return ""
    return f"- 剧本 {rt.script_name}｜当前步 {rt.step_id}｜第 {rt.round} 次"


# [当前轮注入消息事件 2026-10-14] 原 ``build_region``（160 system 区）已退役：
# 160 不再进 system_prompt，改由 ``build_injection_data`` → context_injection 事件。
