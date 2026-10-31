# -*- coding: utf-8 -*-
"""伴生评审卡：派生与批次行为（[伴生评审卡 2026-10-27]）。

钉的是需求里明示的几条：
- 子项目卡／课题卡建卡即派生**一张**评审卡（一对一、幂等、离树）
- 其它类型不派生
- `approve` 绑定 (草稿版本 ＋ 判据版本)，版本一变即失效（P3）
- 全员 approve 才 `all_passed`；没调过的不算通过（默认未通过）
"""
from __future__ import annotations

import pytest

from src.storage import card_db, card_store, review_store, work_store


async def _make_work(title: str = "伴生评审卡测试") -> str:
    wdata = await work_store.create_work(title)
    return str(wdata["work_id"])


async def test_sub_project_derives_one_review_card(sandbox):
    wid = await _make_work()
    r = await card_store.create_card(wid, "子项目甲", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    rid = await review_store.review_card_of(wid, cid)
    assert rid, "建 sub_project 必须自动派生伴生评审卡"
    meta = (await card_store.read_card(wid, rid))["meta"]
    child_meta = (await card_store.read_card(wid, cid))["meta"]
    assert meta["card_type"] == "review"
    assert not (meta.get("parent") or ""), "评审卡不设 parent（离树）"
    assert not (meta.get("relations") or []), "评审配对不应写关系线"
    assert not (child_meta.get("relations") or []), "评审卡与被评卡不应互相建关系线"


async def test_derivation_is_idempotent(sandbox):
    wid = await _make_work()
    r = await card_store.create_card(wid, "子项目乙", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    first = await review_store.review_card_of(wid, cid)
    again = await review_store.ensure_review_card(wid, cid, "子项目乙")
    assert first and again == first, "重复派生不得建出第二张"


async def test_coder_card_has_no_review_card(sandbox):
    wid = await _make_work()
    r = await card_store.create_card(wid, "编程卡甲", "用户直接创建",
                                     card_type="coder")
    cid = str(r["meta"]["card_id"])
    assert await review_store.review_card_of(wid, cid) == "", "只有 sub_project/topic 有伴生卡"


async def test_review_invites_are_batch_scoped_not_relation_lines(sandbox):
    """职能参与评审是本批次临时邀请，不是评审卡与职能卡之间的关系线。"""
    wid = await _make_work()
    r = await card_store.create_card(wid, "子项目邀请", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    rid = await review_store.review_card_of(wid, cid)

    child_before = (await card_store.read_card(wid, cid))["meta"].get("relations") or []
    review_before = (await card_store.read_card(wid, rid))["meta"].get("relations") or []
    invited = ["fn_a", "fn_b"]
    await review_store.open_batch(wid, rid, "R1", members=invited)

    state = await review_store.state_of(wid, rid)
    assert state["members"] == invited
    assert await review_store.members_of(wid, rid) == invited
    assert ((await card_store.read_card(wid, cid))["meta"].get("relations") or []) == child_before
    assert ((await card_store.read_card(wid, rid))["meta"].get("relations") or []) == review_before

    await review_store.close_batch(wid, rid, "R1 verdict")
    assert await review_store.members_of(wid, rid) == []
    closed = await review_store.state_of(wid, rid)
    assert closed["members"] == []


async def test_batch_approve_and_revoke_invalidation(sandbox):
    """批次通过与否：票只在**未被撤销**时有效（判据机制已删）。"""
    wid = await _make_work()
    r = await card_store.create_card(wid, "子项目丙", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    rid = await review_store.review_card_of(wid, cid)
    assert rid

    st = await review_store.open_batch(wid, rid, "R1 收尾", members=["fn_a", "fn_b"])
    assert st["batch"].startswith("rv_")
    assert "criteria" not in st, "评审不再有判据概念"

    await review_store.approve(wid, rid, "fn_a")
    s1 = await review_store.status(wid, rid)
    assert "fn_a" in s1["passed"] and "fn_b" in s1["pending"]
    assert s1["all_passed"] is False, "差一人不得判全员通过"

    await review_store.approve(wid, rid, "fn_b")
    s2 = await review_store.status(wid, rid)
    assert s2["all_passed"] is True

    # 撤销 fn_a 的票 ⇒ 不再全员通过
    await review_store.revoke(wid, rid, "fn_a")
    s3 = await review_store.status(wid, rid)
    assert s3["all_passed"] is False, "撤销一票后不得仍判全员通过"
    assert "fn_a" in s3["pending"]


async def test_revoke_clears_approval(sandbox):
    wid = await _make_work()
    r = await card_store.create_card(wid, "子项目丁", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    rid = await review_store.review_card_of(wid, cid)
    await review_store.open_batch(wid, rid, "R1", members=["fn_x"])
    await review_store.approve(wid, rid, "fn_x")
    assert (await review_store.status(wid, rid))["all_passed"] is True
    await review_store.revoke(wid, rid, "fn_x")
    s = await review_store.status(wid, rid)
    assert s["all_passed"] is False and s["pending"] == ["fn_x"]
