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

from ..storage import card_store, memory_store, work_store
from ..storage.hash_utils import parse_front_matter
from ..thinkchain import step_region as thinkchain_step_region
from ..toolcard import step_prompt as toolcard_step_prompt
from . import context_regions

logger = logging.getLogger("api.context_assembler")

BASE_DIR = Path(__file__).resolve().parents[2]


def _load_cards_index(work_id: str) -> list[dict[str, Any]]:
    """扫描 cards 目录，返回 [{card_id, title, parent}]（按文件名排序）。"""
    out: list[dict[str, Any]] = []
    cd = card_store.cards_dir(work_id)
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


def build_context_regions(work_id: str, current_id: str,
                          memory_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """分区构建（T11-T2，移植对应总表 #4/#5 适配改；T12 二期 append 200 区）：
    渲染 100/150 两个 system 区＋凝练记忆区（condense_fold，开关开且本卡有
    记忆条目时）。

    卡片清单扫描与筛选逻辑（_load_cards_index）一字不动；区渲染纯函数在
    context_regions（渲染零加工）。0 区（使用说明）只持 BASE_PROMPT 指针，
    由 prompts.build_system_prompt 在出口逐字引用，不在此渲染。

    [T12-SP-05] memory_state＝routes 层读好的凝练态（水位 mark＋entries）——
    IO 分离：routes 一次读取、结果传 200/300 区；未传（缺省 None）且开关开时
    此处兜底加载（本模块本就做卡片扫描 IO；context_regions 保持纯函数）。
    开关关（RD-08 缺省）或空记忆（entries 空）→ 不出现 200 区——装配与
    改造前逐字一致（V1/V6）。
    """
    cards = _load_cards_index(work_id)
    wc_id = work_store.work_card_id_of(work_id)
    cur = next((c for c in cards if c["card_id"] == current_id), None)

    regions: list[dict[str, Any]] = [{
        "order": 100,
        "name": "卡片清单",
        "channel": "system",
        "seats": ("main", "viewpoints"),
        "text": context_regions.render_card_list_region(cards, wc_id),
        "entries": len(cards),
    }]
    t150 = context_regions.render_current_card_region(cur)
    if t150:  # 没有来源就不出现这个区
        regions.append({
            "order": 150,
            "name": "当前卡",
            "channel": "system",
            "seats": ("main", "viewpoints"),
            "text": t150,
            "entries": 1,
        })
    # [工具卡底座 T6 · EC-048] 160 区：当前卡的工具卡步骤指令。
    # 只在该卡**确有工具卡运行时**时出现（没有来源就不出现这个区，同 150/200 纪律）。
    # 区内容只含事实与提示词原文（剧本名/当前步/本步要求/上一步落盘的文件路径），
    # **不含**任何评价、结论、剩余步数或通过与否（见 step_prompt 的可说/不可说表）。
    t160 = toolcard_step_prompt.build_region(work_id, current_id)
    if t160:
        regions.append(t160)
    else:
        # [迭代二 S4] 165 区：思维链活板事实三行（当前计划名/当前第几步/本步
        # 要求原文）。**与 t160 互斥**——卡有工具卡运行时（t160 在）则本区
        # 不注入（工具卡先到先得，任务书 S4 步骤 3）；无活板/恢复失败→区不
        # 出现（同 150/200「没有来源就不出现」纪律）。
        t165 = thinkchain_step_region.build_region(work_id, current_id)
        if t165:
            regions.append(t165)
    # [T12-2-1] 凝练记忆区（condense_fold）——order 200，紧随 150。
    # 空记忆 → 区跳过、零内容输出零报错（assemble_regions 空区跳过兜底）。
    state = memory_state
    if state is None and memory_store.condense_enabled():
        state = memory_store.load_memory(work_id, current_id)
    if state is not None:
        entries = state.get("entries") or []
        t200 = context_regions.render_memory_region(entries)
        if t200:  # 没有来源就不出现这个区
            regions.append({
                "order": 200,
                "name": "记忆（condense_fold）",
                "channel": "system",
                "seats": ("main",),
                "text": t200,
                "entries": len(entries),
            })
    return regions


def build_neutral_context(work_id: str, current_id: str) -> str:
    """中性上下文（主模型与三思考席共用同一份）：卡片清单 ＋ 当前卡标识。

    [T11-T2] 本函数降为薄包装：分区渲染后按 order join，输出与改造前逐字
    一致（V1 断言含空 ctx 分支与 rstrip 两条路径）。
    [拉取裁定 2026-10-02] 不推送任何卡片正文——模型需要时经 read_card 调取。
    上一轮模型写入的卡片内容不会再自动进入任何后续输入（回流断根）。
    """
    regions = build_context_regions(work_id, current_id)
    return context_regions.assemble_regions([r["text"] for r in regions])
