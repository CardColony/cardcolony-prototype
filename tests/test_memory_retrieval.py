# -*- coding: utf-8 -*-
"""记忆找回 §二验收（2026-11-20）：read_memory 锚点索引分页 ＋
memory_expand 逐字原文分页。

判据（方案 §二.1/.4）：
- read_memory：43 条隔离记忆实测分页 20/20/3；块号 0 合法；旧 seq 形态、
  合成锚点都在列；锚点升序且页间唯一；from_seq/to_seq 含端点；游标绑
  卡/筛选/索引版本（串卡、改筛、索引变 → validation）；每页抽锚点走展开。
- memory_expand：coveredSeqs=[0,2] 精确选块（块1 唯一标记在回执 0 次），
  逐页 text 拼接与两块存档逐字相等；page ＝字符偏移（0 起），每页 ≤6000 字
  且 render_tool_result 序列化 ≤32KiB；fragments 带块号/角色/双半开区间；
  缺块如实报；负页码 validation；revision 不匹配提示回第 0 页；沉底复活。
"""
from __future__ import annotations

import json

import pytest

from src.api import card_tools
from src.storage import (card_store, memory_domain as md, memory_expand as me,
                         memory_store, origin_store, session_facade, work_store)


async def _mk(prefix: str = "找回"):
    w = await work_store.create_work(f"{prefix}剧本")
    wid = w["work_id"]
    await card_store.create_card(wid, "根卡", "用户直接创建")
    return wid, "card_001"


def _meta(cid: str) -> dict:
    return {cid: {"card_id": cid, "parent": "", "card_type": "normal",
                  "perms": ["read"]}}


def _node(seq: int, gist: str, covered=None, **kw) -> dict:
    n = md.new_node("s1", seq, gist, kind="fact")
    n["level"], n["upper"], n["lower"] = 1, [], []
    if covered is not None:
        n["coveredSeqs"] = covered
    n.update(kw)
    return n


def _legacy_node(seq: int, gist: str) -> dict:
    """旧格式：顶层 seq、无 anchor。"""
    return {"seq": seq, "gist": gist, "level": 1, "role": "gist",
            "coveredSeqs": [seq], "status": "ongoing", "kind": "fact",
            "upper": [], "lower": []}


# ============================================================
# read_memory：锚点索引 ＋ 游标分页
# ============================================================

async def test_read_memory_paginates_20_20_3(sandbox):
    wid, cid = await _mk()
    entries = [_node(i, f"结论{i}", [i]) for i in range(40)]
    entries[0] = _node(0, "结论0（块号0合法）", [0])
    entries.append(_legacy_node(40, "旧 seq 形态条目"))          # 顶层 seq
    entries.append(_node(41, "合成锚点条目"))                   # 无覆盖→合成锚点
    entries.append(_node(42, "末条", [42]))
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 30,
                                    "entries": entries})
    meta = _meta(cid)

    seen, cursors = [], []
    cur = None
    for expect in (20, 20, 3):
        args = {"cursor": cur} if cur else {}
        r = await card_tools.execute_tool(wid, cid, "read_memory", args, meta)
        assert r["ok"], r
        d = r["data"]
        assert len(d["entries"]) == expect
        assert d["card_id"] == cid and d["mark"] == 30
        seen += [e["anchor"]["seq"] for e in d["entries"]]
        cursors.append(d["next_cursor"])
        cur = d["next_cursor"] or None
        if cur is None:
            assert not d["has_more"]
    assert seen == sorted(seen) and len(set(seen)) == 43, "升序且页间锚点唯一"
    # 每项带 ≤200 gist 预览 + level
    assert all(len(e["gist"]) <= 200 and isinstance(e["level"], int)
               for e in r["data"]["entries"])


async def test_read_memory_filter_inclusive_and_empty(sandbox):
    wid, cid = await _mk()
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "a", [0]), _node(5, "b", [5]),
                    _node(9, "c", [9]), _node(10, "d", [10])]})
    meta = _meta(cid)
    r = await card_tools.execute_tool(wid, cid, "read_memory",
                                      {"from_seq": 5, "to_seq": 10}, meta)
    assert [e["anchor"]["seq"] for e in r["data"]["entries"]] == [5, 9, 10]
    # 空记忆
    r2 = await card_tools.execute_tool(wid, cid, "read_memory",
                                       {"card_id": cid}, meta)
    # 先清掉
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
                                              "entries": []})
    r2 = await card_tools.execute_tool(wid, cid, "read_memory", {}, meta)
    assert r2["ok"] and r2["data"]["entries"] == [] \
        and r2["data"]["has_more"] is False and r2["data"]["next_cursor"] == ""


