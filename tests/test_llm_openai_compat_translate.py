# -*- coding: utf-8 -*-
"""期 2 单测 · wire chunk → StreamChunk 翻译。

判据 3：translate（首帧空串不开块、reasoning 先行、tool 分片拼接、压尾、usage 拆账、
空响应报错、malformed 抛错、finish 映射）。
零网络。
"""
from __future__ import annotations

import json

import pytest

from src.model_gateway.llm.errors import LlmError
from src.model_gateway.llm.types import (
    BlockEnd,
    BlockStart,
    ErrorReason,
    FinishChunk,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    TextBlock,
    TextDelta,
    TokenUsage,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallsReason,
    MaxTokensReason,
    UsageChunk,
)
from src.model_gateway.llm_openai_compat.sse import DONE
from src.model_gateway.llm_openai_compat.translate import map_finish_reason, map_usage, translate


async def _payloads(*items):
    for item in items:
        yield item


async def _tr(*items):
    return [c async for c in translate(_payloads(*items))]


def _chunk(delta=None, finish=None, usage=None):
    body: dict = {"choices": []}
    if delta is not None or finish is not None:
        body["choices"] = [{"delta": delta or {}, "finish_reason": finish}]
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body)


async def test_first_empty_reasoning_does_not_open_block():
    out = await _tr(_chunk(delta={"reasoning_content": ""}), _chunk(delta={"reasoning_content": "想"}), DONE)
    assert out[0] == BlockStart(index=0, block_type="reasoning")
    assert out[1] == ReasoningDelta(index=0, text="想")


async def test_reasoning_precedes_text_and_text_block_is_second():
    out = await _tr(_chunk(delta={"reasoning_content": "思"}), _chunk(delta={"content": "答"}), DONE)
    kinds = [c.type for c in out]
    assert kinds[:4] == ["block-start", "reasoning-delta", "block-start", "text-delta"]
    assert out[1] == ReasoningDelta(index=0, text="思")
    assert out[3] == TextDelta(index=1, text="答")


async def test_tool_call_fragments_concatenate_by_index():
    out = await _tr(
        _chunk(delta={"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_card", "arguments": '{"a"'}}]}),
        _chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}),
        _chunk(finish="tool_calls"),
        DONE,
    )
    ends = [c for c in out if isinstance(c, BlockEnd)]
    assert ends == [BlockEnd(index=0, block=ToolCallBlock(id="call_1", name="read_card", arguments='{"a":1}'))]


async def test_finish_and_usage_are_deferred_to_done():
    out = await _tr(_chunk(delta={"content": "hi"}, finish="stop", usage={"prompt_tokens": 5, "completion_tokens": 2}), DONE)
    assert [c.type for c in out] == ["block-start", "text-delta", "block-end", "usage", "finish"]
    assert out[3] == UsageChunk(usage=TokenUsage(input_tokens=5, output_tokens=2))
    assert out[4] == FinishChunk(reason=StopReason())


async def test_trailing_usage_only_chunk_keeps_latest():
    out = await _tr(
        _chunk(delta={"content": "hi"}, finish="stop", usage={"prompt_tokens": 5, "completion_tokens": 2}),
        _chunk(usage={"prompt_tokens": 9, "completion_tokens": 4}),
        DONE,
    )
    usages = [c for c in out if isinstance(c, UsageChunk)]
    assert usages == [UsageChunk(usage=TokenUsage(input_tokens=9, output_tokens=4))]


async def test_usage_subtracts_cache_hits_from_input():
    usage = map_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 7},
        }
    )
    assert usage == TokenUsage(input_tokens=60, output_tokens=20, cache_read_tokens=40, reasoning_tokens=7)
    # OpenAI 兼容拼法 prompt_tokens_details.cached_tokens 同样识别
    assert map_usage({"prompt_tokens": 50, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 10}}).input_tokens == 40


async def test_stop_with_zero_blocks_is_empty_response_error():
    out = await _tr(_chunk(finish="stop"), DONE)
    assert len(out) == 1
    assert isinstance(out[0], FinishChunk)
    assert isinstance(out[0].reason, ErrorReason)
    assert out[0].reason.failure.code == "EMPTY_RESPONSE"


async def test_no_finish_with_zero_blocks_is_empty_response_error():
    out = await _tr(DONE)
    assert isinstance(out[0].reason, ErrorReason)
    assert out[0].reason.failure.code == "EMPTY_RESPONSE"


async def test_malformed_json_raises_malformed_response():
    with pytest.raises(LlmError) as exc:
        await _tr("not-json", DONE)
    assert exc.value.code == "MALFORMED_RESPONSE"


async def test_finish_reason_mapping():
    assert map_finish_reason("stop") == StopReason()
    assert map_finish_reason("tool_calls") == ToolCallsReason()
    assert map_finish_reason("length") == MaxTokensReason()
    other = map_finish_reason("content_filter")
    assert isinstance(other, ErrorReason)
    assert other.failure.code == "CONTENT_FILTER"


async def test_payload_source_ending_without_done_raises():
    with pytest.raises(LlmError) as exc:
        await _tr(_chunk(delta={"content": "x"}))
    assert exc.value.code == "STREAM_CLOSED"
