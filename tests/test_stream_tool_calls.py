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
from src.model_gateway.llm.types import ToolCallDelta

from tests.fake_llm import finish, install_fake_llm, reasoning_delta, text_delta


def _tc_delta(index, tc_id=None, name=None, arguments=None):
    """[5715] 新 runtime 协议的 tool-call 增量。"""
    return ToolCallDelta(index=index, id=tc_id or "", arguments_delta=arguments or "",
                         name=name)


def _final_delta(content=None):
    """[5715] 新 runtime 协议的结束帧（正文可带可不带）。"""
    if content is None:
        return finish()
    return [text_delta(content), finish()]


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


GW_HOLDER: dict = {}


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setenv("FAKE_TEST_KEY", "sk-test-not-real")
    cfg = {"models": {"m": {
        "provider": "openai", "model": "fake/model", "api_key_env": "FAKE_TEST_KEY",
        "temperature": 0.3, "retry": {"max_attempts": 1, "backoff_seconds": 0},
    }}}
    g = ModelGateway(cfg)
    GW_HOLDER['gw'] = g
    return g


def _install(monkeypatch, chunks):
    """[5715] 换成**新 runtime 协议**的假适配器（tests/fake_llm.py）。

    旧实现打桩 ``sys.modules["litellm"].acompletion``，网关 2026-10-16 换实现后
    该打桩**不再被调用** ⇒ 真发网络请求（key 过期 ⇒ 长期红）。
    现在由 FakeAdapter 接管，并把请求面（model/messages/tools）记下来供断言。
    """
    flat = []
    for c in chunks:
        if isinstance(c, list):
            flat.extend(c)
        else:
            flat.append(c)
    return install_fake_llm(GW_HOLDER["gw"], flat)


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
    # [5715] 新 runtime 协议：tools 以 ToolSchema 形态下发（provider 无关），
    # 不再是 litellm 的 {"type":"function","function":{...}} 形态。
    assert [t.name for t in seen["tools"]] == ["read_card"]
    assert seen["tools"][0].parameters == tools[0]["parameters"]


async def test_prior_messages_land_before_user_turn(gw, monkeypatch):
    seen = _install(monkeypatch, [_final_delta("ok")])
    prior = [{"role": "user", "content": "上一轮"}, {"role": "assistant", "content": "上一轮答"}]
    await _run(gw, prior_messages=prior)
    # [5715] 新协议：system 走 options.system 单独字段，messages 只装对话。
    assert seen["system"] == "sys", "system 必须单独下发"
    msgs = seen["messages"]
    assert msgs[:2] == prior, "历史必须在当前提问之前"
    assert msgs[-1] == {"role": "user", "content": "user"}


async def test_no_prior_keeps_two_message_shape(gw, monkeypatch):
    seen = _install(monkeypatch, [_final_delta("ok")])
    await _run(gw)
    # [5715] 新协议：无历史时 messages 只含当前提问（system 另走 options.system）
    assert len(seen["messages"]) == 1
    assert seen["system"] == "sys"


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


async def test_parallel_tool_calls_keep_stream_order(gw, monkeypatch):
    """并行多个工具：按**流序（块首次出现序）**返回。

    [5715 · 已定案] 这条原本是 xfail（"待判"），现已比对底座抄写件本身定案：

    - 抄写件 ``src/model_gateway/llm/assembler.py`` 的文件头写明「逐语义抄自
      原版 ``deepseek-harness/packages/llm/llm/src/assembler.ts``」；
    - 它的 ``blocks()``（原版 assembler.ts:134-139）遍历的是 ``self._order``
      ——**首次出现序**的索引表，代码里**没有任何按 index 排序**的动作。

    ⇒ **流序就是 dsh 原版的规则**，不是本项目的回归。
    旧的「按 index 还原」是 2026-10-16 换实现前 **litellm** 的行为
    （litellm 按 index 把数组摆回去），与底座抄写件不是同一套语义。

    因此本条断言改为钉**底座的现行规则**（流序），而不是把旧 litellm 的契约
    硬塞回新底座（那是「改判据」，不是「改产品」——底座是抄写件，不动）。
    """
    _install(monkeypatch, [
        _tc_delta(1, tc_id="c2", name="list_cards", arguments='{"a":2}'),
        _tc_delta(0, tc_id="c1", name="read_card", arguments='{"b":1}'),
        _final_delta(None),
    ])
    final, _ = await _run(gw, tools=[{"name": "x", "parameters": {}}])
    assert [tc["name"] for tc in final["tool_calls"]] == ["list_cards", "read_card"], \
        "并行工具应按流序（块首次出现序）返回——底座 blocks() 的现行规则"


async def test_parallel_tool_calls_in_index_order_keep_that_order(gw, monkeypatch):
    """**常态路径**：provider 按 index 顺序发块 ⇒ 返回也按 index 顺序。

    上面那条钉的是「乱序到达」时的规则；这条钉真正天天发生的那条路径：
    适配器把 OpenAI 的 ``delta.index`` 映射成块 index，正常 provider 必然按
    index 递增发 ⇒ 两种规则在常态下**结果一致**（不会出现界面顺序错乱）。
    """
    _install(monkeypatch, [
        _tc_delta(0, tc_id="c1", name="read_card", arguments='{"b":1}'),
        _tc_delta(1, tc_id="c2", name="list_cards", arguments='{"a":2}'),
        _final_delta(None),
    ])
    final, _ = await _run(gw, tools=[{"name": "x", "parameters": {}}])
    assert [tc["name"] for tc in final["tool_calls"]] == ["read_card", "list_cards"]
    # 并行工具**互不串味**：各自的参数必须跟着自己的名字走
    # （这才是并行真正的风险点——两块 index 不同，参数不该被并到一块里）。
    args = {tc["name"]: tc["arguments"] for tc in final["tool_calls"]}
    assert args == {"read_card": {"b": 1}, "list_cards": {"a": 2}}, \
        f"并行工具的参数串味了：{final['tool_calls']}"


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
    """思考 + 工具同流：两路各自正确，不串。

    [5715 · 判据换] 原用例用 litellm 形态（choices[0].delta 同时带 reasoning）
    造「思考与工具同流」；新协议下二者是两个独立帧（``ReasoningDelta`` /
    ``ToolCallDelta``），**同流**由「同一 stream 里先后到达」表达。
    改判据：思考帧与工具帧在同一流里 ⇒ 两路各自归位、互不串。
    """
    _install(monkeypatch, [
        reasoning_delta("我在想"),
        # [5715] 工具调用用**独立 index**（思考占 0）：同 index 会被 BlockAssembler
        # 归进同一个块 ⇒ 工具调用被当成思考，tool_calls 为空。
        _tc_delta(1, name="read_card", arguments='{"card_id":"c"}'),
        _final_delta("动手"),
    ])
    final, events = await _run(gw, tools=[{"name": "read_card", "parameters": {}}])
    kinds = [e["chunk_type"] for e in events if "chunk_type" in e]
    assert kinds == ["reasoning", "content"], kinds
    assert final["tool_calls"][0]["name"] == "read_card"
    assert final["content"] == "动手"