# -*- coding: utf-8 -*-
"""canvas.json 缓存维护。canvas.json 是可重建缓存，卡片文件才是真身。

Phase 7：每个 work 各有一份 canvas.json，路径 = works/{work_id}/canvas.json。
canvas.json 顶层新增 positions 字段（卡片在画布上的坐标 + pinned 标记）：
    {
      "project_name": "...",
      "created_at": "...",
      "cards": [...],
      "relations": [...],
      "positions": {"card_001": {"x":0,"y":0,"pinned":false}, ...}
    }

写入失败只记日志，绝不抛出中断调用方，绝不删除任何文件。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import parse_front_matter, split_body_sections

logger = logging.getLogger("storage.canvas")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"
CARDS_DIR = BASE_DIR / "cards"        # 旧路径（兼容）
CANVAS_PATH = BASE_DIR / "canvas.json"  # 旧路径（兼容）

PROJECT_NAME = "推演agent"

_CARD_FILE_RE = re.compile(r"^card_(\d{3,})\.md$")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _canvas_path(work_id: str) -> Path:
    return WORKS_DIR / work_id / "canvas.json"


def _cards_dir(work_id: str) -> Path:
    return WORKS_DIR / work_id / "cards"


# [R4] 结论文本摘要上限——与前端 canvas.js 的 getSummary 逐字一致
# （前端原文：firstLine.length > 120 ? firstLine.slice(0,120) + "…" : firstLine）
SUMMARY_MAX = 120


def _conclusion_summary(body: str) -> str:
    """取现状块（status_block）第一行非空文字，超 120 字截断加省略号。

    与前端 getSummary 的算法逐字一致（先按行 split、取第一个非空 trim 行、超长截断）。
    现状块为空/全空白 → 返回空字符串（不写占位文字）。
    解析失败不抛出——返回空串，绝不中断 rebuild。
    [卡片信息结构改造 S2-11/G-7] 取数键随拆分器归一改 status_block；
    canvas.json 字段名 conclusion_summary **不变**（S2-11，前端快路径依赖它）。
    """
    try:
        sections = split_body_sections(body or "")
        concl = sections.get("status_block", "") or ""
    except Exception:
        return ""
    first_line = ""
    for line in concl.split("\n"):
        s = line.strip()
        if s:
            first_line = s
            break
    if len(first_line) > SUMMARY_MAX:
        return first_line[:SUMMARY_MAX] + "…"
    return first_line


def rebuild(work_id: str) -> dict[str, Any]:
    """全量重建 work 的 canvas.json 并写盘，返回结果字典。

    保留旧 canvas.json 的 created_at 与 positions（若可读）。
    """
    canvas_path = _canvas_path(work_id)
    created_at = _now_iso()
    positions: dict[str, Any] = {}
    try:
        old = json.loads(canvas_path.read_text(encoding="utf-8"))
        if isinstance(old, dict):
            if old.get("created_at"):
                created_at = old["created_at"]
            if isinstance(old.get("positions"), dict):
                positions = old["positions"]
    except Exception:
        pass

    cards: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    cd = _cards_dir(work_id)
    if cd.exists():
        for path in sorted(cd.glob("card_*.md")):
            if not _CARD_FILE_RE.match(path.name):
                continue
            card_id = path.stem
            try:
                meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("解析卡片失败，跳过 %s: %s", path.name, exc)
                continue
            cards.append({
                "card_id": card_id,
                "title": meta.get("title", ""),
                "card_type": meta.get("card_type") or "线索",
                "parent": meta.get("parent", ""),
                "created_at": meta.get("created_at", ""),
                # [R4] 结论摘要随画布一次到手——前端 getSummary 优先读本字段，
                # 免去首屏对每张可见卡各发一次 getCard 请求（N+1）。
                # canvas.json 是可重建缓存（卡片文件才是真身），本字段不写用户真身数据。
                "conclusion_summary": _conclusion_summary(body),
            })
            for rel in meta.get("relations") or []:
                if isinstance(rel, dict):
                    item = {"source": card_id, "target": rel.get("target", ""), "type": rel.get("type", "")}
                    if rel.get("note"):
                        item["note"] = rel["note"]
                    relations.append(item)

    # [R4-3-5] 重建打点（沿用既有 logger，不参与分支、不设闸）
    summary_count = sum(1 for c in cards if c.get("conclusion_summary"))
    logger.info("canvas 重建：%d 卡，含摘要 %d 条", len(cards), summary_count)

    result = {
        "project_name": PROJECT_NAME,
        "work_id": work_id,
        "created_at": created_at,
        "cards": cards,
        "relations": relations,
        "positions": positions,
    }
    try:
        canvas_path.parent.mkdir(parents=True, exist_ok=True)
        canvas_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("canvas.json 写入失败（不中断）: %s", exc)
    return result


async def update_cache(work_id: str) -> dict[str, Any]:
    """v1 用全量重建代替增量维护。"""
    return rebuild(work_id)


def load(work_id: str) -> dict[str, Any]:
    """读 work 的 canvas.json；不存在或解析失败则自动重建。"""
    canvas_path = _canvas_path(work_id)
    try:
        data = json.loads(canvas_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("canvas.json 内容非法，触发重建 work=%s", work_id)
    except FileNotFoundError:
        logger.info("canvas.json 不存在，触发重建 work=%s", work_id)
    except Exception as exc:
        logger.warning("canvas.json 读取失败，触发重建 work=%s: %s", work_id, exc)
    return rebuild(work_id)


def merge_positions(work_id: str, positions: dict[str, Any]) -> dict[str, Any]:
    """合并写入 positions（增量更新单卡坐标）。"""
    data = load(work_id)
    cur = data.get("positions") if isinstance(data.get("positions"), dict) else {}
    cur.update(positions or {})
    data["positions"] = cur
    canvas_path = _canvas_path(work_id)
    try:
        canvas_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("canvas.json positions 写入失败（不中断）: %s", exc)
    return data
