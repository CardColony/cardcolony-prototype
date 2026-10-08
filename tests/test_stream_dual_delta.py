# -*- coding: utf-8 -*-
"""[信息流重出 Task1] stream_call 双增量解包回归（V-T1-1）。

覆盖判据：
1. 双增量 chunk（reasoning + content 同帧序列）→ yield 带 chunk_type 且两类都在；
2. 旧形态 ``{"chunk": str}`` 始终保留（既有消费方零感知）；
3. 思考增量**不混入**聚合 content（红线5：finish / tokens / 续写判定逐字不变）；
4. ``on_reasoning`` 回调与 on_status 并行、可选，只吃思考字；
5. 单增量（只 reasoning 或只 content）不报错；
6. 提供商字段别名 ``reasoning_content`` 同样解出。

零真实 key、零网络、零 works 触碰（conftest 沙箱铁律一；不 import main —— 铁律二）。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.model_gateway.gateway import ModelGateway


def _delta(content=None, reasoning=None, alias=None):
    """造一个 litellm 风格的 chunk：choices[0].delta 带 content / 思考字段。"""
    d = SimpleNamespace(content=content)
    if reasoning is not None:
        d.reasoning = reasoning
    if alias is not None:
        d.reasoning_content = alias
    return SimpleNamespace(choices=[SimpleNamespace(delta=d)])


class _FakeStream:
    """async iterable，按序吐 chunk（模拟 acompletion 返回值）。"""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


@pytest.fixture
def gw(monkeypatch):
    """ModelGateway + 假 litellm；api_key 用假值，绝不碰真实密钥。"""
    monkeypatch.setenv("FAKE_TEST_KEY", "sk-test-not-real")
    cfg = {
        "models": {
            "m": {
                "provider": "openai",
                "model": "fake/model",
                "api_key_env": "FAKE_TEST_KEY",
                "temperature": 0.3,
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
            }
        }
    }
    return ModelGateway(cfg)


def _install_fake_litellm(monkeypatch, chunks):
    """把假 litellm 塞进 sys.modules（stream_call 内部是 __import__("litellm")）。"""
    seen = {}

    async def acompletion(**kwargs):
        seen.update(kwargs)
        return _FakeStream(chunks)

    fake = SimpleNamespace(acompletion=acompletion)
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return seen


async def _drain(gw):
    return [ev async for ev in gw.stream_call("m", "sys", "user")]


# ---------- 1/2/3：双增量主路径 ----------

async def test_dual_delta_yields_both_chunk_types_and_keeps_legacy_key(gw, monkeypatch):
    """V-T1-1 核心：思考与正文分列两路，旧 chunk 键保留，聚合不含思考。"""
    _install_fake_litellm(monkeypatch, [
        _delta(content=None, reasoning="想一下"),
        _delta(content="答案", reasoning="再想"),
        _delta(content="尾巴"),
    ])

    events = await _drain(gw)
    chunky = [e for e in events if "chunk_type" in e]

    kinds = [e["chunk_type"] for e in chunky]
    assert "reasoning" in kinds and "content" in kinds, kinds
    # 旧形态兼容键：每个增量都带 chunk
    assert all("chunk" in e and isinstance(e["chunk"], str) for e in chunky)
    # 文本与 chunk 键一致
    assert all(e["chunk"] == e["text"] for e in chunky)
    # 思考聚合：想一下 + 再想
    assert "".join(e["text"] for e in chunky if e["chunk_type"] == "reasoning") == "想一下再想"

    final = events[-1]
    assert final["content"] == "答案尾巴", "思考增量不得混入聚合 content（红线5）"
    assert "想一下" not in final["content"]


async def test_reasoning_precedes_content_within_stream(gw, monkeypatch):
    """思考先于正文到达，顺序保持上游时序。"""
    _install_fake_litellm(monkeypatch, [
        _delta(content=None, reasoning="r1"),
        _delta(content="c1"),
        _delta(content=None, reasoning="r2"),
        _delta(content="c2"),
    ])
    events = await _drain(gw)
    order = [e["chunk_type"] for e in events if "chunk_type" in e]
    assert order == ["reasoning", "content", "reasoning", "content"], order


# ---------- 4：on_reasoning 回调 ----------

async def test_on_reasoning_callback_receives_only_thinking(gw, monkeypatch):
    got: list[str] = []
    _install_fake_litellm(monkeypatch, [
        _delta(content=None, reasoning="思考A"),
        _delta(content="正文B"),
        _delta(content=None, reasoning="思考C"),
    ])

    events = [ev async for ev in gw.stream_call(
        "m", "sys", "user", on_reasoning=got.append)]
    assert got == ["思考A", "思考C"], got
    assert events[-1]["content"] == "正文B"


async def test_on_reasoning_absent_is_zero_behaviour_change(gw, monkeypatch):
    """缺省 None：不报错、不改变产出。"""
    _install_fake_litellm(monkeypatch, [_delta(content="只有正文")])
    events = await _drain(gw)
    assert events[-1]["content"] == "只有正文"
    assert all("chunk_type" not in e or e["chunk_type"] == "content" for e in events)


async def test_on_reasoning_exception_does_not_break_stream(gw, monkeypatch):
    """回调抛异常不拖垮流（展示层绝不影响主流程）。"""
    def boom(_t):
        raise RuntimeError("cb exploded")

    _install_fake_litellm(monkeypatch, [
        _delta(content=None, reasoning="r"),
        _delta(content="c"),
    ])
    events = [ev async for ev in gw.stream_call("m", "sys", "user", on_reasoning=boom)]
    assert events[-1]["content"] == "c"


# ---------- 5：单增量兜底 ----------

async def test_reasoning_only_stream_does_not_error(gw, monkeypatch):
    _install_fake_litellm(monkeypatch, [
        _delta(content=None, reasoning="只有思考"),
        _delta(content=None, reasoning="仍在想"),
    ])
    events = await _drain(gw)
    kinds = [e["chunk_type"] for e in events if "chunk_type" in e]
    assert kinds == ["reasoning", "reasoning"]
    assert events[-1]["content"] == ""      # 无正文不炸


async def test_content_only_stream_unchanged(gw, monkeypatch):
    _install_fake_litellm(monkeypatch, [_delta(content="甲"), _delta(content="乙")])
    events = await _drain(gw)
    kinds = [e["chunk_type"] for e in events if "chunk_type" in e]
    assert kinds == ["content", "content"]
    assert events[-1]["content"] == "甲乙"


async def test_malformed_chunk_tolerated(gw, monkeypatch):
    """坏帧（无 choices / delta 为 None）不炸流，沿续其后。"""
    bad1 = SimpleNamespace(choices=[])
    bad2 = SimpleNamespace(choices=[SimpleNamespace(delta=None)])
    _install_fake_litellm(monkeypatch, [bad1, bad2, _delta(content="活下来了")])
    events = await _drain(gw)
    assert events[-1]["content"] == "活下来了"


# ---------- 6：提供商字段别名 ----------

async def test_reasoning_content_alias_is_unpacked(gw, monkeypatch):
    """DeepSeek/openrouter 一族用 reasoning_content，同样解出思考。"""
    _install_fake_litellm(monkeypatch, [
        _delta(content=None, alias="别名思考"),
        _delta(content="别名正文"),
    ])
    events = await _drain(gw)
    kinds = [e["chunk_type"] for e in events if "chunk_type" in e]
    assert kinds == ["reasoning", "content"], kinds
    assert events[-1]["content"] == "别名正文"


async def test_non_string_reasoning_not_treated_as_text(gw, monkeypatch):
    """思考字段若为 dict 等非字符串，不误当正文吐出去。"""
    _install_fake_litellm(monkeypatch, [
        _delta(content="正文", reasoning={"unexpected": "dict"}),
    ])
    events = await _drain(gw)
    chunky = [e for e in events if "chunk_type" in e]
    assert [e["chunk_type"] for e in chunky] == ["content"]
    assert events[-1]["content"] == "正文"