# -*- coding: utf-8 -*-
"""职能组建区（system order 91）· 回归网（2026-10-21 · card_029，card_044 轮首改造）。

本区＝「自动模式轮首必须做拆职能判断」的逐字注入：只给判断标准、不建职能表，
约束「职能必须是人类团队里真实存在的岗位」，职能名由模型自己填，系统**不做
白名单/枚举校验**。

三条渲染条件**同时**满足才出区，否则空串：
  1. 当前卡是**项目根卡**：`cur_meta.card_id == work_card_id`（无 card_id 时退回 ctx.card_id）；
  2. **自动模式**：`bool(cur_meta.auto_mode)`；
  3. **轮首**：`int(cur_meta.goal_rounds or 0) == 0`（goal_rounds>0 ⇒ 空串）。
第 3 条 fail-open：读不到/为空/抛异常 ⇒ 按 0 处理（宁可多判断一次，不可静默吞掉）。

本区**不做职能闸门**（不判「有没有职能」「建过没有子卡」）——判断靠提示词要求。

全部用例走**纯函数**（构造 RegionCtx 直调渲染器/装配入口），不碰 works/、不连 DB。
"""
from __future__ import annotations

import json

from src.api import context_regions
from src.api.context_regions import RegionCtx, render_team_function_instruction_region
from src.storage import card_store

# 判据①要求逐字命中的关键句（验收标准里的原文，不是概括）
KEY_SENTENCE = "先做一个明确判断"
KEY_SENTENCE2 = "人类团队里真实存在的岗位"


def _root_ctx(auto_mode=1, meta_card_id="card_001", goal_rounds=0) -> RegionCtx:
    """根卡 + 自动模式（轮首 goal_rounds=0）的装配上下文。"""
    return RegionCtx(
        work_id="work_001",
        card_id=meta_card_id,
        work_card_id="card_001",
        cur_meta={"card_id": meta_card_id, "auto_mode": auto_mode,
                  "goal_rounds": goal_rounds,
                  "card_type": "project", "perms": ["create_child"]},
    )


# ---------------------------------------------------------------------------
# 判据①：根卡 + auto=1 + goal_rounds=0（轮首）→ 非空，且逐字包含关键句
# ---------------------------------------------------------------------------


def test_c1_root_auto_renders_text_with_key_sentence():
    out = render_team_function_instruction_region(_root_ctx())
    assert out != ""
    assert KEY_SENTENCE in out
    assert KEY_SENTENCE2 in out
    assert out.startswith("## 拆职能（你这一轮的第一件事）")
    # goal_rounds 缺省（未设键）也应视为 0 → 出区
    ctx = RegionCtx(work_id="work_001", card_id="card_001", work_card_id="card_001",
                    cur_meta={"card_id": "card_001", "auto_mode": 1})
    assert render_team_function_instruction_region(ctx) != ""


# ---------------------------------------------------------------------------
# 判据②（核心变更）：根卡 + auto=1 + goal_rounds>0（非轮首）→ 空串
# ---------------------------------------------------------------------------


def test_c2_non_first_round_returns_empty():
    """goal_rounds=2 ⇒ **空串**——只轮首出区，轮首之外不再当墙纸每轮念。"""
    assert render_team_function_instruction_region(_root_ctx(goal_rounds=2)) == ""
    # 任意 >0 的轮次都不出
    assert render_team_function_instruction_region(_root_ctx(goal_rounds=1)) == ""
    assert render_team_function_instruction_region(_root_ctx(goal_rounds=5)) == ""


# ---------------------------------------------------------------------------
# 判据③：非根卡（card_id != work_card_id）→ 空串
# ---------------------------------------------------------------------------


def test_c3_non_root_returns_empty():
    ctx = RegionCtx(
        work_id="work_001",
        card_id="card_029",
        work_card_id="card_001",
        cur_meta={"card_id": "card_029", "auto_mode": 1, "goal_rounds": 0,
                  "card_type": "sub_project"},
    )
    assert render_team_function_instruction_region(ctx) == ""
    # cur_meta 无 card_id 时退回 ctx.card_id 比——同样必须是空串
    ctx2 = RegionCtx(work_id="work_001", card_id="card_029",
                     work_card_id="card_001", cur_meta={"auto_mode": 1})
    assert render_team_function_instruction_region(ctx2) == ""


