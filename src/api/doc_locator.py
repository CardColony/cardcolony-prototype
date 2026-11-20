# -*- coding: utf-8 -*-
"""需求文档正稿的**条款定位**（成本优化任务书 §三·第二步「依赖正稿按可信任务范围缩量」）。

依赖区的目标正稿不再整份内联时，宿主替模型算好「这条关系真正消费的是哪几行」
——**模型不许自己编条款号，也不许拿旧行号当当前条款**（任务书：「禁止让模型
根据标题编造条款」「版本变动后重新取当前版本及坐标，避免把旧行号当成当前条款」）。

两种锚：

- **条款号锚**（A2／R10／S9-R01／G0／H1／V1…）：从关系说明里抽条款号，再到
  目标正稿里找**以该条款号起头**的标题或条目（``### A2 …``／``- **H1 …**``／
  ``| G0 …``）。命中即**精确**。
- **标题短语锚**：关系说明里的中文短语**逐字**出现在目标正稿某个标题里，且
  **只出现在一个标题里** ⇒ 定位到该标题的章节。命中即**可复核**（给了标题
  原文，模型能核对拿到的是哪一节）。

本模块**纯函数**：只吃正文文本，不做 IO、不查库、不认卡号。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "DocRange",
    "doc_fingerprint",
    "extract_clause_anchors",
    "extract_title_phrases",
    "locate_clauses",
    "locate_sections",
    "range_read_hint",
]

#: 条款号锚：A2／R10／S9-R01／A4-H1-H3／G0…
_CLAUSE_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Z]{1,4}\d{1,3}"
    r"(?:[-–][A-Z]{0,4}\d{1,3})*"
    r"(?![0-9])")

#: 标题行：``## 4.2 事件输入``
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")

#: 条目行：``- **H1 无第二套权威**：…``／``1. **V1 记录正确**：…``
_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]\s+|\d+[.)]\s+)(.*)$")

#: 表格行：``| G0 契约基线 | … |``
_TABLE_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|")

#: 内联强调：``**H1**``／````H1````
_EMPH_RE = re.compile(r"[*`]+")

#: 中文短语（标题短语锚的候选）：连续 CJK 2 字以上
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")


@dataclass(frozen=True)
class DocRange:
    """正稿里定位到的一段（行号 1 起、闭区间——与 read_card 的 para_* 同口径）。"""

    anchor: str
    title: str
    start: int          # 1 起
    end: int            # 闭区间
    kind: str = "clause"   # clause=条款号锚；section=标题短语锚
    text: str = ""      # 段落正文（可能被长度闸裁过）

    @property
    def lines(self) -> int:
        return max(0, self.end - self.start + 1)


def doc_fingerprint(text: str) -> str:
    """正稿版本指纹（sha256 前 16 位）——随坐标一起给，版本变了指纹就变。"""
    return "sha256:" + hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def extract_clause_anchors(text: str) -> list[str]:
    """从一段文字里抽条款号锚（长的优先——``S9-R01`` 要盖过 ``R01``）。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _CLAUSE_RE.finditer(str(text or "")):
        tok = m.group(0).replace("–", "-")
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    out.sort(key=len, reverse=True)
    return out


def extract_title_phrases(text: str, *, min_len: int = 4, max_len: int = 16
                           ) -> list[str]:
    """从一段文字里抽中文短语候选（长的优先），供标题短语锚使用。"""
    cands: list[str] = []
    seen: set[str] = set()
    for run in _CJK_RUN_RE.findall(str(text or "")):
        n = len(run)
        for size in range(min(n, max_len), min_len - 1, -1):
            for i in range(0, n - size + 1):
                piece = run[i:i + size]
                if piece in seen:
                    continue
                seen.add(piece)
                cands.append(piece)
    return cands


def _strip_md(text: str) -> str:
    return _EMPH_RE.sub("", str(text or "")).strip()


