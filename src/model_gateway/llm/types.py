# -*- coding: utf-8 -*-
"""模型层底座 · 核心词汇表（provider 无关的消息 / 流式协议）。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm/src/types.ts（356 行）

命名口径：字段名用 Python 蛇形，**注释标出原版驼峰名**（如 ``block_type`` ←
``blockType``），保证「逐条语义可对账」且符合本仓风格。

未抄清单（原版有、本系统无消费方，对应点在此说明）：
    - ``ImageBlock`` / ``ContentBlockMap['image']``：DeepSeek wire 路线 text-only，
      图片块无落点（原版 types.ts:65-75）。
    - ``LlmProviderInfo`` / ``LlmConfigurableProvider`` / ``LlmModelDiscoveryRequest`` /
      ``LlmDiscoveredModel`` / ``LlmModelInfo`` / ``LlmModelContext`` /
      ``LlmReasoningEffortInfo`` / ``LlmModelReasoningInfo`` / ``LlmResolvedModelInfo`` /
      ``ModelModality``：dsh 宿主「模型目录 / 设置界面 / 能力探测」面（types.ts:143-281）。
      本系统模型声明由 ``config/models.yaml`` + ``src/api/model_config.py`` 承担，
      不经运行时目录查询，故不抄。
    - ``declare module '@deepseek-ai/cordis'`` 事件声明（types.ts:12-25）：cordis 宿主特有。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Optional, Union

__all__ = [
    "LlmFailure",
    "TextBlock",
    "ReasoningBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ContentBlock",
    "StopReason",
    "ToolCallsReason",
    "MaxTokensReason",
    "AbortedReason",
    "ErrorReason",
    "FinishReason",
    "TokenUsage",
    "ToolSchema",
    "GenerateOptions",
    "BlockStart",
    "TextDelta",
    "ReasoningDelta",
    "ToolCallDelta",
    "BlockEnd",
    "UsageChunk",
    "FinishChunk",
    "StreamChunk",
    "block_to_dict",
    "block_from_dict",
    "chunk_to_dict",
    "chunk_from_dict",
]


# ---------------------------------------------------------------------------
# LlmFailure —— 原版 types.ts:39-51
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LlmFailure:
    """可序列化的 provider / 传输失败事实；是否可重试由策略裁定。

    原版字段：``message`` / ``code`` / ``status?`` / ``providerRetryAfterMs?`` /
    ``requestId?``。本系统把 ``providerRetryAfterMs`` 落为 ``provider_retry_after_ms``。
    """

    message: str
    code: str
    status: Optional[int] = None
    provider_retry_after_ms: Optional[float] = None
    request_id: Optional[str] = None


# ---------------------------------------------------------------------------
# ContentBlock 四型 —— 原版 types.ts:53-110（image 不抄）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TextBlock:
    """面向终端用户的纯文本。原版 types.ts:54-57。"""

    text: str
    type: ClassVar[str] = "text"


@dataclass(frozen=True)
class ReasoningBlock:
    """思考 / 推理内容，与可见文本分离。原版 types.ts:60-63。"""

    text: str
    type: ClassVar[str] = "reasoning"


@dataclass(frozen=True)
class ToolCallBlock:
    """模型发起的一次工具调用。原版 types.ts:78-85。

    ``arguments`` 是模型产出的**原始 JSON 字符串**（原版注释：「Raw JSON string as
    produced by the model」），不是已解析对象。
    """

    id: str
    name: str
    arguments: str
    type: ClassVar[str] = "tool-call"


@dataclass(frozen=True)
class ToolResultBlock:
    """一次工具调用的结果，回送给模型。原版 types.ts:88-93。"""

    tool_call_id: str
    content: tuple["ContentBlock", ...] = ()
    is_error: bool = False
    type: ClassVar[str] = "tool-result"


ContentBlock = Union[TextBlock, ReasoningBlock, ToolCallBlock, ToolResultBlock]


# ---------------------------------------------------------------------------
# FinishReason —— 原版 types.ts:116-125
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StopReason:
    """原版 types.ts:117。"""

    kind: ClassVar[str] = "stop"


@dataclass(frozen=True)
class ToolCallsReason:
    """原版 types.ts:118。"""

    kind: ClassVar[str] = "tool-calls"


@dataclass(frozen=True)
class MaxTokensReason:
    """原版 types.ts:119。"""

    kind: ClassVar[str] = "max-tokens"


@dataclass(frozen=True)
class AbortedReason:
    """原版 types.ts:120（``{kind:'aborted', failure}``）。"""

    failure: LlmFailure
    kind: ClassVar[str] = "aborted"


@dataclass(frozen=True)
class ErrorReason:
    """原版 types.ts:121（``{kind:'error', failure}``）。"""

    failure: LlmFailure
    kind: ClassVar[str] = "error"


FinishReason = Union[StopReason, ToolCallsReason, MaxTokensReason, AbortedReason, ErrorReason]


# ---------------------------------------------------------------------------
# TokenUsage —— 原版 types.ts:127-141
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenUsage:
    """单次模型调用的 token 记账（缓存字段可选）。

    **不重叠口径**（原版 types.ts:130-134）：``input_tokens`` 只算**未命中缓存**的
    输入；缓存命中单列为 ``cache_read_tokens`` / ``cache_write_tokens``（计费输入 =
    三者之和）。把缓存命中折进总 prompt 数的适配器（DeepSeek 的 ``prompt_tokens``）
    须把它减出去。
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None


