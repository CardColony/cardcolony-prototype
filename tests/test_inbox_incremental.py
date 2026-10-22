# -*- coding: utf-8 -*-
"""read_inbox 增量语义（2026-10-22 用户裁定：读过的内容，下次不应再读到）。

判据：
I1 第一次读 incoming ⇒ 拿到全部未读；返回后水位推进
I2 再读一次 incoming（无新信）⇒ 空，且**不会**把同样的信再发一遍
I3 期间来了新信 ⇒ 只回新那一封，旧的不再出现
I4 scope=all ⇒ 回看全部往来（deliberate 翻历史的逃生门）
I5 direction=outgoing 不推进水位（出向不是"读"）
I6 pending_only 参数已移除 ⇒ 传了要拒
I7 schema：有 scope 参数、无 pending_only
"""
from __future__ import annotations

import pytest

from src.api import card_tools, card_msg, model_service
from src.storage import card_db

async def _cards(wid: str) -> dict:
    return card_tools._collect_card_meta(wid)


async def _mk_pair(work) -> tuple[str, str, str]:
    """返回 (work_id, A卡, B卡)——B 是 A 的子卡。"""
    from src.storage import card_store
    wid = work["work_id"]
    root = work["work_card_id"]
    a = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    b = await card_store.create_card(wid, "乙", "模型生成", parent=root)
    aid, bid = a["meta"]["card_id"], b["meta"]["card_id"]
    # 卡间通信必须先有通信线
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(
        wid, aid, "link_cards",
        {"target_id": bid, "type": "关联"}, cards)
    assert r.get("ok") is True, r
    return wid, aid, bid


async def _send(wid: str, frm: str, to: str, body: str) -> str:
    res = await card_tools.execute_tool(
        wid, frm, "send_card_message",
        {"target_card_id": to, "msg_type": "msg_notify", "content": body},
        await _cards(wid))
    assert res.get("ok") is True, res
    # 从库里取回 msg_id
    rows = await card_db.list_inbox(wid, to)
    return str(rows[-1]["msg_id"])


@pytest.mark.asyncio
async def test_first_read_gets_all_unseen_and_advances(work):
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "第一封")
    await _send(wid, a, b, "第二封")
    res = await card_tools.execute_tool(wid, b, "read_inbox", {}, await _cards(wid))
    assert res.get("ok") is True, res
    bodies = [m["content"] for m in res["data"]["messages"]]
    assert "第一封" in bodies and "第二封" in bodies, bodies
    assert res["data"]["scope"] == "unread"
    assert res["data"]["seen_seq"] is not None


@pytest.mark.asyncio
async def test_second_read_returns_nothing_new(work):
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "只有一封")
    first = await card_tools.execute_tool(wid, b, "read_inbox", {}, await _cards(wid))
    assert len(first["data"]["messages"]) == 1
    second = await card_tools.execute_tool(wid, b, "read_inbox", {}, await _cards(wid))
    assert second["data"]["messages"] == [], "读过的不能再发一遍"
    assert second["data"]["count"] == 0


@pytest.mark.asyncio
async def test_new_mail_after_read_comes_alone(work):
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "旧的")
    await card_tools.execute_tool(wid, b, "read_inbox", {}, await _cards(wid))
    await _send(wid, a, b, "新的")
    res = await card_tools.execute_tool(wid, b, "read_inbox", {}, await _cards(wid))
    bodies = [m["content"] for m in res["data"]["messages"]]
    assert bodies == ["新的"], bodies


@pytest.mark.asyncio
async def test_scope_all_is_the_escape_hatch(work):
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "旧信")
    await card_tools.execute_tool(wid, b, "read_inbox", {}, await _cards(wid))
    res = await card_tools.execute_tool(
        wid, b, "read_inbox", {"scope": "all"}, await _cards(wid))
    assert [m["content"] for m in res["data"]["messages"]] == ["旧信"]
    assert res["data"]["scope"] == "all"


@pytest.mark.asyncio
async def test_outgoing_view_does_not_advance_watermark(work):
    wid, a, b = await _mk_pair(work)
    await _send(wid, a, b, "给 B 的信")
    await card_tools.execute_tool(
        wid, a, "read_inbox", {"direction": "outgoing"}, await _cards(wid))
    # A 的入向水位没被出向视图推进：B 若给 A 发信，A 仍能收到
    await _send(wid, b, a, "回给 A")
    res = await card_tools.execute_tool(wid, a, "read_inbox", {}, await _cards(wid))
    assert [m["content"] for m in res["data"]["messages"]] == ["回给 A"]


@pytest.mark.asyncio
async def test_removed_pending_only_is_rejected(work):
    wid, _, b = await _mk_pair(work)
    res = await card_tools.execute_tool(
        wid, b, "read_inbox", {"pending_only": True}, await _cards(wid))
    assert res.get("ok") is False
    assert "pending_only" in res["error"]["message"]


def test_schema_has_scope_and_no_pending_only():
    fn = card_tools.TOOL_REGISTRY["read_inbox"].schema
    props = fn["parameters"]["properties"]
    assert "scope" in props and "pending_only" not in props, props.keys()
