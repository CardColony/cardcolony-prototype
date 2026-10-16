# -*- coding: utf-8 -*-
"""模型层底座 · 重试策略（配置解析 + 退避计算）。

逐语义抄自原版：
    - deepseek-harness/packages/llm/llm/src/retry-policy.ts（191 行）：策略解析
    - deepseek-harness/packages/llm/llm-retry/src/index.ts:58-205：退避与 retry-after 优先

合并说明：原版把「策略解析」（llm 包）与「执行器」（llm-retry 插件）分成两包；
本系统无 cordis 插件面，合并进 ``retry.py`` 一个模块（解析 + 纯函数退避计算）。
原版执行器里依赖会话日志的部分（``agent.session.append``、去重用的 ``policyKey`` 持久化）
**不抄**——本系统的重试由调用层循环驱动，不需要把「已重试次数」落盘。

默认口径（原版 retry-policy.ts:14-24）：2 次重试 / 500ms 起 / 10s 顶 / 对称抖动 0.1；
可重试码集 EMPTY_RESPONSE / RATE_LIMIT / SERVER / TIMEOUT / TRANSPORT。
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Union

from .errors import EMPTY_RESPONSE_CODE
from .types import LlmFailure

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_INITIAL_DELAY_MS",
    "DEFAULT_MAX_DELAY_MS",
    "DEFAULT_JITTER_RATIO",
    "DEFAULT_RETRYABLE_CODES",
    "MAX_TIMER_DELAY_MS",
    "BackoffConfig",
    "NormalRetryPolicyConfig",
    "AlwaysRetryPolicyConfig",
    "ResolvedRetryBackoff",
    "ResolvedNormalRetryPolicy",
    "ResolvedAlwaysRetryPolicy",
    "ResolvedRetryPolicy",
    "resolve_retry_policy",
    "local_delay",
    "retry_delay",
    "should_retry",
]

# 原版取自 @deepseek-ai/dsh-timeout 的 MAX_TIMER_DELAY_MS（JS setTimeout 上限）。
MAX_TIMER_DELAY_MS = 2_147_483_647

DEFAULT_MAX_RETRIES = 2
DEFAULT_INITIAL_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 10_000
DEFAULT_JITTER_RATIO = 0.1
DEFAULT_RETRYABLE_CODES = (
    EMPTY_RESPONSE_CODE,
    "RATE_LIMIT",
    "SERVER",
    "TIMEOUT",
    "TRANSPORT",
)


# ---------------------------------------------------------------------------
# 配置形状 —— 原版 retry-policy.ts:26-79
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BackoffConfig:
    """有界指数退避 + 每档对称抖动。原版 retry-policy.ts:27-34。"""

    initial_delay_ms: Optional[float] = None
    max_delay_ms: Optional[float] = None
    jitter_ratio: Optional[float] = None


@dataclass(frozen=True)
class NormalRetryPolicyConfig:
    """有界瞬时重试。原版 retry-policy.ts:37-46。"""

    mode: str = "normal"
    max_retries: Optional[int] = None
    retryable_codes: Optional[Sequence[str]] = None
    backoff: Optional[BackoffConfig] = None


@dataclass(frozen=True)
class AlwaysRetryPolicyConfig:
    """无界重试（直到成功 / 取消）。原版 retry-policy.ts:49-54。"""

    mode: str = "always"
    backoff: Optional[BackoffConfig] = None


RetryPolicyConfig = Union[NormalRetryPolicyConfig, AlwaysRetryPolicyConfig]


@dataclass(frozen=True)
class ResolvedRetryBackoff:
    """两种模式共享的、已解析的退避参数。原版 retry-policy.ts:60-64。"""

    initial_delay_ms: float
    max_delay_ms: float
    jitter_ratio: float


@dataclass(frozen=True)
class ResolvedNormalRetryPolicy(ResolvedRetryBackoff):
    """已解析的有界瞬时重试策略。原版 retry-policy.ts:67-71。"""

    max_retries: int = DEFAULT_MAX_RETRIES
    retryable_codes: tuple[str, ...] = DEFAULT_RETRYABLE_CODES
    mode: str = "normal"


@dataclass(frozen=True)
class ResolvedAlwaysRetryPolicy(ResolvedRetryBackoff):
    """已解析的无界重试策略。原版 retry-policy.ts:74-76。"""

    mode: str = "always"


ResolvedRetryPolicy = Union[ResolvedNormalRetryPolicy, ResolvedAlwaysRetryPolicy]

_NORMAL_POLICY_KEYS = frozenset({"mode", "max_retries", "retryable_codes", "backoff"})
_ALWAYS_POLICY_KEYS = frozenset({"mode", "backoff"})
_BACKOFF_KEYS = frozenset({"initial_delay_ms", "max_delay_ms", "jitter_ratio"})


def _validate_keys(value: dict, allowed: frozenset, path: str) -> None:
    """原版 retry-policy.ts:111-115（未知键即报错）。"""
    for key in value:
        if key not in allowed:
            raise ValueError(f'{path}: unknown key "{key}"')


def _resolve_backoff(config: Optional[BackoffConfig], path: str) -> ResolvedRetryBackoff:
    """原版 retry-policy.ts:117-137。"""
    if config is not None:
        _validate_keys(config.__dict__, _BACKOFF_KEYS, path)
    initial = DEFAULT_INITIAL_DELAY_MS if config is None or config.initial_delay_ms is None else config.initial_delay_ms
    maxd = DEFAULT_MAX_DELAY_MS if config is None or config.max_delay_ms is None else config.max_delay_ms
    jitter = DEFAULT_JITTER_RATIO if config is None or config.jitter_ratio is None else config.jitter_ratio

    if not _finite(initial) or initial <= 0 or initial > MAX_TIMER_DELAY_MS:
        raise ValueError(f"{path}.initialDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if not _finite(maxd) or maxd <= 0 or maxd > MAX_TIMER_DELAY_MS:
        raise ValueError(f"{path}.maxDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if initial > maxd:
        raise ValueError(f"{path}.initialDelayMs must be less than or equal to maxDelayMs")
    if not _finite(jitter) or jitter < 0 or jitter > 1:
        raise ValueError(f"{path}.jitterRatio must be between 0 and 1")

    return ResolvedRetryBackoff(initial_delay_ms=float(initial), max_delay_ms=float(maxd), jitter_ratio=float(jitter))


def _finite(value: object) -> bool:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def resolve_retry_policy(config: Optional[RetryPolicyConfig], path: str) -> ResolvedRetryPolicy:
    """校验 / 补默认 / 脱钩一份 provider 重试策略。原版 retry-policy.ts:145-191。"""
    if config is None:
        backoff = _resolve_backoff(None, f"{path}.backoff")
        return ResolvedNormalRetryPolicy(
            initial_delay_ms=backoff.initial_delay_ms,
            max_delay_ms=backoff.max_delay_ms,
            jitter_ratio=backoff.jitter_ratio,
            max_retries=DEFAULT_MAX_RETRIES,
            retryable_codes=tuple(DEFAULT_RETRYABLE_CODES),
            mode="normal",
        )

    mode = getattr(config, "mode", None)
    if mode == "normal":
        _validate_keys(config.__dict__, _NORMAL_POLICY_KEYS, path)
        max_retries = DEFAULT_MAX_RETRIES if config.max_retries is None else config.max_retries
        codes = DEFAULT_RETRYABLE_CODES if config.retryable_codes is None else tuple(config.retryable_codes)
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise ValueError(f"{path}.maxRetries must be a non-negative safe integer")
        if len(codes) == 0:
            raise ValueError(f"{path}.retryableCodes must not be empty")
        if any((not isinstance(c, str)) or c == "" for c in codes):
            raise ValueError(f"{path}.retryableCodes must contain only non-empty strings")
        if len(set(codes)) != len(codes):
            raise ValueError(f"{path}.retryableCodes must not contain duplicates")
        backoff = _resolve_backoff(config.backoff, f"{path}.backoff")
        return ResolvedNormalRetryPolicy(
            initial_delay_ms=backoff.initial_delay_ms,
            max_delay_ms=backoff.max_delay_ms,
            jitter_ratio=backoff.jitter_ratio,
            max_retries=int(max_retries),
            retryable_codes=codes,
            mode="normal",
        )
    if mode == "always":
        _validate_keys(config.__dict__, _ALWAYS_POLICY_KEYS, path)
        backoff = _resolve_backoff(config.backoff, f"{path}.backoff")
        return ResolvedAlwaysRetryPolicy(
            initial_delay_ms=backoff.initial_delay_ms,
            max_delay_ms=backoff.max_delay_ms,
            jitter_ratio=backoff.jitter_ratio,
            mode="always",
        )
    raise ValueError(f'{path}.mode must be "normal" or "always"')


# ---------------------------------------------------------------------------
# 退避计算 —— 原版 llm-retry/index.ts:58-63
# ---------------------------------------------------------------------------
def local_delay(
    policy: ResolvedRetryPolicy,
    retry: int,
    rng: Optional[Callable[[], float]] = None,
) -> float:
    """本地有界指数退避 + 对称抖动。原版 llm-retry/index.ts:58-63。

    ``exponent = min(retry-1, 1024)``；``exponential = min(initial * 2**exponent, max)``；
    ``jitter = 1 - ratio + 2*ratio*random()``；结果再以 ``max`` 封顶。
    """
    sample = (rng or _random.random)()
    exponent = min(retry - 1, 1024)
    exponential = min(policy.initial_delay_ms * (2 ** exponent), policy.max_delay_ms)
    jitter = 1 - policy.jitter_ratio + 2 * policy.jitter_ratio * sample
    return min(exponential * jitter, policy.max_delay_ms)


def retry_delay(
    failure: LlmFailure,
    policy: ResolvedRetryPolicy,
    retry: int,
    rng: Optional[Callable[[], float]] = None,
) -> Optional[float]:
    """本次重试应等待的毫秒数；``None`` 表示**放弃重试**。

    原版 llm-retry/index.ts:193-205：供应商 ``retry-after`` 头优先于本地退避——
    - 有 ``provider_retry_after_ms`` 且有限且 > 0：
        - 若它 > ``max_delay_ms``：normal 模式**直接放弃**（返回 None）；always 模式改用本地退避。
        - 否则用它。
    - 否则用本地退避。
    """
    after = failure.provider_retry_after_ms
    if after is not None and _finite(after) and after > 0:
        if after > policy.max_delay_ms:
            if policy.mode == "normal":
                return None
            return local_delay(policy, retry, rng)
        return float(after)
    return local_delay(policy, retry, rng)


def should_retry(policy: ResolvedRetryPolicy, failure: LlmFailure, previous_retry: int) -> bool:
    """该失败是否应再试一次。原版 llm-retry/index.ts:161-190。

    - ``always``：恒真（除取消）。
    - ``normal``：``failure.code`` 必须在 ``retryable_codes`` 内，且 ``previous_retry < max_retries``。
    """
    if policy.mode == "always":
        return True
    if failure.code not in policy.retryable_codes:
        return False
    return previous_retry < policy.max_retries
