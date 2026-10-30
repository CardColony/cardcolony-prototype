# -*- coding: utf-8 -*-
"""Anthropic Messages 适配器（mavis 路由）。

与 ``OpenAICompatAdapter`` 的结构对应关系（逐项对账）：
    - ``options(provider, model)`` 钩子 → ``AnthropicConnectionOptions``
      （base_url／**token_getter**／max_tokens 默认值／extra_headers）；
    - 鉴权走 ``Authorization: Bearer <broker 令牌>``（实测：mavis 网关不读
      x-api-key，只认 Bearer；令牌由 ``mavis_lease.MavisLeaseClient`` 铸造续期）；
    - 非 2xx → ``LlmError``（401/403→AUTH，429→RATE_LIMIT，400→INVALID_REQUEST，5xx→SERVER）；
    - SSE 解析 + 事件翻译在 ``translate.py``；请求体组装在 ``serialize.py``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

from ..llm.errors import LlmError
from ..llm.retry import ResolvedRetryPolicy
from ..llm.runtime import LlmAdapter
from ..llm.types import GenerateOptions, StreamChunk
from .serialize import serialize_request
from .sse import parse_sse
from .translate import translate

__all__ = [
    "DEFAULT_STREAM_IDLE_TIMEOUT_MS",
    "AnthropicConnectionOptions",
    "AnthropicAdapterOptions",
    "AnthropicMessagesAdapter",
]

#: 一次流式读挂起时允许的最大空闲间隔——**已废用**（2026-10-23 用户令：取消墙钟）。
#: 与 openai 包同口径：断连由 socket 异常立刻暴露（TRANSPORT），
#: 「活着但很慢」是正常状态（本地模型排队/重思考可达 12 分钟），不该被杀。
#: None ⇒ 不设读超时；连接超时也交给 TCP 协议自己的握手超时。
DEFAULT_STREAM_IDLE_TIMEOUT_MS: Optional[int] = None

#: 协议必填 max_tokens 的缺省值（Anthropic 不接受缺失；可经 models.yaml 每模型覆盖）。
DEFAULT_MAX_TOKENS = 8192

#: 网关接受的协议版本头（与 Anthropic 官方一致；mavis 网关忽略未知头，无害）。
_ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class AnthropicConnectionOptions:
    """一次操作已校验的连接事实（mavis 路由）。"""

    base_url: str
    token_getter: Callable[[], Awaitable[str]]
    max_tokens: int = DEFAULT_MAX_TOKENS
    stream_idle_timeout_ms: int = DEFAULT_STREAM_IDLE_TIMEOUT_MS
    retry_policy: Optional[ResolvedRetryPolicy] = None
    extra_headers: Optional[dict[str, str]] = None


@dataclass(frozen=True)
class AnthropicAdapterOptions:
    """适配器构造项：``(provider, model) -> 连接事实`` 的解析钩子。"""

    options: Callable[[str, str], AnthropicConnectionOptions]


def _http_error_code(status: int, error: Optional[dict[str, Any]] = None) -> str:
    if status in (401, 403):
        return "AUTH"
    error = error or {}
    detail = " ".join(
        str(x) for x in (error.get("code"), error.get("type"), error.get("message")) if x
    )
    if status == 429:
        return "RATE_LIMIT"
    if status == 400:
        return "INVALID_REQUEST"
    if status >= 500:
        return "SERVER"
    return f"HTTP_{status}"


class AnthropicMessagesAdapter(LlmAdapter):
    """Anthropic Messages 直连适配器（mavis 网关）。"""

    def __init__(self, config: AnthropicAdapterOptions) -> None:
        self._config = config

    def provider_info(self, provider: str) -> dict[str, str]:
        return {"id": provider, "name": provider}

    def provider_retry_policy(self, provider: str) -> Optional[ResolvedRetryPolicy]:
        try:
            return self._config.options(provider, "").retry_policy
        except Exception:  # noqa: BLE001
            return None

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        connection = self._config.options(options.provider, options.model)
        token = await connection.token_getter()

        body = serialize_request(options, max_tokens=connection.max_tokens)
        payload = json.dumps(body, ensure_ascii=False)
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        if connection.extra_headers:
            headers.update({str(k): str(v) for k, v in connection.extra_headers.items()})

        timeout = httpx.Timeout(
            # [取消墙钟 2026-10-23] 远程等待不设墙钟：read/connect 均交给协议与异常；
            # write/pool 是本地资源等待，保留。
            connect=None,
            read=(None if connection.stream_idle_timeout_ms is None
                  else connection.stream_idle_timeout_ms / 1000.0),
            write=30.0,
            pool=30.0,
        )
        url = f"{connection.base_url.rstrip('/')}/messages"

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                request = client.build_request("POST", url, headers=headers, content=payload)
                response = await client.send(request, stream=True)
            except Exception as exc:  # noqa: BLE001
                raise LlmError(
                    f"API request to {connection.base_url} failed", "TRANSPORT", cause=exc
                ) from exc

            try:
                if response.status_code >= 400:
                    raise await self._http_error(response, connection)
                async for chunk in translate(parse_sse(response)):
                    yield chunk
            except httpx.TimeoutException as exc:
                raise LlmError(
                    f"transport timeout to {connection.base_url} "
                    f"(connect/write/pool, no idle wall clock)",
                    "TIMEOUT",
                    cause=exc,
                ) from exc
            except LlmError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise LlmError(
                    f"API stream from {connection.base_url} failed", "TRANSPORT", cause=exc
                ) from exc
            finally:
                await response.aclose()

    async def _http_error(
        self, response: httpx.Response, connection: AnthropicConnectionOptions
    ) -> LlmError:
        status = response.status_code
        message = f"API error (HTTP {status})"
        provider_error: Optional[dict[str, Any]] = None
        try:
            await response.aread()
            parsed = json.loads(response.content.decode("utf-8", "replace"))
            # Anthropic 错误体：{"type":"error","error":{"type":...,"message":...}}
            provider_error = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
            if isinstance(provider_error, dict) and provider_error.get("message"):
                message = str(provider_error["message"])
        except Exception:  # noqa: BLE001 只吞错误体解析：状态码仍能识别失败
            pass
        return LlmError(
            message,
            _http_error_code(status, provider_error),
            status=status,
            request_id=response.headers.get("x-request-id") or None,
        )
