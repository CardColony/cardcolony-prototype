# -*- coding: utf-8 -*-
"""**opencode HTTP 客户端**（`opencode serve` 的 headless API）。

**为什么不用 ACP**：``opencode acp`` 只走 stdio ndJSON ⇒ 得另写一套管道传输；
HTTP 有 162 个端点 + OpenAPI + SSE ⇒ httpx 直接复用。

**★ 一个必须带 UA 的坑**（2026-10-25 实测）：
opencode 自己调 ``opencode.ai/zen/go/v1`` 时，**Bun 的 fetch 不带浏览器 UA**
⇒ 被 Cloudflare 的 Browser Integrity Check 拦 ⇒ **403「error code: 1010」**
⇒ 连**用户已付费的 Go 订阅内的模型**也一起挂。
⇒ 解法：**用自定义 provider 直连该端点**，在 ``options.headers`` 里塞浏览器 UA
（实测这样就通了：``gogo/deepseek-v4.1-flash`` 真回"收到"）。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger("api.wb_exec.oc_client")

#: **全放行**的权限规则集（opencode 的形状是**数组**；写成对象 ⇒ 400 BadRequest）。
FULL_ALLOW_PERMISSION = [{"permission": "*", "pattern": "*", "action": "allow"}]


class OcError(RuntimeError):
    """opencode 调用失败。"""


def _q(directory: str) -> str:
    """目录走 **query**（body 里写会被忽略 ⇒ 用 server 启动目录）。"""
    if not directory:
        return ""
    return "?directory=" + urllib.parse.quote(directory.replace("\\", "/"))


class OcClient:
    """一个 ``opencode serve`` 进程的 HTTP 客户端。"""

    def __init__(self, base_url: str, *, timeout: float = 900.0,
                 connect_timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._client = None

    # ---------------------------------------------------------------- 内部
    def _http(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _json(self, path: str, method: str = "GET",
                    body: Optional[dict] = None, timeout: Optional[float] = None):
        import httpx
        try:
            resp = await self._http().request(
                method, f"{self.base_url}{path}",
                json=body, headers={"Content-Type": "application/json"},
                timeout=timeout or self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise OcError(f"{method} {path} 失败：{type(exc).__name__}: {exc}") from exc
        raw = resp.read()
        if resp.status_code >= 400:
            raise OcError(f"{method} {path} → HTTP {resp.status_code}: "
                          f"{raw.decode('utf-8', 'replace')}")
        try:
            return resp.status_code, json.loads(raw) if raw else {}
        except Exception:
            return resp.status_code, {}

    # ---------------------------------------------------------------- 会话
    async def create_session(self, directory: str, *,
                             permission: Optional[list] = None,
                             title: str = "wb-exec") -> str:
        """建会话（**一卡一目录** ⇒ ``directory`` 走 query）。返回 session id。"""
        body: dict[str, Any] = {"title": title}
        if permission is not None:
            body["permission"] = permission
        st, data = await self._json(f"/session{_q(directory)}", "POST", body)
        sid = ""
        if isinstance(data, dict):
            sid = str(data.get("id") or "")
        if not sid:
            raise OcError(f"建 session 失败：HTTP {st} {str(data)}")
        logger.info("[wb_exec] opencode session 建好 id=%s dir=%s", sid, directory)
        return sid

    async def prompt(self, session_id: str, provider: str, model: str,
                     text: str, directory: str) -> dict:
        """**同步**发任务（等它跑完）。

        ⚠ **响应体里的 ``parts`` 可能是空的**（实测）⇒ **正文要从
        :meth:`messages` 取**（或从 SSE 事件流取）。
        """
        body = {"model": {"providerID": provider, "modelID": model},
                "parts": [{"type": "text", "text": text}]}
        st, data = await self._json(f"/session/{session_id}/message{_q(directory)}",
                                    "POST", body)
        return data if isinstance(data, dict) else {}

    async def prompt_async(self, session_id: str, provider: str, model: str,
                           text: str, directory: str) -> None:
        """**异步**发任务（204 立即返回）⇒ 配合 SSE 用。"""
        body = {"model": {"providerID": provider, "modelID": model},
                "parts": [{"type": "text", "text": text}]}
        await self._json(f"/session/{session_id}/prompt_async{_q(directory)}",
                         "POST", body, timeout=60.0)

    async def messages(self, session_id: str, directory: str) -> list[dict]:
        """取全部消息（**正文在这里**）。"""
        st, data = await self._json(f"/session/{session_id}/message{_q(directory)}")
        return data if isinstance(data, list) else []

    async def abort(self, session_id: str, directory: str) -> None:
        """中止这一轮（用户中断 / 超时）。"""
        try:
            await self._json(f"/session/{session_id}/abort{_q(directory)}", "POST",
                             {}, timeout=30.0)
        except Exception as exc:  # noqa: BLE001 中止失败不阻断
            logger.warning("[wb_exec] opencode abort 失败: %s", exc)

    async def delete_session(self, session_id: str) -> tuple[int, Any]:
        """**删掉这个会话**（2026-11-04 加：卡片做完后在 opencode 里清残留）。

        实测：``DELETE /session/{id}`` ⇒ **HTTP 200**、库里会话数 156→155。

        ★ **只删我们自己 create_session 拿到的那个 id** ⇒ 用户的会话不受影响。
        """
        return await self._json(f"/session/{session_id}", "DELETE", timeout=60.0)

    async def events(self, directory: str) -> AsyncIterator[dict]:
        """订阅 **全局事件流** ``GET /event``（SSE）。

        ⚠ ``/api/session/{id}/event`` **不吐 SSE**（实测 0 条）⇒ 用全局这条，
        自己按 ``properties.sessionID`` 过滤（**必须有 sessionID 字段**）。
        """
        import httpx
        url = f"{self.base_url}/event{_q(directory)}"
        try:
            async with self._http().stream(
                    "GET", url,
                    headers={"Accept": "text/event-stream"},
                    timeout=httpx.Timeout(None, connect=self.connect_timeout)) as resp:
                if resp.status_code >= 400:
                    logger.warning("[wb_exec] opencode /event → HTTP %s",
                                   resp.status_code)
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except Exception as exc:  # noqa: BLE001 事件流断了不致命
            logger.warning("[wb_exec] opencode 事件流结束: %s", exc)


def session_text(messages: list[dict]) -> str:
    """从消息列表里取**最终正文**（最后一个 assistant 消息的 text part）。"""
    out = []
    for m in messages:
        info = m.get("info") if isinstance(m, dict) else None
        role = (info or {}).get("role") or m.get("role")
        if role != "assistant":
            continue
        for pt in ((info or {}).get("parts") or m.get("parts") or []):
            if isinstance(pt, dict) and pt.get("type") == "text":
                t = str(pt.get("text") or "").strip()
                if t:
                    out.append(t)
    return out[-1] if out else ""


def message_parts(messages: list[dict]) -> list[dict]:
    """把消息列表摊平成 part 序列（**正文/工具调用都在 part 里**）。"""
    out: list[dict] = []
    for m in messages:
        info = m.get("info") if isinstance(m, dict) else None
        for pt in ((info or {}).get("parts") or m.get("parts") or []):
            if isinstance(pt, dict):
                out.append(pt)
    return out
