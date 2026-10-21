# -*- coding: utf-8 -*-
"""文本机械清洁（照抄 dsh `context/text-clean.ts`）。

判据：
T1 完整思考段 <think>…</think> 全剥（跨行、大小写）
T2 只有结尾标记 ⇒ 从头剥到标记之后
T3 只有开头标记（未闭合）⇒ 从标记剥到串尾
T4 中文【think】形态
T5 token 字面量整段剥；**普通 HTML 标签不动**
T6 无 `>` 结尾的残段剥除
T7 裸 ▁ 一律剥除（`of-` 残片这类）
T8 join_text_blocks 只取 text 块
"""
from __future__ import annotations

from src.api import text_clean as tc


def test_full_think_block():
    assert tc.clean_text("前<think>内部思考\n第二行</think>后") == "前后"
    assert tc.clean_text("<THINK>x</THINK>正文") == "正文"


def test_close_marker_only():
    assert tc.clean_text("残留思考</think>正文") == "正文"


def test_open_marker_only():
    assert tc.clean_text("正文<think 未闭合的思考") == "正文"


def test_chinese_think():
    assert tc.clean_text("正文【think】内部") == "正文"


def test_token_literals_stripped_but_html_kept():
    assert tc.clean_text("a<｜begin▁of▁sentence｜>b") == "ab"
    assert tc.clean_text("a<foo▁bar>b") == "ab"
    # 普通标签不动
    assert tc.clean_text("a<div>b</div>") == "a<div>b</div>"


def test_unclosed_token_tail():
    assert tc.clean_text("正文<｜begin▁of▁sentence｜") == "正文"


def test_bare_sentinel_removed():
    assert tc.clean_text("of-sentence｜>") == "of-sentence｜>"
    assert tc.clean_text("a▁b") == "ab"


def test_join_text_blocks():
    blocks = [
        {"type": "text", "text": "甲"},
        {"type": "reasoning", "text": "思考"},
        {"type": "tool_call", "text": "调用"},
        {"type": "text", "text": "乙"},
    ]
    assert tc.join_text_blocks(blocks) == "甲乙"


def test_blank_after_clean():
    # dsh 口径：isBlankAfterClean 判的是**已清洁**的文本（不内部再清洁）
    assert tc.is_blank_after_clean(tc.clean_text("<think>x</think>   ")) is True
    assert tc.is_blank_after_clean("有字") is False
    assert tc.is_blank_after_clean("   ") is True
