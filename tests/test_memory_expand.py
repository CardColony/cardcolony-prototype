# -*- coding: utf-8 -*-
"""记忆展开（照抄 dsh `tools/retrieval/expand-tool.ts` 语义）。

判据：
X1 锚点找不到 ⇒ **明确"未找到"**（不静默空）
X2 正常：给结论 + 描述层（archive）+ **逐字原文**（coveredSeqs 取区间）
X3 已沉底 ⇒ 占位说明 + 原文 + **复活**（sunk=false）
X4 降级 ⇒ `needs_redescribe=True`
X5 原文表没数据 ⇒ 明说"尚未入库"，不装作有
X6 分页：给 page_chars 时按页切、并标 truncated
"""
from __future__ import annotations

from src.storage import (card_store, memory_domain as md, memory_expand as me,
                         origin_store, session_facade, work_store)


async def _mk() -> tuple[str, str]:
    w = await work_store.create_work("展开剧本")
    wid = w["work_id"]
    await card_store.create_card(wid, "根卡", "用户直接创建")
    for i in range(3):
        await session_facade.append(wid, "card_001", {"role": "user", "content": f"问{i}"})
        await session_facade.append(wid, "card_001", {"role": "assistant", "content": f"答{i}"})
    return wid, "card_001"


def _node(seq: int, gist: str, covered, **kw) -> dict:
    n = md.new_node("s1", seq, gist, kind="decision")
    n["level"] = 1
    n["upper"], n["lower"] = [], []
    n["coveredSeqs"] = covered
    n.update(kw)
    return n


# ---------------------------------------------------------------- X1

async def test_missing_anchor_says_not_found(sandbox):
    wid, cid = await _mk()
    md.save_chain(wid, cid, [_node(0, "甲", [0, 1])])
    r = me.expand(wid, cid, 999)
    assert r["found"] is False and "999" in r["reason"]
    assert me.render(r).startswith("未找到")


# ---------------------------------------------------------------- X2

async def test_expand_returns_gist_process_and_verbatim_blocks(sandbox):
    wid, cid = await _mk()
    md.save_chain(wid, cid, [_node(0, "甲乙两轮说完", [0, 1, 2, 3],
                                   process="描述层正文")])
    md.save_archive(wid, cid, [_node(0, "甲乙两轮说完", [0, 1, 2, 3],
                                     process="描述层正文")])
    r = me.expand(wid, cid, 0)
    assert r["found"] and r["gist"] == "甲乙两轮说完"
    assert r["process"] == "描述层正文"
    assert [b["content"] for b in r["blocks"]] == ["问0", "答0", "问1", "答1"], "逐字原文"
    text = me.render(r)
    assert "【记忆】" in text and "【描述】" in text and "【原文】" in text  # [F23] 回执不再用【结论】形态


# ---------------------------------------------------------------- X3

async def test_sunk_entry_revives_on_expand(sandbox):
    wid, cid = await _mk()
    md.save_chain(wid, cid, [_node(0, "沉底条目", [0, 1], sunk=True)])
    r = me.expand(wid, cid, 0, now_ms=123456)
    assert r["revived"] is True and r["sunk"] is False
    after = md.load_chain(wid, cid)
    assert after[0].get("sunk") is False, "落库要真复活"
    assert after[0].get("lastUsedAt") == 123456
    assert "复活" in me.render(r)


# ---------------------------------------------------------------- X4

async def test_degraded_marks_needs_redescribe(sandbox):
    wid, cid = await _mk()
    md.save_chain(wid, cid, [_node(0, "降级条目", [0], degraded=True)])
    r = me.expand(wid, cid, 0)
    assert r["degraded"] is True and r["needs_redescribe"] is True


# ---------------------------------------------------------------- X5

async def test_no_origin_rows_says_not_ingested(sandbox):
    wid, cid = await _mk()
    md.save_chain(wid, cid, [_node(500, "无原文条目", [500, 501])])
    r = me.expand(wid, cid, 500)
    assert r["found"] and r["blocks"] == []
    assert "尚未入库" in me.render(r)


# ---------------------------------------------------------------- X6

async def test_pagination(sandbox):
    wid, cid = await _mk()
    md.save_chain(wid, cid, [_node(0, "分页", [0, 1, 2, 3])])
    full = me.expand(wid, cid, 0)
    total = full["total_chars"]
    p0 = me.expand(wid, cid, 0, page=0, page_chars=4)
    assert len(p0["text"]) == 4 and p0["truncated"] is True
    p1 = me.expand(wid, cid, 0, page=1, page_chars=4)
    assert p1["text"] and p1["text"] != p0["text"]
    assert total > 4
