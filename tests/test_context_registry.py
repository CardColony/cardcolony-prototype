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
        assert s["owner"] == "context", s
        assert s["source_owner"] in ("assembler", "prompt", "runner",
                                     "current_turn/message_runner", "simple_service/message_runner"), s
        for f in ("key", "order", "name", "channel", "seats", "owner",
                  "source_owner", "delivery", "profiles", "source", "render", "count"):
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
        "seats": ("main",), "owner": "context", "source_owner": "assembler",
        "delivery": "system", "profiles": ("main",), "source": "测试",
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
# V6：上下文底座是唯一 owner；source_owner 只保留来源审计
# ---------------------------------------------------------------------------


async def test_context_base_composes_all_registered_channels(sandbox):
    wid, cid = await _make_work()
    source = [
        {"role": "user", "content": "历史输入"},
        {"role": "assistant", "content": "历史回复"},
        {"role": "tool", "content": "留痕过滤", "behavior": "plan"},
    ]
    ctx = context_assembler.build_ctx(wid, cid, history_source=source)
    once = [{"role": "assistant", "content": "once"}]
    tail = [{"role": "assistant", "content": "tail"}]
    result = context_regions.compose_model_context(
        ctx, profile="main", user_content="当前任务", turn_once=once, turn_tail=tail)
    keys = {s["key"] for s in context_regions.REGION_TABLE}
    assert {"instruction", "card_duty", "card_list", "current_card", "region_guide",
            "memory", "history", "tool_turn", "user_message", "judgment_protocol",
            "context_tail", "seat_identity"} <= keys
    assert result["system_prompt"].startswith(context_regions.BASE_PROMPT)
    assert result["user_prompt"] == "当前任务"
    assert result["prior_messages"] == [
        {"role": "user", "content": "历史输入"},
        {"role": "assistant", "content": "历史回复"},
    ]
    assert result["turn_once"] == once
    assert result["turn_tail"] == tail


async def test_viewpoint_profile_uses_registry_without_usage_instructions(sandbox):
    wid, cid = await _make_work()
    ctx = context_assembler.build_ctx(wid, cid)
    result = context_regions.compose_model_context(
        ctx, profile="viewpoint", prior_messages=[], user_content="质询")
    assert result["system_prompt"]
    assert "## 卡片清单" in result["system_prompt"]
    assert "系统使用说明" not in result["system_prompt"]
    assert result["user_prompt"] == "质询"


async def test_context_base_is_only_runtime_owner(sandbox):
    wid, cid = await _make_work()
    ctx = context_assembler.build_ctx(wid, cid)
    assert {s["owner"] for s in context_regions.REGION_TABLE} == {"context"}
    result = context_regions.compose_model_context(
        ctx, profile="main", user_content="u", prior_messages=[])
    assert result["system_prompt"].startswith(context_regions.BASE_PROMPT)
    assert result["user_prompt"] == "u"
    assert result["turn_once"] == [] and result["turn_tail"] == []


async def test_legacy_system_region_view_is_registry_derived(sandbox):
    wid, cid = await _make_work()
    regs = context_assembler.build_context_regions(wid, cid)
    source_orders = {s["order"] for s in _TABLE if s["source_owner"] == "assembler"}
    for r in regs:
        assert r["order"] in source_orders, f"不属于 assembler source 的兼容视图条目：{r}"
    assert 0 not in [r["order"] for r in regs]
    assert 300 not in [r["order"] for r in regs]


# ---------------------------------------------------------------------------
# V7：日志也走注册表——**注册表是区的唯一声明处，日志不得另行硬编码**
#
# 隐患：日志若自己写死 order/名称/通道/席位，改了 REGION_TABLE 只会改上下文、
# 不会改日志，两边静默分叉（审计凭据说谎）。三条钉死：
#   源码级  ＝ 函数体里没有任何区名字面量、没有 0/300/900/999 顺序字面量
#   行为级  ＝ 改注册表 → 日志条目跟着改（改名/改序/改席位/改通道）
#   降级级  ＝ 注册项被删 → 降级保审计，不抛异常、不丢条目
# ---------------------------------------------------------------------------


def _func_tree_without_docstring(fn):
    """函数源码树，剥掉 docstring——否则文档里写的区名会被误判成硬编码。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    fnode = tree.body[0]
    first = fnode.body[0] if fnode.body else None
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        fnode.body = fnode.body[1:]
    return tree


def test_log_regions_has_no_hardcoded_zone_metadata():
    import ast

    tree = _func_tree_without_docstring(context_regions.build_log_regions)
    consts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    strs = [v for v in consts if isinstance(v, str)]
    ints = [v for v in consts if isinstance(v, int) and not isinstance(v, bool)]
    for name in ("使用说明", "对话历史", "本轮工具轮", "用户消息"):
        assert name not in strs, f"build_log_regions 仍硬编码区名：{name}"
    for order in (0, 300, 900, 999):
        assert order not in ints, f"build_log_regions 仍硬编码 order：{order}"


def test_log_regions_entry_shape_and_order_unchanged():
    """改前口径逐字不变：0/300/900/999 顺序、七键形状、区名。"""
    log = context_regions.build_log_regions(
        [], [{"role": "user", "content": "hi"}], [], "本轮提问")
    assert [e["order"] for e in log] == [0, 300, 900, 999]
    assert [e["name"] for e in log] == ["使用说明", "对话历史", "本轮工具轮", "用户消息"]
    assert [e["channel"] for e in log] == ["system", "messages", "messages", "user"]
    for e in log:
        assert set(e) == {"order", "name", "channel", "seats",
                          "bytes", "fingerprint", "entries"}
    assert log[-1]["entries"] == 1 and log[1]["entries"] == 1
    assert log[0]["bytes"] == len(context_regions.BASE_PROMPT.encode("utf-8"))


async def test_log_regions_metadata_follows_registry(sandbox, monkeypatch):
    """改注册表 → 日志跟着改（防上下文与日志静默分叉）。"""
    wid, cid = await _make_work()
    sysregs = context_assembler.build_context_regions(wid, cid)
    patched = [dict(s) for s in _TABLE]
    for s in patched:
        if s["key"] == "history":
            s["order"], s["name"] = 250, "历史投影"
            s["channel"], s["seats"] = "messages", ("main",)
    monkeypatch.setattr(context_regions, "REGION_TABLE", patched)

    log = context_regions.build_log_regions(
        sysregs, [{"role": "user", "content": "hi"}], [], "本轮提问")
    by_name = {e["name"]: e for e in log}
    assert "历史投影" in by_name, "history 的名称没跟注册表走"
    h = by_name["历史投影"]
    assert h["order"] == 250 and h["channel"] == "messages" and h["seats"] == ["main"]
    # 顺序也跟着走：250 落在 system 区 155 之后、tool_turn 900 之前
    orders = [e["order"] for e in log]
    assert orders == sorted(orders)
    assert orders.index(250) > orders.index(155)
    assert orders.index(250) < orders.index(900)


def test_log_regions_degrades_when_spec_missing(monkeypatch):
    """注册项被删 → 降级保审计（name 标未注册、order=-1），不抛、不丢条目。"""
    monkeypatch.setattr(context_regions, "REGION_TABLE",
                        [s for s in _TABLE if s["key"] != "history"])
    log = context_regions.build_log_regions([], [], [], "本轮提问")
    names = [e["name"] for e in log]
    assert len(log) == 4, "声明缺失也不许丢条目"
    assert "[未注册:history]" in names
    lost = next(e for e in log if e["name"] == "[未注册:history]")
    assert lost["order"] == -1 and lost["bytes"] == 2  # json.dumps([]) = "[]"
