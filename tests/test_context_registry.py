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

import pytest

import json

from src.api import context_assembler, context_regions
from src.storage import card_store, work_store

# ===========================================================================
# [纯单模型模式 2026-10-17 · 用户令] 本模块用例全为思维链行为（判型/拆解/上板/
# 步推进/三席统合/步事件）。生产已把思维链拆出链路（thinkchain.ENABLED=False）
# ⇒ 整模块跳过；开关置 True 即全部自动恢复（判据同并行线逐函数 skipif，此处
# 收敛为模块级，语义不变）。
# ===========================================================================
pytestmark = pytest.mark.skipif(
    not __import__("src.thinkchain", fromlist=["enabled"]).enabled(),
    reason="纯单模型模式（2026-10-17 用户令）：思维链已拆出生产链路",
)

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


# ---------------------------------------------------------------------------
# V8：父卡展示子卡现状——**只看直接子级，孙级结构上取不到**
# ---------------------------------------------------------------------------


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def test_direct_child_status_shows_direct_children_only(sandbox):
    wid, root = await _make_work()
    with_status = await _mk_child(wid, "子卡有现状", root)
    empty = await _mk_child(wid, "子卡无现状", root)
    grand = await _mk_child(wid, "孙卡有现状", with_status)
    await card_store.update_card(wid, with_status, {"status_block": "子卡现状正文"}, "写")
    await card_store.update_card(wid, grand, {"status_block": "孙卡现状正文"}, "写")

    ctx = context_assembler.build_ctx(wid, root)
    region = context_regions.render_direct_child_status_region(ctx.direct_child_status)
    print("REGION:", region)
    assert "子卡现状正文" in region
    assert "孙卡现状正文" not in region, "孙级现状不得进入父卡上下文"
    assert "孙卡有现状" not in region, "孙级标题也不得出现"
    # [2026-11-02 用户令「父卡需要看到子卡的实时状态」] 空现状的子卡不再不占位：
    # 活性行（运行中/待推进/已暂停…）恒在，父卡要看的就是它活没活着。
    assert f"### {empty} · 子卡无现状" in region, "空现状的子卡也该露面——活性行是它的价值"
    assert "**活性**：" in region
    assert region.startswith("## 直接子卡（活性·使命·现状）")


async def test_grandchild_context_shows_its_own_children(sandbox):
    """孙卡自己当父卡时，看的是它的直接子级（不是把祖卡的规则改掉）。"""
    wid, root = await _make_work()
    mid = await _mk_child(wid, "中间卡", root)
    leaf = await _mk_child(wid, "末级卡", mid)
    await card_store.update_card(wid, leaf, {"status_block": "末级现状"}, "写")
    await card_store.update_card(wid, mid, {"status_block": "中间现状"}, "写")

    ctx = context_assembler.build_ctx(wid, mid)
    region = context_regions.render_direct_child_status_region(ctx.direct_child_status)
    assert "末级现状" in region
    assert "中间现状" not in region, "自己的现状不属于「子卡现状」"


async def test_direct_child_status_region_absent_when_no_children(sandbox):
    """[2026-11-02 活性行入区后] 判据更新：「两块都空」不再让区消失——
    活性行恒在（运行中/待推进/已暂停…），父卡要靠它看子卡活没活着。
    现在整个区不出现的唯一条件是**没有直接子卡**（叶子卡那一条另测）。"""
    wid, root = await _make_work()
    await _mk_child(wid, "子卡甲", root)
    await _mk_child(wid, "子卡乙", root)
    ctx = context_assembler.build_ctx(wid, root)
    region = context_regions.render_direct_child_status_region(ctx.direct_child_status)
    assert "**活性**：" in region, "两块都空也露面——活性行正是给父卡看的"
    orders = [r["order"] for r in context_assembler.build_context_regions(wid, root)]
    assert 170 in orders


async def test_direct_child_status_region_present_in_context(sandbox):
    wid, root = await _make_work()
    child = await _mk_child(wid, "子卡甲", root)
    await card_store.update_card(wid, child, {"status_block": "子卡现状"}, "写")
    regs = context_assembler.build_context_regions(wid, root)
    hit = next((r for r in regs if r["order"] == 170), None)
    assert hit is not None and "子卡现状" in hit["text"]
    assert hit["entries"] == 1
    assert hit["channel"] == "system"


