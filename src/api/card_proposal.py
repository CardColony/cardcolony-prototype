# -*- coding: utf-8 -*-
"""建卡协议解析（白板版 2026-10-02，双形态跨度制）。

模型唯一建卡格式（实际出现过两种形态，都必须接住）：
  形态一（标准）：```card-proposal\n[...]\n```
  形态二（DeepSeek 系泄漏）：<｜｜card-proposal｜｜>\n[...]\n<｜｜/card-proposal｜｜>
    —— 竖线可能全角(U+FF5C)/半角(U+007C)、单条/双条、标记词两侧可能有空白

parse_proposals(reply_text) -> (clean_text, proposals)
  - 提取所有形态的块；clean_text = 剥离协议块后的正文
  - fenced 块内容里嵌 ```（如 content 含代码围栏）会让闭合点歧义：
    对每个候选闭合点按序尝试 JSON 解析，解析成功即为真闭合（纯机械重试）
  - JSON 解析失败：整块跳过，记 warning，不影响正文
  - 只认 title / parent / content 三个字段；多余字段忽略
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

# 形态一：fenced 开标记
_FENCE_OPEN_RE = re.compile(r"```card-proposal\s*\n")
# fenced 候选闭合：行首三反引号（含前导换行，共 4 字符）
_FENCE_CLOSE_RE = re.compile(r"\n```")
# 形态二：尖括号标记（竖线全角/半角、单/双、标记词两侧空白均可）
_BLOCK_ANGLE_RE = re.compile(
    r"<[|｜]{1,2}\s*card-proposal\s*[|｜]{1,2}>"
    r"(.*?)"
    r"<[|｜]{1,2}\s*/card-proposal\s*[|｜]{1,2}>",
    re.DOTALL,
)


def _extract_fenced_spans(reply_text: str) -> list[tuple[int, int, str]]:
    """fenced 块提取：对每个候选闭合点按序尝试 JSON 解析，成功者为真闭合。

    返回 [(start, end, raw_body)]。无任何闭合点时取余文（随后解析失败会留 warning）。
    """
    spans: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        m = _FENCE_OPEN_RE.search(reply_text, pos)
        if not m:
            break
        start, body_start = m.start(), m.end()
        closes = [c.start() for c in _FENCE_CLOSE_RE.finditer(reply_text, body_start)]
        chosen: tuple[int, int, str] | None = None
        for c in closes:
            raw = reply_text[body_start:c]
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                continue
            chosen = (start, c + 4, raw)  # 闭合含 "\n```" 四字符
            break
        if chosen is None:
            if closes:
                c = closes[-1]
                chosen = (start, c + 4, reply_text[body_start:c])
            else:
                chosen = (start, len(reply_text), reply_text[body_start:])
        spans.append(chosen)
        pos = chosen[1]
    return spans


def _parse_items(raw: str, proposals: list[dict[str, Any]]) -> None:
    """解析一个块的 JSON，产出 proposals 项。失败整块跳过留 warning。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("card-proposal 块 JSON 解析失败，整块跳过: %s", exc)
        return
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


def parse_proposals(reply_text: str) -> tuple[str, list[dict[str, Any]]]:
    """提取所有 card-proposal 块（双形态），返回 (clean_text, proposals)。"""
    proposals: list[dict[str, Any]] = []
    angle_spans = [(m.start(), m.end(), m.group(1))
                   for m in _BLOCK_ANGLE_RE.finditer(reply_text)]
    fenced_spans = _extract_fenced_spans(reply_text)
    # 去重：fenced 块完全落在某个 angle 块内部（内容嵌套）时跳过
    fenced_spans = [
        s for s in fenced_spans
        if not any(a[0] <= s[0] and s[1] <= a[1] for a in angle_spans)
    ]
    all_spans = sorted(angle_spans + fenced_spans, key=lambda s: s[0])
    for _, _, raw in all_spans:
        _parse_items(raw, proposals)
    clean_text = reply_text
    for start, end, _ in sorted(all_spans, key=lambda s: s[0], reverse=True):
        clean_text = clean_text[:start] + clean_text[end:]
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
