# -*- coding: utf-8 -*-
"""[事件管理 2026-10-26] 防漏对账：新增卡类型 / 新增事件类型不许悄悄漏接。

两条：
C1 **事件类型**：扫源码里所有事件产生点（字面量形式），必须都在
   ``config/event_catalog.json`` 登记。新增类型没登记 ⇒ 本测试红。
C2 **卡类型**：从 ``config/card_type_registry.json`` 取全部类型，每种类型
   做一次写操作，必须产出事件。新增卡类型没接事件 ⇒ 本测试红。

动态拼接的类型（``ev["type"]`` / ``ev_type`` 变量 / ``f"msg_{mt}"``）静态
扫不到，由 test_event_hub 的运行时用例 + 登记表覆盖；本文件只管"字面量必须登记"。
"""
from __future__ import annotations

import glob
import json
import os
import re

import pytest

from src.api import event_stream
from src.infra import event_hub
from src.storage import (card_store, event_log, function_store, role_store,
                         shadow_store, work_store)


@pytest.fixture(autouse=True)
def _clean():
    event_hub.reset_state()
    yield
    event_hub.reset_state()


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source_files() -> list[str]:
    out = []
    for p in glob.glob(os.path.join(_root(), "src", "**", "*.py"), recursive=True):
        out.append(p)
    return out


#: 字面量事件产生点的三种写法
_PATTERNS = (
    re.compile(r'event_stream\.emit\(\s*[^,]+,\s*[^,]+,\s*"([a-z_0-9]+)"'),
    re.compile(r'event_stream\.emit_work\(\s*[^,]+,\s*"([a-z_0-9]+)"'),
    re.compile(r'(?<![\w.])_emit\(\s*[^,]+,\s*"([a-z_0-9]+)"'),
)


# ---------------------------------------------------------------- C1
def test_every_literal_event_type_is_registered(sandbox):
    """源码里字面量写出的每个事件类型，都必须在登记表里。"""
    catalog = event_hub.load_catalog()
    assert catalog, "登记表为空"

    missing: list[str] = []
    for path in _source_files():
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for pat in _PATTERNS:
            for m in pat.finditer(text):
                t = m.group(1)
                if t not in catalog:
                    rel = os.path.relpath(path, _root())
                    missing.append(f"{rel}: {t}")
    assert not missing, (
        "以下事件类型未在 config/event_catalog.json 登记（新增类型请补登记）：\n  "
        + "\n  ".join(sorted(set(missing))))


# ---------------------------------------------------------------- C2
def _writable_card_types() -> list[str]:
    """从**卡类型注册表**取全部可写类型（不再硬编码）。

    [harness 第 1 步 2026-10-28] 改前这里是硬编码的九个名字，而 docstring 却
    声称「从 config/card_type_registry.json 取全部类型」——于是新增一种卡类型
    不会红、删一种也不会红，报备闸对卡类型这一半是空的。
    现在按 `l1_vocabularies[*].types[*].code` 动态取；`legacy_types` 是已清退
    的历史类型，不参与（它们本来就不该再产事件）。
    """
    from src.storage import card_store
    reg = card_store.load_type_registry()
    out: list[str] = []
    for spec in (reg.get("l1_vocabularies") or {}).values():
        for t in (spec.get("types") or []):
            code = str((t or {}).get("code") or "")
            if code and code not in out:
                out.append(code)
    return out


#: 走 card_store.create_card 直建的卡类型（project 用根卡本身、tool 需 owner）
#: [差事卡 2026-11-01] qa_card/pm_card 是差事场卡——注册表里的可写类型，
#: 直建＋写一块同样要产 card_block_edited。
_CARD_STORE_TYPES = ("sub_project", "worker", "rule", "qa_card", "pm_card", "ui")

#: 卡类型 → 豁免理由（不直建、由别的模块派生）。
#: **双向约束**：这里登记了但测试其实覆盖到 ⇒ 也要红（豁免不能长期挂着）。
_COVERAGE_EXEMPT: dict[str, str] = {
    "review": "评审卡由 review_store 派生（离树、不进画布），"
              "其事件在 test_review_* 里覆盖，不在本文件的建卡分支内",
}


async def _settle():
    await event_log.drain()
    await event_hub.flush_all()


