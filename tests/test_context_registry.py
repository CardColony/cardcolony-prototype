# -*- coding: utf-8 -*-
"""上下文底座化 · 回归网（2026-10-15）。

依据 `docs/上下文底座化-方案-20261015.md`。底座＝三件事：
1. **区＝注册项**（`context_regions.REGION_TABLE` 是**唯一声明处**，每项带
   key/order/name/channel/seats/owner/source/render/count）；
2. **装配＝遍历机**（`build_context_regions` 只按 order 收区、渲染零加工、
   空串不出现——**不再认识任何具体区**）；
3. **控制面**（`config/context_regions.json` 可启停/调序，**fail-open**）。

铁律一：沙箱内跑，不碰真实 works/。
"""
from __future__ import annotations

import json

from src.api import context_assembler, context_regions
from src.storage import card_store, work_store

_TABLE = context_regions.REGION_TABLE


async def _make_work():
    wdata = await work_store.create_work("上下文底座剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    return wid, "card_001"


# ---------------------------------------------------------------------------
# V1：注册表形状（key 唯一 / owner 合法 / 必填字段齐）
# ---------------------------------------------------------------------------


def test_registry_shape():
    keys = [s["key"] for s in _TABLE]
    assert len(keys) == len(set(keys)), "key 必须唯一"
    orders = [s["order"] for s in _TABLE]
    assert len(orders) == len(set(orders)), "order 必须唯一"
    for s in _TABLE:
        assert s["owner"] in ("assembler", "prompt", "runner"), s
        for f in ("key", "order", "name", "channel", "seats", "source", "render", "count"):
            assert f in s, f"缺字段 {f}: {s['key']}"
        assert callable(s["render"])


# ---------------------------------------------------------------------------
# V2：装配器不再认识任何具体区（源码级）
# ---------------------------------------------------------------------------


def test_assembler_knows_no_concrete_region():
    import inspect
    src = inspect.getsource(context_assembler)
    for fn in ("render_card_duty_region", "render_card_list_region",
               "render_current_card_region", "render_card_region_guide",
               "render_memory_region"):
        assert fn not in src, f"装配器仍在直接调用具体区渲染器：{fn}"


# ---------------------------------------------------------------------------
# V3：加一个区＝只改注册表（装配自动多一区）
# ---------------------------------------------------------------------------


async def test_add_region_by_registry_only(sandbox, monkeypatch):
    wid, cid = await _make_work()
    before = [r["order"] for r in context_assembler.build_context_regions(wid, cid)]
    extra = {
        "key": "probe", "order": 777, "name": "探针区", "channel": "system",
        "seats": ("main",), "owner": "assembler", "source": "测试",
        "render": lambda ctx: "## 探针区\nprobe", "count": 1,
    }
    monkeypatch.setattr(context_regions, "REGION_TABLE", _TABLE + [extra])
    after = context_assembler.build_context_regions(wid, cid)
    orders = [r["order"] for r in after]
    print("BEFORE:", before, "AFTER:", orders)
    assert 777 in orders
    assert orders == sorted(orders), "仍按 order 升序"
    assert next(r for r in after if r["order"] == 777)["name"] == "探针区"


# ---------------------------------------------------------------------------
# V4：控制面（启停 / 调序 / fail-open）
# ---------------------------------------------------------------------------


async def test_control_disable_and_reorder(sandbox, monkeypatch, tmp_path):
    wid, cid = await _make_work()
    cfg = tmp_path / "ctx.json"
    cfg.write_text(json.dumps({"regions": {
        "card_duty": {"enabled": False},
        "card_list": {"order": 999},        # 调到最末
    }}), encoding="utf-8")
    monkeypatch.setattr(context_assembler, "_control_path", lambda: cfg)
    regs = context_assembler.build_context_regions(wid, cid)
    orders = [r["order"] for r in regs]
    print("ORDERS:", orders)
    assert 90 not in orders, "card_duty 已停用"
    assert 999 in orders, "card_list 已调序"
    assert orders == sorted(orders)


async def test_control_fail_open(sandbox, monkeypatch, tmp_path):
    """坏配置/缺文件 → 回落注册表默认（不报错、不阻断）。"""
    wid, cid = await _make_work()
    for bad in ("{不是JSON", ""):
        cfg = tmp_path / "bad.json"
        cfg.write_text(bad, encoding="utf-8")
        monkeypatch.setattr(context_assembler, "_control_path", lambda: cfg)
        regs = context_assembler.build_context_regions(wid, cid)
        assert [r["order"] for r in regs] == [90, 100, 150, 155], bad
    # 文件不存在
    monkeypatch.setattr(context_assembler, "_control_path", lambda: tmp_path / "nope.json")
    regs = context_assembler.build_context_regions(wid, cid)
    assert [r["order"] for r in regs] == [90, 100, 150, 155]


# ---------------------------------------------------------------------------
# V5：渲染器仍全部纯函数（context_regions 不做 IO）
# ---------------------------------------------------------------------------


def test_context_regions_stays_pure():
    import inspect
    src = inspect.getsource(context_regions)
    for bad in ("read_text(", "load_type_registry(", "load_memory(", "card_db."):
        assert bad not in src, f"context_regions 出现 IO 迹象：{bad}"


# ---------------------------------------------------------------------------
# V6：owner 语义——装配器只收 assembler 的区
# ---------------------------------------------------------------------------


async def test_only_assembler_owned_regions_rendered(sandbox):
    wid, cid = await _make_work()
    regs = context_assembler.build_context_regions(wid, cid)
    assembler_orders = {s["order"] for s in _TABLE if s["owner"] == "assembler"}
    for r in regs:
        assert r["order"] in assembler_orders, f"渲染了非 assembler 区：{r}"
    # prompt / runner 归属的区不在本装配器输出里
    assert 0 not in [r["order"] for r in regs]
    assert 300 not in [r["order"] for r in regs]
