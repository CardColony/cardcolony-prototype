# -*- coding: utf-8 -*-
"""ACP 客户端（JSON-RPC over SSE，HTTP 绑定）。

**契约全部来自实测**（2026-10-23，WorkBuddy 37.10.3 CLI，`codebuddy-lite-wb` bundle）：

- 握手：``POST /api/v1/acp/connect``  body ``{}`` ⇒
  ``{"data":{"connectionId":"…","sessionToken":"…"}}``（信封是 ``data`` 包裹的）
- 之后：``POST /api/v1/acp``  body ＝**单个** JSON-RPC 消息（不是 NDJSON 数组），
  响应是 ``text/event-stream``：``event: message`` ＋ ``data: {jsonrpc,…}``
- **必带头**：``x-codebuddy-request: 1``（缺 ⇒ 403 Missing required header）
- 必带头：``acp-connection-id`` ＋ ``acp-session-token``
- SSE 注释帧：``:ok``（首帧）＋ 每 30 秒 ``: heartbeat``（网关级心跳）
- prompt 级心跳：``session/update`` 里 ``_meta["codebuddy.ai/streamKeepalive"]=true``（每 60 秒）
- 错误：JSON-RPC error；``data.category=="quota"`` 表示额度/过载类失败（可 fallback）

**为什么一次一个 JSON-RPC**：实测把两个消息放一个 body 会报
``Unexpected non-whitespace character after JSON``（服务端按单 JSON 解析）。
"""
from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

import httpx

logger = logging.getLogger("api.wb_exec.acp")

#: 网关要求的客户端身份头（缺 ⇒ 403）。
HEADER_REQUEST = "x-codebuddy-request"
HEADER_VALUE = "1"
HEADER_CONN = "acp-connection-id"
HEADER_TOKEN = "acp-session-token"

#: 错误分类：WorkBuddy 自己把额度/过载类归到 ``category=="quota"``。
CATEGORY_QUOTA = "quota"
#: 用户主动中断（"Task was interrupted by user"）⇒ **不自动续**。
INTERRUPTED_MARK = "interrupted by user"


