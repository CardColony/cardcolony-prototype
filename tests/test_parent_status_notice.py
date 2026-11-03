# -*- coding: utf-8 -*-
"""子卡改现状 ⇒ **不向父卡推任何消息**（现状推送退役）——回归钉。

历史沿革：
- 2026-10-24 用户令：子卡改现状 ⇒ 父卡收件箱摘要信。
- 2026-10-27 用户裁定「inbox 直接取消」：改落会话块。
- **2026-11-01 用户令「更新现状就对上级同步，通知太多太频繁」**：推送式
  告知整体退役——写现状是高频动作，每次变更往父卡会话塞一条 200 字截断
  摘要只会灌满历史。现状本来就是卡面事实：父卡上下文 170 区（直接子卡·
  使命·现状）每回合装配全文当前值，与 edge_notify 同一条形式主义，撤掉。

**判据**：
T1 改现状 ⇒ 父卡会话块里**没有**任何【现状更新】（推一条都不许）
T2 多次改现状 ⇒ 依然零条（高频正是病灶所在）
T3 改别的键 ⇒ 同样零条
T4 变更事实不失联：卡面现状仍可读（read_card 拿当前值）
"""
from __future__ import annotations

import pytest

from src.storage import card_store, session_facade


async def _mk_child(work) -> tuple[str, str, str]:
    """返回 (work_id, 父卡id, 子卡id)。"""
    wid = work["work_id"]
    root = work["work_card_id"]
    child = await card_store.create_card(wid, "子卡", "模型生成", parent=root)
    return wid, root, str(child["meta"]["card_id"])


def _notices(blocks) -> list[dict]:
    """会话块里【现状更新】的那些。"""
    out = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        blob = str(b.get("text") or b.get("content") or "")
        if "【现状更新】" in blob:
            out.append(b)
    return out


@pytest.mark.asyncio
async def test_t1_status_change_pushes_nothing(work):
    """T1：改现状 ⇒ 父卡会话块里**一条**【现状更新】都不许有。"""
    wid, parent, child = await _mk_child(work)

    await card_store.update_card(wid, child, {"status_block": "已核对完需求"},
                                 "测试：改现状")

    blocks = await session_facade.read(wid, parent)
    notices = _notices(blocks)
    assert not notices, f"现状推送该退役了，父卡会话却收到 {notices}"


@pytest.mark.asyncio
async def test_t2_repeated_status_still_silent(work):
    """T2：高频改现状（病灶场景）⇒ 依然零推送。"""
    wid, parent, child = await _mk_child(work)

    for i in range(5):
        await card_store.update_card(
            wid, child, {"status_block": f"第 {i} 版进度"},
            f"测试：第 {i} 次改现状")

    blocks = await session_facade.read(wid, parent)
    notices = _notices(blocks)
    assert not notices, f"改 5 次现状塞了 {len(notices)} 条——病灶未除"


@pytest.mark.asyncio
async def test_t3_other_key_also_silent(work):
    """T3：改别的键 ⇒ 同样零推送（本来就没发过，守住不回潮）。"""
    wid, parent, child = await _mk_child(work)

    await card_store.update_card(wid, child, {"mission": "新的使命"},
                                 "测试：改使命")

    notices = _notices(await session_facade.read(wid, parent))
    assert not notices


@pytest.mark.asyncio
async def test_t4_status_still_on_card_face(work):
    """T4：推送撤了但**事实不缺**——现状仍在卡面上，父卡 170 区读的就是它。"""
    wid, parent, child = await _mk_child(work)

    await card_store.update_card(wid, child, {"status_block": "里程碑 M1 达成"},
                                 "测试：改现状")

    got = await card_store.read_card(wid, child)
    assert "里程碑 M1 达成" in str(
        (got.get("body") or {}).get("status_block") or "")
    # 装配层给父卡的正是这份：直接子卡 status_block
    from src.api import context_assembler as _ca
    kids = _ca._load_direct_child_status(wid, parent)
    mine = [k for k in kids if k["card_id"] == child]
    assert mine and "里程碑 M1 达成" in str(mine[0].get("status_block") or "")
