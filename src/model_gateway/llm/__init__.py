# -*- coding: utf-8 -*-
"""模型层底座 · llm 核心包（provider 无关的协议 / 运行时 / 装配 / 重试 / 错误）。

整层抄自 dsh：``deepseek-harness/packages/llm/llm/src/``。
本包只含**与供应商无关**的骨架；具体 wire 适配器在 ``llm_openai_compat``（五家共用的 OpenAI 兼容直连适配器）。

    types.py      协议词汇表        ← types.ts
    message.py    消息值类型        ← message.ts
    errors.py     错误体系          ← error.ts（并入 src/infra/errors）
    retry.py      重试策略与退避    ← retry-policy.ts + llm-retry/index.ts
    assembler.py  增量装配器        ← assembler.ts
    runtime.py    运行时 / 注册表   ← index.ts（LlmRuntime 部分）
"""

from __future__ import annotations

from .assembler import BlockAssembler
from .errors import (
    CONTEXT_WINDOW_EXCEEDED_CODE,
    EMPTY_RESPONSE_CODE,
    INVALID_CREDENTIAL_CODE,
    MISSING_API_KEY_CODE,
    QUOTA_EXCEEDED_CODE,
    LlmError,
    is_context_window_exceeded_error,
    is_quota_exceeded_error,
)
from .message import (
    AssistantProvenance,
    Message,
    MessageSource,
    ModelMessageSource,
    ToolMessageSource,
    UserMessageSource,
    create_assistant_message,
    create_message,
    create_tool_result_message,
    create_user_message,
    is_token_delta,
)
from .retry import (
    DEFAULT_JITTER_RATIO,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRYABLE_CODES,
    AlwaysRetryPolicyConfig,
    BackoffConfig,
    NormalRetryPolicyConfig,
    ResolvedAlwaysRetryPolicy,
    ResolvedNormalRetryPolicy,
    ResolvedRetryPolicy,
    local_delay,
    resolve_retry_policy,
    retry_delay,
    should_retry,
)
from .runtime import LlmAdapter, LlmRuntime, RuntimeStats, normalize_llm_failure
from .types import (
    AbortedReason,
    BlockEnd,
    BlockStart,
    ContentBlock,
    ErrorReason,
    FinishChunk,
    FinishReason,
    GenerateOptions,
    LlmFailure,
    MaxTokensReason,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    StreamChunk,
    TextBlock,
    TextDelta,
    TokenUsage,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallsReason,
    ToolResultBlock,
    ToolSchema,
    UsageChunk,
)

__all__ = [
    # assembler
    "BlockAssembler",
    # errors
    "LlmError",
    "CONTEXT_WINDOW_EXCEEDED_CODE",
    "QUOTA_EXCEEDED_CODE",
    "EMPTY_RESPONSE_CODE",
    "INVALID_CREDENTIAL_CODE",
    "MISSING_API_KEY_CODE",
    "is_context_window_exceeded_error",
    "is_quota_exceeded_error",
    # message
    "AssistantProvenance",
    "Message",
    "MessageSource",
    "ModelMessageSource",
    "ToolMessageSource",
    "UserMessageSource",
    "create_message",
    "create_user_message",
    "create_assistant_message",
    "create_tool_result_message",
    "is_token_delta",
    # retry
    "BackoffConfig",
    "NormalRetryPolicyConfig",
    "AlwaysRetryPolicyConfig",
    "ResolvedRetryPolicy",
    "ResolvedNormalRetryPolicy",
    "ResolvedAlwaysRetryPolicy",
    "resolve_retry_policy",
    "local_delay",
    "retry_delay",
    "should_retry",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_DELAY_MS",
    "DEFAULT_JITTER_RATIO",
    "DEFAULT_RETRYABLE_CODES",
    # runtime
    "LlmAdapter",
    "LlmRuntime",
    "RuntimeStats",
    "normalize_llm_failure",
    # types
    "LlmFailure",
    "TextBlock",
    "ReasoningBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ContentBlock",
    "StopReason",
    "ToolCallsReason",
    "MaxTokensReason",
    "AbortedReason",
    "ErrorReason",
    "FinishReason",
    "TokenUsage",
    "ToolSchema",
    "GenerateOptions",
    "BlockStart",
    "TextDelta",
    "ReasoningDelta",
    "ToolCallDelta",
    "BlockEnd",
    "UsageChunk",
    "FinishChunk",
    "StreamChunk",
]
