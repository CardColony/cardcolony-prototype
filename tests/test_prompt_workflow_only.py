# -*- coding: utf-8 -*-
"""提示词只描述流程，不描述「你能／你不能」。

[2026-10-28 用户裁定 · 第一手]
原话：「为什么要强调它可以改需求文档…你应该告诉它需求文档修改的流程是什么，而不是
告诉它"你可以"。这种"你可以"全部都是错误的表述。**所有表述，都应该只是告知工作流程**。」

判据：
- 权限由工具面（perms / 开关 / 类型闸）强制——能不能做，模型看工具在不在手里就知道；
- 提示词再写一遍「你可以 X」＝给许可，模型会把「能」当「该」，放开手乱做；
- 正确写法是「做 X 的流程：第一步…第二步…」，或「判据：满足什么才算 X」。

覆盖范围（模型每轮实际收到的全部文本）：
1. 三套系统提示词：BASE_PROMPT / AUTO_BASE_PROMPT / ROLE_CARD_BASE_PROMPT
2. 全部工具描述（走 `_schema_of`，含 ToolDoc 覆盖后的渲染结果——那才是模型看到的）
"""
from __future__ import annotations

import re

from src.api import card_tools, prompts

#: 许可式表述（讲"能不能"而不是"怎么做"）
FORBIDDEN = ("你可以", "你能", "你有权", "你可以不", "允许", "不能", "不得", "不许", "无权")

_SENT = re.compile(r"(?<=[。；\n])")


def _offending(text: str) -> list[str]:
    out = []
    for s in _SENT.split(text or ""):
        s = s.strip()
        if s and any(w in s for w in FORBIDDEN):
            out.append(s[:90])
    return out


def test_three_prompts_describe_workflow_not_permission():
    cases = [
        ("BASE_PROMPT", prompts.BASE_PROMPT),
        ("AUTO_BASE_PROMPT", prompts.AUTO_BASE_PROMPT),
        ("ROLE_CARD_BASE_PROMPT", prompts.ROLE_CARD_BASE_PROMPT),
    ]
    bad: list[str] = []
    for name, text in cases:
        for s in _offending(text):
            bad.append(f"[{name}] {s}")
    assert not bad, (
        "提示词里出现许可式表述（应改成流程/判据）：\n  · " + "\n  · ".join(bad[:8]))


def test_tool_descriptions_describe_workflow_not_permission():
    """工具描述同样只讲"什么时候用/怎么用"，不讲"你能不能"。"""
    bad: list[str] = []
    for tool, def_ in card_tools.TOOL_REGISTRY.items():
        desc = str(card_tools._schema_of(def_).get("description") or "")
        for s in _offending(desc):
            bad.append(f"[{tool}] {s}")
    assert not bad, "工具描述里出现许可式表述：\n  · " + "\n  · ".join(bad[:8])


def test_requirements_lifecycle_is_described_as_flow():
    """需求文档生命周期已实现 ⇒ 提示词必须**按流程**写出来（不是写"你可以"）。

    [2026-10-28] 草稿/评审/正稿已在代码里（edit_requirements 写草稿 →
    open_review → 全员 approve 转正稿 → 才派工）。提示词要讲这条顺序，
    否则模型不知道往下走要先过评审。
    """
    t = prompts.BASE_PROMPT
    for kw in ("草稿", "评审", "正稿"):
        assert kw in t, f"提示词没写需求文档的「{kw}」环节——流程没告知"
    # 关键动作要写出来：开评审、全员通过、转正稿
    for kw in ("open_review", "全员通过", "转成**正稿**"):
        assert kw in t, f"提示词没写「{kw}」——流程不完整"
