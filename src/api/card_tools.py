# -*- coding: utf-8 -*-
"""卡片工具与权限（白板版 2026-10-02）。

工具接口只含机械事实：
- read_card：返回标题、父卡、连线、正文——不暴露内部四区语义
- edit_card(card_id, content)：覆盖写指定卡正文（内部落存储层正文区）
- link_cards：连线，模型可用类型限 先后/关联（锚定为系统内部关系，不对模型开放）

权限纯函数不变：读＝子树∪血统链∪连线∪锚卡全局可读；写＝自己的子树。
越权不抛异常，返回 {"error": ...} 给模型。
"""

from __future__ import annotations

import logging
from typing import Any

from ..storage import card_store, manuscript_store, work_store

logger = logging.getLogger("api.card_tools")

# [白板版 2026-10-02] 模型可建的连线类型收窄为两个中性关系；
# "锚定"（系统派生目标卡专用）与"兑现"（写作agent残留语义）不再对模型开放
MODEL_REL_TYPES = ("先后", "关联")


def build_tool_schemas() -> list[dict[str, Any]]:
    """OpenAI function calling 工具列表（直接传给 gateway.call(tools=...)）。"""
    return [
        {
            "name": "read_card",
            "description": "读取指定卡（标题、父卡、连线、正文）。只能读取你有权访问的卡。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id，如 card_002"},
                },
                "required": ["card_id"],
            },
        },
        {
            "name": "edit_card",
            "description": "改写你管辖范围内指定卡的正文（覆盖写）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id"},
                    "content": {"type": "string", "description": "新的正文全文（覆盖写）"},
                },
                "required": ["card_id", "content"],
            },
        },
        {
            "name": "delete_card",
            "description": "删除你管辖范围内指定编号的卡片及其对话。剧本根卡与有子卡的卡不可删除。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id"},
                },
                "required": ["card_id"],
            },
        },
        {
            "name": "list_manuscript",
            "description": "列出本项目已有的原文资料章节目录（序号/标题/字数/段数）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "read_manuscript",
            "description": "调取指定章节原文资料，长章请用段落区间分段阅读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter": {"type": "string", "description": "章节号，如 ch_0001"},
                    "para_start": {"type": "integer", "description": "起始段号（从 1 开始，可选）"},
                    "para_end": {"type": "integer", "description": "结束段号（闭区间，可选）"},
                },
                "required": ["chapter"],
            },
        },
        {
            "name": "search_manuscript",
            "description": "在全部原文资料中检索关键词，返回命中章节与上下文片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词（子串匹配）"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                },
                "required": ["keyword"],
            },
        },
        {
            "name": "web_search",
            "description": "查询网页搜索服务，返回结果列表（标题、链接、摘要、来源、日期）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                    "count": {"type": "integer", "description": "返回条数，默认 10"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "fetch_web",
            "description": "获取指定网页的文本内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "网页地址"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "link_cards",
            "description": "在当前卡与目标卡之间建立连线。source 固定为当前对话卡。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标卡 id，如 card_003"},
                    "type": {
                        "type": "string",
                        "enum": ["先后", "关联"],
                        "description": "关系类型",
                    },
                    "note": {"type": "string", "description": "可选备注"},
                },
                "required": ["target_id", "type"],
            },
        },
    ]


def _collect_card_meta(work_id: str) -> dict[str, dict[str, Any]]:
    """轻量收集 work 下所有卡的 meta（含 parent/card_type/relations）。"""
    cards: dict[str, dict[str, Any]] = {}
    cd = card_store._cards_dir(work_id)
    if not cd.exists():
        return cards
    wc_id = work_store.work_card_id_of(work_id)
    for path in cd.glob("card_*.md"):
        cid = path.stem
        m = card_store._meta_of(work_id, cid)
        if m is None:
            continue
        m["card_id"] = cid
        cards[cid] = m
    return cards


