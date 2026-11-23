"""记录适配器已发送请求的证据。"""
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def record_dispatch(options: Any, model: str, *, transport: str,
                    api_base: str = "", attempt: str = "") -> None:
    observer = getattr(options, "on_dispatch", None)
    if observer is not None:
        try:
            observer({"request_model": model, "transport": transport,
                      "api_base": api_base, "attempt_id": attempt})
        except Exception:
            logger.exception("写模型请求发送账失败")


async def send_stream(client: httpx.AsyncClient, request: httpx.Request, options: Any,
                      *, model: str, api_base: str, adapter: str = "") -> "tuple[httpx.Response, str]":
    # [批五·同跳预算] 鉴权准备/请求构造完毕后、实际 ``client.send`` 前
    # 原子领用同跳尝试额度——进入发送调用即记一次尝试；闸拒上抛
    # （控制结果），本次零出站。
    gate = getattr(options, "attempt_gate", None)
    if gate is not None:
        gate()
    # [批七·发送边界台账] 进入发送调用即一笔物理尝试；adapter 自报家门
    # 供端点用量按线路口径归一化（openai_compat/siwc/anthropic）。
    # [返修] **返回 (response, attempt_id)**——回执凭显式编号补记，
    # 并发收口不串到别人的尝试上。
    from ...infra import cost_ledger as _cl
    _led = getattr(options, "ledger", None)      # 显式台账（contextvar 不可见路径）
    facts = getattr(options, "ledger_attempt", None)
    attempt_fields = {k: v for k, v in (facts or {}).items() if k != "attempt_id"}
    _aid = _cl.note_attempt(ledger=_led,
                          request_model=model, api_base=api_base, adapter=adapter,
                          request_bytes=len(request.content or b""), **attempt_fields)
    if facts is not None:
        facts["attempt_id"] = _aid

    observed = False

    def record() -> None:
        nonlocal observed
        if not observed:
            observed = True
            _cl.note_sent(_aid, ledger=_led, request_model=model)
            record_dispatch(options, model, transport="http",
                            api_base=api_base, attempt=_aid)

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
    return response, _aid
