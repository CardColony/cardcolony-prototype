# -*- coding: utf-8 -*-
"""建卡协议与观点清洗：纯函数层，模型内容进入卡片树的唯一入口。

这两处是「模型输出 → 系统结构」的解析面，任何解析回退都会静默丢卡或
把协议块写进正文。纯函数、零依赖、零外网，最适合当回归网的钉子。
"""
from __future__ import annotations

from src.api.card_proposal import parse_proposals
from src.api.simple_service import clean_viewpoint_text


# ---------------------------------------------------------------- 建卡协议

def test_fenced_form_is_parsed_and_stripped():
    txt = ('先说明一下。\n\n```card-proposal\n'
           '[{"title":"甲","parent":"card_001","content":"正文"}]\n```\n收尾。')
    clean, props = parse_proposals(txt)
    assert props == [{"title": "甲", "parent": "card_001", "content": "正文"}]
    assert "card-proposal" not in clean
    assert "收尾" in clean and "先说明一下" in clean


def test_leaked_angle_forms_are_parsed():
    # 全角双竖线（DeepSeek 特殊 token 的可打印近似）
    _, props = parse_proposals(
        '<｜｜card-proposal｜｜>\n[{"title":"乙"}]\n<｜｜/card-proposal｜｜>')
    assert props == [{"title": "乙", "parent": "", "content": ""}]

    # 纯 ASCII 零竖线（第五形态）
    _, props = parse_proposals('<card-proposal>\n[{"title":"丙"}]\n</card-proposal>')
    assert props[0]["title"] == "丙"

    # 斜杠在前的闭合变体
    _, props = parse_proposals('<|card-proposal|>\n[{"title":"丁"}]\n</card-proposal|>')
    assert props[0]["title"] == "丁"


def test_content_may_contain_code_fence():
    txt = ('```card-proposal\n'
           '[{"title":"甲","content":"```py\\nprint(1)\\n```"}]\n'
           '```')
    _, props = parse_proposals(txt)
    assert props and props[0]["title"] == "甲"
    assert "print(1)" in props[0]["content"]


def test_bad_json_is_skipped_without_losing_body():
    clean, props = parse_proposals("```card-proposal\n{nope}\n```\n正文保留")
    assert props == []
    assert "正文保留" in clean


def test_title_is_mandatory():
    _, props = parse_proposals('```card-proposal\n[{"content":"无标题"}]\n```')
    assert props == []


def test_control_chars_in_content_are_repaired():
    """模型偶发把换行写成真实换行（JSON 规范外），须机械修复而非丢卡。"""
    raw = '```card-proposal\n[{"title":"甲","content":"第一行\n第二行"}]\n```'
    _, props = parse_proposals(raw)
    assert props and props[0]["content"] == "第一行\n第二行"


def test_text_without_protocol_is_unchanged():
    clean, props = parse_proposals("就是一段普通回复。")
    assert props == []
    assert clean.strip() == "就是一段普通回复。"


# ---------------------------------------------------------------- 观点清洗

def test_clean_viewpoint_strips_think_block():
    assert clean_viewpoint_text("结论：可行。") == "结论：可行。"
    # [T-0001 §6-2] 磁盘原字面量的 <think>/</think> 标记被文本通道吃掉；
    # 此处补回真标记：成对块整体剥离，块外只留结论
    assert clean_viewpoint_text("<think>内部推理过程…尚需核查…</think>结论：可行。") == "结论：可行。"


def test_clean_viewpoint_drops_unclosed_think():
    """max_tokens 截断导致 <think> 未闭合：整段不可用，返回空串。"""
    assert clean_viewpoint_text("<think>未闭合的思考，没有结束标记") == ""


def test_clean_viewpoint_strips_proposal_block():
    txt = '观点正文\n```card-proposal\n[{"title":"甲"}]\n```'
    assert clean_viewpoint_text(txt) == "观点正文"