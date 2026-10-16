# -*- coding: utf-8 -*-
"""期 1 单测 · 运行时（注册表 / 分发 / 失败归一 / 统计）。

零真实调用、零网络。
"""
from __future__ import annotations

import pytest

from src.model_gateway.llm.errors import LlmError
from src.model_gateway.llm.runtime import LlmAdapter, LlmRuntime, RuntimeStats
from src.model_gateway.llm.types import (
    ErrorReason,
    FinishChunk,
    GenerateOptions,
    StopReason,
    TextDelta,
)


class _FakeAdapter(LlmAdapter):
    def __init__(self, chunks=None, error=None, name="Fake"):
        self._chunks = list(chunks or [])
        self._error = error
        self._name = name

    def provider_info(self, provider):
        return {"id": provider, "name": self._name}

    async def stream(self, options):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def _opts(provider="p", model="m"):
    return GenerateOptions(provider=provider, model=model, messages=())


async def _drain(agen):
    return [c async for c in agen]


async def test_register_and_dispatch_passes_chunks_through():
    rt = LlmRuntime()
    rt.register_adapter(["p"], _FakeAdapter([TextDelta(index=0, text="hi"), FinishChunk(reason=StopReason())]))
    out = await _drain(rt.stream(_opts()))
    assert out == [TextDelta(index=0, text="hi"), FinishChunk(reason=StopReason())]
    assert rt.registered_providers() == ["p"]


async def test_unregistered_provider_yields_no_adapter_terminal_finish():
    rt = LlmRuntime()
    out = await _drain(rt.stream(_opts(provider="missing")))
    assert len(out) == 1
    assert isinstance(out[0], FinishChunk)
    assert isinstance(out[0].reason, ErrorReason)
    assert out[0].reason.failure.code == "NO_ADAPTER"


async def test_register_is_idempotent_for_same_adapter():
    rt = LlmRuntime()
    adapter = _FakeAdapter()
    rt.register_adapter(["p"], adapter)
    rt.register_adapter(["p"], adapter)  # 幂等：不抛
    assert rt.registered_providers() == ["p"]


async def test_duplicate_adapter_conflict_raises():
    rt = LlmRuntime()
    rt.register_adapter(["p"], _FakeAdapter(name="A"))
    with pytest.raises(LlmError) as exc:
        rt.register_adapter(["p"], _FakeAdapter(name="B"))
    assert exc.value.code == "DUPLICATE_ADAPTER"


async def test_empty_provider_name_rejected():
    rt = LlmRuntime()
    with pytest.raises(LlmError) as exc:
        rt.register_adapter([""], _FakeAdapter())
    assert exc.value.code == "INVALID_ADAPTER"


async def test_adapter_throw_becomes_error_finish():
    rt = LlmRuntime()
    rt.register_adapter(["p"], _FakeAdapter([TextDelta(index=0, text="部分")], error=LlmError("boom", "SERVER")))
    out = await _drain(rt.stream(_opts()))
    assert out[0] == TextDelta(index=0, text="部分")
    assert isinstance(out[-1], FinishChunk)
    assert isinstance(out[-1].reason, ErrorReason)
    assert out[-1].reason.failure.code == "SERVER"


async def test_unknown_throw_becomes_unknown_code():
    rt = LlmRuntime()
    rt.register_adapter(["p"], _FakeAdapter(error=RuntimeError("oops")))
    out = await _drain(rt.stream(_opts()))
    assert out[-1].reason.failure.code == "UNKNOWN"


async def test_aborted_signal_marks_aborted_finish():
    rt = LlmRuntime()
    rt.register_adapter(["p"], _FakeAdapter(error=RuntimeError("x")))

    class _Sig:
        aborted = True

    out = await _drain(rt.stream(GenerateOptions(provider="p", model="m", messages=(), signal=_Sig())))
    assert out[-1].reason.kind == "aborted"


async def test_stats_record_calls_and_tokens():
    rt = LlmRuntime()
    from src.model_gateway.llm.types import TokenUsage, UsageChunk

    rt.register_adapter(
        ["p"],
        _FakeAdapter([TextDelta(index=0, text="hi"), UsageChunk(usage=TokenUsage(input_tokens=3, output_tokens=2)), FinishChunk(reason=StopReason())]),
    )
    await _drain(rt.stream(_opts(model="m1")))
    summary = rt.stats.summary()
    assert summary["total_calls"] == 1
    assert summary["total_tokens"] == 5
    assert summary["by_model"]["m1"]["calls"] == 1


def test_runtime_stats_matches_legacy_gateway_stats_shape():
    s = RuntimeStats()
    s.record("m", 10, 5, True)
    s.record("m", 0, 3, False)
    summary = s.summary()
    assert set(summary) == {"total_tokens", "total_calls", "by_model", "recent"}
    assert summary["by_model"]["m"] == {"calls": 2, "tokens": 10, "failures": 1}
