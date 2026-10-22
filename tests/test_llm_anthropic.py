# -*- coding: utf-8 -*-
"""llm_anthropic（Anthropic Messages 适配器，mavis 路由）单测。

纯函数＋假传输：不依赖 MiniMax Code App、零真实网络。
真实全链验证见 tests/test_mavis_live.py（无 App 自动跳过）。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest

from src.model_gateway.llm.types import (
    FinishChunk,
    GenerateOptions,
    ReasoningDelta,
    StopReason,
    TextDelta,
    ToolCallDelta,
    ToolCallsReason,
    ToolSchema,
)
from src.model_gateway.llm_anthropic.adapter import (
    AnthropicAdapterOptions,
    AnthropicConnectionOptions,
    AnthropicMessagesAdapter,
    _http_error_code,
)
from src.model_gateway.llm_anthropic.serialize import serialize_messages, serialize_request
from src.model_gateway.llm_anthropic.translate import map_stop_reason, map_usage, translate


# ---------------------------------------------------------------- serialize


def test_system_is_top_level_and_stream_always_true():
    options = GenerateOptions(
        provider="mavis",
        model="MiniMax-M3.1-Flash",
        messages=({"role": "user", "content": "hi"},),
        system="SYS",
        stream=False,  # call 路径传 False，但 wire 必须仍走流式
    )
    body = serialize_request(options, max_tokens=4096)
    assert body["system"] == "SYS"
    assert body["stream"] is True
    assert body["max_tokens"] == 4096
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_tool_results_merge_into_one_user_message():
    messages = (
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "t", "arguments": '{"a":1}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "结果一"},
        {"role": "tool", "tool_call_id": "call_2", "content": "结果二"},
    )
    wire = serialize_messages(messages)
    assert wire[0] == {"role": "user", "content": "查一下"}
    assistant = wire[1]
    assert assistant["role"] == "assistant"
    block = assistant["content"][0]
    assert block["type"] == "tool_use" and block["id"] == "call_1" and block["input"] == {"a": 1}
    merged = wire[2]
    assert merged["role"] == "user"
    assert [b["type"] for b in merged["content"]] == ["tool_result", "tool_result"]
    assert merged["content"][0]["tool_use_id"] == "call_1"
    assert len(wire) == 3  # 两条 tool 结果必须合成一条 user


def test_tools_use_input_schema_shape():
    options = GenerateOptions(
        provider="mavis",
        model="m",
        messages=({"role": "user", "content": "x"},),
        tools=(ToolSchema(name="read_card", description="读卡", parameters={"type": "object"}),),
    )
    body = serialize_request(options, max_tokens=64)
    assert body["tools"] == [
        {"name": "read_card", "description": "读卡", "input_schema": {"type": "object"}}
    ]


# ---------------------------------------------------------------- translate


async def _aiter(events: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for e in events:
        yield e


async def test_translate_text_thinking_tool_use_and_finish():
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "想"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "在"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "call_1", "name": "read_card"}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '{"card_id":'}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '"c1"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80}},
        {"type": "message_stop"},
    ]
    chunks = [c async for c in translate(_aiter(events))]
    kinds = [c.type for c in chunks]
    assert "reasoning-delta" in kinds and "text-delta" in kinds and "tool-call-delta" in kinds
    finish = [c for c in chunks if c.type == "finish"][-1]
    assert isinstance(finish.reason, ToolCallsReason)

    from src.model_gateway.llm.assembler import BlockAssembler

    assembler = BlockAssembler()
    for c in chunks:
        assembler.push(c)
    blocks = assembler.blocks()
    assert [b.text for b in blocks if getattr(b, "type", None) == "reasoning"] == ["想"]
    assert [b.text for b in blocks if getattr(b, "type", None) == "text"] == ["在"]
    tool_blocks = [b for b in blocks if getattr(b, "type", None) == "tool-call"]
    assert tool_blocks[0].id == "call_1"
    assert json.loads(tool_blocks[0].arguments) == {"card_id": "c1"}
    usage = assembler.usage
    assert usage.input_tokens == 20  # 100 - 80 缓存
    assert usage.cache_read_tokens == 80


def test_map_stop_reason_and_usage():
    assert isinstance(map_stop_reason("end_turn"), StopReason)
    assert isinstance(map_stop_reason("max_tokens"), type(map_stop_reason("max_tokens")))
    usage = map_usage({"input_tokens": 10, "output_tokens": 5,
                       "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
                       "output_tokens_details": {"thinking_tokens": 4}})
    assert usage.input_tokens == 5  # 10 - 3 - 2
    assert usage.cache_read_tokens == 3 and usage.cache_write_tokens == 2
    assert usage.reasoning_tokens == 4


def test_http_error_code_mapping():
    assert _http_error_code(401) == "AUTH"
    assert _http_error_code(403) == "AUTH"
    assert _http_error_code(429) == "RATE_LIMIT"
    assert _http_error_code(400) == "INVALID_REQUEST"
    assert _http_error_code(500) == "SERVER"
    assert _http_error_code(418) == "HTTP_418"


# ---------------------------------------------------------------- adapter（假传输）


class _FakeStreamResponse:
    def __init__(self, events: list[dict[str, Any]], status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        payload = "".join(
            f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events
        ).encode("utf-8")
        self._payload = payload

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self._payload

    async def aread(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


async def test_adapter_end_to_end_with_fake_transport(monkeypatch):
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "在"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    sent = {}

    async def fake_send(self, request, stream=False):  # noqa: ANN001
        sent["url"] = str(request.url)
        sent["auth"] = request.headers.get("authorization")
        sent["body"] = json.loads(request.content.decode())
        return _FakeStreamResponse(events)

    monkeypatch.setattr("httpx.AsyncClient.send", fake_send)

    async def token_getter() -> str:
        return "broker-token"

    adapter = AnthropicMessagesAdapter(
        AnthropicAdapterOptions(
            options=lambda p, m: AnthropicConnectionOptions(
                base_url="https://mavis.example/v1", token_getter=token_getter, max_tokens=128
            )
        )
    )
    options = GenerateOptions(
        provider="mavis", model="MiniMax-M3.1-Flash",
        messages=({"role": "user", "content": "hi"},), system="SYS",
    )
    chunks = [c async for c in adapter.stream(options)]
    assert sent["url"] == "https://mavis.example/v1/messages"
    assert sent["auth"] == "Bearer broker-token"
    assert sent["body"]["stream"] is True and sent["body"]["max_tokens"] == 128
    assert any(isinstance(c, TextDelta) and c.text == "在" for c in chunks)
    assert isinstance(chunks[-1], FinishChunk)
