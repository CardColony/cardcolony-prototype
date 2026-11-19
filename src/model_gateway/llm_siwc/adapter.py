# -*- coding: utf-8 -*-
"""llm_siwc · Sign in with ChatGPT（SIWC）适配器。

背景（2026-11-19 用户令接入）：
  OpenClaw 走的是 OpenAI 官方「Sign in with ChatGPT」开源自助通道——
  用 ChatGPT 账号 OAuth 授权，拿到的 access_token 可以打
  ``https://api.openai.com/v1/responses``，消耗 **ChatGPT 套餐额度**（不是 API key）。

本机实测（2026-11-19，Plus 账号）得到的硬约束：
  · 模型只支持 GPT-6 系：``gpt-6.2-sol`` / ``gpt-6.1-terra`` / ``gpt-6.2-astra`` / ``gpt-6-astra``
    （``gpt-5.x`` / ``codex-mini`` 一律 400 model_not_found）
  · ``input`` 必须是数组（不能是字符串）
  · ``store`` 必须为 ``false``
  · ``stream`` 必须为 ``true``（不支持非流式）
  · 出站必须走本机代理（``api.openai.com`` 直连不通）

凭据：``config/siwc_tokens.json``（由 ``scripts/siwc_login.py`` 生成，已 gitignore）。
access_token 1 小时；到期用 refresh_token 自动续（refresh_token 30 天、每次轮换）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import httpx

from ..llm.errors import (
    CONTEXT_WINDOW_EXCEEDED_CODE,
    EMPTY_RESPONSE_CODE,
    QUOTA_EXCEEDED_CODE,
    LlmError,
    is_context_window_exceeded_error,
    is_quota_exceeded_error,
)
from ..llm.runtime import LlmAdapter, ResolvedRetryPolicy
from ..llm.types import (
    BlockEnd,
    BlockStart,
    ContentBlock,
    ErrorReason,
    FinishChunk,
    FinishReason,
    LlmFailure,
    MaxTokensReason,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    StreamChunk,
    TextBlock,
    TextDelta,
    TokenUsage,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallsReason,
    UsageChunk,
)
from ..llm_openai_compat.adapter import CompatConnectionOptions, http_error_code

__all__ = ["SiwcAdapter", "SiwcAdapterOptions", "SIWC_PROTOCOL"]

SIWC_PROTOCOL = "responses-siwc"
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOKENS_PATH = ROOT / "config" / "siwc_tokens.json"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"


@dataclass(frozen=True)
class SiwcAdapterOptions:
    options: Callable[[str, str], CompatConnectionOptions]
    tokens_path: Path = DEFAULT_TOKENS_PATH
    proxy: Optional[str] = None
    refresh_skew_seconds: int = 120


class _TokenStore:
    """SIWC token 读写与自动刷新（进程内缓存，落盘复用）。

    [多进程 2026-11-19] web + runner×K + condense 各自一个进程，共享同一份
    ``siwc_tokens.json``；refresh_token **每次刷新都会轮换**，两个进程同时拿旧
    refresh 去换 ⇒ 后到的必失败（invalid_grant）。故刷新走**跨进程文件锁**，
    拿到锁后先重读文件：别的进程已刷过就直接用新的，不重复刷。
    """

    def __init__(self, path: Path, proxy: Optional[str], skew: int) -> None:
        self._path = path
        self._proxy = proxy
        self._skew = skew
        self._cache: Optional[dict[str, Any]] = None
        self._lock_path = path.with_suffix(".lock")

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            raise LlmError(
                f"SIWC 凭据不存在：{self._path}（先跑 scripts/siwc_login.py 授权）", "AUTH")
        self._cache = json.loads(self._path.read_text(encoding="utf-8"))
        return self._cache

    def _save(self, data: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        self._cache = data

    def _fresh(self, data: dict[str, Any]) -> bool:
        at = str(data.get("access_token") or "")
        exp = float(data.get("expires_at") or 0)
        return bool(at) and time.time() < exp - self._skew

    def _acquire_lock(self, wait_seconds: float = 30.0) -> bool:
        """原子创建锁文件；持有者崩溃留下的僵尸锁（>60s）可抢占。"""
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    if time.time() - self._lock_path.stat().st_mtime > 60:
                        self._lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.2)
            except OSError:
                return False
        return False

    def _release_lock(self) -> None:
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def access_token(self) -> str:
        data = self._load()
        if self._fresh(data):
            return str(data["access_token"])

        if not self._acquire_lock():
            # 拿不到锁：宁可报错也不并发刷新（并发刷新会互相作废 refresh token）
            raise LlmError("SIWC 刷新锁等待超时（另一进程正在刷新）", "AUTH")
        try:
            # 拿到锁后**重读文件**：别的进程可能已经刷好了
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._cache = data
            except Exception:  # noqa: BLE001
                pass
            if self._fresh(data):
                return str(data["access_token"])

            rt = str(data.get("refresh_token") or "")
            if not rt:
                raise LlmError("SIWC 无 refresh_token，且 access_token 已过期——请重新授权", "AUTH")
            body = {
                "grant_type": "refresh_token",
                "client_id": str(data.get("client_id") or "dynamic_agent_client"),
                "refresh_token": rt,
                "resource": RESOURCE,
            }
            async with httpx.AsyncClient(proxy=self._proxy, timeout=60) as c:
                r = await c.post(TOKEN_ENDPOINT, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
            if r.status_code != 200:
                raise LlmError(
                    f"SIWC 刷新 token 失败：HTTP {r.status_code} {r.text[:200]}"
                    "（refresh_token 可能已被别处轮换 ⇒ 重跑 scripts/siwc_login.py）", "AUTH")
            tok = r.json()
            new_at = str(tok.get("access_token") or "")
            if not new_at:
                raise LlmError("SIWC 刷新响应里没有 access_token", "AUTH")
            data.update({
                "access_token": new_at,
                "refresh_token": str(tok.get("refresh_token") or rt),
                "expires_at": time.time() + int(tok.get("expires_in") or 3600),
                "scope": tok.get("scope") or data.get("scope"),
            })
            self._save(data)
            return new_at
        finally:
            self._release_lock()


@dataclass
class _OpenBlock:
    index: int
    kind: str  # 'text' | 'reasoning' | 'tool-call'
    text: str = ""
    call_id: Optional[str] = None
    name: Optional[str] = None


def _close(block: _OpenBlock) -> ContentBlock:
    if block.kind == "text":
        return TextBlock(text=block.text)
    if block.kind == "reasoning":
        return ReasoningBlock(text=block.text)
    return ToolCallBlock(id=block.call_id or "", name=block.name or "",
                         arguments=block.text)


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    out.append(part["text"])
            elif isinstance(part, str):
                out.append(part)
        return "".join(out)
    return ""


def _build_input(options: Any) -> list[dict[str, Any]]:
    """harness 的 chat 消息 → Responses API 的 input 数组。"""
    items: list[dict[str, Any]] = []
    if options.system:
        items.append({"role": "system", "content": str(options.system)})
    for m in options.messages or ():
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(m.get("tool_call_id") or m.get("call_id") or ""),
                "output": _as_text(m.get("content")),
            })
            continue
        tcs = m.get("tool_calls") or []
        if role == "assistant" and tcs:
            text = _as_text(m.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "arguments": str(fn.get("arguments") or "{}"),
                })
            continue
        text = _as_text(m.get("content"))
        if text:
            items.append({"role": role or "user", "content": text})
    return items


def _build_tools(options: Any) -> list[dict[str, Any]]:
    """harness 的 ToolSchema / chat 形状 tools → Responses 形状（function 字段拍平）。"""
    out: list[dict[str, Any]] = []
    for t in options.tools or ():
        # GenerateOptions.tools 是 ToolSchema 数据类（不是 dict）——实测漏判会让
        # 工具整批送不出去，模型直接回「未提供 read_card 工具」。
        if isinstance(t, dict):
            fn: Any = t.get("function") if isinstance(t.get("function"), dict) else t
            name = str(fn.get("name") or "")
            desc = fn.get("description")
            params = fn.get("parameters")
        else:
            name = str(getattr(t, "name", "") or "")
            desc = getattr(t, "description", None)
            params = getattr(t, "parameters", None)
        if not name:
            continue
        item: dict[str, Any] = {"type": "function", "name": name}
        if desc:
            item["description"] = str(desc)
        if isinstance(params, dict):
            item["parameters"] = params
        out.append(item)
    return out


def _map_usage(usage: dict[str, Any]) -> TokenUsage:
    details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(details.get("cached_tokens") or 0),
        reasoning_tokens=int(out_details.get("reasoning_tokens") or 0),
    )


class SiwcAdapter(LlmAdapter):
    """Sign in with ChatGPT：Responses API + 套餐额度。"""

    def __init__(self, config: SiwcAdapterOptions) -> None:
        self._config = config
        proxy = config.proxy or os.environ.get("SIWC_PROXY") or "http://127.0.0.1:7890"
        self._proxy = proxy
        self._tokens = _TokenStore(config.tokens_path, proxy, config.refresh_skew_seconds)

    def provider_info(self, provider: str) -> dict[str, str]:
        return {"id": provider, "name": provider}

    def provider_retry_policy(self, provider: str) -> Optional[ResolvedRetryPolicy]:
        try:
            return self._config.options(provider, "").retry_policy
        except Exception:  # noqa: BLE001
            return None

    async def stream(self, options: Any) -> AsyncIterator[StreamChunk]:
        connection = self._config.options(options.provider, options.model)
        token = await self._tokens.access_token()

        body: dict[str, Any] = {
            "model": options.model,
            "input": _build_input(options),
            "store": False,     # 实测强制：ChatGPT 套餐通道不许存
            "stream": True,     # 实测强制：不支持非流式
        }
        if options.temperature is not None:
            # [2026-11-19 实测] GPT-6 系是推理模型，端点直接拒收 temperature
            # （Unsupported parameter: temperature）⇒ 该族不传采样温度。
            _m = str(options.model or "").lower()
            if not _m.startswith(("gpt-6", "o1", "o3", "o4", "codex")):
                body["temperature"] = float(options.temperature)
        if options.max_tokens is not None:
            body["max_output_tokens"] = int(options.max_tokens)
        tools = _build_tools(options)
        if tools:
            body["tools"] = tools
        # [思考强度 2026-11-19 实测] 本通道接受 low/medium/high/xhigh 四档；
        # ``minimal`` 被端点拒（unsupported_value）⇒ 降级到 low。
        # summary=auto 才会回 reasoning 事件（实测 high/xhigh 有、low/medium 无）。
        _eff = str(options.reasoning_effort or "").strip().lower() \
            or str(getattr(connection.defaults, "default_effort", "") or "").strip().lower()
        if _eff == "minimal":
            _eff = "low"
        if _eff in ("low", "medium", "high", "xhigh"):
            body["reasoning"] = {"effort": _eff, "summary": "auto"}

        url = f"{connection.base_url.rstrip('/')}/responses"
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if connection.extra_headers:
            headers.update({str(k): str(v) for k, v in connection.extra_headers.items()})

        timeout = httpx.Timeout(connect=None, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, proxy=self._proxy) as client:
            try:
                request = client.build_request("POST", url, headers=headers,
                                               content=json.dumps(body, ensure_ascii=False))
                response = await client.send(request, stream=True)
            except Exception as exc:  # noqa: BLE001
                raise LlmError(f"SIWC 请求发不出去（proxy={self._proxy}）", "TRANSPORT",
                               cause=exc) from exc
            try:
                if response.status_code >= 400:
                    raise await self._http_error(response, url)
                async for chunk in self._translate(response):
                    yield chunk
            except httpx.TimeoutException as exc:
                raise LlmError("SIWC transport timeout", "TIMEOUT", cause=exc) from exc
            except LlmError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise LlmError("SIWC 流读取失败", "TRANSPORT", cause=exc) from exc
            finally:
                await response.aclose()

    async def _http_error(self, response: httpx.Response, url: str) -> LlmError:
        status = response.status_code
        message = f"API error (HTTP {status})"
        provider_error: Optional[dict[str, Any]] = None
        try:
            await response.aread()
            parsed = json.loads(response.content.decode("utf-8", "replace"))
            pe = parsed.get("error")
            if isinstance(pe, dict) and pe.get("message"):
                provider_error = pe
                message = str(pe["message"])
            elif parsed.get("detail"):
                message = str(parsed["detail"])
                provider_error = {"message": message}
        except Exception:  # noqa: BLE001
            pass
        detail = " ".join(str(x) for x in (
            (provider_error or {}).get("code"), (provider_error or {}).get("type"),
            (provider_error or {}).get("message")) if x)
        code = http_error_code(status, provider_error)
        if is_quota_exceeded_error(detail):
            code = QUOTA_EXCEEDED_CODE
        elif is_context_window_exceeded_error(detail):
            code = CONTEXT_WINDOW_EXCEEDED_CODE
        return LlmError(message, code, status=status,
                        request_id=response.headers.get("x-request-id") or None)

    async def _translate(self, response: httpx.Response) -> AsyncIterator[StreamChunk]:
        """Responses API SSE 事件 → harness StreamChunk。"""
        next_index = 0
        order: list[_OpenBlock] = []
        text_block: Optional[_OpenBlock] = None
        reasoning_block: Optional[_OpenBlock] = None
        tool_by_item: dict[str, _OpenBlock] = {}
        pending_usage: Optional[TokenUsage] = None
        finish: Optional[FinishReason] = None
        saw_tool = False

        def open_block(kind: str) -> _OpenBlock:
            nonlocal next_index
            b = _OpenBlock(index=next_index, kind=kind)
            next_index += 1
            order.append(b)
            return b

        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                ev = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                raise LlmError(f"malformed SSE payload: {raw[:120]}", "MALFORMED_RESPONSE") from exc

            etype = str(ev.get("type") or "")

            if etype == "error":
                err = ev.get("error") or {}
                msg = " ".join(str(x) for x in (err.get("code"), err.get("type"),
                                                err.get("message")) if x) or "provider error"
                raise LlmError(f"provider stream error: {msg}",
                               QUOTA_EXCEEDED_CODE if is_quota_exceeded_error(msg)
                               else "PROVIDER_STREAM_ERROR")

            if etype == "response.output_item.added":
                item = ev.get("item") or {}
                if item.get("type") == "function_call":
                    b = open_block("tool-call")
                    b.call_id = str(item.get("call_id") or item.get("id") or "")
                    b.name = str(item.get("name") or "")
                    tool_by_item[str(item.get("id") or "")] = b
                    saw_tool = True
                    yield BlockStart(index=b.index, block_type="tool-call")
                continue

            if etype == "response.function_call_arguments.delta":
                item_id = str(ev.get("item_id") or "")
                b = tool_by_item.get(item_id)
                if b is None:
                    b = open_block("tool-call")
                    tool_by_item[item_id] = b
                    saw_tool = True
                    yield BlockStart(index=b.index, block_type="tool-call")
                frag = str(ev.get("delta") or "")
                b.text += frag
                yield ToolCallDelta(index=b.index, id=b.call_id or "",
                                    arguments_delta=frag, name=b.name)
                continue

            if etype == "response.function_call_arguments.done":
                item_id = str(ev.get("item_id") or "")
                b = tool_by_item.get(item_id)
                if b is not None and not b.text and ev.get("arguments"):
                    frag = str(ev["arguments"])
                    b.text = frag
                    yield ToolCallDelta(index=b.index, id=b.call_id or "",
                                        arguments_delta=frag, name=b.name)
                continue

            if etype in ("response.reasoning_summary_text.delta",
                         "response.reasoning_text.delta"):
                txt = str(ev.get("delta") or "")
                if txt:
                    if reasoning_block is None:
                        reasoning_block = open_block("reasoning")
                        yield BlockStart(index=reasoning_block.index, block_type="reasoning")
                    reasoning_block.text += txt
                    yield ReasoningDelta(index=reasoning_block.index, text=txt)
                continue

            if etype == "response.output_text.delta":
                txt = str(ev.get("delta") or "")
                if txt:
                    if text_block is None:
                        text_block = open_block("text")
                        yield BlockStart(index=text_block.index, block_type="text")
                    text_block.text += txt
                    yield TextDelta(index=text_block.index, text=txt)
                continue

            if etype == "response.completed":
                resp = ev.get("response") or {}
                if isinstance(resp.get("usage"), dict):
                    pending_usage = _map_usage(resp["usage"])
                finish = ToolCallsReason() if saw_tool else StopReason()
                break

            if etype == "response.incomplete":
                resp = ev.get("response") or {}
                if isinstance(resp.get("usage"), dict):
                    pending_usage = _map_usage(resp["usage"])
                finish = MaxTokensReason()
                break

            if etype == "response.failed":
                resp = ev.get("response") or {}
                err = resp.get("error") or {}
                msg = " ".join(str(x) for x in (err.get("code"), err.get("message")) if x) \
                    or "response failed"
                raise LlmError(f"response failed: {msg}",
                               QUOTA_EXCEEDED_CODE if is_quota_exceeded_error(msg)
                               else "PROVIDER_STREAM_ERROR")

        if finish is None:
            raise LlmError("SIWC 流结束但没收到 response.completed", "STREAM_CLOSED")

        for b in order:
            yield BlockEnd(index=b.index, block=_close(b))
        if pending_usage is not None:
            yield UsageChunk(usage=pending_usage)
        if finish.kind == "stop" and not order:
            yield FinishChunk(reason=ErrorReason(
                failure=LlmFailure(message="model returned a completed response with no content",
                                   code=EMPTY_RESPONSE_CODE)))
        else:
            yield FinishChunk(reason=finish)
