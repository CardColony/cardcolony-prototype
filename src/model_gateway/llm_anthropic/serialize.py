# -*- coding: utf-8 -*-
"""GenerateOptions → Anthropic Messages 请求体。

与 OpenAI 形状的差异（都是协议级，不是风格）：
    - ``system`` 是**顶层参数**，不是 role=system 的消息；
    - 工具结果不是 ``role:'tool'`` 独立消息，而是**并进一条 user 消息**的
      ``content:[{type:'tool_result', tool_use_id, content}]`` 块；
    - 带工具调用的 assistant 消息，``content`` 是块数组
      （text 块＋``{type:'tool_use', id, name, input}`` 块）；
    - 工具声明是 ``[{name, description, input_schema}]``（不是 function 包裹）；
    - ``max_tokens`` **必填**（协议要求；缺省由连接事实带下限值，见 adapter）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..llm.types import GenerateOptions, ToolSchema

__all__ = ["serialize_request", "serialize_messages"]


def _flatten_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(b.get("text", "")) if isinstance(b, dict) else str(getattr(b, "text", ""))
            for b in content
        ]
        return "".join(p for p in parts if p)
    return str(content)


def _tool_result_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_result",
            "tool_use_id": str(message.get("tool_call_id") or ""),
            "content": _flatten_text(message.get("content")) or "(no output)",
        }
    ]


def _serialize_assistant_wire(message: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 形状的 assistant 消息 → Anthropic 形状（含 tool_calls 翻译）。"""
    text = _flatten_text(message.get("content"))
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return {"role": "assistant", "content": text}
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in tool_calls:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "input": arguments,
            }
        )
    return {"role": "assistant", "content": blocks}


def serialize_messages(messages: tuple[Any, ...]) -> list[dict[str, Any]]:
    """把调用层消息（OpenAI 形状 wire dict 或 dataclass）译成 Anthropic 消息列表。

    role=tool 的结果**合并进相邻的 user 消息**（Anthropic 要求 tool_result 块
    位于 user 消息内；连续多个工具结果合成一条，避免条数爆炸）。
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            role = getattr(message, "role", None)
            if role == "assistant":
                content = _flatten_text(getattr(message, "content", None))
                wire.append({"role": "assistant", "content": content})
                continue
            role = "user"
            message = {"role": "user", "content": _flatten_text(getattr(message, "content", None))}
        role = message.get("role")
        if role == "system":
            # Anthropic 无 system 消息位；由 serialize_request 提为顶层参数。
            # 走到这里说明调用层把 system 混进了 messages——兜底拼进相邻 user。
            wire.append({"role": "user", "content": _flatten_text(message.get("content"))})
            continue
        if role == "tool":
            blocks = _tool_result_blocks(message)
            if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                wire[-1]["content"].extend(blocks)
            else:
                wire.append({"role": "user", "content": blocks})
            continue
        if role == "assistant":
            wire.append(_serialize_assistant_wire(message))
            continue
        wire.append({"role": "user", "content": _flatten_text(message.get("content"))})
    return wire


def _serialize_tools(tools: Optional[tuple[ToolSchema, ...]]) -> list[dict[str, Any]]:
    if not tools:
        return []
    return [
        {
            "name": str(t.name),
            "description": str(t.description),
            "input_schema": t.parameters if isinstance(t.parameters, dict) else {},
        }
        for t in tools
        if isinstance(t, ToolSchema)
    ]


def serialize_request(
    options: GenerateOptions,
    *,
    max_tokens: int,
    thinking: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造 Anthropic ``/messages`` 请求体。``max_tokens`` 由调用方保证有值（协议必填）。"""
    body: dict[str, Any] = {
        "model": options.model,
        "max_tokens": max_tokens,
        "messages": serialize_messages(tuple(options.messages)),
        # 与 llm_openai_compat 同口径：**永远走流式**——Anthropic 协议无
        # stream_options 之类的非流式副作用字段，流式是安全子集；组装器从
        # chunk 流重建完整结果，``options.stream`` 对本包不生效（call 路径
        # 传 False 时若真发非流式，响应体是普通 JSON、SSE 解析器会读到 0 事件）。
        "stream": True,
    }
    if options.system is not None:
        body["system"] = options.system
    tools = _serialize_tools(options.tools)
    if tools:
        body["tools"] = tools
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.stop:
        body["stop_sequences"] = list(options.stop)
    if thinking:
        body["thinking"] = thinking
    return body
