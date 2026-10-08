# -*- coding: utf-8 -*-
"""[信息流重出 Task4] stream_call 工具调用支持回归。

切主调用到 stream_call 的**前置安全网**：原 stream_call 收了 tools 形参却从不下发、
且恒返回 tool_calls=[]，直接切过去等于静默阉掉 function calling——工具循环整个失效。
本组用例钉住：tools 真下发、prior_messages 真进 messages、分片 arguments 能拼回、
截断参数交 truncated_tool_call 续写（与 call 同契约）。

零真实 key、零网络、零 works 触碰；不 import main。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.model_gateway.gateway import ModelGateway


def _tc_delta(index, tc_id=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=index, id=tc_id, function=fn)
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=None, tool_calls=[tc]),
        finish_reason=None)])


def _final_delta(content=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content), finish_reason="tool_calls")])


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setenv("FAKE_TEST_KEY", "sk-test-not-real")
    cfg = {"models": {"m": {
        "provider": "openai", "model": "fake/model", "api_key_env": "FAKE_TEST_KEY",
        "temperature": 0.3, "retry": {"max_attempts": 1, "backoff_seconds": 0},
    }}}
    return ModelGateway(cfg)


def _install(monkeypatch, chunks):
    seen = {}

    async def acompletion(**kwargs):
        seen.update(kwargs)
        return _FakeStream(chunks)

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    return seen


async def _run(gw, **kw):
    events = [ev async for ev in gw.stream_call("m", "sys", "user", **kw)]
    return events[-1], events


async def test_tools_are_actually_sent_to_provider(gw, monkeypatch):
    """核心回归：tools 必须真的进 kwargs（改前根本没有 tools 键）。"""
    seen = _install(monkeypatch, [_final_delta("")])
    tools = [{"name": "read_card", "description": "d",
              "parameters": {"type": "object", "properties": {}}}]
    await _run(gw, tools=tools)
    assert "tools" in seen, "tools 未下发——切过去等于阉掉工具循环"
    assert seen["tools"] == [{"type": "function", "function": tools[0]}]


async def test_prior_messages_land_before_user_turn(gw, monkeypatch):
    seen = _install(monkeypatch, [_final_delta("ok")])
    prior = [{"role": "user", "content": "上一轮"}, {"role": "assistant", "content": "上一轮答"}]
    await _run(gw, prior_messages=prior)
    msgs = seen["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1:3] == prior, "历史必须夹在 system 与当前提问之间"
    assert msgs[-1] == {"role": "user", "content": "user"}


async def test_no_prior_keeps_two_message_shape(gw, monkeypatch):
    seen = _install(monkeypatch, [_final_delta("ok")])
    await _run(gw)
    assert len(seen["messages"]) == 2


async def test_fragmented_tool_arguments_are_reassembled(gw, monkeypatch):
    """分片到达的 arguments 必须按序拼回可解析 JSON。"""
    _install(monkeypatch, [
        _tc_delta(0, tc_id="call_1", name="read_card", arguments='{"card_'),
        _tc_delta(0, arguments='id": "card_001"}'),
        _final_delta(None),
    ])
    final, _ = await _run(gw, tools=[{"name": "read_card", "parameters": {}}])
    assert final["tool_calls"] == [{"name": "read_card", "arguments": {"card_id": "card_001"}}]
    assert final["truncated_tool_call"] is None
    assert final["finish_reason"] == "tool_calls"


async def test_parallel_tool_calls_keep_index_order(gw, monkeypatch):
    """并行多个工具：按 index 还原，不被到达顺序打乱。"""
    _install(monkeypatch, [
        _tc_delta(1, tc_id="c2", name="list_cards", arguments='{"a":2}'),
        _tc_delta(0, tc_id="c1", name="read_card", arguments='{"b":1}'),
        _final_delta(None),
    ])
    final, _ = await _run(gw, tools=[{"name": "x", "parameters": {}}])
    assert [tc["name"] for tc in final["tool_calls"]] == ["read_card", "list_cards"]


async def test_truncated_arguments_go_to_continuation_contract(gw, monkeypatch):
    """半截 JSON 不吞掉：交 truncated_tool_call 供续写器抢救。"""
    _install(monkeypatch, [
        _tc_delta(0, tc_id="c1", name="write_file", arguments='{"path": "a.md", "content":'),
        _final_delta(None),
    ])
    final, _ = await _run(gw, tools=[{"name": "write_file", "parameters": {}}])
    assert final["tool_calls"] == []
    tc = final["truncated_tool_call"]
    assert tc["name"] == "write_file"
    assert tc["raw_arguments"] == '{"path": "a.md", "content":'
    assert tc["finish_reason"] == "tool_calls"


async def test_tool_call_id_captured(gw, monkeypatch):
    _install(monkeypatch, [_tc_delta(0, tc_id="call_xyz", name="read_card", arguments="{}"),
                           _final_delta(None)])
    final, _ = await _run(gw, tools=[{"name": "read_card", "parameters": {}}])
    assert final["tool_calls"][0]["name"] == "read_card"


async def test_tool_chunks_do_not_leak_into_text_stream(gw, monkeypatch):
    """工具分片不得混进逐字流正文（那是给人看的字）。"""
    _install(monkeypatch, [
        _tc_delta(0, name="read_card", arguments='{"card_id": "c"}'),
        _final_delta("正文"),
    ])
    final, events = await _run(gw, tools=[{"name": "read_card", "parameters": {}}])
    text_chunks = [e["text"] for e in events if e.get("chunk_type") == "content"]
    assert "".join(text_chunks) == "正文"
    assert "card_id" not in "".join(text_chunks)
    assert final["content"] == "正文"


async def test_reasoning_and_tool_call_in_same_stream(gw, monkeypatch):
    """思考 + 工具同流：两路各自正确，不串。"""
    think = SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=None, reasoning="我在想"), finish_reason=None)])
    _install(monkeypatch, [think,
                           _tc_delta(0, name="read_card", arguments='{"card_id":"c"}'),
                           _final_delta("动手")])
    final, events = await _run(gw, tools=[{"name": "read_card", "parameters": {}}])
    kinds = [e["chunk_type"] for e in events if "chunk_type" in e]
    assert kinds == ["reasoning", "content"]
    assert final["tool_calls"][0]["name"] == "read_card"
    assert final["content"] == "动手"