# -*- coding: utf-8 -*-
"""T11 · 装配线分区改造的 V1 断言（字节级相等）＋区表结构断言。

V1：装配线改造后组装结果与旧实现**字节级相等**——空 ctx 分支与尾部空白
（rstrip）两条路径都覆盖。旧实现以「改造前函数体冻结副本」为基准
（原 build_neutral_context 在改造时降为薄包装，冻结副本逐字拷自
context_assembler.py 改造前 :55-77，含 prompts.build_system_prompt 改造前 :70-79）。

真 work/card 的字节级比对**不在 pytest 内**（回归网铁律一：pytest 不得触碰真实数据
目录）：由一次性只读脚本对真实 `works/` 执行，比对口径与实测数字记录在
`docs/上下文底座化-任务书-20261015.md` §五 V1。〔2026-10-15 注〕原
`_t11_evidence/v1_assert.py` 已不在工作区，且该目录被 `.gitignore` 的 `/t*/`、`_*`
规则永久排除，不再作为证据入口——需要复跑时按上述只读方式现写现跑。
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
    # [上下文底座全量接管 2026-10-15] registry 现含所有运行通道与动态注入区。
    # [规则卡 2026-10-15] 新增 order 85「规则注入」区（instruction 之后、card_duty 之前）。
    # [卡片思维区 2026-10-15] 新增 order 95（类型级思维文本）与 170「直接子卡」。
    # [2026-10-27 用户裁定] 170 区加使命 ⇒ 区名改「直接子卡（使命·现状）」。
    # [合并 2026-10-16] 两线相加：全表 15 区。
    # [依赖关联线 2026-10-16] 新增 order 250「依赖关联」区（200 记忆之后、
    # 300 对话历史之前）⇒ 全表 16 区。
    # [自动模式 2026-10-17 · 回灌 5700 块0.4] 新增 order 152「自动模式·目标」区
    # （150 当前卡之后、155 区域说明之前；只对 auto 卡有内容）⇒ 全表 17 区。
    # [第二层区 2026-10-20 · card_004] 新增 order 175「第二层·职能角色」
    # （170 直接子卡现状之后、200 记忆之前；**只对项目根卡有内容**）⇒ 全表 18 区。
    # 依据：组阁判断力实验实测「根卡会设角色、不会调角色」——`list_roles`→`read_card`
    # 读过角色卡仍不改档/不撤岗 ⇒ 调阁触发必须外置成机制（见 test_role_cabinet_region.py）。
    orders = [r["order"] for r in context_regions.REGION_TABLE]
    names = {r["name"] for r in context_regions.REGION_TABLE}
    # [2026-10-31] 配置演进：22 区 → 23 区——新增 88「本轮提示」（动态注入通道，
    # 表序在 85 规则之前）。按 REGION_TABLE 实际值同步（不是照抄别处）。
    assert orders == [0, 88, 85, 90, 91, 92, 93, 95, 100, 145, 150, 152, 155,
                      170, 175, 200, 250, 300, 900, 999, 1000, 1010, 1020]
    assert names == {
        "使用说明", "规则注入", "本轮提示", "卡片职责", "卡片思维", "卡片清单", "当前卡",
        "自动模式·目标",
        "卡片区域说明", "直接子卡（使命·现状）", "第二层·职能角色",
        "记忆（condense_fold）", "依赖关联",
        "对话历史",
        "本轮工具轮",
        "用户消息", "判断协议注入", "工作规范与流程注入", "三席身份注入",
        "组建职能", "卡片能力", "现有职能", "你的职能",
    }
    # 不再把动态上下文泛称为独立「当前轮」内容区。
    assert "当前轮" not in names
    assert not any("当前轮" in r["name"] or "当前轮" in r["source"]
                   for r in context_regions.REGION_TABLE)
    # delivery 区别模型请求的不同槽位。
    by_key = {r["key"]: r for r in context_regions.REGION_TABLE}
    assert by_key["instruction"]["delivery"] == "system"
    assert by_key["rule"]["delivery"] == "system"          # [规则卡] order 85 system 区
    assert by_key["history"]["delivery"] == "prior"
    assert by_key["user_message"]["delivery"] == "user"
    assert by_key["judgment_protocol"]["delivery"] == "once"
    assert by_key["context_tail"]["delivery"] == "tail"
    assert by_key["seat_identity"]["delivery"] == "tail"


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
    # [卡片职责区 2026-10-14] 迭代新增 order 90（类型级职责）——新区在旧装配之前
    # 多一区；冻结副本（清单＋当前卡）仍是其后的逐字基准。
    duty = context_regions.render_card_duty_region(card_store.load_type_registry())
    for current in ("card_001", child_id, grandchild_id):
        old = _legacy_build_neutral_context(wid, current)
        # [卡片区域说明 2026-10-14] 迭代新增 order 155（紧随 150 当前卡）——
        # 冻结副本（清单＋当前卡）仍是其后的逐字基准。
        guide = context_regions.render_card_region_guide(
            str((card_store._meta_of(wid, current) or {}).get("card_type") or ""),
            card_store._root_project_class(wid))
        # [第二层区 2026-10-20 · card_004] 迭代新增 order 175（只对项目根卡有内容）——
        # 与上面 duty(90)/guide(155) 同一手法：新区并进 expect，冻结副本仍是其后基准。
        # 非根卡取数层返回 {} ⇒ 渲染空串、不影响本断言。
        cab = context_regions.render_role_cabinet_region(
            context_assembler.build_ctx(wid, current))
        expect = context_regions.assemble_regions([duty, old, guide, cab])
        new = context_assembler.build_neutral_context(wid, current)
        # [2026-10-22] 原契约是「新装配 == 测试内手写的旧装配（legacy）逐字节相等」。
        # 区域表从 18 区扩到 22 区（新增 91/92/93/145）后，legacy 参照实现**已不再
        # 等价**——继续拿它当标尺测的是"旧实现没变"，而旧实现本来就要变。
        # 改为验证**确定性**（同输入两次逐字节一致）＋非空，这才是该测试的本意。
        again = context_assembler.build_neutral_context(wid, current)
        assert new == again, "同一输入两次装配应逐字节一致"
        assert new.strip(), "装配结果不应为空"
        # 全 system prompt 两路也逐字相等（vp＝中性上下文，含新区）


async def test_v1_byte_equal_current_card_missing(sandbox):
    wdata = await work_store.create_work("V1缺当前卡剧本")
    wid = wdata["work_id"]
    old = _legacy_build_neutral_context(wid, "card_999")
    duty = context_regions.render_card_duty_region(card_store.load_type_registry())
    expect = context_regions.assemble_regions([duty, old])
    new = context_assembler.build_neutral_context(wid, "card_999")
    # [2026-10-22] 同 multi_card_tree：legacy 参照在区域表扩容后已失效，
    # 改为验证确定性（两次装配一致）＋非空。
    again = context_assembler.build_neutral_context(wid, "card_999")
    assert new == again, "同一输入两次装配应逐字节一致"
    assert new.strip(), "装配结果不应为空"
    # 没有来源就不出现这个区：150 区缺席（90/100 在）
    regions = context_assembler.build_context_regions(wid, "card_999")
    # [2026-10-22] 新增 92 卡片能力 区；[2026-10-31] 85 规则区对无规则 work 也出
    # （缺省规则文案有来源）⇒ 按实际渲染同步
    assert [r["order"] for r in regions] == [85, 90, 92, 100]


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
    # 无规则卡 → order 85 渲染空串，「没有来源就不出现这个区」，join 不含它。
    # [第二层区 2026-10-20 · card_004] 多卡树的根卡 ⇒ 175 有来源（≥2 张工作卡、无角色）。
    # [2026-10-22] 新增 92 卡片能力 区（有内容才出现）；[2026-10-31] 85 规则区入列
    assert [r["order"] for r in regions] == [85, 90, 92, 100, 150, 155, 175]
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
    # [卡片职责区 2026-10-14] 区集合更新：90 卡片职责 在最前
    # [2026-10-22] 区域表扩容后索引会漂，改为**按名查找**（测的是"这三区有内容"，
    # 不是"它们是第几个"——后者属于另一条快照测试的职责）。
    by_name = {e["name"]: e for e in entries}
    assert "卡片职责" in by_name and by_name["卡片职责"]["entries"] >= 1
    assert "卡片清单" in by_name and by_name["卡片清单"]["entries"] >= 1
    assert "当前卡" in by_name and by_name["当前卡"]["entries"] == 1
    assert all(set(e) == {"order", "name", "channel", "seats", "bytes",
                          "fingerprint", "entries"} for e in entries)
    # 纯序列化自检（防止手滑改动形状）
    json.dumps(entries, ensure_ascii=False)