async def test_leaf_card_has_no_child_status_region(sandbox):
    """叶子卡（无子卡）不出现该区——「没有来源就不出现」。"""
    wid, root = await _make_work()
    leaf = await _mk_child(wid, "叶子卡", root)
    orders = [r["order"] for r in context_assembler.build_context_regions(wid, leaf)]
    assert 170 not in orders


# ---------------------------------------------------------------------------
# V9（卡片思维区 2026-10-15）：按卡类型挂的文本——只出本类、无字不出现、fail-open
#
# 依据 `docs/卡片思维区-方案-20261015.md`（v2）：配置＝`config/type_texts.json`
# （多维度、**不列类型全表**）；区 key=`type_thinking`、order 95；取数一处 IO
# 落在 `build_ctx`（`load_type_texts`），渲染器保持纯函数。
# 类型清单唯一来源＝类型注册表——本套用例不含任何"类型全表"。
# ---------------------------------------------------------------------------


def _write_type_texts(tmp_path, data):
    p = tmp_path / "type_texts.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


async def _make_work_with_child():
    """根卡（card_001, project_class=rd_project）＋ 一个子项目卡（sub_project）。"""
    wid, cid = await _make_work()
    child = await card_store.create_card(wid, "子项目卡", "用户直接创建", parent=cid)
    assert "error" not in child, child
    return wid, cid, child["meta"]["card_id"]


async def test_v1_type_thinking_region_present_for_current_type(sandbox, monkeypatch, tmp_path):
    """V1：某类有字 ⇒ 该区出现，且只有本类那段（集成）。"""
    wid, _root, sub_id = await _make_work_with_child()
    p = _write_type_texts(
        tmp_path, {"thinking": {"sub_project": "先想清边界与交付物再动手。"}})
    monkeypatch.setattr(context_assembler, "_type_texts_path", lambda: p)
    regs = context_assembler.build_context_regions(wid, sub_id)
    orders = [r["order"] for r in regs]
    assert 95 in orders and 90 in orders
    hit = next(r for r in regs if r["order"] == 95)
    assert hit["name"] == "卡片思维" and hit["channel"] == "system"
    assert hit["text"] == "## 卡片思维\n先想清边界与交付物再动手。"
    assert hit["entries"] == 1  # 有字＝1


def test_v2_type_thinking_only_current_type():
    """V2：别类不串——当前卡是 A 类 ⇒ B 类文本不出现（单测）。"""
    texts = {"thinking": {"sub_project": "子项目思维", "tool": "工具思维"}}
    out = context_regions.render_type_thinking_region("sub_project", texts)
    assert "子项目思维" in out
    assert "工具思维" not in out
    rev = context_regions.render_type_thinking_region("tool", texts)
    assert "工具思维" in rev and "子项目思维" not in rev


def test_v3_type_thinking_absent_when_no_text():
    """V3：本类无字 / 该类型不存在 ⇒ 该区不出现（不报错）（单测）。"""
    texts = {"thinking": {"sub_project": "有字"}}
    assert context_regions.render_type_thinking_region("topic", texts) == ""      # 类型不存在
    assert context_regions.render_type_thinking_region("", texts) == ""           # 无 card_type
    assert context_regions.render_type_thinking_region("sub_project", {}) == ""   # 无该维度
    assert context_regions.render_type_thinking_region(
        "sub_project", {"thinking": {"sub_project": "   "}}) == ""                # 纯空白


async def test_v3_type_thinking_region_absent_in_context(sandbox, monkeypatch, tmp_path):
    """集成：本卡类型没配 ⇒ 整个区不出现（不是空区）。"""
    wid, _root, sub_id = await _make_work_with_child()
    p = _write_type_texts(tmp_path, {"thinking": {"tool": "别的类有字"}})
    monkeypatch.setattr(context_assembler, "_type_texts_path", lambda: p)
    regs = context_assembler.build_context_regions(wid, sub_id)
    assert 95 not in [r["order"] for r in regs]


