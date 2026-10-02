# -*- coding: utf-8 -*-
"""上下文组装器（白板版 2026-10-02）。

只组装机械事实，不注入任何内容：
- 卡片清单：全部卡的 id · 标题 · 父卡（不含类型、不含正文摘要、无空位提示）
- 当前卡：id · 标题（正文不推送——任何卡片正文一律经 read_card 拉取，
  杜绝"上轮模型写入的卡片内容变成下轮常设输入"的回流）

已删除（结构即引导，2026-10-02 全量审计定案）：
- 目标锚全文注入（模型自写结论变常设指令——自我繁殖）
- 血统链沿途结论全文注入
- 管辖域目录（含结论首行摘要与"（暂无结论）"空位邀请）
- 契约摘要、职责区（charter）注入、原文统计行

模型需要某张卡的正文时，经 read_card 工具自行调取（拉取，不推送）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..storage import work_store
from ..storage.card_store import _card_path
from ..storage.hash_utils import parse_front_matter

logger = logging.getLogger("api.context_assembler")

BASE_DIR = Path(__file__).resolve().parents[2]


def _load_cards_index(work_id: str) -> list[dict[str, Any]]:
    """扫描 cards 目录，返回 [{card_id, title, parent}]（按文件名排序）。"""
    out: list[dict[str, Any]] = []
    cd = _card_path(work_id, "card_000").parent
    if not cd.exists():
        return out
    for path in sorted(cd.glob("card_*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _body = parse_front_matter(raw)
        if not meta:
            continue
        out.append({
            "card_id": path.stem,
            "title": meta.get("title", ""),
            "parent": meta.get("parent") or "",
        })
    return out


def build_neutral_context(work_id: str, current_id: str) -> str:
    """中性上下文（主模型与三思考席共用同一份）：卡片清单 ＋ 当前卡标识。

    [拉取裁定 2026-10-02] 不推送任何卡片正文——模型需要时经 read_card 调取。
    上一轮模型写入的卡片内容不会再自动进入任何后续输入（回流断根）。
    """
    cards = _load_cards_index(work_id)
    wc_id = work_store.work_card_id_of(work_id)

    lines: list[str] = ["## 卡片清单"]
    for c in cards:
        if c["card_id"] == wc_id or not c["parent"]:
            lines.append(f"{c['card_id']} · {c['title']}（根）")
        else:
            lines.append(f"{c['card_id']} · {c['title']}（父：{c['parent']}）")

    cur = next((c for c in cards if c["card_id"] == current_id), None)
    if cur is not None:
        lines.append("")
        lines.append("## 当前卡")
        lines.append(f"{cur['card_id']} · {cur['title']}")

    return "\n".join(lines)
