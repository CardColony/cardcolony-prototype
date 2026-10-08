# -*- coding: utf-8 -*-
"""模型网关单例与调用封装。

gateway 真实接口（src/model_gateway/gateway.py，禁止修改，按其实际签名接入）：
- 构造：ModelGateway(config: dict)，config 直接是 models.yaml 解析结果
  （gateway 内部访问 self.config["models"][model_name] 和 config["retry_grading"]）。
- call() 返回 {content, tool_calls, truncated_tool_call, truncated_reply,
  finish_reason, tokens_used, latency_ms}——不含所用模型名。
- 降级：call 内部失败后递归调 call("fallback", ...)，唯一外部可观测信号是
  on_status 回调收到 {"phase": "fallback", "to_model": "fallback"} 事件，
  因此真实模型名通过收集 on_status 事件推断。
- MOCK_LLM=1 时 build_gateway 返回 MockGateway（接口一致，离线可跑）。

Phase 7：call_main 增加 tools 参数透传给 gateway.call。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from ..model_gateway.gateway import build_gateway

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config" / "models.yaml"

_gateway = None


def is_mock() -> bool:
    return os.environ.get("MOCK_LLM") == "1"


def get_gateway():
    """全局单例：读取 config/models.yaml 初始化 gateway。"""
    global _gateway
    if _gateway is None:
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        _gateway = build_gateway(config)
    return _gateway


async def call_main(
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    model_name: str = "main",
) -> tuple[dict[str, Any], str]:
    """以指定席位发起调用，返回 (gateway结果, 实际使用的模型名)。

    Phase 7：tools 透传给 gateway.call（OpenAI function calling）。
    [凝练折叠开启 2026-10-07·任务5] 增 model_name 参数（缺省 "main"，缺省行为
    逐字不变）；凝练线经 model_name="condenser" 指定独立凝练席。

    [信息流重出 Task4 · 2026-10-08] 主路径改走 ``gw.stream_call``（用户裁定：切
    stream_call）。改前 stream_call 是死代码——生产只经 ``call``，逐字流无处可出。
    三点保语义不变：
      1. 聚合 content / tool_calls / finish_reason 全部取自流末尾的终帧，
         与 call 同契约（含 truncated_tool_call 续写口）；
      2. 思考增量**不进** content（只作逐字流事件），故正文判定与续写不受污染；
      3. fallback 仍由 gateway 内部 main→fallback 处理（on_status 事件流不变）。
    逐字流经 event_stream.emit_token 落到**独立** SSE（/tokens），
    归属卡经 contextvar 传入；无卡上下文时（condenser/归拢）自动不落逐字流。
    """
    gw = get_gateway()
    cfg = (gw.config.get("models") or {}).get(model_name, {})
    events: list[dict[str, Any]] = []

    # 逐字流归属（无卡上下文则为 None → 不落逐字流）
    try:
        from . import event_stream as _es
        ctx = _es.current_stream_context()
    except Exception:  # noqa: BLE001
        ctx = None

    result: dict[str, Any] = {}
    async for ev in gw.stream_call(
        model_name,
        system_prompt,
        user_prompt,
        temperature=float(cfg.get("temperature", 0.5)),
        max_tokens=cfg.get("max_tokens"),
        timeout=cfg.get("timeout"),
        on_status=events.append,
        tools=tools,
        prior_messages=prior_messages or None,
    ):
        if "chunk_type" in ev:
            # 逐字流增量：进独立通道，不进 content
            if ctx:
                _es.emit_token(ctx[0], ctx[1],
                               "reasoning_chunk" if ev["chunk_type"] == "reasoning"
                               else "content_chunk",
                               ev.get("text") or "")
        elif "content" in ev:
            result = ev
    if not result:
        result = {"content": "", "tool_calls": [], "truncated_tool_call": None,
                  "finish_reason": "stop", "tokens_used": 0, "latency_ms": 0}
    if is_mock():
        model_used = "mock"
    else:
        entry = model_name
        for ev in events:
            if ev.get("phase") == "fallback":
                entry = ev.get("to_model", "fallback")
        model_used = (gw.config.get("models") or {}).get(entry, {}).get("model", entry)
    return result, model_used
