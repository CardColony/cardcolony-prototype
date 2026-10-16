# -*- coding: utf-8 -*-
"""模型层底座 · 错误体系。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm/src/error.ts（163 行）

并入方式（用户裁定「并入现有 infra/errors（ModelError 子类或码表扩展）」）：
    - ``LlmError`` 是 :class:`src.infra.errors.ModelError` 的**子类** ⇒ 既有
      ``except ModelError`` / ``except AppError`` 调用方零改动即可捕获新底座错误。
    - 码表在 ``src/infra/errors.py::ErrorCode`` 上扩展（AUTH / RATE_LIMIT / …）。
    - ``LlmError.code`` 是**裸字符串**（与 dsh 一致，支持动态 ``HTTP_503`` 这类码）；
      静态码同时登记进 ``ErrorCode`` 供检索。

未抄：``errorChain``（error.ts:114-154，诊断渲染）——本系统日志走 structlog，
无「把 cause 链渲染成一串」的消费方。``HarnessError`` 基类语义并入 ``LlmError``。
"""

from __future__ import annotations

import re
from typing import Optional

from ...infra.errors import ModelError
from .types import LlmFailure

__all__ = [
    "CONTEXT_WINDOW_EXCEEDED_CODE",
    "QUOTA_EXCEEDED_CODE",
    "EMPTY_RESPONSE_CODE",
    "INVALID_CREDENTIAL_CODE",
    "MISSING_API_KEY_CODE",
    "LlmError",
    "is_context_window_exceeded_error",
    "is_quota_exceeded_error",
]

# ---------------------------------------------------------------------------
# 码常量 —— 原版 error.ts:25-48
# ---------------------------------------------------------------------------
CONTEXT_WINDOW_EXCEEDED_CODE = "CONTEXT_WINDOW_EXCEEDED"
QUOTA_EXCEEDED_CODE = "QUOTA"
EMPTY_RESPONSE_CODE = "EMPTY_RESPONSE"
INVALID_CREDENTIAL_CODE = "INVALID_CREDENTIAL"
# 原版没有独立常量（凭据缺失由 dsh-credentials 承担）；本系统沿用旧 gateway 的码名。
MISSING_API_KEY_CODE = "MISSING_API_KEY"


# ---------------------------------------------------------------------------
# 上下文超限 / 配额耗尽的措辞识别 —— 原版 error.ts:50-100
# ---------------------------------------------------------------------------
_STRUCTURED_CONTEXT_OVERFLOW = re.compile(
    r"(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]"
    r"(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)

_TOO_LARGE_FOR_CONTEXT = re.compile(
    r"\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?"
    r"too\s+(?:large|long)\s+for\s+(?:(?:this|the)\s+)?"
    r"(?:model(?:'s)?\s+)?context(?:\s+window)?\b",
    re.IGNORECASE,
)

_EXCEEDS_MODEL_CONTEXT = re.compile(
    r"\b(?:input|prompt|request|messages?)\b.{0,40}"
    r"\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}"
    r"\b(?:the\s+)?(?:model(?:'s)?\s+)?context(?:\s+(?:length|window))?\b",
    re.IGNORECASE,
)

_MAX_CONTEXT = re.compile(
    r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b",
    re.IGNORECASE,
)

_TOO_LONG_FOR_MODEL = re.compile(
    r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b",
    re.IGNORECASE,
)

_QUOTA_PATTERNS = (
    re.compile(r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", re.IGNORECASE),
    re.compile(r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b", re.IGNORECASE),
    re.compile(r"\bexceed(?:ed|s)?[\s_-]+(?:(?:your|the)[\s_-]+)?(?:current[\s_-]+)?quota\b", re.IGNORECASE),
    re.compile(r"\b(?:balance|credits?)[\s_-]+(?:exhausted|depleted)\b", re.IGNORECASE),
    re.compile(r"\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b", re.IGNORECASE),
)


def is_context_window_exceeded_error(detail: str) -> bool:
    """识别「请求超出模型上下文窗口」的措辞。原版 error.ts:80-86。"""
    return bool(
        _STRUCTURED_CONTEXT_OVERFLOW.search(detail)
        or _MAX_CONTEXT.search(detail)
        or _TOO_LARGE_FOR_CONTEXT.search(detail)
        or _TOO_LONG_FOR_MODEL.search(detail)
        or _EXCEEDS_MODEL_CONTEXT.search(detail)
    )


def is_quota_exceeded_error(detail: str) -> bool:
    """识别「账户配额耗尽」（区别于限流）。原版 error.ts:94-100。"""
    return any(p.search(detail) for p in _QUOTA_PATTERNS)


# ---------------------------------------------------------------------------
# LlmError —— 原版 error.ts:13-22 + index.ts:83-117
# ---------------------------------------------------------------------------
class LlmError(ModelError):
    """LLM 相关失败。``code`` 是稳定、可程序化路由的字符串（如 ``AUTH`` /
    ``RATE_LIMIT`` / ``NO_ADAPTER``）；**路由只认 code，绝不解析 message**。

    原版 ``LlmError``（index.ts:83-117）在构造时校验各事实字段并冻结
    ``failure`` 快照；此处保持同语义。
    """

    def __init__(
        self,
        message: str,
        code: str,
        *,
        status: Optional[int] = None,
        provider_retry_after_ms: Optional[float] = None,
        request_id: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        if not isinstance(message, str) or message == "":
            raise ValueError("LlmError message must be a non-empty string")
        if not isinstance(code, str) or code == "":
            raise ValueError("LlmError code must be a non-empty string")
        if status is not None and (not isinstance(status, int) or status < 100 or status > 599):
            raise ValueError("LlmError status must be an integer from 100 through 599")
        if provider_retry_after_ms is not None and (
            not isinstance(provider_retry_after_ms, (int, float)) or provider_retry_after_ms <= 0
        ):
            raise ValueError("LlmError provider_retry_after_ms must be a positive finite number")
        if request_id is not None and (not isinstance(request_id, str) or request_id == ""):
            raise ValueError("LlmError request_id must be a non-empty string")

        super().__init__(code, message)  # AppError.__init__(code, msg)
        if cause is not None:
            self.__cause__ = cause
        self.failure = LlmFailure(
            message=message,
            code=code,
            status=status,
            provider_retry_after_ms=(
                float(provider_retry_after_ms) if provider_retry_after_ms is not None else None
            ),
            request_id=request_id,
        )