async def test_read_memory_cursor_boundaries(sandbox):
    wid, cid = await _mk()
    child = await card_store.create_card(wid, "子卡", "用户直接创建",
                                         parent=cid)
    cid2 = child["meta"]["card_id"]
    entries = [_node(i, f"结论{i}", [i]) for i in range(25)]
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0,
                                    "entries": entries})
    await memory_store.save_memory(wid, cid2,
                                   {"card_id": cid2, "mark": 0,
                                    "entries": entries})
    meta = _meta(cid) | _meta(cid2)
    meta[cid2] = {**meta[cid2], "parent": cid}

    r = await card_tools.execute_tool(wid, cid, "read_memory", {}, meta)
    cur = r["data"]["next_cursor"]
    # ① 串卡：cid2 拿 cid 的游标 → validation
    r2 = await card_tools.execute_tool(wid, cid2, "read_memory",
                                       {"cursor": cur}, meta)
    assert r2["ok"] is False and r2["error"]["code"] == "validation"
    # ② 改筛：同卡但带新筛选 → validation
    r3 = await card_tools.execute_tool(wid, cid, "read_memory",
                                       {"cursor": cur, "from_seq": 5}, meta)
    assert r3["ok"] is False and r3["error"]["code"] == "validation"
    # ③ 正常续页
    r4 = await card_tools.execute_tool(wid, cid, "read_memory",
                                       {"cursor": cur}, meta)
    assert r4["ok"] and len(r4["data"]["entries"]) == 5
    # ④ 翻页之间索引变化 → validation 提示重翻
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": entries + [_node(25, "新条目", [25])]})
    r5 = await card_tools.execute_tool(wid, cid, "read_memory",
                                       {"cursor": cur}, meta)
    assert r5["ok"] is False and r5["error"]["code"] == "validation"
    assert "第一页" in r5["error"]["message"]
    # ⑤ 烂游标
    r6 = await card_tools.execute_tool(wid, cid, "read_memory",
                                       {"cursor": "not-a-cursor"}, meta)
    assert r6["ok"] is False and r6["error"]["code"] == "validation"


async def test_read_memory_index_expand_each_page(sandbox):
    """每页至少抽一个锚点走 memory_expand——索引↔展开同一套锚点语义。"""
    wid, cid = await _mk()
    for i in range(23):
        await session_facade.append(wid, cid,
                                    {"role": "user", "content": f"块{i}正文"})
    entries = [_node(i, f"结论{i}", [i]) for i in range(23)]
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0,
                                    "entries": entries})
    meta = _meta(cid)
    cur = None
    while True:
        args = {"cursor": cur} if cur else {}
        r = await card_tools.execute_tool(wid, cid, "read_memory", args, meta)
        assert r["ok"]
        d = r["data"]
        seq = d["entries"][-1]["anchor"]["seq"]
        xr = await card_tools.execute_tool(wid, cid, "memory_expand",
                                           {"seq": seq}, meta)
        assert xr["ok"] and xr["data"]["found"] is True
        assert xr["data"]["coverage"] == "exact"
        if not d["has_more"]:
            break
        cur = d["next_cursor"]


async def test_read_memory_legacy_and_synthetic_entries(sandbox):
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "块0"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_legacy_node(0, "旧形态条目"),
                    {"anchor": {"seq": 99999}, "gist": "合成锚点",
                     "level": 1, "role": "gist", "kind": "fact",
                     "upper": [], "lower": []}]})
    meta = _meta(cid)
    r = await card_tools.execute_tool(wid, cid, "read_memory", {}, meta)
    seqs = [e["anchor"]["seq"] for e in r["data"]["entries"]]
    assert seqs == [0, 99999]
    # 旧形态条目照常展开；合成锚点 coverage=unknown
    x0 = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 0}, meta)
    assert x0["ok"] and x0["data"]["coverage"] == "exact"
    xs = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 99999}, meta)
    assert xs["ok"] and xs["data"]["coverage"] == "unknown"


async def test_read_memory_permission_denied_sibling(sandbox):
    wid, cid = await _mk()
    other = await card_store.create_card(wid, "子卡乙", "用户直接创建",
                                         parent=cid)
    cid2 = other["meta"]["card_id"]
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
                                              "entries": [_node(0, "x", [0])]})
    meta = _meta(cid) | {cid2: {"card_id": cid2, "parent": cid,
                                "card_type": "normal", "perms": ["read"]}}
    # 子卡读父卡——父子线是通的（同树），换兄弟线：再造一张兄弟卡
    sib = await card_store.create_card(wid, "子卡丙", "用户直接创建",
                                       parent=cid)
    cid3 = sib["meta"]["card_id"]
    meta[cid3] = {"card_id": cid3, "parent": cid, "card_type": "normal",
                  "perms": ["read"]}
    r = await card_tools.execute_tool(wid, cid2, "read_memory",
                                      {"card_id": cid3}, meta)
    assert r["ok"] is False


