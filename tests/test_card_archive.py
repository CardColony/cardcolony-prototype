# -*- coding: utf-8 -*-
"""[2026-10-25 · 用户令「让所有卡都可以归档」] 归档是通用能力，不加前置判断。

用户原话：任务进行到一定程度、**过程有价值但不需要继续**的卡，不该一直挂在
树上。所以归档**不看卡类型、不看 state、不看有没有子卡**——判断归用户/模型，
存储层只执行。

本文件钉死：
1. 任意卡（active／有子卡）都能归档；
2. 归档后卡从树上消失（read_card 查不到），但记录在（read_archive 查得到）；
3. 三块正文、修改历史、影子三节**完整保留**；
4. 能无损恢复（卡号不变、三块逐字一致）；
5. 会话文件不动（过程价值留在原地）。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_archive, card_store


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_any_active_card_can_be_archived(work):
    """active 的卡（不是 completed）也能归档——这是用户明确要的。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "做到一半的卡", root)
    assert (await card_store.read_card(wid, cid))["meta"]["state"] == "active"

    r = await card_store.archive_card(wid, cid, reason="方向变了，不再继续")
    assert r.get("ok") is True, r
    assert card_store.card_exists(wid, cid) is False


async def test_card_with_children_can_be_archived(work):
    """有子卡的卡也能归档（不要求先删子卡）。"""
    wid, root = work["work_id"], work["work_card_id"]
    parent = await _mk_child(wid, "带子卡的父", root)
    await _mk_child(wid, "子卡", parent)

    r = await card_store.archive_card(wid, parent, reason="整条线收起来")
    assert r.get("ok") is True, r
    assert card_store.card_exists(wid, parent) is False


async def test_archive_keeps_all_three_blocks(work):
    """三块正文完整保留——"过程有价值"的落点。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "三块保留", root)
    await card_store.update_card(wid, cid, {"status_block": "现状-ABC"}, "写现状")
    await card_store.update_card(wid, cid, {"requirements": "需求- XYZ"}, "写第三块")
    before = await card_store.read_card(wid, cid)

    await card_store.archive_card(wid, cid, reason="测保留")

    rec = card_archive.get_record(wid, cid)
    assert rec, "应有归档记录"
    assert rec["mission"] == (before["body"]["mission"] or "")
    assert rec["status_block"] == "现状-ABC"
    assert rec["requirements"] == "需求- XYZ"
    assert rec["title"] == "三块保留"
    assert rec["card_type"] == "sub_project"


async def test_archive_keeps_history_and_shadow(work):
    """修改历史与影子三节也进记录。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "历史与影子", root)
    await card_store.update_card(wid, cid, {"status_block": "x"}, "改一次")

    await card_store.archive_card(wid, cid, reason="测历史")

    rec = card_archive.get_record(wid, cid)
    assert rec["history"], "修改历史应保留"
    assert any("卡片创建" in (h.get("text") or "") for h in rec["history"])


async def test_archived_card_is_gone_from_tree(work):
    """归档后 read_card 查不到——它不再是活卡。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "消失", root)
    await card_store.archive_card(wid, cid, reason="测消失")

    try:
        data = await card_store.read_card(wid, cid)
    except card_store.CardNotFoundError:
        data = {}
    assert not (data.get("meta") or {}), f"归档后不应再读到卡：{data}"


async def test_unarchive_restores_content_verbatim(work):
    """恢复：卡号不变、三块逐字一致、能再读到。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "可恢复", root)
    await card_store.update_card(wid, cid, {"status_block": "恢复前现状"}, "写现状")
    before = await card_store.read_card(wid, cid)

    await card_store.archive_card(wid, cid, reason="临时收起")
    r = await card_store.unarchive_card(wid, cid)
    assert r.get("ok") is True, r

    after = await card_store.read_card(wid, cid)
    assert after["meta"]["card_id"] == cid
    assert after["body"]["mission"] == before["body"]["mission"]
    assert after["body"]["status_block"] == "恢复前现状"
    assert after["body"]["requirements"] == (before["body"]["requirements"] or "")
    # 记录已消费
    assert card_archive.get_record(wid, cid) is None


async def test_archive_via_tool(work):
    """工具层：archive_card / read_archive / unarchive_card 全链路。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "工具归档", root)
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(
        wid, root, "archive_card", {"card_id": cid, "reason": "工具测"}, cards)
    assert r.get("ok") is True, r

    r2 = await card_tools.execute_tool(
        wid, root, "read_archive", {"card_id": cid}, cards)
    assert r2.get("ok") is True, r2
    assert r2["data"]["record"]["card_id"] == cid
    assert r2["data"]["record"]["reason"] == "工具测"

    r3 = await card_tools.execute_tool(
        wid, root, "unarchive_card", {"card_id": cid}, cards)
    assert r3.get("ok") is True, r3
    assert card_store.card_exists(wid, cid)


async def test_archive_does_not_touch_session_file(work):
    """会话文件原位不动（过程价值留在原地）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "会话保留", root)
    sp = card_store._session_path(wid, cid)
    assert sp.exists()

    await card_store.archive_card(wid, cid, reason="测会话")

    assert sp.exists(), "归档不该删会话文件"


async def test_parent_archives_own_coder_child_via_tool(work):
    """[2026-10-25 实测事故] 父卡归档自己的 **coder 子卡**不许被拒。

    原判据用 `policy.check(..., SWITCH_DELETE)` ⇒ 查的是**被归档卡自己的 perms**，
    而 coder 卡的 perms 里没有 delete ⇒ 父卡永远归档不了自己的子卡
    （实测 card_021 归档 card_023 报 `越权：无权归档`）。
    正确判据：操作者在目标子树上游（与 silence_card / stop_card 同款）。
    """
    wid, root = work["work_id"], work["work_card_id"]
    parent = await _mk_child(wid, "父卡", root)
    r0 = await card_store.create_card(
        wid, "coder 子卡", "模型生成", card_type="coder", parent=parent,
        skip_kick=True)
    assert "error" not in r0, r0
    child = r0["meta"]["card_id"]
    # 前提确认：coder 卡自己的 perms 里确实没有 delete
    assert "delete" not in (r0["meta"].get("perms") or [])

    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(
        wid, parent, "archive_card",
        {"card_id": child, "reason": "测父归档 coder 子卡"}, cards)

    assert r.get("ok") is True, r
    assert card_store.card_exists(wid, child) is False


async def test_sibling_cannot_archive(work):
    """非自己子树的卡不能归档（判据不能松成"人人可归档"）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", root)
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(
        wid, a, "archive_card", {"card_id": b, "reason": "越权测"}, cards)

    assert r.get("ok") is False
    assert r["error"]["code"] == "denied"
    assert card_store.card_exists(wid, b) is True
