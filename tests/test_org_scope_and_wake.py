# -*- coding: utf-8 -*-
"""[2026-10-22 · 用户三条需求] 组织权与唤醒权的回归测试。

## 三条需求

1. **根卡/父卡能任意管理、激活自己树内的所有卡**
   旧通道 `resume_child` 有四条硬限制（必须 auto、必须有 goal、不能 complete、
   只能子树内），所以非 auto 卡 / 无 goal 卡 / 已完成卡都唤不醒。
   → 新增 `wake_card`：不论状态，一律「让它现在跑一轮」。

2. **根卡也能激活角色卡**
   角色卡离树（parent=""），`_is_in_subtree` 对它恒假 ⇒ resume_child 必然拒。
   → `wake_card` 的作用域：根卡＝本项目任何卡（含离树角色）。

3. **角色卡能在整个树内任意位置挂载卡片来执行它布置的任务**
   role 原本没有 create_child，且 create/reparent 的判定都是「只能在自己子树内」，
   而角色卡没有子树 ⇒ 什么都做不了。
   → role 补 create_child ＋ `_org_scope_allows`：离树角色卡的作用域＝整棵树（不含根卡）。

## 这套测试守住的两类东西

**正例**：上面三条需求真的能用。
**反例**：不能因此放大权限——
   · 角色卡动不了根卡（根卡是项目身份）
   · 兄弟卡之间不能互相唤醒
   · role 仍不得有**执行权**（tools_file / delete / plan / invoke_tool）
     〔v14 · 2026-10-24〕**tools_web 除外**——用户裁定「查资料≠动手」，
     所有卡型（含 role）都能查外部资料。
"""
from __future__ import annotations

import asyncio

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store, work_store


# ---------------------------------------------------------------- 配置层


def test_role_can_create_child_but_still_no_exec():
    """role 有 create_child（要能组队），但**不得**有执行权。

    〔v14 · 2026-10-24 · 用户裁定「查资料≠动手」〕**tools_web 给了**（查外部资料，
    只读检索，无执行面）；执行权仍然不给——角色不干活。
    """
    perms, _ = policy.compute_perms("rd_project", "role")
    assert "create_child" in perms, (
        "role 缺 create_child：用户要求「角色卡能挂载它需要的卡片」，"
        "没有这个权限它一张卡都建不出来")
    # 执行权仍然不给——角色不干活（tools_web 是「查资料」，不是执行权）
    for f in ("tools_file", "delete", "invoke_tool", "plan"):
        assert f not in perms, f"role 不应有 {f}（那是执行权/破坏性动作）"
    # 但能查资料（v14：所有卡型一律有 tools_web）
    assert "tools_web" in perms, "role 应有 tools_web（查资料≠动手）"
    # 组织权要给全
    for f in ("link", "unlink", "edit", "read", "send_card_message"):
        assert f in perms, f"role 应有 {f}"


def test_wake_card_registered():
    """wake_card 必须注册，挂 **CREATE_CHILD**。

    为什么不挂 EDIT：唤醒是**组织动作**（叫别人干活），按 D04「执行权与组织权
    分离」，它应该和 create_child 同档——这样只有 project/sub_project/role
    能唤醒，而 coder/tool（执行体）拿不到。挂 EDIT 会让 coder/tool 也能叫醒别人，
    等于执行体获得了组织权。
    """
    assert "wake_card" in card_tools.TOOL_REGISTRY
    assert card_tools.TOOL_SWITCH["wake_card"] == policy.SWITCH_CREATE_CHILD
    td = card_tools.TOOL_REGISTRY["wake_card"]
    assert td.readonly is False
    assert td.schema["parameters"]["required"] == ["card_id"]
    assert "why" in td.schema["parameters"]["properties"]


# ---------------------------------------------------------------- 需求 1