# ============================================================
# memory_expand：coveredSeqs 精确选块 ＋ 逐字分页
# ============================================================

async def _verbatim_fixture():
    """coveredSeqs=[0,2]：块0 超长隔离块、块1 唯一范围外标记、块2 中文换行尾白。"""
    wid, cid = await _mk("逐字")
    long_q = "隔离正文" * 2000          # 8000 字超长块
    b0 = await session_facade.append(
        wid, cid, {"role": "assistant", "content": long_q,
                   "quarantine": "pseudo_unparsed",
                   "ctx_content": "〔系统〕该回复含调用格式文本。"})
    await session_facade.append(
        wid, cid, {"role": "assistant",
                   "content": "RANGEMARKER-OUT-OF-COVERAGE 这行绝不出现在回执"})
    b2 = await session_facade.append(
        wid, cid, {"role": "user",
                   "content": "中文逐字\n第二行\n尾白   \n"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "覆盖 0,2 的凝练", [0, 2])]})
    return wid, cid, str(b0["content"]), str(b2["content"])


async def test_expand_exact_covered_seqs_verbatim(sandbox):
    wid, cid, c0, c2 = await _verbatim_fixture()
    meta = _meta(cid)
    expect = c0 + c2            # 逐块纯拼接，零分隔

    pages, cur = [], 0
    rev = None
    while True:
        args = {"seq": 0, "page": cur}
        if rev:
            args["revision"] = rev
        r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                          args, meta)
        assert r["ok"], r
        d = r["data"]
        body = card_tools.render_tool_result(r)
        assert len(body.encode("utf-8")) <= 32 * 1024, "回执 ≤32KiB"
        assert len(d["text"]) <= 6000, "单页 ≤6000 字"
        assert "RANGEMARKER-OUT-OF-COVERAGE" not in d["text"]
        assert d["text"].count(c2) <= 1, "原文字段不重复"
        for f in d["fragments"]:
            s, e = f["src_range"]
            blk = {0: c0, 2: c2}[f["seq"]]
            ts, te = f["text_range"]
            assert d["text"][ts:te] == blk[s:e], "双半开区间对齐逐字"
        rev = d["revision"]
        pages.append(d["text"])
        if not d["has_more"]:
            break
        cur = d["next_page"]
    joined = "".join(pages)
    assert joined == expect, "逐页拼接与覆盖块存档逐字相等"
    assert pages[0] != pages[1], "第0、1页内容不同"
    # 覆盖报告 + 隔离标记进 fragments
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, meta)
    assert r["data"]["coverage"] == "exact"
    assert any(f["quarantine"] for f in r["data"]["fragments"])


async def test_expand_missing_blocks_reported(sandbox):
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "在库的块"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "有缺块", [0, 7, 8])]})
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, _meta(cid))
    d = r["data"]
    assert d["coverage"] == "exact" and d["missing_total"] == 2
    assert d["missing"] == [7, 8]
    assert "在库的块" in d["text"], "找到的照给"


async def test_expand_legacy_range_and_anchor_fallback(sandbox):
    wid, cid = await _mk()
    for i in range(3):
        await session_facade.append(wid, cid,
                                    {"role": "user", "content": f"块{i}"})
    # 旧条目：seq_from/seq_to 闭区间（无 coveredSeqs）
    legacy = {"anchor": {"seq": 0}, "gist": "旧区间条目", "level": 1,
              "role": "gist", "kind": "fact", "upper": [], "lower": [],
              "seq_from": 0, "seq_to": 2}
    # 单锚点兜底（无覆盖无区间）
    anchor_only = {"anchor": {"seq": 2}, "gist": "锚点兜底", "level": 1,
                   "role": "gist", "kind": "fact", "upper": [], "lower": []}
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [legacy, anchor_only]})
    meta = _meta(cid)
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, meta)
    assert r["data"]["coverage"] == "legacy_range"
    assert r["data"]["text"] == "块0块1块2"
    r2 = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 2}, meta)
    assert r2["data"]["coverage"] == "anchor"
    assert r2["data"]["text"] == "块2"


async def test_expand_empty_page_and_negative(sandbox):
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "块0"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "x", [0])]})
    meta = _meta(cid)
    # 页尽 ⇒ 空页（如实空，不静默成功）
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0, "page": 9999}, meta)
    assert r["ok"] and r["data"]["text"] == "" \
        and r["data"]["has_more"] is False
    # 负偏移 ⇒ validation
    r2 = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 0, "page": -1}, meta)
    assert r2["ok"] is False and r2["error"]["code"] == "validation"
    # 非法 section
    r3 = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 0, "section": "bogus"}, meta)
    assert r3["ok"] is False and r3["error"]["code"] == "validation"


