# -*- coding: utf-8 -*-
"""卡片内容哈希与 Front Matter 解析工具。

哈希算法（严格按规格）：
  a. 找到文件开头第一对 "---" 行之间的 Front Matter 区域。
  b. 在该区域内删除以 "content_hash:" 开头的整行（含换行符）。
  c. 对处理后的全文做 SHA-256，返回十六进制字符串。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

import yaml


def _front_matter_bounds(lines: list[str]) -> Optional[tuple[int, int]]:
    """返回 Front Matter 定界行 (start_idx, end_idx)，即第一对 '---' 行的下标。"""
    delim = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(delim) >= 2:
        return delim[0], delim[1]
    return None


def calc_card_hash(content: str) -> str:
    """按规格计算卡片内容哈希。"""
    lines = content.splitlines(keepends=True)
    bounds = _front_matter_bounds(lines)
    if bounds is not None:
        s, e = bounds
        kept = (
            lines[: s + 1]
            + [l for l in lines[s + 1 : e] if not l.startswith("content_hash:")]
            + lines[e:]
        )
    else:
        kept = lines
    return hashlib.sha256("".join(kept).encode("utf-8")).hexdigest()


def apply_hash(content: str) -> str:
    """写入前回填哈希：先把 content_hash 行写成 content_hash: ""，
    计算哈希 H，再替换为 content_hash: "H"。只处理 Front Matter 区域内。"""
    lines = content.splitlines(keepends=True)
    bounds = _front_matter_bounds(lines)
    if bounds is None:
        return content
    s, e = bounds
    hash_line_idx = None
    for i in range(s + 1, e):
        if lines[i].startswith("content_hash:"):
            hash_line_idx = i
            break
    if hash_line_idx is None:
        # 不存在则在定界行前插入占位行
        lines.insert(e, 'content_hash: ""\n')
        hash_line_idx = e
    lines[hash_line_idx] = 'content_hash: ""\n'
    digest = calc_card_hash("".join(lines))
    lines[hash_line_idx] = f'content_hash: "{digest}"\n'
    return "".join(lines)


def parse_front_matter(content: str) -> tuple[dict[str, Any], str]:
    """解析 Front Matter，返回 (meta字典, 正文文本)。解析失败返回 ({}, 全文)。"""
    lines = content.splitlines(keepends=True)
    bounds = _front_matter_bounds(lines)
    if bounds is None:
        return {}, content
    s, e = bounds
    fm_text = "".join(lines[s + 1 : e])
    try:
        meta = yaml.safe_load(fm_text) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    body = "".join(lines[e + 1 :])
    return meta, body


def replace_fm_field(content: str, key: str, value_line: str) -> str:
    """把 Front Matter 区域内以 "{key}:" 开头的行替换为 value_line（须自带换行）。"""
    lines = content.splitlines(keepends=True)
    bounds = _front_matter_bounds(lines)
    if bounds is None:
        return content
    s, e = bounds
    for i in range(s + 1, e):
        if lines[i].startswith(f"{key}:"):
            lines[i] = value_line
            return "".join(lines)
    return content


_HEADING_RE = re.compile(r"^## ", re.MULTILINE)

# [卡片信息结构改造 S2-1] 新区名→新键映射。与旧表并存，按正文实际区头自适应。
#   ## 使命 → mission｜## 现状 → status_block｜## 定论 → rulings｜## 修改历史 → history_text
NEW_SECTION_MAPPING = {
    "## 使命": "mission",
    "## 现状": "status_block",
    "## 定论": "rulings",
    "## 修改历史": "history_text",
}

# 旧区名→键（S2-2 规范键归一：旧区头命中时内容归入新键，读侧一律走新键）
OLD_SECTION_MAPPING = {
    "## 职责区": "charter",
    "## 结论区": "conclusion",
    "## 否决记录区": "rejections",
    "## 修改历史": "history_text",
}

# 旧键→新键（S2-2 规范键归一）。history_text 是两表共有键，不在归一名录内。
OLD_KEY_TO_NEW = {
    "charter": "mission",
    "conclusion": "status_block",
    "rejections": "rulings",
}

# 规范新键（读侧返回序；history_text 由调用方转 history 列表）
NEW_KEYS = ("mission", "status_block", "rulings", "history_text")

# 判形态时忽略的共有区头（它不区分新旧）
_COMMON_HEADING = "## 修改历史"


def _detect_form(body: str) -> str:
    """形态判定（S2-3）：按正文实际出现的区头。

    "old"＝只出现旧区名（或无区头——现存无区头卡全是旧形态产物）
    "new"＝只出现新区名｜"mixed"＝两套都出现。
    """
    has_old = any(h in body for h in OLD_SECTION_MAPPING if h != _COMMON_HEADING)
    has_new = any(h in body for h in NEW_SECTION_MAPPING if h != _COMMON_HEADING)
    if has_old and has_new:
        return "mixed"
    return "new" if has_new else "old"


def effective_form(form: str, body: str) -> str:
    """把形态判定收敛成渲染/拆分用的唯一形态 ∈ {"old","new"}。

    混用形态（mixed）取**外层优先**——正文里第一个判别性区头所属的那一套。
    这条是保形的关键（RD-09）：存量旧卡的正文里可能含新区头**字面行**
    （如 card_027 把「## 使命/## 现状/## 定论」写在结论区里讨论本设计），
    若按单一区头表拆，这些字面行会被拽进新区块、重渲染时内容位移＝存量卡损坏。
    外层优先 ⇒ 旧卡内的字面行只是普通内容行，原样留在原区块。
    """
    if form != "mixed":
        return "new" if form == "new" else "old"
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("## ") or s == _COMMON_HEADING:
            continue
        if s in NEW_SECTION_MAPPING:
            return "new"
        if s in OLD_SECTION_MAPPING:
            return "old"
    return "old"


def _section_table(form: str) -> dict[str, str]:
    """按形态取区头→规范新键表。旧区名在此归一（旧键不出现在返回值里）。"""
    if form == "new":
        return dict(NEW_SECTION_MAPPING)
    # old / mixed-outer-old：只用旧区头表，新区头字面行视作内容行
    table = {"## 修改历史": "history_text"}
    for h, oldk in OLD_SECTION_MAPPING.items():
        if h == _COMMON_HEADING:
            continue
        table[h] = OLD_KEY_TO_NEW[oldk]
    return table


def split_body_sections(body: str) -> dict[str, str]:
    """把正文按区头拆分，返回规范新键 + _form 形态标记（S2-1~S2-4）。

    新区名：## 使命 / ## 现状 / ## 定论 / ## 修改历史
    旧区名：## 职责区 / ## 结论区 / ## 否决记录区 / ## 修改历史（旧卡保形，见 RD-09）
    两套并存、自适应：旧卡读出 mission/status_block/rulings，新卡同样；
    混用卡按外层形态归位（effective_form），不报错、不位移内容。
    某区不存在时对应键为空字符串。

    返回第三键 "_form"（"old"/"new"/"mixed"）——**不落盘、不进 read_card 的 body**，
    仅供 _render_card 决定输出哪套区头（G-3/G-7）。渲染前须经 effective_form
    收敛为 old/new，mixed 直接传渲染器会被拒（防混用形态落盘）。
    """
    form = _detect_form(body)
    table = _section_table(effective_form(form, body))
    sections: dict[str, str] = {k: "" for k in NEW_KEYS}
    current: Optional[str] = None
    buf: dict[str, list[str]] = {v: [] for v in NEW_KEYS}
    for line in body.splitlines():
        stripped = line.strip()
        if stripped in table:
            current = table[stripped]
            continue
        if current:
            buf[current].append(line)
    for k, v in buf.items():
        sections[k] = "\n".join(v).strip("\n")
    sections["_form"] = form
    return sections