# ---------------------------------------------------------------------------
# ToolSchema / GenerateOptions —— 原版 types.ts:312-356
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolSchema:
    """发给模型的工具 JSON-schema 描述。原版 types.ts:312-317。"""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class GenerateOptions:
    """一次组装完毕的模型请求。原版 types.ts:320-356。

    字段序与原版略有出入：Python dataclass 要求「无默认值字段在前」，故把原版插在
    ``reasoningEffort`` 之前的 ``messages`` 提前到 ``model`` 之后；语义一一对应。
    """

    provider: str
    model: str
    messages: tuple[Any, ...]
    reasoning_effort: Optional[str] = None
    system: Optional[str] = None
    tools: Optional[tuple[ToolSchema, ...]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[tuple[str, ...]] = None
    signal: Any = None
    session_id: Optional[str] = None
    purpose: Optional[str] = None
    # ★ 本系统扩展（原版无此概念，原版只有流式）：``call`` 路径需**一次性非流式取回**
    #   （``tool_fold`` 折叠压缩走它，且历史上非流式不发 stream_options，换成流式有
    #   端点拒收 stream_options 的风险）。默认 True = 流式（主路径）。
    #   deepseek 适配器忽略此位（它只有流式一条路）；litellm 薄壳据此决定
    #   ``acompletion(stream=...)``。
    stream: bool = True


# ---------------------------------------------------------------------------
# StreamChunk 七态 —— 原版 types.ts:283-303
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BlockStart:
    """原版 types.ts:292。``block_type`` ← ``blockType``。"""

    index: int
    block_type: str
    type: ClassVar[str] = "block-start"


@dataclass(frozen=True)
class TextDelta:
    """原版 types.ts:293。"""

    index: int
    text: str
    type: ClassVar[str] = "text-delta"


@dataclass(frozen=True)
class ReasoningDelta:
    """原版 types.ts:294。"""

    index: int
    text: str
    type: ClassVar[str] = "reasoning-delta"


@dataclass(frozen=True)
class ToolCallDelta:
    """原版 types.ts:295。``arguments_delta`` ← ``argumentsDelta``。

    原版字段序 ``{index, id, name?, argumentsDelta}``；Python 要求必填在前，故
    ``name`` 后置。
    """

    index: int
    id: str
    arguments_delta: str
    name: Optional[str] = None
    type: ClassVar[str] = "tool-call-delta"


@dataclass(frozen=True)
class BlockEnd:
    """原版 types.ts:296。"""

    index: int
    block: ContentBlock
    type: ClassVar[str] = "block-end"


@dataclass(frozen=True)
class UsageChunk:
    """原版 types.ts:297。"""

    usage: TokenUsage
    type: ClassVar[str] = "usage"


@dataclass(frozen=True)
class FinishChunk:
    """原版 types.ts:298-303。``replay_state`` ← ``replayState``。"""

    reason: FinishReason
    replay_state: Any = None
    type: ClassVar[str] = "finish"


StreamChunk = Union[
    BlockStart,
    TextDelta,
    ReasoningDelta,
    ToolCallDelta,
    BlockEnd,
    UsageChunk,
    FinishChunk,
]


# ---------------------------------------------------------------------------
# 协议编解码（dict 往返）
# ---------------------------------------------------------------------------
# ★ 说明：原版是 TypeScript 的 plain object，JSON.stringify 天然可往返，故无显式 codec。
#   Python 侧协议是 dataclass，判据「六态协议序列化往返」需要一个最小 codec；
#   本系统目前**没有**生产消费方（chunk 不落盘），此函数组只服务协议自洽性验证
#   与未来可能的原始 chunk 日志。**不改变任何协议语义**。
_BLOCK_CLASSES = {
    "text": TextBlock,
    "reasoning": ReasoningBlock,
    "tool-call": ToolCallBlock,
    "tool-result": ToolResultBlock,
}
_CHUNK_CLASSES = {
    "block-start": BlockStart,
    "text-delta": TextDelta,
    "reasoning-delta": ReasoningDelta,
    "tool-call-delta": ToolCallDelta,
    "block-end": BlockEnd,
    "usage": UsageChunk,
    "finish": FinishChunk,
}


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """ContentBlock → 纯 dict（``tool-result`` 递归转换其嵌套 content）。"""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ReasoningBlock):
        return {"type": "reasoning", "text": block.text}
    if isinstance(block, ToolCallBlock):
        return {"type": "tool-call", "id": block.id, "name": block.name, "arguments": block.arguments}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool-result",
            "tool_call_id": block.tool_call_id,
            "content": [block_to_dict(b) for b in block.content],
            "is_error": block.is_error,
        }
    raise ValueError(f"unknown content block: {block!r}")


