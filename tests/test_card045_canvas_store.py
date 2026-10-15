# -*- coding: utf-8 -*-
"""card_045 E6 · 后端写路径深合并 验收（坐标存储换 cards 表后逐字保留的防线）。

判据（任务书 5.1 #2）：发一条只带标记的载荷 → 横纵坐标仍在（局部更新不抹其他
字段）。配套：全量替换仍可行（显式给全字段＝写全字段，语义不变）。

[画布重构 2026-10-15] merge_positions 的存储从 ``layout.json`` 换成 **cards 表
坐标列**——种子与回读全部走 cards 通道；深合并断言（E6 成果的防线）逐字保留，
并新增两条随存储换代而必须成立的判据：
- **只 UPDATE 不 INSERT**：merge_positions 绝不往 cards 表塞新行（位置是卡的
  属性，不是可以独立存在的记录）；
- **绝不触碰 canvas.json**：不读、不写、不建——用户数据与派生缓存分家
  （不变量 L4）。
"""
from __future__ import annotations

import json

from src.storage import card_db, card_store, canvas_store


async def _mkcard(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "用户直接创建", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_deep_merge_keeps_missing_fields(sandbox, work):
    wid, root = work["work_id"], work["work_card_id"]
    # 种子走 cards 通道（布局坐标存在卡自己的行上）
    c10, c16, c19 = [await _mkcard(wid, f"E6卡{i}", root) for i in (10, 16, 19)]
    await canvas_store.merge_positions(wid, {c10: {"x": 341.091, "y": 294.455, "pinned": True}})

    # 「整理画布」形态的载荷：只带标记、不带坐标
    await canvas_store.merge_positions(wid, {c10: {"pinned": False}})
    rec = (await canvas_store.load_positions(wid))[c10]
    assert rec["x"] == 341.091 and rec["y"] == 294.455, "深合并必须保留缺失的坐标字段"
    assert rec["pinned"] is False

    # 显式给全字段＝写全字段（既有语义不回归）
    await canvas_store.merge_positions(wid, {c16: {"x": 352.0, "y": -14.0, "pinned": True}})
    positions = await canvas_store.load_positions(wid)
    assert positions[c16] == {"x": 352.0, "y": -14.0, "pinned": True}
    assert positions[c10]["x"] == 341.091  # 他卡不受影响

    # 新卡部分字段：基线是「没有坐标」，只落给的字段
    await canvas_store.merge_positions(wid, {c19: {"pinned": True}})
    assert (await canvas_store.load_positions(wid))[c19] == {
        "x": None, "y": None, "pinned": True,
    }, "只钉不摆的卡：坐标为空、钉住为真"

    # [核心断言] merge_positions 只 UPDATE，绝不 INSERT：cards 表行数不因写坐标而变
    def _count(conn):
        return conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]

    before = await card_db.run_read(wid, _count, 0)
    await canvas_store.merge_positions(wid, {"card_999": {"x": 1.0, "y": 2.0, "pinned": True}})
    assert await card_db.run_read(wid, _count, 0) == before, "写坐标绝不允许凭空造卡"

    # [核心断言] merge_positions 绝不触碰 canvas.json：不读、不写、不建——用户数据
    # 与派生缓存分家（不变量 L4）。
    # 判据取「字节与 mtime 都不许变」而不是「文件不许存在」：建卡路径本身会重建
    # canvas.json（那是派生缓存该干的事），「写坐标这件事不许碰它」才是本条的真意。
    cpath = canvas_store._canvas_path(wid)
    snap = (cpath.read_bytes(), cpath.stat().st_mtime_ns) if cpath.exists() else None
    await canvas_store.merge_positions(wid, {c10: {"x": 1.0, "y": 2.0, "pinned": True}})
    after = (cpath.read_bytes(), cpath.stat().st_mtime_ns) if cpath.exists() else None
    assert after == snap, "merge_positions 不得读/写/建 canvas.json"
    # 并且 canvas.json 里从此不该再有 positions 键（存量残留已另行清理）
    if snap is not None:
        assert "positions" not in json.loads(snap[0].decode("utf-8")), \
            "canvas.json 是纯派生缓存，不许再挂 positions"
