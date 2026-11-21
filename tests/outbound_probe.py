# -*- coding: utf-8 -*-
"""出站抓取器（批二验收共用件）：拦 httpx 出站，抓**适配器实际发出的请求正文**。

抓取点＝``httpx.AsyncClient.send``——它在适配器序列化之后、真正上网之前，
所以抓到的字节就是实际出站的请求体（OpenAI 兼容 chat-completions 与
Responses 两类序列化都在这里被抓到）。

用例与回放脚本共用这一份（``tests/outbound_probe.py``）⇒ 两边的口径逐字一致。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx

LIST_MARK = "〔卡片清单〕"
CHILD_MARK = "〔直接子卡现状〕"
DEP_MARK = "## 依赖关联"


class OutboundRecorder:
    """拦 httpx 出站，记录**实际发送的请求正文**。"""

    def __init__(self, responder: Callable[[int, dict], Any]):
        self.requests: list[dict[str, Any]] = []
        self._responder = responder

    async def send(self, client, request, **kwargs):  # noqa: ANN001
        raw = request.content or b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 非 JSON 体也记下来（字节照样算）
            body = {}
        rec = {"index": len(self.requests), "url": str(request.url),
               "bytes": len(raw), "body": body, "raw": raw}
        self.requests.append(rec)
        out = self._responder(rec["index"], body)
        if isinstance(out, BaseException):
            raise out
        return httpx.Response(200, content=out, request=request)

    def install(self, monkeypatch) -> "OutboundRecorder":
        """挂到 httpx 上。**必须是普通函数**（挂成 bound method 不会重绑 self，
        参数会错位——实测踩过：适配器侧报的是「API request … failed」）。"""
        rec = self

        async def _send(client, request, **kwargs):  # noqa: ANN001
            return await rec.send(client, request, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "send", _send)
        return self

    # -- 取数面 ---------------------------------------------------------
    def texts(self, index: int) -> list[str]:
        """第 index 次出站里所有消息的正文（按 wire 顺序）。"""
        body = self.requests[index]["body"] or {}
        out: list[str] = []
        for m in body.get("messages") or []:
            c = m.get("content")
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, list):
                out.append("".join(str(x.get("text") or "") for x in c
                                   if isinstance(x, dict)))
        return out

    def joined(self, index: int) -> str:
        return "\n".join(self.texts(index))

    def count_mark(self, index: int, mark: str) -> int:
        return self.joined(index).count(mark)

    def system_text(self, index: int) -> str:
        body = self.requests[index]["body"] or {}
        for m in body.get("messages") or []:
            if m.get("role") == "system":
                c = m.get("content")
                return c if isinstance(c, str) else str(c or "")
        return ""

    def input_text(self, index: int) -> str:
        """Responses API（SIWC 座）的 ``input`` 数组文本。"""
        body = self.requests[index]["body"] or {}
        return json.dumps(body.get("input") or [], ensure_ascii=False)


def sse(*frames: dict) -> bytes:
    """OpenAI 兼容 chat-completions 的 SSE 帧序列。"""
    out = ["data: " + json.dumps(fr, ensure_ascii=False) + "\n\n" for fr in frames]
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def frame_text(text: str, model: str = "fake-model") -> dict:
    return {"model": model, "choices": [{"index": 0, "delta": {"content": text}}]}


def frame_tool(call_id: str, name: str, args: dict, model: str = "fake-model") -> dict:
    return {"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": call_id, "type": "function",
         "function": {"name": name,
                      "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


def frame_finish(reason: str = "tool_calls", model: str = "fake-model") -> dict:
    return {"model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def lcp_bytes(a: str, b: str) -> int:
    """两段文本的最长公共前缀（UTF-8 字节）。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return len(a[:i].encode("utf-8"))


def seg_bytes(joined: str, mark: str, header: str) -> int:
    """某份材料在出站正文里的字节数。

    口径：tail 档的材料是**独立消息**（按消息边界算）；system 档的材料是区段
    （从区头到下一个区头）。
    """
    if mark and mark in joined:
        i = joined.find(mark)
        start = joined.rfind("\n", 0, i) + 1
        j = joined.find("\n", i)
        nxt = len(joined)
        for other in (CHILD_MARK, LIST_MARK):
            k = joined.find(other, i + 1)
            if k > 0:
                nxt = min(nxt, joined.rfind("\n", 0, k) + 1)
        seg = joined[start:nxt] if nxt > start else joined[start:]
        return len(seg.encode("utf-8"))
    i = joined.find(header)
    if i < 0:
        return 0
    j = joined.find("\n## ", i + 1)
    seg = joined[i:j if j > 0 else len(joined)]
    return len(seg.encode("utf-8"))


class TurnScript:
    """按请求内容回包（不靠次数）：带工具结果 ⇒ 续跳；否则 ⇒ 首跳。"""

    def __init__(self, *, fail_first: bool = False, on_first_hop=None):
        self.fail_first = fail_first
        self.on_first_hop = on_first_hop
        self.served = 0

    def __call__(self, index: int, body: dict) -> Any:
        msgs = body.get("messages") or []
        has_tool_result = any(m.get("role") == "tool" for m in msgs)
        if has_tool_result:
            return sse(frame_text("已按判据核对完毕。"), frame_finish("stop"))
        if self.fail_first and self.served == 0:
            self.served += 1
            raise httpx.ConnectError("（替身）首试断连——触发同位置重发")
        if self.on_first_hop is not None:
            self.on_first_hop()
        return sse(
            frame_tool("call_1", "read_card", {"card_id": "", "block": "现状"}),
            frame_finish("tool_calls"),
        )
