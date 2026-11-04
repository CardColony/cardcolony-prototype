# -*- coding: utf-8 -*-
"""统一模型调用入口（**兼容壳**）—— 模型层底座整体抄写 · 期 3。

背景（用户 2026-10-16 裁定「冲突 A 选甲」）：
    任务书 §三 期 3 要求 `ModelGateway` 删除，但红线 C3（调用方零改动）与判据
    「失败差集为空」要求它必须存活：
      - `src/api/settings_api.py`（非白名单）import `build_gateway`；
      - `simple_service.py:281,399`（红线「一行不动」）用 `gw.config` / `gw.stream_call`；
      - `tool_fold.py:112`（红线「一行不动」）用 `gw.call`；
      - 4 个既有测试（非白名单）import `ModelGateway`。
    ⇒ 用户裁定：**`gateway.py` 保留为薄模块，`ModelGateway` 名保留为新写的兼容壳，
    旧单体实现删除，内部委托新 runtime。**

本壳职责：
    - 保留 `.config`（编译后平铺 config，逐字不变）与 `call` / `stream_call` 的**签名与契约**；
    - 内部把请求组装成 `GenerateOptions`，走 `LlmRuntime.stream` + `BlockAssembler`；
    - 保留旧有的重试分级、on_status 事件、完成标记检测、截断抢救。
    - **[模型设置 2026-10-19 产品裁定] 无降级模型**：调用失败重试耗尽后如实上抛，不做 fallback 切换。

适配器路由（[2026-10-16 单适配器改造] 五家归一）：
    五家（opencode/deepseek、aliyun/qwen、zhipu/glm、minimax、nous/haiku）wire 协议同构，
    全部由 **`OpenAICompatAdapter` 一个适配器**承接；各厂差异（思考参数形态）进
    `llm_openai_compat/dialects.py` 方言表。注册键 = **models.yaml 的 providers 键（vendor）**，
    `GenerateOptions.provider` 即 vendor；`GenerateOptions.model` 是 wire 模型名。
    （旧 litellm 薄壳与按传输方式分的 llm_deepseek 包**均已退役删除**。）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, AsyncIterator, Optional

from ..infra.errors import ErrorCode, ModelError
from ..infra.logging import get_logger
from .llm.assembler import BlockAssembler
from .llm.errors import LlmError
from .llm.runtime import LlmAdapter, LlmRuntime, RuntimeStats
from .llm.types import (
    ErrorReason,
    GenerateOptions,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolSchema,
)
from .llm_openai_compat.adapter import (
    CompatAdapterOptions,
    CompatConnectionOptions,
    OpenAICompatAdapter,
)
from .llm_openai_compat.serialize import RequestDefaults
from .llm_anthropic.adapter import (
    AnthropicAdapterOptions,
    AnthropicConnectionOptions,
    AnthropicMessagesAdapter,
)
from .llm_anthropic.adapter import DEFAULT_MAX_TOKENS as DEFAULT_ANTHROPIC_MAX_TOKENS
from .mavis_lease import get_default_client

logger = get_logger(__name__)

#: mavis 路由（MiniMax Code agent 网关）的 vendor 键——与 models.yaml 的 provider 名对应。
#: 该路由走 Anthropic Messages 协议 + broker 租约鉴权，与 OpenAI 兼容路由平行。
MAVIS_VENDOR = "mavis"

# 重试分级：按错误类别限定最大尝试次数（与 model 配置取 min 钳制）。语义承自旧单体。
RETRY_CLASS_CAPS = {
    "auth": 1,
    "timeout": 2,
    "503": 5,
    "rate_limit": 5,
    "connection": 3,
    "error": 3,
}
INCOMPLETE_MARKER_CAP = 3
RETRY_TOTAL_BUDGET_SECONDS = 300
RETRY_BACKOFF_MAX_SECONDS = 30

#: 走原生 SSE 的 opencode 兼容端点 URL 前缀（承自旧补丁 NATIVE_SSE_URL_PREFIXES）。
_OPENCODE_URL_PREFIXES = ("https://opencode.ai/",)
_DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

_COMPLETION_MARKER = "已完成"


def _vendor_of(model_config: dict[str, Any]) -> str:
    """取模型所属供应商（models.yaml providers 键；编译层登记为 ``vendor``）。

    手搓 config（无 vendor 字段）回落到 ``provider``（协议名）——保持旧测试装置可用。
    """
    return str(model_config.get("vendor") or model_config.get("provider") or "")


def _echo_fill_kind(model_config: dict[str, Any]) -> str:
    """该端点「无思考可回传」时补什么 —— **判据只来自配置**（models.yaml 的
    ``reasoning_echo_fill``），**不看模型名**。

    三态：

      * ``"placeholder"`` —— 补**非空**占位（commandcode 的 ``reflection/beam:free``：
        字段必须存在且非空，实测空串照样 400）；
      * ``"empty"``       —— 补**空串**（DeepSeek 系官方 API 生产实测口径：字段必须
        存在，空串即可）；
      * ``""``（缺省）     —— **不补**（该端点不要求回传）。

    〔5715 · 模型名收口〕改前是「模型名含 deepseek / vendor ∈ {opencode,deepseek} /
    base_url 前缀」这种**名字启发式**——模型一换代或改名，判据就悄悄失效（要嘛
    不补字段→400，要嘛乱补）。现改为**逐档显式登记**：models.yaml 里每个需要的档
    写明 ``reasoning_echo_fill``，行为逐字不变，但**判据不再依赖模型名**。
    旧字段 ``echo_reasoning: true`` 仍被认（等价于 placeholder），兼容旧配置。
    """
    v = model_config.get("reasoning_echo_fill")
    if v is None:
        # 兼容旧配置：echo_reasoning: true ⇒ 非空占位
        return "placeholder" if model_config.get("echo_reasoning") else ""
    s = str(v).strip().lower()
    if s in ("placeholder", "true", "1"):
        return "placeholder"
    if s in ("empty", "false", "0"):
        return "empty"
    if s == "":
        return "empty"
    return ""


def reasoning_echo_fill(model_name: str, model_config: dict[str, Any]) -> Optional[str]:
    """该端点「无思考可回传」时，assistant 消息的 reasoning_content 该填什么。

    返回三态，调用方按返回值决定加不加字段：

      * ``None``  —— 这个端点不要求回传 ⇒ **别加字段**（既有行为逐字不变）；
      * ``""``    —— DeepSeek 系：字段必须存在，空串即可（官方 API 生产实测口径）；
      * 非空占位  —— 其他 ``echo_reasoning`` 端点（commandcode 的
        ``reflection/beam:free``）：字段必须存在**且非空**（实测空串照样 400
        "Missing required parameter: 'messages[N].reasoning_content'"）。

    [多轮思考回传 2026-10-23] 单一判据源：gateway 的注入消息（``_ds_norm``）与
    api 层本轮 assistant 消息（message_runner）都调它，避免两处各写一套。

    〔5715 · 模型名收口〕判据改读**配置显式标记**（``reasoning_echo_fill``），
    不再按模型名/vendor 猜——见 ``_echo_fill_kind``。
    """
    kind = _echo_fill_kind(model_config)
    if kind == "placeholder":
        return REASONING_PLACEHOLDER
    if kind == "empty":
        return ""
    return None


#: 注入的 assistant 消息（判断协议等）无模型思考时可补的占位思考。
#: [多轮思考回传 2026-10-23] 只对 ``echo_reasoning`` 登记的端点用——它们要求
#: reasoning_content **非空**（实测空串照样 400 "Missing required parameter"）。
#: 写明是系统注入，不冒充模型思考。
#: 公开名：api 层（message_runner 构造本轮 assistant 消息）要引同一个值，
#: 免得两层各写一份字面量日后漂移。
REASONING_PLACEHOLDER = "(系统注入的消息，无模型思考记录)"


def _needs_ds_norm(model_name: str, model_config: dict[str, Any]) -> bool:
    """是否要给注入的 assistant 消息补 reasoning_content。

    〔5715 · 模型名收口〕判据**只来自配置**（models.yaml 的 ``reasoning_echo_fill``
    显式标记，兼容旧字段 ``echo_reasoning``）——改前是「模型名含 deepseek / vendor
    ∈ {opencode,deepseek} / base_url 前缀」这种**名字启发式**：模型换代或改名，
    判据就悄悄失效（要嘛不补字段→400，要嘛乱补）。

    两类端点要求「**每一条** assistant 消息都带 reasoning_content」，缺一条就 400：
    DeepSeek 系（官方 API 生产实测：不补 ⇒ 400 "reasoning_content ... must be
    passed back"）与 commandcode 的 ``reflection/beam:free``（实测空串照样 400
    "This model needs the reasoning of every earlier assistant message"）。
    """
    return _echo_fill_kind(model_config) != ""


def _ds_norm(msgs: Optional[list[dict[str, Any]]],
             placeholder: str = "") -> Optional[list[dict[str, Any]]]:
    """[DeepSeek 思考回传 2026-10-15] 给无 reasoning_content 的 assistant 消息补占位。

    语义原样承自旧单体（生产实测：不补 → 400 "reasoning_content ... must be passed back"）。

    [多轮思考回传 2026-10-23] ``placeholder`` 可调：DeepSeek 系补**空串**（官方
    生产实测口径，逐字不变）；``echo_reasoning`` 登记的端点（beam）空串照样 400，
    必须补**非空**占位（实测：任意非空串均被接受）。
    """
    if not msgs:
        return msgs
    out = []
    for m in msgs:
        if m.get("role") == "assistant" and "reasoning_content" not in m:
            m = dict(m)
            m["reasoning_content"] = placeholder
        out.append(m)
    return out


class ModelGateway:
    """统一模型调用入口（兼容壳）。对外契约与旧单体逐字一致。"""

    def __init__(self, config: dict[str, Any], stats: Optional[RuntimeStats] = None) -> None:
        self.config = config
        self.stats = stats if stats is not None else RuntimeStats()
        # [并发调度 2026-10-19] 每个模型一把信号量：同时最多 N 个调用在跑，
        # 其余排队等（不失败、不丢活）。N 见 config.models.yaml 的 concurrency 段。
        # [双循环 2026-11-04] 按 (model, loop) 分槽：asyncio.Semaphore 首次
        # acquire 时绑定所在 loop——单键共享时，一个跑错 loop 的调用会把闸
        # 永久绑死到 B，A 侧此后全部 "bound to a different event loop"
        # （事故实证：post_work_message 整场跑在 B ⇒ cmd-ds-v41-flash 闸被
        # 绑 B ⇒ A 侧所有调用 ModelError）。分槽后流浪调用只锁自己那条 loop
        # 的闸；正常全 A 调用共享同一把，语义不变。
        self._sems: dict[tuple[str, Any], asyncio.Semaphore] = {}
        # [智能路由 2026-10-20] 每个模型的总容量，供 load_of() 算占用率
        self._sem_limits: dict[tuple[str, Any], int] = {}
        self._models: dict[str, dict[str, Any]] = {
            k: v for k, v in (config.get("models") or {}).items() if isinstance(v, dict)
        }
        self._by_vendor: dict[str, list[str]] = {}
        for name, mc in self._models.items():
            self._by_vendor.setdefault(_vendor_of(mc), []).append(name)
        self._runtime = self._build_runtime(config)

    @staticmethod
    def _sem_key(model_name: str) -> tuple[str, Any]:
        """闸的键＝(模型, 当前 loop)。无线程本地运行环（同步/线程上下文
        直接调 load_of 之类）⇒ loop=None 单列，语义仍收敛。"""
        try:
            return (model_name, asyncio.get_running_loop())
        except RuntimeError:  # noqa: BLE001
            return (model_name, None)

    def _sem_for(self, model_name: str) -> asyncio.Semaphore:
        """该模型的并发闸（懒建，进程内单例）。

        上限取 ``concurrency.per_model[model]``，缺省回 ``concurrency.default``（3）。
        **这是排队不是拒绝**：拿不到就 await，不抛错、不丢活。
        """
        key = self._sem_key(model_name)
        sem = self._sems.get(key)
        if sem is None:
            cfg = self.config.get("concurrency") or {}
            per = cfg.get("per_model") or {}
            try:
                n = int(per.get(model_name) or cfg.get("default") or 3)
            except (TypeError, ValueError):
                n = 3
            n = max(1, n)
            sem = asyncio.Semaphore(n)
            self._sems[key] = sem
            self._sem_limits[key] = n
        return sem

    def load_of(self, model_name: str) -> float:
        """该模型当前占用率（0＝空闲，1＝满）。没建过闸 ⇒ 0。"""
        key = self._sem_key(model_name)
        sem = self._sems.get(key)
        total = self._sem_limits.get(key) or 0
        if sem is None or total <= 0:
            return 0.0
        used = total - sem._value
        return max(0.0, min(1.0, used / total))

    def pick_from(self, models: list, mode: str = "spread") -> str:
        """从候选模型里挑**当前占用率最低**的一个（智能路由）。

        [2026-10-20 用户令] 不是哈希分散——哈希只保证"分散"，不保证"不撞"：
        某个端点已经满了，哈希选中的卡还是会排队，旁边空着的不去用。
        这里按实时余量选；并列时用 crc32 打破平局（同卡稳定）。
        全满 ⇒ 仍然返回一个（调用方排队，不失败）。
        """
        cands = [str(m) for m in (models or []) if m]
        if not cands:
            return ""
        # [主备 vs 分散 2026-10-20]
        #   primary：**顺序优先**——按列表顺序取第一个还有余量的（主模型满/挂了才用备用）
        #   spread ：**轮询**——负载并列时若用固定规则，并发会全挤一个端点，等于没分散
        if mode == "primary":
            for m in cands:
                if self.load_of(m) < 1.0:
                    return m
            return cands[0]      # 全满 ⇒ 第一个（排队）
        free = [m for m in cands if self.load_of(m) < 1.0]
        pool = free or cands
        self._rr = (getattr(self, "_rr", 0) + 1) % len(pool)
        return pool[self._rr]

    # ------------------------------------------------------------------
    # runtime / 适配器装配
    # ------------------------------------------------------------------
    def _build_runtime(self, config: dict[str, Any]) -> LlmRuntime:
        runtime = LlmRuntime(stats=self.stats)
        if os.environ.get("MOCK_LLM") == "1":
            # MockAdapter 注册在 **vendor 路由键**上（与真实适配器同一路由面）；
            # 罐头内容里的模型名取**配置键**（与旧 MockGateway 逐字一致）。
            key_by_wire = {str(mc.get("model") or k): k for k, mc in self._models.items()}
            runtime.register_adapter(list(self._by_vendor.keys()), MockAdapter(key_by_wire))
            return runtime
        # 五家共用一个适配器实例（各厂差异进 facts/dialects），注册键 = vendor。
        runtime.register_adapter(
            [v for v in self._by_vendor if v != MAVIS_VENDOR],
            OpenAICompatAdapter(CompatAdapterOptions(options=self._facts_for)),
        )
        # mavis 路由（MiniMax Code 网关）：Anthropic Messages 协议，broker 租约鉴权。
        if MAVIS_VENDOR in self._by_vendor:
            runtime.register_adapter(
                [MAVIS_VENDOR],
                AnthropicMessagesAdapter(
                    AnthropicAdapterOptions(options=self._anthropic_facts_for)
                ),
            )
        return runtime

    def _anthropic_facts_for(self, provider: str, model: str) -> AnthropicConnectionOptions:
        """``(provider, wire_model) -> mavis 连接事实``（含 broker 令牌提供者）。"""
        mc = self._lookup(provider, model)
        try:
            max_tokens = int(mc.get("max_tokens"))
        except (TypeError, ValueError):
            max_tokens = None
        return AnthropicConnectionOptions(
            base_url=str(mc.get("base_url") or ""),
            token_getter=get_default_client().get_token,
            max_tokens=max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS,
            extra_headers=mc.get("extra_headers") if isinstance(mc.get("extra_headers"), dict) else None,
        )

    def _facts_for(self, provider: str, model: str) -> CompatConnectionOptions:
        """``(vendor, wire_model) -> 连接事实``。facts 全部来自编译 config。"""
        mc = self._lookup(provider, model)
        vendor = provider or _vendor_of(mc)
        thinking = mc.get("thinking")
        default_effort = mc.get("default_effort")
        return CompatConnectionOptions(
            base_url=str(mc.get("base_url") or _DEFAULT_DEEPSEEK_BASE_URL),
            api_key=os.environ.get(str(mc.get("api_key_env") or ""), ""),
            vendor=vendor,
            defaults=RequestDefaults(
                vendor=vendor,
                thinking=thinking if isinstance(thinking, str) or thinking is None else str(thinking),
                default_effort=default_effort if isinstance(default_effort, str) else None,
            ),
            max_tokens=None,  # 拆闸铁律：不注入默认 max_tokens
            extra_headers=mc.get("extra_headers") if isinstance(mc.get("extra_headers"), dict) else None,
        )

    def _lookup(self, provider: str, model: str) -> dict[str, Any]:
        keys = self._by_vendor.get(provider) or []
        for key in keys:
            if str(self._models[key].get("model") or key) == model:
                return self._models[key]
        if keys:
            return self._models[keys[0]]
        return {}

    # ------------------------------------------------------------------
    # 请求组装
    # ------------------------------------------------------------------
    def _build_options(
        self,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[list[dict[str, Any]]],
        prior_messages: Optional[list[dict[str, Any]]],
        turn_tail: Optional[list[dict[str, Any]]],
        turn_once: Optional[list[dict[str, Any]]],
        *,
        stream: bool,
        reasoning_effort: Optional[str] = None,
    ) -> GenerateOptions:
        mc = self.config["models"][model_name]
        messages: list[dict[str, Any]] = []
        if prior_messages:
            messages.extend(prior_messages)
        # [工具轮去假 user 2026-10-21] **只有真的有用户发言才拼 user 消息**。
        # 工具轮的 prior 末尾是 role=tool（工具结果），模型据此直接续写即可——
        # 原来无条件拼一条 user，逼得调用方编「工具结果已返回。」塞进用户位，
        # 模型会把它当成"用户说的话"（实测每轮思考都在念它）。
        # dsh 与 WorkBuddy 均无此占位；协议也不要求末尾必须是 user。
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})
        if turn_once:
            messages.extend(turn_once)
        if turn_tail:
            messages.extend(turn_tail)
        schemas = None
        if tools:
            schemas = tuple(
                ToolSchema(
                    name=str(t.get("name", "")),
                    description=str(t.get("description", "")),
                    parameters=t.get("parameters") or {},
                )
                for t in tools
            )
        return GenerateOptions(
            provider=_vendor_of(mc),
            model=str(mc.get("model") or model_name),
            messages=tuple(messages),
            system=system_prompt,
            tools=schemas,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            reasoning_effort=reasoning_effort,
        )

    def _retry_params(self, model_name: str) -> tuple[int, int]:
        mc = self.config["models"][model_name]
        rc = mc.get("retry", {"max_attempts": 3, "backoff_seconds": 2})
        return int(rc.get("max_attempts", 3)), int(rc.get("backoff_seconds", 2))

    def _retry_grading(self) -> tuple[bool, int, dict[str, int]]:
        cfg = self.config.get("retry_grading", {}) or {}
        enabled = bool(cfg.get("enabled", True))
        budget = int(cfg.get("total_budget_seconds", RETRY_TOTAL_BUDGET_SECONDS))
        caps = dict(RETRY_CLASS_CAPS)
        for k, v in (cfg.get("class_caps") or {}).items():
            try:
                caps[k] = int(v)
            except (TypeError, ValueError):
                pass
        return enabled, budget, caps

    # ------------------------------------------------------------------
    # 结果装配
    # ------------------------------------------------------------------
    @staticmethod
    def _result_from(assembler: BlockAssembler, model_name: str, started: float,
                     tools: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        blocks = assembler.blocks()
        content = "".join(b.text for b in blocks if isinstance(b, TextBlock))
        tool_calls: list[dict[str, Any]] = []
        truncated_tool_call: Optional[dict[str, Any]] = None
        finish_reason = _legacy_finish(assembler.finish)
        # [单工具补名 2026-10-30 · 实测 glm-5.3-flash @ open.bigmodel.cn/coding]
        # 该端点的工具调用分片长这样：
        #   {"index":0,"type":"function","function":{"arguments":"{}"}}
        # **既无 id 也无 function.name**——工具名被写进正文（AntML 形态
        # `<tool_call><hash></tool_call>`）。原实现 `if not b.name: continue`
        # 把整个调用丢弃 ⇒ 端点报 finish_reason=tool_calls，我们拿到 tool_calls=[]，
        # 归拢第三步永远提交不了。
        # 机械判据：本轮只登记了一个工具 ⇒ 缺名的分片只可能是它（不是猜内容）。
        # 多工具轮次无法机械归属，保持原样丢弃（宁可如实空，不瞎配名字）。
        single_tool_name = ""
        if tools and len(tools) == 1:
            _t = tools[0] or {}
            _fn = _t.get("function") if isinstance(_t.get("function"), dict) else _t
            single_tool_name = str((_fn or {}).get("name") or "")
        for b in blocks:
            if not isinstance(b, ToolCallBlock):
                continue
            name = b.name or single_tool_name
            if not name:
                continue
            raw_args = b.arguments
            try:
                parsed = json.loads(raw_args) if raw_args else {}
                if not isinstance(parsed, dict):
                    raise ValueError("arguments 不是对象")
                tool_calls.append({"name": name, "arguments": parsed})
            except Exception:  # noqa: BLE001 JSON 截断 → 交续写器
                truncated_tool_call = {
                    "name": name,
                    "raw_arguments": raw_args,
                    "finish_reason": finish_reason or "length",
                }
        usage = assembler.usage
        tokens = (usage.input_tokens + usage.output_tokens) if usage is not None else len(content) // 4
        latency = int((time.monotonic() - started) * 1000)
        return {
            "content": content,
            "tool_calls": tool_calls,
            "truncated_tool_call": truncated_tool_call,
            "truncated_reply": None,
            "finish_reason": finish_reason or "stop",
            "tokens_used": tokens,
            "latency_ms": latency,
        }

    # ------------------------------------------------------------------
    # 非流式调用（旧契约）
    # ------------------------------------------------------------------
    async def call(
        self,
        model_name: str,
        *args: Any,
        fallbacks: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """调用模型；``fallbacks`` = **同层**兜底链（层内依次尝试，绝不跨层）。

        [层内兜底 2026-10-21 · 用户令] 「不是原来那套用一个模型兜所有底，而是按照
        层级策略，每一个层级内互相兜底」⇒ 首选重试耗尽就换同层下一个，
        层内全挂才如实上抛，不会落到 system.default_model 那种全局兜底。

        单模型的全部逻辑（重试/续写/统计）原样留在 ``_call_once``，本函数只加一层
        「换人」循环——这样两条路径的行为差异只有「换哪个模型」。
        """
        chain = _dedup_chain(model_name, fallbacks)
        last: ModelError | None = None
        for idx, mname in enumerate(chain):
            try:
                res = await self._call_once(mname, *args, **kwargs)
                # [层内兜底] 把**实际成功的模型**带回去：切过人之后日志/界面
                # 必须记「真跑的是谁」，不能还写着首选（否则排查又被误导）。
                if isinstance(res, dict):
                    res.setdefault("model_used", mname)
                return res
            except ModelError as exc:
                last = exc
                if idx + 1 >= len(chain):
                    break
                logger.warning("[model] 层内兜底：%s 失败 ⇒ 换 %s（%s）",
                               mname, chain[idx + 1], str(exc)[:160])
        raise last if last is not None else ModelError(
            ErrorCode.MODEL_CALL_FAILED, f"模型 {model_name} 调用失败")

    async def _call_once(
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
        *,
        trace_id: Optional[str] = None,
        caller: str = "",
        work_id: str = "",
        card_id: str = "",
    ) -> dict[str, Any]:
        # [F22 · 2026-10-31 观测重建] 新增**可选** keyword-only 形参 trace_id/caller：
        # 只加不改——既有调用方零感知（不传＝自动生成 trace_id、caller 空）。
        # 每次真实模型调用（含重试的每次 attempt）落一条 model_call JSONL，
        # prompt/completion token 取流式 usage 真值（端点报了才有，否则 None 如实留空）。
        # [2026-10-31 · 二轮审计] 补 work_id/card_id（在途台账与 model_call 行的
        # 「卡」位）＋在途台账接线（call_begin/每帧 call_chunk/call_end）——
        # 「是不是停了，看状态就知道」的字面落实；台账纯事实，不参与任何裁决。
        mc = self.config["models"][model_name]
        from ..infra import observe as _obs
        _trace = trace_id or _obs.new_trace_id()
        _obs.call_begin(_trace, caller=caller, work_id=work_id, card_id=card_id,
                        seat=model_name, model=model_name)
        _fill = reasoning_echo_fill(model_name, mc)
        if _fill is not None:
            turn_tail = _ds_norm(turn_tail, _fill)
            turn_once = _ds_norm(turn_once, _fill)
        max_attempts, backoff = self._retry_params(model_name)
        rg_enabled, rg_budget, rg_caps = self._retry_grading()
        loop_start = time.monotonic()

        def _emit(phase: str, attempt: int, **kw: Any) -> None:
            if not on_status:
                return
            ev = {"phase": phase, "model": model_name, "attempt": attempt + 1, "max_attempts": max_attempts}
            ev.update(kw)
            try:
                on_status(ev)
            except Exception:  # noqa: BLE001
                pass

        for attempt in range(max_attempts):
            # [模型核验 2026-10-20] 打一行实际调用的模型名——排查"配了 A 实际跑 B"用。
            logger.info("[model] call model=%s attempt=%d", model_name, attempt)
            _emit("start" if attempt == 0 else "retry", attempt,
                  **({} if attempt == 0 else {"reason": "unknown"}))
            started = time.monotonic()
            try:
                options = self._build_options(
                    model_name, system_prompt, user_prompt, temperature, max_tokens,
                    tools, prior_messages, turn_tail, turn_once, stream=False,
                )
                assembler = BlockAssembler()
                emitted: list[str] = []
                async for chunk in _guarded_stream(self._sem_for(model_name), self._runtime.stream(options)):
                    _obs.call_chunk(_trace)
                    if chunk.type == "text-delta":
                        emitted.append(chunk.text)
                        if stream and on_chunk:
                            try:
                                on_chunk(chunk.text)
                            except Exception:  # noqa: BLE001
                                pass
                    assembler.push(chunk)
                failure = _failure_of(assembler)
                if failure is not None:
                    salvaged = _salvage_truncated(failure.message)
                    if salvaged is not None:
                        self.stats.record(model_name, 0, 0, True)
                        return {
                            "content": "", "tool_calls": [], "truncated_tool_call": salvaged,
                            "truncated_reply": None,
                            "finish_reason": salvaged.get("finish_reason", "length"),
                            "tokens_used": 0, "latency_ms": 0,
                        }
                    raise ModelCallFailure(failure.message, _classify(failure))

                result = self._result_from(assembler, model_name, started, tools)
                content = result["content"]
                # DeepSeek 等推理模型返回 reasoning_content 而非 content 的兜底
                if not content:
                    reasoning = "".join(
                        b.text for b in assembler.blocks() if isinstance(b, ReasoningBlock)
                    )
                    if reasoning:
                        content = reasoning
                        result["content"] = content
                        logger.info("model_reasoning_fallback", model=model_name)

                marker = mc.get("completion_marker")
                if (marker and not skip_completion_marker and not result["tool_calls"]
                        and marker not in content):
                    _emit("incomplete", attempt, reason="completion_marker_missing", marker=marker)
                    cap = max_attempts if not rg_enabled else min(max_attempts, INCOMPLETE_MARKER_CAP)
                    if attempt + 1 < cap:
                        await asyncio.sleep(backoff)
                        continue

                self.stats.record(model_name, result["tokens_used"], result["latency_ms"], True)
                _emit("success", attempt)
                result["truncated_reply"] = self._truncated_reply(result, max_tokens)
                # [F22] 因果链回执：trace/caller/usage 真值随 result 回传 + JSONL 落账
                usage = assembler.usage
                result["trace_id"] = _trace
                result["caller"] = caller
                result["usage_in"] = getattr(usage, "input_tokens", None) if usage else None
                result["usage_out"] = getattr(usage, "output_tokens", None) if usage else None
                _obs.log_model_call(
                    trace_id=_trace, caller=caller, model=model_name,
                    work_id=work_id, card_id=card_id, seat=model_name,
                    prompt_tokens=result["usage_in"], completion_tokens=result["usage_out"],
                    tokens_used=result["tokens_used"], latency_ms=result["latency_ms"],
                    finish_reason=str(result.get("finish_reason") or ""),
                    text_len=len(result.get("content") or ""),
                    tool_calls=len(result.get("tool_calls") or []),
                    reasoning_chars=sum(
                        len(b.text) for b in assembler.blocks()
                        if isinstance(b, ReasoningBlock)))
                _obs.call_end(_trace)
                return result

            except ModelCallFailure as exc:
                reason = exc.reason
                logger.warning("model_call_retry", model_name=model_name,
                               attempt=attempt + 1, error=exc.message)
                _emit("retry", attempt, reason=reason)
                if self._should_give_up(attempt, max_attempts, reason, rg_enabled, rg_budget, rg_caps, loop_start):
                    # [模型设置 2026-10-19 产品裁定] 无降级模型：重试耗尽即如实失败。
                    _obs.call_end(_trace)
                    raise ModelError(ErrorCode.MODEL_CALL_FAILED,
                                     f"模型 {model_name} 重试 {max_attempts} 次后仍失败: {exc.message}")
                await asyncio.sleep(
                    min(backoff * (2 ** attempt), RETRY_BACKOFF_MAX_SECONDS) if rg_enabled else backoff
                )
            except Exception as exc:  # noqa: BLE001 组装/序列化层异常
                reason = _classify_message(str(exc))
                _emit("retry", attempt, reason=reason)
                if self._should_give_up(attempt, max_attempts, reason, rg_enabled, rg_budget, rg_caps, loop_start):
                    # [模型设置 2026-10-19 产品裁定] 无降级模型：重试耗尽即如实失败。
                    _obs.call_end(_trace)
                    raise ModelError(ErrorCode.MODEL_CALL_FAILED,
                                     f"模型 {model_name} 重试 {max_attempts} 次后仍失败: {exc}")
                await asyncio.sleep(
                    min(backoff * (2 ** attempt), RETRY_BACKOFF_MAX_SECONDS) if rg_enabled else backoff
                )

        # [F22] 终败也落账（对账时"调用过但没成功"必须可见）
        _obs.log_model_call(trace_id=_trace, caller=caller, model=model_name,
                            work_id=work_id, card_id=card_id, seat=model_name,
                            latency_ms=int((time.monotonic() - loop_start) * 1000),
                            error=f"重试 {max_attempts} 次后仍失败")
        _obs.call_end(_trace)
        raise ModelError(ErrorCode.MODEL_CALL_FAILED, f"模型 {model_name} 重试 {max_attempts} 次后仍失败")

    @staticmethod
    def _truncated_reply(result: dict[str, Any], max_tokens: Optional[int]) -> Optional[str]:
        content = result.get("content") or ""
        if not content or result.get("tool_calls") or result.get("truncated_tool_call"):
            return None
        has_marker = _COMPLETION_MARKER in content[-200:]
        near = False
        try:
            if max_tokens:
                near = len(content) >= max_tokens * 1.8 * 0.95
        except Exception:  # noqa: BLE001
            pass
        if result.get("finish_reason") == "length" or (near and not has_marker):
            return content
        return None

    @staticmethod
    def _should_give_up(attempt, max_attempts, reason, rg_enabled, rg_budget, rg_caps, loop_start) -> bool:
        if not rg_enabled:
            return attempt + 1 >= max_attempts
        elapsed = time.monotonic() - loop_start
        cap = min(max_attempts, rg_caps.get(reason, max_attempts))
        return not (attempt + 1 < cap and elapsed < rg_budget and reason != "auth")

    # ------------------------------------------------------------------
    # 流式调用（旧契约）
    # ------------------------------------------------------------------
    async def stream_call(
        self,
        model_name: str,
        *args: Any,
        fallbacks: list[str] | None = None,
        work_id: str = "",
        card_id: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用；``fallbacks`` = **同层**兜底链。

        [层内兜底 2026-10-21] 与 ``call`` 同一语义，但多一条硬约束：
        **一旦已经吐过增量就不再换模型**——否则用户会看到半句话接另一个模型的
        开头，等于把同一轮输出两遍。只有「一个字都还没出就失败」才换下一个。
        """
        chain = _dedup_chain(model_name, fallbacks)
        last: ModelError | None = None
        for idx, mname in enumerate(chain):
            emitted = False
            try:
                async for ev in self._stream_once(mname, *args,
                                                  work_id=work_id, card_id=card_id,
                                                  **kwargs):
                    if "chunk_type" in ev:
                        emitted = True
                    elif isinstance(ev, dict):
                        ev.setdefault("model_used", mname)   # 终帧：实际使用的模型
                    yield ev
                return
            except ModelError as exc:
                last = exc
                if emitted or idx + 1 >= len(chain):
                    break
                logger.warning("[model] 层内兜底（流式）：%s 失败 ⇒ 换 %s（%s）",
                               mname, chain[idx + 1], str(exc)[:160])
        raise last if last is not None else ModelError(
            ErrorCode.MODEL_CALL_FAILED, f"模型 {model_name} 流式调用失败")

    async def _stream_once(
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
        reasoning_effort: str | None = None,
        *,
        on_chunk: Any = None,
        trace_id: Optional[str] = None,
        caller: str = "",
        work_id: str = "",
        card_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        # [F22 · 2026-10-31] 新增**可选** on_chunk/trace_id/caller（只加不改）：
        # on_chunk ＝ 每个 chunk 事件旁路（凝练 F8 静默判死的计数依据）；
        # trace/caller ＝ 因果链；每次真实调用落一条 model_call JSONL。
        mc = self.config["models"][model_name]
        from ..infra import observe as _obs
        _trace = trace_id or _obs.new_trace_id()
        _obs.call_begin(_trace, caller=caller, work_id=work_id, card_id=card_id,
                        seat=model_name, model=model_name)
        _fill = reasoning_echo_fill(model_name, mc)
        if _fill is not None:
            turn_tail = _ds_norm(turn_tail, _fill)
            turn_once = _ds_norm(turn_once, _fill)
        max_attempts, backoff = self._retry_params(model_name)

        def _emit(phase: str, attempt: int, **kw: Any) -> None:
            if not on_status:
                return
            ev = {"phase": phase, "model": model_name, "attempt": attempt + 1, "max_attempts": max_attempts}
            ev.update(kw)
            try:
                on_status(ev)
            except Exception:  # noqa: BLE001
                pass

        for attempt in range(max_attempts):
            _emit("start" if attempt == 0 else "retry", attempt,
                  **({} if attempt == 0 else {"reason": "stream_error"}))
            started = time.monotonic()
            options = self._build_options(
                model_name, system_prompt, user_prompt, temperature, max_tokens,
                tools, prior_messages, turn_tail, turn_once, stream=True,
                reasoning_effort=reasoning_effort,
            )
            assembler = BlockAssembler()
            try:
                async for chunk in _guarded_stream(self._sem_for(model_name), self._runtime.stream(options)):
                    _obs.call_chunk(_trace)
                    if on_chunk:
                        try:
                            on_chunk(chunk.text if hasattr(chunk, "text") else "")
                        except Exception:  # noqa: BLE001 旁路不伤主路
                            pass
                    if chunk.type == "reasoning-delta":
                        if on_reasoning:
                            try:
                                on_reasoning(chunk.text)
                            except Exception:  # noqa: BLE001
                                pass
                        yield {"chunk_type": "reasoning", "chunk": chunk.text, "text": chunk.text}
                    elif chunk.type == "text-delta":
                        yield {"chunk_type": "content", "chunk": chunk.text, "text": chunk.text}
                    assembler.push(chunk)
                failure = _failure_of(assembler)
                if failure is not None:
                    raise ModelCallFailure(failure.message, _classify(failure))
                result = self._result_from(assembler, model_name, started, tools)
                self.stats.record(model_name, result["tokens_used"], result["latency_ms"], True)
                _emit("success", attempt)
                # [F22] 因果链回执 + JSONL 落账（usage 真值随行）
                usage = assembler.usage
                result["trace_id"] = _trace
                result["caller"] = caller
                result["usage_in"] = getattr(usage, "input_tokens", None) if usage else None
                result["usage_out"] = getattr(usage, "output_tokens", None) if usage else None
                _obs.log_model_call(
                    trace_id=_trace, caller=caller, model=model_name,
                    work_id=work_id, card_id=card_id, seat=model_name,
                    prompt_tokens=result["usage_in"], completion_tokens=result["usage_out"],
                    tokens_used=result["tokens_used"], latency_ms=result["latency_ms"],
                    finish_reason=str(result.get("finish_reason") or ""),
                    text_len=len(result.get("content") or ""),
                    tool_calls=len(result.get("tool_calls") or []))
                yield result
                _obs.call_end(_trace)
                return
            except ModelCallFailure as exc:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(backoff)
                    continue
                # [模型设置 2026-10-19 产品裁定] 无降级模型：流式重试耗尽即如实失败。
                _obs.call_end(_trace)
                raise ModelError(
                    ErrorCode.MODEL_CALL_FAILED,
                    f"模型 {model_name} 流式重试 {max_attempts} 次后仍失败: {exc.message}",
                ) from None
            except Exception as exc:  # noqa: BLE001
                if attempt < max_attempts - 1:
                    await asyncio.sleep(backoff)
                    continue
                _obs.call_end(_trace)
                raise ModelError(
                    ErrorCode.MODEL_CALL_FAILED,
                    f"模型 {model_name} 流式重试 {max_attempts} 次后仍失败: {exc}",
                ) from exc


def _dedup_chain(primary: str, fallbacks: list[str] | None) -> list[str]:
    """``[首选] + 同层兜底``，去重去空、保持顺序。"""
    out = [str(primary)] if str(primary or "").strip() else []
    for m in (fallbacks or []):
        m = str(m or "").strip()
        if m and m not in out:
            out.append(m)
    return out


class ModelCallFailure(Exception):
    """一次调用失败（内部信号，驱动重试；[2026-10-19 裁定] 无降级）。"""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


# ============================================================
# [断流兜底 2026-10-19 · permexp]
#
# 背景：实测 4 张卡在**同一毫秒**（16:44:14）停止，最后事件都是 reasoning、
# 没有 done/error——流既没抛异常也没结束，协程永久挂在 await 上，静默 57 分钟。
# 上层（auto_driver）只在"回合收口"后才起下一轮 ⇒ 不收口 = 永不再动。
#
# ★ 判的是「**多久没有新 chunk**」，不是「总共跑了多久」：
#   正常推理每秒几十个 chunk（card_009 实测连续推理 40 分钟、11 万条事件），
#   10 分钟一个字都没有，只可能是连接挂起（用户裁定：至少 10 分钟）。
#   这与被否掉的"墙钟闸"是两回事。
#
# [2026-10-23 用户令：取消墙钟，改由信息反馈判定] 上面那段自我辩解站不住：
#   「10 分钟没有 chunk 就只可能是连接挂起」——本地模型排队/重思考阶段本来就可能
#   长时间不出字（用户实测单请求 12.5 分钟），它会把**正常的慢**判成挂起。
#   而真断连根本不用等：socket 会立刻抛异常（TRANSPORT），那是信息反馈。
#   ⇒ 值改 **None**（`asyncio.wait_for(timeout=None)` = 不设超时）。
#   ★ 这是一处**常量取值**的改动，不动本模块任何接口/签名（「禁改」指的是接口）。
#   代价如实写在这里：真挂死的连接会一直挂着，直到人手动停止——
#   要根治得做「按状态探活」（查端点进度/连接状态），不是再换个数字。
# ============================================================
STREAM_IDLE_TIMEOUT: Optional[float] = None


async def _stream_with_idle_timeout(agen: Any,
                                    idle: Optional[float] = STREAM_IDLE_TIMEOUT):
    """包一层流：相邻 chunk 间隔超过 ``idle`` 秒 ⇒ 判流挂起（抛 ModelCallFailure）。"""
    it = agen.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(it.__anext__(), timeout=idle)
        except asyncio.TimeoutError:
            raise ModelCallFailure(
                f"流静默超过 {idle:g}s（连接挂起，一个字都没收到）", "timeout"
            ) from None
        except StopAsyncIteration:
            return
        yield chunk


async def _guarded_stream(sem: asyncio.Semaphore, agen: Any):
    """并发闸 + 静默兜底：整个流期间占一个额度，流结束才释放。

    [并发调度 2026-10-19] 一个流占一个额度 ⇒ N 个并发上限就是 N 条流同时在跑，
    其余在 ``async with`` 上排队等（不失败、不丢活）。
    """
    async with sem:
        async for chunk in _stream_with_idle_timeout(agen):
            yield chunk


class MockAdapter(LlmAdapter):
    """离线 mock 适配器（旧 ``MockGateway`` 迁移）：产 ``StreamChunk``，罐头内容形态不变。"""

    def __init__(self, key_by_wire: Optional[dict[str, str]] = None) -> None:
        self._key_by_wire = key_by_wire or {}

    def provider_info(self, provider: str) -> dict[str, str]:
        return {"id": provider, "name": "Mock"}

    async def stream(self, options: GenerateOptions) -> AsyncIterator[Any]:
        from .llm.types import FinishChunk, StopReason, TextDelta

        user_prompt = ""
        for m in options.messages:
            if isinstance(m, dict) and m.get("role") == "user":
                user_prompt = str(m.get("content") or "")
        # 旧 MockGateway 用**配置键**作模型名（`f"[mock:{model_name}] ..."`），此处还原。
        label = self._key_by_wire.get(options.model, options.model)
        content = f"[mock:{label}] {user_prompt[:120]}"
        for seg in re.findall(r".{1,8}", content):
            if seg:
                yield TextDelta(index=0, text=seg)
        yield FinishChunk(reason=StopReason())


def _failure_of(assembler: BlockAssembler):
    reason = assembler.finish
    if isinstance(reason, ErrorReason):
        return reason.failure
    if getattr(reason, "kind", None) == "aborted":
        return reason.failure
    return None


def _legacy_finish(reason) -> str:
    kind = getattr(reason, "kind", "stop")
    if kind == "stop":
        return "stop"
    if kind == "tool-calls":
        return "tool_calls"
    if kind == "max-tokens":
        return "length"
    failure = getattr(reason, "failure", None)
    if failure is not None and failure.code:
        return str(failure.code).lower()
    return "stop"


def _classify(failure) -> str:
    code = str(getattr(failure, "code", "") or "")
    if code == "AUTH":
        return "auth"
    if code == "RATE_LIMIT":
        return "rate_limit"
    if code == "TIMEOUT":
        return "timeout"
    if code == "SERVER":
        return "503"
    if code == "TRANSPORT":
        return "connection"
    return _classify_message(str(getattr(failure, "message", "")))


def _classify_message(msg: str) -> str:
    """把错误信息归类成简短 reason（承自旧 `_classify_error`）。"""
    low = msg.lower()
    if "503" in low or "service unavailable" in low or "overload" in low:
        return "503"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "429" in low or "rate" in low:
        return "rate_limit"
    if "401" in low or "403" in low or "api key" in low or "unauthorized" in low or "forbidden" in low:
        return "auth"
    if "connection" in low:
        return "connection"
    return "error"


def _salvage_truncated(err_str: str) -> Optional[dict[str, Any]]:
    """从 litellm 解析截断 tool_call 的异常里抢救原始 arguments（承自旧单体）。

    旧单体在 ``except`` 里做；新底座把异常归一成终帧（message=str(exc)），故这里吃字符串。
    """
    if "_handle_invalid_parallel_tool_calls" not in err_str and "Unterminated string" not in err_str:
        return None
    if "received_args" not in err_str and "'tool_calls'" not in err_str:
        return None
    raw_args_repr = ""
    match = re.search(r"'arguments':\s*'(.*?)',\s*'(?:id|type|name|index|logprobs)':", err_str, re.DOTALL)
    if match:
        raw_args_repr = match.group(1)
    else:
        match2 = re.search(r"'arguments':\s*'(.*?)'(?:\s*}|\s*,)", err_str, re.DOTALL)
        if match2:
            raw_args_repr = match2.group(1)
        else:
            match3 = re.search(r"'arguments':\s*'(.+?)'\}", err_str, re.DOTALL)
            if match3:
                raw_args_repr = match3.group(1)
    if not raw_args_repr:
        return None
    raw_args = raw_args_repr.replace("\\'", "'").replace('\\"', '"')
    finish_reason = "tool_calls"
    fr = re.search(r"'finish_reason':\s*'(\w+)'", err_str)
    if fr:
        finish_reason = fr.group(1)
    name = "write_file"
    nm = re.search(r"'name':\s*'(\w+)'", err_str)
    if nm:
        name = nm.group(1)
    return {"name": name, "raw_arguments": raw_args, "finish_reason": finish_reason}


def build_gateway(config: dict[str, Any]) -> ModelGateway:
    """构建网关（兼容壳 + 新 runtime）。MOCK_LLM=1 时 runtime 内注册 MockAdapter。"""
    return ModelGateway(config)
