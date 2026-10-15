# -*- coding: utf-8 -*-
"""模型网关单例与调用封装。

gateway 真实接口（src/model_gateway/gateway.py，禁止修改，按其实际签名接入）：
- 构造：ModelGateway(config: dict)，config 是 models.yaml 经 model_config 编译后的平铺结构
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
from typing import Any, Optional

from ..model_gateway.gateway import build_gateway
from . import model_config

_gateway = None


def is_mock() -> bool:
    return os.environ.get("MOCK_LLM") == "1"


def get_gateway():
    """全局单例：经 model_config 读+编译配置初始化 gateway。"""
    global _gateway
    if _gateway is None:
        config = model_config.load()
        _gateway = build_gateway(config)
    return _gateway


def reload_gateway():
    """热重载：重读+重编译配置，重建 gateway 单例，一步换引用。

    在跑调用持旧对象跑完、新调用拿新对象——不重启、不打断、不杀任务。
    """
    global _gateway
    _gateway = build_gateway(model_config.reload())
    return _gateway


async def call_main(
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    model_name: Optional[str] = None,
    turn_tail: Optional[list[dict[str, Any]]] = None,
    turn_once: Optional[list[dict[str, Any]]] = None,
) -> tuple[dict[str, Any], str]:
    """以指定模型名发起调用，返回 (gateway结果, 实际使用的模型名)。

    Phase 7：tools 透传给 gateway.call（OpenAI function calling）。
    [凝练折叠开启 2026-10-07·任务5] 增 model_name 参数（缺省 None → 取当前默认模型，
    缺省行为逐字不变）；凝练线经 model_name=model_config.resolve("condense") 指定凝练模型。

    [信息流重出 Task4 · 2026-10-08] 主路径改走 ``gw.stream_call``（用户裁定：切
    stream_call）。改前 stream_call 是死代码——生产只经 ``call``，逐字流无处可出。
    三点保语义不变：
      1. 聚合 content / tool_calls / finish_reason 全部取自流末尾的终帧，
         与 call 同契约（含 truncated_tool_call 续写口）；
      2. 思考增量**不进** content（只作逐字流事件），故正文判定与续写不受污染；
      3. fallback 仍由 gateway 内部按 system.fallback_model 处理（on_status 事件流不变）。
    逐字流经 event_stream.emit_token 落到**独立** SSE（/tokens），
    归属卡经 contextvar 传入；无卡上下文时（凝练/归拢调用）自动不落逐字流。
    """
    model_name = model_name or model_config.default_model()
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
    # [DeepSeek 思考回传 2026-10-15] 攒本轮思考增量：DeepSeek 思考模型要求
    # 多轮调用时把上一轮的 reasoning_content 原样回传（不回传 → 400
    # "The `reasoning_content` in the thinking mode must be passed back"）。
    # 思考只在调用链内部传递（作 assistant 条的协议字段），不进对话内容、
    # 不进前端渲染——前端展示仍走 emit_token 通道，两路互不影响。
    _reasoning_parts: list[str] = []
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
        # [当前轮注入消息事件 2026-10-14] 本轮注入消息（context_preamble/
        # context_injection 的投影）——追加在用户消息之后。默认 None ⇒ 既有
        # 全部调用方（三席/凝练/起草/卡间调起）消息构造逐字不变。
        turn_tail=turn_tail or None,
        # [判断协议改注入 2026-10-14] 「仅本次」下发消息（判断协议投影）——
        # 追加在用户消息之后，**不进轮槽、不固化**；默认 None ⇒ 逐字同旧。
        turn_once=turn_once or None,
    ):
        if "chunk_type" in ev:
            # 逐字流增量：进独立通道，不进 content
            if ev["chunk_type"] == "reasoning":
                _reasoning_parts.append(ev.get("text") or "")   # 攒起来供回传
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
    # [DeepSeek 思考回传 2026-10-15] 本轮思考随 result 返回（调用方决定是否回传）。
    # 字段名用协议名 reasoning_content；无思考时为空串（与旧行为逐字兼容——
    # 旧 result 无此键，新增键不改变既有读取）。
    result["reasoning_content"] = "".join(_reasoning_parts)
    if is_mock():
        model_used = "mock"
    else:
        entry = model_name
        for ev in events:
            if ev.get("phase") == "fallback":
                entry = ev.get("to_model", "fallback")
        model_used = (gw.config.get("models") or {}).get(entry, {}).get("model", entry)
    return result, model_used
