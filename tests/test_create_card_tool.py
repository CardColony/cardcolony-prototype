# -*- coding: utf-8 -*-
"""建卡工具通道（create_card）· 回归网

[建卡工具化 2026-10-08] 钉住三件事：
1. **权限口径**：只能建自己的子卡片（用户 2026-10-08 14:14 定）——越权必拒。
2. **回执真身**：成功后返回真身 card_id，模型能确证建成（工具化的核心收益）。
3. **双通道并存 + 跨通道去重**：工具通道建过的卡名，文本块同名不重复建。

机制层铁律同conftest：不 import main、不触真实数据（沙箱由 autouse fixture 提供）。
真模型实弹验证走 tests/test_cardperm_live.py 那套（须显式开关），
本文件只钉纯行为，不烧模型额度。
"""
from __future__ import annotations

import pytest

from src.api import card_proposal, card_tools
from src.storage import card_store


# ---------------------------------------------------------------- schema 面

def test_create_card_in_tool_schemas():
    """工具表含 create_card，且带 title/parent/content 三参数。"""
    names = [t["name"] for t in card_tools.build_tool_schemas()]
    assert names.count("create_card") == 1
    schema = next(t for t in card_tools.build_tool_schemas()
                  if t["name"] == "create_card")
    assert set(schema["parameters"]["properties"]) == {"title", "parent", "content"}
    assert schema["parameters"]["required"] == ["title"]


def test_create_card_is_write_tool_not_readonly():
    """建卡落存储层新卡 ⇒ 属写工具，不进只读白名单（折叠【依据】语义依赖此分类）。"""
    assert "create_card" not in card_tools.READONLY_TOOLS


def test_tool_table_version_bumped():
    """工具集变更须版本化（B-15/ T12-R3 同款纪律）。

    [卡片信息结构改造 S3-8 · 2026-10-08] 推进到 tc-2：edit_card schema 随闸语义更新。
    """
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-2"


def test_base_prompt_declares_tool_and_drops_text_protocol():
    """新 prompt 教工具通道、旧 card-proposal 段已删（避免两套教法并存）。"""
    from src.api import prompts
    # [卡片信息结构改造 S1-6 · 2026-10-08] 推进到 tc-2：edit_card 说明改写为三块语法。
    assert prompts.BASE_PROMPT_VERSION == "tc-2"
    assert "create_card(title, parent?, content?)" in prompts.BASE_PROMPT
    assert "card-proposal" not in prompts.BASE_PROMPT
    assert "建卡接口" not in prompts.BASE_PROMPT


# ---------------------------------------------------------------- 权限面

async def _mk(work_id: str, title: str, parent: str) -> str:
    r = await card_store.create_card(work_id, title, "用户直接创建", parent=parent)
    return r["meta"]["card_id"]


async def test_create_card_defaults_parent_to_current(work, sandbox):
    """parent 缺省＝挂当前卡自身（建自己的下一层）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, cur, "create_card", {"title": "子议题", "content": "正文"}, cards)
    assert out.get("ok") is True
    assert out["parent"] == cur


async def test_create_card_accepts_own_descendant(work, sandbox):
    """parent 落在自己子树内 ⇒ 放行（建孙卡合法）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    child = await _mk(wid, "一级子卡", cur)
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, child, "create_card", {"title": "孙卡"}, cards)
    assert out.get("ok") is True
    assert out["parent"] == child


async def test_create_card_denies_out_of_subtree(work, sandbox):
    """越权：parent 是别的分支的卡 ⇒ 拒，且**不建卡**（结构事实，不是模型自觉）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    other = await _mk(wid, "别人的分支卡", cur)
    me = await _mk(wid, "我的分支卡", cur)
    before = len(list((sandbox.works / wid / "cards").glob("card_*.md")))
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, me, "create_card", {"title": "越权卡", "parent": other}, cards)
    assert "error" in out
    assert "只能建自己的子卡片" in out["error"]
    after = len(list((sandbox.works / wid / "cards").glob("card_*.md")))
    assert after == before, "越权必须零落盘"


async def test_create_card_rejects_sibling_parent(work, sandbox):
    """兄弟卡不可作为 parent（同级不属自己子树）——口径最严的一档。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    a = await _mk(wid, "甲", cur)
    b = await _mk(wid, "乙", cur)
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, a, "create_card", {"title": "越权", "parent": b}, cards)
    assert "error" in out


