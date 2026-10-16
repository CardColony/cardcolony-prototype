# -*- coding: utf-8 -*-
"""期 1 单测 · 协议词汇表（六态/七态）序列化往返。

判据 2 第一条：「六态协议序列化往返」。
覆盖 StreamChunk 七态、ContentBlock 四型、FinishReason 五态、TokenUsage 的 dict 往返。
零真实调用、零网络。
"""
from __future__ import annotations

from src.model_gateway.llm.types import (
    AbortedReason,
    BlockEnd,
    BlockStart,
    ErrorReason,
    FinishChunk,
    LlmFailure,
    MaxTokensReason,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    TextBlock,
    TextDelta,
    TokenUsage,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallsReason,
    ToolResultBlock,
    UsageChunk,
    block_from_dict,
    block_to_dict,
    chunk_from_dict,
    chunk_to_dict,
)

_FAILURE = LlmFailure(message="boom", code="SERVER", status=503, provider_retry_after_ms=1500.0, request_id="req-1")


def test_text_and_reasoning_blocks_round_trip():
    for block in (TextBlock(text="hi"), ReasoningBlock(text="think")):
        assert block_from_dict(block_to_dict(block)) == block


def test_tool_call_and_tool_result_blocks_round_trip():
    tc = ToolCallBlock(id="call_1", name="read_card", arguments='{"a":1}')
    assert block_from_dict(block_to_dict(tc)) == tc
    # tool-result 递归嵌套 content
    tr = ToolResultBlock(
        tool_call_id="call_1",
        content=(TextBlock(text="inner"), ToolCallBlock(id="c2", name="n", arguments="{}")),
        is_error=True,
    )
    assert block_from_dict(block_to_dict(tr)) == tr


def test_all_seven_chunk_states_round_trip():
    chunks = [
        BlockStart(index=0, block_type="text"),
        TextDelta(index=0, text="甲"),
        ReasoningDelta(index=1, text="想"),
        ToolCallDelta(index=2, id="call_x", arguments_delta='{"a"', name="read_card"),
        BlockEnd(index=0, block=TextBlock(text="甲")),
        UsageChunk(usage=TokenUsage(input_tokens=10, output_tokens=3, cache_read_tokens=7, reasoning_tokens=2)),
        FinishChunk(reason=StopReason()),
    ]
    for chunk in chunks:
        assert chunk_from_dict(chunk_to_dict(chunk)) == chunk


def test_finish_reason_five_kinds_round_trip():
    reasons = [
        StopReason(),
        ToolCallsReason(),
        MaxTokensReason(),
        AbortedReason(failure=_FAILURE),
        ErrorReason(failure=_FAILURE),
    ]
    for reason in reasons:
        restored = chunk_from_dict(chunk_to_dict(FinishChunk(reason=reason)))
        assert restored.reason == reason


def test_finish_chunk_carries_replay_state():
    chunk = FinishChunk(reason=StopReason(), replay_state={"cursor": 3})
    assert chunk_from_dict(chunk_to_dict(chunk)).replay_state == {"cursor": 3}


def test_token_usage_is_disjoint_and_optional_fields_default_none():
    usage = TokenUsage(input_tokens=100, output_tokens=20)
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.reasoning_tokens is None
    # 计费输入 = 三者之和（不重叠口径）
    assert usage.input_tokens == 100
