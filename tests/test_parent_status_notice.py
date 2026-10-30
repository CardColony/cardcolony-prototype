# -*- coding: utf-8 -*-
"""子卡改现状 ⇒ 父卡收件箱摘要信（现状告知）——2026-10-25 回归钉。

**故障（实测 2026-10-25 沙箱跑 wb-oc 验收时抓到的）**：子卡改 ``status_block``
后，父卡**收不到**任何摘要信；日志里只有一句
``[status] 父卡告知失败 ...: name 'body_patch' is not defined``。

**根因（两个，都致命）**：
  1. **作用域错**：`body_patch` 是在写事务的回调 ``_fn`` **里面**定义的局部变量，
     函数外根本取不到 ⇒ 每次都 NameError ⇒ 被 ``except Exception`` 吞掉 ⇒
     摘要信**一次都没发出去过**（整个功能是死的）。
  2. **旧/新值取反**：就算不报错也没用——``body_patch["status_block"]`` 是
     **新值**（= updates 里那个），拿它当"旧值"跟新值比 ⇒ **恒等**
     ⇒ 永远走不到发信那一行。

**判据**：
T1 改现状 ⇒ 父卡收件箱里**真有一条**摘要信（内容含新现状）
T2 现状**没变** ⇒ 不发信（去重）
T3 摘要信只进父卡收件箱，**不唤醒**父卡
T4 不改现状（改别的键）⇒ 不发信
"""
from __future__ import annotations

import pytest

from src.storage import card_db, card_store


async def _mk_child(work) -> tuple[str, str, str]:
    """返回 (work_id, 父卡id, 子卡id)。"""
    wid = work["work_id"]
    root = work["work_card_id"]
    child = await card_store.create_card(wid, "子卡", "模型生成", parent=root)
    return wid, root, str(child["meta"]["card_id"])


@pytest.mark.asyncio
async def test_t1_status_change_notifies_parent(work):
    """T1：改现状 ⇒ 父卡收件箱真有一条摘要信。"""
    wid, parent, child = await _mk_child(work)

    await card_store.update_card(wid, child, {"status_block": "已核对完需求"},
                                 "测试：改现状")

    rows = await card_db.list_inbox(wid, parent)
    notices = [r for r in rows if "【现状更新】" in str(r.get("content") or "")]
    assert notices, f"父卡没收到摘要信（收件箱={rows}）"
    assert "已核对完需求" in str(notices[-1]["content"])
    assert str(notices[-1].get("from_card")) == child


@pytest.mark.asyncio
async def test_t2_same_status_no_duplicate(work):
    """T2：现状没变 ⇒ 不发信（去重）。"""
    wid, parent, child = await _mk_child(work)

    await card_store.update_card(wid, child, {"status_block": "第一版"},
                                 "测试：改现状")
    before = len([r for r in await card_db.list_inbox(wid, parent)
                  if "【现状更新】" in str(r.get("content") or "")])

    # 再写一次**同一个值** ⇒ 旧值==新值 ⇒ 不该再发
    await card_store.update_card(wid, child, {"status_block": "第一版"},
                                 "测试：改现状（同值）")
    after = len([r for r in await card_db.list_inbox(wid, parent)
                 if "【现状更新】" in str(r.get("content") or "")])
    assert after == before, "现状没变却重复发信了"


@pytest.mark.asyncio
async def test_t4_other_key_no_notice(work):
    """T4：只改别的键（不动 status_block）⇒ 不发信。"""
    wid, parent, child = await _mk_child(work)

    await card_store.update_card(wid, child, {"mission": "新的使命"},
                                 "测试：改使命")

    rows = await card_db.list_inbox(wid, parent)
    notices = [r for r in rows if "【现状更新】" in str(r.get("content") or "")]
    assert not notices, f"没改现状却发了摘要信（{notices}）"
