# -*- coding: utf-8 -*-
"""[report_up 2026-10-26] 用户三条硬要求：不走 inbox / 不落用户块 / 唤醒父卡。

P1 父卡收件箱**零新增**（不走 inbox）
P2 父卡会话**没有 user 块**（不冒充用户说的话）
P3 父卡**被唤醒**（_run_message 被调用，且 content 是汇报正文）
"""
from __future__ import annotations

import pytest

from src.api import card_tools, message_runner, routes
from src.storage import card_db, card_store, session_facade, work_store


@pytest.fixture(autouse=True)
def _clean():
    yield


async def _seed():
    w = await work_store.create_work("report 三条")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(wid, "子", "模型生成",
                                        card_type="sub_project",
                                        parent=root))["meta"]["card_id"]
    return wid, root, sub


async def test_report_up_three_requirements(sandbox, monkeypatch):
    """[通信收拢 2026-10-28 改写] 汇报三条硬约定：

    P1 不进账本（收件箱已取消，消息只落会话块）
    P2 **落会话块**（正文进上下文、可被凝练）——10-27 起就是这个口径，
       改前这条断言写的是"不落 user 块"，与实现相反，一直是假绿。
    P3 只发**直接父卡**（不发总裁、不召集职能），并唤醒它
    """
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    wid, root, sub = await _seed()
    started: dict = {}

    async def _fake_run(work_id, card_id, content, *a, **kw):
        started.update({"card": card_id, "content": content,
                        "source": kw.get("source")})

    monkeypatch.setattr(routes, "_run_message", _fake_run)
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_report_up(
        wid, sub, {"content": "阶段完成，证据在影子卡"}, cards)
    assert r.get("delivered"), r

    # P1 不进账本
    assert await card_db.list_pending(wid, root) == [], "汇报不该进账本"
    # P2 落会话块：actor=card:<发信卡>，正文带汇报内容
    blocks = await session_facade.read(wid, root) or []
    hit = [b for b in blocks if str(b.get("actor") or "") == f"card:{sub}"]
    assert hit, f"汇报必须落成收件方会话块: {blocks}"
    assert any("阶段完成" in str(b.get("content") or "") for b in hit), hit
    # P3 只到直接父卡，并唤醒它
    assert r.get("recipients") == [root], r
    import asyncio
    await asyncio.sleep(0.1)
    assert started.get("card") == root, started
    assert (started.get("source") or {}).get("from") == sub, started
    assert hit, f"汇报必须落成收件方会话块: {blocks}"
    assert any("阶段完成" in str(b.get("content") or "") for b in hit), hit
    # P3 只到直接父卡，并唤醒它
    assert r.get("recipients") == [root], r
    import asyncio
    await asyncio.sleep(0.1)
    assert started.get("card") == root, started
    assert (started.get("source") or {}).get("from") == sub, started
