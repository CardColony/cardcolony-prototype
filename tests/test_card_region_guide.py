# -*- coding: utf-8 -*-
"""卡片区域说明（system order 155）· 回归网（2026-10-14）。

新区＝「卡片区域说明」：把**正卡三块 ＋ 影子四节**「是什么、怎么写」写进上下文，
让模型不乱写。依据 `docs/卡片区域说明-上下文文案-20261014.md`。

钉死的四条口径（用户 2026-10-14 22:04）：
1. **第三块的名字由本树决定，文案里只出那一个名字**——研发树文案**不得出现**「定论」，
   研究树文案**不得出现**「需求文档」；
2. **不含「修改历史」**；
3. **每块要写清"怎么写"**；
4. 五类跨卡信息各有落点。

铁律一：沙箱内跑，不碰真实 works/。
"""
from __future__ import annotations

from src.api import context_assembler, context_regions
from src.storage import card_store, work_store

_GUIDE = context_regions.render_card_region_guide


async def _make_work():
    wdata = await work_store.create_work("卡片区域说明剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    return wid, "card_001"


# ---------------------------------------------------------------------------
# V1：第三块只出本树那一个名字（不许并列、不许互相提及）
# ---------------------------------------------------------------------------


def test_rd_card_shows_requirements_and_never_mentions_rulings():
    t = _GUIDE("sub_project", "rd_project")
    assert "需求文档" in t
    assert "定论" not in t, "研发树文案不得出现「定论」（用户 22:04 ①）"


def test_research_card_shows_rulings_and_never_mentions_requirements():
    t = _GUIDE("topic", "research_project")
    assert "定论" in t
    assert "需求文档" not in t, "研究树文案不得出现「需求文档」（用户 22:04 ①）"


def test_root_card_mission_is_project_scope():
    t = _GUIDE("project", "rd_project")
    assert "整个项目要做成什么" in t
    assert "这张卡要做什么" not in t


# ---------------------------------------------------------------------------
# V2：不含「修改历史」（用户 22:04 ②）
# ---------------------------------------------------------------------------


def test_no_change_history_mention():
    for ct, pc in (("sub_project", "rd_project"), ("project", "rd_project"),
                   ("topic", "research_project")):
        assert "修改历史" not in _GUIDE(ct, pc)


# ---------------------------------------------------------------------------
# V3：每块都写了「怎么写」（用户 22:04 ③）
# ---------------------------------------------------------------------------


def test_every_block_has_howto():
    t = _GUIDE("sub_project", "rd_project")
    # 正卡三块 ＋ 影子三块（迭代历史＝系统自动写；进度＝不必维护）各有交代
    assert t.count("写法：") >= 4
    for block in ("使命——", "现状——", "需求文档——", "产物清单——", "待协作事项——"):
        assert block in t, f"缺区块：{block}"
    assert "迭代历史——" in t and "进度——" in t


# ---------------------------------------------------------------------------
# V4：五类跨卡信息各有落点（对账方案 §五）
# ---------------------------------------------------------------------------


def test_five_cross_card_categories_present():
    t = _GUIDE("sub_project", "rd_project")
    for k in ("当前确定状态", "接口与约定", "依赖与影响范围",
              "交接物与权威位置", "待协作事项"):
        assert k in t, f"缺跨卡信息类别：{k}"


# ---------------------------------------------------------------------------
# V5：无来源 → 空串（「没有来源就不出现这个区」）
# ---------------------------------------------------------------------------


def test_empty_card_type_yields_empty():
    assert _GUIDE("", "rd_project") == ""


# ---------------------------------------------------------------------------
# V6：区表——155 存在、在 150 之后、仅主模型
# ---------------------------------------------------------------------------


async def test_region_present_after_current_card(sandbox, monkeypatch):
    wid, cid = await _make_work()
    monkeypatch.setattr(card_store, "_meta_of", lambda w, c: {"card_type": "sub_project"})
    monkeypatch.setattr(card_store, "_root_project_class", lambda w: "rd_project")
    regions = context_assembler.build_context_regions(wid, cid)
    orders = [r["order"] for r in regions]
    print("ORDERS:", orders)
    assert 155 in orders
    assert orders.index(150) < orders.index(155)
    r155 = next(r for r in regions if r["order"] == 155)
    assert r155["name"] == "卡片区域说明"
    assert r155["channel"] == "system"
    assert r155["seats"] == ("main",)
    assert set(r155) == {"order", "name", "channel", "seats", "text", "entries"}
    assert "需求文档" in r155["text"]


async def test_region_absent_without_card_type(sandbox, monkeypatch):
    wid, cid = await _make_work()
    monkeypatch.setattr(card_store, "_meta_of", lambda w, c: {})
    monkeypatch.setattr(card_store, "_root_project_class", lambda w: "rd_project")
    regions = context_assembler.build_context_regions(wid, cid)
    assert 155 not in [r["order"] for r in regions]


# ---------------------------------------------------------------------------
# V7：不写进会话、不改事件层（本区纯 system 装配）
# ---------------------------------------------------------------------------


async def test_region_is_pure_system_no_side_effects(sandbox, monkeypatch):
    from src.storage import session_facade
    wid, cid = await _make_work()
    monkeypatch.setattr(card_store, "_meta_of", lambda w, c: {"card_type": "sub_project"})
    monkeypatch.setattr(card_store, "_root_project_class", lambda w: "rd_project")
    context_assembler.build_context_regions(wid, cid)
    blocks = await session_facade.read(wid, cid)
    joined = "\n".join(str(b.get("content", "")) for b in blocks)
    assert "卡片区域说明" not in joined
    assert "交接物与权威位置" not in joined