async def test_root_can_wake_non_auto_completed_card(work, monkeypatch):
    """需求 1：根卡能唤醒「非 auto + 已完成」的卡——旧通道做不到这种卡。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "沉睡的卡", "模型生成", parent=root)
    cid = r["meta"]["card_id"]
    await card_store.update_card(wid, cid,
                                 {"auto_mode": 0, "state": "completed"},
                                 "测试：造一个旧通道唤不醒的状态")
    meta = card_store._meta_of(wid, cid) or {}
    assert not meta.get("auto_mode"), "夹具应是非 auto"

    # 记录 _run_message 被调起（不真跑模型）
    calls = []

    async def fake_run(work_id, card_id, content, *a, **kw):
        calls.append({"work_id": work_id, "card_id": card_id,
                      "content": content})
        return {"ok": True}

    from src.api import routes as _routes
    monkeypatch.setattr(_routes, "_run_message", fake_run)
    # wake_prompt 分支用的就是 _routes._run_message（懒 import，patch 生效）

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_wake_card(
        wid, root, {"card_id": cid, "why": "检查一下你上次留下的东西"}, cards)

    assert res.get("ok") is True, res
    assert res.get("started") is True, res
    # _spawn_tracked 走 asyncio.create_task，让事件循环跑一下再看
    await asyncio.sleep(0.05)
    assert calls, "应该真的起了一轮"
    assert "检查一下你上次留下的东西" in calls[0]["content"], (
        "why 必须进 prompt——否则被叫醒的卡不知道为什么醒")
    assert calls[0]["card_id"] == cid


async def test_root_can_wake_card_without_goal(work, monkeypatch):
    """需求 1：无 goal 的卡也能被唤醒（resume_child 会报「没有目标」）。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "无目标的卡", "模型生成", parent=root)
    cid = r["meta"]["card_id"]

    calls = []

    async def fake_run(work_id, card_id, content, *a, **kw):
        calls.append(card_id)
        return {"ok": True}

    from src.api import routes as _routes
    monkeypatch.setattr(_routes, "_run_message", fake_run)

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_wake_card(wid, root, {"card_id": cid}, cards)
    assert res.get("ok") is True, res
    assert res.get("started") is True, res
    await asyncio.sleep(0.05)
    assert calls, "无 goal 的卡也应被真的叫起一轮"


# ---------------------------------------------------------------- 需求 2


async def test_root_can_wake_offtree_role(work, monkeypatch):
    """需求 2：根卡能唤醒**离树**的角色卡——这是 resume_child 永远做不到的。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "产品负责人", "模型生成",
                                     card_type="role")
    rid = r["meta"]["card_id"]
    meta = card_store._meta_of(wid, rid) or {}
    assert str(meta.get("parent") or "") == "", "角色卡应离树"
    # 前提确认：resume_child 的判据对它必然为假
    cards = card_tools._collect_card_meta(wid)
    assert policy._is_in_subtree(root, rid, cards) is False

    started = {"n": 0}

    async def fake_run(*a, **kw):
        started["n"] += 1
        return {"ok": True}

    from src.api import routes as _routes
    monkeypatch.setattr(_routes, "_run_message", fake_run)

    res = await card_tools._h_wake_card(
        wid, root, {"card_id": rid, "why": "现在做一次巡检"}, cards)
    assert res.get("ok") is True, res
    # 两条路都算对：有待处理同步走巡检通道；没有则主动造一轮
    assert res.get("via") in ("role_manual_check", "wake_prompt"), res
    assert res.get("started") is True, res


async def test_sibling_cannot_wake_each_other(work):
    """反证：兄弟卡之间不能互相唤醒（作用域没被放大成「谁都能叫谁」）。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    a = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    b = await card_store.create_card(wid, "乙", "模型生成", parent=root)
    aid, bid = a["meta"]["card_id"], b["meta"]["card_id"]

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_wake_card(wid, aid, {"card_id": bid}, cards)
    assert res.get("ok") is False, f"兄弟互唤醒应被拒: {res}"
    assert res.get("error", {}).get("code") == "denied", res


# ---------------------------------------------------------------- 需求 3


