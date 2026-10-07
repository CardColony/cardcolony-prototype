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
    逐字不变）；凝练线经 model_name="condenser" 指定独立凝练席（gateway.call
    直读 config["models"][model_name]，R2 实读支持任意席位名，gateway.py 禁改令不触发）。
    """
    gw = get_gateway()
    cfg = (gw.config.get("models") or {}).get(model_name, {})
    events: list[dict[str, Any]] = []
    result = await gw.call(
        model_name,
        system_prompt,
        user_prompt,
        temperature=float(cfg.get("temperature", 0.5)),
        max_tokens=cfg.get("max_tokens"),
        timeout=cfg.get("timeout"),
        skip_completion_marker=True,
        tools=tools,
        prior_messages=prior_messages or None,
        on_status=events.append,
    )
    if is_mock():
        model_used = "mock"
    else:
        entry = model_name
        for ev in events:
            if ev.get("phase") == "fallback":
                entry = ev.get("to_model", "fallback")
        model_used = (gw.config.get("models") or {}).get(entry, {}).get("model", entry)
    return result, model_used
