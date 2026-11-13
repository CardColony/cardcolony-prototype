# -*- coding: utf-8 -*-
"""需求文档块的两种写模式（2026-10-22 用户裁定：不要只追加，也不要强制整块重写）。

判据：
R1 精准编辑：old_string 唯一 ⇒ 只替换那一处，块里其他原文一字不动
R2 old_string 出现多次 ⇒ 拒，且块**不变**
R3 old_string 找不到 ⇒ 拒，且块**不变**
R4 只给 old_string 或只给 new_string ⇒ 拒（必须成对）
R5 整块写：只给 text ⇒ 整块替换
R6 工具 schema：text 不再是必填；old_string/new_string 存在
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_store



async def _cards(wid: str) -> dict:
    return card_tools._collect_card_meta(wid)


async def _req(wid: str, cid: str) -> str:
    cur = await card_store.read_card(wid, cid)
    return str((cur["body"] or {}).get("requirements") or "")


# ---------------------------------------------------------------- R1

@pytest.mark.asyncio
async def test_precise_edit_replaces_only_that_span(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, cid, {
        "requirements": "R1 要支持导出\nR2 要支持批量\nR3 界面要暗色模式"}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "edit_requirements",
        {"old_string": "R2 要支持批量", "new_string": "R2 要支持批量（上限 500 条）"},
        await _cards(wid))
    assert res.get("ok") is True, res
    # [2026-10-28 草稿列] edit_requirements 写 meta.requirements_draft
    # （草稿空时以正稿为底）；正稿列不被它动——判据落到草稿列上。
    card = await card_store.read_card(wid, cid)
    draft = str((card.get("meta") or {}).get("requirements_draft") or "")
    assert draft == "R1 要支持导出\nR2 要支持批量（上限 500 条）\nR3 界面要暗色模式", draft
    assert await _req(wid, cid) == "R1 要支持导出\nR2 要支持批量\nR3 界面要暗色模式", \
        "正稿列不被草稿编辑动"


# ---------------------------------------------------------------- R2

@pytest.mark.asyncio
async def test_ambiguous_old_string_is_rejected_and_block_untouched(work):
    wid, cid = work["work_id"], work["work_card_id"]
    before = "R1 要支持导出\nR2 要支持导出\nR3 收尾"
    await card_store.update_card(wid, cid, {"requirements": before}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "edit_requirements",
        {"old_string": "要支持导出", "new_string": "要支持导入"}, await _cards(wid))
    assert res.get("ok") is False, res
    assert "2 次" in res["error"]["message"], res
    assert await _req(wid, cid) == before, "拒了就不能动块"


# ---------------------------------------------------------------- R3

@pytest.mark.asyncio
async def test_missing_old_string_is_rejected_and_block_untouched(work):
    wid, cid = work["work_id"], work["work_card_id"]
    before = "R1 要支持导出"
    await card_store.update_card(wid, cid, {"requirements": before}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "edit_requirements",
        {"old_string": "不存在的句子", "new_string": "X"}, await _cards(wid))
    assert res.get("ok") is False, res
    assert "找不到" in res["error"]["message"], res
    assert await _req(wid, cid) == before, "拒了就不能动块"


# ---------------------------------------------------------------- R4

@pytest.mark.asyncio
async def test_half_pair_is_rejected(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, cid, {"requirements": "R1 导出"}, "预置")
    for args in ({"old_string": "R1 导出"}, {"new_string": "R1 导入"}):
        res = await card_tools.execute_tool(
            wid, cid, "edit_requirements", args, await _cards(wid))
        assert res.get("ok") is False, (args, res)
        assert ("old_string" in res["error"]["message"]
                or "new_string" in res["error"]["message"]), res
    assert await _req(wid, cid) == "R1 导出", "拒了就不能动块"


# ---------------------------------------------------------------- R5

@pytest.mark.asyncio
async def test_whole_block_write_is_rejected(work):
    """[2026-10-28 去全量更新] 整块写通道已取消：只给 text 必须被拒、块零改动。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, cid, {"requirements": "旧内容一整块"}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "edit_requirements",
        {"text": "R1 新需求\nR2 验收口径"}, await _cards(wid))
    assert res.get("ok") is False, res
    assert "old_string" in res["error"]["message"], res
    assert await _req(wid, cid) == "旧内容一整块", "拒了就不能动块"


