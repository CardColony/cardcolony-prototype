# -*- coding: utf-8 -*-
"""需求文档生命周期：草稿 → 评审 → 正稿 → 才能派工。

[2026-10-28 用户令 · 第一手]
- 「coder 卡，不需要走评审；只有子卡的需求文档需要评审」
- 「子卡的需求文档不是正稿的情况下，取消它的派工权限」
- 「需求文档可以分成草稿和正稿两版，草稿随便改，不对外暴露，正稿必须经过评审」
- 流程：使命 → 写草稿 → 评审 → 正稿 → 按正稿执行 → 冲突改草稿 → 再评审

钉住：
T1 草稿只有本卡看得到（不进 read_card / 不进别的卡上下文），正稿才是公共读
T2 没有正稿 ⇒ 派工三件（create_card / create_coder_card / direct）不在工具面
T3 评审对象是草稿：没有草稿 ⇒ 拒绝开评审
T4 全员通过 ⇒ 草稿转正稿、草稿清空、状态 approved
T5 有正稿 ⇒ 派工三件回到工具面
T6 coder 卡不在这条流程里（没有需求文档、不评审）
"""
from __future__ import annotations

import pytest

from src.api import card_tools, function_room
from src.storage import card_store, function_store, review_store, work_store

GATED = ("create_card", "create_coder_card", "direct")


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH,
                       str(function_room._DEFAULT_PATH))
    function_room.reset_cache()


async def _seed(monkeypatch):
    _use_repo_catalog(monkeypatch)
    w = await work_store.create_work("需求生命周期")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    await function_room.seed_work(wid)
    sub = (await card_store.create_card(wid, "子卡", "模型生成",
                                        parent=root))["meta"]["card_id"]
    return wid, root, sub


def _face(wid, cid):
    cards = card_tools._collect_card_meta(wid)
    return {str(t.get("name")) for t in
            card_tools.build_tool_schemas(card_id=cid, work_id=wid, cards=cards)}


# ---------------------------------------------------------------- T2 / T5

