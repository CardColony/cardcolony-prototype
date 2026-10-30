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

import asyncio
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


#: 此刻正在模型流里的卡（key=(work_id, card_id)）。
#:
#: 停止路径要用它做**信息判定**（不是时间判定）：在流里 ⇒ 停止信号立刻让流收口
#: （已产出的字保住），不需要 cancel；不在流里（卡在工具/装配）⇒ 信号没人等，
#: 必须直接 cancel，否则回合永不收口、互斥体永不释放（实测 card_031 卡 9 分钟）。
_IN_STREAM: dict[tuple[str, str], int] = {}


def in_stream(work_id: str, card_id: str) -> bool:
    """该卡此刻是否正在模型流里（停止路径的判据）。"""
    return _IN_STREAM.get((str(work_id), str(card_id)), 0) > 0


async def _stop_raced(agen: Any, stop_ev: Optional[asyncio.Event],
                      stream_key: Optional[tuple[str, str]] = None):
    """把「下一个 chunk」和「停止信号」一起等，谁先到算谁（无墙钟）。

    [取消墙钟 2026-10-23 · 用户令「整个机制全部通过信息反馈」]
    原来停止只在**帧边界**生效：模型一个字都不吐时边界永远不来，只能靠
    `_hard_cancel_after` 的 1.5 秒定时去猜「软停是不是没生效」。那是墙钟。
    现在停止本身是可 await 的信号 ⇒ 停止先到就直接收口（**已产出的字保留**，
    走调用方同一 `_stopped` 收口路径），不需要任何定时兜底。

    ``stop_ev`` 为 None（没有在途回合/非卡内调用）⇒ 退化为原样透传。
    """
    if stream_key is not None:
        _IN_STREAM[stream_key] = _IN_STREAM.get(stream_key, 0) + 1
    it = agen.__aiter__()
    nxt: Optional[asyncio.Future] = None
    try:
      while True:
        if nxt is None:
            nxt = asyncio.ensure_future(it.__anext__())
        if stop_ev is None:
            try:
                yield nxt.result() if nxt.done() else await nxt
            except StopAsyncIteration:
                return
            nxt = None
            continue
        stopper = asyncio.ensure_future(stop_ev.wait())
        try:
            done, _pending = await asyncio.wait(
                {nxt, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not stopper.done():
                stopper.cancel()
        if nxt in done:
            try:
                chunk = nxt.result()
            except StopAsyncIteration:
                return
            nxt = None
            yield chunk
            continue
        # 停止信号先到：取消挂起的取帧，交还调用方（它会把已产出的字收口）
        nxt.cancel()
        nxt = None
        return
    finally:
        if stream_key is not None:
            _left = _IN_STREAM.get(stream_key, 0) - 1
            if _left > 0:
                _IN_STREAM[stream_key] = _left
            else:
                _IN_STREAM.pop(stream_key, None)


def _parse_text_tool_calls(text: str) -> list[dict[str, Any]]:
    """从正文里解析 `<tool_call>{"name":..,"arguments":..}</tool_call>` 块。

    [2026-10-29] 纯兼容层：只把**文本形态的调用**还原成结构化 tool_calls，
    不改变任何模型行为。名字/参数原样取，不做猜测性修补。
    """
    import json as _json
    import re as _re

    out: list[dict[str, Any]] = []
    for m in _re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, _re.S):
        try:
            obj = _json.loads(m.group(1))
        except Exception:  # noqa: BLE001 不是合法 JSON 就当正文
            continue
        if not isinstance(obj, dict):
            continue
        name = str(obj.get("name") or "").strip()
        if not name:
            continue
        args = obj.get("arguments")
        out.append({"name": name,
                    "arguments": args if isinstance(args, dict) else {}})
    return out


async def call_main(
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    model_name: Optional[str] = None,
    turn_tail: Optional[list[dict[str, Any]]] = None,
    turn_once: Optional[list[dict[str, Any]]] = None,
    fallbacks: Optional[list[str]] = None,
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

    # [取消墙钟 2026-10-23 · 用户令「整个机制全部通过信息反馈」]
    # 停止**不再**靠「等下一帧边界 + 1.5 秒硬取消兜底」：那是用钟去猜
    # 「软停有没有生效」。改成把停止做成信号，和「下一个 chunk」一起 await，
    # 谁先到算谁 —— 停止立即生效、已产出的字照旧保住（走下面同一 `_stopped` 收口）。
    # [停止失效修复 2026-10-23] 必须 stop_event_for（没有就建）：改前用 stop_event
    # （只取不建），而事件只在点停止时才创建 ⇒ 流启动时恒为 None ⇒ 不与信号赛跑
    # ⇒ 点停止后流照跑到底（用户实测「根本停不下来」）。
    _stop_ev = _ctl.stop_event_for(ctx[0], ctx[1]) if ctx else None
    _stream_key = (str(ctx[0]), str(ctx[1])) if ctx else None

    _stream = _stop_raced(
            gw.stream_call(
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
                # [层内兜底 2026-10-21 · 用户令] 首选重试耗尽就换**同一层**的下一个；
                # 层内全挂才如实失败，不跨层、不落到全局兜底。
                fallbacks=fallbacks or None,
            ),
            _stop_ev, _stream_key)
    try:
        async for ev in _stream:
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
    finally:
        # 消费者 break 出循环时生成器不会自动关闭 ⇒ 显式 aclose，
        # 否则 _stop_raced 的 finally 不跑、在流登记泄漏（停止判据永远为真）。
        await _stream.aclose()
    if _stop_ev is not None and _stop_ev.is_set() and not result:
        # 停止信号先到（流还没吐终帧）⇒ 与「帧边界停止」同一收口路径
        _stopped = True
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
    # [2026-10-29 文本工具调用兼容] 有些模型（实弹：deepseek-v4-1-flash-economy）
    # 不用结构化 `tool_calls`，而是把调用写成正文：
    #     <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    # 网关只认 delta.tool_calls ⇒ 这些调用**一个都不执行**，模型却以为改了，
    # 下一轮又挑同样问题（实弹里因此跑到第 23 版）。
    # 这里做兼容：结构化 tool_calls 为空时，把正文里的 tool_call 块解析出来，
    # 只接受**本回合确实给了的工具名**（不在 tools 里的当正文，不当调用）。
    if not (result.get("tool_calls") or []):
        parsed = _parse_text_tool_calls(str(result.get("content") or ""))
        allowed = {str(((t or {}).get("function") or t).get("name") or "")
                   for t in (tools or [])}
        parsed = [c for c in parsed if not allowed or c["name"] in allowed]
        if parsed:
            result["tool_calls"] = parsed
    if is_mock():
        model_used = "mock"
    else:
        # [层内兜底 2026-10-21] 实际所用由网关回报（切过人时 ≠ 请求指定的那个）。
        entry = (result.get("model_used") if isinstance(result, dict) else None) or model_name
        model_used = (gw.config.get("models") or {}).get(entry, {}).get("model", entry)
    return result, model_used