async def test_expand_revision_continuation(sandbox):
    wid, cid = await _mk()
    big = "长正文" * 3000      # 9000 字 → 两页
    await session_facade.append(wid, cid, {"role": "user", "content": big})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "x", [0])]})
    meta = _meta(cid)
    r0 = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 0}, meta)
    rev = r0["data"]["revision"]
    # ① 带上页 revision 续读 → 正常给页
    r1 = await card_tools.execute_tool(
        wid, cid, "memory_expand",
        {"seq": 0, "page": r0["data"]["next_page"], "revision": rev}, meta)
    assert r1["ok"] and r1["data"]["text"] and \
        r1["data"]["revision_mismatch"] is False
    # ② 错 revision → 提示回第 0 页，不发当页
    r2 = await card_tools.execute_tool(
        wid, cid, "memory_expand",
        {"seq": 0, "page": 6000, "revision": "deadbeef"}, meta)
    assert r2["ok"] and r2["data"]["revision_mismatch"] is True
    assert r2["data"]["text"] == "" and "page=0" in r2["data"]["note"]
    # ③ 旧调用只传 page（无 revision）→ 当下页照给
    r3 = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 0, "page": 6000}, meta)
    assert r3["ok"] and r3["data"]["text"] == big[6000:]


async def test_expand_revision_changes_with_coverage(sandbox):
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "块0"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "x", [0])]})
    meta = _meta(cid)
    rev1 = (await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0}, meta))["data"]["revision"]
    # 覆盖集变化 ⇒ 版本必变
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "x", [0, 0])]})   # 去重后仍 [0]
    rev2 = (await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0}, meta))["data"]["revision"]
    assert rev1 == rev2, "去重后同覆盖同版本（账目刷新沿用旧页）"


async def test_expand_sunk_revival_via_tool(sandbox):
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "块0"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "沉底", [0], sunk=True)]})
    meta = _meta(cid)
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, meta)
    assert r["ok"] and r["data"]["revived"] is True
    after = md.load_chain(wid, cid)
    assert after[0].get("sunk") is False, "复活以实际写入结果为准"


async def test_expand_details_section(sandbox):
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "块0"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "有描述", [0], process="经过详述" * 3000)]})
    r = await card_tools.execute_tool(
        wid, cid, "memory_expand",
        {"seq": 0, "section": "details"}, _meta(cid))
    d = r["data"]
    assert d["section"] == "details" and "经过详述" in d["text"]
    assert d["has_more"] is True      # 描述区也分页
    r1 = await card_tools.execute_tool(
        wid, cid, "memory_expand",
        {"seq": 0, "section": "details", "page": d["next_page"],
         "revision": d["revision"]}, _meta(cid))
    assert r1["ok"] and r1["data"]["text"] != d["text"]


async def test_expand_not_found_keeps_reason(sandbox):
    wid, cid = await _mk()
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
                                              "entries": [_node(0, "x", [0])]})
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 4242}, _meta(cid))
    assert r["ok"] and r["data"]["found"] is False
    assert "4242" in r["data"]["reason"]


# ============================================================
# 返工项：32KiB 卡死 / 等长改写换版 / 回执边界 / 归拢净化视图
# ============================================================

async def test_receipt_cap_no_hang_oversized_gist(sandbox):
    """40KB gist + 1 字原文：裁剪必须收敛，且先压元数据给原文留空间
    （旧版先裁 text：text="" 但 has_more=true、next_page=0 → 原地打转）。"""
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "短"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "X" * 40_000, [0])]})
    import time
    t0 = time.monotonic()
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, _meta(cid))
    assert time.monotonic() - t0 < 10, "裁剪不得卡死"
    assert r["ok"]
    d = r["data"]
    assert len(card_tools.render_tool_result(r).encode("utf-8")) <= 32 * 1024
    assert d["text"] == "短", "元数据压完后 1 字原文必须给到"
    assert d["has_more"] is False
    assert len(d["gist"]) <= 600, "结论预览被压到预算内"


