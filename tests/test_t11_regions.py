# -*- coding: utf-8 -*-
"""T11 · 装配线分区改造的 V1 断言（字节级相等）＋区表结构断言。

V1：装配线改造后组装结果与旧实现**字节级相等**——空 ctx 分支与尾部空白
（rstrip）两条路径都覆盖。旧实现以「改造前函数体冻结副本」为基准
（原 build_neutral_context 在改造时降为薄包装，冻结副本逐字拷自
context_assembler.py 改造前 :55-77，含 prompts.build_system_prompt 改造前 :70-79）。

真 work/card 的字节级比对由独立脚本 _t11_evidence/v1_assert.py 对真实
works/ 只读执行（回归网铁律一：pytest 不得触碰真实数据目录）。
"""
from __future__ import annotations

import json

import pytest

from src.api import context_assembler, context_regions, prompts
from src.api.prompts import BASE_PROMPT
from src.storage import card_store, work_store

# ---------------------------------------------------------------------------
# 改造前函数体冻结副本（V1 比对基准；逐字拷自改造前源码，勿改动）
# ---------------------------------------------------------------------------


def _legacy_build_neutral_context(work_id: str, current_id: str) -> str:
    cards = context_assembler._load_cards_index(work_id)
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


def _legacy_build_system_prompt(context_block: str) -> str:
    if context_block and context_block.strip():
        return BASE_PROMPT + "\n" + context_block.rstrip() + "\n"
    return BASE_PROMPT


def _legacy_build_viewpoint_system_prompt(context_block: str) -> str:
    return context_block or ""


# ---------------------------------------------------------------------------
# 区表结构（V9：区表无独立「当前轮」区）
# ---------------------------------------------------------------------------


def test_region_table_six_regions_no_current_turn_region():
    # [T12] 凝练记忆区（order 200，condense_fold）按任务书 REL-01 append——
    # T11 六区条目一字不动，仅区表追加 200；本断言随 append 最小随改。
    orders = [r["order"] for r in context_regions.REGION_TABLE]
    names = {r["name"] for r in context_regions.REGION_TABLE}
    assert orders == [0, 100, 150, 200, 300, 900, 999]
    assert names == {"使用说明", "卡片清单", "当前卡", "记忆（condense_fold）", "对话历史", "本轮工具轮", "用户消息"}
    # 用户 2026-10-04 23:26 明令：不引入「当前轮」概念
    assert "当前轮" not in names
    assert not any("当前轮" in r["name"] or "当前轮" in r["source"]
                   for r in context_regions.REGION_TABLE)
    # join 规则的通道归属
    by_order = {r["order"]: r for r in context_regions.REGION_TABLE}
    assert by_order[0]["channel"] == "system" and by_order[0]["seats"] == ("main",)
    assert by_order[900]["channel"] == "messages" and by_order[900]["seats"] == ("main",)
    assert by_order[999]["channel"] == "user"


# ---------------------------------------------------------------------------
# V1：新旧两路字节级相等（沙箱 work：多卡树形、单卡、当前卡缺位）
# ---------------------------------------------------------------------------


async def _make_work_with_cards():
    wdata = await work_store.create_work("V1断言剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    await card_store.create_card(wid, "子卡甲", "用户直接创建", parent="card_001")
    child = await card_store.create_card(wid, "子卡乙", "用户直接创建", parent="card_001")
    grandchild = await card_store.create_card(
        wid, "孙卡丙", "用户直接创建", parent=child["meta"]["card_id"])
    return wid, child["meta"]["card_id"], grandchild["meta"]["card_id"]


async def test_v1_byte_equal_multi_card_tree(sandbox):
    wid, child_id, grandchild_id = await _make_work_with_cards()
    for current in ("card_001", child_id, grandchild_id):
        old = _legacy_build_neutral_context(wid, current)
        new = context_assembler.build_neutral_context(wid, current)
        assert old == new
        assert old.encode("utf-8") == new.encode("utf-8")
        # 全 system prompt 两路也逐字相等
        assert (_legacy_build_system_prompt(old).encode("utf-8")
                == prompts.build_system_prompt(new).encode("utf-8"))
        assert (_legacy_build_viewpoint_system_prompt(old).encode("utf-8")
                == prompts.build_viewpoint_system_prompt(new).encode("utf-8"))


async def test_v1_byte_equal_current_card_missing(sandbox):
    wdata = await work_store.create_work("V1缺当前卡剧本")
    wid = wdata["work_id"]
    old = _legacy_build_neutral_context(wid, "card_999")
    new = context_assembler.build_neutral_context(wid, "card_999")
    assert old == new
    assert old.encode("utf-8") == new.encode("utf-8")
    # 没有来源就不出现这个区：150 区缺席
    regions = context_assembler.build_context_regions(wid, "card_999")
    assert [r["order"] for r in regions] == [100]


async def test_v1_empty_and_rstrip_paths_of_build_system_prompt():
    """空 ctx 分支与 rstrip 两条路径（V1 硬要求的两条路径）。"""
    cases = [
        "",                    # 空 ctx → 裸 BASE_PROMPT
        "   ",                 # 全空白 ctx → strip 判空 → 裸 BASE_PROMPT
        " \n\t ",              # 同上（混合空白）
        "## 卡片清单",          # 正常无尾空白
        "## 卡片清单\n  \n \n",  # 尾部空白 → rstrip 路径
        "## 卡片清单\n\n## 当前卡\ncard_001 · x\n\n\n",
    ]
    for ctx in cases:
        old = _legacy_build_system_prompt(ctx)
        new = prompts.build_system_prompt(ctx)
        assert old == new, repr(ctx)
        assert old.encode("utf-8") == new.encode("utf-8"), repr(ctx)
    # 两条路径的机械确认：空/空白走裸 BASE_PROMPT；带尾空白的被剥净
    assert prompts.build_system_prompt("") == BASE_PROMPT
    assert prompts.build_system_prompt("  ") == BASE_PROMPT
    assert prompts.build_system_prompt("abc\n  \n") == BASE_PROMPT + "\n" + "abc" + "\n"


async def test_v1_context_regions_join_equals_neutral_context(sandbox):
    wid, _child, _grand = await _make_work_with_cards()
    regions = context_assembler.build_context_regions(wid, "card_001")
    joined = context_regions.assemble_regions([r["text"] for r in regions])
    assert joined == context_assembler.build_neutral_context(wid, "card_001")
    assert [r["order"] for r in regions] == [100, 150]
    # regions 条目只带指针性元数据，不带正文复制字段以外的意外大字段
    for r in regions:
        assert set(r) == {"order", "name", "channel", "seats", "text", "entries"}


async def test_region_log_entries_shape(sandbox):
    """T4/T7：regions 日志条目形状（order/名称/通道/席位/字节数/指纹/条目数）。"""
    wid, _child, _grand = await _make_work_with_cards()
    regions = context_assembler.build_context_regions(wid, "card_001")
    import hashlib

    entries = []
    for r in regions:
        raw = r["text"].encode("utf-8")
        entries.append({
            "order": r["order"], "name": r["name"], "channel": r["channel"],
            "seats": list(r["seats"]), "bytes": len(raw),
            "fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest()[:16],
            "entries": r["entries"],
        })
    assert entries[0]["name"] == "卡片清单" and entries[0]["entries"] >= 1
    assert entries[1]["name"] == "当前卡" and entries[1]["entries"] == 1
    assert all(set(e) == {"order", "name", "channel", "seats", "bytes",
                          "fingerprint", "entries"} for e in entries)
    # 纯序列化自检（防止手滑改动形状）
    json.dumps(entries, ensure_ascii=False)
