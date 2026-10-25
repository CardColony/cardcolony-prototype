# -*- coding: utf-8 -*-
"""用户两条铁律的回归网（2026-10-25）。

① 「不应该看对话，对话应该直接干掉」：read_linked 只有 正文／影子。
② 「不允许有任何截断，截断不如不显示，截断的内容没有任何意义」：
   工具结果注入路径不许有截断器（我曾在 message_runner 加过 200KB bound_text，
   用户追问「哪来的超限？限制是谁给的？」——那个数是我编的，已删）。
   唯一真实的界限是模型自己的上下文窗口，撞了由端点如实报错。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_read_linked_parts_have_no_duihua():
    from src.api import card_tools
    assert card_tools.READ_VIA_PARTS == ("正文", "影子")


def test_injection_path_has_no_truncator():
    """注入路径不许再出现截断器（防止有人再拍一个 magic number 回来）。"""
    runner = (REPO / "src" / "api" / "message_runner.py").read_text(encoding="utf-8")
    assert "bound_text" not in runner, "注入路径出现截断器——违反「截断不如不显示」"
    tools = (REPO / "src" / "api" / "card_tools.py").read_text(encoding="utf-8")
    assert "def bound_text" not in tools
    assert "TOOL_RESULT_INJECT_BUDGET" not in tools
