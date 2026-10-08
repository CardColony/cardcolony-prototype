# -*- coding: utf-8 -*-
"""卡片工具与权限（白板版 2026-10-02）。

工具接口只含机械事实：
- read_card：返回标题、父卡、连线、正文——不暴露内部四区语义
- edit_card(card_id, content)：覆盖写指定卡正文（内部落存储层正文区）
- link_cards：连线，模型可用类型限 先后/关联（锚定为系统内部关系，不对模型开放）

[卡片权限改造 2026-10-07] 权限纯函数：读＝全盘可读（存在即放行，亲属链/连线/锚卡读限整制去除——小说项目机制）；写＝自己的子树（不随读放开）。
越权不抛异常，返回 {"error": ...} 给模型。

[建卡工具化 2026-10-08] create_card 入标准工具通道（此前只走文本协议块
card-proposal，见 card_proposal.py）。两条通道并存：工具通道优先、文本通道
兜底（模型过渡期仍写文本块），跨通道同轮去重防重复建卡。
**建卡权限口径＝写权限（用户 2026-10-08 14:14 定）**：只能建自己的子卡片
（parent 落在current 的子树内；parent 缺省＝当前卡自身，即建自己的下一层）。
与 edit_card/delete_card 同用 can_edit 判据，不新增第二套权限规则。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..storage import card_store, manuscript_store, memory_store, shadow_store, work_store
from ..storage.hash_utils import parse_front_matter
from . import file_tools

logger = logging.getLogger("api.card_tools")

# [白板版 2026-10-02] 模型可建的连线类型收窄为两个中性关系；
# "锚定"（系统派生目标卡专用）与"兑现"（写作agent残留语义）不再对模型开放
MODEL_REL_TYPES = ("先后", "关联")

# [T11-T10] 只读工具白名单（折叠【依据】「可按原参数重取」语义的依据源）。
# 白名单是「工具的属性」，属性跟工具住一起（方案自拍 #15）；落点＝工具定义处。
# 名单＝调研纪要 §二 实证枚举（10 只读）；其余 6 件为写工具
# （edit_card/delete_card/update_shadow/link_cards/unlink_cards/write_file）。
# [T12-R3] 增补 read_memory（记忆锚点下探，condense_fold 展开通道，只读拉取）。
# [建卡工具化 2026-10-08] create_card 是写工具（落存储层新卡）——不进白名单。
READONLY_TOOLS = frozenset({
    "read_card", "read_shadow", "list_manuscript", "read_manuscript",
    "search_manuscript", "web_search", "fetch_web",
    "list_files", "read_file", "search_files",
    "read_memory",
})

# [T12-R3] 工具表基准版本化固化：工具集/schema 变更须递增版本号并对账基准指纹。
# tools-t12-1：T12 增补 read_memory（其余 16 件与 T11 基线逐字一致）。
# tools-tc-1：[建卡工具化 2026-10-08] 增补 create_card（共 18 件）。
TOOL_TABLE_VERSION = "tools-tc-1"


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
        # [T12-R3] 记忆锚点下探（condense_fold 展开通道）：按块序号锚点取回已凝
        # 记忆条目——锚点不可用则凝练历史不可回查（方案 §七 展开工具）。只读拉取；
        # 记忆条目不是卡片正文，正文仍经 read_card。
        {
            "name": "read_memory",
            "description": "下探指定卡的凝练记忆（condense_fold：已凝练历史经归拢的条目，含【结论】/【依据】）。可用块序号区间按锚点取回。记忆条目不是卡片正文——正文请用 read_card。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id，如 card_002；缺省＝当前卡"},
                    "from_seq": {"type": "integer", "description": "起始块序号（含），锚点下界；缺省不设下界"},
                    "to_seq": {"type": "integer", "description": "结束块序号（含），锚点上界；缺省不设上界"},
                },
                "required": [],
            },
        },
        # [建卡工具化 2026-10-08] 标准工具通道建卡。权限＝写权限（can_edit）：
        # 只能建自己的子卡片（用户 2026-10-08 14:14 定）——parent 缺省＝当前卡自身。
        # source 恒「模型生成」、card_type 恒「线索」（不向模型开放类型枚举，
        # 避免模型选型；类型是系统内部概念，与文字协议块旧行为逐字一致）。
        {
            "name": "create_card",
            "description": (
                "在你管辖范围内创建一张新卡片。只能建自己的子卡片："
                "parent 缺省＝当前卡自身；给 parent 时该卡必须落在你的子树内。"
                "建卡成功后系统会派生该卡的影子卡（过程记录），不需要你手动建。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "卡名，如「性能优化·监听器泄漏」"},
                    "parent": {
                        "type": "string",
                        "description": "父卡 id，如 card_006；缺省＝挂在当前卡下。必须在你的子树内，越权会被拒。",
                    },
                    "content": {
                        "type": "string",
                        "description": "卡的正文（结论正文，缺省空）。",
                    },
                },
                "required": ["title"],
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
            "name": "update_shadow",
            "description": "覆盖写你自己（当前卡）的影子卡——过程性记录。无 card_id 参数，只能写当前卡自己的影子。影子卡正文有且只有三节，每节内容边界（不得越界、不得改节名、不得加节）：【进度】只放本卡推进的时间线记录（什么时间、做到哪一步、当前什么状态），不写结论/分析/无关杂事；【产物清单】只放本卡推进中产生的文件/代码/文档的路径与用途指针（一行一个：路径＋一句话说明它是什么），只写指针不搬运内容正文；【迭代历史】只放改动指针（改了什么、为什么改），细节回既有通道查、不在此复述。三条禁止：①不许把正卡的结论正文写进影子卡（正卡写结论、影子卡写过程，不得混）；②不许写与本卡推进无关的内容；③不许把影子卡当备忘录堆杂事。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "影子正文全文（覆盖写）。必须含且仅含三节，节名固定：## 进度 / ## 产物清单 / ## 迭代历史。每节只放该节边界内的内容（进度＝时间线；产物清单＝路径与用途指针；迭代历史＝改动指针），不得写入正卡结论正文、不得写无关内容、不得堆杂事。"},
                },
                "required": ["content"],
            },
        },
        {
            "name": "read_shadow",
            "description": "读取指定卡的影子卡全文（过程性记录）。card_id 缺省＝当前卡自己的影子；上级卡可借此查看任一子卡的影子（子卡影子的更新由此到达上级）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "正卡 id，如 card_002；缺省＝当前卡"},
                },
                "required": [],
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
        {
            "name": "unlink_cards",
            "description": "解除当前卡上指向目标卡的连线（source 固定为当前对话卡）。目标卡已被删除时仍可解除——用于清理指向已删卡的悬空连线。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标卡 id"},
                    "type": {
                        "type": "string",
                        "enum": ["先后", "关联"],
                        "description": "可选，关系类型；缺省解除全部指向该目标的连线",
                    },
                },
                "required": ["target_id"],
            },
        },
    ] + file_tools.build_file_tool_schemas()


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
    # [影子卡 2026-10-03] 影子 meta 并入同一字典（R-09 读侧：影子挂在正卡之下，
    # can_read 四判据经 parent 链自动作用在影子上）；键＝shadow_{正卡id}
    sd = shadow_store._shadows_dir(work_id)
    if sd.exists():
        for path in sd.glob("*.md"):
            m = shadow_store._meta_of(work_id, path.stem)
            if m is None:
                continue
            sid = m.get("card_id") or shadow_store.shadow_id_of(path.stem)
            m["card_id"] = sid
            cards[sid] = m
    return cards


def can_read(
    current_id: str, target_id: str, cards: dict[str, dict[str, Any]],
    work_card_id: str = "",
) -> bool:
    """读权限：[卡片权限改造 2026-10-07] 全盘放开——target 存在即放行。

    用户令（2026-10-07 11:26）：任一卡可读全项目任何卡的信息，但不放开写；
    原按亲属链限读（判据①读自己/②子树/③血统链/④连线/⑤锚卡全局）是小说
    项目移植的机制，整制退役。仅保留存在性校验（不因放开而丢）：
    target 不在 cards（不存在/已删）→ False；current 不在 cards
    （无有效对话卡上下文，维持旧态行为）→ False。

    cards: {card_id: meta dict（含 parent, card_type, relations）}
    work_card_id: 形参保留（四个消费点调用签名不动）；判据⑤退役后无判据使用。
    """
    if cards.get(target_id) is None:
        return False
    if cards.get(current_id) is None:
        return False
    return True


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
            if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
                return {"error": f"越权：无权读取 {target_id}"}
            result = await card_store.read_card(work_id, target_id)
            return {
                "card_id": target_id,
                "title": result["meta"].get("title", ""),
                "parent": result["meta"].get("parent", ""),
                "relations": result["meta"].get("relations") or [],
                "body": result["body"].get("conclusion", ""),
            }
        # [T12-R3] 记忆锚点下探（condense_fold 展开工具）：权限同 read_card
        # （读＝子树∪血统链∪连线∪锚卡全局可读）；entries 原样返回，块序号区间
        # 是机械过滤非加工；越界/空记忆如实返回空（不构造内容）。
        if tool_name == "read_memory":
            target_id = arguments.get("card_id") or current_id
            if target_id not in cards:
                return {"error": f"卡片不存在: {target_id}"}
            if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
                return {"error": f"越权：无权读取 {target_id}"}
            state = memory_store.load_memory(work_id, target_id)
            entries = [e for e in (state.get("entries") or []) if isinstance(e, dict)]
            lo = arguments.get("from_seq")
            hi = arguments.get("to_seq")
            if isinstance(lo, int) and not isinstance(lo, bool):
                entries = [e for e in entries if isinstance(e.get("seq"), int) and e["seq"] >= lo]
            if isinstance(hi, int) and not isinstance(hi, bool):
                entries = [e for e in entries if isinstance(e.get("seq"), int) and e["seq"] <= hi]
            return {"card_id": target_id, "mark": state.get("mark", 0), "entries": entries}
        if tool_name == "create_card":
            # [建卡工具化 2026-10-08] 权限＝写权限（can_edit）：只能建自己的子卡片
            # （用户 2026-10-08 14:14 定）。parent 缺省＝当前卡自身（建自己下一层）；
            # 给了 parent 则该卡必须落在 current 的子树内——用 can_edit 判，
            # 与 edit_card/delete_card 同一判据，不新增第二套权限规则。
            # 结构性护栏（剧本根卡/带子卡者不可删）不在此——建卡不涉删。
            title = str(arguments.get("title", "") or "").strip()
            if not title:
                return {"error": "title 不能为空"}
            # parent 缺省＝当前卡自身；显式给空串也按缺省处理（模型常传空串）
            target_parent = str(arguments.get("parent", "") or "").strip() or current_id
            # 存在性先判：已删/不存在的 parent 不误报越权（与 read_card 同序）
            if target_parent not in cards:
                return {"error": f"父卡不存在: {target_parent}"}
            if not can_edit(current_id, target_parent, cards):
                return {
                    "error": (
                        f"越权：只能建自己的子卡片，{target_parent} 不在你的子树内"
                        f"（当前卡 {current_id}）"
                    )
                }
            result = await card_store.create_card(
                work_id, title, "模型生成",
                parent=target_parent,
                conclusion=str(arguments.get("content", "") or ""),
            )
            if "error" in result:
                return result
            # 建卡同步刷新 meta 缓存：同轮后续工具（如 link_cards→新卡）能看到新卡
            cards[result["meta"]["card_id"]] = {
                **result["meta"], "card_id": result["meta"]["card_id"],
            }
            # 回执给模型：卡号真身在此——模型据此确证建成、不必再猜（建卡工具化的核心收益）
            return {
                "ok": True,
                "card_id": result["meta"]["card_id"],
                "title": result["meta"]["title"],
                "parent": result["meta"]["parent"],
                "shadow": "已派生影子卡（过程记录，你不用管）",
            }
        if tool_name == "edit_card":
            target_id = arguments.get("card_id", "")
            content = arguments.get("content", "")
            # [语义修正 2026-10-02] 先判存在再判权限
            if target_id not in cards:
                return {"error": f"卡片不存在: {target_id}"}
            # [影子卡 2026-10-03] 影子进字典后 can_edit 会放行正卡的上级（子树判据），
            # 此守卫封死该后门：影子只能由对应正卡经 update_shadow 修改（R-06/R-09 写侧）
            if cards[target_id].get("card_type") == "shadow":
                return {"error": "影子卡只能由对应正卡经 update_shadow 修改"}
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
        if tool_name == "update_shadow":
            # [影子卡 2026-10-03] schema 无 card_id——写入目标恒为当前卡自己的影子
            # （R-09 写侧：不是可绕过的校验规则，是工具签名本身写不出别的目标）
            content = arguments.get("content", "")
            return await shadow_store.update_shadow(work_id, current_id, content)
        if tool_name == "read_shadow":
            # [影子卡 2026-10-03] 读侧跟随现有 can_read（R-09 读侧：不另设判据）；
            # 上级卡经此查子卡影子——推送（裁定 1）的到达机制
            target_id = arguments.get("card_id", "") or current_id
            if target_id.startswith("shadow_"):
                target_id = target_id[len("shadow_"):]
            if target_id not in cards:
                return {"error": f"卡片不存在: {target_id}"}
            if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
                return {"error": f"越权：无权读取 {target_id} 的影子"}
            raw = await shadow_store.read_shadow_raw(work_id, target_id)
            if raw is None:
                return {"error": f"影子卡不存在: {target_id}"}
            meta, _ = parse_front_matter(raw)
            return {
                "owner_card_id": target_id,
                "updated_at": str(meta.get("updated_at", "")),
                "content": raw,
            }
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
            if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
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
        if tool_name == "unlink_cards":
            # [解线 2026-10-03] 与 link_cards 对称；走 update_card(relations)，
            # 与前端 ✕ 删线同一条存储通道。悬空线主用例：目标卡已删时
            # relations 条目仍留在 source 卡上，此处不检查目标卡存在性。
            target_id = arguments.get("target_id", "")
            rel_type = (arguments.get("type") or "").strip()
            if not target_id:
                return {"error": "target_id 不能为空"}
            if rel_type and rel_type not in MODEL_REL_TYPES:
                return {"error": f"关系类型非法: {rel_type}，只允许 先后/关联"}
            current = await card_store.read_card(work_id, current_id)
            existing = current["meta"].get("relations") or []

            def _match(rel: Any) -> bool:
                if not isinstance(rel, dict):
                    return False
                if rel.get("target") != target_id:
                    return False
                return (not rel_type) or rel.get("type") == rel_type

            remaining = [r for r in existing if not _match(r)]
            removed = len(existing) - len(remaining)
            if removed == 0:
                detail = f" 的 {rel_type} 关系" if rel_type else ""
                return {"error": f"关系不存在: 当前卡无指向 {target_id}{detail}"}
            cur_title = current["meta"].get("title", "")
            result = await card_store.update_card(
                work_id, current_id,
                {"relations": remaining},
                f"由「{cur_title}」对话解除关系 → {target_id}",
            )
            if "error" in result:
                return result
            return {"ok": True, "source": current_id, "target": target_id, "removed": removed}
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
        # [并发修复 2026-10-04] 联网两工具是同步 def（requests 网络 IO，SearxNG
        # 最长 30s、阿里云备用通道最长 120s），必须卸载到线程池执行——直接
        # 调用会冻结 uvicorn 事件循环，期间所有请求（progress 轮询/其他剧本
        # 的消息/探活）全部排队，前端 15s 闸（api.js:26-28）一到就弹「请求超时」。
        # 这与 10-03 16:20 file_tools 修复同族，当时只修了文件工具、漏了这两个。
        # 这不是闸，是执行位置修正；search_tool.py 函数本身一行未改。
        if tool_name == "web_search":
            from . import search_tool
            return await asyncio.to_thread(
                search_tool.web_search,
                arguments.get("query", ""),
                count=int(arguments.get("count") or 10),
            )
        if tool_name == "fetch_web":
            from . import search_tool
            return await asyncio.to_thread(
                search_tool.fetch_web,
                arguments.get("url", ""),
            )
        # [本地文件能力 2026-10-03] 四件文件工具：边界与兜底全在 file_tools 内，
        # 与卡片树权限无耦合（材料全局共享），不进任何常设上下文
        # [并发修复 2026-10-03] 四件工具是同步 def（文件 IO 阻塞），必须
        # 卸载到线程池执行——直接调用会冻结 uvicorn 事件循环，期间所有
        # 请求（progress 轮询/其他剧本的消息/探活）全部排队（实测：全库
        # search_files 0.906s 期间事件循环 ticker 零打点）。这不是闸，
        # 是执行位置修正；工具函数本身一行未改。
        if tool_name == "list_files":
            return await asyncio.to_thread(
                file_tools.list_files,
                arguments.get("path", ""),
                limit=int(arguments.get("limit") or 200),
            )
        if tool_name == "read_file":
            return await asyncio.to_thread(
                file_tools.read_file,
                arguments.get("path", ""),
                para_start=arguments.get("para_start"),
                para_end=arguments.get("para_end"),
            )
        if tool_name == "search_files":
            return await asyncio.to_thread(
                file_tools.search_files,
                arguments.get("keyword", ""),
                arguments.get("path", ""),
                limit=int(arguments.get("limit") or 50),
            )
        if tool_name == "write_file":
            return await asyncio.to_thread(
                file_tools.write_file,
                arguments.get("path", ""),
                arguments.get("content", ""),
            )
        return {"error": f"未知工具: {tool_name}"}
    except card_store.CardNotFoundError:
        return {"error": f"卡片不存在: {arguments.get('card_id','')}"}
    except manuscript_store.ChapterNotFoundError:
        return {"error": f"章节不存在: {arguments.get('chapter','')}"}
    except Exception as exc:  # noqa: BLE001
        logger.error("execute_tool 异常 tool=%s args=%s: %s", tool_name, arguments, exc)
        return {"error": str(exc)}
