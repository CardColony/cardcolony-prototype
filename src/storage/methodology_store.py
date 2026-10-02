# -*- coding: utf-8 -*-
"""方法论沉淀存储层（Phase 9 模块 A）。

方法论 = 跨项目的推演方法知识（假设怎么立死法、因果链怎么拆、证据怎么分级），
与"这本书的知识"（行为体/世界观卡）严格分开。

存储约定：
- 项目根 methodology/ 目录（跨剧本全局，与 works/ 平级）。
  目录不存在 = 库为空，零成本，一切读函数返回空结果。
- 每条一个文件 m_0001.md（四位序号）。
- front matter：id/title/tags(JSON 数组)/source_work/source_card/created_at/updated_at，
  正文自由（建议模板：适用场景/做法/注意与反例三节）。
- 无索引文件：列表现算，单一真相源是条目文件（对齐 8.5 manuscript 哲学）。

函数：
- list_all() → 摘要列表，按 id 排序
- read(mid) → 完整条目 dict（含正文）或 None
- save(title, content, tags, source_work, source_card)
  → title 精确相同的条目已存在则更新它（正文/tags 覆盖，updated_at 刷新，id 不变），
    否则新建 next id。返回条目 dict。
- update(mid, updates) → 供 PATCH：title/content/tags 局部更新，条目不存在返回 None。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .hash_utils import parse_front_matter

BASE_DIR = Path(__file__).resolve().parents[2]
MTH_DIR = BASE_DIR / "methodology"

_ID_RE = re.compile(r"^m_\d{4}$")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _word_count(text: str) -> int:
    """字数 = 去除全部空白字符后的字符数（与 manuscript_store 同口径）。"""
    return len(re.sub(r"\s", "", text))


def _path_of(mid: str) -> Path:
    return MTH_DIR / f"{mid}.md"


def _norm_tags(tags: Any) -> list[str]:
    """tags 归一：非 list 一律回退空列表；元素转 str 去首尾空白，剔除空项。"""
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        s = str(t).strip()
        if s:
            out.append(s)
    return out


def _render(meta: dict[str, Any], content: str) -> str:
    """渲染条目文件文本。title 与 tags 用 JSON 编码保证 YAML 安全。"""
    lines = [
        "---",
        f"id: {meta['id']}",
        f"title: {json.dumps(meta.get('title', ''), ensure_ascii=False)}",
        f"tags: {json.dumps(_norm_tags(meta.get('tags')), ensure_ascii=False)}",
        f"source_work: {json.dumps(meta.get('source_work', ''), ensure_ascii=False)}",
        f"source_card: {json.dumps(meta.get('source_card', ''), ensure_ascii=False)}",
        f"created_at: {json.dumps(meta.get('created_at', ''), ensure_ascii=False)}",
        f"updated_at: {json.dumps(meta.get('updated_at', ''), ensure_ascii=False)}",
        "---",
        "",
    ]
    return "\n".join(lines) + (content.rstrip("\n") + "\n" if content else "")


def _load(mid: str) -> Optional[dict[str, Any]]:
    """读单条（meta + content），文件不存在/损坏返回 None。"""
    if not _ID_RE.match(mid or ""):
        return None
    path = _path_of(mid)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = parse_front_matter(raw)
    if not meta:
        return None
    content = body.strip("\n")
    return {
        "id": str(meta.get("id", mid)),
        "title": str(meta.get("title", "")),
        "tags": _norm_tags(meta.get("tags")),
        "source_work": str(meta.get("source_work", "") or ""),
        "source_card": str(meta.get("source_card", "") or ""),
        "created_at": str(meta.get("created_at", "") or ""),
        "updated_at": str(meta.get("updated_at", "") or ""),
        "content": content,
        "words": _word_count(content),
    }


def _all_ids() -> list[str]:
    if not MTH_DIR.exists():
        return []
    return sorted(p.stem for p in MTH_DIR.glob("m_*.md") if _ID_RE.match(p.stem))


def _next_id() -> str:
    ids = _all_ids()
    if not ids:
        return "m_0001"
    return f"m_{int(ids[-1][2:]) + 1:04d}"


def list_all() -> list[dict[str, Any]]:
    """全部条目摘要（不含正文），按 id 排序。库空/目录不存在 → []。"""
    result: list[dict[str, Any]] = []
    for mid in _all_ids():
        e = _load(mid)
        if e is None:
            continue
        result.append({
            "id": e["id"],
            "title": e["title"],
            "tags": e["tags"],
            "source_work": e["source_work"],
            "source_card": e["source_card"],
            "updated_at": e["updated_at"],
            "words": e["words"],
        })
    return result


def read(mid: str) -> Optional[dict[str, Any]]:
    """完整条目（含正文）；不存在返回 None。"""
    return _load(mid)


def save(
    title: str,
    content: str,
    tags: Optional[list[str]] = None,
    source_work: str = "",
    source_card: str = "",
) -> dict[str, Any]:
    """保存一条方法论。

    - title 精确相同的条目已存在 → 更新它（content/tags 覆盖，updated_at 刷新，
      id/created_at/source_* 不变），否则新建 next id。
    - 返回条目 dict（同 read）。title 为空返回 {"error": ...}。
    """
    title = (title or "").strip()
    if not title:
        return {"error": "title 不能为空"}
    content = (content or "").strip()
    now = _now_iso()

    # 找 title 精确相同的既有条目
    for mid in _all_ids():
        e = _load(mid)
        if e is not None and e["title"] == title:
            meta = {
                "id": e["id"],
                "title": title,
                "tags": _norm_tags(tags),
                "source_work": e["source_work"],
                "source_card": e["source_card"],
                "created_at": e["created_at"],
                "updated_at": now,
            }
            _path_of(e["id"]).write_text(_render(meta, content), encoding="utf-8")
            return _load(e["id"]) or {"error": f"写入后读取失败: {e['id']}"}

    MTH_DIR.mkdir(exist_ok=True)
    mid = _next_id()
    meta = {
        "id": mid,
        "title": title,
        "tags": _norm_tags(tags),
        "source_work": source_work or "",
        "source_card": source_card or "",
        "created_at": now,
        "updated_at": now,
    }
    _path_of(mid).write_text(_render(meta, content), encoding="utf-8")
    return _load(mid) or {"error": f"写入后读取失败: {mid}"}


def update(mid: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """局部更新（供前端 PATCH）：允许键 title/content/tags，其余忽略。

    条目不存在返回 None；title 传空串视为不更新。
    """
    e = _load(mid)
    if e is None:
        return None
    updates = updates or {}
    new_title = str(updates.get("title") or "").strip() or e["title"]
    new_content = updates["content"] if "content" in updates else e["content"]
    new_tags = _norm_tags(updates["tags"]) if "tags" in updates else e["tags"]
    meta = {
        "id": e["id"],
        "title": new_title,
        "tags": new_tags,
        "source_work": e["source_work"],
        "source_card": e["source_card"],
        "created_at": e["created_at"],
        "updated_at": _now_iso(),
    }
    _path_of(mid).write_text(_render(meta, str(new_content or "").strip()), encoding="utf-8")
    return _load(mid)
