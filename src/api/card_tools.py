# -*- coding: utf-8 -*-
"""卡片工具与权限（模块 F）。

工具 schema（OpenAI function 格式，供 gateway 的 tools 参数）：
- read_card(card_id)：返回目标卡 meta + 四区 JSON。
- edit_card(card_id, section, content)：section ∈ {"conclusion","charter"}。

权限纯函数（可单测）：
- can_read(current_id, target_id, cards)
- can_edit(current_id, target_id, cards)

越权不抛异常，返回 {"error": "越权：..."} 给模型。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..storage import card_store, manuscript_store, methodology_store, work_store
from ..storage.card_store import _subtree_ids

logger = logging.getLogger("api.card_tools")

SECTION_CN = {"conclusion": "结论区", "charter": "职责区"}

# [phase8.6-D] 关系类型白名单
REL_TYPES = ("先后", "锚定", "兑现", "关联")


def build_tool_schemas() -> list[dict[str, Any]]:
    """OpenAI function calling 工具列表（直接传给 gateway.call(tools=...)）。"""
    return [
        {
            "name": "read_card",
            "description": "调取指定卡的全文（meta + 职责区/结论区/否决记录/修改历史）。只能读取你有权访问的卡。",
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
            "description": "修改指定下级卡的'结论区'或'职责区'。只能编辑你管辖域内的卡（子树）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id"},
                    "section": {
                        "type": "string",
                        "enum": ["conclusion", "charter"],
                        "description": "要修改的区：conclusion=结论区，charter=职责区",
                    },
                    "content": {"type": "string", "description": "新的整段内容（覆盖写）"},
                },
                "required": ["card_id", "section", "content"],
            },
        },
        # [phase8.5] 原文读取工具：原文是剧本的客观现状，所有卡对话均可读取，不做权限判定
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
        # [phase9-B] 方法论沉淀与查阅（跨剧本全局库，不做权限判定）
        {
            "name": "deposit_methodology",
            "description": "把跨剧本可复用的推演方法结论沉淀进全局方法论库（同名条目会被更新）。只沉淀手艺级通用结论；本书专属节点/世界观写卡片结论区。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "方法论标题，简短明确，如'长线假设的日常化埋点'"},
                    "content": {"type": "string", "description": "正文，建议三节：适用场景/做法/注意与反例"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选标签，如 [\"假设\",\"节奏\"]",
                    },
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "list_methodology",
            "description": "列出全局方法论库全部条目（id/标题/标签/来源/字数）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "read_methodology",
            "description": "按 id 调取方法论条目全文，如 read_methodology(id=\"m_0001\")。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "条目 id，如 m_0001"},
                },
                "required": ["id"],
            },
        },
        # [phase8.6-D] 关系建立工具：source 由服务端固定为当前对话卡
        {
            "name": "link_cards",
            "description": "在当前卡与目标卡之间建立关系连线。当你发现两张卡存在先后/锚定/兑现/关联时，用本工具建立连线。source 固定为当前对话卡。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "可省略；无论传什么，服务端一律以当前对话卡为 source"},
                    "target_id": {"type": "string", "description": "目标卡 id，如 card_003"},
                    "type": {
                        "type": "string",
                        "enum": ["先后", "锚定", "兑现", "关联"],
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

    # 4. [phase8.6-F] 目标锚全局可读：target 锚定剧本卡（全树之锚）→ 任何卡可读
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
            if not can_read(current_id, target_id, cards):
                return {"error": f"越权：无权读取 {target_id}"}
            result = await card_store.read_card(work_id, target_id)
            return {
                "card_id": target_id,
                "meta": result["meta"],
                "charter": result["body"]["charter"],
                "conclusion": result["body"]["conclusion"],
                "rejections": result["body"]["rejections"],
            }
        if tool_name == "edit_card":
            target_id = arguments.get("card_id", "")
            section = arguments.get("section", "")
            content = arguments.get("content", "")
            if section not in ("conclusion", "charter"):
                return {"error": f"section 非法: {section}，只允许 conclusion/charter"}
            if not can_edit(current_id, target_id, cards):
                return {"error": f"越权：无权编辑 {target_id}"}
            current = await card_store.read_card(work_id, current_id)
            cur_title = current["meta"].get("title", "")
            history_note = f"由「{cur_title}」对话编辑{SECTION_CN[section]}"
            result = await card_store.update_card(
                work_id, target_id, {section: content}, history_note,
            )
            if "error" in result:
                return result
            return {"card_id": target_id, "ok": True, "section": section}
        # [phase8.5] 原文工具：不做权限判定（原文是剧本的客观现状，所有卡对话均可读取）
        if tool_name == "list_manuscript":
            chapters = manuscript_store.list_chapters(work_id)
            if not chapters:
                return {"chapters": [], "note": "本剧本尚无原文"}
            return {"chapters": chapters}
        if tool_name == "read_manuscript":
            if not manuscript_store.list_chapters(work_id):
                return {"error": "本剧本尚无原文"}
            return manuscript_store.read_chapter(
                work_id,
                arguments.get("chapter", ""),
                para_start=arguments.get("para_start"),
                para_end=arguments.get("para_end"),
            )
        # [phase9-B] 方法论工具：全局库，不做权限判定；来源由服务端自动补当前剧本/当前卡
        if tool_name == "deposit_methodology":
            title = (arguments.get("title") or "").strip()
            content = (arguments.get("content") or "").strip()
            if not title:
                return {"error": "title 不能为空"}
            if not content:
                return {"error": "content 不能为空"}
            entry = methodology_store.save(
                title, content,
                tags=arguments.get("tags"),
                source_work=work_id,
                source_card=current_id,
            )
            if "error" in entry:
                return entry
            # 条目 id 回写当前卡 meta.methodology_refs（去重）
            current = await card_store.read_card(work_id, current_id)
            refs = list(current["meta"].get("methodology_refs") or [])
            if entry["id"] not in refs:
                result = await card_store.update_card(
                    work_id, current_id,
                    {"methodology_refs": refs + [entry["id"]]},
                    f"沉淀方法论：{title}",
                )
                if "error" in result:
                    return result
            return {"ok": True, "id": entry["id"], "title": entry["title"]}
        if tool_name == "list_methodology":
            return {"entries": methodology_store.list_all()}
        if tool_name == "read_methodology":
            mid = arguments.get("id", "")
            entry = methodology_store.read(mid)
            if entry is None:
                return {"error": f"方法论不存在: {mid}"}
            return entry
        # [phase8.6-D] link_cards：source 固定当前卡（无视模型传的 source_id，防越权）
        if tool_name == "link_cards":
            target_id = arguments.get("target_id", "")
            rel_type = arguments.get("type", "")
            note = (arguments.get("note") or "").strip()
            if rel_type not in REL_TYPES:
                return {"error": f"关系类型非法: {rel_type}，只允许 先后/锚定/兑现/关联"}
            if target_id not in cards:
                return {"error": f"目标卡不存在: {target_id}"}
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
                return {"error": "本剧本尚无原文"}
            hits = manuscript_store.search_manuscript(
                work_id,
                arguments.get("keyword", ""),
                limit=int(arguments.get("limit") or 10),
            )
            return {"hits": hits}
        return {"error": f"未知工具: {tool_name}"}
    except card_store.CardNotFoundError:
        return {"error": f"卡片不存在: {arguments.get('card_id','')}"}
    except manuscript_store.ChapterNotFoundError:
        return {"error": f"章节不存在: {arguments.get('chapter','')}"}
    except Exception as exc:  # noqa: BLE001
        logger.error("execute_tool 异常 tool=%s args=%s: %s", tool_name, arguments, exc)
        return {"error": str(exc)}