async def test_expand_pages_always_advance(sandbox):
    """has_more=true ⇒ next_page 必须前进（逐页能读完的不变式）。"""
    wid, cid = await _mk()
    big = "逐字" * 8000          # 16000 字 → 3 页
    await session_facade.append(wid, cid, {"role": "user", "content": big})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "长", [0])]})
    meta = _meta(cid)
    seen_offsets, cur = set(), 0
    for _ in range(10):
        r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                          {"seq": 0, "page": cur}, meta)
        d = r["data"]
        assert cur not in seen_offsets, "同一偏移不得重复给页（原地打转）"
        seen_offsets.add(cur)
        if not d["has_more"]:
            break
        nxt = d["next_page"]
        assert isinstance(nxt, int) and nxt > cur, "has_more ⇒ 偏移必前进"
        cur = nxt
    assert seen_offsets == {0, 6000, 12000}


async def test_receipt_cap_read_memory_and_details(sandbox):
    """read_memory 满页 200 字 gist ×20、details 长描述——回执都 ≤32KiB。"""
    wid, cid = await _mk()
    entries = [_node(i, "概" * 200, [i]) for i in range(25)]
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0,
                                    "entries": entries})
    meta = _meta(cid)
    r = await card_tools.execute_tool(wid, cid, "read_memory", {}, meta)
    assert r["ok"] and len(r["data"]["entries"]) == 20
    assert len(card_tools.render_tool_result(r).encode("utf-8")) <= 32 * 1024
    # details 区大 process + 40KB gist 混合边界
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "G" * 40_000, [0], process="过" * 9_999)]})
    r2 = await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0, "section": "details"}, meta)
    assert r2["ok"]
    assert len(card_tools.render_tool_result(r2).encode("utf-8")) <= 32 * 1024


async def test_revision_same_length_rewrite_rejected(sandbox):
    """等长改写必须换版——旧 revision/旧游标不得串新旧页。"""
    wid, cid = await _mk()
    big = "甲" * 9000
    await session_facade.append(wid, cid, {"role": "user", "content": big})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(i, f"结论{i}", [0]) for i in range(1)]})
    meta = _meta(cid)
    r0 = await card_tools.execute_tool(wid, cid, "memory_expand",
                                       {"seq": 0}, meta)
    rev_old = r0["data"]["revision"]
    # 等长改写原文（同长度换内容——append 只增不改，直接改原文库一行）→ 版本必变
    from src.storage import card_db
    def _rewrite(conn):
        conn.execute("UPDATE session_blocks SET content=? "
                     "WHERE card_id=? AND seq=0", ("乙" * 9000, cid))
    card_db._write_sync(wid, _rewrite)
    r1 = await card_tools.execute_tool(
        wid, cid, "memory_expand",
        {"seq": 0, "page": r0["data"]["next_page"], "revision": rev_old}, meta)
    assert r1["data"]["revision_mismatch"] is True and r1["data"]["text"] == ""
    # 索引侧：等长改写 gist → index_rev 必变，旧游标被拒
    ents = [_node(i, f"结论{i}", [0]) for i in range(25)]
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0, "entries": ents})
    p1 = await card_tools.execute_tool(wid, cid, "read_memory", {}, meta)
    cur = p1["data"]["next_cursor"]
    ents[0]["gist"] = "改" * len(ents[0]["gist"])   # 等长改写
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0, "entries": ents})
    r2 = await card_tools.execute_tool(wid, cid, "read_memory",
                                       {"cursor": cur}, meta)
    assert r2["ok"] is False and r2["error"]["code"] == "validation"


async def test_expand_legacy_toplevel_seq_finds_real_block(sandbox):
    """只有旧顶层 seq 的条目：索引列出，真实原文存在时必须取回。"""
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "旧锚点原文"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [{"seq": 0, "gist": "顶层seq裸条目", "level": 1,
                     "role": "gist", "kind": "fact",
                     "upper": [], "lower": []}]})
    meta = _meta(cid)
    idx = await card_tools.execute_tool(wid, cid, "read_memory", {}, meta)
    assert [e["anchor"]["seq"] for e in idx["data"]["entries"]] == [0]
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, meta)
    assert r["data"]["coverage"] == "anchor"
    assert r["data"]["text"] == "旧锚点原文"


async def test_expand_missing_paginated(sandbox):
    """70 个缺块：missing 分页查全（50/20），不再只看到前 50。"""
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "在库块"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "大量缺块", list(range(71)))]})
    meta = _meta(cid)
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, meta)
    d = r["data"]
    assert d["missing_total"] == 70 and len(d["missing"]) == 50
    assert d["missing_has_more"] is True
    r2 = await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0, "missing_page": 1}, meta)
    d2 = r2["data"]
    assert len(d2["missing"]) == 20 and d2["missing_has_more"] is False
    assert d["missing"] + d2["missing"] == list(range(1, 71))


