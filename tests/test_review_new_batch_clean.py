# -*- coding: utf-8 -*-
"""开新批次 ⇒ 旧批次的所有内容清干净（2026-10-31 用户裁定）。

用户原话：「每一次调新批次，旧批次的所有内容，都应该清干净，
每一次评审都是全新的开始」

钉三件事：
1. 旧会话清空（发言、工具结果全走，新批次从零开始）
2. 旧审批票删除（review_approvals 里本评审卡旧批次的行清掉）
3. prev_verdict 是**唯一**带过来的东西（换上下文，不是带过程）
"""
from __future__ import annotations

import sqlite3

from src.storage import review_store, session_facade


async def _make(sandbox, n: int = 2):
    from tests.test_review_user_intervention import _setup as _base
    return await _base(n)


async def test_new_batch_clears_old_session(sandbox):
    """开新批次 ⇒ 旧会话文件清空（发言/工具结果全走，只留文件头）。"""
    wid, cid, fids, rid = await _make(sandbox)

    # 造旧批次的会话内容
    await session_facade.append(wid, rid, {
        "actor": "assistant",
        "content": f"〔总裁 · rv_OLD〕旧批次的发言，不应该出现在新批次里",
    })
    await session_facade.append(wid, rid, {
        "actor": "tool",
        "content": "〔read_card · rv_OLD〕 旧批次的工具结果",
    })
    blocks = await session_facade.read(wid, rid) or []
    assert len(blocks) >= 2, "前置：旧批次有会话内容"

    # 开新批次
    st = await review_store.open_batch(wid, rid, "新焦点",
                                        members=fids, target=cid, brief="新背景")

    # 会话应该被清空
    blocks2 = await session_facade.read(wid, rid) or []
    assert blocks2 == [], f"新批次开会后旧会话应清空，实际还有 {len(blocks2)} 块"


async def test_new_batch_deletes_old_approvals(sandbox):
    """开新批次 ⇒ 本评审卡旧批次的审批票全删（其他评审卡不受影响）。"""
    wid, cid, fids, rid = await _make(sandbox)
    old_st = await review_store.state_of(wid, rid)
    old_batch = old_st.get("batch")

    # 造旧审批
    await review_store.approve(wid, rid, fids[0])
    await review_store.approve(wid, rid, fids[1])

    # 再造另一张评审卡的审批（不该被误删）
    from src.storage import card_store
    r2 = await card_store.create_card(wid, "另一子卡", "测试", card_type="sub_project")
    cid2 = str((r2.get("meta") or {}).get("card_id") or "")
    # 手动开一个批次（不走 _h_open_review，避免自动起轮）
    st2 = await review_store.open_batch(wid, "card_TEMP", "另评",
                                         members=["x"], target=cid2, brief="b")

    # 开新批次
    st = await review_store.open_batch(wid, rid, "新焦点",
                                        members=fids, target=cid, brief="新背景")

    # 本评审卡的旧票应该没了
    def _count(conn):
        return conn.execute(
            "SELECT COUNT(*) FROM review_approvals WHERE batch_id=?",
            (old_batch,)).fetchone()[0]
    n = review_store.card_db.read_sync(wid, _count, 0)
    assert n == 0, f"旧批次 {old_batch} 的审批应该全删，实际还有 {n} 条"


async def test_prev_verdict_is_the_only_thing_carried(sandbox):
    """prev_verdict 是唯一带过来的；spoken/approvals/brief 全是新批次的。"""
    wid, cid, fids, rid = await _make(sandbox)
    old_st = await review_store.state_of(wid, rid)
    old_batch = old_st.get("batch")

    # 造一些旧数据
    await review_store.approve(wid, rid, fids[0])
    await review_store.mark_spoken(wid, rid, fids[0])
    await session_facade.append(wid, rid, {
        "actor": "assistant", "content": "旧发言",
    })

    # 收口旧批次（写入正稿）
    await review_store.close_batch(wid, rid, "旧批次的正稿内容")

    # 开新批次
    st = await review_store.open_batch(wid, rid, "新焦点",
                                        members=fids, target=cid, brief="新brief")

    # prev_verdict 带过来
    assert st.get("prev_verdict") == "旧批次的正稿内容"
    # 其他全是新的
    assert st.get("batch") != old_batch
    assert st.get("draft") == 1
    assert "criteria" not in st, "评审不再有判据概念"
    assert st.get("brief") == "新brief"
    assert st.get("spoken", {}).get("roles") in ([], None)  # 新批次没人说过话
    # 会话为空
    blocks = await session_facade.read(wid, rid) or []
    assert blocks == [], "新批次应该没有旧发言"


async def test_concurrent_review_cards_not_affected(sandbox):
    """同一 work 里另一张评审卡的审批不该被误删。"""
    wid, cid, fids, rid = await _make(sandbox)
    from src.storage import card_store

    # 建第二张子卡 + 评审卡，开一个批次
    r2 = await card_store.create_card(wid, "另一子卡", "测试", card_type="sub_project")
    cid2 = str((r2.get("meta") or {}).get("card_id") or "")
    rid2 = await review_store.ensure_review_card(wid, cid2, "另一子卡")
    st2 = await review_store.open_batch(wid, rid2, "另一评审",
                                         members=["fn_001"], target=cid2, brief="b2")
    await review_store.approve(wid, rid2, "fn_001")

    # 给第一张也开新批次
    st1 = await review_store.open_batch(wid, rid, "新焦点",
                                         members=fids, target=cid, brief="新brief")

    # 第二张的票应该还在
    st2_now = await review_store.status(wid, rid2)
    assert "fn_001" in st2_now.get("passed"), \
        f"另一张评审卡的审批不该被误删（passed={st2_now.get('passed')}）"
