# -*- coding: utf-8 -*-
"""llm_openai_compat · OpenAI 兼容直连适配器（transport only，五家共用一个）。

模板出处：原 ``llm_deepseek/adapter.py``（346 行）——**传输层逐语义沿用**，泛化为：
    - 连接事实由 facts 配置表供给（`CompatConnectionOptions`），不再绑死 DeepSeek 端点；
    - 思考参数由 :mod:`dialects` 按供应商分派；
    - 错误码映射 / retry-after 解析一份，全五家共用。

未抄（模板有、本系统无对应物）：
    - ``idleWatchdog`` / ``AbortSignal.any``：用 ``httpx`` timeout + asyncio 取消（等价）。
    - ``attributionHeaders()`` / ``x-deepseek-harness-*`` 遥测头：dsh 宿主遥测面。
    - ``listModels`` / ``resolveModel``：dsh 宿主模型目录面。
    - ★ ``DEFAULT_MAX_TOKENS = 256_000`` 的默认物化：本仓**拆闸铁律** ⇒ 不注入默认 max_tokens。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

from ..llm.errors import (
    CONTEXT_WINDOW_EXCEEDED_CODE,
    QUOTA_EXCEEDED_CODE,
    LlmError,
    is_context_window_exceeded_error,
    is_quota_exceeded_error,
)
from ..llm.retry import ResolvedRetryPolicy
from ..llm.runtime import LlmAdapter
from ..llm.types import GenerateOptions, StreamChunk
from .serialize import RequestDefaults, serialize_request
from .sse import parse_sse
from .translate import translate

__all__ = [
    "DEFAULT_STREAM_IDLE_TIMEOUT_MS",
    "CompatConnectionOptions",
    "CompatAdapterOptions",
    "OpenAICompatAdapter",
    "http_error_code",
    "provider_retry_after_ms",
]

#: 流式读的空闲墙钟——**已废用**（2026-10-23 用户令）。
#:
#: 原来这里是 300_000ms：读挂起超过 5 分钟就判 TIMEOUT。这是**墙钟类闸**，
#: 与 2026-10-02 拆闸裁定（「不允许墙钟类闸」）直接冲突——只是它藏在常量里，
#: 不像闸。它的两个害处：
#:   ① 真断连根本不用等钟：socket 会立刻抛异常（TRANSPORT），0 秒就知道；
#:   ② 慢但在干活（本地模型重思考 12 分钟、排队阶段不出字）会被误杀。
#: ⇒ 现在**不设读超时**（read=None）：活着的连接要等多久都等，死了靠异常立刻发现。
#: 保留常量名与字段只为兼容（值改成 None 表示不设），不再参与任何判定。
DEFAULT_STREAM_IDLE_TIMEOUT_MS: Optional[int] = None


@dataclass(frozen=True)
class CompatConnectionOptions:
    """一次操作已校验的连接事实。模板 adapter.ts:49-71。

    ★ 端点与密钥**同代配对**：两者来自同一次解析，请求绝不会把某一代的 URL 与另一代的密钥配在一起。
    """

    base_url: str
    api_key: str
    vendor: Optional[str] = None
    defaults: RequestDefaults = field(default_factory=RequestDefaults)
    max_tokens: Optional[int] = None
    stream_idle_timeout_ms: int = DEFAULT_STREAM_IDLE_TIMEOUT_MS
    retry_policy: Optional[ResolvedRetryPolicy] = None
    extra_headers: Optional[dict[str, str]] = None


@dataclass(frozen=True)
class CompatAdapterOptions:
    """适配器构造项：``(provider, model) -> 连接事实`` 的解析钩子。"""

    options: Callable[[str, str], CompatConnectionOptions]
    resolve_api_key: Optional[Callable[[CompatConnectionOptions], Awaitable[str]]] = None


# ---------------------------------------------------------------------------
# retry-after 头解析 —— 模板 adapter.ts:117-125
# ---------------------------------------------------------------------------
def provider_retry_after_ms(value: Optional[str]) -> Optional[float]:
    """解析 ``retry-after`` 头（**秒数**或 **HTTP 日期**）为毫秒。模板 adapter.ts:117-125。"""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        delay = int(value) * 1000
        return float(delay) if delay > 0 else None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - datetime.now(timezone.utc)).total_seconds() * 1000
    return float(delay) if delay > 0 else None


# ---------------------------------------------------------------------------
# HTTP 状态 → 稳定 LlmError 码 —— 模板 adapter.ts:138-149
# ---------------------------------------------------------------------------
def http_error_code(status: int, error: Optional[dict[str, Any]] = None) -> str:
    """把一个 HTTP 状态映射成稳定的 LlmError 码。模板 ``httpErrorCode``（adapter.ts:138-149）。"""
    if status in (401, 403):
        return "AUTH"
    error = error or {}
    detail = " ".join(str(x) for x in (error.get("code"), error.get("type"), error.get("message")) if x)
    if is_quota_exceeded_error(detail):
        return QUOTA_EXCEEDED_CODE
    if status == 429:
        return "RATE_LIMIT"
    if status == 400:
        if is_context_window_exceeded_error(detail):
            return CONTEXT_WINDOW_EXCEEDED_CODE
        return "INVALID_REQUEST"
    if status >= 500:
        return "SERVER"
    return f"HTTP_{status}"


class OpenAICompatAdapter(LlmAdapter):
    """OpenAI 兼容直连适配器：一个实例服务它被注册的全部供应商路由。"""

    def __init__(self, config: CompatAdapterOptions) -> None:
        self._config = config

    def provider_info(self, provider: str) -> dict[str, str]:
        return {"id": provider, "name": provider}

    def provider_retry_policy(self, provider: str) -> Optional[ResolvedRetryPolicy]:
        # 取该路由下任一模型的策略（同供应商同策略）；取不到交给 runtime 用 normal 默认。
        try:
            return self._config.options(provider, "").retry_policy
        except Exception:  # noqa: BLE001
            return None

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        """流式发起一次调用。模板 adapter.ts:214-269。"""
        # 每次 stream 调用解析一次：连接事实与凭据在此冻结，整条请求持有。
        connection = self._config.options(options.provider, options.model)
        api_key = connection.api_key
        if self._config.resolve_api_key is not None:
            api_key = await self._config.resolve_api_key(connection)

        body = serialize_request(options, connection.defaults)
        payload = json.dumps(body, ensure_ascii=False)
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if connection.extra_headers:
            headers.update({str(k): str(v) for k, v in connection.extra_headers.items()})

        timeout = httpx.Timeout(
            # [取消墙钟 2026-10-23 · 用户令] 远程等待一律不设墙钟：
            #   · read=None  ：不设读超时。断连由 socket 异常立刻暴露（TRANSPORT）；
            #                  「活着但很慢」是正常状态，不该被杀。
            #   · connect=None：不设连接超时。对端拒绝/不可路由本来就立即报错；
            #                  只有「包被静默丢弃」才需要等——那交给 **TCP 协议自己**
            #                  的握手超时（SYN 重传，Windows 约 21s），
            #                  不该由我们拍一个数（30s 之类的数在协议面前只是摆设）。
            # 保留 write/pool：它们是**本地资源**等待（socket 缓冲、连接池），
            # 不涉及远端，不是墙钟闸。
            connect=None,
            read=(None if connection.stream_idle_timeout_ms is None
                  else connection.stream_idle_timeout_ms / 1000.0),
            write=30.0,
            pool=30.0,
        )
        url = f"{connection.base_url.rstrip('/')}/chat/completions"

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                request = client.build_request("POST", url, headers=headers, content=payload)
                response = await client.send(request, stream=True)
            except Exception as exc:  # noqa: BLE001
                if _is_cancelled(options.signal):
                    raise LlmError("request aborted by caller", "ABORTED", cause=exc) from exc
                raise LlmError(
                    f"API request to {connection.base_url} failed", "TRANSPORT", cause=exc
                ) from exc

            try:
                if response.status_code >= 400:
                    raise await self._http_error(response, connection)
                async for chunk in translate(parse_sse(_aiter_bytes(response), None)):
                    yield chunk
            except httpx.TimeoutException as exc:
                # 读超时已被取消（read=None），这里只剩 connect/write/pool 超时
                # 或端点自己发的超时。文案不再声称是「空闲墙钟」。
                raise LlmError(
                    f"transport timeout to {connection.base_url} "
                    f"(connect/write/pool, no idle wall clock)",
                    "TIMEOUT",
                    cause=exc,
                ) from exc
            except LlmError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_cancelled(options.signal):
                    raise LlmError("request aborted by caller", "ABORTED", cause=exc) from exc
                raise LlmError(
                    f"API stream from {connection.base_url} failed", "TRANSPORT", cause=exc
                ) from exc
            finally:
                await response.aclose()

    async def _http_error(self, response: httpx.Response, connection: CompatConnectionOptions) -> LlmError:
        """把非 2xx 响应构造成 LlmError。模板 adapter.ts:321-339。"""
        status = response.status_code
        message = f"API error (HTTP {status})"
        provider_error: Optional[dict[str, Any]] = None
        try:
            await response.aread()
            parsed = json.loads(response.content.decode("utf-8", "replace"))
            provider_error = parsed.get("error")
            if isinstance(provider_error, dict) and provider_error.get("message"):
                message = provider_error["message"]
        except Exception:  # noqa: BLE001 只吞错误体解析：状态码仍能识别失败
            pass
        delay = provider_retry_after_ms(response.headers.get("retry-after"))
        request_id = response.headers.get("x-request-id") or response.headers.get("x-deepseek-request-id")
        return LlmError(
            message,
            http_error_code(status, provider_error),
            status=status,
            provider_retry_after_ms=delay,
            request_id=request_id or None,
        )


async def _aiter_bytes(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_bytes():
        yield chunk


def _is_cancelled(signal: Any) -> bool:
    return bool(getattr(signal, "aborted", False)) or bool(getattr(signal, "cancelled", False))