@pytest.mark.asyncio
async def test_no_args_is_rejected(work):
    wid, cid = work["work_id"], work["work_card_id"]
    res = await card_tools.execute_tool(wid, cid, "edit_requirements", {}, await _cards(wid))
    assert res.get("ok") is False, res


# ---------------------------------------------------------------- R6

def test_schema_has_no_whole_write_param():
    """[2026-10-28 去全量更新] schema 里**不该再有 text**（整块写通道已删）。"""
    fn = card_tools.TOOL_REGISTRY["edit_requirements"].schema
    props = fn["parameters"]["properties"]
    assert "text" not in props, props
    for k in ("old_string", "new_string"):
        assert k in props, k


# ---------------------------------------------------------------- 定位诊断 2026-11-02

@pytest.mark.asyncio
async def test_not_found_returns_nearest_window_with_lines(work):
    """[card_021 实案] 找不到时回近似原文窗＋行号（不是头 3000 字、
    也不是「去 read_card 核对」）——模型拿逐字原文修锚点，不必重读。"""
    wid, cid = work["work_id"], work["work_card_id"]
    before = ("头部条目\n" + "填充行\n" * 20 +
              "目标条款：上限 500 条\n目标条款：沿用旧制\n" +
              "填充行\n" * 20 + "收尾\n")
    await card_store.update_card(wid, cid, {"requirements": before}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "edit_requirements",
        {"old_string": "目标条款：上限 900 条\n目标条款：沿用旧制",
         "new_string": "X"}, await _cards(wid))
    assert res.get("ok") is False, res
    msg = res["error"]["message"]
    assert "目标条款：上限 500 条" in msg, msg          # 近似窗里是逐字原文
    assert "行起" in msg and "lines=" in msg, msg      # 给了行号与行定位出口
    assert await _req(wid, cid) == before, "拒了就不能动块"


@pytest.mark.asyncio
async def test_ambiguous_lists_match_positions(work):
    """多次命中给各命中行号＋行首——消歧也不用回读全文。"""
    wid, cid = work["work_id"], work["work_card_id"]
    before = "R1 相同句\n间隔行\nR2 相同句\n"
    await card_store.update_card(wid, cid, {"requirements": before}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "edit_requirements",
        {"old_string": "相同句", "new_string": "不同句"}, await _cards(wid))
    assert res.get("ok") is False, res
    msg = res["error"]["message"]
    assert "2 次" in msg and "第 1 行" in msg and "第 3 行" in msg, msg
    assert await _req(wid, cid) == before, "拒了就不能动块"


# ---------------------------------------------------------------------------
# read_card 分段读＋大块封顶（2026-11-02 · card_021 实案续）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_card_block_range_returns_lines(work):
    """block=需求文档＋para_start/para_end：按行区间只回一段，带总行数。"""
    wid, cid = work["work_id"], work["work_card_id"]
    doc = "\n".join(f"第{i}条需求" for i in range(1, 51))
    await card_store.update_card(wid, cid, {"requirements": doc}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "read_card",
        {"card_id": cid, "block": "需求文档", "para_start": 10, "para_end": 12},
        await _cards(wid))
    assert res.get("ok") is True, res
    data = res["data"]
    assert data["total_lines"] == 50
    assert data["read_lines"] == "10-12"
    assert data["content"] == "第10条需求\n第11条需求\n第12条需求"


@pytest.mark.asyncio
async def test_read_card_big_block_returns_full_text(work):
    """[截断裁定撤销] 大块不带区间回**全文**——READ_CARD_BLOCK_CAP 已删
    （_cap_block_text 恒透传，截断制造沟通断层）；导航靠模型自选
    para_start/para_end 区间读。"""
    wid, cid = work["work_id"], work["work_card_id"]
    doc = "\n".join(f"第{i:03d}条需求内容" for i in range(1, 201))
    await card_store.update_card(wid, cid, {"requirements": doc}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "read_card", {"card_id": cid}, await _cards(wid))
    assert res.get("ok") is True, res
    text = res["data"]["需求文档"]
    assert text == doc                          # 全文回，不截断
    # 区间读仍能拿后段（导航参数保留）
    res2 = await card_tools.execute_tool(
        wid, cid, "read_card",
        {"card_id": cid, "block": "需求文档", "para_start": 199},
        await _cards(wid))
    assert res2["data"]["read_lines"] == "199-200"
    assert "第200条需求内容" in res2["data"]["content"]


