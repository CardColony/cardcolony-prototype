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
    return str((cur["body"] or {}).get("rulings") or "")


# ---------------------------------------------------------------- R1

@pytest.mark.asyncio
async def test_precise_edit_replaces_only_that_span(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, cid, {
        "rulings": "R1 要支持导出\nR2 要支持批量\nR3 界面要暗色模式"}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "append_ruling",
        {"old_string": "R2 要支持批量", "new_string": "R2 要支持批量（上限 500 条）"},
        await _cards(wid))
    assert res.get("ok") is True, res
    assert res["data"]["mode"] == "edit"
    got = await _req(wid, cid)
    assert got == "R1 要支持导出\nR2 要支持批量（上限 500 条）\nR3 界面要暗色模式", got


# ---------------------------------------------------------------- R2

@pytest.mark.asyncio
async def test_ambiguous_old_string_is_rejected_and_block_untouched(work):
    wid, cid = work["work_id"], work["work_card_id"]
    before = "R1 要支持导出\nR2 要支持导出\nR3 收尾"
    await card_store.update_card(wid, cid, {"rulings": before}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "append_ruling",
        {"old_string": "要支持导出", "new_string": "要支持导入"}, await _cards(wid))
    assert res.get("ok") is False, res
    assert "2 次" in res["error"]["message"], res
    assert await _req(wid, cid) == before, "拒了就不能动块"


# ---------------------------------------------------------------- R3

@pytest.mark.asyncio
async def test_missing_old_string_is_rejected_and_block_untouched(work):
    wid, cid = work["work_id"], work["work_card_id"]
    before = "R1 要支持导出"
    await card_store.update_card(wid, cid, {"rulings": before}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "append_ruling",
        {"old_string": "不存在的句子", "new_string": "X"}, await _cards(wid))
    assert res.get("ok") is False, res
    assert "找不到" in res["error"]["message"], res
    assert await _req(wid, cid) == before, "拒了就不能动块"


# ---------------------------------------------------------------- R4

@pytest.mark.asyncio
async def test_half_pair_is_rejected(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, cid, {"rulings": "R1 导出"}, "预置")
    for args in ({"old_string": "R1 导出"}, {"new_string": "R1 导入"}):
        res = await card_tools.execute_tool(
            wid, cid, "append_ruling", args, await _cards(wid))
        assert res.get("ok") is False, (args, res)
        assert "old_string" in res["error"]["message"], res
    assert await _req(wid, cid) == "R1 导出", "拒了就不能动块"


# ---------------------------------------------------------------- R5

@pytest.mark.asyncio
async def test_whole_block_write_replaces_everything(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await card_store.update_card(wid, cid, {"rulings": "旧内容一整块"}, "预置")
    res = await card_tools.execute_tool(
        wid, cid, "append_ruling",
        {"text": "R1 新需求\nR2 验收口径"}, await _cards(wid))
    assert res.get("ok") is True, res
    assert res["data"]["mode"] == "rewrite"
    assert await _req(wid, cid) == "R1 新需求\nR2 验收口径"


@pytest.mark.asyncio
async def test_no_args_is_rejected(work):
    wid, cid = work["work_id"], work["work_card_id"]
    res = await card_tools.execute_tool(wid, cid, "append_ruling", {}, await _cards(wid))
    assert res.get("ok") is False, res


# ---------------------------------------------------------------- R6

def test_schema_no_longer_requires_text():
    """text 不再是必填（否则精准编辑模式根本调不动）。"""
    fn = card_tools.TOOL_REGISTRY["append_ruling"].schema
    assert "required" not in fn["parameters"], fn["parameters"]
    props = fn["parameters"]["properties"]
    for k in ("text", "old_string", "new_string"):
        assert k in props, k
