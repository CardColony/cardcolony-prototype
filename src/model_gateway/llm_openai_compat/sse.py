# -*- coding: utf-8 -*-
"""llm_openai_compat · SSE 字节流解析（物理迁自 llm_deepseek，全仓唯一一份）。

逐语义抄自原版：
    deepseek-harness/packages/llm/llm-deepseek/src/sse.ts（40 行）

原版把 framing 交给 ``eventsource-parser``（chunk 重组 / UTF-8 / CRLF / BOM / 注释与
非 data 字段跳过 / 多 ``data:`` 拼接）。本系统不引该依赖，按 **W3C SSE 规范**自实现同一
framing（原版 sse.ts:2-9 明示「Framing is spec-strict」）。

保住的 DeepSeek 协议（原版 sse.ts:6-9）：
    - 字面 ``[DONE]`` **会被产出**，让调用方自行决定收尾；
    - 未遇 ``[DONE]`` 就 EOF ⇒ 抛 ``LlmError('STREAM_CLOSED')``；
    - **严格按空行终结才分发事件** ⇒ EOF 处的未终结尾巴是**截断**，不是可 flush 的载荷。
"""

from __future__ import annotations

import codecs
from typing import AsyncIterable, AsyncIterator, Callable, Optional

from ..llm.errors import LlmError

__all__ = ["DONE", "parse_sse"]

#: DeepSeek（与 OpenAI）在最后一个 chunk 后发出的终结载荷。原版 sse.ts:18。
DONE = "[DONE]"


def _strip_bom(text: str) -> str:
    """去掉 UTF-8 BOM（``TextDecoderStream`` 默认行为）。"""
    return text[1:] if text.startswith("\ufeff") else text


async def parse_sse(
    stream: AsyncIterable[bytes],
    on_comment: Optional[Callable[[str], None]] = None,
) -> AsyncIterator[str]:
    """把 SSE 字节流解析成 event ``data`` 载荷。

    [流收尾判据 2026-10-21] ``[DONE]`` 是 **OpenAI 自己的哨兵，不是 SSE 规范**：
    nous（跑 anthropic 模型）、minimax 这些兼容端点直接关连接、不发 ``[DONE]``，
    但它们会正常发 ``finish_reason``。所以**本层不再判协议违约**——EOF 就是流结束，
    完整性由上层按 ``finish_reason`` 判（见 ``translate.translate``）。
    改前这里在 EOF 无条件抛 ``STREAM_CLOSED``，于是 haiku / minimax 的流一律被判成
    「断了」，重试 3 次全失败（实测 `SSE stream ended without [DONE]`）。

    :param stream: 原始 SSE 字节；读取可切在任何位置，包括 UTF-8 序列中间。
    :param on_comment: 可选的传输活动回调；注释**绝不**进入产出载荷流。
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    buf = ""
    data_lines: list[str] = []
    seen_first = False

    async for raw in stream:
        text = decoder.decode(raw, final=False)
        if not seen_first:
            text = _strip_bom(text)
            seen_first = True
        buf += text

        while True:
            newline_at = _find_line_terminator(buf)
            if newline_at is None:
                break
            index, width = newline_at
            line = buf[:index]
            buf = buf[index + width :]
            payload = _handle_line(line, data_lines, on_comment)
            if payload is not None:
                yield payload
                if payload == DONE:
                    return

    # EOF：流自然结束。**不在这里判违约**——有没有 finish_reason 由上层判。
    return


def _find_line_terminator(buf: str) -> Optional[tuple[int, int]]:
    """找下一个行终结符；返回 ``(下标, 宽度)``，未找到返回 None。

    SSE 规范允许 ``CRLF`` / ``LF`` / ``CR`` 三种。``\\r`` 恰在缓冲末尾时**不消费**
    （可能是 ``\\r\\n`` 的前半，须等下一片）。
    """
    for i, ch in enumerate(buf):
        if ch == "\n":
            return i, 1
        if ch == "\r":
            if i + 1 >= len(buf):
                return None  # 可能是 CRLF 前半，等更多字节
            return i, 2 if buf[i + 1] == "\n" else 1
    return None


def _handle_line(
    line: str,
    data_lines: list[str],
    on_comment: Optional[Callable[[str], None]],
) -> Optional[str]:
    """处理一行；返回要产出的事件载荷（空行终结时），否则 None。"""
    if line == "":
        # 空行 = 事件终结。data 缓冲为空则不分发（W3C SSE 规范）。
        if not data_lines:
            return None
        payload = "\n".join(data_lines)
        data_lines.clear()
        return payload
    if line.startswith(":"):
        # 注释：只经 onComment 上报，绝不进载荷流（原版 sse.ts:25）。
        if on_comment is not None:
            try:
                on_comment(line[1:])
            except Exception:  # noqa: BLE001 传输活动回调绝不拖垮解析
                pass
        return None
    if line.startswith("data:"):
        value = line[5:]
        if value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
        return None
    # event: / id: / retry: 等其余字段：本系统不消费，忽略。
    return None
