# -*- coding: utf-8 -*-
"""[级联删除 2026-10-21 · 用户令] 有子卡不再拒删，一次删整棵子树。

用户质问：「为什么有子卡的项目删不了？凭什么删不了？」
原设计把复杂度推给用户（先手动删子卡）；正确语义是删父卡 = 删它和它下面的一切，
每张都进回收站、可逐个恢复。
"""
from __future__ import annotations

import pytest

from src.storage import card_store
from src.storage.card_store import CardDeleteDeniedError


async def _mk(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "用户直接创建", card_type="", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_delete_with_children_denied_without_cascade(work):
    """不带 cascade：仍然拒删（旧行为保留，供模型工具等保守调用方）。"""
    wid, root = work["work_id"], work["work_card_id"]
    mid = await _mk(wid, "中间卡", root)
    await _mk(wid, "叶子卡", mid)
    with pytest.raises(CardDeleteDeniedError):
        await card_store.delete_card(wid, mid)
    assert card_store.card_exists(wid, mid)


async def test_cascade_delete_removes_whole_subtree(work):
    """带 cascade：整棵子树（孙卡在内）一次删净，且每张都进回收站。"""
    wid, root = work["work_id"], work["work_card_id"]
    mid = await _mk(wid, "中间卡", root)
    leaf = await _mk(wid, "叶子卡", mid)
    grand = await _mk(wid, "孙卡", leaf)

    r = await card_store.delete_card(wid, mid, cascade=True)
    assert r.get("ok") is True
    assert r.get("cascade") is True, r
    assert set(r.get("deleted_subtree") or []) == {leaf, grand}, r

    for cid in (mid, leaf, grand):
        assert not card_store.card_exists(wid, cid), f"{cid} 应已删除"

    trash = {t["card_id"] for t in await card_store.list_trash(wid)}
    assert {mid, leaf, grand} <= trash, f"回收站应能找回：{trash}"


async def test_cascade_does_not_touch_siblings(work):
    """只删子树，不动兄弟分支。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    a1 = await _mk(wid, "甲的子", a)
    b = await _mk(wid, "乙", root)
    await card_store.delete_card(wid, a, cascade=True)
    assert not card_store.card_exists(wid, a)
    assert not card_store.card_exists(wid, a1)
    assert card_store.card_exists(wid, b), "兄弟分支不该被波及"


async def test_root_card_still_denied(work):
    """项目根卡仍然不可删（它是项目入口）。"""
    wid, root = work["work_id"], work["work_card_id"]
    with pytest.raises(CardDeleteDeniedError):
        await card_store.delete_card(wid, root, cascade=True)
