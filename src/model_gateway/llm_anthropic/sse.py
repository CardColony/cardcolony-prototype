# -*- coding: utf-8 -*-
"""Anthropic SSE 字节流 → 事件 dict 流。

Anthropic 的 SSE 帧是 ``event: <type>`` ＋ ``data: <json>`` 双行、空行分隔；
事件负载在 ``data`` 行里自带 ``type`` 字段，``event:`` 行只是标签，故忽略。
与 ``llm_openai_compat/sse.py`` 的分界：那边切 OpenAI 形状的
``data: {...}``＋``[DONE]`` 哨兵；这边无哨兵、以 ``message_stop`` 事件终结
（由 translate 负责），两套 wire 不共享解析。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

__all__ = ["parse_sse"]


async def parse_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """把 HTTP 响应体切成 Anthropic 事件 dict 流。"""
    data_lines: list[str] = []
    async for raw in response.aiter_bytes():
        for line in raw.decode("utf-8", "replace").splitlines():
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        event = json.loads(payload)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(event, dict):
                        yield event
                continue
            if line.startswith(":"):  # 注释/心跳
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            # event: 行不带负载，忽略（负载 data 里有 type 字段）