def can_read(current_id: str, target_id: str, cards: dict[str, dict[str, Any]]) -> bool:
    """读权限：target 在 current 的子树内，或在 current 的血统链上（含剧本卡），
    或在 current 的 relations 里 → True。

    cards: {card_id: meta dict（含 parent, card_type, relations）}
    """
    if target_id == current_id:
        return True
    cur = cards.get(current_id)
    if cur is None:
        return False
    tgt = cards.get(target_id)
    if tgt is None:
        return False

    # 1. 子树：反向遍历 target 的 parent 链，看能否到达 current
    visited: set[str] = set()
    node = tgt.get("parent", "")
    guard = 0
    while node and guard < 1000:
        guard += 1
        if node in visited:
            break
        visited.add(node)
        if node == current_id:
            return True
        nm = cards.get(node)
        if nm is None:
            break
        node = nm.get("parent", "")

    # 2. 血统链：反向遍历 current 的 parent 链，看是否含 target
    visited2: set[str] = set()
    node = cur.get("parent", "")
    guard = 0
    while node and guard < 1000:
        guard += 1
        if node in visited2:
            break
        visited2.add(node)
        if node == target_id:
            return True
        nm = cards.get(node)
        if nm is None:
            break
        node = nm.get("parent", "")

    # 3. relations：current 的 relations 里指向 target
    for rel in cur.get("relations") or []:
        if isinstance(rel, dict) and rel.get("target") == target_id:
            return True

    # 4. 锚卡全局可读：target 锚定剧本卡（系统派生结构）→ 任何卡可读
    for rel in tgt.get("relations") or []:
        if isinstance(rel, dict) and rel.get("type") == "锚定":
            anchor_tgt = cards.get(rel.get("target", ""))
            if anchor_tgt is not None and anchor_tgt.get("card_type") == "剧本":
                return True

    return False


def can_edit(current_id: str, target_id: str, cards: dict[str, dict[str, Any]]) -> bool:
    """写权限：target 在 current 的子树内（含 current 自身）→ True。

    血统链上级不可写、契约卡不可写。
    """
    if target_id == current_id:
        return True
    # 反向遍历 target 的 parent 链，能否到达 current
    tgt = cards.get(target_id)
    if tgt is None:
        return False
    visited: set[str] = set()
    node = tgt.get("parent", "")
    guard = 0
    while node and guard < 1000:
        guard += 1
        if node in visited:
            break
        visited.add(node)
        if node == current_id:
            return True
        nm = cards.get(node)
        if nm is None:
            break
        node = nm.get("parent", "")
    return False