async def test_create_card_nonexistent_parent_not_oversight_error(work, sandbox):
    """已删/不存在的 parent 报「不存在」，不误报「越权」（语义修正同 read_card）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(
        wid, cur, "create_card", {"title": "X", "parent": "card_999"}, cards)
    assert out.get("error", "").startswith("父卡不存在")


async def test_create_card_empty_title_rejected(work, sandbox):
    """title 空 ⇒ 拒（不建空名卡）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(wid, cur, "create_card", {"title": "   "}, cards)
    assert "error" in out


# ---------------------------------------------------------------- 回执面

async def test_create_card_receipt_has_real_card_id(work, sandbox):
    """回执带真身 card_id —— 模型据此确证建成，不必再猜卡号。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(wid, cur, "create_card", {"title": "回执卡"}, cards)
    assert out["card_id"].startswith("card_")
    # 回执卡号必须是磁盘真身（不靠猜、不靠预判）
    assert (sandbox.works / wid / "cards" / f"{out['card_id']}.md").exists()
    # 正卡即派生影子卡（一正一影，R-03）
    assert (sandbox.works / wid / "shadows" / f"{out['card_id']}.md").exists()


async def test_create_card_syncs_meta_cache(work, sandbox):
    """建卡后同轮后续工具能看见新卡（meta 缓存同步，不留幽灵）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(wid, cur, "create_card", {"title": "新卡"}, cards)
    assert out["card_id"] in cards, "新卡须进入同轮 cards 字典"


# ------------------------------------------------- 文本通道同口径 + 跨通道去重

async def test_text_channel_enforces_same_subtree_rule(work, sandbox):
    """文本块通道施加**同一**权限口径（否则工具化改造留漏洞）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    other = await _mk(wid, "他人卡", cur)
    mine = await _mk(wid, "本卡", cur)
    before = len(list((sandbox.works / wid / "cards").glob("card_*.md")))
    created, denied = await card_proposal.apply_proposals(
        wid, mine, [{"title": "越权文本卡", "parent": other, "content": ""}])
    assert created == []
    assert len(denied) == 1 and "只能建自己的子卡片" in denied[0]["reason"]
    after = len(list((sandbox.works / wid / "cards").glob("card_*.md")))
    assert after == before, "越权文本卡必须零落盘"


async def test_text_channel_allows_own_subtree(work, sandbox):
    """文本块在子树内 ⇒ 照旧建卡（兜底通道不被砍）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    created, denied = await card_proposal.apply_proposals(
        wid, cur, [{"title": "文本建卡", "parent": "", "content": "x"}])
    assert len(created) == 1 and created[0]["parent"] == cur
    assert denied == []


async def test_cross_channel_dedup_same_title(work, sandbox):
    """同轮工具通道已建同名 ⇒ 文本块跳过（防一次回复建两遍）。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    created, denied = await card_proposal.apply_proposals(
        wid, cur, [{"title": "同名卡", "parent": "", "content": "x"}],
        exclude_titles={"同名卡"},
    )
    assert created == []
    assert len(denied) == 1 and "去重" in denied[0]["reason"]


async def test_apply_proposals_returns_tuple(work):
    """破坏性变更的显式钉子：返回 (created, denied) 二元组。"""
    wid = work["work_id"]
    cur = work["work_card_id"]
    out = await card_proposal.apply_proposals(wid, cur, [])
    assert isinstance(out, tuple) and len(out) == 2