class WbAcpError(RuntimeError):
    """ACP 调用失败。

    ``category=="quota"`` ⇒ 可 fallback 到次级模型（额度/过载）。
    ``is_auth_required`` ⇒ **不可重试**（CLI 没登录，重连多少次都没用）⇒ 直接回落。
    ``is_user_interruption`` ⇒ 人主动中断，**不自动续**。
    """

    def __init__(self, message: str, *, code: Optional[int] = None,
                 category: str = "", data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.data = data

    @property
    def is_quota(self) -> bool:
        return self.category == CATEGORY_QUOTA

    @property
    def is_auth_required(self) -> bool:
        """**账号未登录**（CLI 缺 authSession）。

        **必须靠文本判断，不能只看 code**（2026-10-23 实跑抓到的坑）：
        ACP 的 ``-32000`` **不只**是鉴权失败——**入口被占用**（同一网关并发跑两个
        任务，第二个被拒）返回的也是 ``-32000``。
        ⇒ 早先"``code == -32000`` ⇒ 鉴权失败"的写法 ⇒ 多卡并发时**误判成未登录**，
        还会把**整个执行体**永久标死（``mark_unauthenticated``）⇒ 后续所有卡秒回落。

        真鉴权失败长这样（文本一定带关键词）::

            Authentication required. Please use /login command to sign in to your account

        ⇒ 现在**只认文本关键词**；``code`` 只作辅助（文本被截断时兜底）。
        """
        low = str(self).lower()
        text_hit = ("authentication required" in low or "authsession" in low
                    or "auth_required" in low or "not logged in" in low
                    or "please use /login" in low or "sign in to your account" in low)
        if text_hit:
            return True
        # **只有 code、没有文本 ⇒ 不判成鉴权失败**：
        # 熔断的代价（整个执行体 60 秒内全回落）远大于误判的代价
        # （当普通错误重试几次 ⇒ 回落系统模型）⇒ 宁可重试，不要熔断。
        return False

    @property
    def is_user_interruption(self) -> bool:
        return INTERRUPTED_MARK in str(self).lower()


class WbModelUnavailable(WbAcpError):
    """**该执行体没有这个模型**（配比表里的模型在这个版本的目录里不存在）。

    实测（2026-10-23 20:45，cn sidecar）：返回体是
    ``stopReason=refusal`` ＋ ``FAILED_MODEL_REQUEST`` ＋
    ``displayMsg:{"en":"Model unavailable. Please switch models"}``
    ⇒ **换配比表里的另一个候选**，不是失败、也不是静默回落系统模型。

    **⚠ 别拿 ``session/load`` 的 ``availableModels`` 当目录**（实测 20:45：
    那是**被过滤过的本地列表**，cn 只回 5 个，可 hy3 / step-5-preview 实测都能调）
    ⇒ 只有**实测**报这个错才算数。
    """


class WbExecutorUnavailable(WbAcpError):
    """**这个执行体现在用不了**（桌面端网关没起 / 没登录 / 进程被回收）。

    ⇒ **换配比表里的另一个候选**（比如国际版网关正好被回收 ⇒ 改走国版），
    而不是"重连三次然后整卡失败"。

    实测（2026-10-23 20:50）：桌面端托管的那个网关**会自己消失**（桌面端回收
    它的 agent runtime 进程）⇒ 换候选是必须的。
    """


@dataclass
class AcpNotification:
    """一条 agent→client 的通知（``session/update`` 等）。"""

    method: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def update_kind(self) -> str:
        upd = self.params.get("update")
        if isinstance(upd, dict):
            return str(upd.get("sessionUpdate") or "")
        return ""

    @property
    def is_keepalive(self) -> bool:
        upd = self.params.get("update")
        if not isinstance(upd, dict):
            return False
        meta = upd.get("_meta")
        if not isinstance(meta, dict):
            return False
        return bool(meta.get("codebuddy.ai/streamKeepalive"))


def _text_of(content: Any) -> str:
    """ACP content 块 → 纯文本（只取 type=="text" 的块）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        return "".join(_text_of(x) for x in content)
    return ""


class AcpClient:
    """一个网关上的一个 ACP 连接。"""

    def __init__(self, base_url: str, *, timeout: float = 600.0,
                 connect_timeout: float = 30.0,
                 on_notification: Optional[Callable[[AcpNotification], None]] = None,
                 on_keepalive: Optional[Callable[[], None]] = None,
                 extra_headers: Optional[dict[str, str]] = None,
                 ) -> None:
        self.base_url = base_url.rstrip("/")
        #: **读超时**（传输层兜底；必须 > WorkBuddy 的 60s keepalive 间隔，
        #: 否则"正在思考"会被误判成"断了"）。**不是业务判据。**
        self.timeout = timeout
        #: **建连超时**（纯传输层）。
        self.connect_timeout = connect_timeout
        self.connection_id = ""
        self.session_token = ""
        self._rpc_id = 0
        self._on_notification = on_notification
        self._on_keepalive = on_keepalive
        self._extra_headers = dict(extra_headers or {})
        self._client: Optional[httpx.AsyncClient] = None

    # ---------------------------------------------------------------- 生命周期
    async def __aenter__(self) -> "AcpClient":
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 关闭失败零阻断
                pass
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout))
        return self._client

    def _headers(self) -> dict[str, str]:
        h = dict(self._extra_headers)
        h.update({
            HEADER_REQUEST: HEADER_VALUE,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        if self.connection_id:
            h[HEADER_CONN] = self.connection_id
        if self.session_token:
            h[HEADER_TOKEN] = self.session_token
        return h

    # ---------------------------------------------------------------- 握手
    async def connect(self) -> None:
        """``POST /api/v1/acp/connect`` 换 connectionId + sessionToken。

        **传输层重试一次**（2026-10-23 18:20 实测踩到）：httpx 会复用连接池里的
        keep-alive 连接；**服务端刚好把那条空闲连接关掉**时，POST 会直接
        ``Server disconnected without sending a response``
        （一个响应都没给）。⇒ 纯传输层的偶发失败**重试一次**即可，
        不是业务判据、也不引入任何时间闸。
        """
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                await self._connect_once()
                return
            except httpx.TransportError as exc:
                last = exc
                logger.info("[wb_exec] acp/connect 传输失败（第 %d 次）：%s",
                            attempt + 1, exc)
                continue
        raise WbAcpError(f"连接网关失败: {last}")

    async def _connect_once(self) -> None:
        """握手（一次）。"""
        client = self._http()
        try:
            resp = await client.post(
                f"{self.base_url}/api/v1/acp/connect",
                headers={HEADER_REQUEST: HEADER_VALUE,
                         "Content-Type": "application/json"},
                json={},
            )
        except httpx.TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WbAcpError(f"连接网关失败: {exc}") from exc
        if resp.status_code != 200:
            raise WbAcpError(
                f"acp/connect HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise WbAcpError("acp/connect 响应不是 JSON") from exc
        # **信封有两种形态**（实测）：桌面端网关（password 模式）返回**扁平**的
        # ``{"connectionId":…,"sessionToken":…}``；``--auth none`` 的自建网关
        # 才是 ``{"data":{…}}``。⇒ 两种都认。
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        self.connection_id = str(data.get("connectionId") or "")
        self.session_token = str(data.get("sessionToken") or "")
        if not self.connection_id or not self.session_token:
            raise WbAcpError(f"acp/connect 响应缺 id/token: {str(payload)[:200]}")

    # ---------------------------------------------------------------- JSON-RPC
    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _sse(self, resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
        """把 SSE 拆成 (kind, value)：kind ∈ {"comment","event","data"}。"""
        event = ""
        async for line in resp.aiter_lines():
            line = line.rstrip("\r")
            if not line:
                event = ""
                continue
            if line.startswith(":"):
                yield ("comment", line[1:].strip())
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                yield ("event", event)
                continue
            if line.startswith("data:"):
                yield ("data", line[5:].strip())

    async def rpc(self, method: str, params: Optional[dict[str, Any]] = None,
                  *, timeout: Optional[float] = None,
                  on_result: Optional[Callable[[dict[str, Any]], None]] = None,
                  ) -> dict[str, Any]:
        """发一个 JSON-RPC 请求，等它的 result/error。

        期间的 ``session/update`` 通知走 ``self._on_notification`` 回调；
        SSE 注释帧（``: heartbeat``）走 ``self._on_keepalive`` 回调。
        """
        rpc_id = self._next_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0", "id": rpc_id, "method": method,
        }
        if params is not None:
            payload["params"] = params
        client = self._http()
        rt = timeout if timeout is not None else self.timeout
        # 建连与读分开：**读超时是传输层兜底**（> keepalive 间隔），
        # 建连失败才是真问题。
        tmo = httpx.Timeout(rt, connect=self.connect_timeout)
        try:
            async with client.stream(
                "POST", f"{self.base_url}/api/v1/acp",
                headers=self._headers(), json=payload, timeout=tmo,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise WbAcpError(
                        f"{method} HTTP {resp.status_code}: {body[:200]}")
                async for kind, val in self._sse(resp):
                    if kind == "comment":
                        if self._on_keepalive is not None:
                            self._on_keepalive()
                        continue
                    if kind != "data" or not val:
                        continue
                    try:
                        obj = json.loads(val)
                    except Exception:  # noqa: BLE001 坏帧不致命
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("id") == rpc_id:
                        if "error" in obj:
                            err = obj.get("error") or {}
                            data = err.get("data") if isinstance(err, dict) else None
                            category = ""
                            if isinstance(data, dict):
                                category = str(data.get("category") or "")
                            msg = str((err or {}).get("message") or "ACP 调用失败")
                            if not category and "quota" in msg.lower():
                                category = CATEGORY_QUOTA
                            raise WbAcpError(msg, code=err.get("code"),
                                             category=category, data=data)
                        result = obj.get("result")
                        if on_result is not None and isinstance(result, dict):
                            on_result(result)
                        return result if isinstance(result, dict) else {}
                    # **★ 服务端请求**（带 id 的 method）⇒ **客户端必须应答**。
                    #   实测（2026-10-24）：WorkBuddy 需要人工批准时，会通过 ACP
                    #   发来**带 id 的 method**（权限请求一类）。
                    #   早先的客户端只把"带 method 的帧"当通知 ⇒ **从来没人应答**
                    #   ⇒ agent 一直等 ⇒ ``runStatus=waiting_for_permission``
                    #   ⇒ 那个 agent 槽**被永久占住** ⇒ 后面的卡全卡死。
                    #   ⇒ 现在：**自动批准**（用户令：「卡审批就给它审批」）。
                    if obj.get("method") and obj.get("id") is not None:
                        await self._answer_server_request(obj)
                        continue
                    # 通知（无 id 或 id 不同）
                    if obj.get("method"):
                        if self._on_notification is not None:
                            try:
                                res = self._on_notification(AcpNotification(
                                    method=str(obj.get("method")),
                                    params=obj.get("params") or {},
                                ))
                                # 回调可以是协程（async def）——等它完成
                                if inspect.isawaitable(res):
                                    await res
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("[wb_exec] 通知回调异常（不阻断）: %s", exc)
        except WbAcpError:
            raise
        except httpx.TimeoutException as exc:
            raise WbAcpError(f"{method} 超时（{timeout or self.timeout}s）") from exc
        except Exception as exc:  # noqa: BLE001
            raise WbAcpError(f"{method} 失败: {exc}") from exc
        raise WbAcpError(f"{method} 未收到响应（流已结束）")

    # ---------------------------------------------------------------- 服务端请求
    @staticmethod
    def _permission_grant(params: dict) -> dict:
        """权限请求 ⇒ **选一个"允许"项**（没有允许项 ⇒ 选第一个 ⇒ 都没有才取消）。

        **★ 为什么要自动批准**（用户令 2026-10-24：「卡审批就他妈给它审批」）：
        没人会来点这个批准 ⇒ agent 干等 ⇒ **槽被永久占住** ⇒ 后面的卡全卡死。
        自动化场景里我们就是那个审批人 ⇒ 直接放行。
        """
        options = (params.get("options") or [])
        allow = ""
        fallback = ""
        for opt in options:
            if not isinstance(opt, dict):
                continue
            oid = str(opt.get("optionId") or "")
            kind = str(opt.get("kind") or "").lower()
            if not oid:
                continue
            if not fallback:
                fallback = oid
            if not allow and "allow" in kind and "reject" not in kind \
                    and "deny" not in kind:
                allow = oid
        chosen = allow or fallback
        if chosen:
            return {"outcome": {"outcome": "selected", "optionId": chosen}}
        return {"outcome": {"outcome": "cancelled"}}

    async def _answer_server_request(self, obj: dict) -> None:
        """**自动应答服务端的请求**（权限批准）。

        ACP over HTTP 的应答走**同一个端点的另一次 POST**（带 connection 头）。
        """
        method = str(obj.get("method") or "")
        rid = obj.get("id")
        params = obj.get("params") or {}
        result = self._permission_grant(params)
        body = {"jsonrpc": "2.0", "id": rid, "result": result}
        try:
            await self._http().post(f"{self.base_url}/api/v1/acp",
                                     headers=self._headers(), json=body)
            logger.info("[wb_exec] 已自动批准服务端请求 %s（id=%s）⇒ %s",
                        method, rid, result)
        except Exception as exc:  # noqa: BLE001 应答失败不阻断主流程
            logger.warning("[wb_exec] 应答服务端请求 %s 失败: %s", method, exc)

    # ---------------------------------------------------------------- ACP 方法
    async def initialize(self) -> dict[str, Any]:
        return await self.rpc("initialize", {
            "protocolVersion": 1, "clientCapabilities": {},
        })

    async def session_new(self, cwd: str,
                          session_id: Optional[str] = None,
                          ) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": cwd, "mcpServers": []}
        if session_id:
            params["sessionId"] = session_id
        return await self.rpc("session/new", params)

    async def session_load(self, session_id: str, cwd: str, *,
                           take_writer: bool = True,
                           ) -> dict[str, Any]:
        """**跨进程恢复会话**（实测：网关进程被杀后，新进程 session/load 可恢复）。

        ``takeWriter`` 抢会话写者权；抢不到 ⇒ 服务端走只读跟随
        （``_meta["codebuddy.ai/followOnlyAccept"]``），此时**不能继续发 prompt**。
        """
        params: dict[str, Any] = {
            "sessionId": session_id, "cwd": cwd, "mcpServers": [],
        }
        if take_writer:
            params["_meta"] = {"codebuddy.ai/takeWriter": True}
        return await self.rpc("session/load", params)

    async def set_config_option(self, session_id: str, config_id: str,
                                value: Any) -> dict[str, Any]:
        """per-session 配置（model / permission_mode / sandbox / context_window…）。"""
        return await self.rpc("session/set_config_option", {
            "sessionId": session_id, "configId": config_id, "value": value,
        })

    async def prompt(self, session_id: str, text: str, *,
                     timeout: Optional[float] = None,
                     on_update: Optional[Callable[[AcpNotification], None]] = None,
                     ) -> dict[str, Any]:
        """发起一次任务。**响应在任务结束后才到**，期间全是 session/update 通知。"""
        if on_update is not None:
            self._on_notification = on_update
        try:
            return await self.rpc("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=timeout)
        finally:
            self._on_notification = None

    async def enqueue_followup(self, session_id: str, text: str) -> dict[str, Any]:
        """**追加**后续任务（幂等续跑的正路：不重跑已完成段、不重复消耗）。"""
        return await self.rpc("session/enqueue_followup", {
            "sessionId": session_id,
            "contentBlocks": [{"type": "text", "text": text}],
        })

    async def cancel(self, session_id: str) -> dict[str, Any]:
        return await self.rpc("session/cancel", {"sessionId": session_id})


async def fetch_health(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """探网关健康（带必带头）。**失败不抛**，返回 ``{"ok":False,…}``。"""
    url = f"{base_url.rstrip('/')}/api/v1/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url, headers={HEADER_REQUEST: HEADER_VALUE})
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code}
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        return {"ok": True, **(data if isinstance(data, dict) else {})}
    except Exception as exc:  # noqa: BLE001 探活失败不是错误，是"不健康"
        return {"ok": False, "error": str(exc)}
