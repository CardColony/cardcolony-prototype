# -*- coding: utf-8 -*-
"""期 2 单测 · harness 消息 → wire 请求序列化。

判据 3：serialize（effort 全规则、thinking 顶层形态、passback 只在工具轮、content=""不 null、
'(no output)'、可选字段不发 null）。
零网络。
"""
from __future__ import annotations

import pytest

from src.model_gateway.llm.errors import LlmError
from src.model_gateway.llm.message import (
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)
from src.model_gateway.llm.types import (
    GenerateOptions,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
)
from src.model_gateway.llm_openai_compat.serialize import (
    RequestDefaults,
    serialize_messages,
    serialize_request,
)


def _opts(messages=(), **kw):
    return GenerateOptions(provider="p", model="m", messages=tuple(messages), **kw)


# ---------------- assistant 回放 ----------------

def test_assistant_text_only_turn():
    msg = create_assistant_message((TextBlock(text="答"),), provider="p", model="m")
    assert serialize_messages((msg,)) == [{"role": "assistant", "content": "答"}]


def test_pure_tool_call_turn_sends_empty_string_never_null():
    """纯工具轮 ``content=""``，**绝不 null**（原版 serialize.ts:86-95）。"""
    msg = create_assistant_message(
        (ToolCallBlock(id="c1", name="read_card", arguments="{}"),), provider="p", model="m"
    )
    wire = serialize_messages((msg,))
    assert wire[0]["content"] == ""
    assert wire[0]["content"] is not None
    assert wire[0]["tool_calls"] == [{"id": "c1", "type": "function", "function": {"name": "read_card", "arguments": "{}"}}]
    assert "reasoning_content" not in wire[0]


def test_reasoning_only_turn_still_sends_empty_string():
    """reasoning-only 轮：``content=""``（发 null 会让该会话之后每轮 400）。"""
    msg = create_assistant_message((ReasoningBlock(text="想"),), provider="p", model="m")
    wire = serialize_messages((msg,))
    assert wire[0]["content"] == ""


def test_reasoning_content_passed_back_only_on_tool_call_turns():
    with_tools = create_assistant_message(
        (ReasoningBlock(text="想"), TextBlock(text="答"), ToolCallBlock(id="c", name="n", arguments="{}")),
        provider="p",
        model="m",
    )
    assert serialize_messages((with_tools,))[0]["reasoning_content"] == "想"
    plain = create_assistant_message((ReasoningBlock(text="想"), TextBlock(text="答")), provider="p", model="m")
    assert "reasoning_content" not in serialize_messages((plain,))[0]


# ---------------- tool 结果 ----------------

def test_tool_result_becomes_standalone_tool_message():
    msg = create_tool_result_message("call_1", (TextBlock(text="结果"),), is_error=False)
    wire = serialize_messages((msg,))
    assert wire == [{"role": "tool", "tool_call_id": "call_1", "content": "结果"}]


def test_empty_tool_output_becomes_no_output_placeholder():
    msg = create_tool_result_message("call_1", (), is_error=False)
    wire = serialize_messages((msg,))
    assert wire == [{"role": "tool", "tool_call_id": "call_1", "content": "(no output)"}]


def test_user_text_and_tool_results_expand_separately():
    msg = create_tool_result_message("call_1", (TextBlock(text="r"),), is_error=False)
    # 手工构造混合 user 消息：文本 + 工具结果
    from src.model_gateway.llm.message import Message, UserMessageSource

    mixed = Message(role="user", content=(TextBlock(text="看这个"),) + msg.content, source=UserMessageSource())
    wire = serialize_messages((mixed,))
    assert wire[0] == {"role": "user", "content": "看这个"}
    assert wire[1] == {"role": "tool", "tool_call_id": "call_1", "content": "r"}


# ---------------- 请求体 ----------------

def test_request_is_always_streaming_with_usage():
    body = serialize_request(_opts((create_user_message((TextBlock(text="hi"),)),)))
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["model"] == "m"


def test_optional_fields_are_omitted_not_null():
    body = serialize_request(_opts((create_user_message((TextBlock(text="hi"),)),)))
    for key in ("temperature", "max_tokens", "stop", "tools", "thinking", "reasoning_effort"):
        assert key not in body


def test_system_slot_prepended():
    body = serialize_request(_opts((create_user_message((TextBlock(text="hi"),)),), system="SYS"))
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_thinking_is_top_level_type_form():
    body = serialize_request(
        _opts((create_user_message((TextBlock(text="hi"),)),)), RequestDefaults(thinking="enabled")
    )
    assert body["thinking"] == {"type": "enabled"}
    assert "extra_body" not in body


def test_effort_off_disables_thinking_and_never_sends_off():
    body = serialize_request(
        _opts((create_user_message((TextBlock(text="hi"),)),)), RequestDefaults(reasoning_effort="off")
    )
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_effort_high_and_max_enable_thinking():
    for effort in ("high", "max"):
        body = serialize_request(
            _opts((create_user_message((TextBlock(text="hi"),)),)), RequestDefaults(reasoning_effort=effort)
        )
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == effort


def test_deployment_disabled_rejects_non_off_effort():
    with pytest.raises(LlmError) as exc:
        serialize_request(
            _opts((create_user_message((TextBlock(text="hi"),)),)),
            RequestDefaults(thinking="disabled", reasoning_effort="high"),
        )
    assert exc.value.code == "UNSUPPORTED_REASONING_EFFORT"


def test_session_title_purpose_forces_thinking_disabled():
    body = serialize_request(
        _opts((create_user_message((TextBlock(text="hi"),)),), purpose="session-title"),
        RequestDefaults(thinking="enabled", reasoning_effort="max"),
    )
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_caller_effort_overrides_defaults_and_unknown_is_rejected():
    body = serialize_request(
        _opts((create_user_message((TextBlock(text="hi"),)),), reasoning_effort="max"),
        RequestDefaults(),
    )
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    with pytest.raises(LlmError) as exc:
        serialize_request(
            _opts((create_user_message((TextBlock(text="hi"),)),), reasoning_effort="bogus"), RequestDefaults()
        )
    assert exc.value.code == "UNSUPPORTED_REASONING_EFFORT"


def test_tools_serialized_in_openai_function_shape():
    from src.model_gateway.llm.types import ToolSchema

    body = serialize_request(
        _opts(
            (create_user_message((TextBlock(text="hi"),)),),
            tools=(ToolSchema(name="read_card", description="d", parameters={"type": "object"}),),
        )
    )
    assert body["tools"] == [
        {"type": "function", "function": {"name": "read_card", "description": "d", "parameters": {"type": "object"}}}
    ]
