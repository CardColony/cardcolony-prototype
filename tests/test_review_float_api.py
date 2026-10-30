# -*- coding: utf-8 -*-
"""评审浮窗的数据源：`GET /cards/{cid}/review-status`（[评审浮窗 2026-10-29]）。

用户口径（第一手）：
> 「有进行中的批次才出现，收口就消失。没有评审时界面上什么都不多。」

所以这个端点的契约只有三条，全部在这里钉死：
1. 没有伴生评审卡 ⇒ `has_review=False`（非子项目/课题卡，浮窗不出现）；
2. 有评审卡但批次已收口 ⇒ `active=False`（**收口就消失**）；
3. 有进行中批次 ⇒ `active=True`，带齐进度（members/passed/pending）与中文名。
"""
from __future__ import annotations

import httpx
import pytest

from src.api import card_tools
from src.storage import card_store, function_store, review_store, work_store


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def _work_with_review(n: int = 2):
    wdata = await work_store.create_work("评审浮窗测试")
    wid = str(wdata["work_id"])
    fids = []
    for i in range(n):
        prof = await function_store.create_function(
            wid, f"职能{i}", {"jurisdiction": {"catalog_code": f"f{i}"}})
        fids.append(str(prof.get("function_id") or ""))
    r = await card_store.create_card(wid, "子项目甲", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid, {"requirements_draft": "R1 草稿", "requirements_state": "draft"},
        "测试前置：写草稿")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "背景/现状/评审"}, cards)
    return wid, cid, fids, str(out["data"]["review_card_id"])


async def test_active_batch_shows_float_with_progress(sandbox, app):
    wid, cid, fids, rid = await _work_with_review(2)
    async with _client(app) as client:
        r = await client.get(f"/api/works/{wid}/cards/{cid}/review-status")
    assert r.status_code == 200
    d = r.json()
    assert d["has_review"] is True and d["active"] is True
    assert d["review_card_id"] == rid
    assert set(d["members"]) == set(fids)
    assert d["passed"] == [] and set(d["pending"]) == set(fids)
    # 中文名要带上——浮窗写「还差：技术总监」，不能写 fn_005
    assert all(d["member_names"].get(f) for f in fids)


async def test_closed_batch_means_float_disappears(sandbox, app):
    """收口 ⇒ active=False（前端据此把浮窗收掉）。"""
    wid, cid, fids, rid = await _work_with_review(2)
    cards = card_store.iter_card_metas(wid)
    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)
    # [2026-10-29] 收口＝正稿写完，入口才消失
    await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)
    # [2026-10-29] 全员通过只是「可以定稿」；写完正稿才收口、浮窗才消失
    await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)

    async with _client(app) as client:
        r = await client.get(f"/api/works/{wid}/cards/{cid}/review-status")
    d = r.json()
    assert d["has_review"] is True
    assert d["active"] is False, "收口后不该再出现浮窗"
    assert d["batch"] == ""


async def test_card_without_review_card_reports_no_review(sandbox, app):
    """coder 卡没有伴生评审卡 ⇒ has_review=False（界面上什么都不多）。"""
    wdata = await work_store.create_work("无评审卡")
    wid = str(wdata["work_id"])
    r0 = await card_store.create_card(wid, "编程甲", "用户直接创建",
                                      card_type="coder")
    cid = str(r0["meta"]["card_id"])
    async with _client(app) as client:
        r = await client.get(f"/api/works/{wid}/cards/{cid}/review-status")
    d = r.json()
    assert d["has_review"] is False and d["active"] is False


async def test_nav_lists_active_reviews_and_drops_them_after_close(sandbox, app):
    """左栏临时出入口：有批次才在 nav.reviews 里，收口即消失。"""
    wid, cid, fids, rid = await _work_with_review(2)
    async with _client(app) as client:
        r = await client.get("/api/nav")
    proj = [p for p in r.json()["projects"] if p["work_id"] == wid][0]
    assert [x["review_card_id"] for x in proj["reviews"]] == [rid]
    assert proj["reviews"][0]["target"] == cid
    assert proj["reviews"][0]["total"] == 2

    cards = card_store.iter_card_metas(wid)
    for f in fids:
        await card_tools._h_approve(wid, f, {"review_card_id": rid}, cards)
    # [2026-10-29] 全员通过只是「可以定稿」；写完正稿才收口，左栏入口才消失
    await card_tools._h_promote_draft(wid, cid, {"card_id": cid}, cards)

    async with _client(app) as client:
        r2 = await client.get("/api/nav")
    proj2 = [p for p in r2.json()["projects"] if p["work_id"] == wid][0]
    assert proj2["reviews"] == [], "收口后左栏入口必须消失"


async def test_status_carries_each_members_last_opinion(sandbox, app, monkeypatch):
    """窗口里显示的是「谁要求改什么」⇒ 状态里必须带**每人的原话**。

    [2026-10-29 用户令「你的信息显示根本没有优化」] 改前窗口只有
    「某某发言 2269 字」这种流水账，看完不知道这场评审卡在谁身上。
    """
    from src.api import model_service, review_runner

    wid, cid, fids, rid = await _work_with_review(2)

    async def say(sys_p, user_p, convo, tools, model):
        return ({"content": "第三条不满足：没有证据",
                 "tool_calls": [{"name": "reject", "arguments": {"reason": "测试：判据没落点"}}]}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", say)
    await review_runner.speak(wid, rid, fids[0])

    async with _client(app) as client:
        r = await client.get(f"/api/works/{wid}/cards/{cid}/review-status")
    d = r.json()
    assert "第三条不满足" in str((d["opinions"].get(fids[0]) or {}).get("text") or ""), \
        "每人最近一条发言原话必须带出来"
    assert d["target_title"], "窗口标题要知道在评什么"
