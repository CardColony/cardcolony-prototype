# -*- coding: utf-8 -*-
"""卡片职责区（system order 90）· 回归网（2026-10-14）。

新区＝**类型级**「卡片职责」：每类卡一行 `- {label}（{code}）：{description}`，
来源＝`config/card_type_registry.json`（`l1_vocabularies.*.types[]`）。
核心诉求＝**扩展性**：新增一种卡＝注册表加一项 ⇒ 本区零代码自动多一行。

铁律一：沙箱内跑，不碰真实 works/。
"""
from __future__ import annotations

import json

from src.api import context_assembler, context_regions
from src.storage import card_store, work_store


async def _make_work():
    wdata = await work_store.create_work("卡片职责区剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    return wid, "card_001"


def _registry_types(registry: dict) -> list[dict]:
    """按渲染顺序展开注册表类型项（root 在前，其余按 l1_vocabularies 声明序）。"""
    vocabs = registry.get("l1_vocabularies") or {}
    scopes = (["root"] if "root" in vocabs else []) + [k for k in vocabs if k != "root"]
    out: list[dict] = []
    # [2026-10-31 对齐渲染器] 三套词表里 function / rule / role / review 是**同一个
    # code 重复声明**的。渲染器原来逐词表平铺 ⇒ 同一类型在一份 system 里出现 2–3 行
    # （实测评审席上下文里「职能卡」3 行、「规则卡」2 行、「角色卡」2 行），
    # 现已按 code 去重（保留首次出现的那份描述）。本函数是断言基准，必须同口径，
    # 否则就是把"重复渲染"这个缺陷钉成规格。
    seen: set[str] = set()
    for s in scopes:
        for t in ((vocabs.get(s) or {}).get("types") or []):
            code = str((t or {}).get("code") or "")
            if code and code in seen:
                continue
            if code:
                seen.add(code)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# V1：新区存在（order==90 / name=="卡片职责" / channel=="system"）
# ---------------------------------------------------------------------------


async def test_v1_duty_region_present(sandbox):
    wid, cid = await _make_work()
    regions = context_assembler.build_context_regions(wid, cid)
    print("REGIONS:", [(r["order"], r["name"], r["channel"], r["seats"]) for r in regions])
    r90 = next((r for r in regions if r["order"] == 90), None)
    assert r90 is not None
    assert r90["name"] == "卡片职责"
    assert r90["channel"] == "system"
    assert r90["seats"] == ("main",)
    assert set(r90) == {"order", "name", "channel", "seats", "text", "entries"}
    assert r90["entries"] == len(_registry_types(card_store.load_type_registry()))


# ---------------------------------------------------------------------------
# V2＋V4：内容逐条对应注册表（label/code/description），无增无删、逐字相等
# ---------------------------------------------------------------------------


async def test_v2_lines_match_registry_exactly(sandbox):
    wid, cid = await _make_work()
    r90 = next(r for r in context_assembler.build_context_regions(wid, cid) if r["order"] == 90)
    types = _registry_types(card_store.load_type_registry())
    assert len(types) >= 4  # 现 4 类：project / sub_project / tool / topic
    lines = r90["text"].splitlines()
    assert lines[0] == "## 卡片职责"
    body = lines[1:]
    assert len(body) == len(types)  # 无增无删
    for line, t in zip(body, types):
        assert line == f"- {t['label']}（{t['code']}）：{t['description']}"  # 逐字（V4）


# ---------------------------------------------------------------------------
# V3：顺序 90 在 100 之前
# ---------------------------------------------------------------------------


async def test_v3_order_before_card_list(sandbox):
    wid, cid = await _make_work()
    orders = [r["order"] for r in context_assembler.build_context_regions(wid, cid)]
    assert orders.index(90) < orders.index(100)
    assert orders == sorted(orders)


# ---------------------------------------------------------------------------
# V6：扩展性——注册表加一类型 ⇒ 本区自动多一行（零代码改动）
# ---------------------------------------------------------------------------


async def test_v6_new_type_auto_appears(sandbox, monkeypatch, tmp_path):
    wid, cid = await _make_work()  # 建卡走真实注册表
    # [2026-10-27] 先取"加类型之前"的行数：本测试的意图是"加一项就多一行"，
    # 原断言写死 "== 10"，任何人往词表加类型都会让它红（现已 13+ 条）。
    r90_before = next(
        r for r in context_assembler.build_context_regions(wid, cid) if r["order"] == 90)
    reg = json.loads(card_store.TYPE_REGISTRY_PATH.read_text(encoding="utf-8"))
    reg["l1_vocabularies"]["rd_project"]["types"].append({
        "code": "new_kind", "label": "新卡种",
        "description": "扩展性探针：注册表加一项即多一行。",
        "can_have_children": False, "requires_owner": False,
        "default_template": "new_kind", "allowed_transitions": ["active"],
    })
    p = tmp_path / "reg_extended.json"
    p.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(card_store, "TYPE_REGISTRY_PATH", p)

    r90 = next(r for r in context_assembler.build_context_regions(wid, cid) if r["order"] == 90)
    assert "- 新卡种（new_kind）：扩展性探针：注册表加一项即多一行。" in r90["text"]
    # [2026-10-27] 改为与"加之前"比：+1。意图不变（加类型即自动多一行），
    # 但不再因词表条目总数变化而误红。
    assert r90["entries"] == r90_before["entries"] + 1


# ---------------------------------------------------------------------------
# V7：fail-open——注册表读不到 ⇒ 区不出现、不报错、装配继续
# ---------------------------------------------------------------------------


async def test_v7_fail_open_missing_registry(sandbox, monkeypatch, tmp_path):
    wid, cid = await _make_work()
    monkeypatch.setattr(card_store, "TYPE_REGISTRY_PATH", tmp_path / "nope.json")
    regions = context_assembler.build_context_regions(wid, cid)  # 不抛
    # [2026-10-22] 新增 92 卡片能力 区
    assert [r["order"] for r in regions] == [92, 100, 150, 155]
    assert not any(r["name"] == "卡片职责" for r in regions)


# ---------------------------------------------------------------------------
# V8：区进 prompt 日志 regions
# ---------------------------------------------------------------------------


async def test_v8_log_regions_contains_90(sandbox):
    wid, cid = await _make_work()
    regions = context_assembler.build_context_regions(wid, cid)
    log_regions = context_regions.build_log_regions(regions, [], [], "u")
    assert 90 in [e["order"] for e in log_regions]
    e90 = next(e for e in log_regions if e["order"] == 90)
    assert e90["name"] == "卡片职责" and e90["channel"] == "system"


# ---------------------------------------------------------------------------
# N6：职责文本不得塞进 100 区（两区职责不得混）
# ---------------------------------------------------------------------------


async def test_n6_duty_not_in_card_list(sandbox):
    wid, cid = await _make_work()
    regions = context_assembler.build_context_regions(wid, cid)
    r100 = next(r for r in regions if r["order"] == 100)
    assert "## 卡片职责" not in r100["text"]
    for t in _registry_types(card_store.load_type_registry()):
        assert t["description"] not in r100["text"]


# ---------------------------------------------------------------------------
# 纯函数直测：空注册表 → 空串（没有来源就不出现这个区）
# ---------------------------------------------------------------------------


def test_render_empty_registry_returns_empty():
    assert context_regions.render_card_duty_region({}) == ""
    assert context_regions.render_card_duty_region({"l1_vocabularies": {}}) == ""
    assert context_regions.render_card_duty_region({"l1_vocabularies": {"root": {"types": []}}}) == ""
