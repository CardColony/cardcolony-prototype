# -*- coding: utf-8 -*-
"""文本机械清洁（照抄 dsh `gen3/context/text-clean.ts`；叶子件，零 import）。

## 为什么要有它（dsh 总工拍板 §0-3，逐字保留其意）

> 「**阻止非我想要的信息污染轮内信息**，比如很多模型的思考信息」

本件只做**机械产物清除**，**不做任何语义判断/改写**。

## dsh 2026-09-16 终修的教训（照抄，别退回去）

此前多轮"加正则"始终剥不净（`of-` 残片反复出现、导致模型 echo 异常）。
**根因＝拆段匹配**（先剥前半、再剥后半，中间留残段）。⇒ **整段一次剥**。

## 适用范围（dsh 原注）

**只用于 assistant 产出与历史表文本；用户消息原文不清。**

## 剥什么

1. 思考段：`<think>…</think>` 全族（含只有结尾标记、只有开头标记、中文 `【think】` 三种残缺形态）；
2. token 字面量：`<…▁…>` / `<｜…>` 从 `<` 到最近的 `>` **整段剥**（不含 ▁/｜ 的普通标签不动）；
3. 无 `>` 结尾的残段（token 字面量断在行尾）；
4. **裸 `▁`** 一律剥除（tokenizer 专用符，正文不会出现 ⇒ 最简、无副作用）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# ① 完整形态 <think>…</think>（大小写不敏感、跨行）
_THINK_FULL_RE = re.compile(r"<think[\s\S]*?</think>", re.IGNORECASE)
# ② 只有结尾标记 / ③ 只有开头标记
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think", re.IGNORECASE)
# ③b 中文方括号形态
_THINK_CN = "【think】"
# ④ token 字面量：<…> 且中间含 ▁ 或 ｜
_TAG_RE = re.compile(r"<[^<>\n]{0,256}>")
_TOKEN_CHARS_RE = re.compile(r"[\u2581\uff5c]")
# ⑤ 无 > 结尾的残段
_TOKEN_TAIL_RE = re.compile(r"<[^<>\n]{0,128}[\u2581\uff5c][^<>\n]{0,128}(?=\n|$)")
# ⑥ 裸 ▁
_BARE_SENTINEL_RE = re.compile("\u2581")


def clean_text(raw: str) -> str:
    """剥思考段与特殊 token 字面量；返回清洁后文本（可能为空串）。"""
    t = str(raw or "")

    # ── 思考段 ──
    t = _THINK_FULL_RE.sub("", t)
    close_idx = _THINK_CLOSE_RE.search(t)
    if close_idx is not None and _THINK_OPEN_RE.search(t) is None:
        t = t[close_idx.end():]
    open_idx = _THINK_OPEN_RE.search(t)
    if open_idx is not None:
        t = t[:open_idx.start()]
    cn_idx = t.find(_THINK_CN)
    if cn_idx != -1:
        t = t[:cn_idx]

    # ── token 字面量（整段一次剥，不拆分）──
    t = _TAG_RE.sub(lambda m: "" if _TOKEN_CHARS_RE.search(m.group(0)) else m.group(0), t)
    t = _TOKEN_TAIL_RE.sub("", t)
    t = _BARE_SENTINEL_RE.sub("", t)
    return t


def join_text_blocks(blocks: Iterable[Any]) -> str:
    """从消息块里只取 `type == 'text'` 的块拼正文（reasoning/thinking/tool-call 自然剔除）。"""
    out = ""
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            out += b["text"]
    return out


def is_blank_after_clean(text: str) -> bool:
    """清洁后是否"无实质内容"（纯壳判定；有无工具调用由调用方判）。"""
    return str(text or "").strip() == ""
