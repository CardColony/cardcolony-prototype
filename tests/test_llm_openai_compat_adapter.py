# -*- coding: utf-8 -*-
"""llm_openai_compat · 直连适配器单测（错误码映射 / retry-after / httpx 流式端到端）。

迁自原 ``tests/test_llm_deepseek_adapter.py``（import 路径与类名改指新包，断言语义不变）。
零真实调用、零网络（httpx.MockTransport 假 SSE 字节流）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.model_gateway.llm.errors import LlmError
from src.model_gateway.llm.types import (
    FinishChunk,
    GenerateOptions,
    StopReason,
    TextDelta,
)
from src.model_gateway.llm_openai_compat.adapter import (
    CompatAdapterOptions,
    CompatConnectionOptions,
    OpenAICompatAdapter,
    http_error_code,
    provider_retry_after_ms,
)


# ---------------- 错误码映射 ----------------

def test_http_error_code_mapping():
    assert http_error_code(401) == "AUTH"
    assert http_error_code(403) == "AUTH"
    assert http_error_code(429) == "RATE_LIMIT"
    assert http_error_code(500) == "SERVER"
    assert http_error_code(503) == "SERVER"
    assert http_error_code(400, {"message": "This model's maximum context length is 65536 tokens"}) == "CONTEXT_WINDOW_EXCEEDED"
    assert http_error_code(400, {"message": "bad request"}) == "INVALID_REQUEST"
    assert http_error_code(400, {"message": "insufficient_quota"}) == "QUOTA"
    assert http_error_code(418) == "HTTP_418"


def test_retry_after_parses_seconds_and_http_date():
    assert provider_retry_after_ms("3") == 3000.0
    assert provider_retry_after_ms(None) is None
    assert provider_retry_after_ms("0") is None
    assert provider_retry_after_ms("not-a-date") is None
    import datetime as _dt
    from email.utils import format_datetime

    future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=30)
    delay = provider_retry_after_ms(format_datetime(future))
    assert delay is not None and 0 < delay <= 30_000


# ---------------- httpx 流式端到端 ----------------

def _sse_body(*payloads: str) -> bytes:
    out = b""
    for p in payloads:
        out += f"data: {p}\n\n".encode("utf-8")
    return out


@pytest.fixture
def patch_transport(monkeypatch):
    """把适配器内部构造的 httpx.AsyncClient 换成一个走 MockTransport 的实例。"""

    def install(handler):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return install


def _adapter(base_url="https://example.test/v1", vendor="opencode"):
    conn = CompatConnectionOptions(base_url=base_url, api_key="sk-fake", vendor=vendor)
    return OpenAICompatAdapter(CompatAdapterOptions(options=lambda provider, model: conn))


async def _drain(adapter, options):
    return [c async for c in adapter.stream(options)]


def _opts():
    return GenerateOptions(provider="opencode", model="deepseek-chat", messages=())


async def test_adapter_streams_chunks_from_sse(patch_transport):
    body = _sse_body(
        json.dumps({"choices": [{"delta": {"content": "你"}, "finish_reason": None}]}),
        json.dumps({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}),
        "[DONE]",
    )
    patch_transport(lambda request: httpx.Response(200, content=body))
    out = await _drain(_adapter(), _opts())
    assert TextDelta(index=0, text="你") in out
    assert TextDelta(index=0, text="好") in out
    assert out[-1] == FinishChunk(reason=StopReason())


async def test_adapter_sends_expected_request(patch_transport):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=_sse_body("[DONE]"))

    patch_transport(handler)
    await _drain(_adapter("https://example.test/v1"), _opts())
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-fake"
    assert seen["accept"] == "text/event-stream"
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}


async def test_adapter_maps_http_error_with_retry_after(patch_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "slow down"}},
            headers={"retry-after": "5", "x-request-id": "req-9"},
        )

    patch_transport(handler)
    with pytest.raises(LlmError) as exc:
        await _drain(_adapter(), _opts())
    assert exc.value.code == "RATE_LIMIT"
    assert exc.value.failure.status == 429
    assert exc.value.failure.provider_retry_after_ms == 5000.0
    assert exc.value.failure.request_id == "req-9"


async def test_adapter_keeps_status_when_error_body_is_malformed(patch_transport):
    patch_transport(lambda request: httpx.Response(500, content=b"<html>not json</html>"))
    with pytest.raises(LlmError) as exc:
        await _drain(_adapter(), _opts())
    assert exc.value.code == "SERVER"
    assert exc.value.failure.status == 500


async def test_adapter_transport_failure_is_transport_code(patch_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    patch_transport(handler)
    with pytest.raises(LlmError) as exc:
        await _drain(_adapter(), _opts())
    assert exc.value.code == "TRANSPORT"
