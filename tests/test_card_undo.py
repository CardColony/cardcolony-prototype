# -*- coding: utf-8 -*-
"""删卡可撤销（回收站）验收。

[画布 2026-10-19] 删卡原来是硬删：行 + 修改历史 + 会话文件一起消失，界面上只有
一句"此操作不可撤销"。卡片正文动辄几千字，误删一次不可挽回。

本文件锁死四条不变量：
1. **恢复必须保留原 card_id** —— 正文里到处是 "见 card_037"，links 里也存着 id，
   换新 id 恢复等于把引用全指向空气；
2. **正文逐字回来**（mission / status_block）；
3. **修改历史逐项回来**（seq / ts / text 三者都不能改，否则等于伪造历史）；
4. **幂等**：重复恢复不报错、不重复插入。
"""
from __future__ import annotations

from src.storage import card_db, card_store


async def _mkcard(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "用户直接创建", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_delete_then_restore_keeps_id_body_and_history(sandbox, work):
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mkcard(wid, "撤销验收卡", root)

    # 写正文 + 两次带 history_note 的修改（产生修改历史）
    await card_store.update_card(wid, cid, {"mission": "正文第一版"}, "第一次改动")
    await card_store.update_card(wid, cid, {"mission": "正文第二版：必须原样回来"}, "第二次改动")

    before = await card_store.read_card(wid, cid)
    before_hist = [(h.get("seq"), h.get("ts"), h.get("text")) for h in before.get("history", [])]
    assert before["body"]["mission"] == "正文第二版：必须原样回来"

    # 删
    r = await card_store.delete_card(wid, cid)
    assert r.get("ok"), r
    assert not card_store.card_exists(wid, cid), "删除后卡必须真的不在了"

    # 撤销
    r = await card_store.restore_card(wid, cid)
    assert r.get("ok") and r.get("restored") == cid, r

    after = await card_store.read_card(wid, cid)
    assert after["meta"]["card_id"] == cid, "恢复必须保留原 card_id（否则引用全断）"
    assert after["body"]["mission"] == "正文第二版：必须原样回来", "正文没回来"

    after_hist = [(h.get("seq"), h.get("ts"), h.get("text")) for h in after.get("history", [])]
    assert after_hist == before_hist, "修改历史不一致（seq/ts/text 有变动）"


async def test_restore_is_idempotent(sandbox, work):
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mkcard(wid, "幂等验收卡", root)
    await card_store.delete_card(wid, cid)

    assert (await card_store.restore_card(wid, cid)).get("ok")
    # 第二次：卡已存在，回收站也已清空 —— 必须幂等返回，不报错
    second = await card_store.restore_card(wid, cid)
    assert second.get("ok") and second.get("already"), second
    assert card_store.card_exists(wid, cid)


async def test_restore_unknown_card_reports_error(sandbox, work):
    wid = work["work_id"]
    r = await card_store.restore_card(wid, "card_999")
    assert "error" in r, r