async def test_expand_sender_card_message(sandbox):
    """卡间消息块的 fragments.sender 要带上发信卡（data.from）。"""
    wid, cid = await _mk()
    b = await session_facade.append(
        wid, cid, {"role": "assistant", "content": "兄弟卡来信",
                   "from": "card_009"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "x", [0])]})
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, _meta(cid))
    senders = [f["sender"] for f in r["data"]["fragments"]]
    assert senders == ["card_009"]


async def test_expand_revive_write_failure_reported(sandbox, monkeypatch):
    """复活落库失败 ⇒ revived=False（回执不得假报复活成功）。"""
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "块0"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "沉底", [0], sunk=True)]})
    # [§三.2] 复活写口已换成事务内局部更新 revive_sunk（不再整链 save_chain）
    monkeypatch.setattr(md, "revive_sunk", lambda *a, **k: False)
    r = await card_tools.execute_tool(wid, cid, "memory_expand",
                                      {"seq": 0}, _meta(cid))
    assert r["ok"] and r["data"]["revived"] is False
    assert "未落库" in r["data"]["note"]


async def test_revision_covers_block_boundaries(sandbox):
    """跨块挪一个字符（拼接全文不变、片段坐标变）⇒ 版本必变。"""
    wid, cid = await _mk()
    await session_facade.append(wid, cid, {"role": "user", "content": "块A尾"})
    await session_facade.append(wid, cid, {"role": "user", "content": "块B头"})
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
        "entries": [_node(0, "x", [0, 1])]})
    meta = _meta(cid)
    rev0 = (await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0}, meta))["data"]["revision"]
    # 把块0 末字挪到块1 开头：拼接全文 "块A尾块B头" 完全不变，块边界变了
    from src.storage import card_db
    def _shift(conn):
        conn.execute("UPDATE session_blocks SET content=? "
                     "WHERE card_id=? AND seq=0", ("块A", cid))
        conn.execute("UPDATE session_blocks SET content=? "
                     "WHERE card_id=? AND seq=1", ("尾块B头", cid))
    card_db._write_sync(wid, _shift)
    r1 = await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0, "revision": rev0}, meta)
    assert r1["data"]["revision_mismatch"] is True, "块边界变化必须换版"
    # 索引侧：covers（覆盖集）变化同样换版
    ents = [_node(i, f"结论{i}", [0]) for i in range(25)]
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0, "entries": ents})
    p1 = await card_tools.execute_tool(wid, cid, "read_memory", {}, meta)
    cur = p1["data"]["next_cursor"]
    ents[0]["coveredSeqs"] = [0, 1]
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0, "entries": ents})
    r2 = await card_tools.execute_tool(wid, cid, "read_memory",
                                       {"cursor": cur}, meta)
    assert r2["ok"] is False and r2["error"]["code"] == "validation"


# ------------------------------------------------ 归拢口（方案 §二.3）

async def _fold_fixture():
    """可归拢场景：2 顶层条目（隔离块＋覆盖外标记块）。"""
    wid, cid = await _mk("归拢")
    # seq10 隔离块（raw vs 净化视图）；seq11 不在任何覆盖集——绝不可被回灌；
    # seq20 普通覆盖块。
    for i in range(10):
        await session_facade.append(wid, cid,
                                    {"role": "user", "content": f"垫{i}"})
    await session_facade.append(
        wid, cid, {"role": "assistant", "content": "RAW-PRE-QUARANTINE 隔离前正文",
                   "quarantine": "pseudo_unparsed",
                   "ctx_content": "SANITIZED-VIEW 净化视图残部"})
    # seq11：覆盖集内的**工具结果**块——凝练口径正文不进归拢模型；
    # status=error 是结构化失败状态，宿主行必须如实带回。
    await session_facade.append(
        wid, cid, {"actor": "tool", "content": "TOOL-RAW-RESULT 工具结果正文",
                   "tool_call_id": "call-1", "status": "error"})
    # seq12：不在任何覆盖集——绝不可被回灌
    await session_facade.append(
        wid, cid, {"role": "assistant",
                   "content": "UNCOVERED-MARKER 覆盖集合之外的块"})
    # seq13：覆盖集内的**评审消息**（actor=card:<评审卡>）——凝练口径
    # drop_stale_review(keep=0) 一条不留，正文也不许进归拢。
    await session_facade.append_card_message(
        wid, cid, from_card="card_rev9", text="REVIEW-MSG-评审派单正文",
        msg_type="msg_directive")
    entries = [
        _node(10, "甲线决策", [10, 11, 13], status="done"),
        _node(20, "乙线决策", [20], status="done"),
        _node(30, "沉底条目", [30], sunk=True),
    ]
    return wid, cid, entries