@pytest.mark.asyncio
async def test_read_card_block_range_out_of_bounds(work):
    """para_start 超界报 validation（不静默回空），块名非法报 validation。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, cid, {"requirements": "三行\n文档\n内容"}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "read_card",
        {"card_id": cid, "block": "需求文档", "para_start": 99},
        await _cards(wid))
    assert res.get("ok") is False
    assert "共 3 行" in str(res.get("error"))
    res2 = await card_tools.execute_tool(
        wid, cid, "read_card", {"card_id": cid, "block": "不存在的块"},
        await _cards(wid))
    assert res2.get("ok") is False


# ---------------------------------------------------------------------------
# 草稿编辑回执：改前轮内一次、改后只给触及块（2026-11-14 用户裁定）
# ---------------------------------------------------------------------------

async def _seed_draft(wid: str, cid: str) -> str:
    doc = ("### 甲块\n甲行1\n甲行2\n"
           "### 乙块\n乙行1\n乙行2\n"
           "### 丙块\n丙行1\n丙行2")
    await card_store.update_card(wid, cid, {"requirements_draft": doc}, "预置")
    return doc


@pytest.mark.asyncio
async def test_draft_receipt_baseline_once_per_run_then_touched(work):
    """同轮第二次编辑不再回吐全行号稿；改后回执＝触及块带行号正文＋全稿块目。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await _seed_draft(wid, cid)
    cards = await _cards(wid)
    card_tools.set_tool_run_id("run-once-A")
    try:
        r1 = await card_tools.execute_tool(
            wid, cid, "edit_requirements",
            {"old_string": "甲行1", "new_string": "甲行1改"}, cards)
        assert r1.get("ok") is True, r1
        d1 = r1["data"]
        assert "1│ ### 甲块" in (d1.get("需求文档·草稿（行号）") or ""), \
            "本轮首次编辑给全行号基线"

        r2 = await card_tools.execute_tool(
            wid, cid, "edit_requirements",
            {"old_string": "乙行1", "new_string": "乙行1改"}, cards)
        assert r2.get("ok") is True, r2
        d2 = r2["data"]
        assert "需求文档·草稿（行号）" not in d2, "同轮第二次不再给全行号稿"
        assert "需求文档" not in (d2.get("blocks") or {}), \
            "不再往 blocks 塞草稿全文副本"
        touched = d2["需求文档·草稿·改动块"]
        assert "乙行1改" in touched and "甲行1改" not in touched, \
            "改动块只回触及的那一 `### ` 节"
        assert "4│" in touched, "行号是全稿坐标（乙块起于第 4 行）"
        toc = d2["需求文档·草稿·块目"]
        assert "甲块" in toc and "乙块" in toc and "丙块" in toc, \
            "块目覆盖全稿给导航"
    finally:
        card_tools.set_tool_run_id("")


@pytest.mark.asyncio
async def test_draft_receipt_baseline_resets_next_run(work):
    """换一轮（新 pair_id）基线重新给。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await _seed_draft(wid, cid)
    cards = await _cards(wid)
    try:
        card_tools.set_tool_run_id("run-reset-1")
        r1 = await card_tools.execute_tool(
            wid, cid, "edit_requirements",
            {"old_string": "甲行1", "new_string": "甲行1改"}, cards)
        assert "需求文档·草稿（行号）" in r1["data"]
        card_tools.set_tool_run_id("run-reset-2")
        r2 = await card_tools.execute_tool(
            wid, cid, "edit_requirements",
            {"old_string": "乙行1", "new_string": "乙行1改"}, cards)
        assert "需求文档·草稿（行号）" in r2["data"], "新一轮基线重新给"
    finally:
        card_tools.set_tool_run_id("")


@pytest.mark.asyncio
async def test_draft_receipt_no_run_marker_keeps_baseline(work):
    """无轮标记的调用路（测试直调等）保守兜底：每次都给全行号稿。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await _seed_draft(wid, cid)
    cards = await _cards(wid)
    card_tools.set_tool_run_id("")
    for old, new in (("甲行1", "甲行壹"), ("甲行2", "甲行贰")):
        r = await card_tools.execute_tool(
            wid, cid, "edit_requirements",
            {"old_string": old, "new_string": new}, cards)
        assert r.get("ok") is True, r
        assert "需求文档·草稿（行号）" in r["data"], \
            "无轮标记时不省基线"
