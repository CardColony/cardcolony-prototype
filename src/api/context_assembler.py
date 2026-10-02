# -*- coding: utf-8 -*-
"""上下文组装器（模块 D）。

替换 context_builder.py 的职责（旧文件保留不删，顶部加"已取代"注释）。

对"当前卡 X"组装 context_block，按顺序四段（推送最小化）：
1. 宪法：剧本卡结论区全文（若 X 是剧本卡则跳过）。
2. 血统链：剧本卡 → X 的父卡，每张输出【标题｜类型】+ 结论区全文。
3. 管辖域目录：X 的整个子树（不含 X），树形缩进。
4. 契约：X 的 relations 指向的卡。

card_block = X 自己的 职责区 + 结论区 + 否决记录区（修改历史不注入）。

build_directory 是纯函数，不做 I/O，供单测。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..storage import manuscript_store, work_store
from ..storage.card_store import _card_path, _meta_of, _subtree_ids
from ..storage.hash_utils import parse_front_matter, split_body_sections

logger = logging.getLogger("api.context_assembler")

BASE_DIR = Path(__file__).resolve().parents[2]


def _load_all_cards(work_id: str) -> dict[str, dict[str, Any]]:
    """扫描 work 下所有卡，返回 {card_id: {meta + body sections}}。

    meta 兜底 card_type/parent；body 拆四区。
    """
    result: dict[str, dict[str, Any]] = {}
    cd = _card_path(work_id, "card_000").parent  # works/{work_id}/cards
    if not cd.exists():
        return result
    wc_id = work_store.work_card_id_of(work_id)
    for path in sorted(cd.glob("card_*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_front_matter(raw)
        if not meta:
            continue
        meta["card_id"] = path.stem
        if not meta.get("card_type"):
            meta["card_type"] = "线索"
        if "parent" not in meta or meta.get("parent") is None:
            meta["parent"] = wc_id if meta["card_type"] != "剧本" else ""
        sections = split_body_sections(body)
        result[path.stem] = {"meta": meta, "sections": sections}
    return result


def _one_line_summary(conclusion: str) -> str:
    """结论区第一个非空行；超 60 字截断加 …；空写"（暂无结论）"。"""
    for line in conclusion.splitlines():
        s = line.strip()
        if s:
            return (s[:60] + "…") if len(s) > 60 else s
    return "（暂无结论）"


def build_directory(
    cards: dict[str, dict[str, Any]],
    root_id: str,
) -> list[str]:
    """纯函数：从 root_id 子树（不含 root）生成目录行列表（树形缩进，每层两空格）。

    cards: {card_id: {"meta": {...含 parent,card_type,title}, "sections": {...}}}
    检测到环时断开该边并记 warning，不崩溃。

    每行格式：
        card_id · 类型 · 标题 —— 一句话摘要
    """
    # 以 parent 字段建邻接表（parent → children）
    children: dict[str, list[str]] = {}
    for cid, info in cards.items():
        if cid == root_id:
            continue
        parent = info["meta"].get("parent", "")
        children.setdefault(parent, []).append(cid)

    lines: list[str] = []
    visited: set[str] = set()

    def _walk(node: str, depth: int) -> None:
        kids = sorted(children.get(node, []))
        for kid in kids:
            if kid in visited:
                logger.warning("build_directory: 检测到环，断开边 %s→%s", node, kid)
                continue
            visited.add(kid)
            info = cards.get(kid)
            if info is None:
                continue
            m = info["meta"]
            summary = _one_line_summary(info["sections"].get("conclusion", ""))
            indent = "  " * depth
            lines.append(
                f"{indent}{kid} · {m.get('card_type','线索')} · "
                f"{m.get('title','')} —— {summary}"
            )
            _walk(kid, depth + 1)

    _walk(root_id, 0)
    return lines


def build_manuscript_line(chapters: list[dict[str, Any]]) -> str:
    """纯函数：由 list_chapters 结果生成一行原文统计；无原文返回空串。

    [phase8.5] 统计数据由调用方（I/O 层）读好传入，本函数不做 I/O，可单测。
    """
    if not chapters:
        return ""
    n = len(chapters)
    m = sum(int(c.get("words", 0)) for c in chapters)
    return (
        f"原文：本剧本已有正文 {n} 章共 {m} 字，"
        f"可用 list_manuscript / read_manuscript / search_manuscript 工具查阅。"
    )


# [无内容预设改造 2026-10-02] build_methodology_line 已删除：
# 方法论库自动注入是模型自我繁殖污染链的一环（m_0001 事件），不再进入任何上下文。


def build_anchor_block(
    cards: dict[str, dict[str, Any]],
    work_card_id: str,
    current_id: str,
) -> str:
    """[phase8.6-F] 纯函数：目标锚块——锚定剧本卡的卡（通常是目标卡）结论全文。

    找不到锚卡、或锚卡就是 current_id 本人 → 返回空串。
    多张锚卡按 card_id 排序全部输出。
    """
    if not work_card_id:
        return ""
    parts: list[str] = []
    for cid in sorted(cards):
        if cid == current_id:
            continue
        info = cards[cid]
        rels = info["meta"].get("relations") or []
        anchored = any(
            isinstance(r, dict)
            and r.get("target") == work_card_id
            and r.get("type") == "锚定"
            for r in rels
        )
        if not anchored:
            continue
        m = info["meta"]
        concl = info["sections"].get("conclusion", "").strip()
        parts.append(
            f"【目标锚｜{m.get('title','')}｜{cid}】\n"
            + (concl if concl else "（暂无结论）")
        )
    return "\n\n".join(parts)


def _ancestry_chain(
    cards: dict[str, dict[str, Any]],
    work_card_id: str,
    target_id: str,
) -> list[str]:
    """从剧本卡 → target 的父卡 的链（含剧本卡，不含 target 自身）。

    用于"血统链"段。检测环时断开。
    """
    chain: list[str] = []
    visited: set[str] = {target_id}
    cur = cards.get(target_id, {}).get("meta", {}).get("parent", "")
    guard = 0
    while cur and guard < 1000:
        guard += 1
        if cur in visited:
            logger.warning("ancestry_chain: 检测到环，断开 at %s", cur)
            break
        if cur not in cards:
            break
        visited.add(cur)
        chain.append(cur)
        if cur == work_card_id:
            break  # 到剧本卡为止
        cur = cards[cur]["meta"].get("parent", "")
    # 翻转：剧本卡在前
    chain.reverse()
    return chain


def build_context_block(
    work_id: str,
    current_id: str,
) -> tuple[str, str]:
    """返回 (context_block, card_block)。

    context_block 含宪法/血统链/管辖域目录/契约四段；
    card_block 含当前卡的 charter+conclusion+rejections。
    """
    cards = _load_all_cards(work_id)
    wc_id = work_store.work_card_id_of(work_id)
    cur = cards.get(current_id)
    if cur is None:
        return "", ""

    is_work_card = (current_id == wc_id) or cur["meta"].get("card_type") == "剧本"

    blocks: list[str] = []

    # 1. 树根卡结论（剧本卡结论区全文；当前卡是树根卡则跳过）——[无内容预设改造] 去"宪法"定名
    if not is_work_card and wc_id and wc_id in cards:
        constitution = cards[wc_id]["sections"].get("conclusion", "").strip()
        if constitution:
            blocks.append("## 剧本卡（树根）结论\n\n" + constitution)

    # 1.5 [phase8.6-F] 目标锚：锚定剧本卡的卡结论全文，全局推送（锚卡自身对话时为空）
    anchor_block = build_anchor_block(cards, wc_id, current_id)
    if anchor_block:
        blocks.append("## 目标锚\n\n" + anchor_block)

    # 2. 血统链（剧本卡 → 当前卡的父卡，每张【标题｜类型】+ 结论全文）
    if not is_work_card:
        chain = _ancestry_chain(cards, wc_id, current_id)
        if chain:
            lines = ["## 血统链（从剧本卡到本卡）"]
            for cid in chain:
                info = cards.get(cid)
                if info is None:
                    continue
                m = info["meta"]
                lines.append(f"\n### 【{m.get('title','')}｜{m.get('card_type','线索')}】 {cid}")
                concl = info["sections"].get("conclusion", "").strip()
                lines.append(concl if concl else "（暂无结论）")
            blocks.append("\n".join(lines))

    # 3. 管辖域目录（当前卡子树，不含自身）
    subtree = _subtree_ids(work_id, current_id) - {current_id}
    # 用 build_directory 但限定在 subtree 内：直接传 current_id 作 root，
    # build_directory 会自己遍历 parent==current_id 的子；但若子卡再向下，
    # 其孙卡的 parent 是子卡 id，仍在 cards 字典里，OK。
    dir_lines = build_directory(cards, current_id)
    if dir_lines:
        blocks.append("## 管辖域目录\n\n" + "\n".join(dir_lines))

    # 3.5 原文统计（[phase8.5] 管辖域目录段之后；无原文一个字都不追加）
    manuscript_line = build_manuscript_line(manuscript_store.list_chapters(work_id))
    if manuscript_line:
        blocks.append(manuscript_line)

    # 4. 契约（relations 指向的卡）
    rels = cur["meta"].get("relations") or []
    contract_lines: list[str] = []
    for rel in rels:
        if not isinstance(rel, dict):
            continue
        target = rel.get("target", "")
        if not target or target not in cards:
            continue
        info = cards[target]
        m = info["meta"]
        summary = _one_line_summary(info["sections"].get("conclusion", ""))
        rel_type = rel.get("type", "")
        contract_lines.append(
            f"{target} · {m.get('card_type','线索')} · "
            f"{m.get('title','')} —— {summary}（关系：{rel_type}）"
        )
    if contract_lines:
        blocks.append("## 契约（关联卡）\n\n" + "\n".join(contract_lines))

    context_block = "\n\n".join(blocks)
    if context_block:
        context_block = "## 当前剧本上下文\n\n" + context_block

    # card_block：当前卡 charter + conclusion + rejections
    secs = cur["sections"]
    card_parts: list[str] = ["## 当前讨论卡", "", f"card_id: {current_id}", f"标题: {cur['meta'].get('title','')}"]
    if secs.get("charter", "").strip():
        card_parts.append(f"\n### 职责区\n{secs['charter'].strip()}")
    concl = secs.get("conclusion", "").strip()
    card_parts.append(f"\n### 结论区\n{concl if concl else '（暂无结论）'}")
    rej = secs.get("rejections", "").strip()
    if rej:
        card_parts.append(f"\n### 否决记录区\n{rej}")
    card_block = "\n".join(card_parts)

    return context_block, card_block
