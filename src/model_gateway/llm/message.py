# -*- coding: utf-8 -*-
"""模型层底座 · 消息值类型与不可变构造助手。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm/src/message.ts（261 行）

裁剪说明：
    - 原版 ``Message.id: MessageId`` 与 ``MessageId`` brand（message.ts:3,131）**不抄**：
      本系统消息在调用层即时构造、不落会话日志、无跨边界身份需求（用户裁定：溯源裁到
      provider+model 两字段）。对应点：原版靠 id 做「投递 / 耐久历史 / 模型请求」三态
      同一性，本系统只有「模型请求」一态。
    - ``ContextForm`` / ``ContextFormed`` / ``ContextSnapshotSection`` /
      ``CONTEXT_SUMMARY_MAX_CHARS`` / ``boundContextSummary``（message.ts:32-123）**不抄**：
      dsh 宿主「注入内容形态」面；本系统注入形态由事件层（``context_preamble`` /
      ``context_injection``）承担，与此无关。
    - ``MessageSourceMap`` 的 ``plugin`` 分支（message.ts:102）不抄：本系统无 plugin 源。
    - ``UserMessage`` / ``AssistantMessage`` / ``ToolResultMessage`` 三个**子类型**
      （message.ts:141-156）不抄为独立类：它们只是 role/source 的静态收窄，Python 侧用
      工厂函数（``create_user_message`` 等）表达同一约束，避免 dataclass 继承里
      「子类 ClassVar 覆盖父类字段」的语义陷阱。
    - ``freezeMessage`` 用 ``deepFreeze(structuredClone(...))``；Python 侧改用
      **不可变容器**（content 存 tuple）表达同一「发布前冻结」语义，不引入深冻结遍历。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Union

from .types import ContentBlock, StreamChunk, ToolResultBlock

__all__ = [
    "AssistantProvenance",
    "ModelMessageSource",
    "ToolMessageSource",
    "UserMessageSource",
    "MessageSource",
    "Message",
    "create_message",
    "create_user_message",
    "create_assistant_message",
    "create_tool_result_message",
    "is_token_delta",
]


# ---------------------------------------------------------------------------
# 溯源与来源 —— 原版 message.ts:7-105
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AssistantProvenance:
    """assistant 消息的 provider / model 溯源。

    原版 message.ts:8-19 还带 ``replayState?``；按用户裁定**裁到 provider + model
    两字段**——本系统无跨适配器重放需求（原版 replayState 只服务于 dsh 的重放路由）。
    """

    provider: str
    model: str


@dataclass(frozen=True)
class ModelMessageSource(AssistantProvenance):
    """路由模型产出的 assistant 消息来源。原版 message.ts:22-24。"""

    kind: ClassVar[str] = "model"


@dataclass(frozen=True)
class ToolMessageSource:
    """携带一条工具结果的 user 角色消息来源。原版 message.ts:27-30。"""

    call_id: str
    kind: ClassVar[str] = "tool"


@dataclass(frozen=True)
class UserMessageSource:
    """用户直接输入。原版 message.ts:101（``{kind:'user'}``）。"""

    kind: ClassVar[str] = "user"


MessageSource = Union[UserMessageSource, ModelMessageSource, ToolMessageSource]


# ---------------------------------------------------------------------------
# Message —— 原版 message.ts:129-156
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Message:
    """投递 / 耐久历史 / 模型请求共享的一条不可变消息。

    原版 message.ts:129-138 的字段为 ``id / role / content / source``；本系统去 ``id``
    （见模块头裁剪说明）。``content`` 存 tuple 表达原版的「发布前冻结」。
    """

    role: str
    content: tuple[ContentBlock, ...]
    source: MessageSource


# ---------------------------------------------------------------------------
# 构造助手 —— 原版 message.ts:164-241
# ---------------------------------------------------------------------------
def create_message(role: str, content: "tuple[ContentBlock, ...]", source: MessageSource) -> Message:
    """构造一条不可变消息。原版 ``createMessage``（message.ts:178-185）去掉 id 分配。"""
    return Message(role=role, content=tuple(content), source=source)


def create_user_message(content: "tuple[ContentBlock, ...]") -> Message:
    """构造一条 user 角色消息。原版 ``createUserMessage``（message.ts:192-199）。"""
    return Message(role="user", content=tuple(content), source=UserMessageSource())


def create_assistant_message(
    content: "tuple[ContentBlock, ...]",
    provider: str,
    model: str,
) -> Message:
    """构造一条模型产出的 assistant 消息。原版 ``createAssistantMessage``（message.ts:206-217）。"""
    return Message(
        role="assistant",
        content=tuple(content),
        source=ModelMessageSource(provider=provider, model=model),
    )


def create_tool_result_message(
    call_id: str,
    content: "tuple[ContentBlock, ...]" = (),
    is_error: bool = False,
) -> Message:
    """构造一条工具结果消息。原版 ``createToolResultMessage``（message.ts:231-241）。

    原版把结果包成**单元素** ``content: [ToolResultBlock]`` 且 role 恒为 ``user``
    （message.ts:152-156）——此处保持同形。
    """
    return Message(
        role="user",
        content=(ToolResultBlock(tool_call_id=call_id, content=tuple(content), is_error=is_error),),
        source=ToolMessageSource(call_id=call_id),
    )


# ---------------------------------------------------------------------------
# isTokenDelta —— 原版 message.ts:251-261
# ---------------------------------------------------------------------------
def is_token_delta(chunk: StreamChunk) -> bool:
    """该 chunk 是否携带**可见**模型输出（首 token 边界）。

    原版 message.ts:251-261：空增量（心跳、空工具帧）不算首 token。
    """
    kind = getattr(chunk, "type", None)
    if kind in ("text-delta", "reasoning-delta"):
        return chunk.text != ""
    if kind == "tool-call-delta":
        return chunk.arguments_delta != "" or chunk.name is not None
    return False