async def test_fold_expand_uses_sanitized_view_bounded_no_revive(
        sandbox, monkeypatch):
    from src.storage import review_scope
    monkeypatch.setattr(review_scope, "review_card_ids",
                        lambda _w, **_: frozenset({"card_rev9"}))
    wid, cid, entries = await _fold_fixture()
    await memory_store.save_memory(wid, cid,
                                   {"card_id": cid, "mark": 0,
                                    "entries": entries})
    from src.storage import memory_store as ms
    out = ms._expand_for_fold(wid, cid, [10, 30], page_chars=6000)
    assert "SANITIZED-VIEW 净化视图残部" in out, "隔离块投净化视图"
    assert "RAW-PRE-QUARANTINE" not in out, "隔离前正文不得回灌"
    assert "TOOL-RAW-RESULT" not in out, "覆盖集内工具结果正文不回归拢"
    assert "status=error" in out, "失败工具的结构化状态如实带回"
    assert "tool_result" in out, "宿主状态行保留来源（块号/型）"
    assert "块13" in out, "评审块保留块号/型溯源"
    assert "REVIEW-MSG" not in out, "评审消息正文不进归拢（keep=0 同口径）"
    # 沉底条目不复活（revive=False 回灌口）
    after = md.load_chain(wid, cid)
    sunk = [n for n in after if (n.get("anchor") or {}).get("seq") == 30]
    assert sunk and sunk[0].get("sunk") is True, "归拢展开不得触发复活写入"
    # 拼装后有界
    big = await _mk("大")
    await session_facade.append(big[0], big[1],
                                {"role": "user", "content": "长" * 50_000})
    await memory_store.save_memory(big[0], big[1], {"card_id": big[1],
        "mark": 0, "entries": [_node(0, "G" * 30_000, [0],
                                     process="P" * 30_000)]})
    out2 = ms._expand_for_fold(big[0], big[1], [0], page_chars=6000)
    assert len(out2) <= 6000, "拼装产出封顶 page_chars（截断标记计入预算）"


async def test_maybe_fold_expand_request_sanitized(sandbox, monkeypatch):
    """沿 _maybe_fold 截获展开回灌后的下一次模型请求：送净化视图＋有界。"""
    from src.storage import review_scope
    monkeypatch.setattr(review_scope, "review_card_ids",
                        lambda _w, **_: frozenset({"card_rev9"}))
    wid, cid, entries = await _fold_fixture()
    await memory_store.save_memory(
        wid, cid, {"card_id": cid, "mark": 0, "batch_count": 5,
                   "entries": entries})
    from src.storage import memory_store as ms
    calls: list[tuple[str, str]] = []

    async def fake_fold(system, user, tools=None, **kw):
        calls.append((str(kw.get("stage") or ""), str(user)))
        stage = kw.get("stage") or ""
        if stage == "s1":
            return {"content": "结论：有合并\n"
                               '{"groups":[{"label":"甲乙合","members":[10,20]}]}'}
        if stage.endswith("x"):      # 展开回灌后的重写轮
            return {"content": "【成员】10,20\n【标题】合\n【起因】q\n"
                               "【经过】p\n【结果】r\n【教训】l\n【kind】fact"}
        return {"content": "【需展开：seq 10,20】"}

    monkeypatch.setattr(ms, "_fold_call", fake_fold)
    await ms._maybe_fold(wid, cid)
    exp = [u for s, u in calls if "【展开材料】" in u]
    assert exp, "s2' 应走到展开回灌往返"
    body = exp[0]
    assert "SANITIZED-VIEW 净化视图残部" in body
    assert "RAW-PRE-QUARANTINE" not in body, "隔离前正文不进归拢模型请求"
    assert "TOOL-RAW-RESULT" not in body, "覆盖集内工具结果正文不进归拢请求"
    assert "status=error" in body, "失败状态从结构化记录带回"
    assert "REVIEW-MSG" not in body, "评审消息正文不进归拢模型请求"
    assert "UNCOVERED-MARKER" not in body, "覆盖外块不得回灌（精确按块号取数）"
    assert len(body) < 200_000, "回灌有界（step2_in+展开材料）"


