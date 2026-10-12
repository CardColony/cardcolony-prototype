# -*- coding: utf-8 -*-
"""card_045 E6 · 后端写路径深合并 验收。

判据（任务书 5.1 #2）：发一条只带标记的载荷 → 横纵坐标仍在（局部更新不抹其他字段）。
配套：全量替换仍可行（显式给全字段＝写全字段，语义不变）。

[画布第二步 D2 同步] merge_positions 改为只读写 layout.json（薄委托 save_layout）
——种子与回读全部迁到 layout 通道；深合并断言（E6 成果的防线）逐字保留。
并新增拆存储核心断言：merge_positions **绝不触碰 canvas.json**（不读不写不建）。
"""
from __future__ import annotations

from src.storage import canvas_store


def test_deep_merge_keeps_missing_fields(sandbox):
    wid = "work_e6"
    # 种子改走 layout 通道（D2 前＝直写 canvas.json；D2 后 merge 不再读它）
    canvas_store.save_layout(wid, {"card_010": {"x": 341.091, "y": 294.455, "pinned": True}})

    # 「整理画布」形态的载荷：只带标记、不带坐标
    canvas_store.merge_positions(wid, {"card_010": {"pinned": False}})
    rec = canvas_store.load_layout(wid)["positions"]["card_010"]
    assert rec["x"] == 341.091 and rec["y"] == 294.455, "深合并必须保留缺失的坐标字段"
    assert rec["pinned"] is False

    # 显式给全字段＝写全字段（既有语义不回归）
    canvas_store.merge_positions(wid, {"card_016": {"x": 352.0, "y": -14.0, "pinned": True}})
    positions = canvas_store.load_layout(wid)["positions"]
    assert positions["card_016"] == {"x": 352.0, "y": -14.0, "pinned": True}
    assert positions["card_010"]["x"] == 341.091  # 他卡不受影响

    # 新卡部分字段：基线为空 dict，只落给的字段
    canvas_store.merge_positions(wid, {"card_019": {"pinned": True}})
    assert canvas_store.load_layout(wid)["positions"]["card_019"] == {"pinned": True}

    # [D2 核心断言] merge_positions 绝不触碰 canvas.json：不读（种子只在 layout）、
    # 不写、不建——用户数据与派生缓存分家（不变量 L4）。
    assert not canvas_store._canvas_path(wid).exists(), \
        "merge_positions 不得创建/写 canvas.json"
