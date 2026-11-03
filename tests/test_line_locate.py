"""行号定位：上下文里给行号，模型按行改，不用复制原文。

[2026-10-29 用户令]
- 「如果你觉得需要填写原文才能编辑，那么为什么不是代码代劳？为什么需要它自己复制黏贴？」
- 精准编辑本来就有 ``lines="12-15"`` 这条纯行号通道（代码按行替换），但上下文里
  从来没给过行号 ⇒ 模型算不出第几行 ⇒ 只能退回 old_string 逐字复制原文。
  定位所需的信息本来就该由代码给。

钉住：
T1 现状块按行标号，号与 split("\\n") 下标 +1 一致
T2 用标出来的号调 edit_status(lines=…) 命中的就是那一行
T3 需求文档有草稿时**只标草稿**（工具改的是草稿），正稿不标——两份都标会串号
T4 使命块不标号（没有写通道）
T5 write_requirements_final 也吃 lines
"""

from __future__ import annotations

from src.api import card_tools, context_assembler, context_regions
from src.storage import card_store, work_store


async def _card(status: str = "", requirements: str = "",
                draft: str = ""):
    w = await work_store.create_work("行号定位")
    wid = str(w["work_id"])
    cid = work_store.work_card_id_of(wid)
    upd = {}
    if status:
        upd["status_block"] = status
    if requirements:
        upd["requirements"] = requirements
    if draft:
        upd["requirements_draft"] = draft
    if upd:
        await card_store.update_card(wid, cid, upd, "预置")
    return wid, cid


def _region(wid, cid) -> str:
    meta = card_store.iter_card_metas(wid)
    body, shadow = context_assembler._load_current_card_state(wid, cid)
    ctx = context_regions.RegionCtx(
        work_id=wid, card_id=cid, cur=meta.get(cid),
        cur_meta=card_store._meta_of(wid, cid),
        cur_body=body, cur_shadow=shadow)
    return context_regions.render_current_card_region(ctx)


def _block_text(region: str, head: str) -> str:
    i = region.index(head)
    return region[i:]


# ---------------------------------------------------------------- T1 / T2

async def test_status_block_numbered_and_line_mode_hits(sandbox):
    wid, cid = await _card(status="第一行\n第二行\n第三行")
    region = _region(wid, cid)
    seg = _block_text(region, "### 现状")
    assert " 1│ 第一行" in seg and " 3│ 第三行" in seg, seg[:400]

    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_edit_status(
        wid, cid, {"lines": "2", "new_string": "第二行（改）"}, cards)
    assert out.get("ok"), out
    assert out["at_line"] == 2
    card = await card_store.read_card(wid, cid)
    assert str(card["body"]["status_block"]) == "第一行\n第二行（改）\n第三行"


# ---------------------------------------------------------------- T3

async def test_requirements_numbers_follow_draft(sandbox):
    """[2026-11-01 用户令「草稿不进上下文」] 需求文档恒标正稿；草稿不在场，
    只有一行「另有一份草稿在改」的说明。改草稿的行号在 edit_requirements
    回执里（lines 定位依旧打草稿的行）。"""
    wid, cid = await _card(requirements="正稿甲\n正稿乙", draft="草稿甲\n草稿乙")
    region = _region(wid, cid)
    seg = _block_text(region, "### 需求文档")
    assert "│ 正稿甲" in seg, "需求文档恒标正稿"
    assert "草稿甲" not in seg, "草稿不进上下文"
    assert "草稿" in seg, "有草稿时要留一行状态说明"

    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_edit_requirements(
        wid, cid, {"lines": "1", "new_string": "草稿甲（改）"}, cards)
    assert out.get("ok"), out
    card = await card_store.read_card(wid, cid)
    assert str(card["meta"]["requirements_draft"]) == "草稿甲（改）\n草稿乙"


# ---------------------------------------------------------------- T4

async def test_mission_not_numbered(sandbox):
    wid, cid = await _card(status="事实一行")
    await card_store.update_card(wid, cid, {"mission": "使命一句话"}, "预置使命")
    region = _region(wid, cid)
    seg = _block_text(region, "### 使命")
    assert "使命一句话" in seg
    assert "│ 使命一句话" not in seg, "使命没有写通道，不该给行号"


# ---------------------------------------------------------------- T5

async def test_write_final_accepts_lines(sandbox):
    wid, cid = await _card(requirements="需求甲\n需求乙")
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools._h_write_requirements_final(
        wid, cid, {"lines": "2", "new_string": "需求乙（改）"}, cards)
    assert out.get("ok"), out
    card = await card_store.read_card(wid, cid)
    assert str(card["body"]["requirements"]) == "需求甲\n需求乙（改）"
    assert (card.get("meta") or {}).get("requirements_state") == "approved"
