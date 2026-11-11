# -*- coding: utf-8 -*-
"""**内联思考标签**的识别与剥离（共享件，2026-11-10 从 simple_service 下沉）。

**为什么下沉**：`<think>…</think>` 内联在 content 里的形态（MiniMax M 系实测
全系带、部分端点的 `<thinking>` 写法）原来只在三席路径剥——**卡执行主路径**
（model_service → message_runner → 卡面/会话）一个字都没剥，思考连同标签
原样进了正文、实时逐字流、会话块（用户实踩：卡的内部推理以 `<think>` 开头
整段贴在卡面上）。

剥法分两层，全消费方通用：

  · **流上切分**（``ThinkStreamSplitter``）：逐帧把思考增量与正文增量分开——
    标签可能跨帧切裂，残片留缓冲等下一帧补齐再判。
  · **整段剥离**（``strip_inline_think``）：非流式结果/终帧正文一次剥净；
    未闭合的思考区从开标签起丢（中断/截断不把半截思考当正文）。

调用方注意：剥下的思考归「思考通道」（reasoning_chunk / reasoning_content），
**正文通道永远不该见到标签或思考文本**。
"""
from __future__ import annotations

import re

#: 各家思考块形态表（探针实测 2026-10-12；原为 simple_service._THINK_TAGS）
_THINK_TAGS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),          # minimax-m3 实测形态
    ("<thinking>", "</thinking>"),    # 部分端点的写法，一并适配
)
_THINK_OPEN = tuple(p for p, _q in _THINK_TAGS)
_THINK_CLOSE = tuple(q for _p, q in _THINK_TAGS)
_ALL_THINK_TAGS = tuple(sorted({t for pair in _THINK_TAGS for t in pair},
                               key=len, reverse=True))
_MAX_TAG_LEN = max(len(t) for t in _ALL_THINK_TAGS)
#: 闭合思考块：成对整段剥离
_THINK_BLOCK_RES = tuple(
    re.compile(re.escape(p) + r".*?" + re.escape(q), re.DOTALL) for p, q in _THINK_TAGS
)


class ThinkStreamSplitter:
    """把**内联在正文流里**的思考块按标签边界切出来（逐帧有状态，纯逻辑可单测）。

    标签可能跨帧切裂（"<thi" + "nk>…"），故帧尾不足一个标签的残片留在缓冲，
    等下一帧补齐再判；缓冲也可能是标签前缀（"</think>" 的 "</thin"），只有
    真正补齐了才判定。in_think 期间到闭合标签之间的文本＝思考；之外＝正文。
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    @property
    def in_think(self) -> bool:
        return self._in_think

    @property
    def pending(self) -> str:
        """尚在缓冲、不足以判定的残片长度（诊断用）。"""
        return self._buf

    def feed(self, delta: str) -> tuple[str, str]:
        """吃一帧正文增量 → (思考增量, 正文增量)。两路都只含**标签之间的字**，不含标签本身。"""
        buf = self._buf + (delta or "")
        think_parts: list[str] = []
        body_parts: list[str] = []
        while buf:
            idx = -1
            tag = ""
            for t in _ALL_THINK_TAGS:
                i = buf.find(t)
                if i >= 0 and (idx < 0 or i < idx):
                    idx, tag = i, t
            if idx < 0:
                keep = self._partial_tag_len(buf)
                rest, self._buf = buf[:len(buf) - keep], buf[len(buf) - keep:]
                (think_parts if self._in_think else body_parts).append(rest)
                break
            before, buf = buf[:idx], buf[idx + len(tag):]
            (think_parts if self._in_think else body_parts).append(before)
            self._in_think = tag in _THINK_OPEN
        return "".join(think_parts), "".join(body_parts)

    def flush(self) -> tuple[str, str]:
        """流结束：把缓冲残片按当前归属吐出（截断帧不因残片被吞字）。"""
        rest, self._buf = self._buf, ""
        return (rest, "") if self._in_think else ("", rest)

    @staticmethod
    def _partial_tag_len(buf: str) -> int:
        """buf 尾部若是某个标签的真前缀，返回该前缀长度（否则 0）。"""
        for k in range(min(len(buf), _MAX_TAG_LEN - 1), 0, -1):
            tail = buf[-k:]
            if any(t.startswith(tail) for t in _ALL_THINK_TAGS):
                return k
        return 0


def strip_inline_think(text: str) -> str:
    """整段剥思考（纯函数）：闭合块剥掉；未闭合思考区从开标签起丢、之前正文保留。

    [拆雷① 2026-10-12] 未闭合不再整段作废；整段都是思考（开标签在开头且
    无闭合）才返回空串。两种标签写法一视同仁。剥思考**不剥别的**——
    建卡协议块等由调用方各自的清洗函数继续处理。
    """
    text = text or ""
    for rx in _THINK_BLOCK_RES:
        text = rx.sub("", text)
    idx = -1
    for p in _THINK_OPEN:
        i = text.find(p)
        if i >= 0 and (idx < 0 or i < idx):
            idx = i
    if idx >= 0:
        text = text[:idx]
    return text