def _bold_anchor(text: str, anchor: str) -> bool:
    """条目行里这个条款号是不是**正文自己标出来的**（``**A2**``／``**H1 无…**``）。

    散文中偶然出现的同形缩写（``4. L4 业务方案…``）不算——那不是条款标记。
    判据：条目正文**以加粗的该条款号起头**（加粗可以裹住整个短语）。
    """
    a = str(anchor or "").strip()
    if not a:
        return False
    body = str(text or "").lstrip()
    if not body.startswith("**" + a):
        return False
    rest = body[2 + len(a):]
    if not rest:
        return True
    nxt = rest[0]
    # 后面紧跟字母/数字/汉字 ⇒ 是更长的标识（如 H1x），不是这个条款号
    return not (nxt.isalnum() or "一" <= nxt <= "鿿")


def _heading_index(lines: list[str]) -> list[tuple[int, int, str]]:
    """(行号1起, 级别, 标题文本) 列表。"""
    out: list[tuple[int, int, str]] = []
    for i, ln in enumerate(lines, 1):
        m = _HEADING_RE.match(ln)
        if m:
            out.append((i, len(m.group(1)), _strip_md(m.group(2))))
    return out


def _heading_span(lines: list[str], heads: list[tuple[int, int, str]],
                  pos: int) -> int:
    """某个标题的章节结束行（下一个同级或更高级标题的前一行；到文件尾）。"""
    _, level, _ = heads[pos]
    for j in range(pos + 1, len(heads)):
        if heads[j][1] <= level:
            return heads[j][0] - 1
    return len(lines)


def _item_span(lines: list[str], start: int) -> int:
    """条目（``- **H1 …``）的结束行：下一个同级条目/标题之前。"""
    m = _ITEM_RE.match(lines[start - 1])
    indent = len(m.group(1)) if m else 0
    j = start
    while j < len(lines):
        ln = lines[j]
        if _HEADING_RE.match(ln):
            break
        mi = _ITEM_RE.match(ln)
        if mi and len(mi.group(1)) <= indent:
            break
        if ln.strip() and not ln.startswith(" ") and not ln.startswith("\t"):
            # 顶格非空行且不是续行 ⇒ 段落结束
            break
        j += 1
    end = j            # j 是 0 起下标 ⇒ 1 起行号 j 即最后一行
    while end > start and not lines[end - 1].strip():
        end -= 1
    return max(start, end)


def locate_clauses(doc_text: str, anchors: list[str],
                   *, max_chars: int = 6000) -> list[DocRange]:
    """按条款号锚在正稿里定位。命中即精确（标题/条目以该条款号起头）。"""
    lines = str(doc_text or "").split("\n")
    if not lines:
        return []
    heads = _heading_index(lines)
    out: list[DocRange] = []
    used_ranges: list[tuple[int, int]] = []
    for anchor in anchors or []:
        a = str(anchor or "").strip()
        if not a:
            continue
        # 复合锚的后缀已被复合锚覆盖 ⇒ 跳过（避免 S9-R10 与 R10 各定位一次）
        if any(a != b and a in b for b in (o.anchor for o in out)):
            continue
        hit = _locate_one(lines, heads, a)
        if hit is None and "-" in a:
            # 复合锚（A4-H1-H3）多半不是正文里的一个整体标记——按段拆开各自定位
            # （A4 是章节标题，H1/H3 是条目），逐段给坐标。
            for part in [p for p in a.split("-") if p]:
                sub = _locate_one(lines, heads, part)
                if sub is None:
                    continue
                start, end, title = sub
                if any(not (end < s or start > e) for s, e in used_ranges):
                    continue
                used_ranges.append((start, end))
                body = "\n".join(lines[start - 1:end])
                if len(body) > max_chars:
                    body = body[:max_chars] + "…（本段过长，已截断；完整正文按坐标回读）"
                out.append(DocRange(anchor=part, title=title, start=start, end=end,
                                    kind="clause", text=body))
            continue
        if hit is None:
            continue
        start, end, title = hit
        if any(not (end < s or start > e) for s, e in used_ranges):
            continue
        used_ranges.append((start, end))
        body = "\n".join(lines[start - 1:end])
        if len(body) > max_chars:
            body = body[:max_chars] + "…（本段过长，已截断；完整正文按坐标回读）"
        out.append(DocRange(anchor=a, title=title, start=start, end=end,
                            kind="clause", text=body))
    out.sort(key=lambda r: r.start)
    return out


