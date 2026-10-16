# -*- coding: utf-8 -*-
"""期 2 单测 · SSE 解析（framing / [DONE] / 截断）。

判据 3：sse（CRLF / BOM / 多 data 拼接 / 未终结尾=截断 / [DONE] 缺失=STREAM_CLOSED）。
零网络。
"""
from __future__ import annotations

import pytest

from src.model_gateway.llm.errors import LlmError
from src.model_gateway.llm_openai_compat.sse import DONE, parse_sse


async def _feed(*chunks: bytes):
    for chunk in chunks:
        yield chunk


async def _collect(*chunks: bytes, on_comment=None):
    return [p async for p in parse_sse(_feed(*chunks), on_comment)]


async def test_plain_lf_event_yields_data():
    assert await _collect(b"data: hello\n\n", b"data: [DONE]\n\n") == ["hello", DONE]


async def test_crlf_framing():
    assert await _collect(b"data: hello\r\n\r\n", b"data: [DONE]\r\n\r\n") == ["hello", DONE]


async def test_bom_is_stripped():
    assert await _collect(b"\xef\xbb\xbfdata: x\n\n", b"data: [DONE]\n\n") == ["x", DONE]


async def test_multiple_data_lines_join_with_newline():
    assert await _collect(b"data: a\ndata: b\n\n", b"data: [DONE]\n\n") == ["a\nb", DONE]


async def test_comment_lines_are_reported_not_yielded():
    seen: list[str] = []
    out = await _collect(b": ping\n\n", b"data: x\n\n", b"data: [DONE]\n\n", on_comment=seen.append)
    assert out == ["x", DONE]
    assert seen == [" ping"]


async def test_non_data_fields_are_ignored():
    assert await _collect(b"event: message\nid: 7\nretry: 100\ndata: y\n\n", b"data: [DONE]\n\n") == ["y", DONE]


async def test_unterminated_tail_at_eof_is_truncation_not_payload():
    """未以空行终结的尾巴是**截断**，不得当作载荷分发 ⇒ 最终 STREAM_CLOSED。"""
    with pytest.raises(LlmError) as exc:
        await _collect(b"data: {\"a\":1}\n")
    assert exc.value.code == "STREAM_CLOSED"


async def test_missing_done_raises_stream_closed():
    with pytest.raises(LlmError) as exc:
        await _collect(b"data: x\n\n")
    assert exc.value.code == "STREAM_CLOSED"


async def test_multibyte_utf8_split_across_reads():
    raw = "data: 数据\n\n".encode("utf-8")
    mid = len(raw) // 2
    assert await _collect(raw[:mid], raw[mid:], b"data: [DONE]\n\n") == ["数据", DONE]


async def test_cr_at_buffer_end_waits_for_possible_lf():
    """``\\r`` 恰在缓冲末尾时不得消费（可能是 CRLF 前半）。"""
    assert await _collect(b"data: z\r", b"\n\r\n", b"data: [DONE]\n\n") == ["z", DONE]
