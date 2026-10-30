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
    assert meta["card_type"] == "review"
    assert not (meta.get("parent") or ""), "评审卡不设 parent（离树）"


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


async def test_batch_approve_and_version_invalidation(sandbox):
    wid = await _make_work()
    r = await card_store.create_card(wid, "子项目丙", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    rid = await review_store.review_card_of(wid, cid)
    assert rid

    st = await review_store.open_batch(wid, rid, "R1 收尾", members=["fn_a", "fn_b"])
    assert st["batch"].startswith("rv_")

    await review_store.approve(wid, rid, "fn_a")
    s1 = await review_store.status(wid, rid)
    assert "fn_a" in s1["passed"] and "fn_b" in s1["pending"]
    assert s1["all_passed"] is False, "差一人不得判全员通过"

    await review_store.approve(wid, rid, "fn_b")
    s2 = await review_store.status(wid, rid)
    assert s2["all_passed"] is True

    # 判据版本 +1 ⇒ 旧通过全部失效（P3：approve 绑 (draft, criteria)）
    def _bump(conn):
        import json
        row = conn.execute("SELECT review_state FROM cards WHERE card_id=?", (rid,)).fetchone()
        d = json.loads(row["review_state"])
        d["criteria"] = int(d.get("criteria") or 1) + 1
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (json.dumps(d, ensure_ascii=False), rid))

    await card_db.run_write(wid, _bump)
    s3 = await review_store.status(wid, rid)
    assert s3["all_passed"] is False, "判据变了，旧通过必须作废"
    assert set(s3["pending"]) == {"fn_a", "fn_b"}


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
