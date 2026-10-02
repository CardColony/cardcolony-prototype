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
) -> tuple[dict[str, Any], str]:
    """以 main 模型发起调用，返回 (gateway结果, 实际使用的模型名)。

    Phase 7：tools 透传给 gateway.call（OpenAI function calling）。
    """
    gw = get_gateway()
    cfg = (gw.config.get("models") or {}).get("main", {})
    events: list[dict[str, Any]] = []
    result = await gw.call(
        "main",
        system_prompt,
        user_prompt,
        temperature=float(cfg.get("temperature", 0.5)),
        max_tokens=int(cfg.get("max_tokens", 2048)),
        timeout=int(cfg.get("timeout", 120)),
        skip_completion_marker=True,
        tools=tools,
        prior_messages=prior_messages or None,
        on_status=events.append,
    )
    if is_mock():
        model_used = "mock"
    else:
        entry = "main"
        for ev in events:
            if ev.get("phase") == "fallback":
                entry = ev.get("to_model", "fallback")
        model_used = (gw.config.get("models") or {}).get(entry, {}).get("model", entry)
    return result, model_used
