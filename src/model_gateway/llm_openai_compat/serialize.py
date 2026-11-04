# -*- coding: utf-8 -*-
"""llm_openai_compat · harness 消息 → OpenAI 兼容 chat-completions 请求体。

模板出处：原 ``llm_deepseek/serialize.py``（187 行）——消息序列化**逐字沿用**
（五家 wire 同构），仅把「思考参数」一段改为经 :mod:`dialects` 按供应商分派。

用户文本拼接；assistant 文本进 ``content``、工具调用进 ``tool_calls``、工具结果变成
**独立**的 ``role:'tool'`` 消息；assistant 的 reasoning 只在**带 tool_calls 的轮次**回放
为 ``reasoning_content``（thinking-mode passback 要求）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..llm.errors import LlmError
from ..llm.message import Message
from ..llm.types import ContentBlock, GenerateOptions
from .dialects import ThinkingSpec, apply_thinking

__all__ = ["RequestDefaults", "serialize_messages", "serialize_request"]


@dataclass(frozen=True)
class RequestDefaults:
    """适配器级请求默认值。

    模板：原 ``llm_deepseek/serialize.py::RequestDefaults``（``thinking`` /
    ``reasoningEffort``）。新增 ``vendor``（方言分派）与 ``default_effort``
    （models.yaml 的档位声明）。
    """

    vendor: Optional[str] = None
    thinking: Optional[str] = None
    default_effort: Optional[str] = None
    reasoning_effort: Optional[str] = None


def _flatten_text(blocks: tuple[ContentBlock, ...]) -> str:
    """拼接一条消息里的 text 块。模板 serialize.ts:56-61。"""
    return "".join(b.text for b in blocks if getattr(b, "type", None) == "text")


def _assert_text_only(blocks: tuple[ContentBlock, ...]) -> None:
    """在任何「拍平文本」路径**静默抹掉**图片之前显式拒绝。模板 serialize.ts:64-68。"""
    for block in blocks:
        if getattr(block, "type", None) == "image":
            raise LlmError(
                "The OpenAI-compatible adapter does not support image content.",
                "UNSUPPORTED_CONTENT",
            )
        if getattr(block, "type", None) == "tool-result":
            _assert_text_only(block.content)


def _serialize_assistant(message: Message) -> dict[str, Any]:
    """序列化一条 assistant 消息。模板 serialize.ts:71-102。"""
    text = _flatten_text(message.content)
    reasoning = "".join(b.text for b in message.content if getattr(b, "type", None) == "reasoning")
    tool_calls = [
        {"id": b.id, "type": "function", "function": {"name": b.name, "arguments": b.arguments}}
        for b in message.content
        if getattr(b, "type", None) == "tool-call"
    ]
    out: dict[str, Any] = {
        "role": "assistant",
        # ★ 模板 serialize.ts:86-95：无文本轮发 ``""``——**绝不 null**（reasoning-only 轮
        #   发 null 会被 API 400 "content or tool_calls must be set"，且该消息耐久留在会话里）。
        "content": text,
    }
    if tool_calls and reasoning:
        out["reasoning_content"] = reasoning
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def serialize_messages(messages: tuple[Any, ...]) -> list[dict[str, Any]]:
    """序列化整段对话。模板 serialize.ts:112-141。"""
    wire: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            # 本系统调用层消息本就是 wire dict（既有形态，C3 零改动）——直通。
            wire.append(dict(message))
            continue
        _assert_text_only(message.content)
        if message.role == "system":
            wire.append({"role": "system", "content": _flatten_text(message.content)})
            continue
        if message.role == "assistant":
            wire.append(_serialize_assistant(message))
            continue
        tool_results = [b for b in message.content if getattr(b, "type", None) == "tool-result"]
        text = _flatten_text(message.content)
        if text != "" or not tool_results:
            wire.append({"role": "user", "content": text})
        for result in tool_results:
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": _flatten_text(result.content) or "(no output)",
                }
            )
    return wire


def serialize_request(options: GenerateOptions, defaults: Optional[RequestDefaults] = None) -> dict[str, Any]:
    """构造完整 wire 请求体。模板 serialize.ts:151-187 + dialects 分派。"""
    defaults = defaults if defaults is not None else RequestDefaults()
    messages: list[dict[str, Any]] = []
    if options.system is not None:
        messages.append({"role": "system", "content": options.system})
    messages.extend(serialize_messages(tuple(options.messages)))

    # [2026-11-04 实测修复] 带 tool_calls 的 assistant 消息**必须回传非空推理
    # 字段**：commandcode 的 reflection/beam:free、stealth/glyph-cluster:free
    # 实测缺字段 / 空串 → 400 "Invalid request"（AI_APICallError），补非空
    # reasoning_content 或 reasoning → 200。字段在投影链上会丢：
    #   · context_regions 只在 `m.get("reasoning_content")` 非空时保留；
    #   · 旧会话消息、系统注入消息本就没有该字段；
    #   · _serialize_assistant 只在有 reasoning 块时写。
    # 故在 wire 出口统一兜底（只补缺失，绝不改写已有内容）——与 runner 的
    # REASONING_PLACEHOLDER 同文案，非新机制。
    _NO_REASONING = "(系统注入的消息，无模型思考记录)"
    for msg in messages:
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        if any(str(msg.get(k) or "").strip()
               for k in ("reasoning_content", "reasoning")):
            continue
        msg["reasoning_content"] = _NO_REASONING

    body: dict[str, Any] = {
        "model": options.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    if options.tools is not None:
        tools = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            if not isinstance(t, dict)
            else (t if "type" in t else {"type": "function", "function": t})
            for t in options.tools
        ]
        if tools:
            body["tools"] = tools
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.max_tokens is not None:
        body["max_tokens"] = options.max_tokens
    if options.stop is not None:
        body["stop"] = list(options.stop)

    # 思考参数：按供应商方言写进 body 顶层（模板 serialize.ts:171 的 resolveThinking）
    apply_thinking(
        body,
        ThinkingSpec(
            vendor=defaults.vendor,
            thinking=defaults.thinking,
            default_effort=defaults.default_effort if defaults.default_effort is not None else defaults.reasoning_effort,
        ),
        requested_effort=options.reasoning_effort,
        purpose=options.purpose,
    )
    return body
