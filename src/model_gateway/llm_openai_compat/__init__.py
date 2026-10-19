# -*- coding: utf-8 -*-
"""模型层底座 · llm_openai_compat 包（OpenAI 兼容直连适配器，**单适配器承接五家**）。

设计依据（2026-10-16）：五家（opencode/deepseek、aliyun/qwen、zhipu/glm、minimax、
nous/haiku）wire 协议**同构**——同为 OpenAI Chat Completions（``/chat/completions`` +
SSE + ``delta.content``），思考字段同为 ``delta.reasoning_content``。故按**传输方式**
（直连 fetch）分，不按厂商分：**一个适配器 + 一张方言表**。

    sse.py        SSE 字节流 → data 载荷（物理迁自 llm_deepseek，全仓唯一一份）
    translate.py  wire chunk → StreamChunk（同上）
    types.py      wire 格式 TypedDict（同上）
    serialize.py  harness 消息 → wire 请求（模板 llm_deepseek/serialize.py，泛化）
    dialects.py   **各厂差异唯一落点**：thinking 参数形态
    adapter.py    transport（httpx 流式）+ 错误码映射（模板 llm_deepseek/adapter.py，泛化）
"""

from __future__ import annotations

from .adapter import (
    DEFAULT_STREAM_IDLE_TIMEOUT_MS,
    CompatAdapterOptions,
    CompatConnectionOptions,
    OpenAICompatAdapter,
    http_error_code,
    provider_retry_after_ms,
)
from .dialects import DIALECTS, ThinkingSpec, apply_thinking, dialect_for
from .serialize import RequestDefaults, serialize_messages, serialize_request
from .sse import DONE, parse_sse
from .translate import map_finish_reason, map_usage, translate

__all__ = [
    "DONE",
    "parse_sse",
    "translate",
    "map_finish_reason",
    "map_usage",
    "serialize_request",
    "serialize_messages",
    "RequestDefaults",
    "OpenAICompatAdapter",
    "CompatAdapterOptions",
    "CompatConnectionOptions",
    "http_error_code",
    "provider_retry_after_ms",
    "DEFAULT_STREAM_IDLE_TIMEOUT_MS",
    "ThinkingSpec",
    "DIALECTS",
    "dialect_for",
    "apply_thinking",
]