async def test_v4_new_registry_type_without_text_is_silent(sandbox, monkeypatch, tmp_path):
    """V4：注册表加一个类型、type_texts 里没有 ⇒ 装配正常、区不出现（不报错）。"""
    wid, cid = await _make_work()
    # 机制侧只动注册表：rd_project 词表加一个新类型
    reg = json.loads(card_store.TYPE_REGISTRY_PATH.read_text(encoding="utf-8"))
    reg["l1_vocabularies"]["rd_project"]["types"].append({
        "code": "probe_kind", "label": "探针卡", "description": "扩展性探针",
        "can_have_children": False, "requires_owner": False,
        "default_template": "sub_project", "allowed_transitions": ["active", "completed"],
    })
    rp = tmp_path / "reg_extended.json"
    rp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(card_store, "TYPE_REGISTRY_PATH", rp)

    child = await card_store.create_card(
        wid, "探针卡", "用户直接创建", card_type="probe_kind", parent=cid)
    assert "error" not in child, child
    probe_id = child["meta"]["card_id"]

    # type_texts 里没有 probe_kind（只配了别类）⇒ 装配正常、思维区不出现
    p = _write_type_texts(tmp_path, {"thinking": {"sub_project": "别的类有字"}})
    monkeypatch.setattr(context_assembler, "_type_texts_path", lambda: p)
    regs = context_assembler.build_context_regions(wid, probe_id)
    orders = [r["order"] for r in regs]
    assert 95 not in orders, "未配置的新类型不得出现思维区"
    assert 90 in orders, "装配照常（卡片职责仍在）"


def test_v5_load_type_texts_fail_open(monkeypatch, tmp_path):
    """V5：坏配置 / 缺文件 / 结构不对 ⇒ 视为全空、不阻断（fail-open）（单测）。"""
    for bad in ("{不是JSON", ""):          # 坏 JSON / 空文件
        p = tmp_path / "bad.json"
        p.write_text(bad, encoding="utf-8")
        monkeypatch.setattr(context_assembler, "_type_texts_path", lambda p=p: p)
        assert context_assembler.load_type_texts() == {}
    monkeypatch.setattr(context_assembler, "_type_texts_path",
                        lambda: tmp_path / "nope.json")  # 文件不存在
    assert context_assembler.load_type_texts() == {}
    for wrong in ("[1, 2, 3]", '{"thinking": "不是字典"}'):  # 结构不对
        p = tmp_path / "wrong.json"
        p.write_text(wrong, encoding="utf-8")
        monkeypatch.setattr(context_assembler, "_type_texts_path", lambda p=p: p)
        assert context_assembler.load_type_texts() == {}


def test_v5_load_type_texts_keeps_only_nonempty(monkeypatch, tmp_path):
    """只留非空字符串条目；`_meta` 等下划线键不是维度。"""
    p = tmp_path / "tt.json"
    p.write_text(json.dumps({
        "_meta": {"version": "x"},
        "thinking": {"sub_project": "有字", "tool": "", "worker": "   ", "topic": "T"},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(context_assembler, "_type_texts_path", lambda: p)
    got = context_assembler.load_type_texts()
    assert got == {"thinking": {"sub_project": "有字", "topic": "T"}}
    assert "_meta" not in got


async def test_v7_type_thinking_control_disable_and_reorder(sandbox, monkeypatch, tmp_path):
    """V7：控制面可停用 / 调序该区（单测）。"""
    wid, _root, sub_id = await _make_work_with_child()
    tt = _write_type_texts(tmp_path, {"thinking": {"sub_project": "思维正文"}})
    monkeypatch.setattr(context_assembler, "_type_texts_path", lambda: tt)
    assert 95 in [r["order"] for r in context_assembler.build_context_regions(wid, sub_id)]

    cfg = tmp_path / "ctx.json"
    monkeypatch.setattr(context_assembler, "_control_path", lambda: cfg)
    cfg.write_text(json.dumps({"regions": {"type_thinking": {"enabled": False}}}),
                   encoding="utf-8")
    assert 95 not in [r["order"] for r in context_assembler.build_context_regions(wid, sub_id)]
    cfg.write_text(json.dumps({"regions": {"type_thinking": {"order": 120}}}),
                   encoding="utf-8")
    orders = [r["order"] for r in context_assembler.build_context_regions(wid, sub_id)]
    assert 120 in orders and 95 not in orders
    assert orders == sorted(orders)
