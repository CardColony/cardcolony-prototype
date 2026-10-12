# -*- coding: utf-8 -*-
"""画布第二步 D1 · layout.json 读写能力 验收（四条，任务书 5.1 #1–#4）。

1. load_layout：文件不存在 → 返回 {} 不抛
2. load_layout：内容损坏 → 返回 {} 不抛
3. save_layout：深合并——发 {pinned:false} 后 x/y 仍在（E6 语义随迁，RD-04）
4. layout.json 含 version: 1
"""
from __future__ import annotations

import json

from src.storage import canvas_store


def test_load_layout_missing_returns_empty(sandbox):
    assert canvas_store.load_layout("work_d1_missing") == {}


def test_load_layout_corrupt_returns_empty_no_raise(sandbox):
    p = canvas_store._layout_path("work_d1_corrupt")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not json !!!", encoding="utf-8")
    assert canvas_store.load_layout("work_d1_corrupt") == {}
    # 形态非法（非 dict / 缺 positions）同样按空处理
    p.write_text('{"version": 1, "no_positions": 1}', encoding="utf-8")
    assert canvas_store.load_layout("work_d1_corrupt") == {}


def test_save_layout_deep_merge_keeps_fields(sandbox):
    wid = "work_d1_merge"
    # 首写：全量三字段
    canvas_store.save_layout(wid, {"card_010": {"x": 341.091, "y": 294.455, "pinned": True}})
    # 深合并：只带标记、不带坐标（「整理画布」载荷形态）
    canvas_store.save_layout(wid, {"card_010": {"pinned": False}})
    rec = canvas_store.load_layout(wid)["positions"]["card_010"]
    assert rec["x"] == 341.091 and rec["y"] == 294.455, "深合并必须保留缺失字段"
    assert rec["pinned"] is False


def test_layout_json_has_version(sandbox):
    wid = "work_d1_version"
    doc = canvas_store.save_layout(wid, {"card_001": {"x": 1.0, "y": 2.0, "pinned": True}})
    assert doc["version"] == 1
    on_disk = json.loads(canvas_store._layout_path(wid).read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