# ---------------------------------------------------------------------------
# 判据④：根卡但 auto_mode=0 → 空串
# ---------------------------------------------------------------------------


def test_c4_root_but_not_auto_returns_empty():
    assert render_team_function_instruction_region(_root_ctx(auto_mode=0)) == ""
    # auto_mode 键缺失同样不算自动模式
    ctx = RegionCtx(work_id="work_001", card_id="card_001", work_card_id="card_001",
                    cur_meta={"card_id": "card_001", "goal_rounds": 0})
    assert render_team_function_instruction_region(ctx) == ""


# ---------------------------------------------------------------------------
# 判据⑤：cur_meta 为空 dict / None → 空串，且不抛异常（fail-open）
# ---------------------------------------------------------------------------


def test_c5_empty_cur_meta_returns_empty_no_raise():
    ctx = RegionCtx(work_id="work_001", card_id="card_001",
                    work_card_id="card_001", cur_meta={})
    assert render_team_function_instruction_region(ctx) == ""
    # 连 card_id 都读不到（既无 meta.card_id 也无 ctx.card_id）→ 同样空串不抛
    ctx2 = RegionCtx(work_id="work_001", cur_meta={})
    assert render_team_function_instruction_region(ctx2) == ""
    # 默认 RegionCtx（一个字段都没给）也不抛
    assert render_team_function_instruction_region(RegionCtx()) == ""


# ---------------------------------------------------------------------------
# 判据⑥：走真实装配入口，确认 order=91 出区、90<91<92
# ---------------------------------------------------------------------------


def test_c6_assembly_entry_order_91_between_90_and_92():
    ctx = _root_ctx()
    ctx.type_registry = card_store.load_type_registry()   # 让 90 区真的出内容
    regions = context_regions.render_registered_regions(
        ctx, profile="main", deliveries={"system"})
    orders = [r["order"] for r in regions]
    assert orders == sorted(orders)

    r91 = next((r for r in regions if r["key"] == "team_function_instruction"), None)
    assert r91 is not None, f"order 91 区未出：{orders}"
    assert r91["order"] == 91
    assert r91["name"] == "组建职能"
    assert r91["payload"] != ""                    # 不为空
    assert KEY_SENTENCE in r91["payload"]
    assert r91["channel"] == "system" and r91["delivery"] == "system"

    assert 90 in orders and 92 in orders, f"缺 90/92：{orders}"
    assert orders.index(90) < orders.index(91) < orders.index(92)


# ---------------------------------------------------------------------------
# 配置面：控制面里注册本区（enabled/order），version 保持在 v9
# ---------------------------------------------------------------------------


def test_c7_config_registers_region():
    cfg = json.loads(card_store.CONTEXT_REGIONS_PATH.read_text(encoding="utf-8")) \
        if hasattr(card_store, "CONTEXT_REGIONS_PATH") else None
    if cfg is None:
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / "config" / "context_regions.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
    entry = cfg["regions"]["team_function_instruction"]
    assert entry["enabled"] is True
    assert entry["order"] == 91
    # [card_031] 本步把 config 版本推进到 v9（新增 93 现有职能 ＋ 145 你的职能）
    assert cfg["_meta"]["version"] == "context-regions-v9"
    # 阅读顺序与 order 一致：放在 card_duty 之后、card_ability 之前
    keys = list(cfg["regions"].keys())
    assert keys.index("team_function_instruction") == keys.index("card_duty") + 1
    assert keys.index("team_function_instruction") < keys.index("card_ability")


# ---------------------------------------------------------------------------
# 纪律面：**不建职能表、不做白名单校验**——系统不得因职能名而改判
# ---------------------------------------------------------------------------


def test_c8_no_whitelist_gate():
    """区是纯常量注入：任何 meta（含带职能名/白名单字段的 meta）输出都逐字相同。"""
    a = render_team_function_instruction_region(_root_ctx())
    ctx = _root_ctx()
    ctx.cur_meta["functions"] = ["产品经理", "架构师", "不存在的职能"]
    ctx.cur_meta["role_whitelist"] = ["产品经理"]
    b = render_team_function_instruction_region(ctx)
    assert a == b, "输出不得随 meta 里的职能名/白名单变化——本卡不做白名单校验"
    # 判断标准必须是「人类团队里真实存在的岗位」而不是某个具体职能名被指定
    assert "不是清单" in a