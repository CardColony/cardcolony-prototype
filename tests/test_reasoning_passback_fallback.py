# -*- coding: utf-8 -*-
"""带 tool_calls 的 assistant 消息必须回传**非空**推理字段（2026-11-04 实测修复）。

病根：commandcode 的 reflection/beam:free、stealth/glyph-cluster:free 实测
「assistant 带 tool_calls 但缺 reasoning_content」或「字段为空串」→ 400
AI_APICallError / Invalid request；补非空 reasoning_content（或 reasoning）→ 200。
字段在投影链上会丢（context_regions 只在非空时保留、旧会话/系统注入消息本就
没有、_serialize_assistant 只在有 reasoning 块时写），故在 wire 出口统一兜底。
"""
from __future__ import annotations

from src.model_gateway.llm.types import GenerateOptions
from src.model_gateway.llm_openai_compat.serialize import serialize_request

CALL = {"id": "c1", "type": "function",
        "function": {"name": "read_card", "arguments": "{}"}}


def _body(messages):
    opts = GenerateOptions(
        provider="commandcode", model="reflection/beam:free",
        messages=tuple(messages), system="sys",
    )
    return serialize_request(opts)


def test_missing_reasoning_on_tool_call_is_filled():
    wire = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "", "tool_calls": [CALL]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    body = _body(wire)
    asst = body["messages"][2]  # sys + user + assistant
    assert asst.get("reasoning_content"), "必须补非空推理字段"
    assert asst["reasoning_content"].strip() != ""
    # 原 wire dict 不许被改写（prompt 日志/失败兜底继续吃原文）
    assert "reasoning_content" not in wire[1]


def test_empty_string_reasoning_also_repaired():
    """空串同样 400（实测），不能当作已存在。"""
    wire = [
        {"role": "assistant", "content": "", "tool_calls": [CALL],
         "reasoning_content": ""},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    asst = _body(wire)["messages"][1]
    assert asst["reasoning_content"].strip() != ""


def test_existing_reasoning_untouched():
    wire = [
        {"role": "assistant", "content": "", "tool_calls": [CALL],
         "reasoning_content": "真实思考"},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    assert _body(wire)["messages"][1]["reasoning_content"] == "真实思考"


def test_alternate_reasoning_field_is_respected():
    """端点也认 `reasoning` 字段（beam/glyph 用它），已有就不重复补。"""
    wire = [
        {"role": "assistant", "content": "", "tool_calls": [CALL],
         "reasoning": "真实思考"},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    out = _body(wire)["messages"][1]
    assert out["reasoning"] == "真实思考"
    assert "reasoning_content" not in out


def test_messages_without_tool_calls_are_untouched():
    wire = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    body = _body(wire)
    assert "reasoning_content" not in body["messages"][2]
