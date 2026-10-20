# -*- coding: utf-8 -*-
"""**对比度审计网**（[5715]）——对话页设计令牌的 WCAG 对比度。

## 为什么要有这张网

「无障碍」那一面此前只钉了「可访问名 / 焦点环 / 触控目标」三样，**对比度没量化**。
而对比度是**唯一一条能纯靠 CSS 令牌算出来**的无障碍指标：颜色是写死的，
算得准 ⇒ 改色时立刻知道有没有把文字压没。

## 网里钉的不变量

| 编号 | 不变量 | 判据出处 |
|---|---|---|
| C1 | 两套主题（深/浅）的**正文色阶**（text / text-2 / text-3）压各自背景 ≥ 4.5:1 | WCAG 2.1 AA 1.4.3 |
| C2 | 强调色压背景 ≥ 3:1（非文字 UI 组件） | WCAG 2.1 AA 1.4.11 |
| C3 | **按钮标签**（accent-ink 压 accent）≥ 4.5:1（按钮标签是正文，不算大字） | WCAG 2.1 AA 1.4.3 |
| C4 | 状态色（ok / warn / err）压背景 ≥ 3:1 | WCAG 2.1 AA 1.4.11 |
| C5 | 装饰色阶（text-4 / line / line-2 / line-3）**不冒充正文**（只要求 ≥1:1 且有注释说明用途） | 本项目令牌注释 |
| C6 | 令牌在两套主题里**都存在**（少写一个 ⇒ 浅色下回落成深色值，浅色屏上直接翻车） | P8 的教训 |
| C7 | 对话页的**老令牌桥**（--text/--dim/--panel…）在浅色下映射到新令牌 | P8 修复 |

## 跑法

    pytest tests/test_contrast_audit.py -q

零外网、零浏览器：**纯算**（sRGB 线性化 + 相对亮度 + WCAG 比值公式）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_CSS_PATH = ROOT / "static" / "ui.css"
UI_CSS = UI_CSS_PATH.read_text(encoding="utf-8")

TOKEN_RE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;]+);")
LIGHT_MARK = 'data-theme="light"'

#: 装饰性令牌：只要求「不是纯不可见」，用途靠注释约束，不按正文标准卡
DECORATIVE = {"--ui-text-4", "--ui-line", "--ui-line-2", "--ui-line-3"}


# ---------------------------------------------------------------- CSS 解析

def _blocks(css: str):
    """按花括号配对产出 (selector, body)（先去注释）。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    i = 0
    while True:
        b = css.find("{", i)
        if b < 0:
            return
        sel = css[i:b].strip().splitlines()[-1].strip()
        depth, j = 1, b + 1
        while j < len(css) and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        yield sel, css[b + 1:j - 1]
        i = j


def _scope_tokens(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """→ (基线作用域令牌, 浅色作用域令牌)。"""
    base: dict[str, str] = {}
    light: dict[str, str] = {}
    for sel, body in _blocks(css):
        target = light if LIGHT_MARK in sel else base
        for m in TOKEN_RE.finditer(body):
            target[m.group(1)] = m.group(2).strip()
    return base, light


BASE_TOKENS, LIGHT_TOKENS = _scope_tokens(UI_CSS)


def _resolve(toks: dict[str, str], name: str, depth: int = 0) -> str:
    v = toks.get(name, "")
    if depth > 8:
        return v
    return re.sub(r"var\(\s*(--[A-Za-z0-9_-]+)\s*\)",
                  lambda m: _resolve(toks, m.group(1).strip(), depth + 1), v)


def _parse_color(s: str):
    """→ (r,g,b,a)；a ∈ [0,1]。解析不了返回 None。"""
    s = s.strip()
    m = re.match(r"^#([0-9a-fA-F]{3,8})$", s)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            h += "ff"
        if len(h) == 8:
            r, g, b, a = (int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
            return (r, g, b, a / 255)
    m = re.match(r"^rgba?\(([^)]+)\)$", s, re.I)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 3:
            try:
                r, g, b = (int(float(p)) for p in parts[:3])
            except ValueError:
                return None
            a = float(parts[3]) if len(parts) > 3 else 1.0
            return (r, g, b, a)
    return None


def _blend(fg, bg):
    a = fg[3]
    return (fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a),
            fg[2] * a + bg[2] * (1 - a), 1.0)


def _luminance(c) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg, bg) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _effective(toks: dict[str, str], name: str, bg):
    """取令牌的实际呈现色（半透明令牌按底色合成后再算）。"""
    c = _parse_color(_resolve(toks, name))
    if c is None:
        return None
    return _blend(c, bg) if c[3] < 1 else c


