# -*- coding: utf-8 -*-
"""llm_openai_compat · wire chunk → harness StreamChunk 翻译（物理迁自 llm_deepseek，全仓唯一一份）。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm-deepseek/src/translate.ts（185 行）

状态机：每个 content / reasoning / tool-call index 一个 harness 块。
    - **首帧** ``reasoning_content=""`` **不开块**（只有非空串才开）；
    - reasoning 先于 text 处理（思考模式交织序）；
    - ``tool_calls`` 按 ``index`` 分片拼接，``id``/``name`` 只取首片；
    - ``finish_reason`` 与**最新** usage 暂存，压到 ``[DONE]`` 统一产出，覆盖
      「附在 finish chunk」与「尾随 usage-only chunk」两种形态，且保证 finish 之后无 chunk。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterable, AsyncIterator, Optional

from ..llm.errors import EMPTY_RESPONSE_CODE, LlmError
from ..llm.types import (
    BlockEnd,
    BlockStart,
    ContentBlock,
    ErrorReason,
    FinishChunk,
    FinishReason,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    StreamChunk,
    TextBlock,
    TextDelta,
    TokenUsage,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallsReason,
    MaxTokensReason,
    UsageChunk,
)
from .sse import DONE

__all__ = ["map_finish_reason", "map_usage", "translate"]


@dataclass
class _OpenBlock:
    """一个装配中的块。原版 translate.ts:17-24。"""

    index: int
    kind: str  # 'text' | 'reasoning' | 'tool-call'
    text: str = ""
    call_id: Optional[str] = None  # tool-call only
    name: Optional[str] = None     # tool-call only


def map_finish_reason(reason: str) -> FinishReason:
    """wire ``finish_reason`` → harness FinishReason。原版 translate.ts:31-43。

    未识别值（content_filter、insufficient_system_resource、未来新增）→ ``error``，
    其 ``code`` 为**大写化**的原值。
    """
    if reason == "stop":
        return StopReason()
    if reason == "tool_calls":
        return ToolCallsReason()
    if reason == "length":
        return MaxTokensReason()
    return ErrorReason(failure=_failure(f"model stopped: {reason}", reason.upper()))


def _failure(message: str, code: str):
    from ..llm.types import LlmFailure

    return LlmFailure(message=message, code=code)


def map_usage(usage: dict) -> TokenUsage:
    """wire usage → harness 用量。原版 translate.ts:53-62。

    DeepSeek 的 ``prompt_tokens`` **含**缓存命中（= hit + miss）；harness 是不重叠口径，
    故把缓存读**减出** ``input_tokens``。
    """
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning = completion_details.get("reasoning_tokens")
    return TokenUsage(
        input_tokens=usage["prompt_tokens"] - (cache_read or 0),
        output_tokens=usage["completion_tokens"],
        cache_read_tokens=cache_read,
        reasoning_tokens=reasoning,
    )


def _close_block(block: _OpenBlock) -> ContentBlock:
    """为一个装配中的块产出最终 ContentBlock。原版 translate.ts:65-76。"""
    if block.kind == "text":
        return TextBlock(text=block.text)
    if block.kind == "reasoning":
        return ReasoningBlock(text=block.text)
    return ToolCallBlock(id=block.call_id or "", name=block.name or "", arguments=block.text)


async def translate(payloads: AsyncIterable[str]) -> AsyncIterator[StreamChunk]:
    """消费 SSE data 载荷（以 ``[DONE]`` 终结）并产出 StreamChunk。

    原版 ``translate``（translate.ts:86-185）。坏 JSON 载荷以 ``MALFORMED_RESPONSE``
    中止流（**不静默跳过**）。
    """
    next_index = 0
    text_block: Optional[_OpenBlock] = None
    reasoning_block: Optional[_OpenBlock] = None
    tool_blocks: dict[int, _OpenBlock] = {}
    order: list[_OpenBlock] = []
    pending_finish: Optional[FinishReason] = None
    pending_usage: Optional[TokenUsage] = None

    def open_block(kind: str) -> _OpenBlock:
        nonlocal next_index
        block = _OpenBlock(index=next_index, kind=kind)
        next_index += 1
        order.append(block)
        return block

    async for payload in payloads:
        if payload == DONE:
            for block in order:
                yield BlockEnd(index=block.index, block=_close_block(block))
            if pending_usage is not None:
                yield UsageChunk(usage=pending_usage)
            reason = pending_finish if pending_finish is not None else StopReason()
            if reason.kind == "stop" and len(order) == 0:
                # 退化完成（stop 且零块）→ EMPTY_RESPONSE 错误，不是成功空消息。
                yield FinishChunk(
                    reason=ErrorReason(
                        failure=_failure("model returned a completed response with no content", EMPTY_RESPONSE_CODE)
                    )
                )
            else:
                yield FinishChunk(reason=reason)
            return

        try:
            chunk = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"malformed SSE payload: {payload[:120]}", "MALFORMED_RESPONSE") from exc

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}

            # reasoning 先行：思考模式把它交织在 text 之前。首帧空串不得开块。
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning != "":
                if reasoning_block is None:
                    reasoning_block = open_block("reasoning")
                    yield BlockStart(index=reasoning_block.index, block_type="reasoning")
                reasoning_block.text += reasoning
                yield ReasoningDelta(index=reasoning_block.index, text=reasoning)

            content = delta.get("content")
            if isinstance(content, str) and content != "":
                if text_block is None:
                    text_block = open_block("text")
                    yield BlockStart(index=text_block.index, block_type="text")
                text_block.text += content
                yield TextDelta(index=text_block.index, text=content)

            for call in delta.get("tool_calls") or []:
                wire_index = call.get("index")
                block = tool_blocks.get(wire_index)
                if block is None:
                    block = open_block("tool-call")
                    tool_blocks[wire_index] = block
                    yield BlockStart(index=block.index, block_type="tool-call")
                if call.get("id") is not None:
                    block.call_id = call["id"]
                function = call.get("function") or {}
                if function.get("name") is not None:
                    block.name = function["name"]
                fragment = function.get("arguments") or ""
                block.text += fragment
                yield ToolCallDelta(
                    index=block.index,
                    id=block.call_id or "",
                    arguments_delta=fragment,
                    name=block.name,
                )

            if isinstance(choice.get("finish_reason"), str):
                pending_finish = map_finish_reason(choice["finish_reason"])

        # usage 可能附在 finish chunk，也可能是尾随的 usage-only chunk —— 取最新。
        if chunk.get("usage"):
            pending_usage = map_usage(chunk["usage"])

    # parse_sse 保证 [DONE] 哨兵（否则抛错）；走到这里说明载荷源违约了。
    raise LlmError("SSE payload stream ended without [DONE]", "STREAM_CLOSED")
