# -*- coding: utf-8 -*-
"""期 1 单测 · 错误体系与码分级。

判据 2 第四条：错误码分级；并验证「并入 infra/errors」的 ModelError 兼容。
零真实调用、零网络。
"""
from __future__ import annotations

import pytest

from src.infra.errors import ErrorCode, ModelError
from src.model_gateway.llm.errors import (
    CONTEXT_WINDOW_EXCEEDED_CODE,
    EMPTY_RESPONSE_CODE,
    INVALID_CREDENTIAL_CODE,
    QUOTA_EXCEEDED_CODE,
    LlmError,
    is_context_window_exceeded_error,
    is_quota_exceeded_error,
)

_GRADED = ["AUTH", "RATE_LIMIT", "QUOTA", "CONTEXT_WINDOW_EXCEEDED", "INVALID_REQUEST",
           "SERVER", "TIMEOUT", "TRANSPORT", "EMPTY_RESPONSE", "STREAM_CLOSED",
           "MALFORMED_RESPONSE", "MISSING_API_KEY", "NO_ADAPTER"]


def test_llm_error_is_a_model_error_for_existing_catchers():
    """调用方 ``except ModelError`` 零改动即可捕获新底座错误。"""
    err = LlmError("nope", "AUTH", status=401)
    assert isinstance(err, ModelError)
    assert isinstance(err, LlmError)
    with pytest.raises(ModelError):
        raise LlmError("nope", "AUTH", status=401)


def test_code_grading_carries_provider_neutral_codes():
    for code in _GRADED:
        err = LlmError("m", code)
        assert err.code == code
        assert err.failure.code == code


def test_dynamic_http_code_is_allowed():
    err = LlmError("m", "HTTP_418", status=418)
    assert err.code == "HTTP_418"


def test_llm_codes_are_registered_in_error_code_table():
    """码表扩展：静态 LLM 码登记进 infra ErrorCode。"""
    for name in ("AUTH", "RATE_LIMIT", "QUOTA", "CONTEXT_WINDOW_EXCEEDED", "SERVER",
                 "TIMEOUT", "TRANSPORT", "EMPTY_RESPONSE", "STREAM_CLOSED",
                 "MALFORMED_RESPONSE", "NO_ADAPTER"):
        assert ErrorCode[name].value == name


def test_llm_error_validates_facts():
    with pytest.raises(ValueError):
        LlmError("", "AUTH")
    with pytest.raises(ValueError):
        LlmError("m", "")
    with pytest.raises(ValueError):
        LlmError("m", "AUTH", status=99)
    with pytest.raises(ValueError):
        LlmError("m", "AUTH", provider_retry_after_ms=0)
    with pytest.raises(ValueError):
        LlmError("m", "AUTH", request_id="")


def test_failure_snapshot_keeps_optional_facts():
    err = LlmError("m", "RATE_LIMIT", status=429, provider_retry_after_ms=1500.0, request_id="r1")
    assert err.failure.status == 429
    assert err.failure.provider_retry_after_ms == 1500.0
    assert err.failure.request_id == "r1"


def test_cause_is_chained():
    cause = RuntimeError("inner")
    err = LlmError("outer", "TRANSPORT", cause=cause)
    assert err.__cause__ is cause


def test_context_window_exceeded_classifier():
    positives = [
        "This model's maximum context length is 65536 tokens",
        "context_length_exceeded",
        "The request is too large for the model's context window",
        "input exceeds the model context",
    ]
    for text in positives:
        assert is_context_window_exceeded_error(text) is True, text
    for text in ["rate limit exceeded", "all good", "maximum output tokens"]:
        assert is_context_window_exceeded_error(text) is False, text


def test_quota_exceeded_classifier():
    positives = [
        "insufficient_quota",
        "You exceeded your current quota",
        "usage limit reached",
        "balance exhausted",
        "out of credits",
    ]
    for text in positives:
        assert is_quota_exceeded_error(text) is True, text
    for text in ["rate limit exceeded", "invalid api key"]:
        assert is_quota_exceeded_error(text) is False, text


def test_code_constants():
    assert CONTEXT_WINDOW_EXCEEDED_CODE == "CONTEXT_WINDOW_EXCEEDED"
    assert QUOTA_EXCEEDED_CODE == "QUOTA"
    assert EMPTY_RESPONSE_CODE == "EMPTY_RESPONSE"
    assert INVALID_CREDENTIAL_CODE == "INVALID_CREDENTIAL"
