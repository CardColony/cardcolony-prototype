# -*- coding: utf-8 -*-
"""[tool_calls 空串覆盖 2026-10-21] ark-code-latest 流式工具调用被丢弃。

实测原始帧（火山 plan 端点）：
    第 1 片: {"tool_calls":[{"function":{"arguments":"","name":"bash"},"id":"call_x","index":0,"type":"function"}]}
    第 2 片: {"tool_calls":[{"function":{"arguments":"{","name":""},"id":"","index":0}]}
    第 3 片: {"tool_calls":[{"function":{"arguments":"\"command\": \"date\"}","name":"","id":""},"index":0}]}
    末  片: {"delta":{"content":""},"finish_reason":"tool_calls"}

后续分片的 `id`/`name` 是**空串**（不是 None）。translate 原来写「非 None 就覆盖」，
于是空串把首片的 `bash`/`call_x` 覆盖成 "" ⇒ 工具名空 ⇒ 上层认不出 ⇒ 整个调用被丢。
现象：模型 `finish_reason=tool_calls`，我们解析出 0 个调用 ⇒「调一次就断、根本不执行」。
"""
from __future__ import annotations

import json

from src.model_gateway.llm.types import BlockEnd, ToolCallBlock
from src.model_gateway.llm_openai_compat.translate import translate


async def _collect(payloads):
    async def gen():
        for p in payloads:
            yield p
    return [c async for c in translate(gen())]


async def _blocks(payloads):
    out = []
    for c in await _collect(payloads):
        if isinstance(c, BlockEnd):
            out.append(c.block)
    return out


async def test_tool_call_survives_empty_string_fragments():
    """ark 的后续分片带空串 id/name——不得覆盖首片的真值。"""
    payloads = [
        json.dumps({"choices": [{"delta": {"role": "assistant", "tool_calls": [
            {"function": {"arguments": "", "name": "bash"}, "id": "call_x",
             "index": 0, "type": "function"}]}}]}),
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": "{", "name": ""}, "id": "", "index": 0}]}}]}),
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": "\"command\": \"date\"}", "name": "", "id": ""},
             "index": 0}]}}]}),
        json.dumps({"choices": [{"delta": {"content": "", "role": "assistant"},
                                 "finish_reason": "tool_calls"}]}),
        "[DONE]",
    ]
    blocks = [b for b in await _blocks(payloads) if isinstance(b, ToolCallBlock)]
    assert len(blocks) == 1, f"应解析出 1 个工具调用，实为 {len(blocks)}"
    b = blocks[0]
    assert b.name == "bash", f"工具名被空串覆盖了：{b.name!r}"
    assert b.id == "call_x", f"调用 id 被空串覆盖了：{b.id!r}"
    assert json.loads(b.arguments) == {"command": "date"}, b.arguments


async def test_multi_tool_calls_by_index_still_work():
    """多个工具按 index 分桶，互不串味（标准 OpenAI 分片形态）。"""
    payloads = [
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": "", "name": "bash"}, "id": "c1", "index": 0},
            {"function": {"arguments": "", "name": "python"}, "id": "c2", "index": 1}]}}]}),
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": "{\"command\":\"ls\"}", "name": ""}, "id": "", "index": 0},
            {"function": {"arguments": "{\"code\":\"1\"}", "name": ""}, "id": "", "index": 1}]}}]}),
        json.dumps({"choices": [{"delta": {"content": ""}, "finish_reason": "tool_calls"}]}),
        "[DONE]",
    ]
    blocks = [b for b in await _blocks(payloads) if isinstance(b, ToolCallBlock)]
    assert [b.name for b in blocks] == ["bash", "python"]
    assert [b.id for b in blocks] == ["c1", "c2"]