@pytest.mark.asyncio
async def test_no_approved_requirements_means_no_dispatch(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    names = _face(wid, sub)
    for t in GATED:
        assert t not in names, f"没有正稿却拿到了派工工具 {t}"
    # 汇报与同步不受影响
    assert "report_up" in names and "sync" in names


# ---------------------------------------------------------------- T1

@pytest.mark.asyncio
async def test_draft_is_private_and_does_not_touch_approved(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    await card_store.update_card(wid, sub, {"requirements": "正稿：R1 导出"}, "预置正稿")
    await card_store.update_card(wid, sub, {"requirements_state": "approved"}, "预置状态")

    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(
        wid, sub, "edit_requirements",
        {"old_string": "正稿：R1 导出", "new_string": "正稿：R1 导出（改）"}, cards)
    assert r.get("ok") is True, r

    meta = card_store._meta_of(wid, sub)
    body = (await card_store.read_card(wid, sub))["body"]
    assert str(meta.get("requirements_draft") or "").strip() == "正稿：R1 导出（改）"
    assert str(body.get("requirements") or "").strip() == "正稿：R1 导出", "正稿不该被草稿改动带跑"

    # 别的卡读它：只有正稿，看不到草稿
    reader = (await card_store.create_card(wid, "读者", "模型生成",
                                           parent=root))["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    out = await card_tools.execute_tool(wid, reader, "read_card",
                                        {"card_id": sub}, cards)
    body = out["data"]
    assert "R1 导出（改）" not in str(body), "草稿不该进别人的读卡结果"
    assert "正稿：R1 导出" in str(body.get("需求文档") or "")


# ---------------------------------------------------------------- T3

@pytest.mark.asyncio
async def test_review_requires_draft(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    cards = card_tools._collect_card_meta(wid)
    # [2026-10-28 用户令] 评审只能由**该卡自己**发起
    r = await card_tools.execute_tool(
        wid, sub, "open_review",
        {"focus": "需求", "brief": "评审需求"}, cards)
    assert r.get("ok") is False, r
    assert "草稿" in r["error"]["message"], r


@pytest.mark.asyncio
async def test_only_the_card_itself_can_open_review(sandbox, monkeypatch):
    """[2026-10-28 用户令「必须经过子卡发起评审」]

    草稿谁都能写（自己／父卡／根卡），但开评审只能这张卡自己：
    代开等于替它答应。父卡写了草稿 ⇒ 用 direct 通知它来开。
    """
    wid, root, sub = await _seed(monkeypatch)
    cards = card_tools._collect_card_meta(wid)
    await card_tools.execute_tool(
        wid, sub, "edit_requirements",
        {"old_string": "", "new_string": "R1 草稿"}, cards)
    # 根卡代开 → 拒
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(
        wid, root, "open_review",
        {"card_id": sub, "focus": "需求", "brief": "根卡代开"}, cards)
    assert r.get("ok") is False, r
    assert r["error"]["code"] == "denied", r
    assert card_store._meta_of(wid, sub).get("requirements_state") == "draft"
    # 本卡自己开 → 通过
    cards = card_tools._collect_card_meta(wid)
    r2 = await card_tools.execute_tool(
        wid, sub, "open_review",
        {"focus": "需求", "brief": "本卡自己开"}, cards)
    assert r2.get("ok") is not False, r2
    assert card_store._meta_of(wid, sub).get("requirements_state") == "in_review"


# ---------------------------------------------------------------- T4 / T5

@pytest.mark.asyncio
async def test_all_approved_promotes_draft_to_final(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    cards = card_tools._collect_card_meta(wid)
    await card_tools.execute_tool(
        wid, sub, "edit_requirements",
        {"old_string": "", "new_string": "R1 支持导出（判据：pytest 全绿）"}, cards)
    assert card_store._meta_of(wid, sub).get("requirements_state") == "draft"

    # 开需求评审（对象＝草稿）
    cards = card_tools._collect_card_meta(wid)
    opened = await card_tools.execute_tool(
        wid, sub, "open_review",
        {"focus": "需求", "brief": "评审需求草稿"}, cards)
    assert opened.get("ok") is not False, opened
    assert card_store._meta_of(wid, sub).get("requirements_state") == "in_review"

    rid = await review_store.ensure_review_card(wid, sub, "子卡")
    members = await review_store.members_of(wid, rid)
    assert members, "评审要有在职职能成员"

    # 全员逐个 approve，最后一个触发收口 + 转正
    for i, m in enumerate(members):
        cards = card_tools._collect_card_meta(wid)
        r = await card_tools.execute_tool(wid, m, "approve",
                                          {"review_card_id": rid}, cards)
        assert r.get("ok") is not False, (i, m, r)

    meta = card_store._meta_of(wid, sub)
    body = (await card_store.read_card(wid, sub))["body"]
    assert meta.get("requirements_state") == "approved", meta.get("requirements_state")
    assert "R1 支持导出" in str(body.get("requirements") or ""), "草稿要转成正稿"
    assert not str(meta.get("requirements_draft") or "").strip(), "转正后草稿要清空"

    # T5 有正稿 ⇒ 派工三件回到工具面
    names = _face(wid, sub)
    for t in GATED:
        assert t in names, f"已有正稿却仍拿不到 {t}"


# ---------------------------------------------------------------- T6

@pytest.mark.asyncio
async def test_coder_card_not_in_lifecycle(sandbox, monkeypatch):
    wid, root, sub = await _seed(monkeypatch)
    coder = (await card_store.create_card(wid, "编程卡", "模型生成", parent=root,
                                          card_type="coder"))["meta"]["card_id"]
    names = _face(wid, coder)
    assert "edit_requirements" not in names, "编程卡没有需求文档"
    # 编程卡本来就没有派工三件（无子树），这里只钉"它不受需求评审限制"
    assert card_store._meta_of(wid, coder).get("requirements_state") in (None, "")
