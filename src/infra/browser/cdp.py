# -*- coding: utf-8 -*-
"""CDP 客户端（Chrome DevTools Protocol over WebSocket，纯标准库）。

覆盖本底座需要的子集：
- 浏览器级连接（DevToolsActivePort 给出的 browser ws）；
- Target.createTarget / attachToTarget（flatten 会话）／closeTarget；
- 会话域命令（Page/Runtime/Input/Log/Network/Emulation）；
- 事件缓冲：console（Runtime.consoleAPICalled + Log.entryAdded +
  Runtime.exceptionThrown）、network（request/response/failed）、
  页面导航（Page.frameNavigated / loadEventFired）。

模型不可见这一层；上一层 session.py 把它包成语义动作。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import wsclient


class CDPError(Exception):
    pass


class CDPCallError(CDPError):
    def __init__(self, code: int, message: str):
        super().__init__(f"CDP {code}: {message}")
        self.code = code
        self.message = message


class CDPConnection:
    """一条到 Chrome browser 端点的 ws 连接，管理多个 flatten 会话。"""

    def __init__(self, ws_url: str, timeout: float = 15.0):
        self._ws = wsclient.WSConnection.connect(ws_url, timeout=timeout)
        self._next_id = 0
        self._lock = threading.Lock()
        # 事件缓冲：sessionId → list[event dict]
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._browser_events: list[dict[str, Any]] = []

    # ---- 基础收发 ----

    def _next(self) -> int:
        self._next_id += 1
        return self._next_id

    def send(self, method: str, params: dict[str, Any] | None = None,
             session_id: str = "", timeout: float = 30.0) -> dict[str, Any]:
        """发一条命令并等到同 id 的响应；中途收到的事件进缓冲。"""
        with self._lock:
            mid = self._next()
            msg: dict[str, Any] = {"id": mid, "method": method}
            if params:
                msg["params"] = params
            if session_id:
                msg["sessionId"] = session_id
            self._ws.send_text(json.dumps(msg))
            while True:
                try:
                    raw = self._ws.recv_message(timeout=timeout)
                except wsclient.WSClosed as exc:
                    raise CDPError(f"连接断开: {exc}") from exc
                except (TimeoutError, OSError) as exc:
                    raise CDPError(f"等待响应超时({method}): {exc}") from exc
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if frame.get("id") == mid:
                    if "error" in frame:
                        err = frame["error"]
                        raise CDPCallError(int(err.get("code", 0)),
                                           str(err.get("message", "")))
                    return frame.get("result") or {}
                if "method" in frame:
                    self._buffer_event(frame)
                # 其他 id 的响应（理论上不会出现——串行调用）丢掉。

    def _buffer_event(self, frame: dict[str, Any]) -> None:
        sid = str(frame.get("sessionId") or "")
        entry = {"method": str(frame.get("method")),
                 "params": frame.get("params") or {}}
        if sid:
            buf = self._events.setdefault(sid, [])
        else:
            buf = self._browser_events
        buf.append(entry)
        if len(buf) > 2000:  # 防失控上界
            del buf[:1000]

    def drain_events(self, session_id: str = "") -> list[dict[str, Any]]:
        """取走并清空某会话（或浏览器级）的事件缓冲。"""
        buf = self._events.get(session_id) if session_id else self._browser_events
        if not buf:
            return []
        out, buf[:] = buf[:], []
        return out

    def pump(self, timeout: float = 0.05) -> int:
        """非阻塞地把已到的事件收进缓冲（动作后捞导航/控制台用）。

        先用 select 探可读性再读——recv_message 里 socket.timeout 不再
        标记断连，但读半截帧还是浪费；select 只保证「有字节可收」，
        读到半截时给一个正常帧超时兜底。
        """
        import select
        n = 0
        while True:
            try:
                r, _, _ = select.select([self._ws._sock], [], [], timeout)
            except (OSError, ValueError):
                break
            if not r:
                break
            try:
                raw = self._ws.recv_message(timeout=10.0)
            except (TimeoutError, OSError, wsclient.WSClosed):
                break
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "method" in frame:
                self._buffer_event(frame)
                n += 1
        return n

    # ---- Target 层 ----

    def create_target(self, url: str = "about:blank",
                      width: int = 1280, height: int = 800) -> str:
        res = self.send("Target.createTarget", {
            "url": url, "width": width, "height": height,
            "newWindow": False, "background": False})
        return str(res.get("targetId") or "")

    def attach(self, target_id: str) -> str:
        res = self.send("Target.attachToTarget",
                        {"targetId": target_id, "flatten": True})
        return str(res.get("sessionId") or "")

    def close_target(self, target_id: str) -> None:
        try:
            self.send("Target.closeTarget", {"targetId": target_id},
                      timeout=10.0)
        except CDPError:
            pass

    def list_targets(self) -> list[dict[str, Any]]:
        res = self.send("Target.getTargets", timeout=10.0)
        return list(res.get("targetInfos") or [])

    # ---- 常用会话命令的薄封装 ----

    def evaluate(self, session_id: str, expression: str,
                 timeout: float = 15.0, await_promise: bool = False) -> Any:
        """Runtime.evaluate → returnByValue。页面异常抛 CDPError。"""
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, session_id=session_id, timeout=timeout)
        exc = res.get("exceptionDetails")
        if exc:
            text = ((exc.get("exception") or {}).get("description")
                    or exc.get("text") or "页面脚本异常")
            raise CDPError(str(text)[:800])
        return (res.get("result") or {}).get("value")

    def enable_session_domains(self, session_id: str) -> None:
        """开一个页会话要用的域：Page/Runtime/Log/Network/Emulation。"""
        self.send("Page.enable", session_id=session_id)
        self.send("Runtime.enable", session_id=session_id)
        try:
            self.send("Log.enable", session_id=session_id)
        except CDPError:
            pass
        try:
            self.send("Network.enable", session_id=session_id,
                      params={"maxTotalBufferSize": 4 * 1024 * 1024,
                              "maxResourceBufferSize": 1024 * 1024})
        except CDPError:
            pass
        try:
            self.send("Emulation.setDeviceMetricsOverride", {
                "width": 1280, "height": 800,
                "deviceScaleFactor": 1, "mobile": False},
                session_id=session_id)
        except CDPError:
            pass
        # 关掉 Chrome 自带的页面级噪声（不影响功能，能省一点心跳）。
        try:
            self.send("Page.setWebLifecycleState",
                      {"state": "active"}, session_id=session_id)
        except CDPError:
            pass

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass
