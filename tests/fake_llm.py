# -*- coding: utf-8 -*-
"""测试用假 LLM 适配器（[5715]）——**按新 runtime 协议**打桩。

## 为什么要有这个文件

`tests/test_stream_dual_delta.py` 与 `tests/test_stream_tool_calls.py` 原本
**零网络**（打桩 ``sys.modules["litellm"].acompletion``）。但 2026-10-16
「模型层底座抄写」把网关的流式实现换成了 ``LlmRuntime`` ＋ **provider 适配器**
（``src/model_gateway/llm/runtime.py``），**litellm 打桩不再被调用** ⇒
测试真去发网络请求 ⇒ key 过期 ⇒ 长期红着，**红账里混进了「key 过期」这种噪声**。

本文件给这两个网提供**与现实现同协议**的替身：

- ``FakeAdapter``：实现 ``LlmAdapter``（只需 ``stream``），把「裸 delta 脚本」
  自动补成完整帧序列（``block-start`` → 若干 delta → ``block-end`` → ``finish``）——
  **这一步是必须的**：`BlockAssembler` 只在收到 ``block-end`` 时把 partial 冻结成块，
  缺了它，终帧的 content 恒为空（实测踩过）。
- ``install_fake_llm``：把 ``ModelGateway._runtime`` 换成注册了假适配器的 runtime，
  并把 ``options``（model / messages / tools）记下来供断言。

⇒ 打桩重新生效，这几个网**离线可跑**，失败计数恢复可对账。
（``test_model_pick.py`` 的 6 条一并从「硬编码模型名」改成**锁机制**，
配置一改就不必改测试；三个网合计 **26 条全绿、0 xfail**。）
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from src.model_gateway.llm.runtime import LlmAdapter, LlmRuntime
from src.model_gateway.llm.types import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    GenerateOptions,
    ReasoningBlock,
    ReasoningDelta,
    StopReason,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallsReason,
)

#: 正文用的独立 index：避免与工具调用的 index=0 撞进同一个块。
TEXT_INDEX = 99


class FakeAdapter(LlmAdapter):
    """按脚本吐 chunk 的适配器（唯一必需方法＝``stream``）。

    脚本项可以是裸 delta（``TextDelta`` / ``ReasoningDelta`` / ``ToolCallDelta``），
    本适配器**自动补** block-start / block-end 帧（见模块说明：缺了终帧 content 恒空）。
    """

    def __init__(self, chunks: Iterable[Any], captured: Optional[dict[str, Any]] = None):
        self._chunks = list(chunks)
        self._captured = captured if captured is not None else {}

    def provider_info(self, provider: str) -> dict[str, str]:
        return {"id": provider, "name": "Fake"}

    async def stream(self, options: GenerateOptions):
        # 把请求面记下来，供用例断言（tools 真下发 / prior 真进 messages …）
        self._captured["provider"] = options.provider
        self._captured["model"] = options.model
        self._captured["messages"] = list(options.messages)
        self._captured["tools"] = list(options.tools or ())
        self._captured["system"] = options.system
        self._captured["temperature"] = options.temperature
        self._captured["reasoning_effort"] = options.reasoning_effort

        opened: dict[int, dict[str, Any]] = {}
        has_tool = False

        for c in self._chunks:
            if isinstance(c, (TextDelta, ReasoningDelta, ToolCallDelta)):
                idx = c.index
                if idx not in opened:
                    kind = ("text" if isinstance(c, TextDelta)
                            else "reasoning" if isinstance(c, ReasoningDelta)
                            else "tool-call")
                    opened[idx] = {"kind": kind, "text": "", "id": "", "name": "",
                                  "arguments": ""}
                    yield BlockStart(index=idx, block_type=kind)
                slot = opened[idx]
                if isinstance(c, TextDelta):
                    slot["text"] += c.text
                elif isinstance(c, ReasoningDelta):
                    slot["text"] += c.text
                else:
                    has_tool = True
                    slot["arguments"] += c.arguments_delta
                    if c.name:
                        slot["name"] = c.name
                    if c.id:
                        slot["id"] = c.id
                yield c
                continue
            if isinstance(c, FinishChunk):
                continue          # 结束帧由本适配器统一补（带对 tool-calls 的判据）
            yield c

        for idx, slot in opened.items():
            if slot["kind"] == "text":
                block = TextBlock(text=slot["text"])
            elif slot["kind"] == "reasoning":
                block = ReasoningBlock(text=slot["text"])
            else:
                block = ToolCallBlock(id=slot["id"], name=slot["name"],
                                      arguments=slot["arguments"])
            yield BlockEnd(index=idx, block=block)

        # [5715] 有工具调用 ⇒ 结束原因是 **tool-calls**（真实适配器就是这么发的；
        # 漏了它，网关的 finish_reason 恒 'stop'，工具循环的续跳判据会失准）。
        yield FinishChunk(reason=ToolCallsReason() if has_tool else StopReason())


def install_fake_llm(gw, chunks: Iterable[Any], provider: str = "openai") -> dict[str, Any]:
    """把 ``gw`` 的 runtime 换成只注册了假适配器的 runtime；返回请求面记录器。"""
    captured: dict[str, Any] = {}
    runtime = LlmRuntime()
    runtime.register_adapter([provider], FakeAdapter(chunks, captured))
    gw._runtime = runtime
    return captured


# ---------------------------------------------------------------- chunk 构造

def text_delta(text: str, index: int = TEXT_INDEX) -> TextDelta:
    return TextDelta(index=index, text=text)


def reasoning_delta(text: str, index: int = 0) -> ReasoningDelta:
    """思考增量用 index=0（与文本的 99 错开）——同 index 会被归进同一个块。"""
    return ReasoningDelta(index=index, text=text)


def tool_call_delta(index: int, id: str, arguments_delta: str = "",
                    name: Optional[str] = None) -> ToolCallDelta:
    return ToolCallDelta(index=index, id=id, arguments_delta=arguments_delta, name=name)


def finish(reason: Any = None) -> FinishChunk:
    return FinishChunk(reason=reason or StopReason())
