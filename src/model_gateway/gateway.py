"""统一模型调用入口，封装重试、超时、降级、Token 统计。

离线可跑：设置环境变量 MOCK_LLM=1 时使用 MockGateway（不调用真实 LLM）。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Optional

from ..infra.errors import ErrorCode, ModelError
from ..infra.logging import get_logger
from .stats import GatewayStats

logger = get_logger(__name__)


# 重试分级：按错误类别限定最大尝试次数（与 model 配置取 min 钳制）。
# 依据：永久性错误重试无意义；临时性错误有限重试；超时每次都烧满
# timeout 秒，必须最快放弃。凡代码能判的不交给模型判。
RETRY_CLASS_CAPS = {
    "auth": 1,          # 永久性：403/401 零重试，首败直切 fallback
    "timeout": 2,       # 每次失败已烧满 timeout 秒（讯飞 180s），最多 2 次
    "503": 5,           # 临时过载：有限重试
    "rate_limit": 5,
    "connection": 3,
    "error": 3,         # 未知杂类：保守限 3
}
INCOMPLETE_MARKER_CAP = 3      # 完成标记缺失（P-3）：最多 3 次全额调用
RETRY_TOTAL_BUDGET_SECONDS = 300   # 单次 call 重试总时长熔断（含调用耗时）
RETRY_BACKOFF_MAX_SECONDS = 30     # 指数退避单次 sleep 上限


class ModelGateway:
    """统一模型调用入口，封装重试、超时、降级、Token统计。"""

    def __init__(self, config: dict[str, Any], stats: Optional[GatewayStats] = None):
        self.config = config
        self.stats = stats or GatewayStats()

    async def call(
        self,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.5,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        skip_completion_marker: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_status: Any = None,
        stream: bool = False,
        on_chunk: Any = None,
        prior_messages: list[dict[str, Any]] | None = None,
        turn_tail: list[dict[str, Any]] | None = None,
        turn_once: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """调用模型，返回 {content, tool_calls, truncated_tool_call, finish_reason, ...}。

        ``skip_completion_marker``: True 时跳过完成标记检测（适用于 JSON 输出场景）。

        ``tools``: OpenAI function calling 工具列表。传入时模型可在回复中
        发起 tool_calls（如 write_file），由调用方（orchestrator）执行。

        截断处理：当 tool_call.arguments 因 max_tokens 被截断导致 JSON 解析失败时，
        返回 truncated_tool_call（含原始被截断的 arguments 字符串）。
        """
        model_config = self.config["models"][model_name]
        provider = model_config["provider"]
        model = model_config["model"]
        api_key = self._get_api_key(model_config.get("api_key_env"))

        retry_config = model_config.get("retry", {"max_attempts": 3, "backoff_seconds": 2})
        max_attempts = int(retry_config.get("max_attempts", 3))
        backoff = int(retry_config.get("backoff_seconds", 2))

        # 重试分级配置（模型网关重试分级施工）：读一次存局部，避免每轮重复解析
        retry_grading_cfg = self.config.get("retry_grading", {}) or {}
        rg_enabled = bool(retry_grading_cfg.get("enabled", True))
        rg_total_budget = int(retry_grading_cfg.get("total_budget_seconds", RETRY_TOTAL_BUDGET_SECONDS))
        rg_class_caps = dict(RETRY_CLASS_CAPS)
        for _k, _v in (retry_grading_cfg.get("class_caps") or {}).items():
            try:
                rg_class_caps[_k] = int(_v)
            except (TypeError, ValueError):
                pass

        def _emit(phase, **kw):
            if not on_status:
                return
            ev = {"phase": phase, "model": model_name, "attempt": attempt + 1,
                  "max_attempts": max_attempts}
            ev.update(kw)
            try:
                on_status(ev)
            except Exception:  # noqa: BLE001
                pass

        loop_start = time.monotonic()
        for attempt in range(max_attempts):
            try:
                if attempt == 0:
                    _emit("start")
                else:
                    _emit("retry", reason="unknown")
                start = time.monotonic()
                # 延迟导入，避免离线（无 litellm/无 key）环境下导入失败
                litellm = __import__("litellm")
                # [推演agent有意偏离ZhuGe原版] 历史消息置于当前提问之前；
                # 原版将prior_messages追加在后（服务于工具续写场景），对话场景会答非所问
                messages = [{"role": "system", "content": system_prompt}]
                if prior_messages:
                    messages.extend(prior_messages)
                messages.append({"role": "user", "content": user_prompt})
                # [判断协议改注入 2026-10-14] 「仅本次」消息（判断协议投影）——
                # 追加在用户消息之后、**不进轮槽**；默认 None ⇒ 与改造前逐字一致。
                if turn_once:
                    messages.extend(turn_once)
                # [当前轮注入消息事件 2026-10-14] 本轮尾部消息（注入事件投影）——
                # 追加在**用户消息之后**、不替代它。默认 None ⇒ 与改造前逐字一致。
                if turn_tail:
                    messages.extend(turn_tail)
                kwargs = {
                    "model": f"{provider}/{model}",
                    "messages": messages,
                    "temperature": temperature,
                    "api_key": api_key,
                }
                # [拆闸裁定 2026-10-02] max_tokens/timeout 仅在显式设置时传入；
                # None = 不设，模型按端点自然行为返回（禁止任何输出限制与墙钟）
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                if timeout is not None:
                    kwargs["timeout"] = timeout
                # 支持自定义 base_url（如讯飞星辰等 OpenAI 兼容提供商）
                if model_config.get("base_url"):
                    kwargs["api_base"] = model_config["base_url"]
                # 提供商私有参数透传（如 DeepSeek flash 关思考 thinking:disabled，
                # 不关则草稿吃光 max_tokens 导致正文为空）
                if model_config.get("extra_body"):
                    kwargs["extra_body"] = model_config["extra_body"]
                # [席位3 opencode-go 端点 2026-10-06] opencode.go 硬要求 x-opencode-session 头
                # （cordis.patch.yml:28-30 MissingSessionID 拒单）；透传自定义请求头
                if model_config.get("extra_headers"):
                    kwargs["extra_headers"] = model_config["extra_headers"]
                # function calling 工具（OpenAI 标准协议）
                if tools:
                    kwargs["tools"] = [
                        {"type": "function", "function": t} for t in tools
                    ]

                # stage_FE4：流式模式——逐 chunk 转发并累加为完整 content
                if stream and on_chunk:
                    kwargs["stream"] = True
                    full_parts: list[str] = []
                    response = await litellm.acompletion(**kwargs)
                    async for chunk in response:
                        try:
                            delta = chunk.choices[0].delta.content or ""
                        except Exception:  # noqa: BLE001
                            delta = ""
                        if delta:
                            full_parts.append(delta)
                            try:
                                on_chunk(delta)
                            except Exception:  # noqa: BLE001
                                pass
                    content = "".join(full_parts)
                    latency = int((time.monotonic() - start) * 1000)
                    message = chunk.choices[0].message if "chunk" in dir() else None
                    # 兜底：防止 None 导致下游 Worker 崩溃
                    content = content or ""
                else:
                    response = await litellm.acompletion(**kwargs)
                    latency = int((time.monotonic() - start) * 1000)
                    message = response.choices[0].message
                    content = message.content
                    # DeepSeek V4 Pro 等推理模型返回 reasoning_content 而非 content
                    if not content:
                        reasoning = getattr(message, "reasoning_content", None)
                        if reasoning:
                            content = reasoning
                            logger.info("model_reasoning_fallback", model=model_name)
                    # 兜底：防止 None 导致下游 Worker 崩溃
                    content = content or ""

                # 解析 tool_calls（function calling）
                tool_calls = []
                # 截断的 tool_call：当 arguments JSON 被 max_tokens 砍断导致解析失败时，
                # 保留原始数据（function name + 被截断的 arguments 字符串）供续写器抢救
                truncated_tool_call = None
                finish_reason = ""
                try:
                    finish_reason = response.choices[0].finish_reason or ""
                except Exception:  # noqa: BLE001
                    pass
                raw_tool_calls = getattr(message, "tool_calls", None)
                if raw_tool_calls:
                    import json as _json
                    for tc in raw_tool_calls:
                        try:
                            fn = tc.function
                            args = _json.loads(fn.arguments) if fn.arguments else {}
                            tool_calls.append({
                                "name": fn.name,
                                "arguments": args,
                            })
                        except Exception:  # noqa: BLE001
                            # JSON 解析失败 → 大概率是 max_tokens 截断
                            # 保留原始数据（function name + 被截断的 arguments 字符串）供续写
                            fn_name = ""
                            raw_args = ""
                            try:
                                fn_name = tc.function.name or ""
                                raw_args = tc.function.arguments or ""
                            except Exception:  # noqa: BLE001
                                pass
                            truncated_tool_call = {
                                "name": fn_name,
                                "raw_arguments": raw_args,
                                "finish_reason": finish_reason,
                            }
                            logger.warning(
                                "tool_call_truncated",
                                model=model_name,
                                finish_reason=finish_reason,
                                raw_args_len=len(raw_args),
                            )
                if tool_calls:
                    logger.info(
                        "tool_calls_received",
                        model=model_name,
                        count=len(tool_calls),
                        names=[tc["name"] for tc in tool_calls],
                    )

                tokens = response.usage.total_tokens if response.usage else 0

                # 完成标记检测（讯飞等不稳定模型：响应拿到了但不一定完整）
                # 关联文档读取需求 v2.0：tool_calls 响应（finish_reason=tool_calls）不以正文
                # 【已完成】标记结尾，属正常完成，跳过标记检测；否则每次读文件都会误判
                # "不完整"而白烧 2~3 次重试（实测复现：tool_calls_received→incomplete→retry 循环）。
                completion_marker = model_config.get("completion_marker")
                if (
                    completion_marker
                    and not skip_completion_marker
                    and not tool_calls  # 关联文档读取需求 v2.0：tool_call 响应跳过【已完成】检测
                    and completion_marker not in (content or "")
                ):
                    logger.warning(
                        "model_incomplete_response",
                        model=model_name,
                        attempt=attempt + 1,
                        marker=completion_marker,
                        content_len=len(content or ""),
                    )
                    _emit("incomplete", reason="completion_marker_missing",
                          marker=completion_marker)
                    _incomplete_cap = max_attempts if not rg_enabled else min(
                        max_attempts, INCOMPLETE_MARKER_CAP
                    )
                    if attempt + 1 < _incomplete_cap:
                        await asyncio.sleep(backoff)
                        continue
                    # 耗尽重试次数仍然不完整，记录并返回（不丢数据，由下游决定）
                    logger.error(
                        "model_incomplete_exhausted",
                        model=model_name,
                        attempts=max_attempts,
                    )

                self.stats.record(model_name, tokens, latency, True)
                logger.info(
                    "model_call_success",
                    model=model_name,
                    tokens=tokens,
                    latency_ms=latency,
                )
                _emit("success")
                # N1：纯文本 reply 被 max_tokens 截断（无 tool_call）时，
                # 透传部分 reply，确保"输出不可丢"（不依赖 write_file 工具调用）。
                #
                # 触发条件（满足其一即视为被截断）：
                # 1. 模型自报 finish_reason=="length"（标准信号，但讯飞等国产模型不可靠）
                # 2. 启发式：reply 长度 ≥ max_tokens 的 95% 且无【已完成】标记
                #    （行为驱动判断，不依赖模型自报；覆盖讯飞 finish_reason 异常的情况）
                truncated_reply: Optional[str] = None
                completion_marker = "已完成"
                has_marker = (
                    content is not None
                    and completion_marker in content[-200:]
                )
                near_token_limit = False
                try:
                    if max_tokens and content:
                        # 粗估：1 token ≈ 1.5 个中文字符 / 4 个英文字符，取偏保守的 1.8
                        # reply 字符数接近 max_tokens 的 95% 即认为可能被截断
                        near_token_limit = len(content) >= max_tokens * 1.8 * 0.95
                except Exception:  # noqa: BLE001
                    pass
                truncated_by_signal = (
                    finish_reason == "length"
                    and not tool_calls
                    and not truncated_tool_call
                    and content
                )
                truncated_by_heuristic = (
                    not tool_calls
                    and not truncated_tool_call
                    and content
                    and near_token_limit
                    and not has_marker
                )
                if truncated_by_signal or truncated_by_heuristic:
                    truncated_reply = content
                    logger.info(
                        "reply_truncated",
                        model=model_name,
                        finish_reason=finish_reason,
                        content_len=len(content),
                        trigger="signal" if truncated_by_signal else "heuristic",
                    )
                return {
                    "content": content,
                    "tool_calls": tool_calls,
                    "truncated_tool_call": truncated_tool_call,
                    "truncated_reply": truncated_reply,
                    "finish_reason": finish_reason,
                    "tokens_used": tokens,
                    "latency_ms": latency,
                }

            except Exception as e:  # noqa: BLE001
                # 检测 litellm 解析截断 tool_call 抛的 APIError
                # （长文档超 max_tokens，tool_call.arguments JSON 被截断，
                #  litellm 在 _handle_invalid_parallel_tool_calls 阶段就抛异常，
                #  我们的解析代码根本拿不到响应对象——只能在 except 里抢救）
                truncated = self._extract_truncated_from_exception(e)
                if truncated:
                    # 不重试——重试也是同样截断（讯飞会稳定复现截断）。
                    # 直接返回截断数据给 orchestrator 续写
                    logger.info(
                        "tool_call_truncated_from_exception",
                        model=model_name,
                        finish_reason=truncated.get("finish_reason", ""),
                        raw_args_len=len(truncated.get("raw_arguments", "")),
                    )
                    self.stats.record(model_name, 0, 0, True)
                    return {
                        "content": "",
                        "tool_calls": [],
                        "truncated_tool_call": truncated,
                        "finish_reason": truncated.get("finish_reason", "length"),
                        "tokens_used": 0,
                        "latency_ms": 0,
                    }
                import traceback
                reason = self._classify_error(e)
                logger.warning(
                    "model_call_retry",
                    model_name=model_name,
                    attempt=attempt + 1,
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
                _emit("retry", reason=reason)

                # 4.3 重试分级：按错误类别钳制上限 + 总时长熔断
                elapsed = time.monotonic() - loop_start
                if rg_enabled:
                    effective_cap = min(max_attempts, rg_class_caps.get(reason, max_attempts))
                    should_retry = (
                        attempt + 1 < effective_cap
                        and elapsed < rg_total_budget
                        and reason != "auth"
                    )
                else:
                    # 回退旧一刀切行为：满 max_attempts 才放弃
                    should_retry = attempt + 1 < max_attempts

                if should_retry:
                    sleep_s = (
                        min(backoff * (2 ** attempt), RETRY_BACKOFF_MAX_SECONDS)
                        if rg_enabled else backoff
                    )
                    await asyncio.sleep(sleep_s)
                    continue

                # 提前耗尽（分级/熔断）→ 与"自然耗尽"汇入同一 fallback 出口，不复制代码
                logger.warning(
                    "retry_grading_giveup",
                    model=model_name,
                    reason=reason,
                    attempt=attempt + 1,
                    elapsed_seconds=int(elapsed),
                )
                fallback = self.config["models"].get("fallback")
                if fallback and model_name != "fallback":
                    logger.warning("model_fallback", original=model_name, fallback="fallback")
                    _emit("fallback", from_model=model_name, to_model="fallback")
                    return await self.call(
                        "fallback", system_prompt, user_prompt, temperature,
                        max_tokens, timeout,
                        skip_completion_marker=skip_completion_marker,
                        tools=tools,
                        prior_messages=prior_messages,
                        turn_tail=turn_tail,
                        turn_once=turn_once,
                    )
                self.stats.record(model_name, 0, 0, False)
                raise ModelError(
                    ErrorCode.MODEL_CALL_FAILED,
                    f"模型 {model_name} 重试 {max_attempts} 次后仍失败: {e}",
                )

    @staticmethod
    def _classify_error(e: Exception) -> str:
        """stage_FE3：把异常归类成简短 reason，便于前端状态条展示。"""
        msg = str(e).lower()
        if "503" in msg or "service unavailable" in msg or "overload" in msg:
            return "503"
        if "timeout" in msg or "timed out" in msg:
            return "timeout"
        if "429" in msg or "rate" in msg:
            return "rate_limit"
        if "401" in msg or "403" in msg or "api key" in msg \
                or "unauthorized" in msg or "forbidden" in msg:
            return "auth"
        if "connection" in msg:
            return "connection"
        return "error"

    def _extract_truncated_from_exception(self, e: Exception) -> Optional[dict]:
        """从 litellm 抛的异常里抢救被截断的 tool_call 数据。

        litellm 在解析截断的 tool_call.function.arguments 时抛 APIError，
        异常消息里包含完整的 response_object（含被截断的原始 arguments 字符串）。
        我们从异常字符串里提取 path 和部分 content。

        触发的异常特征（litellm 内部）：
        - _handle_invalid_parallel_tool_calls 调用 json.loads(tool_call.function.arguments)
        - JSONDecodeError: Unterminated string
        - 异常消息含 received_args={'response_object': {...'tool_calls':[{'function':{'arguments':'...'}}]...}}
        """
        err_str = str(e)
        # 特征 1：必须是 litellm 解析 tool_call 失败的异常
        if "_handle_invalid_parallel_tool_calls" not in err_str and "Unterminated string" not in err_str:
            return None
        # 特征 2：必须含 response_object（litellm 把原始响应塞进异常消息）
        if "received_args" not in err_str and "'tool_calls'" not in err_str:
            return None

        # 从异常消息里提取被截断的 arguments 字符串
        # 异常消息格式：...'arguments': '{"path":"docs/x.md","content":"# 标题...'...
        # 用非贪婪匹配拿 arguments 的值（Python repr 格式，内部转义）
        # 匹配 'arguments': '...' 到下一个字段或行尾
        match = re.search(
            r"'arguments':\s*'(.*?)',\s*'(?:id|type|name|index|logprobs)':",
            err_str,
            re.DOTALL,
        )
        raw_args_repr = ""
        if match:
            raw_args_repr = match.group(1)
        else:
            # 兜底：匹配 'arguments': ' 之后的内容
            # repr 格式里，字符串值的结束标志是 ' 后面跟 , 或 }
            # 被截断时 arguments 是最后一个字段，所以匹配到 '}}]}}]}} 这种闭合序列
            match2 = re.search(
                r"'arguments':\s*'(.*?)'(?:\s*}|\s*,)",
                err_str,
                re.DOTALL,
            )
            if match2:
                raw_args_repr = match2.group(1)
            else:
                # 最后兜底：取 'arguments': ' 之后到第一个 '}} 序列
                match3 = re.search(r"'arguments':\s*'(.+?)'\}", err_str, re.DOTALL)
                if match3:
                    raw_args_repr = match3.group(1)

        if not raw_args_repr:
            return None

        # 异常消息里的 arguments 是 Python repr 格式（外层多了一层转义），
        # 需要还原一层：\\\" → \"，\\\\ → \\ 等
        # 这样得到的是模型原始输出的 JSON 片段（带 JSON 自己的转义）
        raw_args = raw_args_repr.replace("\\'", "'").replace('\\"', '"')

        # 推断 finish_reason
        finish_reason = "tool_calls"
        if "finish_reason" in err_str:
            fr_match = re.search(r"'finish_reason':\s*'(\w+)'", err_str)
            if fr_match:
                finish_reason = fr_match.group(1)

        # 提取 function name
        name = "write_file"
        name_match = re.search(r"'name':\s*'(\w+)'", err_str)
        if name_match:
            name = name_match.group(1)

        return {
            "name": name,
            "raw_arguments": raw_args,
            "finish_reason": finish_reason,
        }

    async def stream_call(
        self,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.5,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        on_status: Any = None,
        tools: list[dict[str, Any]] | None = None,
        on_reasoning: Any = None,
        prior_messages: list[dict[str, Any]] | None = None,
        turn_tail: list[dict[str, Any]] | None = None,
        turn_once: list[dict[str, Any]] | None = None,
    ):
        """stage_FE4：流式调用（async generator）。

        逐个 yield ``{"chunk_type": "reasoning"|"content", "chunk": text, "text": text}``，
        最后 yield 一个完整结果 dict（与 ``call`` 返回结构一致：含 content / tool_calls /
        truncated_reply 等）。完成标记检测与续写仍基于完整聚合文本（由 orchestrator 在
        收到最终结果后处理），保证【已完成】标记检测。

        [信息流重出 Task1] 双增量解包：思考增量（``delta.reasoning`` /
        ``delta.reasoning_content``）与正文增量（``delta.content``）分别 yield，
        旧消费方只认 ``chunk`` 键——该键始终保留，故零感知。
        **思考增量不计入 ``parts``**：聚合 content / tokens / finish 判定语义与改前逐字一致。
        ``on_reasoning`` 与 ``on_status`` 并行、可选、缺省 None 时零行为变化。
        """
        model_config = self.config["models"][model_name]
        provider = model_config["provider"]
        model = model_config["model"]
        api_key = self._get_api_key(model_config.get("api_key_env"))
        retry_config = model_config.get("retry", {"max_attempts": 3, "backoff_seconds": 2})
        max_attempts = int(retry_config.get("max_attempts", 3))
        backoff = int(retry_config.get("backoff_seconds", 2))

        for attempt in range(max_attempts):
            try:
                if attempt == 0:
                    if on_status: on_status({"phase": "start", "model": model_name, "attempt": 1, "max_attempts": max_attempts})
                else:
                    if on_status: on_status({"phase": "retry", "model": model_name, "attempt": attempt + 1, "max_attempts": max_attempts, "reason": "stream_error"})
                litellm = __import__("litellm")
                # [信息流重出 Task4] 与 call 同款消息序：历史在当前提问之前
                # （与 call.py:111-116 保持一致，否则对话场景答非所问）
                messages = [{"role": "system", "content": system_prompt}]
                if prior_messages:
                    messages.extend(prior_messages)
                messages.append({"role": "user", "content": user_prompt})
                # [判断协议改注入 2026-10-14] 「仅本次」消息（判断协议投影）——
                # 追加在用户消息之后、**不进轮槽**；默认 None ⇒ 逐字同旧。
                if turn_once:
                    messages.extend(turn_once)
                # [当前轮注入消息事件 2026-10-14] 本轮尾部消息（注入事件投影），
                # 追加在用户消息之后。默认 None ⇒ 与改造前逐字一致。
                if turn_tail:
                    messages.extend(turn_tail)
                kwargs = {
                    "model": f"{provider}/{model}",
                    "messages": messages,
                    "temperature": temperature,
                    "api_key": api_key,
                    "stream": True,
                }
                # [信息流重出 Task4] 工具透传：原 stream_call 收了 tools 形参却从不下发，
                # 导致切过去等于静默阉掉 function calling（工具循环整个失效）。此处补齐，
                # 写法与 call 路径同款（call.py:141-144）。
                if tools:
                    kwargs["tools"] = [
                        {"type": "function", "function": t} for t in tools
                    ]
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                if timeout is not None:
                    kwargs["timeout"] = timeout
                if model_config.get("base_url"):
                    kwargs["api_base"] = model_config["base_url"]
                # 提供商私有参数透传（与非流式路径同款）
                if model_config.get("extra_body"):
                    kwargs["extra_body"] = model_config["extra_body"]
                # [席位3 opencode-go 端点 2026-10-06] opencode.go 硬要求 x-opencode-session 头
                # （cordis.patch.yml:28-30 MissingSessionID 拒单）；透传自定义请求头
                if model_config.get("extra_headers"):
                    kwargs["extra_headers"] = model_config["extra_headers"]
                start = time.monotonic()
                response = await litellm.acompletion(**kwargs)
                parts: list[str] = []
                # [信息流重出 Task4] 流式工具调用累积：OpenAI 协议下 tool_calls 按
                # index 分片到达（arguments 是**片段**，须按序拼接），首片带 id/name。
                tool_acc: dict[int, dict[str, Any]] = {}
                finish_reason = ""
                async for chunk in response:
                    # [信息流重出 Task1] 双增量解包：思考与正文分走两个出口。
                    # 思考字段名各提供商不一（reasoning / reasoning_content），两路都取。
                    delta_obj = None
                    try:
                        delta_obj = chunk.choices[0].delta
                    except Exception:  # noqa: BLE001
                        delta_obj = None
                    try:
                        finish_reason = getattr(chunk.choices[0], "finish_reason", "") or finish_reason
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        content_delta = (delta_obj.content or "") if delta_obj is not None else ""
                    except Exception:  # noqa: BLE001
                        content_delta = ""
                    reasoning_delta = ""
                    if delta_obj is not None:
                        reasoning_delta = (
                            getattr(delta_obj, "reasoning", None)
                            or getattr(delta_obj, "reasoning_content", None)
                            or ""
                        )
                        if not isinstance(reasoning_delta, str):
                            reasoning_delta = ""   # 非字符串（如 dict）不误当正文
                    # 思考先于正文到达，故先 yield；单增量（只 reasoning 或只 content）不报错
                    if reasoning_delta:
                        if on_reasoning:
                            try:
                                on_reasoning(reasoning_delta)
                            except Exception:  # noqa: BLE001 回调异常不拖垮流
                                pass
                        yield {"chunk_type": "reasoning", "chunk": reasoning_delta, "text": reasoning_delta}
                    if content_delta:
                        parts.append(content_delta)
                        yield {"chunk_type": "content", "chunk": content_delta, "text": content_delta}
                    # [Task4] 工具调用分片累积（不进逐字流：那是给人看的字）
                    raw_tcs = getattr(delta_obj, "tool_calls", None) if delta_obj is not None else None
                    if raw_tcs:
                        try:
                            for tc in raw_tcs:
                                idx = getattr(tc, "index", None)
                                if idx is None:
                                    idx = 0
                                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                tid = getattr(tc, "id", None)
                                if tid:
                                    slot["id"] = tid
                                fn = getattr(tc, "function", None)
                                if fn is not None:
                                    nm = getattr(fn, "name", None)
                                    if nm:
                                        slot["name"] = nm
                                    ag = getattr(fn, "arguments", None)
                                    if ag:
                                        slot["arguments"] += ag
                        except Exception:  # noqa: BLE001 工具分片坏不致命
                            pass
                content = "".join(parts)
                latency = int((time.monotonic() - start) * 1000)
                tokens = len(content) // 4
                self.stats.record(model_name, tokens, latency, True)
                if on_status: on_status({"phase": "success", "model": model_name, "attempt": attempt + 1, "max_attempts": max_attempts})
                # [Task4] 工具调用汇总：按 index 还原；arguments 解析失败则交
                # truncated_tool_call（与 call 同契约，否则切到流式后工具参数半截就废）
                import json as _json
                tool_calls: list[dict[str, Any]] = []
                truncated_tool_call = None
                for idx in sorted(tool_acc):
                    slot = tool_acc[idx]
                    if not slot["name"]:
                        continue
                    raw_args = slot["arguments"]
                    try:
                        parsed = _json.loads(raw_args) if raw_args else {}
                        if not isinstance(parsed, dict):
                            raise ValueError("arguments 不是对象")
                        tool_calls.append({"name": slot["name"], "arguments": parsed})
                    except Exception:  # noqa: BLE001
                        truncated_tool_call = {
                            "name": slot["name"],
                            "raw_arguments": raw_args,
                            "finish_reason": finish_reason or "length",
                        }
                yield {
                    "content": content,
                    "tool_calls": tool_calls,
                    "truncated_tool_call": truncated_tool_call,
                    "truncated_reply": None,
                    "finish_reason": finish_reason or "stop",
                    "tokens_used": tokens,
                    "latency_ms": latency,
                }
                return
            except Exception as e:  # noqa: BLE001
                if attempt < max_attempts - 1:
                    await asyncio.sleep(backoff)
                else:
                    fb = self.config["models"].get("fallback")
                    if fb and model_name != "fallback":
                        if on_status: on_status({"phase": "fallback", "from_model": model_name, "to_model": "fallback"})
                        async for ev in self.stream_call("fallback", system_prompt, user_prompt, temperature, max_tokens, timeout, on_status, tools, on_reasoning, prior_messages, turn_tail, turn_once=turn_once):
                            yield ev
                        return
                    raise ModelError(ErrorCode.MODEL_CALL_FAILED, f"模型 {model_name} 流式重试 {max_attempts} 次后仍失败: {e}")

    def _get_api_key(self, env_var: Optional[str]) -> str:
        key = os.environ.get(env_var or "")
        if not key:
            raise ModelError(
                ErrorCode.MISSING_API_KEY,
                f"环境变量 {env_var} 未设置",
            )
        return key


class MockGateway:
    """离线 mock 网关：根据 system_prompt 内容返回确定性（echo 式）内容。

    使得整条流水线与 pytest 在无 API Key 环境下也能跑通，并支撑 QA。

    call 接口与 ModelGateway 完全一致。
    """

    def __init__(self, config: Optional[dict[str, Any]] = None, stats: Optional[GatewayStats] = None):
        self.config = config or {}
        self.stats = stats or GatewayStats()

    async def call(
        self,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.5,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        skip_completion_marker: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_status: Any = None,
        stream: bool = False,
        on_chunk: Any = None,
        prior_messages: list[dict[str, Any]] | None = None,
        turn_tail: list[dict[str, Any]] | None = None,
        turn_once: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if on_status:
            try:
                on_status({"phase": "start", "model": model_name, "attempt": 1, "max_attempts": 1})
                on_status({"phase": "success", "model": model_name, "attempt": 1, "max_attempts": 1})
            except Exception:  # noqa: BLE001
                pass
        content = self._fake_content(model_name, system_prompt, user_prompt)
        # stage_FE4：mock 也支持流式，逐段回调 on_chunk
        if stream and on_chunk:
            import re as _re
            segs = _re.findall(r".{1,8}", content)
            for seg in segs:
                if seg:
                    try:
                        on_chunk(seg)
                    except Exception:  # noqa: BLE001
                        pass
        tool_calls = []
        tokens = max(1, len(content) // 4)
        self.stats.record(model_name, tokens, 1, True)
        return {
            "content": content,
            "tool_calls": tool_calls,
            "truncated_tool_call": None,
            "finish_reason": "stop",
            "tokens_used": tokens,
            "latency_ms": 1,
        }

    async def stream_call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                           max_tokens=None, timeout=None, on_status=None, tools=None,
                           on_reasoning=None, prior_messages=None, turn_tail=None,
                           turn_once=None):
        """stage_FE4：mock 流式调用（async generator）。

        [信息流重出 Task1/Task4] 签名与真实网关对齐（含 on_reasoning / prior_messages），
        增量同样带 chunk_type，兼容旧 {"chunk": seg} 键。
        """
        if on_status:
            try:
                on_status({"phase": "start", "model": model_name, "attempt": 1, "max_attempts": 1})
                on_status({"phase": "success", "model": model_name, "attempt": 1, "max_attempts": 1})
            except Exception:  # noqa: BLE001
                pass
        content = self._fake_content(model_name, system_prompt, user_prompt)
        import re as _re
        for seg in _re.findall(r".{1,8}", content):
            if seg:
                yield {"chunk_type": "content", "chunk": seg, "text": seg}
        tool_calls = []
        yield {
            "content": content,
            "tool_calls": tool_calls,
            "truncated_tool_call": None,
            "truncated_reply": None,
            "finish_reason": "stop",
            "tokens_used": max(1, len(content) // 4),
            "latency_ms": 1,
        }

    def _fake_content(self, model_name: str, system_prompt: str, user_prompt: str) -> str:
        sp = system_prompt or ""
        return f"[mock:{model_name}] {user_prompt[:120]}"


def build_gateway(config: dict[str, Any]) -> ModelGateway | MockGateway:
    """根据环境变量 MOCK_LLM 选择真实网关或 Mock 网关。"""
    if os.environ.get("MOCK_LLM") == "1":
        return MockGateway(config)
    return ModelGateway(config)
