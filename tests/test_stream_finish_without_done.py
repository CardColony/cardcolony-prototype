# -*- coding: utf-8 -*-
"""流收尾判据：``[DONE]`` 不是必需的，``finish_reason`` 才是。

[2026-10-21 · 用户令]「haiku5.5 现在的症状和之前的 ark 一模一样，是不是一样的问题？
一样的问题的话，为什么要一个模型一个模型的修？」

实测结论：haiku 和 ark **不是**同一个问题（ark 是工具分片空串覆盖，haiku 是流收尾判据）。
但 haiku 和 minimax 是同一个问题，根因在收尾判据：

    sse.py 原实现：EOF 时未见到 ``[DONE]`` ⇒ 无条件抛 STREAM_CLOSED

``[DONE]`` 是 OpenAI 自己的哨兵，**不是 SSE 规范**。nous（跑 anthropic 模型）、
minimax 这些兼容端点直接关连接、不发 ``[DONE]``，但会正常发 ``finish_reason``。
于是它们的流一律被判成「断了」，重试 3 次全失败。

修法：SSE 层 EOF 就是结束；完整性由翻译层按 ``finish_reason`` 判。
本文件钉住三条：有 finish_reason 无 DONE 必须成功、两者都没有必须判截断、
有 DONE 的老路径不许回退。
"""
from __future__ import annotations

import json

import pytest

from src.model_gateway.llm.errors import LlmError
from src.model_gateway.llm_openai_compat.sse import parse_sse
from src.model_gateway.llm_openai_compat.translate import translate


def _chunk(*, content: str = "", finish: str | None = None,
           tool_calls: list | None = None) -> str:
    delta: dict = {}
    if content:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    choice: dict = {"delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return json.dumps({"choices": [choice]})


async def _aiter(items):
    for it in items:
        yield it


async def _collect(payloads):
    return [c async for c in translate(_aiter(payloads))]


async def test_finish_reason_without_done_is_success():
    """nous / minimax 的形态：有 finish_reason，没有 [DONE] ⇒ 必须正常收尾。"""
    chunks = await _collect([
        _chunk(content="收到"),
        _chunk(finish="stop"),
    ])
    kinds = [type(c).__name__ for c in chunks]
    assert "FinishChunk" in kinds, kinds
    finish = [c for c in chunks if type(c).__name__ == "FinishChunk"][-1]
    assert finish.reason.kind == "stop", finish.reason


async def test_tool_call_without_done_is_success():
    """工具轮同样：有 finish_reason=tool_calls 就不算截断。"""
    chunks = await _collect([
        _chunk(tool_calls=[{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "bash", "arguments": ""},
        }]),
        _chunk(tool_calls=[{
            "index": 0, "id": "", "function": {"name": "", "arguments": "{\"command\":\"date\"}"},
        }]),
        _chunk(finish="tool_calls"),
    ])
    finish = [c for c in chunks if type(c).__name__ == "FinishChunk"][-1]
    assert finish.reason.kind == "tool-calls", finish.reason


async def test_no_done_and_no_finish_reason_is_still_truncated():
    """真截断（两者都没有）必须照旧判错——不许为了兼容把截断也放过去。"""
    with pytest.raises(LlmError) as ei:
        await _collect([_chunk(content="半句")])
    assert "STREAM_CLOSED" in str(getattr(ei.value, "code", "") or ei.value)


async def test_done_path_unchanged():
    """有 [DONE] 的老路径逐字不变。"""
    chunks = await _collect([
        _chunk(content="收到"),
        _chunk(finish="stop"),
        "[DONE]",
    ])
    finish = [c for c in chunks if type(c).__name__ == "FinishChunk"][-1]
    assert finish.reason.kind == "stop"


async def test_sse_eof_does_not_raise():
    """SSE 解析层：流自然结束就是结束，不再抛「没有 [DONE]」。"""
    raw = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    ]
    payloads = [p async for p in parse_sse(_aiter(raw))]
    assert len(payloads) == 2
    assert payloads[-1].startswith('{"choices"')


async def test_sse_done_still_stops():
    raw = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b"data: [DONE]\n\n",
        b'data: {"choices":[{"delta":{"content":"after"}}]}\n\n',
    ]
    payloads = [p async for p in parse_sse(_aiter(raw))]
    assert payloads[-1] == "[DONE]", "见到 [DONE] 就该收，不再吃后面的载荷"
