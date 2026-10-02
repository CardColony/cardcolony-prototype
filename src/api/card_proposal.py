# -*- coding: utf-8 -*-
"""建卡协议解析（白板版 2026-10-02）。

模型唯一建卡格式：```card-proposal\n[{"title": "", "parent": "", "content": ""}]\n```

parse_proposals(reply_text) -> (clean_text, proposals)
  - 提取所有 fenced 块；clean_text = 剥离协议块后的正文
  - JSON 解析失败：整块跳过，记 warning，不影响正文
  - 只认 title / parent / content 三个字段；多余字段（type/charter/relations 等）忽略
  - title 空 → 跳过该项

apply_proposals(work_id, current_card_id, proposals) -> list[created]
  - 逐项调 create_card(source="模型生成")；content 落存储层正文区
  - parent 缺失/不存在回退 current_card_id
  - 连线不经建卡协议（link_cards 工具单独走）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..storage import card_store

logger = logging.getLogger("api.card_proposal")

# fenced code block: ```card-proposal ... ```
_BLOCK_RE = re.compile(
    r"```card-proposal\s*\n(.*?)\n```",
    re.DOTALL,
)


def parse_proposals(reply_text: str) -> tuple[str, list[dict[str, Any]]]:
    """提取所有 card-proposal 块，返回 (clean_text, proposals)。

    proposals 每项形如 {"title":..., "parent":..., "content":...}
    """
    proposals: list[dict[str, Any]] = []

    def _collect(match: re.Match) -> str:
        raw = match.group(1)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("card-proposal 块 JSON 解析失败，整块跳过: %s", exc)
            return ""  # 从正文中剥离
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            if not title:
                logger.warning("card-proposal 项缺 title，跳过: %s", item)
                continue
            proposals.append({
                "title": title,
                "parent": (item.get("parent") or "").strip(),
                "content": (item.get("content") or "").strip(),
            })
        return ""

    clean_text = _BLOCK_RE.sub(_collect, reply_text)
    # 清理由剥离产生的多余空行（连续 3+ 换行压成 2）
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip() + "\n"
    return clean_text, proposals


async def apply_proposals(
    work_id: str,
    current_card_id: str,
    proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """逐项调 create_card(source='模型生成')。parent 缺失/不存在回退 current_card_id。

    返回成功创建的卡列表 [{"card_id","title","parent"}]。
    单项失败记日志继续下一项。
    """
    created: list[dict[str, Any]] = []
    for p in proposals:
        parent = p.get("parent") or ""
        if parent == "":
            parent = current_card_id
        try:
            result = await card_store.create_card(
                work_id,
                p["title"],
                "模型生成",
                parent=parent,
                conclusion=p["content"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("apply_proposals 单项失败 title=%s: %s", p["title"], exc)
            continue
        if "error" in result:
            logger.error("apply_proposals 单项失败 title=%s: %s", p["title"], result["error"])
            continue
        meta = result["meta"]
        created.append({
            "card_id": meta["card_id"],
            "title": meta["title"],
            "parent": meta["parent"],
        })
    return created
