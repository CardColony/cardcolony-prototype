# -*- coding: utf-8 -*-
"""迭代四 F1 · 可读性（样式线）验收网（U-1~U-5，全部 grep/解析机械判）。

六病灶：字号碎档／px 行高／行宽无上界／负边距／--dimmer 机制化／间距刻度。
豁免口径（任务书 F1①⑥）：画布区（.card-node 及其注入徽章 .kid-badge /
.cross-link-badge）字号与间距零改动——断言按选择器归属豁免，不按行号（行号会漂）。

[首页两态 2026-10-15] 原项目列表页的豁免选择器（.book/.metho-* 等）随该页整体退役
一并删除：对应 CSS 段已从 style.css 摘净，豁免若留着就是永不生效的死规则。
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "static" / "style.css"

ALLOWED_FONT_PX = {"11", "12", "13", "14", "18", "36"}
CANVAS_SEL = re.compile(r"\.(card-node|kid-badge|cross-link-badge|tname|msg-badge)\b")


def _rules():
    """把 style.css 解析成 [(选择器, 声明文本, 所在行)] 列表（容忍多行选择器）。"""
    text = CSS.read_text(encoding="utf-8")
    out = []
    buf_sel, buf_body, start = "", "", 0
    depth = 0
    for ln, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if depth == 0 and "{" in line and not stripped.startswith("/*"):
            buf_sel = line.split("{")[0]
            buf_body = line.split("{", 1)[1]
            start = ln
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                depth = 0
                out.append((buf_sel, buf_body, start))
                buf_sel, buf_body = "", ""
            continue
        if depth > 0:
            buf_body += "\n" + line
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                depth = 0
                out.append((buf_sel, buf_body, start))
                buf_sel, buf_body = "", ""
    return out


def _decl_blocks():
    text = CSS.read_text(encoding="utf-8")
    # 单行形态的规则（`sel { decl; }`）与多行形态统一按整文本正则判
    return text


def test_u1_font_sizes_within_allowed_set():
    """U-1：非豁免区字号唯一值 ⊆ {11,12,13,14,18,36}px。"""
    bad = []
    for sel, body, ln in _rules():
        if CANVAS_SEL.search(sel):
            continue
        for m in re.finditer(r"font-size:\s*([0-9.]+)px", body):
            if m.group(1) not in ALLOWED_FONT_PX:
                bad.append(f"L{ln} {sel.strip()[:50]} font-size: {m.group(1)}px")
    assert not bad, "越集字号（非画布）: " + "; ".join(bad)


def test_u2_no_px_line_height():
    """U-2：px 行高归零（全部改倍数）。"""
    hits = [ln for ln, line in enumerate(CSS.read_text(encoding="utf-8").split("\n"), 1)
            if re.search(r"line-height:\s*[0-9.]+px", line)]
    assert not hits, f"残留 px 行高行: {hits}"


def test_u3_line_width_capped():
    """U-3：720px 固定气泡机制归零、统一内容列带门槛（min(88%, 62em)）。

    [对话区会话流样式 2026-10-12] .msg/.step-row/.sys-* 全部退役 min(82%, 720px)，
    改由 .msgs 统一内容列约束——U-3 立法意图（行宽必有上界）由 em 上界继承，
    测试断言同步为新机制。
    [2026-10-12 23:18 用户裁定] 底卡压缩时文字区少缩、先裁两侧留白 →
    .msgs 百分比 70%→82%，断言同步。
    [对话流全消息类型 2026-10-13 用户 00:07] 「模型消息整体的宽度，再宽一点，
    现在太局促了」→ 上界 48em→62em、百分比 82%→88%，断言同步。
    """
    t = CSS.read_text(encoding="utf-8")
    assert t.count("max-width: 82%") == 0
    assert t.count("min(82%, 720px)") == 0
    assert t.count("min(82%, 48em)") == 0
    assert t.count("min(88%, 62em)") >= 1


def test_u4_no_negative_margin_tighten():
    """U-4：`margin-top: -9px` 归零；`.step-row + .step-row` 邻接收紧选择器零命中。"""
    t = CSS.read_text(encoding="utf-8")
    assert t.count("margin-top: -9px") == 0
    assert t.count(".step-row + .step-row") == 0


def test_u4b_chat_gap_and_step_row_zero_margin():
    """配套：.chat gap 收 8px；.step-row 显式 margin-top: 0。"""
    t = CSS.read_text(encoding="utf-8")
    m = re.search(r"\.chat\s*\{[^}]*\}", t, re.S)
    assert m and "gap: 8px" in m.group(0)
    m2 = re.search(r"\.step-row\s*\{[^}]*\}", t, re.S)
    assert m2 and re.search(r"margin-top:\s*0", m2.group(0))


def test_dimmer_mechanized():
    """`var(--dimmer)` 引用归零（现场实测本就 0——机制化=定义行改名 --stroke-soft）；定义在位。"""
    t = CSS.read_text(encoding="utf-8")
    assert t.count("var(--dimmer)") == 0
    assert "--stroke-soft:" in t
    # 机制化语义：注释声明「仅限边框／分隔线，禁用于 color」
    m = re.search(r"--stroke-soft:[^;]*;([^\n]*)", t)
    assert m and "仅限边框" in m.group(1) and "禁用于 color" in m.group(1)


def test_spacing_scale_in_three_regions():
    """间距刻度：三区域规则块内 padding/margin/gap 不再出现 5/6/10/14/18px 裸值。"""
    allowed_offscale = set()  # 三区域内不允许出现映射表旧值（一律就高取大）
    bad = []
    for sel, body, ln in _rules():
        if not re.search(r"\.(msg|step-row|sys-[a-z]+|insp-[a-z-]+|nav-[a-z-]+)\b", sel):
            continue
        if CANVAS_SEL.search(sel):
            continue
        for m in re.finditer(
                r"(?:padding|margin|gap)(?:-[a-z]+)?:\s*([^;]+);", body):
            for v in m.group(1).split():
                vm = re.fullmatch(r"(\d+(?:\.\d+)?)px", v)
                if vm and vm.group(1) in {"5", "6", "10", "14", "18"}:
                    bad.append(f"L{ln} {sel.strip()[:40]} {m.group(0).strip()[:50]}")
    assert not bad, "三区域残留旧刻度值: " + "; ".join(bad[:10])
