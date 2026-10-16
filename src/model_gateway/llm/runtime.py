# -*- coding: utf-8 -*-
"""模型层底座 · 运行时（适配器注册表 + 流式调用分发）。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm/src/index.ts（947 行中的 ``LlmRuntime`` / ``LlmAdapter``）

裁剪说明（原版有、本系统无消费方）：
    - **waterfall 拦截点**（原版 index.ts:46-67 的 ``llm/stream`` 事件 + ``ctx.waterfall``）：
      本系统无 cordis 事件面、无「重试 / 重放 / 路由」中间件消费方，**留接缝注释不实现**。
      对应点：原版靠该接缝挂 llm-retry 插件；本系统重试由调用层循环 + ``retry.py`` 纯函数承担。
    - ``registerConfigurableProviders`` / ``listConfigurableProviders`` / ``registerModelDiscovery`` /
      ``discoverModels`` / ``listModels`` / ``resolveModelInfo`` / ``resolveCallConfig`` /
      ``prepareCall``（原版 index.ts:423-814）：dsh 宿主「模型目录 / 设置界面 / 能力探测 /
      预解析调用」面。本系统模型声明由 ``config/models.yaml`` 承担，不经运行时目录查询。
    - ``AdapterRegistrationHandle.replace`` / ``DirectoryRegistrationHandle``（原版 index.ts:239-278）：
      热替换语义；本系统重建 runtime 即整体换引用（``build_gateway`` 一步换单例），不需原地替换。
    - ``forAdapter`` 的 replayState 过滤（原版 index.ts:822-836）：本系统裁掉 replayState，无此需求。

★ 与原版的一处**有意差异**：``register_adapter`` 做成**幂等**（任务书明示「幂等」）——
    同一 adapter 实例重复注册同一 provider 是 no-op；不同 adapter 抢注同一 provider 才报
    ``DUPLICATE_ADAPTER``。原版（index.ts:338-396）对任何重复注册都抛错。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Optional

from .errors import LlmError
from .retry import ResolvedRetryPolicy, resolve_retry_policy
from .types import (
    AbortedReason,
    ErrorReason,
    FinishChunk,
    GenerateOptions,
    LlmFailure,
    StreamChunk,
)

__all__ = ["LlmAdapter", "LlmRuntime", "RuntimeStats", "normalize_llm_failure"]


# ---------------------------------------------------------------------------
# GatewayStats 语义并入 —— 原 src/model_gateway/stats.py（已删除）
# ---------------------------------------------------------------------------
class RuntimeStats:
    """累计各模型调用次数 / token / 最近调用日志。

    语义承自旧 ``src/model_gateway/stats.py::GatewayStats``（字段名与 ``summary()``
    形状逐字保留，供既有读法零改动）。
    """

    def __init__(self) -> None:
        self.total_tokens: int = 0
        self.total_calls: int = 0
        self.by_model: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "tokens": 0, "failures": 0}
        )
        self.recent: list[dict[str, Any]] = []

    def record(self, model_name: str, tokens: int, latency_ms: int, success: bool = True) -> None:
        self.total_calls += 1
        self.total_tokens += tokens
        entry = self.by_model[model_name]
        entry["calls"] += 1
        entry["tokens"] += tokens
        if not success:
            entry["failures"] += 1
        self.recent.append(
            {"model": model_name, "tokens": tokens, "latency_ms": latency_ms, "success": success}
        )
        if len(self.recent) > 100:
            self.recent = self.recent[-100:]

    def summary(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "total_calls": self.total_calls,
            "by_model": {k: dict(v) for k, v in self.by_model.items()},
            "recent": self.recent[-10:],
        }


# ---------------------------------------------------------------------------
# LlmAdapter —— 原版 index.ts:180-233
# ---------------------------------------------------------------------------
class LlmAdapter(ABC):
    """provider 线上协议的适配器基类。

    原版 ``LlmAdapter``（index.ts:180-233）：只有 ``stream`` 是必需的；``providerInfo`` /
    ``providerRetryPolicy`` / ``listModels`` / ``resolveModel`` 有默认实现。本系统只保留
    前两个（其余属宿主目录面）。
    """

    def provider_info(self, provider: str) -> dict[str, str]:
        """原版 index.ts:186-188（默认 id==name）。"""
        return {"id": provider, "name": provider}

    def provider_retry_policy(self, provider: str) -> Optional[ResolvedRetryPolicy]:
        """原版 index.ts:195-197（默认 None ⇒ 用 normal 默认）。"""
        return None

    @abstractmethod
    def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        """流式发起一次模型调用，产出原始 chunk（唯一必需方法）。原版 index.ts:232。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 注册记录 —— 原版 index.ts:941-945
# ---------------------------------------------------------------------------
@dataclass
class _Registration:
    adapter: LlmAdapter
    provider_id: str
    provider_name: str
    retry_policy: ResolvedRetryPolicy