async def execute_tool(
    work_id: str,
    current_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    cards: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """执行单个工具调用，返回结果 dict（成功返回数据，越权/失败返回 {"error":...}）。

    不抛异常（吞掉异常转 error，便于继续工具循环）。
    """
    try:
        if tool_name == "read_card":
            target_id = arguments.get("card_id", "")
            # [语义修正 2026-10-02] 先判存在再判权限：已删除的卡不再误报"越权"
            if target_id not in cards:
                return {"error": f"卡片不存在: {target_id}"}
            if not can_read(current_id, target_id, cards):
                return {"error": f"越权：无权读取 {target_id}"}
            result = await card_store.read_card(work_id, target_id)
            return {
                "card_id": target_id,
                "title": result["meta"].get("title", ""),
                "parent": result["meta"].get("parent", ""),
                "relations": result["meta"].get("relations") or [],
                "body": result["body"].get("conclusion", ""),
            }
        if tool_name == "edit_card":
            target_id = arguments.get("card_id", "")
            content = arguments.get("content", "")
            # [语义修正 2026-10-02] 先判存在再判权限
            if target_id not in cards:
                return {"error": f"卡片不存在: {target_id}"}
            if not can_edit(current_id, target_id, cards):
                return {"error": f"越权：无权编辑 {target_id}"}
            current = await card_store.read_card(work_id, current_id)
            cur_title = current["meta"].get("title", "")
            history_note = f"由「{cur_title}」对话编辑正文"
            # [白板版] 模型只见"正文"一个概念；内部固定落存储层正文区（conclusion）
            result = await card_store.update_card(
                work_id, target_id, {"conclusion": content}, history_note,
            )
            if "error" in result:
                return result
            return {"card_id": target_id, "ok": True}
        if tool_name == "delete_card":
            # [模型删卡 2026-10-02] 用户在系统内授权（"你现在能删卡了，再试试"）。
            # 权限＝写权限（管辖域内）；结构性护栏由存储层 delete_card 机械执行
            # （剧本根卡拒删、有子卡拒删），本层不另设判断。
            target_id = arguments.get("card_id", "")
            if target_id not in cards:
                return {"error": f"卡片不存在: {target_id}"}
            if not can_edit(current_id, target_id, cards):
                return {"error": f"越权：无权删除 {target_id}"}
            if target_id == work_store.work_card_id_of(work_id):
                return {"error": f"剧本根卡不可删除: {target_id}"}
            try:
                result = await card_store.delete_card(work_id, target_id)
            except card_store.CardDeleteDeniedError as exc:
                return {"error": str(exc)}
            if "error" in result:
                return result
            # cards 元数据缓存同步，防止同轮后续工具仍看到已删卡
            cards.pop(target_id, None)
            return result
        if tool_name == "list_manuscript":
            chapters = manuscript_store.list_chapters(work_id)
            if not chapters:
                return {"chapters": [], "note": "本项目尚无原文"}
            return {"chapters": chapters}
        if tool_name == "read_manuscript":
            if not manuscript_store.list_chapters(work_id):
                return {"error": "本项目尚无原文"}
            return manuscript_store.read_chapter(
                work_id,
                arguments.get("chapter", ""),
                para_start=arguments.get("para_start"),
                para_end=arguments.get("para_end"),
            )
        if tool_name == "link_cards":
            target_id = arguments.get("target_id", "")
            rel_type = arguments.get("type", "")
            note = (arguments.get("note") or "").strip()
            if rel_type not in MODEL_REL_TYPES:
                return {"error": f"关系类型非法: {rel_type}，只允许 先后/关联"}
            if target_id not in cards:
                return {"error": f"卡片不存在: {target_id}"}
            if target_id == current_id:
                return {"error": "不能与自身建立关系"}
            if not can_read(current_id, target_id, cards):
                return {"error": f"越权：无权与 {target_id} 建立关系"}
            current = await card_store.read_card(work_id, current_id)
            existing = current["meta"].get("relations") or []
            for rel in existing:
                if (isinstance(rel, dict)
                        and rel.get("target") == target_id
                        and rel.get("type") == rel_type):
                    # 幂等：同 target+type 已存在，不重复追加
                    return {"ok": True, "existed": True}
            new_rel: dict[str, Any] = {"target": target_id, "type": rel_type}
            if note:
                new_rel["note"] = note
            cur_title = current["meta"].get("title", "")
            result = await card_store.update_card(
                work_id, current_id,
                {"relations": list(existing) + [new_rel]},
                f"由「{cur_title}」对话建立关系：{rel_type} → {target_id}",
            )
            if "error" in result:
                return result
            return {"ok": True, "source": current_id, "target": target_id, "type": rel_type}
        if tool_name == "search_manuscript":
            if not manuscript_store.list_chapters(work_id):
                return {"error": "本项目尚无原文"}
            hits = manuscript_store.search_manuscript(
                work_id,
                arguments.get("keyword", ""),
                limit=int(arguments.get("limit") or 10),
            )
            return {"hits": hits}
        # [联网能力 2026-10-02] 只给主模型的中性读工具：结果仅作工具返回进
        # 当前调用，不写卡、不进常设上下文；三思考席收 tools=None 物理上不可调用
        if tool_name == "web_search":
            from . import search_tool
            return search_tool.web_search(
                arguments.get("query", ""),
                count=int(arguments.get("count") or 10),
            )
        if tool_name == "fetch_web":
            from . import search_tool
            return search_tool.fetch_web(arguments.get("url", ""))
        return {"error": f"未知工具: {tool_name}"}
    except card_store.CardNotFoundError:
        return {"error": f"卡片不存在: {arguments.get('card_id','')}"}
    except manuscript_store.ChapterNotFoundError:
        return {"error": f"章节不存在: {arguments.get('chapter','')}"}
    except Exception as exc:  # noqa: BLE001
        logger.error("execute_tool 异常 tool=%s args=%s: %s", tool_name, arguments, exc)
        return {"error": str(exc)}
