# -*- coding: utf-8 -*-
"""对话页宽度网（[P29 · 2026-10-30]）

## 用户的话

「现在的对话页太窄了，加宽一点，**注意输入框要同宽**。」

## 真正的机关在哪（这是本轮最贵的一条认知）

项目里有**两套**横向尺寸规则：

| 套 | 文件 | 是否作用于对话页 | 宽度来源 |
|---|---|---|---|
| **ui.css（新设计系统）** | `static/ui.css` | ✅ **生效**（`body.page-chat` 作用域） | `--ui-col` |
| style.css（dsh 照抄旧规范） | `static/style.css` | ❌ 被 `body.page-chat` 覆盖（死规范） | 三处硬编码 748/780/calc(100%-748px) |

第一遍改宽度时**改到了 style.css 那套**（被覆盖、不生效），结果内容列与
输入卡变成一个 960 一个 768 —— **反而更不齐**。

⇒ 本网的第一条纪律就是：**改横向尺寸前先确认哪一套生效**，
   并用断言把「对话页横向尺寸只有 --ui-col 一个真相源」钉死。

## ui.css 这套本来就是同源的（为什么改一个数就够）

    :602   .flow-view-scroll          padding: 0 var(--ui-col-pad) 28px
    :606   .flow-view-column          max-width: var(--ui-col)          ← 内容列
    :1594  .flow-view-to-bottom-slot   padding-right: calc((100% - var(--ui-col))/2 - 48px)
    :1642  .composer-seat             --flow-composer-card-max-width: var(--ui-col) ← 输入卡
    :1643  .composer-seat             --flow-composer-side-clearance: var(--ui-col-pad)

⇒ 内容列与输入卡引用**同一个变量** ⇒ 改一处必然同宽 ⇒ 天生对不齐不了。

## 本网钉住的不变量

| 编号 | 不变量 |
|---|---|
| W1 | `--ui-col` 存在且已放宽（768 → 960px） |
| W2 | 对话页横向尺寸**只有 `--ui-col` 一个真相源**（六处全引用它） |
| W3 | 内容列 = `var(--ui-col)` |
| W4 | 输入卡 = `var(--ui-col)` ⇒ **输入框与内容列同宽** |
| W5 | 回底钮引用 `--ui-col`（不会飘） |
| W6 | 对话页模板引用 ui.css 且**带版本戳**（漏升＝浏览器吃旧缓存＝改了不生效） |
| W7 | 消息侧与输入侧**都** `scrollbar-gutter: stable`（同宽的第二个机关） |
| W8 | 一次性探针/调试产物已清干净 |

## W7 为什么是独立的一条（W3/W4 管不到它）

W3/W4 保证「内容列和输入卡引用**同一个** `--ui-col`」——
但**同一个宽度值 ≠ 同一个可用宽度**：

    消息列在 .flow-view-scroll（滚动容器）里 ⇒ Chromium 竖向滚动条实占 10px
    输入卡不在滚动容器里                          ⇒ 两边可用宽度天然差 10px

实测（CDP，同一页面 A/B/C 三变体对照）：

| 视口 | 变体 | 内容列 | 输入卡 | 差 |
|---|---|---|---|---|
| 1256（--ui-col 不绑定） | A 已发布（两侧 stable） | 300→1218 w=918 | 300→1218 w=918 | **0px** |
| 1256 | B 修复前（两侧 auto） | 300→1218 w=918 | 300→1228 w=928 | 宽度差 10px |
| 1256 | C 只改一侧 | 300→1218 w=918 | 300→1228 w=928 | 宽度差 10px |
| 1576（--ui-col 绑定 960） | A 已发布 | 439→1399 w=960 | 439→1399 w=960 | **0px** |
| 1576 | B 修复前 | 439→1399 w=960 | 444→1404 w=960 | 右缘差 5px（居中偏移） |

⇒ A 变体两种视口都**像素级完全重合**；B/C 都错开。
⇒ 取证方式：CDP（`_mywork/_p29_measure_cdp.py`）——
   `--dump-dom` 在 load 时刻就 dump、`--screenshot` 反推像素两次给出矛盾值，
   两者都不可作数。

铁律一：源码级断言，不碰真实服务。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_CSS = (ROOT / "static" / "ui.css").read_text(encoding="utf-8")
CARD_HTML = (ROOT / "templates" / "card.html").read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去掉注释——源码级断言不能被注释里的旧魔数骗过。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    return src


UI_CODE = _code_only(UI_CSS)


def _rule(selector: str) -> str:
    """取某选择器的**最后**一个规则块正文。

    ★ 必须取最后一个：CSS 同名选择器**后者覆盖前者**，而 ui.css 里同一个
      选择器可能散着好几段（如 .flow-view-scroll 有滚动条样式段，也有 padding 段）。
      取第一个会读到不相干的那一段 ⇒ 断言假失败。
    """
    blocks = re.findall(re.escape(selector) + r"\s*\{[^}]*\}", UI_CODE, re.S)
    assert blocks, f"ui.css 里找不到 {selector}"
    return blocks[-1]


def test_w1_ui_col_widened():
    """W1：--ui-col 已放宽（768 → 960px）。"""
    assert "--ui-col:" in UI_CODE, "ui.css 缺少 --ui-col（对话页宽度的真相源）"
    m = re.search(r"--ui-col:\s*(\d+)px", UI_CODE)
    assert m, "--ui-col 必须有明确像素值"
    w = int(m.group(1))
    assert w >= 900, f"内容列宽应已放宽（P29：768→960），实得 {w}px"
    assert w <= 1200, f"内容列宽别失控（行宽可读性上界），实得 {w}px"


def test_w2_single_source_of_truth():
    """W2：对话页的横向尺寸只有一个真相源 --ui-col。

    ★ 本轮最大的教训：另有一套 style.css 的 dsh 旧规范（748/780 硬编码），
      在对话页**不生效**。若误改那一套 ⇒ 内容列与输入卡宽度分叉 ⇒ 反而更不齐。
      ⇒ 这里断言：对话页这几个横向尺寸**全部引用 --ui-col**。
    """
    assert "--ui-col" in _rule("body.page-chat .flow-view-column"), "内容列必须引用 --ui-col"
    assert "--ui-col" in _rule("body.page-chat .composer-seat"), "输入卡必须引用 --ui-col"
    assert "--ui-col" in _rule("body.page-chat .flow-view-to-bottom-slot"), "回底钮必须引用 --ui-col"
    assert "--ui-col" in _rule("body.page-chat .flow-view-scroll"), "滚动区留白必须引用 --ui-col-pad"
    # ui.css 里不许再出现裸的旧列宽
    for bad in ("768px", "780px"):
        assert bad not in UI_CODE, f"ui.css 里仍有硬编码 {bad}（宽度必须收敛到 --ui-col）"


def test_w3_content_column_uses_ui_col():
    """W3：内容列 = var(--ui-col)。"""
    blk = _rule("body.page-chat .flow-view-column")
    assert "max-width: var(--ui-col)" in blk, "内容列必须 max-width: var(--ui-col)"


def test_w4_composer_same_width_as_column():
    """W4：输入卡 = var(--ui-col) ⇒ 输入框与内容列**完全同宽**。

    验算（区域宽 A；内容列在滚动区内、左右 padding 各 --ui-col-pad；
          输入卡 clearance 也＝--ui-col-pad ⇒ 两侧留白相同）：
        列左缘 = P + (A-2P-C)/2 = A/2 - C/2
        卡左缘 = P + (A-2P-C)/2 = A/2 - C/2      ⇒ 相同 ✓
    """
    blk = _rule("body.page-chat .composer-seat")
    assert "--flow-composer-card-max-width: var(--ui-col)" in blk, \
        "输入卡宽度必须＝内容列宽（同一个变量）"
    assert "--flow-composer-side-clearance: var(--ui-col-pad)" in blk, \
        "输入卡左右留白必须与内容列滚动区留白一致（否则边缘错开）"


def test_w5_to_bottom_follows_ui_col():
    """W5：回底钮按 --ui-col 算右内边距（列一改宽它自动跟上）。"""
    blk = _rule("body.page-chat .flow-view-to-bottom-slot")
    assert "var(--ui-col)" in blk, "回底钮必须引用 --ui-col"


def test_w6_card_html_pins_ui_css_version():
    """W6：对话页模板引用 ui.css 且带版本戳（漏升＝浏览器吃旧缓存＝改了不生效）。"""
    m = re.search(r"/static/ui\.css\?v=(\d+)", CARD_HTML)
    assert m, "card.html 引用 ui.css 没有版本戳（?v=）"
    assert int(m.group(1)) >= 43, \
        f"ui.css 内容已改（滚动条槽位），戳必须 ≥43（实测 {m.group(1)}）——旧戳＝浏览器吃旧缓存"


def test_w7_both_sides_reserve_same_scrollbar_gutter():
    """W7：消息侧与输入侧**都**用 `scrollbar-gutter: stable` 预留同宽槽位。

    ★ 这是「同宽」的**第二个机关**，W3/W4 只保证「同一个 --ui-col」，
      不保证「同一个可用宽度」：
        消息列在 `.flow-view-scroll`（滚动容器）里 ⇒ Chromium 的竖向滚动条
        实占 **10px**（scrollbar-width:thin，nav.css:86-88 已记此坑）；
        输入卡不在滚动容器里 ⇒ 两边「可用宽度」天然差 10px
        ⇒ 同一个 --ui-col **不等于**同宽（实测右缘差 10.3px）。

    ⇒ 两侧必须用**同一套**机制预留槽位（浏览器同平台给出的槽位宽必然相同）。
    ⚠ 只加一侧更糟：一个恒定、一个随滚动条出现而变 ⇒ 两者互相漂移。
    """
    msg_side = _rule("body.page-chat .flow-view-scroll")
    assert "scrollbar-gutter: stable" in msg_side, \
        "消息侧滚动容器必须 scrollbar-gutter: stable（恒定预留槽位）"
    in_side = _rule("body.page-chat .flow-input-root")
    assert "scrollbar-gutter: stable" in in_side, \
        "输入侧必须同样 scrollbar-gutter: stable（两侧槽位必须同宽，否则输入框与内容列错开）"
    assert "scrollbar-color: transparent transparent" in in_side, \
        "输入侧槽位要隐形（透明轨道＋透明滑块），否则右侧多一条可见滚动条"
    assert "overflow-y: auto" in in_side, \
        "输入侧必须是滚动容器（否则 scrollbar-gutter 不生效）"


def test_w8_no_probe_artifacts_left():
    """W8：一次性探针/调试产物必须清干净（别把量具留在生产里）。"""
    probe = ROOT / "static" / "_p29_probe.css"
    assert not probe.exists(), "P29 探针 CSS 已量完，必须删"
    assert "_p29_probe" not in CARD_HTML, "card.html 里还挂着探针 <link>，必须删"
    measure = ROOT / "static" / "_p29_measure.html"
    assert not measure.exists(), "P29 测量页已用完，必须删"