async def test_every_card_type_records_events(sandbox):
    """每种卡片类型做一次写操作，都必须留下事件。

    各类型的建立方式不同（这是卡类型注册表本身的约束，不是本测试的豁免）：
      project     → 根卡本身就是 project，直接 update 它
      sub_project/worker/rule → 直建
      tool        → 必须带 owner_card_id
      topic       → 只属于 research_project 词表，单独建一个该类的 work
      function/role → 档案 store
      shadow      → 由正卡派生
    """
    w = await work_store.create_work("卡类型对账")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    missed: list[str] = []

    # ① project：根卡本身
    await card_store.update_card(wid, root, {"mission": "project 使命"}, "init")

    # ② 直建类型
    for ct in _CARD_STORE_TYPES:
        r = await card_store.create_card(wid, f"卡-{ct}", "模型生成",
                                         card_type=ct, parent=root)
        cid = (r.get("meta") or {}).get("card_id") or ""
        if not cid:
            missed.append(f"{ct}: 建卡失败 {r}")
            continue
        await card_store.update_card(wid, cid, {"mission": f"{ct} 使命"}, "init")

    # ③ tool（需 owner）
    rt = await card_store.create_card(wid, "工具卡", "模型生成", card_type="tool",
                                      parent=root, owner_card_id=root)
    tool_id = (rt.get("meta") or {}).get("card_id") or ""
    if not tool_id:
        missed.append(f"tool: 建卡失败 {rt}")
    else:
        await card_store.update_card(wid, tool_id, {"mission": "tool 使命"}, "init")

    # ④ 职能卡（function_store 档案）
    await function_store.ensure_profile(wid, "fn_001", {
        "function_name": "对账职能", "jurisdiction": {"domain": "x"}})
    await function_store.update_profile(wid, "fn_001", {"charter": "职责书"})

    # ⑤ 角色卡（role_store 档案）
    await role_store.ensure_profile(wid, "role_001", {
        "role_name": "对账角色", "jurisdiction": {"domain": "x"}})
    await role_store.update_profile(wid, "role_001", {"charter": "职责书"})

    # ⑥ 影子卡（由正卡派生 + 写一次）
    await shadow_store.derive_shadow(wid, root)
    await shadow_store.write_shadow_raw(wid, root, "## 结论\n对账写一次", "对账")

    await _settle()

    rows = event_hub.list_events(wid, ev_type="card_block_edited", limit=500)
    seen = {json.loads(r["data"]).get("card_type") for r in rows}
    for ct in ("project", *_CARD_STORE_TYPES, "tool", "function", "role", "shadow"):
        if ct not in seen:
            missed.append(f"{ct}: 无 card_block_edited 事件")

    assert not missed, "以下卡类型没有产出事件：\n  " + "\n  ".join(missed)

    # [harness 第 1 步 2026-10-28] 与注册表对账（双向约束，照 system_claims 的
    # MANUAL/UNMET 手法）：
    #   · 注册表里的可写类型，必须在这里**被测到**，或在 `_COVERAGE_EXEMPT` 里
    #     登记豁免理由——新增一种卡类型而没人接事件 ⇒ 本测试红。
    #   · 豁免项一旦被真正覆盖，也必须把豁免删掉——修好了不删同样红。
    covered = set(seen) | {"topic"}
    uncovered = [t for t in _writable_card_types()
                 if t not in covered and t not in _COVERAGE_EXEMPT]
    assert not uncovered, (
        "以下卡类型已在注册表里，但本测试没有验证它会产出事件"
        "（请在下面加构造分支，或登记到 _COVERAGE_EXEMPT 并写明理由）：\n  "
        + "\n  ".join(uncovered))
    stale = [t for t in _COVERAGE_EXEMPT if t in covered]
    assert not stale, (
        "以下豁免项已被真正覆盖，请从 _COVERAGE_EXEMPT 删掉（豁免不能长期挂着）：\n  "
        + "\n  ".join(stale))

    # ⑦ topic：只属于 research_project 词表，单独开一棵该类项目树
    w2 = await work_store.create_work("研究项目对账", project_class="research_project")
    wid2 = w2.get("work_id") or ""
    if not wid2:
        missed2 = [f"topic: 建 research_project 失败 {w2}"]
    else:
        root2 = work_store.work_card_id_of(wid2)
        r7 = await card_store.create_card(wid2, "议题卡", "模型生成",
                                          card_type="topic", parent=root2)
        t7 = (r7.get("meta") or {}).get("card_id") or ""
        missed2 = []
        if not t7:
            missed2.append(f"topic: 建卡失败 {r7}")
        else:
            await card_store.update_card(wid2, t7, {"mission": "topic 使命"}, "init")
            await _settle()
            rows2 = event_hub.list_events(wid2, ev_type="card_block_edited", limit=100)
            if not any(json.loads(r["data"]).get("card_type") == "topic" for r in rows2):
                missed2.append("topic: 无 card_block_edited 事件")
    assert not missed2, "以下卡类型没有产出事件：\n  " + "\n  ".join(missed2)