def block_from_dict(data: dict[str, Any]) -> ContentBlock:
    """纯 dict → ContentBlock（:func:`block_to_dict` 的逆）。"""
    kind = data["type"]
    if kind == "text":
        return TextBlock(text=data["text"])
    if kind == "reasoning":
        return ReasoningBlock(text=data["text"])
    if kind == "tool-call":
        return ToolCallBlock(id=data["id"], name=data["name"], arguments=data["arguments"])
    if kind == "tool-result":
        return ToolResultBlock(
            tool_call_id=data["tool_call_id"],
            content=tuple(block_from_dict(b) for b in data.get("content", ())),
            is_error=bool(data.get("is_error", False)),
        )
    raise ValueError(f"unknown content block type: {kind}")


def _usage_to_dict(usage: TokenUsage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


def _usage_from_dict(data: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        cache_read_tokens=data.get("cache_read_tokens"),
        cache_write_tokens=data.get("cache_write_tokens"),
        reasoning_tokens=data.get("reasoning_tokens"),
    )


def _failure_to_dict(failure: LlmFailure) -> dict[str, Any]:
    return {
        "message": failure.message,
        "code": failure.code,
        "status": failure.status,
        "provider_retry_after_ms": failure.provider_retry_after_ms,
        "request_id": failure.request_id,
    }


def _failure_from_dict(data: dict[str, Any]) -> LlmFailure:
    return LlmFailure(
        message=data["message"],
        code=data["code"],
        status=data.get("status"),
        provider_retry_after_ms=data.get("provider_retry_after_ms"),
        request_id=data.get("request_id"),
    )


def _finish_to_dict(reason: FinishReason) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": reason.kind}
    failure = getattr(reason, "failure", None)
    if failure is not None:
        out["failure"] = _failure_to_dict(failure)
    return out


def _finish_from_dict(data: dict[str, Any]) -> FinishReason:
    kind = data["kind"]
    if kind == "stop":
        return StopReason()
    if kind == "tool-calls":
        return ToolCallsReason()
    if kind == "max-tokens":
        return MaxTokensReason()
    if kind == "aborted":
        return AbortedReason(failure=_failure_from_dict(data["failure"]))
    if kind == "error":
        return ErrorReason(failure=_failure_from_dict(data["failure"]))
    raise ValueError(f"unknown finish reason kind: {kind}")


def chunk_to_dict(chunk: StreamChunk) -> dict[str, Any]:
    """StreamChunk → 纯 dict（七态一一对应）。"""
    if isinstance(chunk, BlockStart):
        return {"type": "block-start", "index": chunk.index, "block_type": chunk.block_type}
    if isinstance(chunk, TextDelta):
        return {"type": "text-delta", "index": chunk.index, "text": chunk.text}
    if isinstance(chunk, ReasoningDelta):
        return {"type": "reasoning-delta", "index": chunk.index, "text": chunk.text}
    if isinstance(chunk, ToolCallDelta):
        return {
            "type": "tool-call-delta",
            "index": chunk.index,
            "id": chunk.id,
            "name": chunk.name,
            "arguments_delta": chunk.arguments_delta,
        }
    if isinstance(chunk, BlockEnd):
        return {"type": "block-end", "index": chunk.index, "block": block_to_dict(chunk.block)}
    if isinstance(chunk, UsageChunk):
        return {"type": "usage", "usage": _usage_to_dict(chunk.usage)}
    if isinstance(chunk, FinishChunk):
        return {"type": "finish", "reason": _finish_to_dict(chunk.reason), "replay_state": chunk.replay_state}
    raise ValueError(f"unknown stream chunk: {chunk!r}")


def chunk_from_dict(data: dict[str, Any]) -> StreamChunk:
    """纯 dict → StreamChunk（:func:`chunk_to_dict` 的逆）。"""
    kind = data["type"]
    if kind == "block-start":
        return BlockStart(index=data["index"], block_type=data["block_type"])
    if kind == "text-delta":
        return TextDelta(index=data["index"], text=data["text"])
    if kind == "reasoning-delta":
        return ReasoningDelta(index=data["index"], text=data["text"])
    if kind == "tool-call-delta":
        return ToolCallDelta(
            index=data["index"],
            id=data["id"],
            arguments_delta=data["arguments_delta"],
            name=data.get("name"),
        )
    if kind == "block-end":
        return BlockEnd(index=data["index"], block=block_from_dict(data["block"]))
    if kind == "usage":
        return UsageChunk(usage=_usage_from_dict(data["usage"]))
    if kind == "finish":
        return FinishChunk(reason=_finish_from_dict(data["reason"]), replay_state=data.get("replay_state"))
    raise ValueError(f"unknown stream chunk type: {kind}")
