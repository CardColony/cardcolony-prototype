# -*- coding: utf-8 -*-
"""llm_openai_compat · wire 格式（OpenAI 兼容 chat-completions）。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm-deepseek/src/types.ts（152 行）

原版是 TS interface（类型 only）；Python 侧用 ``TypedDict`` 表达同一形状——运行时
就是 dict，适配器直接读写 JSON 解析结果。
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

__all__ = [
    "WireRequest",
    "WireSystemMessage",
    "WireUserMessage",
    "WireToolMessage",
    "WireAssistantMessage",
    "WireMessage",
    "WireToolCall",
    "WireTool",
    "WireChunk",
    "WireChoice",
    "WireDelta",
    "WireToolCallDelta",
    "WireUsage",
    "WireError",
]


class WireRequest(TypedDict, total=False):
    """``POST {baseURL}/chat/completions`` 的请求体。原版 types.ts:13-30。"""

    model: str
    messages: list["WireMessage"]
    stream: Literal[True]
    stream_options: dict[str, bool]
    # 思考模式开关（**顶层**，不是 extra_body 里）。原版 types.ts:18-19。
    thinking: dict[str, str]
    reasoning_effort: str
    tools: list["WireTool"]
    temperature: float
    max_tokens: int
    stop: list[str]


class WireSystemMessage(TypedDict):
    """system 角色：单串指令。原版 types.ts:33-36。"""

    role: str
    content: str


class WireUserMessage(TypedDict):
    """user 角色：单串输入。原版 types.ts:39-42。"""

    role: str
    content: str


class WireToolMessage(TypedDict):
    """tool 角色：一次工具调用的结果，按 call id 关联。原版 types.ts:45-49。"""

    role: str
    tool_call_id: str
    content: str


class WireAssistantMessage(TypedDict, total=False):
    """assistant 历史消息。原版 types.ts:63-73。

    ``content`` 在纯工具轮回放 ``""``（**绝不 null**）；``reasoning_content`` 只在
    带 tool_calls 的轮次回放。
    """

    role: str
    content: Optional[str]
    reasoning_content: str
    tool_calls: list["WireToolCall"]


WireMessage = dict  # 判别联合（role 判别）；Python 侧即 dict


class WireToolCall(TypedDict):
    """assistant 历史里回放的一次已完成工具调用。原版 types.ts:76-80。"""

    id: str
    type: str
    function: dict[str, str]


class WireTool(TypedDict):
    """请求 ``tools`` 数组的一项。原版 types.ts:83-90。"""

    type: str
    function: dict[str, Any]


class WireChunk(TypedDict, total=False):
    """一条已解析的 SSE ``data:`` 载荷（chat.completion.chunk）。原版 types.ts:93-97。"""

    choices: list["WireChoice"]
    usage: Optional["WireUsage"]


class WireChoice(TypedDict, total=False):
    """一条流式 choice；``finish_reason`` 仅在其终帧非空。原版 types.ts:100-103。"""

    delta: "WireDelta"
    finish_reason: Optional[str]


class WireDelta(TypedDict, total=False):
    """一条流式 choice 的增量内容。原版 types.ts:106-116。"""

    role: str
    content: Optional[str]
    reasoning_content: Optional[str]
    tool_calls: list["WireToolCallDelta"]


class WireToolCallDelta(TypedDict, total=False):
    """一次工具调用的流式分片；同 ``index`` 的分片拼接成一次调用。原版 types.ts:119-131。"""

    index: int
    id: str
    type: str
    function: dict[str, Optional[str]]


class WireUsage(TypedDict, total=False):
    """wire token 记账。``prompt_tokens`` **含**缓存命中。原版 types.ts:140-147。"""

    prompt_tokens: int
    completion_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int
    prompt_tokens_details: dict[str, int]
    completion_tokens_details: dict[str, int]


class WireError(TypedDict, total=False):
    """非 2xx 错误体。原版 types.ts:150-152。"""

    error: dict[str, str]
