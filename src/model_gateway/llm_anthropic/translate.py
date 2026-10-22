# -*- coding: utf-8 -*-
"""Anthropic Messages SSE 事件 → harness StreamChunk 翻译。

事件序列（Anthropic wire）：
    message_start                 → 带 usage 快照（可忽略）
    content_block_start           → BlockStart(index, block_type)
    content_block_delta           → text_delta→TextDelta／thinking_delta→ReasoningDelta／
                                    input_json_delta→ToolCallDelta(partial_json)
    content_block_stop            → BlockEnd(index, block)
    message_delta                 → stop_reason→FinishReason；usage→UsageChunk
    message_stop                  → 流自然结束
    error                         → ErrorReason(failure)

与 ``llm_openai_compat/translate.py`` 的分界：那边吃 OpenAI 的
``choices[0].delta`` 形状；这边吃 Anthropic 的**具名事件**形状，两套 wire
不存在共享解析的可能，故独立成包（同一套 StreamChunk 词汇表，见 llm/types.py）。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

from ..llm.errors import LlmError
from ..llm.types import (
    BlockEnd,
    BlockStart,
    ContentBlock,
    FinishChunk,
    FinishReason,
    MaxTokensReason,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    StreamChunk,
    TextBlock,
    TextDelta,
    TokenUsage,
    ToolCallsReason,
    ToolCallBlock,
    ToolCallDelta,
    UsageChunk,
)

__all__ = ["map_stop_reason", "map_usage", "translate"]


def map_stop_reason(reason: Optional[str]) -> FinishReason:
    """Anthropic ``stop_reason`` → harness FinishReason。"""
    if reason in (None, "end_turn", "stop_sequence"):
        return StopReason()
    if reason == "tool_use":
        return ToolCallsReason()
    if reason == "max_tokens":
        return MaxTokensReason()
    return StopReason()


def map_usage(usage: Optional[dict[str, Any]]) -> TokenUsage:
    """Anthropic usage → harness TokenUsage（缓存字段分开计，不折进 input）。"""
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    # 口径（llm/types.py TokenUsage）：input_tokens 只算未命中缓存的输入
    input_tokens = max(0, input_tokens - cache_read - cache_write)
    details = usage.get("output_tokens_details") or {}
    reasoning_tokens = details.get("thinking_tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read or None,
        cache_write_tokens=cache_write or None,
        reasoning_tokens=int(reasoning_tokens) if reasoning_tokens else None,
    )


class _BlockTracker:
    """按 index 跟踪已开块，供 content_block_stop 产出完整 ContentBlock。"""

    def __init__(self) -> None:
        self._open: dict[int, dict[str, Any]] = {}

    def start(self, index: int, block: dict[str, Any]) -> None:
        self._open[index] = dict(block)

    def delta(self, index: int, delta: dict[str, Any]) -> None:
        block = self._open.get(index)
        if block is None:
            return
        kind = delta.get("type")
        if kind == "text_delta":
            block["text"] = (block.get("text") or "") + str(delta.get("text") or "")
        elif kind == "thinking_delta":
            block["thinking"] = (block.get("thinking") or "") + str(delta.get("thinking") or "")
        elif kind in ("input_json_delta", "partial_json"):
            block["_json"] = (block.get("_json") or "") + str(delta.get("partial_json") or "")
        elif kind == "signature_delta":
            block["_signature"] = str(delta.get("signature") or "")

    def stop(self, index: int) -> Optional[ContentBlock]:
        block = self._open.pop(index, None)
        if block is None:
            return None
        kind = block.get("type")
        if kind == "text":
            return TextBlock(text=block.get("text") or "")
        if kind == "thinking":
            return ReasoningBlock(text=block.get("thinking") or "")
        if kind == "tool_use":
            raw = block.get("_json") or "{}"
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                arguments = {}
            return ToolCallBlock(
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                arguments=json.dumps(arguments, ensure_ascii=False),
            )
        return None


async def translate(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[StreamChunk]:
    """Anthropic SSE 事件流 → StreamChunk 流。"""
    tracker = _BlockTracker()
    async for event in events:
        kind = event.get("type")
        if kind == "error":
            error = event.get("error") or {}
            raise LlmError(
                str(error.get("message") or "anthropic stream error"),
                "STREAM_ERROR",
            )
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            index = int(event.get("index") or 0)
            tracker.start(index, block)
            yield BlockStart(index=index, block_type=str(block.get("type") or "text"))
        elif kind == "content_block_delta":
            index = int(event.get("index") or 0)
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                yield TextDelta(index=index, text=str(delta.get("text") or ""))
            elif dtype == "thinking_delta":
                yield ReasoningDelta(index=index, text=str(delta.get("thinking") or ""))
            elif dtype in ("input_json_delta", "partial_json"):
                yield ToolCallDelta(
                    index=index,
                    id="",
                    arguments_delta=str(delta.get("partial_json") or ""),
                )
            tracker.delta(index, delta)
        elif kind == "content_block_stop":
            index = int(event.get("index") or 0)
            block = tracker.stop(index)
            if block is not None:
                yield BlockEnd(index=index, block=block)
        elif kind == "message_start":
            usage = ((event.get("message") or {}).get("usage")) or None
            if usage:
                yield UsageChunk(usage=map_usage(usage))
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if isinstance(delta.get("stop_reason"), str):
                yield FinishChunk(reason=map_stop_reason(delta["stop_reason"]))
            usage = event.get("usage")
            if isinstance(usage, dict):
                yield UsageChunk(usage=map_usage(usage))
        elif kind == "message_stop":
            return