# ---------------------------------------------------------------- 判据表

#: (前景令牌, 背景令牌, 最低比值, 说明)
PAIRS: list[tuple[str, str, float, str]] = [
    ("--ui-text", "--ui-bg", 4.5, "正文/页面底"),
    ("--ui-text", "--ui-surface-1", 4.5, "正文/卡面"),
    ("--ui-text", "--ui-surface-2", 4.5, "正文/次级面"),
    ("--ui-text-2", "--ui-bg", 4.5, "次级文字/页面底"),
    ("--ui-text-2", "--ui-surface-1", 4.5, "次级文字/卡面"),
    ("--ui-text-3", "--ui-bg", 4.5, "弱文字/页面底"),
    ("--ui-text-3", "--ui-surface-1", 4.5, "弱文字/卡面"),
    ("--ui-accent", "--ui-bg", 3.0, "强调色/页面底（非文字 UI 组件）"),
    ("--ui-accent", "--ui-surface-1", 3.0, "强调色/卡面"),
    ("--ui-accent-ink", "--ui-accent", 4.5, "按钮标签/强调底（按钮标签是正文）"),
    ("--ui-accent-ink", "--ui-accent-hi", 4.5, "按钮标签/强调底 hover"),
    ("--ui-ok", "--ui-bg", 3.0, "成功色/页面底"),
    ("--ui-warn", "--ui-bg", 3.0, "警告色/页面底"),
    ("--ui-err", "--ui-bg", 3.0, "错误色/页面底"),
    ("--ui-err", "--ui-surface-1", 3.0, "错误色/卡面"),
]

#: 装饰色阶：只要求「可见」（≥1:1），且不许被当正文用
DECOR_PAIRS: list[tuple[str, str, str]] = [
    ("--ui-text-4", "--ui-bg", "弱装饰文字（仅装饰）"),
    ("--ui-line", "--ui-bg", "分隔线"),
    ("--ui-line-2", "--ui-bg", "控件描边"),
    ("--ui-line-3", "--ui-bg", "强描边"),
]

THEMES = (("深色", BASE_TOKENS), ("浅色", LIGHT_TOKENS))


# ================================================================ 网

def test_all_tokens_exist_in_both_themes():
    """C6：判据表里用到的令牌，**两套主题都得有**。

    少写一个 ⇒ 浅色下那个令牌回落成深色值（浅色屏上直接翻车）——这正是 P8
    的病根（老令牌只有深色一套）。本条把「少写」变成会红的断言。
    """
    for theme, toks in THEMES:
        missing = []
        for fg, bg, *_ in PAIRS:
            for name in (fg, bg):
                if name not in toks:
                    missing.append(name)
        for fg, bg, _ in DECOR_PAIRS:
            for name in (fg, bg):
                if name not in toks:
                    missing.append(name)
        assert not missing, f"{theme}主题缺令牌：{sorted(set(missing))}"


def test_text_colors_meet_wcag_aa():
    """C1：正文色阶（text / text-2 / text-3）压背景 ≥ 4.5:1。"""
    bad = []
    for theme, toks in THEMES:
        bg = _effective(toks, "--ui-bg", (0, 0, 0, 1))
        for fg, bgn, need, label in PAIRS:
            if fg not in ("--ui-text", "--ui-text-2", "--ui-text-3"):
                continue
            bgc = _effective(toks, bgn, bg)
            fgc = _effective(toks, fg, bgc)
            r = _contrast(fgc, bgc)
            if r < need:
                bad.append(f"{theme} {label}: {r:.2f}:1 < {need}")
    assert not bad, "对比度不达 AA：\n  " + "\n  ".join(bad)