# ---------------------------------------------------------------- C3
def test_catalog_types_have_required_fields(sandbox):
    """登记表每条都要有 label/group/source/channel/triggerable ＋ **fields 契约**。"""
    bad = []
    for code, entry in event_hub.load_catalog().items():
        for k in ("label", "group", "source", "channel", "triggerable"):
            if k not in entry:
                bad.append(f"{code}: 缺 {k}")
        if entry.get("channel") not in ("card", "work"):
            bad.append(f"{code}: channel 非法 {entry.get('channel')!r}")
    assert not bad, "登记表条目不完整：\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------- C4
def test_every_type_declares_field_contract(sandbox):
    """[harness 第 1 步 2026-10-28] 每条事件必须声明字段契约。

    报备的实质不是「登记一个类型名」，而是「登记它必须带哪些零件」——
    只登记名字的话，报备完照样能发一条只有 content 的事件，条件面上是空的。

    三条硬约束：
      1. 必须有 `fields` 块，且 part/optional/payload 三个键都在（可为空列表）；
      2. 三类之间**不许交叉**——同一个键不能既是零件又是载荷，否则"7 天清载荷、
         零件长期留"的保留策略会自相矛盾；
      3. `triggerable=true` 的类型 **part 不许为空**——能被规则触发的类型，
         若一个零件都没有，那条规则就没有任何可组合的条件面。
    """
    bad: list[str] = []
    for code, entry in event_hub.load_catalog().items():
        f = entry.get("fields")
        if not isinstance(f, dict):
            bad.append(f"{code}: 缺 fields 块")
            continue
        groups: dict[str, set[str]] = {}
        for k in ("part", "optional", "payload"):
            if k not in f:
                bad.append(f"{code}: fields 缺 {k}")
                groups[k] = set()
                continue
            v = f.get(k)
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                bad.append(f"{code}: fields.{k} 必须是字符串列表")
                groups[k] = set()
            else:
                groups[k] = set(v)
        # ② 三类不许交叉
        for a, b in (("part", "optional"), ("part", "payload"), ("optional", "payload")):
            dup = groups[a] & groups[b]
            if dup:
                bad.append(f"{code}: {a} 与 {b} 重复 {sorted(dup)}")
        # ③ 可触发 ⇒ 必须有零件
        if entry.get("triggerable") and not groups["part"]:
            bad.append(f"{code}: triggerable=true 但 fields.part 为空"
                       f"（没有零件就组合不出条件）")
    assert not bad, "字段契约不完整：\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------- C5
def test_deprecated_must_be_write_blocked(sandbox):
    """[清退机制 2026-10-28] 清退必须成对：deprecated 与 write_blocked 同时为真。

    只标 deprecated 不标 write_blocked ⇒ 旧类型还能继续写，等于没清退；
    只标 write_blocked 不标 deprecated ⇒ 历史数据失去"已停用"的解释。
    **清退 ≠ 删行**：历史 jsonl 里躺着旧类型的记录，删了登记表那些历史就
    变成无法解释的孤儿（照 card_type_registry.legacy_types 的手法）。
    """
    bad = []
    for code, entry in event_hub.load_catalog().items():
        dep = bool(entry.get("deprecated"))
        blk = bool(entry.get("write_blocked"))
        if dep != blk:
            bad.append(f"{code}: deprecated={dep} / write_blocked={blk}（必须成对）")
    assert not bad, "清退标记不成对：\n  " + "\n  ".join(bad)
