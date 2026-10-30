# -*- coding: utf-8 -*-
"""[卡面版本 2026-10-24 · 用户令] 正文块历史版本：写前存旧值、可列、可回退。

背景：card_021 一次 edit_card 把 64,802 字符的需求块覆盖成 12 字符，
写入闸没有任何反应（需求文档超集校验已废除）。用户裁定：**不加拦截，
加历史版本记录，让模型操作失误后能自己找回**。

本文件钉死四条：
1. 写前存**旧值**（回退到 N ＝ 回到第 N 次改动之前）；
2. 只存**变了的块**（未动块不复制）；
3. 每卡保留最近 `_REVISION_KEEP` 版，超了删最旧；
4. 工具层 `edit_card` 的 `list_versions` / `restore_version` 两条通道。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_db, card_store


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _revs(wid: str, cid: str) -> list[dict]:
    return await card_db.run_read(
        wid, lambda c: card_db.list_revisions(c, cid), default=[])


async def _rev_blocks(wid: str, cid: str, ver: int) -> dict[str, str]:
    return await card_db.run_read(
        wid, lambda c: card_db.get_revision(c, cid, ver), default={})


# ------------------------------------------------------------------ 存旧值

async def test_revision_stores_pre_image(work):
    """改之前那块长什么样，存的就是它——回退到 N ＝ 回到第 N 次改动之前。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-旧值", root)
    before = (await card_store.read_card(wid, cid))["body"]["status_block"]

    await card_store.update_card(wid, cid, {"status_block": "新现状-ABC"}, "测试改现状")

    vers = await _revs(wid, cid)
    assert vers, "写入后应有版本记录"
    top = vers[0]
    assert top["sizes"]["status_block"] == len(before), "存的应是**改前**长度"
    blocks = await _rev_blocks(wid, cid, top["version"])
    assert blocks["status_block"] == before, "存的内容应逐字等于改前内容"


async def test_only_changed_block_recorded(work):
    """只动现状时，版本里只出现 status_block，不复制使命/需求文档。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-只存变动块", root)

    await card_store.update_card(wid, cid, {"status_block": "只改现状"}, "测试")

    top = (await _revs(wid, cid))[0]
    assert top["blocks"] == ["status_block"], top["blocks"]


async def test_meta_only_change_makes_no_version(work):
    """只改标题（不碰正文块）⇒ 不产生版本，不浪费版本号。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-只改元数据", root)
    n0 = len(await _revs(wid, cid))

    await card_store.update_card(wid, cid, {"title": "改了标题"}, "测试改标题")

    assert len(await _revs(wid, cid)) == n0


# -------------------------------------------------------------------- 回退

async def test_restore_brings_back_previous_content(work):
    """改两次后回退到第 1 版 ⇒ 内容回到第一次改动**之前**。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-回退", root)
    v0 = (await card_store.read_card(wid, cid))["body"]["status_block"]

    await card_store.update_card(wid, cid, {"status_block": "第一版"}, "改1")
    await card_store.update_card(wid, cid, {"status_block": "第二版"}, "改2")
    assert (await card_store.read_card(wid, cid))["body"]["status_block"] == "第二版"

    # 找存着 v0 的那个版本
    target = None
    for v in await _revs(wid, cid):
        b = await _rev_blocks(wid, cid, v["version"])
        if b.get("status_block") == v0:
            target = v["version"]
            break
    assert target is not None, "应能找到存着初始内容的版本"

    blocks = await _rev_blocks(wid, cid, target)
    r = await card_store.update_card(wid, cid, blocks, f"从版本 {target} 恢复")
    assert "error" not in r
    assert (await card_store.read_card(wid, cid))["body"]["status_block"] == v0


async def test_restore_is_itself_versioned(work):
    """回退前也会先存一版 ⇒ 回退错了还能再退回来。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-回退可再退", root)
    await card_store.update_card(wid, cid, {"status_block": "A"}, "改A")
    await card_store.update_card(wid, cid, {"status_block": "B"}, "改B")
    n_before = len(await _revs(wid, cid))

    blocks = await _rev_blocks(wid, cid, 1)
    await card_store.update_card(wid, cid, blocks, "从版本 1 恢复")

    assert len(await _revs(wid, cid)) == n_before + 1


# ---------------------------------------------------------------- 保留上限

async def test_keep_limit_prunes_oldest(work):
    """连改 25 次 ⇒ 版本数不超过 _REVISION_KEEP（删最旧）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-保留上限", root)
    keep = card_db._REVISION_KEEP
    for i in range(keep + 5):
        await card_store.update_card(wid, cid, {"status_block": f"第{i}次"}, f"改{i}")

    vers = await _revs(wid, cid)
    assert len(vers) <= keep
    # 最旧的必须已经被删（版本号连续到 keep 以内）
    assert max(v["version"] for v in vers) - min(v["version"] for v in vers) < keep


# ------------------------------------------------------------ 工具层两条通道

async def test_edit_card_list_versions_tool(work):
    """edit_card(list_versions=true) 只读列版本，不写卡。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-工具列", root)
    await card_store.update_card(wid, cid, {"status_block": "改动"}, "测试")
    cards = card_tools._collect_card_meta(wid)
    before = (await card_store.read_card(wid, cid))["body"]["status_block"]

    r = await card_tools.execute_tool(
        wid, cid, "edit_card", {"card_id": cid, "list_versions": True}, cards)

    assert r.get("ok") is True, r
    assert r["data"]["versions"], r["data"]
    assert "restore_version" in r["data"]["hint"]
    # 只读：卡面一个字没动
    assert (await card_store.read_card(wid, cid))["body"]["status_block"] == before


async def test_edit_card_restore_tool(work):
    """edit_card(restore_version=N) 回退成功，且不需要 content。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-工具回退", root)
    v0 = (await card_store.read_card(wid, cid))["body"]["status_block"]
    await card_store.update_card(wid, cid, {"status_block": "改坏了"}, "改坏")
    cards = card_tools._collect_card_meta(wid)

    target = None
    for v in await _revs(wid, cid):
        b = await _rev_blocks(wid, cid, v["version"])
        if b.get("status_block") == v0:
            target = v["version"]
            break
    assert target is not None

    r = await card_tools.execute_tool(
        wid, cid, "edit_card", {"card_id": cid, "restore_version": target}, cards)

    assert r.get("ok") is True, r
    assert r["data"]["restored_from"] == target
    assert (await card_store.read_card(wid, cid))["body"]["status_block"] == v0


async def test_edit_card_missing_version_is_not_found(work):
    """回退到不存在的版本 ⇒ not_found，卡面不动。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-不存在", root)
    cards = card_tools._collect_card_meta(wid)
    before = (await card_store.read_card(wid, cid))["body"]["status_block"]

    r = await card_tools.execute_tool(
        wid, cid, "edit_card", {"card_id": cid, "restore_version": 9999}, cards)

    assert r.get("ok") is False
    assert r["error"]["code"] == "not_found"
    assert (await card_store.read_card(wid, cid))["body"]["status_block"] == before


async def test_edit_card_empty_content_rejected(work):
    """既没 content 也没版本参数 ⇒ validation（不许把卡清空）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk_child(wid, "版本-空内容", root)
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(
        wid, cid, "edit_card", {"card_id": cid}, cards)

    assert r.get("ok") is False
    assert r["error"]["code"] == "validation"
