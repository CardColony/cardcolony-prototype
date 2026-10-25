# -*- coding: utf-8 -*-
"""对话读取窗口 + 工具结果注入有界（2026-10-25 用户报障修复的回归网）。

病根：read_linked(part=对话) 返全量 timeline。两条卡全史合计 287 万字符
（各自含对方全史，嵌套膨胀），第二轮请求 ≈190 万 token，顶爆所有模型上下文
（kimi 1M 先死，兜底 aliyun 报 "Range of input length [1, 1000000]"）。
"""
from __future__ import annotations

import json

from src.api import card_tools


def _ev(seq, type_, data):
    return {"seq": seq, "type": type_, "data": data}


def test_digest_clips_tool_result_and_skips_noise(monkeypatch):
    """工具结果只留摘要（嵌套膨胀载体），reasoning/控制事件不进摘要，总量有界。"""
    huge = {"ok": True, "value": "X" * 500_000}
    events = [
        _ev(1, "round_start", {"round": 1}),
        _ev(2, "user_message", {"content": "开始"}),
        _ev(3, "reasoning", {"text": "Y" * 50_000}),
        _ev(4, "tool_result", {"tool": "read_linked", "target": "card_x", "ok": True, "result": huge}),
        _ev(5, "assistant_text", {"content": "做完了"}),
    ]
    monkeypatch.setattr(card_tools.event_log, "timeline",
                        lambda w, c, tail=0, **kw: events[-tail:] if tail else events)
    v = card_tools._conversation_digest("w", "c")
    texts = [e["text"] for e in v["events"]]
    assert len(v["events"]) == 3, "reasoning 与 round_start 不得进摘要"
    assert all(len(t) < 2_500 for t in texts), "单条必须被截断"
    tool_line = [t for t in texts if t.startswith("工具结果")][0]
    assert "X" * 1000 not in tool_line, "工具结果正文不得回灌"
    assert "已截断" in tool_line or len(tool_line) < 500


def test_digest_total_budget_and_truncated_flag(monkeypatch):
    """总量超预算 ⇒ 从最旧的丢，并明示 truncated。"""
    events = [_ev(i, "assistant_text", {"content": "字" * 1200}) for i in range(1, 200)]
    monkeypatch.setattr(card_tools.event_log, "timeline",
                        lambda w, c, tail=0, **kw: events[-tail:] if tail else events)
    v = card_tools._conversation_digest("w", "c")
    total = sum(len(e["text"]) for e in v["events"])
    assert total <= card_tools._CONV_TOTAL_MAX
    assert v["truncated"] is True
    assert "丢弃" in v["window"]


def test_bound_text_under_and_over_budget():
    assert card_tools.bound_text("短") == "短"
    big = "字" * 200_000  # 中文 3 字节/字 ⇒ 600KB
    out = card_tools.bound_text(big)
    assert len(out.encode("utf-8")) <= card_tools.TOOL_RESULT_INJECT_BUDGET + 500
    assert "已截断" in out and "不完整数据" in out, "截断必须明示且告警"


def test_bound_text_keeps_json_valid_prefix_semantics():
    """截断后仍是可读文本（不是半个 JSON 崩溃），且带原始字节数。"""
    payload = json.dumps({"k": "v" * 100_000}, ensure_ascii=False)
    out = card_tools.bound_text(payload, max_bytes=1000)
    assert out.startswith('{"k": "vvv')
    assert "原文" in out
