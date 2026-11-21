"""记录适配器已发送请求的证据。"""
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def record_dispatch(options: Any, model: str, *, transport: str, api_base: str = "") -> None:
    observer = getattr(options, "on_dispatch", None)
    if observer is not None:
        try:
            observer({"request_model": model, "transport": transport, "api_base": api_base})
        except Exception:
            logger.exception("写模型请求发送账失败")


async def send_stream(client: httpx.AsyncClient, request: httpx.Request, options: Any,
                      *, model: str, api_base: str) -> httpx.Response:
    if getattr(options, "on_dispatch", None) is None:
        return await client.send(request, stream=True)

    observed = False

    def record() -> None:
        nonlocal observed
        if not observed:
            observed = True
            record_dispatch(options, model, transport="http", api_base=api_base)

    previous = request.extensions.get("trace")
    request_body_started = False

    async def trace(name: str, info: dict[str, Any]) -> None:
        nonlocal request_body_started
        scope, _, event = name.rpartition(".")
        if scope in ("http11.send_request_body", "http2.send_request_body"):
            if event == "started":
                # HTTPS 代理的 CONNECT 也可能复用 trace，只认实际模型请求的 POST。
                method = getattr(info.get("request"), "method", None)
                request_body_started = method in (request.method, request.method.encode("ascii"))
            elif event == "complete" and request_body_started:
                record()
        if previous is not None:
            await previous(name, info)

    request.extensions["trace"] = trace
    response = await client.send(request, stream=True)
    # 某些自定义传输没有 httpcore trace。响应头同样证明请求已发。
    record()
    return response
