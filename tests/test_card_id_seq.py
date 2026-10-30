# -*- coding: utf-8 -*-
"""[编号脱节修复 2026-10-25] 发号必须与**实际最大 card 编号**对齐。

事故：`work_027` 的 `meta.seq = 52`，而库里最大已是 `card_055`。
下一次建子卡发 `card_053` ⇒ 撞上已存在的卡：
``[internal] UNIQUE constraint failed: cards.card_id``。

根因：`card_id` 有两条生成路径——正常发号 `card_{seq}` 与**强制指定**
（`create_card(card_id=…)`，用于复活归档卡、建职能卡 fn 号段）。强制路径
不让 seq 跟着 id 走，seq 只按「建了几张」前进 ⇒ 一旦某张强制 id 用了比 seq
大的号，seq 就落后，此后正常发号必然撞。

本文件钉死：发号取 ``max(meta.seq, 实际最大) + 1``，两条路径不再脱节。
"""
from __future__ import annotations

import pytest

from src.storage import card_db, card_store


async def _set_seq(work_id: str, value: int) -> None:
    """改 meta.seq（写事务——read_sync 是只读连接，写会报 readonly）。"""
    def _fn(conn):
        card_db.set_meta_value(conn, "seq", str(value))

    await card_db.run_write(work_id, _fn)


async def test_next_seq_is_max_of_meta_and_actual(work):
    """seq 落后于实际最大号时，以实际最大号兜底（不再发到已存在的号上）。"""
    wid = work["work_id"]
    # 现有 card_001，故意把 seq 压到 0
    await _set_seq(wid, 0)

    nxt = await card_db.run_read(wid, lambda c: card_db.next_seq(c), 0)
    assert nxt == 2, f"应发 2（实际最大 1 + 1），实得 {nxt}"


async def test_next_seq_keeps_forward_when_seq_ahead(work):
    """seq 超前时用 seq（不因实际最大号小而回退，避免复用已发放的号）。"""
    wid = work["work_id"]
    await _set_seq(wid, 99)

    nxt = await card_db.run_read(wid, lambda c: card_db.next_seq(c), 0)
    assert nxt == 100


async def test_create_card_after_seq_lag_does_not_collide(work):
    """核心回归：seq 落后 → 建卡不撞 UNIQUE。"""
    wid, root = work["work_id"], work["work_card_id"]
    await _set_seq(wid, 0)

    r = await card_store.create_card(wid, "落后后建卡", "模型生成", parent=root)
    assert "error" not in r, r
    # 必须是新号，不能复用 card_001
    assert r["meta"]["card_id"] != root


async def test_seq_advances_with_forced_id(work):
    """强制指定 id 时 seq 仍前进（不会因为不走发号就停住）。"""
    wid, root = work["work_id"], work["work_card_id"]
    before = await card_db.run_read(wid, lambda c: int(card_db.get_meta_value(c, "seq", "0")), 0)

    r = await card_store.create_card(
        wid, "强制号", "模型生成", parent=root, card_id="card_900")
    assert "error" not in r, r

    after = await card_db.run_read(wid, lambda c: int(card_db.get_meta_value(c, "seq", "0")), 0)
    assert after > before


async def test_fn_ids_do_not_disturb_card_seq(work):
    """fn_NNN 不参与 card 号段的 MAX 计算。"""
    wid, root = work["work_id"], work["work_card_id"]
    await card_store.create_card(
        wid, "职能", "系统派生", parent=root, card_id="fn_900")

    nxt = await card_db.run_read(wid, lambda c: card_db.next_seq(c), 0)
    # 只有 card_001 计入；fn_900 不该把号段顶到 901
    assert nxt < 100, f"fn 号段串进 card 号段了：{nxt}"