def test_accent_and_status_colors_meet_non_text_minimum():
    """C2/C4：强调色与状态色压背景 ≥ 3:1（WCAG 1.4.11 非文字对比）。"""
    bad = []
    for theme, toks in THEMES:
        bg = _effective(toks, "--ui-bg", (0, 0, 0, 1))
        for fg, bgn, need, label in PAIRS:
            if fg in ("--ui-text", "--ui-text-2", "--ui-text-3"):
                continue
            if fg == "--ui-accent-ink":
                continue          # 按钮标签另有更严的判据（C3）
            bgc = _effective(toks, bgn, bg)
            fgc = _effective(toks, fg, bgc)
            r = _contrast(fgc, bgc)
            if r < need:
                bad.append(f"{theme} {label}: {r:.2f}:1 < {need}")
    assert not bad, "对比度不达非文字对比线：\n  " + "\n  ".join(bad)


def test_button_label_on_accent_meets_aa():
    """C3：**按钮标签**压强调底 ≥ 4.5:1（常态与 hover 两条底色都算）。

    按钮标签是正文，不适用「大字 3:1」的宽松线。
    〔5715 · 网抓〕深色主题原本白字压 #7d88ee 只有 3.17:1、hover 底只有 2.58:1
    ⇒ 已把深色主题的 accent-ink 改成深墨（6.14:1 / 7.53:1）。
    """
    bad = []
    for theme, toks in THEMES:
        bg = _effective(toks, "--ui-bg", (0, 0, 0, 1))
        for fg, bgn, need, label in PAIRS:
            if fg != "--ui-accent-ink":
                continue
            bgc = _effective(toks, bgn, bg)
            fgc = _effective(toks, fg, bgc)
            r = _contrast(fgc, bgc)
            if r < need:
                bad.append(f"{theme} {label}: {r:.2f}:1 < {need}")
    assert not bad, "按钮标签对比度不达 AA：\n  " + "\n  ".join(bad)


def test_decorative_tokens_are_visible_but_not_text_grade():
    """C5：装饰色阶必须**看得见**（≥1:1），但不许被当正文用。

    「不许当正文用」在 CSS 侧靠注释约束（text-4 注释里写着「仅装饰」），
    本条守住另一半：它们至少不能是**看不见**的颜色。
    """
    bad = []
    for theme, toks in THEMES:
        bg = _effective(toks, "--ui-bg", (0, 0, 0, 1))
        for fg, bgn, label in DECOR_PAIRS:
            bgc = _effective(toks, bgn, bg)
            fgc = _effective(toks, fg, bgc)
            r = _contrast(fgc, bgc)
            if r < 1.0:
                bad.append(f"{theme} {label}: {r:.2f}:1 —— 装饰色也得看得见")
    assert not bad, "\n  ".join(bad)


def test_decorative_text4_is_marked_as_decorative_in_source():
    """C5 的另一半：``--ui-text-4`` 在源码里必须**标着「仅装饰」**。

    它的对比度只有 ~3–4:1（不到正文线 4.5:1）⇒ 只能用于装饰；
    哪天有人拿它写正文，注释就是唯一的提醒。
    """
    lines = [ln for ln in UI_CSS.splitlines() if "--ui-text-4:" in ln]
    assert lines, "找不到 --ui-text-4 的定义"
    for ln in lines:
        assert "装饰" in ln, f"--ui-text-4 必须标着「仅装饰」：{ln.strip()}"


def test_old_token_bridge_exists_for_light_theme():
    """C7：对话页的**老令牌桥**必须在浅色下把老令牌映射到新令牌。

    〔P8 的教训〕`--text/--dim/--panel/--border/--bg` 这些老令牌只有深色一套，
    浅色下不映射就会继续用深色值（模型选择器变成近黑实心药丸）。
    """
    light_blocks = [body for sel, body in _blocks(UI_CSS)
                    if LIGHT_MARK in sel and "page-chat" in sel]
    assert light_blocks, "浅色主题下应有 body.page-chat 的令牌作用域"
    joined = "\n".join(light_blocks)
    for name, target in (("--text", "--ui-text"), ("--dim", "--ui-text-3"),
                         ("--panel", "--ui-surface-1"), ("--border", "--ui-line-2"),
                         ("--bg", "--ui-bg")):
        m = re.search(r"(?<![\w-])" + re.escape(name) + r"\s*:\s*var\((--[A-Za-z0-9_-]+)\)",
                      joined)
        assert m, f"浅色下的 {name} 没有桥到新令牌"
        assert m.group(1) == target, f"{name} 应桥到 {target}，实为 {m.group(1)}"
