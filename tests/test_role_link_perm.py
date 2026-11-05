# -*- coding: utf-8 -*-
"""[2026-10-22 · 用户实测「根卡不能连线自己的角色卡」] role 的建线权回归测试。

## 为什么有这份文件

role 类型的 perms 原本是：
    read / edit / read_linked / tools_read_file / send_card_message / shadow_write
——**有 send_card_message 却没有 link**。而发消息的前置是
「已有通信线 OR 互为祖先-后代」；角色卡是**离树**的（parent=""），
永远不可能是任何卡的祖先/后代 ⇒ 只能靠通信线；没有 link 就建不了线。
⇒ 两头堵死：谁都连不上它，它也发不出任何消息。

历史印证：work_018 的涌现观察员当时报过「我能观测、能落账，
但观测产出送不到任何地方」——根因就在这里。

## 判据（可推广）

**离树的卡必须有 link**，否则它没有任何通信路径。
   · project / sub_project：在树内，祖先链兜底 ⇒ link 是"能主动连别人"
   · worker / tool：在树内，主卡底下 ⇒ 祖先链兜底，不需要 link
   · role：离树，**只有线** ⇒ 必须给 link

## 同时守住的反面

补 link 是补"组织权"，**不是**补执行权。role 仍不得有
tools_file / tools_web / create_child / delete / bash 一类能力。
"""
from __future__ import annotations

import pytest

from src.api import card_tools
from src.permissions import policy
from src.storage import card_store, work_store


# ---------------------------------------------------------------- 配置层


def test_role_has_link_and_unlink():
    """配置里 role 必须有 link / unlink —— 否则角色无法被连线、也无法连别人。"""
    perms, _ver = policy.compute_perms("rd_project", "role")
    assert "link" in perms, (
        "role 缺 link：角色卡离树，没有通信线就彻底孤立——"
        "既连不上别人，别人也连不上它")
    assert "unlink" in perms, "role 缺 unlink：建了线断不掉"

    rp, _ = policy.compute_perms("research_project", "role")
    assert "link" in rp and "unlink" in rp, "research_project 的 role 同样要补"


def test_role_still_has_no_exec_rights():
    """补 link 是补组织权，不是补执行权——role 仍不得有写文件/执行/建卡能力。

    〔v14 · 2026-10-24 · 用户裁定「查资料≠动手」〕**tools_web 给了**（查外部资料，
    只读检索、无执行面）；执行权仍然不给——角色不干活。
    """
    perms, _ = policy.compute_perms("rd_project", "role")
    forbidden = ("tools_file", "delete", "invoke_tool", "plan")
    for f in forbidden:
        assert f not in perms, f"role 不应有 {f}（那是执行权/破坏性动作）"
    # 查资料：所有卡型都有（v14）
    assert "tools_web" in perms, "role 应有 tools_web（查资料≠动手）"
    # [2026-10-22 · 用户需求] 组织权要给全——包括建卡（挂载执行单元）
    for f in ("link", "unlink", "create_child", "send_card_message"):
        assert f in perms, f"role 应有 {f}（组织权）"


# ---------------------------------------------------------------- 行为层


