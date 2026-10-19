# -*- coding: utf-8 -*-
"""模型网关单例与调用封装。

gateway 真实接口（src/model_gateway/gateway.py，禁止修改，按其实际签名接入）：
- 构造：ModelGateway(config: dict)，config 是 models.yaml 经 model_config 编译后的平铺结构
  （gateway 内部访问 self.config["models"][model_name] 和 config["retry_grading"]）。
- call() 返回 {content, tool_calls, truncated_tool_call, truncated_reply,
  finish_reason, tokens_used, latency_ms}——不含所用模型名。
- **[模型设置 2026-10-19 产品裁定] 无降级模型**：请求指定哪个模型就用哪个，失败如实上抛。
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
      2. 思考增量**不进** content（只作逐字流事件），故正文判定与续写不受污染（无降级，见 2026-10-19 裁定）。
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
    # [模型层底座抄写 2026-10-16] DeepSeek 思考回传的「每条 assistant 必带
    # reasoning_content」规范化**已上移**到网关兼容壳（`gateway._ds_norm`，对
    # turn_tail/turn_once 生效）——此处的旧 `_ds_norm` 删除，避免两处重复。
    # 行为逐字不变（网关判定条件同旧：模型名/线名含 deepseek 或路由到 deepseek）。

    _reasoning_parts: list[str] = []
    # [直接停止 2026-10-15] 生成循环内查人停置位：命中即 break 断流，
    # 停止接收增量、不再等模型吐完（软断直停，正常收口不留半截轮）。
    # [直接停止·不丢字 2026-10-15 裁定] 断流**不等于丢字**：已收到的正文增量
    # 要照常交付（既有口径「不丢已产出的字」，test_slots_control 钉着）。
    # 故此处累计 content 增量，断流时用它合成终帧——与「立刻停」同时成立。
    _content_parts: list[str] = []
    _stopped = False
    from . import control as _ctl
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
            else:
                _content_parts.append(ev.get("text") or "")     # 断流时合成终帧用
            if ctx:
                _es.emit_token(ctx[0], ctx[1],
                               "reasoning_chunk" if ev["chunk_type"] == "reasoning"
                               else "content_chunk",
                               ev.get("text") or "")
        elif "content" in ev:
            result = ev
        # [直接停止 2026-10-15] 停位检查放在**本帧处理之后**：这一帧已经收到、
        # 已经累计/已发流，就照收；从**下一帧**起不再接收 ⇒ 既立刻停、又不丢已产出的字。
        if ctx and _ctl.pending_stop(ctx[0], ctx[1]):
            _stopped = True
            break
    if not result and _stopped:
        # 人停断流：把已产出的正文照常交付（工具轮未走完 ⇒ 不产生 tool_calls）。
        result = {"content": "".join(_content_parts), "tool_calls": [],
                  "truncated_tool_call": None, "finish_reason": "stop",
                  "tokens_used": 0, "latency_ms": 0}
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
        # [模型设置 2026-10-19 产品裁定] 无降级模型：实际所用即请求所指定。
        entry = model_name
        model_used = (gw.config.get("models") or {}).get(entry, {}).get("model", entry)
    return result, model_used
