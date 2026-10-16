# -*- coding: utf-8 -*-
"""期 1 单测 · 重试策略与退避。

判据 2 第三条：retry（退避曲线、可重试码集、retry-after 优先）。
零真实调用、零网络。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.model_gateway.llm.retry import (
    DEFAULT_INITIAL_DELAY_MS,
    DEFAULT_JITTER_RATIO,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRYABLE_CODES,
    AlwaysRetryPolicyConfig,
    BackoffConfig,
    NormalRetryPolicyConfig,
    local_delay,
    resolve_retry_policy,
    retry_delay,
    should_retry,
)
from src.model_gateway.llm.types import LlmFailure

_MID = lambda: 0.5  # noqa: E731  抖动因子恰为 1.0（对称中点）


def test_default_policy_matches_documented_defaults():
    p = resolve_retry_policy(None, "p")
    assert p.mode == "normal"
    assert p.max_retries == DEFAULT_MAX_RETRIES == 2
    assert p.initial_delay_ms == DEFAULT_INITIAL_DELAY_MS == 500
    assert p.max_delay_ms == DEFAULT_MAX_DELAY_MS == 10_000
    assert p.jitter_ratio == DEFAULT_JITTER_RATIO == 0.1
    assert tuple(p.retryable_codes) == tuple(DEFAULT_RETRYABLE_CODES)
    assert set(DEFAULT_RETRYABLE_CODES) == {"EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"}


def test_backoff_curve_is_bounded_exponential():
    p = resolve_retry_policy(None, "p")
    assert local_delay(p, 1, _MID) == 500
    assert local_delay(p, 2, _MID) == 1000
    assert local_delay(p, 3, _MID) == 2000
    assert local_delay(p, 5, _MID) == 8000
    assert local_delay(p, 6, _MID) == 10_000  # 封顶
    assert local_delay(p, 20, _MID) == 10_000  # 再高也不越顶


def test_symmetric_jitter_bounds():
    p = resolve_retry_policy(None, "p")
    assert local_delay(p, 1, lambda: 0.0) == pytest.approx(450)   # 500 * 0.9
    assert local_delay(p, 1, lambda: 1.0) == pytest.approx(550)   # 500 * 1.1
    # 抖动不得越顶
    assert local_delay(p, 6, lambda: 1.0) == 10_000


def test_retryable_code_set_decides_should_retry():
    p = resolve_retry_policy(None, "p")
    assert should_retry(p, LlmFailure(message="", code="SERVER"), 0) is True
    assert should_retry(p, LlmFailure(message="", code="AUTH"), 0) is False        # 不可重试
    assert should_retry(p, LlmFailure(message="", code="MALFORMED_RESPONSE"), 0) is False
    assert should_retry(p, LlmFailure(message="", code="SERVER"), 2) is False      # 用满 maxRetries


def test_always_mode_retries_everything():
    p = resolve_retry_policy(AlwaysRetryPolicyConfig(), "p")
    assert p.mode == "always"
    assert should_retry(p, LlmFailure(message="", code="AUTH"), 99) is True


def test_provider_retry_after_takes_precedence():
    p = resolve_retry_policy(None, "p")
    # 头在本地顶之内 → 直接用头的值
    assert retry_delay(LlmFailure(message="", code="RATE_LIMIT", provider_retry_after_ms=1500.0), p, 1, _MID) == 1500.0
    # 头超出本地顶 + normal → 放弃重试
    assert retry_delay(LlmFailure(message="", code="RATE_LIMIT", provider_retry_after_ms=20000.0), p, 1, _MID) is None
    # 无头 → 本地退避
    assert retry_delay(LlmFailure(message="", code="RATE_LIMIT"), p, 1, _MID) == 500


def test_retry_after_over_max_in_always_mode_falls_back_to_local():
    p = resolve_retry_policy(AlwaysRetryPolicyConfig(), "p")
    assert retry_delay(LlmFailure(message="", code="SERVER", provider_retry_after_ms=20000.0), p, 1, _MID) == 500


def test_resolve_validates_config():
    with pytest.raises(ValueError):
        resolve_retry_policy(SimpleNamespace(mode="normal", bogus=1), "p")  # 未知键
    with pytest.raises(ValueError):
        resolve_retry_policy(NormalRetryPolicyConfig(backoff=BackoffConfig(initial_delay_ms=0)), "p")
    with pytest.raises(ValueError):
        resolve_retry_policy(
            NormalRetryPolicyConfig(backoff=BackoffConfig(initial_delay_ms=9000, max_delay_ms=1000)), "p"
        )
    with pytest.raises(ValueError):
        resolve_retry_policy(NormalRetryPolicyConfig(backoff=BackoffConfig(jitter_ratio=1.5)), "p")
    with pytest.raises(ValueError):
        resolve_retry_policy(NormalRetryPolicyConfig(retryable_codes=()), "p")
    with pytest.raises(ValueError):
        resolve_retry_policy(NormalRetryPolicyConfig(retryable_codes=("A", "A")), "p")
    with pytest.raises(ValueError):
        resolve_retry_policy(SimpleNamespace(mode="bogus"), "p")


def test_resolved_policy_is_frozen():
    p = resolve_retry_policy(None, "p")
    with pytest.raises(Exception):
        p.max_retries = 9  # type: ignore[misc]
