# -*- coding: utf-8 -*-
"""模型层底座 · 增量装配器（chunk → message）。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm/src/assembler.ts（164 行）

这是把原始 ``StreamChunk`` 流装配成完整 ``ContentBlock`` / assistant ``Message`` 的
**唯一规范算法**（原版 assembler.ts:1-6）。

容错两条（原版 assembler.ts:30-34）：
    1. 容忍 delta-only 协议（无 block-start / block-end）——按 delta 类型自动开块；
    2. 已被 block-end 关闭的块，其**迟到 delta 一律丢弃**（坏适配器不能撑爆内存 /
       污染已完成块）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .message import Message, MessageSource, UserMessageSource, create_message
from .types import (
    BlockEnd,
    BlockStart,
    ContentBlock,
    FinishReason,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    StreamChunk,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDelta,
    TokenUsage,
    UsageChunk,
)

__all__ = ["BlockAssembler"]


@dataclass
class _PartialBlock:
    """一个装配中的块。原版 assembler.ts:15-23。"""

    block_type: str
    text: str = ""
    tool_call_id: Optional[str] = None
    tool_call_name: Optional[str] = None
    tool_call_arguments: str = ""
    # 由 block-end 设定——权威值，并冻结该 partial（原版 assembler.ts:21-22）。
    block: Optional[ContentBlock] = None


class BlockAssembler:
    """把原始 ``StreamChunk`` 增量装配成完整块与最终 assistant 消息。

    原版 ``BlockAssembler``（assembler.ts:36-164）。调用方在流进行中喂 ``push``，
    流结束后读 ``blocks()`` / ``message()`` / ``usage`` / ``finish``。
    """

    def __init__(self) -> None:
        self._partials: dict[int, _PartialBlock] = {}
        self._order: list[int] = []
        self._usage: Optional[TokenUsage] = None
        self._finish: Optional[FinishReason] = None
        self._replay_state: object = None

    # -- push —— 原版 assembler.ts:47-94 ------------------------------------
    def push(self, chunk: StreamChunk) -> None:
        """把一个 chunk 喂进装配状态（按流序）。"""
        kind = chunk.type
        if kind == "block-start":
            assert isinstance(chunk, BlockStart)
            if chunk.index not in self._partials:
                self._order.append(chunk.index)
                self._partials[chunk.index] = _PartialBlock(block_type=chunk.block_type)
            return
        if kind in ("text-delta", "reasoning-delta"):
            assert isinstance(chunk, (TextDelta, ReasoningDelta))
            partial = self._ensure(chunk.index, "text" if kind == "text-delta" else "reasoning")
            if partial.block is not None:
                return  # 已被 block-end 关闭；迟到 delta 丢弃
            partial.text += chunk.text
            return
        if kind == "tool-call-delta":
            assert isinstance(chunk, ToolCallDelta)
            partial = self._ensure(chunk.index, "tool-call")
            if partial.block is not None:
                return
            partial.tool_call_id = chunk.id
            if chunk.name:
                partial.tool_call_name = chunk.name
            partial.tool_call_arguments += chunk.arguments_delta
            return
        if kind == "block-end":
            assert isinstance(chunk, BlockEnd)
            partial = self._ensure(chunk.index, chunk.block.type)
            # 首次关闭生效；忽略重复关闭的迟到帧，保证「流式输出」与「最终块」一致。
            if partial.block is not None:
                return
            partial.block = chunk.block
            return
        if kind == "usage":
            assert isinstance(chunk, UsageChunk)
            self._usage = chunk.usage
            return
        if kind == "finish":
            self._finish = chunk.reason  # type: ignore[union-attr]
            self._replay_state = getattr(chunk, "replay_state", None)
            return
        return

    # -- ensure / assemble —— 原版 assembler.ts:96-126 ----------------------
    def _ensure(self, index: int, block_type: str) -> _PartialBlock:
        partial = self._partials.get(index)
        if partial is None:
            partial = _PartialBlock(block_type=block_type)
            self._partials[index] = partial
            self._order.append(index)
        return partial

    def _assemble(self, partial: _PartialBlock, index: int) -> ContentBlock:
        if partial.block is not None:
            return partial.block
        if partial.block_type == "text":
            return TextBlock(text=partial.text)
        if partial.block_type == "reasoning":
            return ReasoningBlock(text=partial.text)
        if partial.block_type == "tool-call":
            return ToolCallBlock(
                id=partial.tool_call_id if partial.tool_call_id is not None else f"call-{index}",
                name=partial.tool_call_name if partial.tool_call_name is not None else "",
                arguments=partial.tool_call_arguments,
            )
        raise ValueError(f'cannot assemble incomplete block of type "{partial.block_type}"')

    def _must_get(self, index: int) -> _PartialBlock:
        partial = self._partials.get(index)
        if partial is None:
            raise ValueError(f"BlockAssembler invariant violated: no partial for index {index}")
        return partial

    # -- 读取面 —— 原版 assembler.ts:134-163 --------------------------------
    def blocks(self) -> list[ContentBlock]:
        """按流序装配目前看到的所有块。

        原版 assembler.ts:134-139：``max-tokens`` 截断会**丢弃无法安全执行的 tool-call**；
        未闭合块按其累积 delta 装配（未知块类型且从未被 block-end 关闭则抛错）。
        """
        blocks = [self._assemble(self._must_get(i), i) for i in self._order]
        if self.finish.kind == "max-tokens":
            return [b for b in blocks if b.type != "tool-call"]
        return blocks

    @property
    def usage(self) -> Optional[TokenUsage]:
        """来自 ``usage`` chunk 的用量；未到达前为 None。原版 assembler.ts:142-144。"""
        return self._usage

    @property
    def finish(self) -> FinishReason:
        """``finish`` chunk 的收尾原因；流未给则以 ``stop`` 兜底。原版 assembler.ts:147-149。"""
        return self._finish if self._finish is not None else StopReason()

    @property
    def replay_state(self) -> object:
        """终帧携带的适配器私有重放状态（如有）。原版 assembler.ts:152-154。"""
        return self._replay_state

    def message(self, source: Optional[MessageSource] = None) -> Message:
        """装配出的 assistant 消息。原版 assembler.ts:161-163。

        原版默认 source 是 ``{kind:'plugin', plugin:'dsh-llm/assembler'}``；本系统无
        plugin 源，默认落 ``UserMessageSource()``（调用方一般显式传 source）。
        """
        return create_message(
            role="assistant",
            content=tuple(self.blocks()),
            source=source if source is not None else UserMessageSource(),
        )