@pytest.mark.parametrize("single_char", [True, False])
async def test_expand_300_tiny_blocks_join_and_cap(sandbox, single_char):
    """逐页读回 300 块，每个返回字符都有原块坐标。"""
    wid, cid = await _mk()
    contents = ([chr(0x4e00 + i) for i in range(300)] if single_char
                else [f"B{i:03d}" + "字" * 17 for i in range(300)])
    await session_facade.append_many(
        wid, cid, [{"role": "user", "content": c} for c in contents])
    await memory_store.save_memory(
        wid, cid, {"card_id": cid, "mark": 0,
                   "entries": [_node(0, "长" * 300, list(range(300)))]})
    meta = _meta(cid)
    pages: list[str] = []
    cur, revision = 0, None
    for _ in range(10):
        r = await card_tools.execute_tool(
            wid, cid, "memory_expand",
            {"seq": 0, "page": cur, "revision": revision}, meta, perm_gate=True)
        assert r["ok"], r
        assert len(card_tools.render_tool_result(r).encode("utf-8")) \
            <= 32 * 1024, "每页回执必须在预算内"
        d = r["data"]
        assert d["text"], "有原文的页必须返回非空内容"
        assert not d["revision_mismatch"]
        revision = d["revision"]
        text = d["text"]
        pos = 0
        for f in d["fragments"]:
            sr0, sr1 = f["src_range"]
            tr0, tr1 = f["text_range"]
            assert tr0 == pos < tr1
            assert contents[f["seq"]][sr0:sr1] == text[tr0:tr1]
            pos = tr1
        assert pos == len(text), "溯源片段必须覆盖整页"
        pages.append(text)
        if not d["has_more"]:
            assert d["next_page"] is None
            break
        assert d["next_page"] == cur + len(text)
        cur = d["next_page"]
    assert len(pages) > 1
    assert "".join(pages) == "".join(contents), "逐页拼接与存档逐字相等"


async def test_expand_json_escaping_counts_toward_cap(sandbox):
    wid, cid = await _mk()
    content = chr(1) * 6000
    assert origin_store.write_blocks_sync(
        wid, cid, [{"seq": 0, "actor": "human", "content": content}]) == 1
    await memory_store.save_memory(
        wid, cid, {"card_id": cid, "entries": [_node(0, "摘要", [0])]})
    first = await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0}, _meta(cid), perm_gate=True)
    assert first["ok"] and first["data"]["has_more"]
    first_data = first["data"]
    end = first_data["next_page"]
    assert 0 < end == len(first_data["text"]) < len(content)
    second = await card_tools.execute_tool(
        wid, cid, "memory_expand",
        {"seq": 0, "page": end, "revision": first_data["revision"]},
        _meta(cid), perm_gate=True)
    assert second["ok"] and not second["data"]["has_more"]
    assert first_data["text"] + second["data"]["text"] == content
    for offset, result in [(0, first), (end, second)]:
        assert len(card_tools.render_tool_result(result).encode("utf-8")) <= 32 * 1024
        data = result["data"]
        assert len(data["fragments"]) == 1
        fragment = data["fragments"][0]
        assert fragment["text_range"] == [0, len(data["text"])]
        assert fragment["src_range"] == [offset, offset + len(data["text"])]


async def test_expand_reports_when_one_verbatim_fragment_cannot_fit(sandbox):
    wid, cid = await _mk()
    assert origin_store.write_blocks_sync(
        wid, cid, [{"seq": 0, "actor": "human", "content": "短",
                    "sender": "S" * 40_000}]) == 1
    await memory_store.save_memory(
        wid, cid, {"card_id": cid, "entries": [_node(0, "摘要", [0])]})
    result = await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0}, _meta(cid), perm_gate=True)
    assert not result["ok"] and result["error"]["code"] == "result_too_large"


async def test_revision_covers_sender_rewrite(sandbox):
    """发信卡改写（正文/块号/覆盖集全不变）⇒ revision 必变。"""
    wid, cid = await _mk()
    await session_facade.append(
        wid, cid, {"role": "assistant", "content": "同文块A",
                   "from": "card_017"})
    await session_facade.append(
        wid, cid, {"role": "assistant", "content": "同文块B",
                   "from": "card_017"})
    await memory_store.save_memory(
        wid, cid, {"card_id": cid, "mark": 0,
                   "entries": [_node(0, "x", [0, 1])]})
    meta = _meta(cid)
    rev0 = (await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0}, meta))["data"]["revision"]
    # 只改 data JSON 里的发信卡：card_017 → card_049（正文/块号不动）
    from src.storage import card_db
    def _rewrite(conn):
        conn.execute(
            "UPDATE session_blocks SET data=REPLACE(data,'card_017','card_049') "
            "WHERE card_id=? AND seq=0", (cid,))
    card_db._write_sync(wid, _rewrite)
    r1 = await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0, "revision": rev0}, meta)
    assert r1["data"]["revision_mismatch"] is True, \
        "来源字段（发信卡）改写必须换版"
    # 新 revision 可正常续读，且发信者已是新卡
    r2 = await card_tools.execute_tool(
        wid, cid, "memory_expand", {"seq": 0}, meta)
    assert r2["data"]["revision"] != rev0
    senders = {f["sender"] for f in r2["data"]["fragments"]}
    assert "card_049" in senders
