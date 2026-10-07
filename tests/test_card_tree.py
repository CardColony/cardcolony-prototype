# -*- coding: utf-8 -*-
"""不变量一：卡片 CRUD ＋ 权限由树机械算出（card_012 工作项 2）。

权限是产品的第一不变量：谁能读、谁能写、谁能删，全部由 parent 树与
relations 算出——不看内容、不看类型、不问模型。树一动就坏的东西，先钉死。

约定（card_tools.py 权威定义）：
- 读 = 全盘可读（[卡片权限改造 2026-10-07] 存在即放行；原亲属链/连线/锚卡读限整制去除）
- 写 = 仅自己的子树（血统链上级不可写）
- 越权一律返回 {"error": ...} 数据，不抛异常（工具循环靠这个继续跑）
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.storage import card_store, shadow_store


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


# ---------------------------------------------------------------- 根卡与序号

async def test_work_root_card_shape(work):
    wid, root = work["work_id"], work["work_card_id"]
    assert root == "card_001"

    data = await card_store.read_card(wid, root)
    assert data["meta"]["card_type"] == "剧本"
    assert data["meta"]["parent"] == ""          # 剧本卡无视 parent 入参
    assert data["meta"]["card_id"] == root

    # 一正一影：建卡即派生
    assert shadow_store.shadow_path(wid, root).exists()
    # 序号在工作内独立自增
    assert card_store._next_seq(wid) == 2


# ---------------------------------------------------------------- 权限纯函数

async def test_permission_is_computed_from_tree(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)         # card_002
    b = await _mk_child(wid, "乙", a)            # card_003（甲的子）
    c = await _mk_child(wid, "丙", root)         # card_004（甲的旁支）
    cards = card_tools._collect_card_meta(wid)

    # 读：[卡片权限改造 2026-10-07] 全盘放开——存在即放行（含旁支；
    # 原「旁支不可读」断言随亲属链读限机制退役）
    assert card_tools.can_read(a, root, cards)
    assert card_tools.can_read(a, b, cards)
    assert card_tools.can_read(b, root, cards)
    assert card_tools.can_read(root, c, cards)   # 根可读全部
    assert card_tools.can_read(a, c, cards)
    assert card_tools.can_read(c, b, cards)

    # 写：只有自己的子树，血统链上级也不可写
    assert card_tools.can_edit(a, a, cards)
    assert card_tools.can_edit(a, b, cards)
    assert not card_tools.can_edit(a, root, cards)
    assert not card_tools.can_edit(b, a, cards)
    assert not card_tools.can_edit(a, c, cards)

    # 不存在的卡一律无权（判存在在工具层，纯函数层给 False）
    assert not card_tools.can_read(a, "card_999", cards)
    assert not card_tools.can_edit(a, "card_999", cards)


# ---------------------------------------------------------------- 工具层语义

async def test_tool_errors_are_data_not_exceptions(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    c = await _mk_child(wid, "丙", root)
    cards = card_tools._collect_card_meta(wid)

    # [卡片权限改造 2026-10-07] 读侧全盘放开：旁支 read_card 放行（原越权断言退役）
    r = await card_tools.execute_tool(wid, b, "read_card", {"card_id": c}, cards)
    assert "error" not in r and r["card_id"] == c

    # 先判存在再判权限：已删/不存在的卡报「不存在」，不误报越权
    r = await card_tools.execute_tool(wid, b, "read_card", {"card_id": "card_999"}, cards)
    assert "不存在" in r["error"]

    r = await card_tools.execute_tool(wid, b, "edit_card",
                                      {"card_id": root, "content": "越权改根"}, cards)
    assert "越权" in r["error"]
    # 越权必须真的没落盘
    root_body = (await card_store.read_card(wid, root))["body"]["conclusion"]
    assert "越权改根" not in root_body

    # [卡片权限改造 2026-10-07] 旁支读影子放行（原越权断言随读限退役；
    # 影子写侧独占不变）——错误即数据性质由下方越权改根用例继续覆盖


async def test_edit_own_subtree_and_shadow_backdoor(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(wid, a, "edit_card",
                                      {"card_id": b, "content": "乙的新正文"}, cards)
    assert r.get("ok") is True
    assert (await card_store.read_card(wid, b))["body"]["conclusion"] == "乙的新正文"

    # 影子进同一 meta 字典，但只能由对应正卡经 update_shadow 写
    sid = shadow_store.shadow_id_of(b)
    assert sid in cards
    assert cards[sid]["card_type"] == "shadow"
    assert cards[sid]["parent"] == b

    r = await card_tools.execute_tool(wid, a, "edit_card",
                                      {"card_id": sid, "content": "黑入影子"}, cards)
    assert "error" in r and "update_shadow" in r["error"]


async def test_update_shadow_is_silent_for_canvas(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    c = await _mk_child(wid, "丙", root)
    cards = card_tools._collect_card_meta(wid)

    canvas = card_store.WORKS_DIR / wid / "canvas.json"
    before = canvas.read_bytes()

    body = ("## 进度\n- 已建网\n\n## 产物清单\n- tests/\n\n## 迭代历史\n- v1\n")
    r = await card_tools.execute_tool(wid, a, "update_shadow", {"content": body}, cards)
    assert r.get("ok") is True
    # 影子写入不得扰动画布缓存（影子不进画布）
    assert canvas.read_bytes() == before

    raw = shadow_store.shadow_path(wid, a).read_text(encoding="utf-8")
    assert "## 进度" in raw and "tests/" in raw
    # 影子写侧恒为当前卡自己的影子——正是「被写入者」是 a
    assert f"shadow_of: \"{a}\"" in raw

    # 读侧跟随 can_read：[卡片权限改造 2026-10-07] 全盘放开——旁支读影子放行
    # （原越权断言随读限机制退役；影子写侧独占不变）
    r = await card_tools.execute_tool(wid, c, "read_shadow", {"card_id": a}, cards)
    assert r["owner_card_id"] == a and "## 进度" in r["content"]
    # 上级可读下级影子（推送到达机制）
    r = await card_tools.execute_tool(wid, root, "read_shadow", {"card_id": a}, cards)
    assert r["owner_card_id"] == a and "## 进度" in r["content"]


# ---------------------------------------------------------------- 删除护栏

async def test_delete_guards_and_cascade(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    b = await _mk_child(wid, "乙", a)
    c = await _mk_child(wid, "丙", root)
    cards = card_tools._collect_card_meta(wid)

    # 剧本根卡不可删
    with pytest.raises(card_store.CardDeleteDeniedError):
        await card_store.delete_card(wid, root)
    # 有子卡的卡不可删（防孤儿）
    with pytest.raises(card_store.CardDeleteDeniedError):
        await card_store.delete_card(wid, a)
    # 工具层：越权删在权限层就被拦
    r = await card_tools.execute_tool(wid, c, "delete_card", {"card_id": a}, cards)
    assert "越权" in r["error"]

    # 叶子可删：卡文件 ＋ session ＋ 影子 三件同去
    seq = b.split("_", 1)[1]
    session_file = card_store.WORKS_DIR / wid / "sessions" / f"session_{seq}.md"
    shadow_file = shadow_store.shadow_path(wid, b)
    assert session_file.exists() and shadow_file.exists()

    r = await card_tools.execute_tool(wid, a, "delete_card", {"card_id": b}, cards)
    assert r.get("ok") is True and r["deleted"] == b
    assert not card_store._card_path(wid, b).exists()
    assert not shadow_file.exists()
    assert not session_file.exists()
    # 同轮后续工具不得再看到已删卡（缓存同步）
    assert b not in cards


# ---------------------------------------------------------------- 哈希自洽

async def test_read_card_keeps_hash_consistent(work):
    """纯读写不得触发「外部修改」：读一次不该改变文件一字节。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, "甲", root)
    path = card_store._card_path(wid, a)

    before = path.read_bytes()
    for _ in range(3):
        data = await card_store.read_card(wid, a)
    assert path.read_bytes() == before
    assert not any("外部修改" in line for line in data["body"]["history"])