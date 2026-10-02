# -*- coding: utf-8 -*-
"""建卡协议解析（模块 E）。

parse_proposals(reply_text) -> (clean_text, proposals)
  - 提取所有 ```card-proposal fenced 块
  - clean_text = 剥离协议块后的正文
  - 块内 JSON 解析失败：整块跳过，记 warning，不影响正文
  - 逐项确定性校验（不问模型）

apply_proposals(work_id, current_card_id, proposals) -> list[created]
  - 逐项调 create_card(source="模型生成")
  - 单项失败记日志继续
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..storage import card_store

logger = logging.getLogger("api.card_proposal")

# fenced code block: ```card-proposal ... ```
# 用非贪婪匹配，允许多个块
_BLOCK_RE = re.compile(
    r"```card-proposal\s*\n(.*?)\n```",
    re.DOTALL,
)

ALLOWED_TYPES = ("世界观", "行为体", "节点", "假设", "线索")
# [phase8.6-D] relations.type 白名单
ALLOWED_REL_TYPES = ("先后", "锚定", "兑现", "关联")


def parse_proposals(reply_text: str) -> tuple[str, list[dict[str, Any]]]:
    """提取所有 card-proposal 块，返回 (clean_text, proposals)。

    - clean_text：剥离所有协议块后的正文（保留正文原本结构）。
    - proposals：合法项列表，每项形如
        {"title":..., "type":..., "parent":..., "charter":..., "conclusion":...}
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
            raw_type = item.get("type", "")
            # type=剧本 或不在白名单 → 降级线索
            card_type = raw_type if raw_type in ALLOWED_TYPES else "线索"
            # [phase8.6-D] 可选 relations 字段：这里只做形状收集，
            # 合法性校验（target 存在/type 白名单/非自身）在 apply_proposals 落盘前做
            raw_rels = item.get("relations")
            relations: list[dict[str, Any]] = []
            if isinstance(raw_rels, list):
                for rel in raw_rels:
                    if not isinstance(rel, dict):
                        continue
                    entry = {
                        "target": (rel.get("target") or "").strip(),
                        "type": (rel.get("type") or "").strip(),
                    }
                    note = (rel.get("note") or "").strip()
                    if note:
                        entry["note"] = note
                    relations.append(entry)
            proposals.append({
                "title": title,
                "type": card_type,
                "parent": (item.get("parent") or "").strip(),
                "charter": (item.get("charter") or "").strip(),
                "conclusion": (item.get("conclusion") or "").strip(),
                "relations": relations,
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

    返回成功创建的卡列表 [{"card_id","title","card_type","parent"}]。
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
                card_type=p["type"],
                parent=parent,
                charter=p["charter"],
                conclusion=p["conclusion"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("apply_proposals 单项失败 title=%s: %s", p["title"], exc)
            continue
        if "error" in result:
            logger.error("apply_proposals 单项失败 title=%s: %s", p["title"], result["error"])
            continue
        meta = result["meta"]
        new_id = meta["card_id"]
        # [phase8.6-D] 校验并写入合法 relations（非法条目静默丢弃，不整单拒绝）
        valid_rels = []
        for rel in p.get("relations") or []:
            tgt = rel.get("target", "")
            rtype = rel.get("type", "")
            if rtype not in ALLOWED_REL_TYPES:
                continue
            if tgt == new_id or not tgt:
                continue
            if card_store._meta_of(work_id, tgt) is None:
                continue
            valid_rels.append(rel)
        if valid_rels:
            try:
                upd = await card_store.update_card(
                    work_id, new_id, {"relations": valid_rels}, "建卡协议写入关系",
                )
                if "error" in upd:
                    logger.error("apply_proposals 写 relations 失败 card=%s: %s", new_id, upd["error"])
            except Exception as exc:  # noqa: BLE001
                logger.error("apply_proposals 写 relations 异常 card=%s: %s", new_id, exc)
        created.append({
            "card_id": new_id,
            "title": meta["title"],
            "card_type": meta["card_type"],
            "parent": meta["parent"],
        })
    return created