async def test_root_can_link_to_role_card(work):
    """根卡给 role 卡建线：必须成功，且线真的落在卡面上。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "产品负责人", "模型生成",
                                     card_type="role")
    rid = r["meta"]["card_id"]

    # 新建的 role 卡就该带 link（配置驱动，不是手工补）
    meta = card_store._meta_of(wid, rid) or {}
    assert "link" in (meta.get("perms") or []), (
        f"新建 role 卡未带 link，落库 perms={meta.get('perms')}")

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_link_cards(
        wid, root,
        {"target_id": rid, "type": "关联",
         "note": "订阅产品负责人：需求范围与可观测性把关"},
        cards)
    assert res.get("ok") is True, f"根卡连 role 卡失败: {res}"

    # 卡面复核
    m2 = card_store._meta_of(wid, root) or {}
    links = m2.get("links") or []
    assert any(str(it.get("target")) == rid for it in links
               if isinstance(it, dict)), f"卡面没落这条线: {links}"


async def test_role_can_link_out_and_send(work):
    """role 反过来能连自己的关注对象，并沿这条线发消息（旧实现这里必然失败）。"""
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "架构师", "模型生成",
                                     card_type="role")
    rid = r["meta"]["card_id"]
    c = await card_store.create_card(wid, "被关注卡", "模型生成", parent=root)
    cid = c["meta"]["card_id"]

    # role → 关注对象：建线
    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_link_cards(
        wid, rid, {"target_id": cid, "type": "关联", "note": "我盯这张"}, cards)
    assert res.get("ok") is True, f"role 建线失败: {res}"

    # role → 根卡：先建线再发消息（离树卡只能走线）
    cards = card_tools._collect_card_meta(wid)
    res2 = await card_tools._h_link_cards(
        wid, rid, {"target_id": root, "type": "关联", "note": "向主卡汇报用"},
        cards)
    assert res2.get("ok") is True, f"role 连根卡失败: {res2}"

    cards = card_tools._collect_card_meta(wid)
    res3 = await card_tools._h_send_card_message(
        wid, rid,
        {"target_card_id": root, "msg_type": "msg_notify",
         "content": "架构师已就位，开始盯接口口径。"},
        cards, require_link=True)
    assert res3.get("ok") is not False, f"role 发消息失败: {res3}"
    assert res3.get("msg_id"), f"应返回 msg_id: {res3}"


async def test_role_without_link_cannot_link_out(work):
    """反证（对齐真实语义）：**发起方**没有 link 时，建线必须被拒。

    当前语义只查发起方（`_require_wire_switch`）：目标卡不需要 link，
    因为线记录写在发起方卡面，目标卡上没有任何"同意/拒绝"的处置面。
    所以这里剥的是 **role 卡自己**的 link，然后让它去连别人。
    """
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "被剥权角色", "模型生成",
                                     card_type="role")
    rid = r["meta"]["card_id"]
    c = await card_store.create_card(wid, "关注对象", "模型生成", parent=root)
    cid = c["meta"]["card_id"]

    # 剥掉 role 自己的 link（模拟修复前：role 没有 link）
    meta = card_store._meta_of(wid, rid) or {}
    stripped = [p for p in (meta.get("perms") or []) if p != "link"]
    await card_store.update_card(wid, rid, {"perms": stripped}, "测试：剥掉 link")

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_link_cards(
        wid, rid, {"target_id": cid, "type": "关联"}, cards)
    assert res.get("ok") is False, (
        f"没有 link 的卡不该能主动连别人；实得 {res}")
    assert res.get("error", {}).get("code") == "denied", res


async def test_target_card_needs_no_link(work):
    """对齐语义：目标卡**不需要** link —— 剥掉目标卡的 link，建线仍应成功。

    这条锁住「门槛只在发起方」这个契约，防止有人日后又把判定改回
    「查目标卡 perms」——那会让 role/worker/tool/rule 全部变成连不上的死卡。
    """
    wid = work["work_id"]
    root = work["work_card_id"]

    r = await card_store.create_card(wid, "目标卡", "模型生成", card_type="role")
    rid = r["meta"]["card_id"]

    meta = card_store._meta_of(wid, rid) or {}
    stripped = [p for p in (meta.get("perms") or []) if p != "link"]
    await card_store.update_card(wid, rid, {"perms": stripped},
                                 "测试：目标卡剥掉 link")

    cards = card_tools._collect_card_meta(wid)
    res = await card_tools._h_link_cards(
        wid, root, {"target_id": rid, "type": "关联"}, cards)
    assert res.get("ok") is True, (
        f"目标卡没有 link 也应能被人连上（门槛只在发起方）；实得 {res}")
