# -*- coding: utf-8 -*-
"""原文资料只读存储（Phase 8.5 模块 A）。

存储约定：
- 目录 works/{work_id}/manuscript/，章节文件 ch_0001.md（四位序号）。
- 文件格式：YAML front matter 只含 title 一项，--- 之后是正文，段落按空行分隔。
- 无独立索引文件：字数/段数全部从章节文件现算，单一真相源是文件本身。
- 目录不存在或为空 = 本剧本无原文，一切函数返回空结果，不抛异常。

三个纯读函数（无锁、无写路径，按 card_store 纯读助手惯例保持同步）：
- list_chapters(work_id)
- read_chapter(work_id, chapter, para_start=None, para_end=None)
- search_manuscript(work_id, keyword, limit=10)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .hash_utils import parse_front_matter

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"

# 整章读取的字数上限（超过则按整段截断返回）
READ_CHAR_LIMIT = 8000

_CHAPTER_RE = re.compile(r"^ch_\d{4}$")


class ChapterNotFoundError(Exception):
    """章节不存在。"""


def _manuscript_dir(work_id: str) -> Path:
    return WORKS_DIR / work_id / "manuscript"


def _word_count(text: str) -> int:
    """字数 = 去除全部空白字符后的字符数。"""
    return len(re.sub(r"\s", "", text))


def _split_paras(body: str) -> list[str]:
    """按空行拆分自然段，段内保留原样（仅去除段首尾空白）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body.strip())]
    return [p for p in paras if p]


def _load_chapter(work_id: str, chapter: str) -> tuple[str, list[str]]:
    """读一章，返回 (title, paras)。章不存在 → ChapterNotFoundError。"""
    if not _CHAPTER_RE.match(chapter or ""):
        raise ChapterNotFoundError(chapter)
    path = _manuscript_dir(work_id) / f"{chapter}.md"
    if not path.exists():
        raise ChapterNotFoundError(chapter)
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(raw)
    title = str((meta or {}).get("title", ""))
    return title, _split_paras(body)


def list_chapters(work_id: str) -> list[dict[str, Any]]:
    """列出全部章节，按序号升序。

    每章 {"chapter": "ch_0001", "title": str, "words": int, "paras": int}。
    目录不存在或为空 → []。
    """
    md = _manuscript_dir(work_id)
    if not md.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(md.glob("ch_*.md")):
        if not _CHAPTER_RE.match(path.stem):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_front_matter(raw)
        paras = _split_paras(body)
        result.append({
            "chapter": path.stem,
            "title": str((meta or {}).get("title", "")),
            "words": _word_count(body),
            "paras": len(paras),
        })
    return result


def read_chapter(
    work_id: str,
    chapter: str,
    para_start: Optional[int] = None,
    para_end: Optional[int] = None,
) -> dict[str, Any]:
    """读取一章正文（段落编号从 1 开始，与 list_chapters 的 paras 对齐）。

    - 无区间：返回整章；正文超过 READ_CHAR_LIMIT 字时只返回前若干整段
      （累计不超限），truncated=true 并带 note 提示用 para 区间续读。
    - 有区间：返回 [para_start, para_end] 闭区间段落文本；越界自动夹紧到
      [1, paras_total]，夹紧后区间为空则返回空文本。
    - 章不存在 → ChapterNotFoundError。
    """
    title, paras = _load_chapter(work_id, chapter)
    total = len(paras)
    result: dict[str, Any] = {
        "chapter": chapter,
        "title": title,
        "paras_total": total,
        "para_start": 0,
        "para_end": 0,
        "text": "",
        "truncated": False,
    }
    if total == 0:
        return result

    if para_start is None and para_end is None:
        # 整章；超限则按整段截断
        acc: list[str] = []
        used = 0
        for i, p in enumerate(paras, start=1):
            w = _word_count(p)
            if acc and used + w > READ_CHAR_LIMIT:
                result["truncated"] = True
                result["note"] = (
                    f"本章共 {total} 段，已截断至前 {i - 1} 段"
                    f"（约 {used} 字），请用 para_start/para_end 区间续读。"
                )
                break
            acc.append(p)
            used += w
        result["para_start"] = 1
        result["para_end"] = len(acc)
        result["text"] = "\n\n".join(acc)
        return result

    # 有区间：夹紧到 [1, total]
    start = 1 if para_start is None else max(1, int(para_start))
    end = total if para_end is None else min(total, int(para_end))
    if start > total or end < 1 or start > end:
        result["para_start"] = start
        result["para_end"] = end
        return result
    result["para_start"] = start
    result["para_end"] = end
    result["text"] = "\n\n".join(paras[start - 1 : end])
    return result


def search_manuscript(work_id: str, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    """逐章逐段做子串匹配（大小写原样，不做分词）。

    每命中一条 {"chapter", "title", "para_no", "snippet"}；
    snippet = 命中段内以关键词为中心前后各约 40 字（段落不足则整段）。
    超过 limit 停止扫描。keyword 为空串 → []。
    """
    if not keyword:
        return []
    hits: list[dict[str, Any]] = []
    md = _manuscript_dir(work_id)
    if not md.exists():
        return []
    for path in sorted(md.glob("ch_*.md")):
        if not _CHAPTER_RE.match(path.stem):
            continue
        try:
            title, paras = _load_chapter(work_id, path.stem)
        except (ChapterNotFoundError, OSError):
            continue
        for no, p in enumerate(paras, start=1):
            idx = p.find(keyword)
            if idx < 0:
                continue
            lo = max(0, idx - 40)
            hi = min(len(p), idx + len(keyword) + 40)
            snippet = p[lo:hi]
            if lo > 0:
                snippet = "…" + snippet
            if hi < len(p):
                snippet = snippet + "…"
            hits.append({
                "chapter": path.stem,
                "title": title,
                "para_no": no,
                "snippet": snippet,
            })
            if len(hits) >= limit:
                return hits
    return hits
