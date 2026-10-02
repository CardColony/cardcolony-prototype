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


def split_body_sections(body: str) -> dict[str, str]:
    """把正文按四区拆分：## 职责区 / ## 结论区 / ## 否决记录区 / ## 修改历史。

    旧卡无 ## 职责区 时 charter 为空字符串（不报错）。
    """
    sections = {"charter": "", "conclusion": "", "rejections": "", "history_text": ""}
    mapping = {
        "## 职责区": "charter",
        "## 结论区": "conclusion",
        "## 否决记录区": "rejections",
        "## 修改历史": "history_text",
    }
    current: Optional[str] = None
    buf: dict[str, list[str]] = {v: [] for v in mapping.values()}
    for line in body.splitlines():
        stripped = line.strip()
        if stripped in mapping:
            current = mapping[stripped]
            continue
        if current:
            buf[current].append(line)
    for k, v in buf.items():
        sections[k] = "\n".join(v).strip("\n")
    return sections