def _locate_one(lines: list[str], heads: list[tuple[int, int, str]],
                anchor: str) -> Optional[tuple[int, int, str]]:
    """找一个条款号：先看标题首词，再看条目行，再看表格行首格。"""
    # ① 标题：首词就是该条款号（``### A2 负责／不负责矩阵``）
    for pos, (ln, _lvl, title) in enumerate(heads):
        first = title.split()[0] if title.split() else ""
        first = first.strip("：:、,，。．.")
        if first == anchor:
            return ln, _heading_span(lines, heads, pos), title
    # ② 条目行：``- **H1 …**``／``1. **V1 …**``——条款号必须是**正文自己标出来的**
    #    （加粗）。散文里的偶然同形词（``4. L4 业务方案…`` 里的 "L4"）不算条款号：
    #    那种行号不是判据，只是行文里提到了同一个缩写。
    for i, raw in enumerate(lines, 1):
        m = _ITEM_RE.match(raw)
        if not m:
            continue
        body_raw = m.group(2)
        if not _bold_anchor(body_raw, anchor):
            continue
        body = _strip_md(body_raw)
        end = _item_span(lines, i)
        return i, end, body[:120]
    # ③ 表格行首格
    for i, raw in enumerate(lines, 1):
        m = _TABLE_ROW_RE.match(raw)
        if not m:
            continue
        token = _strip_md(m.group(1)).split()[0] if _strip_md(m.group(1)).split() else ""
        token = token.strip("：:、,，。．.")
        if token == anchor:
            return i, i, raw[:120]
    return None


def locate_sections(doc_text: str, phrases: list[str], *,
                    min_len: int = 4, max_chars: int = 6000) -> list[DocRange]:
    """按标题短语锚定位章节（短语须**逐字**出现在且只出现在一个标题里）。"""
    lines = str(doc_text or "").split("\n")
    if not lines:
        return []
    heads = _heading_index(lines)
    if not heads:
        return []
    titles = [(ln, lvl, title) for ln, lvl, title in heads
              if lvl >= 2]          # 一级标题＝文档大标题，不是「章节」
    out: list[DocRange] = []
    seen_titles: set[int] = set()
    for ph in phrases or []:
        p = str(ph or "").strip()
        if len(p) < min_len:
            continue
        hits = [i for i, (_ln, _lvl, title) in enumerate(titles) if p in title]
        if len(hits) != 1:
            continue                      # 零命中＝定位不到；多命中＝不可靠
        pos = hits[0]
        ln, _lvl, title = titles[pos]
        hpos = next(i for i, h in enumerate(heads)
                    if h[0] == ln and h[2] == title)
        if ln in seen_titles:
            continue
        seen_titles.add(ln)
        end = _heading_span(lines, heads, hpos)
        body = "\n".join(lines[ln - 1:end])
        if len(body) > max_chars:
            body = body[:max_chars] + "…（本节过长，已截断；完整正文按坐标回读）"
        out.append(DocRange(anchor=p, title=title, start=ln, end=end,
                             kind="section", text=body))
    out.sort(key=lambda r: r.start)
    return out


def range_read_hint(card_id: str, block: str, start: int, end: int) -> str:
    """按行段回读的命令形态（与 read_card 的 para_* 契约同口径：1 起、闭区间）。"""
    return (f'read_card(card_id="{card_id}", block="{block}", '
            f"para_start={int(start)}, para_end={int(end)})")
