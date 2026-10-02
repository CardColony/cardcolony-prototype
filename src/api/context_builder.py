# -*- coding: utf-8 -*-
"""【已由 context_assembler 取代】Phase 7 后不再使用，保留供旧测试与回退参考。

新代码请使用 src/api/context_assembler.py 的 build_context_block。
"""
# 结论注入：把所有确定卡片的结论区全量注入 system prompt。
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..storage.hash_utils import parse_front_matter, split_body_sections

BASE_DIR = Path(__file__).resolve().parents[2]
CARDS_DIR = BASE_DIR / "cards"


def build_system_prompt(current_card: dict[str, Any], confirmed_cards: list[dict[str, Any]]) -> str:
    """严格模板渲染。current_card / confirmed_cards 均为
    {card_id, title, status, conclusion} 形式的字典。"""
    lines = [
        "你是推演项目的创作讨论助手。以下是本项目所有已确定的结论，视为既定事实，讨论中不得违背：",
        "",
        "【确定卡片结论】",
    ]
    if confirmed_cards:
        for card in confirmed_cards:
            lines.append(f"### {card['card_id']} {card['title']}")
            lines.append(card.get("conclusion", ""))
    else:
        lines.append("（当前无确定结论）")
    lines += [
        "",
        "【当前讨论卡片】",
        f"{current_card['card_id']} {current_card['title']}（状态：{current_card['status']}）",
    ]
    return "\n".join(lines)


def collect_confirmed_cards() -> list[dict[str, Any]]:
    """扫描所有卡片，status == "确定" 的全部返回（全量，不筛选、不检索、不摘要）。"""
    confirmed: list[dict[str, Any]] = []
    if not CARDS_DIR.exists():
        return confirmed
    for path in sorted(CARDS_DIR.glob("card_*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_front_matter(content)
        if meta.get("status") != "确定":
            continue
        sections = split_body_sections(body)
        confirmed.append({
            "card_id": path.stem,
            "title": meta.get("title", ""),
            "status": "确定",
            "conclusion": sections["conclusion"],
        })
    return confirmed
