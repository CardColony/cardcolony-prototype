# -*- coding: utf-8 -*-
"""期 1 单测 · 增量装配器（BlockAssembler）。

判据 2 第二条：assembler（delta-only 容错、闭块后迟到 delta 丢弃、乱序 index）。
零真实调用、零网络。
"""
from __future__ import annotations

from src.model_gateway.llm.assembler import BlockAssembler
from src.model_gateway.llm.types import (
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
    ToolCallBlock,
    ToolCallDelta,
    TokenUsage,
    UsageChunk,
)


def test_delta_only_protocol_auto_opens_blocks():
    """无 block-start / block-end：按 delta 类型自动开块。"""
    a = BlockAssembler()
    a.push(TextDelta(index=0, text="你好"))
    a.push(TextDelta(index=0, text="世界"))
    a.push(ReasoningDelta(index=1, text="想想"))
    blocks = a.blocks()
    assert blocks == [TextBlock(text="你好世界"), ReasoningBlock(text="想想")]


def test_late_delta_after_block_end_is_dropped():
    """闭块后迟到 delta 丢弃（坏适配器不能污染已完成块）。"""
    a = BlockAssembler()
    a.push(BlockStart(index=0, block_type="text"))
    a.push(TextDelta(index=0, text="A"))
    a.push(BlockEnd(index=0, block=TextBlock(text="A")))
    a.push(TextDelta(index=0, text="LATE"))  # 应被丢弃
    assert a.blocks() == [TextBlock(text="A")]


def test_block_end_is_authoritative_and_first_close_wins():
    a = BlockAssembler()
    a.push(TextDelta(index=0, text="raw"))
    a.push(BlockEnd(index=0, block=TextBlock(text="authoritative")))
    a.push(BlockEnd(index=0, block=TextBlock(text="second")))  # 第二次关闭忽略
    assert a.blocks() == [TextBlock(text="authoritative")]


def test_out_of_order_index_keeps_first_seen_order():
    """乱序 index：装配顺序按**首见**顺序，不按数值排序。"""
    a = BlockAssembler()
    a.push(TextDelta(index=5, text="后到"))
    a.push(TextDelta(index=1, text="先到"))
    assert a.blocks() == [TextBlock(text="后到"), TextBlock(text="先到")]


def test_tool_call_deltas_assemble_and_id_name_from_first_fragment():
    a = BlockAssembler()
    a.push(ToolCallDelta(index=0, id="call_1", arguments_delta='{"a"', name="read_card"))
    a.push(ToolCallDelta(index=0, id="call_1", arguments_delta=":1}"))
    blocks = a.blocks()
    assert blocks == [ToolCallBlock(id="call_1", name="read_card", arguments='{"a":1}')]


def test_tool_call_id_fallback_only_when_id_absent():
    """原版 ``??`` 是 nullish 合并：空串 ``""`` 原样保留，只有 id 缺省（None）才合成 ``call-<index>``。"""
    a = BlockAssembler()
    a.push(ToolCallDelta(index=3, id="", arguments_delta="{}"))
    assert a.blocks() == [ToolCallBlock(id="", name="", arguments="{}")]

    b = BlockAssembler()
    b.push(ToolCallDelta(index=7, id=None, arguments_delta="{}"))  # type: ignore[arg-type]
    assert b.blocks() == [ToolCallBlock(id="call-7", name="", arguments="{}")]


def test_max_tokens_drops_tool_calls():
    """max-tokens 截断：丢弃无法安全执行的 tool-call。"""
    a = BlockAssembler()
    a.push(TextDelta(index=0, text="部分正文"))
    a.push(ToolCallDelta(index=1, id="c", arguments_delta='{"half":'))
    a.push(FinishChunk(reason=MaxTokensReason()))
    assert a.blocks() == [TextBlock(text="部分正文")]


def test_usage_and_finish_readers():
    a = BlockAssembler()
    assert a.usage is None
    assert a.finish.kind == "stop"  # 未给 finish 时兜底 stop
    a.push(UsageChunk(usage=TokenUsage(input_tokens=1, output_tokens=2)))
    a.push(FinishChunk(reason=StopReason()))
    assert a.usage == TokenUsage(input_tokens=1, output_tokens=2)
    assert a.finish == StopReason()


def test_error_finish_is_kept():
    a = BlockAssembler()
    a.push(FinishChunk(reason=ErrorReason(failure=LlmFailure(message="x", code="SERVER"))))
    assert a.finish.kind == "error"


def test_message_wraps_blocks_as_assistant():
    a = BlockAssembler()
    a.push(TextDelta(index=0, text="答"))
    msg = a.message()
    assert msg.role == "assistant"
    assert msg.content == (TextBlock(text="答"),)