# ---------------------------------------------------------------------------
# LlmRuntime —— 原版 index.ts:284-928
# ---------------------------------------------------------------------------
class LlmRuntime:
    """适配器注册表 + 流式模型调用 API。原版 ``LlmRuntime``（index.ts:284-928）。"""

    def __init__(self, stats: Optional[RuntimeStats] = None) -> None:
        self._adapters: dict[str, _Registration] = {}
        self.stats = stats if stats is not None else RuntimeStats()

    # -- 注册 —— 原版 index.ts:338-413（+ 任务书「幂等」要求）---------------
    def register_adapter(self, providers: Iterable[str], adapter: LlmAdapter) -> None:
        """为给定 provider 路由注册一个适配器。

        - 同一 adapter 实例重复注册同一 provider：no-op（幂等）。
        - 不同 adapter 抢注已注册 provider：抛 ``DUPLICATE_ADAPTER``。
        - 空 provider 名 / 非法元数据：抛 ``INVALID_ADAPTER``。
        """
        prepared: list[_Registration] = []
        seen: set[str] = set()
        for provider in providers:
            if not isinstance(provider, str) or provider == "":
                raise LlmError("adapter provider names must be non-empty", "INVALID_ADAPTER")
            if provider in seen:
                raise LlmError(
                    f'an adapter for provider "{provider}" is already registered', "DUPLICATE_ADAPTER"
                )
            seen.add(provider)
            existing = self._adapters.get(provider)
            if existing is not None:
                if existing.adapter is adapter:
                    continue  # 幂等：同实例同 provider
                raise LlmError(
                    f'an adapter for provider "{provider}" is already registered', "DUPLICATE_ADAPTER"
                )
            info = adapter.provider_info(provider)
            if (
                not isinstance(info.get("id"), str)
                or info["id"] != provider
                or not isinstance(info.get("name"), str)
                or info["name"] == ""
            ):
                raise LlmError(
                    f'adapter metadata for provider "{provider}" must preserve its id and have a non-empty name',
                    "INVALID_ADAPTER",
                )
            retry_policy = adapter.provider_retry_policy(provider) or resolve_retry_policy(
                None, f'llm: provider "{provider}" retryPolicy'
            )
            prepared.append(
                _Registration(
                    adapter=adapter,
                    provider_id=info["id"],
                    provider_name=info["name"],
                    retry_policy=retry_policy,
                )
            )
        for reg in prepared:
            self._adapters[reg.provider_id] = reg

    def registered_providers(self) -> list[str]:
        """已注册的 provider 路由（按注册序）。"""
        return list(self._adapters.keys())

    def provider_retry_policy(self, provider: str) -> ResolvedRetryPolicy:
        """取某 provider 注册时捕获的重试策略。原版 index.ts:566-568。"""
        return self._registration(provider).retry_policy

    def _registration(self, provider: str) -> _Registration:
        """原版 index.ts:816-820（未注册抛 NO_ADAPTER）。"""
        reg = self._adapters.get(provider)
        if reg is None:
            raise LlmError(f'no adapter registered for provider "{provider}"', "NO_ADAPTER")
        return reg

    # -- 流式调用 —— 原版 index.ts:838-927 --------------------------------
    def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        """流式发起一次模型调用（token 级 delta）。

        原版 ``stream``（index.ts:913-915）→ ``streamWithRegistration``（917-927）→
        ``adapterStream``（843-900）。**waterfall 接缝**（原版 921-926 的
        ``ctx.waterfall(this, 'llm/stream', ...)``）在本系统不实现——见模块头说明。

        适配器选择 / 分发 / 迭代失败 → 归一成终帧 ``finish``（``error`` / ``aborted``）；
        调用方（消费侧）异常仍照常抛出。原版 index.ts:930-939 ``adapterFailureChunk``。
        """
        return self._adapter_stream(options)

    async def _adapter_stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        started = time.monotonic()
        tokens = 0
        failed = False
        completed = False
        iterator: Optional[AsyncIterator[StreamChunk]] = None
        try:
            try:
                registration = self._registration(options.provider)
            except LlmError as exc:
                failed = True
                yield _failure_chunk(exc, options.signal)
                return

            try:
                iterator = registration.adapter.stream(options).__aiter__()
            except BaseException as exc:  # noqa: BLE001
                failed = True
                yield _failure_chunk(exc, options.signal)
                return

            while True:
                try:
                    item = await iterator.__anext__()
                except StopAsyncIteration:
                    completed = True
                    return
                except BaseException as exc:  # noqa: BLE001
                    completed = True
                    failed = True
                    yield _failure_chunk(exc, options.signal)
                    return
                kind = getattr(item, "type", None)
                if kind == "usage":
                    try:
                        tokens = int(item.usage.input_tokens) + int(item.usage.output_tokens)
                    except Exception:  # noqa: BLE001
                        pass
                elif kind == "finish":
                    if getattr(item.reason, "kind", None) in ("error", "aborted"):
                        failed = True
                yield item
        finally:
            if not completed and iterator is not None:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass
            self._record(options, tokens, started, success=not failed)

    def _record(self, options: GenerateOptions, tokens: int, started: float, success: bool) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            self.stats.record(options.model, tokens, latency_ms, success)
        except Exception:  # noqa: BLE001 统计绝不影响主流程
            pass


# ---------------------------------------------------------------------------
# 终帧归一 —— 原版 index.ts:930-939 / adapter-failure.ts:16-28
# ---------------------------------------------------------------------------
def _failure_chunk(error: BaseException, signal: Any) -> FinishChunk:
    """把一个适配器抛错归一成流协议的终帧。原版 ``adapterFailureChunk``（index.ts:930-939）。"""
    failure = normalize_llm_failure(error)
    aborted = bool(getattr(signal, "aborted", False)) or failure.code == "ABORTED"
    return FinishChunk(reason=AbortedReason(failure=failure) if aborted else ErrorReason(failure=failure))


def normalize_llm_failure(value: BaseException) -> LlmFailure:
    """把适配器边界抛出的值脱钩成可序列化的 provider 无关事实。

    原版 ``normalizeLlmFailure``（adapter-failure.ts:16-28）：只有本仓 ``LlmError``
    携带的码才可信，其余归 ``UNKNOWN``。
    """
    if isinstance(value, LlmError):
        return value.failure
    message = str(value) or "LLM adapter failed"
    return LlmFailure(message=message, code="UNKNOWN")
