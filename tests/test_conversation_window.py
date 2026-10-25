# -*- coding: utf-8 -*-
"""工具结果注入有界 ＋ read_linked 去对话（2026-10-25 用户两条令的回归网）。

演进记录：
- 病根：read_linked(part=对话) 返全量 timeline。两条卡全史合计 287 万字符
  （各自含对方全史，嵌套膨胀），第二轮请求 ≈190 万 token，顶爆所有模型
  上下文（kimi 1M 先死，兜底 aliyun 报 "Range of input length [1, 1000000]"）。
- 第一版修法：对话改尾部窗口摘要（f92312e）。
- 用户裁定（「不应该看对话，对话应该直接干掉」）：对话 part **整体删除**——
  跨卡读对话语义本就不对（读对方是看进展＝正文现状区＋影子，不是搬历史）。
  故本文件只保留「注入有界」护栏的回归；part 集断言在 test_card_base_linked。
"""
from __future__ import annotations

import json

from src.api import card_tools


def test_read_linked_parts_exclude_duihua():
    """[用户令 2026-10-25] read_linked 只允许 正文/影子——对话已删除。"""
    assert card_tools.READ_VIA_PARTS == ("正文", "影子")
    import inspect
    src = inspect.getsource(card_tools.read_via)
    assert "对话" not in src.replace("对话是各卡自己的过程记录", "")


def test_bound_text_under_and_over_budget():
    """护栏：低于预算原样；超预算截断并**明示不完整**（不许静默丢数据）。"""
    assert card_tools.bound_text("短") == "短"
    big = "字" * 200_000  # 中文 3 字节/字 ⇒ 600KB
    out = card_tools.bound_text(big)
    assert len(out.encode("utf-8")) <= card_tools.TOOL_RESULT_INJECT_BUDGET + 500
    assert "已截断" in out and "不完整数据" in out


def test_bound_text_keeps_json_valid_prefix_semantics():
    """截断后仍是可读文本（不是半截 JSON 崩溃），且带原始字节数。"""
    payload = json.dumps({"k": "v" * 100_000}, ensure_ascii=False)
    out = card_tools.bound_text(payload, max_bytes=1000)
    assert out.startswith('{"k": "vvv')
    assert "原文" in out