async def test_role_can_create_card_anywhere_in_tree(work):
    """需求 3：角色卡能在**别人**的子树下建卡（旧判定要求在自己子树内，恒假）。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "产品负责人", "模型生成",
                                     card_type="role")
    rid = r["meta"]["card_id"]
    host = await card_store.create_card(wid, "承载卡", "模型生成", parent=root)
    host_id = host["meta"]["card_id"]

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_create_card(
        wid, rid,
        {"title": "角色派的活",
         "mission": "由产品负责人派出的执行单元，负责某一具体交付面。",
         "parent": host_id},
        cards)
    assert res.get("ok") is True, f"角色卡应能挂到树内任意位置: {res}"
    new_id = res.get("card_id")
    m = card_store._meta_of(wid, new_id) or {}
    assert str(m.get("parent")) == host_id, f"新卡应挂在 {host_id} 下: {m.get('parent')}"


async def test_role_can_create_coder_with_parent(work):
    """需求 3：角色卡能建**执行卡**并指定挂点（否则它建的执行卡无处可挂）。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "架构师", "模型生成", card_type="role")
    rid = r["meta"]["card_id"]
    host = await card_store.create_card(wid, "承载卡", "模型生成", parent=root)
    host_id = host["meta"]["card_id"]

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_create_coder_card(
        wid, rid,
        {"title": "执行单元", "task": "完成架构师交代的一件事。",
         "parent": host_id},
        cards)
    assert res.get("ok") is True, f"角色卡应能建执行卡到指定位置: {res}"
    new_id = res.get("card_id")
    m = card_store._meta_of(wid, new_id) or {}
    assert str(m.get("card_type")) == "coder"
    assert str(m.get("parent")) == host_id


async def test_role_can_reparent_any_card_in_tree(work):
    """需求 3：角色卡能移动树内任意卡（旧判定要求「两者都在自己子树内」，恒假）。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "产品负责人", "模型生成",
                                     card_type="role")
    rid = r["meta"]["card_id"]
    a = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    b = await card_store.create_card(wid, "乙", "模型生成", parent=root)
    aid, bid = a["meta"]["card_id"], b["meta"]["card_id"]

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_reparent_card(
        wid, rid, {"card_id": aid, "new_parent_id": bid}, cards)
    assert res.get("ok") is True, f"角色卡应能移动树内任意卡: {res}"
    m = card_store._meta_of(wid, aid) or {}
    assert str(m.get("parent")) == bid


async def test_role_cannot_touch_root(work):
    """反证：角色卡**动不了根卡**——根卡是项目身份，挂走它等于换项目。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "产品负责人", "模型生成",
                                     card_type="role")
    rid = r["meta"]["card_id"]
    host = await card_store.create_card(wid, "承载卡", "模型生成", parent=root)
    host_id = host["meta"]["card_id"]

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_reparent_card(
        wid, rid, {"card_id": root, "new_parent_id": host_id}, cards)
    assert res.get("ok") is False, f"角色卡不该能移动根卡: {res}"
    assert res.get("error", {}).get("code") == "denied", res


async def test_in_tree_card_scope_unchanged(work):
    """反证：树内的卡作用域**没变**——它仍然只能动自己子树内的卡。

    这条防止「为了方便角色卡而把所有卡的作用域都放开了」这个错误。
    """
    wid = work["work_id"]
    root = work["work_card_id"]

    a = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    b = await card_store.create_card(wid, "乙", "模型生成", parent=root)
    aid, bid = a["meta"]["card_id"], b["meta"]["card_id"]
    a_child = await card_store.create_card(wid, "甲的子树", "模型生成", parent=aid)
    ac_id = a_child["meta"]["card_id"]

    # 甲能移动自己子树内的卡
    cards = card_tools._collect_card_meta(wid)
    res1 = await card_tools._h_reparent_card(
        wid, aid, {"card_id": ac_id, "new_parent_id": aid}, cards)
    assert res1.get("ok") is True, f"甲应能整理自己子树: {res1}"

    # 甲**不能**移动兄弟乙
    cards = card_tools._collect_card_meta(wid)
    res2 = await card_tools._h_reparent_card(
        wid, aid, {"card_id": bid, "new_parent_id": aid}, cards)
    assert res2.get("ok") is False, f"甲不该能移动兄弟乙: {res2}"