# -*- coding: utf-8 -*-
"""[规则卡 2026-10-15] 区注入（块1）＋ 规则面板/审批通道 API（块2）＋ 根卡审批通道（块3）。

硬判据：
块1：rule 区在 REGISTRY order 85；context_regions.rule_section_names==六区；
     permissions rule perms==["read"]；render_rule_region 空/命中/缺区三态；
     controls enabled:false 时 rule 区不出现；load_control fail-open。
块2：GET /works/{w}/rule 六区齐全（含空区）、hashes 与盘上一致、inject_type_counts；
     PATCH /rule/sections/{sec} hash 比对（不符→409）、仅替换目标区、
     其它区字节不变、经 update_card 写闸；
     create_work_card(card_type=rule) 播种六区空模板。
块3：工具面分化（根卡有 draft_rule_edit、非根卡无、执行层非根卡直调 denied）；
     draft 返回 {draft, base_hash, section, old} 且盘上零改动；
     /rule/commit 比对过 → 落盘＋影子 ## 迭代历史 行，hash 冲突 → 409 零写；
     注入按当前卡 card_type 择区（order 85 位于 instruction 与 card_duty 之间）；
     无规则卡零报错；rule 卡模型面无写工具；面板分区保存其它区字节不变；
     前端审批弹窗源码级守卫（同意→commit、拒绝/关闭→/rule/dismiss 驳回落痕）；
     [2026-11-03 驳回落痕] /rule/dismiss 记 card_id:seq 进 rule_dismissals.json、
     GET /rule 下发 dismissed、起草卡收驳回回执（落信不唤起）、重复驳回幂等、
     扫描三重过滤（已驳回/已落盘/基底过期）防历史草案反复弹窗。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.api import card_tools, context_assembler, context_regions, routes
from src.permissions.policy import perms_for
from src.storage import card_store, session_facade, shadow_store, work_store

_ROOT = Path(__file__).resolve().parents[1]


def _strip_js_comments(src: str) -> str:
    """去掉 /* */ 与 // 注释（源码级断言不被注释里的词条带偏）。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _h(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


@pytest.fixture
async def client(sandbox):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _mk_rd_work():
    wdata = await work_store.create_work("研发树", project_class="rd_project")
    assert "error" not in wdata, wdata
    return wdata["work_id"]


async def _seed_sections(wid: str, sections: dict[str, str]) -> str:
    """把规则写进本项目的规则文件（``works/<w>/rules.md``），返回文件正文。

    [规则节点 2026-10-28 · 用户令「一个项目一个文件」] 改前这里往规则卡的
    mission 里播种——规则不是卡之后，播种对象就是规则文件本身。
    """
    from src.storage import rule_store
    rule_store.write_sections(wid, sections)
    return rule_store.rules_path(wid).read_text(encoding="utf-8")


async def _mk_typed(wid: str, root: str, card_type: str, title: str) -> str:
    """建一张指定 card_type 的子卡，返回 card_id（工具卡带 owner＝根卡，闸3）。"""
    kwargs: dict[str, str] = {"card_type": card_type, "parent": root}
    if card_type == "tool":
        kwargs["owner_card_id"] = root
    r = await card_store.create_card(wid, title, "用户直接创建", **kwargs)
    assert "error" not in r, r
    return r["meta"]["card_id"]


# ---------------------------------------------------------------------------
# 块1 · 区注入
# ---------------------------------------------------------------------------


def test_rule_region_is_order_85():
    by_key = {r["key"]: r for r in context_regions.REGION_TABLE}
    assert by_key["rule"]["order"] == 85
    assert by_key["rule"]["name"] == "规则注入"


def test_rule_section_names_all_plus_type_codes():
    """分区＝**总规则 all ＋ 注册表全部 card_type code**（用户令：总规则进所有卡）。

    类型注册表长出新类型（function / review），分区就跟着长——这是设计：
    每类卡都有自己的附加规则档，空着也是合法状态。
    """
    names = context_regions.rule_section_names(card_store.load_type_registry())
    assert names[0] == "all", "总规则必须排第一"
    assert names == ["all", "project", "function", "sub_project",
                     "tool", "worker", "ui", "qa_card", "pm_card",
                     "align", "rule", "review", "topic"]
    # 词表缺失 ⇒ 至少保留总规则（总规则与词表无关）
    assert context_regions.rule_section_names({}) == ["all"]


def test_rule_type_has_no_perms():
    """[2026-10-28 用户令「规则卡不挂模型和对话，不需要工具」]

    改前 rule 类型挂 `read`＋`tools_web`（于是它有 read_card/web_search 等 7 件）。
    规则节点没有模型、没有对话 ⇒ 一件工具都不该有。权限空＝工具面空。
    """
    assert perms_for("rd_project", "rule") == []
    assert perms_for("research_project", "rule") == []


def test_render_rule_region_states():
    """注入渲染四态：都空／只有本类／只有总规则／两段都有。"""
    assert context_regions.render_rule_region({}, "sub_project") == ""
    assert context_regions.render_rule_region({"sub_project": "A"}, "sub_project") == "## 规则\nA"
    # 缺区（没有该 card_type 也没有总规则）→ 空串
    assert context_regions.render_rule_region({"tool": "T"}, "sub_project") == ""
    # 只有总规则 → 所有卡都拿得到（用户令：总规则进所有卡的上下文）
    assert context_regions.render_rule_region({"all": "总则"}, "sub_project") == "## 规则\n总则"
    assert context_regions.render_rule_region({"all": "总则"}, "worker") == "## 规则\n总则"
    # 两段都有 → 总规则在前、本类卡在后
    both = context_regions.render_rule_region({"all": "总则", "sub_project": "本类"}, "sub_project")
    assert both.startswith("## 规则\n")
    assert both.index("总则") < both.index("本类")
    assert "总规则（所有卡）" in both and "子项目卡" in both


async def test_rule_region_absent_when_all_sections_empty(sandbox):
    """规则区是「没有来源就不出现」：全部分区为空 ⇒ 85 区不 join。

    [规则节点 2026-10-28] 改前这条断言的是「没有规则卡」；规则随项目自带之后，
    「空」的判据变成「每个区都没内容」——用户令「规则可以不用」，不用就是空。
    """
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    from src.storage import rule_store
    rule_store.write_sections(wid, {})          # 全空
    regions = context_assembler.build_context_regions(wid, root)
    assert 85 not in [r["order"] for r in regions]


async def test_load_control_fail_open(sandbox):
    # config/context_regions.json 缺失 → {}（不阻断装配）
    assert isinstance(context_assembler.load_control(), dict)


# ---------------------------------------------------------------------------
# 块2 · 规则面板 API
# ---------------------------------------------------------------------------


async def test_get_rule_six_sections_and_hashes(client, sandbox):
    wid = await _mk_rd_work()
    r = await client.get(f"/api/works/{wid}/rule")
    assert r.status_code == 200
    d = r.json()
    # [规则=项目基建 2026-10-25] exists 恒 True(随项目自带),默认内容在位
    assert d["exists"] is True
    names = context_regions.rule_section_names(card_store.load_type_registry())
    assert set(d["sections"].keys()) == set(names)
    assert set(d["hashes"].keys()) == set(names)
    for n in names:
        assert d["hashes"][n] == _h(d["sections"][n])
    assert "拆解用户意图" in d["sections"]["project"]      # 工作流默认值


async def test_get_rule_reads_seeded_sections(client, sandbox):
    wid = await _mk_rd_work()
    from src.storage import work_store as _ws
    _ws.set_rules(wid, {"sub_project": "子项目规则A", "tool": "工具规则B"})
    r = await client.get(f"/api/works/{wid}/rule")
    d = r.json()
    assert d["exists"] is True
    assert d["sections"]["sub_project"] == "子项目规则A"
    assert d["sections"]["tool"] == "工具规则B"
    assert d["hashes"]["sub_project"] == _h("子项目规则A")
    assert d["hashes"]["tool"] == _h("工具规则B")
    # inject_type_counts：rule 出现两次（rd+research），其余一次
    assert d["inject_type_counts"]["rule"] == 2
    assert d["inject_type_counts"]["sub_project"] == 1
    assert d["inject_type_counts"]["project"] == 1


async def test_patch_rule_section_replaces_only_target(client, sandbox):
    wid = await _mk_rd_work()
    await _seed_sections(wid, {"sub_project": "旧内容", "tool": "工具内容", "worker": "编码内容"})
    r = await client.get(f"/api/works/{wid}/rule")
    base = r.json()["hashes"]["sub_project"]

    r2 = await client.patch(
        f"/api/works/{wid}/rule/sections/sub_project",
        json={"content": "新内容", "base_hash": base})
    assert r2.status_code == 200
    assert r2.json()["new_hash"] == _h("新内容")

    r3 = await client.get(f"/api/works/{wid}/rule")
    d3 = r3.json()
    assert d3["sections"]["sub_project"] == "新内容"
    # 其它区字节不变
    assert d3["sections"]["tool"] == "工具内容"
    assert d3["sections"]["worker"] == "编码内容"


async def test_patch_rule_section_hash_mismatch_409(client, sandbox):
    wid = await _mk_rd_work()
    await _seed_sections(wid, {"sub_project": "旧内容"})
    r2 = await client.patch(
        f"/api/works/{wid}/rule/sections/sub_project",
        json={"content": "改", "base_hash": "wrong_hash"})
    assert r2.status_code == 409
    # 卡面零改动
    r3 = await client.get(f"/api/works/{wid}/rule")
    assert r3.json()["sections"]["sub_project"] == "旧内容"


async def test_patch_rule_section_illegal_name_400(client, sandbox):
    wid = await _mk_rd_work()
    r = await client.patch(
        f"/api/works/{wid}/rule/sections/unknown_section",
        json={"content": "x", "base_hash": ""})
    assert r.status_code == 400


async def test_patch_rule_section_missing_card_autocreates(client, sandbox):
    """[规则卡自带 2026-10-25 · 用户令「根本没有建的入口」] 面板保存即合法
    建卡入口:规则卡不存在 ⇒ 按默认模板自动建(不再 404 死路),用户内容写入。"""
    wid = await _mk_rd_work()
    # 第一次 PATCH:触发自动建卡;base_hash="" 对不上默认内容 ⇒ 409
    # (防并发覆盖机制在正常工作,同时卡已建成)。
    r1 = await client.patch(
        f"/api/works/{wid}/rule/sections/sub_project",
        json={"content": "x", "base_hash": ""})
    assert r1.status_code == 409
    # 真实客户端流:GET 拿当前区 hash,再 PATCH 写入。
    g = await client.get(f"/api/works/{wid}/rule")
    h = g.json()["hashes"]["sub_project"]
    r = await client.patch(
        f"/api/works/{wid}/rule/sections/sub_project",
        json={"content": "自定义工作流规则", "base_hash": h})
    assert r.status_code == 200, r.text
    # 再读:卡已在,用户内容落在 sub_project 区,其他区是默认内容
    g = await client.get(f"/api/works/{wid}/rule")
    data = g.json()
    assert data["exists"] is True
    assert "自定义工作流规则" in data["sections"]["sub_project"]
    assert "拆解用户意图" in data["sections"]["project"]   # 默认内容在位


async def test_create_rule_card_is_rejected(client, sandbox):
    """[2026-10-28 用户令「手动不能建，所有项目都带一张规则卡」]

    规则随项目自带（rules.md），不能手动建卡：建出来会是一张不挂模型、
    不挂对话、没有工具的死卡，只会让人以为规则存在。
    """
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    r = await client.post(
        f"/api/works/{wid}/cards",
        json={"title": "规则卡", "source": "用户直接创建",
              "card_type": "rule", "parent": root})
    assert r.status_code == 400, r.text
    assert "rules.md" in r.text, "拒绝时要指出去处"
    # 零落卡
    n = card_store._meta_of(wid, root)  # 根卡还在
    assert n is not None
    from src.storage import card_db
    cnt = card_db.read_sync(
        wid, lambda c: c.execute(
            "SELECT count(*) AS n FROM cards WHERE card_type='rule'").fetchone()["n"], -1)
    assert cnt == 0


# ---------------------------------------------------------------------------
# 块3 · 注入按卡类型择区（硬判据5）
# ---------------------------------------------------------------------------

_SECTIONS_ALL = {
    "project": "根卡规则P", "sub_project": "子项目规则A", "tool": "工具规则T",
    "worker": "编码规则C", "rule": "规则卡规则R", "topic": "课题规则Q",
}


async def test_inject_section_by_card_type(sandbox):
    """各类型卡注入规则卡对应区；order 85 位于 instruction(0) 与 card_duty(90) 之间。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    await _seed_sections(wid, _SECTIONS_ALL)

    # ① sub_project 卡 → sub_project 区（硬判据5 主场景：顺序断言）
    sub = await _mk_typed(wid, root, "sub_project", "子项目")
    main = context_assembler.build_context_bundle(wid, sub)["main"]
    regs = main["system_regions"]
    names = [r["name"] for r in regs]
    i_rule = names.index("规则注入")
    assert regs[i_rule]["order"] == 85
    assert i_rule < names.index("卡片职责"), "order 85 须在卡片职责(90)之前"
    assert regs[i_rule]["text"] == "## 规则\n子项目规则A"
    # compose 结果断言：提示词在前 < 规则注入 < 卡片职责
    # （锚点用真实标题：提示词开头是「## 系统结构」，注入区是「## 规则」，
    #  卡片职责区紧随其后——order 85 < 90）
    sp = main["system_prompt"]
    assert sp.index("## 系统结构") < sp.index("## 规则\n子项目规则A") < sp.index("## 卡片职责")

    # ② worker 卡 → worker 区；tool 卡 → tool 区；rule 卡自身 → rule 区；根卡 → project 区
    for ct, sec in (("worker", "编码规则C"), ("tool", "工具规则T"),
                    ("rule", "规则卡规则R"), ("project", "根卡规则P")):
        cid = root if ct == "project" else await _mk_typed(wid, root, ct, f"{ct}卡")
        m = context_assembler.build_context_bundle(wid, cid)["main"]
        rule_regs = [r for r in m["system_regions"] if r["name"] == "规则注入"]
        assert rule_regs, f"{ct} 卡应注入规则区"
        assert rule_regs[0]["text"] == f"## 规则\n{sec}", ct
        assert f"## 规则\n{sec}" in m["system_prompt"], ct


async def test_inject_with_empty_rules_zero_error(sandbox):
    """规则为空：装配零报错、规则区不出现（fail-closed 注入）。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    sub = await _mk_typed(wid, root, "sub_project", "子项目")
    from src.storage import rule_store
    rule_store.write_sections(wid, {})
    main = context_assembler.build_context_bundle(wid, sub)["main"]
    assert "规则注入" not in [r["name"] for r in main["system_regions"]]
    assert "## 规则\n" not in main["system_prompt"]


async def test_inject_all_section_reaches_every_card_type(sandbox):
    """总规则进**所有**卡的上下文；本类区只进本类卡。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    await _seed_sections(wid, {"all": "总则Z", "sub_project": "子项S", "worker": "编码C"})
    for ct, expect, forbid in (("sub_project", "子项S", "编码C"),
                               ("worker", "编码C", "子项S")):
        cid = await _mk_typed(wid, root, ct, f"{ct}卡")
        main = context_assembler.build_context_bundle(wid, cid)["main"]
        regs = [r for r in main["system_regions"] if r["name"] == "规则注入"]
        assert regs, f"{ct} 应注入规则区"
        assert "总则Z" in regs[0]["text"], "总规则必须进所有卡"
        assert expect in regs[0]["text"], ct
        assert forbid not in regs[0]["text"], f"{ct} 不该拿到别类卡的规则"


# ---------------------------------------------------------------------------
# 块3 · 根卡审批通道（draft_rule_edit → 弹窗 → /rule/commit）
# ---------------------------------------------------------------------------


def test_base_prompt_rule_section():
    """提示词里要有「## 规则」节，且讲的是**流程**（规则是项目基建，不是卡）。"""
    from src.api import prompts
    t = prompts.BASE_PROMPT
    assert "## 项目规则" in t, "提示词要有「项目规则」节（与注入区「## 规则」区分开）"
    assert "draft_rule_edit" in t
    # [规则节点 2026-10-28] 规则＝一个项目一个文件；总规则进所有卡
    assert "rules.md" in t, "要写明载体（项目文件夹里的 rules.md）"
    assert "总规则" in t, "要写明总规则进所有卡"
    # 只讲流程，不讲「你不能」——流程式表述见 test_prompt_workflow_only
    assert "改规则的流程" in t


async def test_draft_rule_edit_toolface_root_only(sandbox):
    """硬判据2：根卡工具面含 draft_rule_edit；其余类型全无（第四段根卡闸）。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    cards = card_tools._collect_card_meta(wid)
    root_face = {t["name"] for t in
                 card_tools.build_tool_schemas(root, work_id=wid, cards=cards)}
    assert "draft_rule_edit" in root_face, "根卡应见 draft_rule_edit"
    # rd 词表类型全覆盖（topic 属 research 词表，rd work 建不出——六区覆盖见注入测试）
    for ct in ("sub_project", "worker", "tool", "rule"):
        cid = await _mk_typed(wid, root, ct, f"{ct}卡")
        cards = card_tools._collect_card_meta(wid)
        face = {t["name"] for t in
                card_tools.build_tool_schemas(cid, work_id=wid, cards=cards)}
        assert "draft_rule_edit" not in face, f"{ct} 卡不得见 draft_rule_edit"


async def test_rule_card_has_no_toolface(sandbox):
    """[2026-10-28 用户令「规则卡不挂模型和对话，不需要工具」]

    规则节点不挂模型 ⇒ 一件工具都没有。即使历史库里残留一张 card_type=rule 的卡
    （存量数据），它也不该拿到任何工具面（perms 空 ⇒ 默认关门）。
    """
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    # 绕过 API 直接落一张（模拟存量），验证工具面仍然为空
    legacy = await card_store.create_card(
        wid, "历史规则卡", "用户直接创建", card_type="rule", parent=root)
    assert "error" not in legacy, legacy
    rc = legacy["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    face = [t["name"] for t in
            card_tools.build_tool_schemas(rc, work_id=wid, cards=cards)]
    assert face == [], f"规则节点不该有任何工具：{face}"


async def test_draft_rule_edit_execution_gates(sandbox):
    """硬判据2/3 执行层：非根卡直调 → denied；无规则卡 → not_found；非法区 → validation。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    sub = await _mk_typed(wid, root, "sub_project", "子项目")
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, sub, "draft_rule_edit",
                                      {"section": "sub_project", "content": "x"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r
    # [规则=基建 2026-10-25] 规则随项目自带,根卡随时可起草(不再有 not_found)
    r = await card_tools.execute_tool(wid, root, "draft_rule_edit",
                                      {"section": "sub_project", "content": "x"}, cards)
    assert r["ok"] is True, r
    # 非法区名
    r = await card_tools.execute_tool(wid, root, "draft_rule_edit",
                                      {"section": "unknown_section", "content": "x"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "validation", r


async def test_draft_rule_edit_returns_payload_zero_write(sandbox):
    """硬判据3：返回 {draft, base_hash, section, old}，mission 逐字节零改动。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    from src.storage import work_store as _ws
    _ws.set_rules(wid, {"sub_project": "旧规则", "tool": "工具规则T"})
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, root, "draft_rule_edit",
                                      {"section": "sub_project",
                                       "content": "新提议"}, cards)
    assert r["ok"] is True, r
    d = r["data"]
    # [2026-11-01 多区起草] 返回形统一为 sections 数组（单区＝单元素）
    s0 = d["sections"][0]
    assert s0["section"] == "sub_project"
    assert s0["draft"] == "新提议"
    assert s0["old"] == "旧规则"
    assert s0["base_hash"] == _h("旧规则")
    # 起草零落盘:work.json rules 未变
    assert _ws.get_rules(wid)["sub_project"] == "旧规则"


async def test_rule_commit_approval_flow(client, sandbox):
    """硬判据4：draft → 用户「同意」→ commit 落盘＋影子行；其它区字节不变。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    from src.storage import work_store as _ws
    _ws.set_rules(wid, {"sub_project": "旧规则", "tool": "工具规则T",
                        "worker": "编码规则C"})
    cards = card_tools._collect_card_meta(wid)
    draft = await card_tools.execute_tool(
        wid, root, "draft_rule_edit",
        {"section": "sub_project", "content": "审批后规则"}, cards)
    assert draft["ok"] is True, draft
    d = draft["data"]["sections"][0]

    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": d["section"], "content": d["draft"],
              "base_hash": d["base_hash"]})
    assert r.status_code == 200, r.text
    assert r.json()["new_hash"] == _h("审批后规则")

    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert g["sections"]["sub_project"] == "审批后规则"
    assert g["sections"]["tool"] == "工具规则T"
    assert g["sections"]["worker"] == "编码规则C"
    # 其它区逐字节不变(work.json rules)
    rules = _ws.get_rules(wid)
    assert rules["tool"] == "工具规则T" and rules["worker"] == "编码规则C"


async def test_rule_commit_hash_conflict_409(client, sandbox):
    """硬判据4：base_hash 不符 → 409，卡面零改动。"""
    wid = await _mk_rd_work()
    from src.storage import work_store as _ws
    _ws.set_rules(wid, {"sub_project": "旧规则"})
    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": "sub_project", "content": "x", "base_hash": "bad_hash"})
    assert r.status_code == 409, r.status_code
    assert _ws.get_rules(wid)["sub_project"] == "旧规则"


async def test_rule_commit_gates_404_400(client, sandbox):
    """无规则卡 → 404；非法区名 → 400。"""
    wid = await _mk_rd_work()
    # [规则=基建 2026-10-25] 无"不存在"状态:hash 不符 → 409
    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": "sub_project", "content": "x", "base_hash": ""})
    assert r.status_code == 409, r.status_code
    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": "unknown_section", "content": "x", "base_hash": ""})
    assert r.status_code == 400, r.status_code


async def test_rule_commit_reject_notifies_root(client, sandbox):
    """[2026-11-01 死流程修复] commit 闸拒绝已批准草案时，不能只把错误
    留在审批弹窗——模型不在环永远不知道错因。判据：拒绝后根卡会话里
    有一条 actor=card:system 的会话块，带同一条拒绝原因与重起草指引；
    错误文本里带「已通知根卡重新起草」。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": "project", "content": "# 总规则\n## 一、根卡职责边界\nx",
              "base_hash": ""})
    assert r.status_code == 400, r.text
    body = r.json()
    _msg = body.get("detail") or body.get("error") or str(body)
    assert "已通知根卡重新起草" in _msg, _msg
    blocks = await session_facade.read(wid, root)
    sys_msgs = [b for b in blocks
                if str(b.get("actor") or "") == "card:system"
                and "提交闸拒绝" in str(b.get("content") or "")]
    assert len(sys_msgs) == 1, f"根卡应收到一条系统通知，实际 {len(sys_msgs)}"
    text = sys_msgs[0]["content"]
    assert "## 一、根卡职责边界" in text and "### " in text, text


async def test_draft_then_reject_zero_write(client, sandbox):
    """拒绝/关闭路径：不发 commit 请求即零落盘（后端侧实证：draft 后无人 commit）。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    rules_before = await _seed_sections(wid, {"sub_project": "旧规则"})
    cards = card_tools._collect_card_meta(wid)
    draft = await card_tools.execute_tool(
        wid, root, "draft_rule_edit",
        {"section": "sub_project", "content": "被拒绝的提议"}, cards)
    assert draft["ok"] is True
    # 只起草、不 commit（用户拒绝）→ 规则文件逐字节零改动
    from src.storage import rule_store
    assert rule_store.rules_path(wid).read_text(encoding="utf-8") == rules_before
    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert g["sections"]["sub_project"] == "旧规则"


def test_frontend_rule_approval_modal_wiring():
    """块3前端守卫（源码级，照 test_msg_baseclose_frontend 先例）：

    [2026-11-14 待决草案表] 触发链重造——弹窗不再押对话收口/timeline 扫描：
    真相源＝GET /rule 的 pending（服务端待决表，落库即记、处置即删）；
    触发＝工作级 SSE rule_draft_pending ＋ 页面加载查表 ＋ 处置完链下一条；
    处置＝同意 → POST /rule/commit 带 draft_id；拒绝/关闭 → POST /rule/dismiss
    带 draft_id。"""
    ws = (_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    body = _strip_js_comments(ws)
    dbl = body.replace("'", '"')
    # 同意 → commit（带草案身份 draft_id，服务端据此清待决表）
    assert "/rule/commit" in body and "submitRuleCommit" in body
    assert "draft_id" in body
    # 拒绝/关闭＝POST /rule/dismiss（待决表删除⇒永不再弹），但绝不打 commit
    assert "/rule/dismiss" in body and "dismissRuleDraft" in body
    i = body.index("function closeRuleModal()")
    j = body.index("dmRuleConfirm.onclick", i)
    close_body = body[i:j]
    assert "dismissRuleDraft" in close_body, "关窗须走处置"
    assert "/rule/commit" not in close_body, "关闭不得落盘规则"
    # 驳回要通知起草卡但不唤起；已试过落盘的关窗静默（commitAttempted→notify=false），
    # 落盘成功的关窗（committed）不再处置
    assert "dismissRuleDraft(!r.commitAttempted)" in close_body
    assert "notify" in body and "r.committed" in close_body
    # 审批弹窗只留 同意/拒绝（无反馈重起草），标题为规则修改审批
    assert 'dmRuleFeedback.style.display = "none"' in dbl
    assert "规则修改审批" in body
    # 弹窗数据源＝待决表 pending 条目：{draft_id, card_id, sections:[…]}
    for k in ("draft.sections", "draft.draft_id", "draft.card_id",
              "s.section", "s.old", "s.draft", "s.base_hash"):
        assert k in body, f"缺 {k}"
    # 新触发链：查待决表的 checkRulePending＋工作级 SSE 事件名
    assert "checkRulePending" in body
    assert '"rule_draft_pending"' in dbl
    assert "data.pending" in body
    # 旧机制全拆：无 timeline 扫描、无 seq 去重、无收口钩子
    assert "__maybeOpenRuleEditModal" not in body
    assert "ruleSeen" not in body
    assert 'd.tool !== "draft_rule_edit"' not in dbl
    # 处置链下一条：关窗/落盘后接着查（多条待决逐个批）
    assert "checkRulePending();" in close_body


async def test_rule_dismiss_records_and_notifies(client, sandbox, monkeypatch):
    """[2026-11-03 反复弹出修复] 用户「拒绝」⇒
    ① 驳回表记 card_id:seq（GET /rule 的 dismissed 下发给扫描过滤）；
    ② 起草卡会话收一封 actor=card:system 的驳回回执（落信）；
    ③ **不唤起**：deliver_now 一次都不被调（用户令「通知不唤起」）；
    ④ rules.md 逐字节零改动；⑤ 重复驳回幂等（不重复落信）。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    rules_before = await _seed_sections(wid, {"sub_project": "旧规则"})
    wakes: list = []
    async def _spy(*a, **kw):
        wakes.append((a, kw))
    monkeypatch.setattr(routes.message_runner, "deliver_now", _spy)

    r = await client.post(
        f"/api/works/{wid}/rule/dismiss",
        json={"card_id": root, "seq": 183, "sections": ["sub_project"]})
    assert r.status_code == 200, r.text
    assert r.json()["key"] == f"{root}:183" and r.json()["notified"] is True

    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert f"{root}:183" in g["dismissed"]

    blocks = await session_facade.read(wid, root)
    msgs = [b for b in blocks
            if str(b.get("actor") or "") == "card:system"
            and "已被用户拒绝" in str(b.get("content") or "")]
    assert len(msgs) == 1, f"应有一封驳回回执，实际 {len(msgs)}"
    assert "sub_project" in msgs[0]["content"], msgs[0]["content"]

    assert wakes == [], "驳回通知只落信不唤起，deliver_now 不得被调"

    from src.storage import rule_store
    assert rule_store.rules_path(wid).read_text(encoding="utf-8") == rules_before

    r2 = await client.post(
        f"/api/works/{wid}/rule/dismiss",
        json={"card_id": root, "seq": 183, "sections": ["sub_project"]})
    assert r2.status_code == 200 and r2.json()["notified"] is False
    blocks2 = await session_facade.read(wid, root)
    msgs2 = [b for b in blocks2
             if str(b.get("actor") or "") == "card:system"
             and "已被用户拒绝" in str(b.get("content") or "")]
    assert len(msgs2) == 1, "重复驳回不得重复落信"


async def test_rule_dismiss_silent_and_gates(client, sandbox, monkeypatch):
    """notify=false ⇒ 只落痕不落信（落盘失败后的关窗走这条）；
    坏 work → 404；缺草案身份（draft_id 与 card_id 都无）→ 400。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    wakes: list = []
    async def _spy(*a, **kw):
        wakes.append((a, kw))
    monkeypatch.setattr(routes.message_runner, "deliver_now", _spy)

    r = await client.post(
        f"/api/works/{wid}/rule/dismiss",
        json={"card_id": root, "seq": 7, "notify": False})
    assert r.status_code == 200 and r.json()["notified"] is False
    blocks = await session_facade.read(wid, root)
    assert not [b for b in blocks
                if "已被用户拒绝" in str(b.get("content") or "")], "静默驳回不得落信"
    assert wakes == []
    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert f"{root}:7" in g["dismissed"]

    r = await client.post("/api/works/nope/rule/dismiss",
                          json={"card_id": root, "seq": 1})
    assert r.status_code == 404
    r = await client.post(f"/api/works/{wid}/rule/dismiss",
                          json={"seq": 1})
    assert r.status_code == 400


async def test_rule_dismissal_store_roundtrip(sandbox):
    """驳回表单元级：add→list/is_dismissed 回环、幂等不改 ts、损坏文件拒覆写。"""
    from src.storage import rule_store

    wid = await _mk_rd_work()
    assert rule_store.list_dismissals(wid) == []
    assert rule_store.add_dismissal(wid, "card_x", 42, ["project"]) is True
    assert rule_store.add_dismissal(wid, "card_y", 9) is True
    got = rule_store.list_dismissals(wid)
    assert got == ["card_x:42", "card_y:9"]
    assert rule_store.is_dismissed(wid, "card_x", 42) is True
    assert rule_store.is_dismissed(wid, "card_x", 43) is False
    p = rule_store.dismissals_path(wid)
    first = p.read_text(encoding="utf-8")
    assert rule_store.add_dismissal(wid, "card_x", 42, ["other"]) is True
    assert p.read_text(encoding="utf-8") == first, "重复 key 不得改记录"
    # 损坏文件：不覆写别人的记录（返回 False）
    p.write_text("{不是json", encoding="utf-8")
    assert rule_store.add_dismissal(wid, "card_z", 1) is False
    assert rule_store.list_dismissals(wid) == []


async def test_pending_draft_lifecycle(client, sandbox):
    """[2026-11-14 待决草案表] 审批弹窗唯一真相源——
    ① draft_rule_edit 落库即记 ⇒ GET /rule 的 pending 带 draft_id+sections；
    ② dismiss(draft_id) ⇒ 删表＋起草卡收回执，pending 空；再驳幂等不落信；
    ③ commit(draft_id) ⇒ 落盘＋清 pending；
    ④ 基底过期（规则被别处改过）⇒ 死草案自动清出表、不进 pending。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    await _seed_sections(wid, {"sub_project": "旧规则"})
    cards = card_tools._collect_card_meta(wid)
    from src.storage import rule_store, work_store as _ws

    # ① 起草 ⇒ pending
    d1 = await card_tools.execute_tool(
        wid, root, "draft_rule_edit",
        {"section": "sub_project", "content": "提议A"}, cards)
    assert d1["ok"] is True, d1
    did1 = d1["data"]["draft_id"]
    g = (await client.get(f"/api/works/{wid}/rule")).json()
    pend = g["pending"]
    assert len(pend) == 1, f"应有一条待决，实际 {pend}"
    assert pend[0]["draft_id"] == did1 and pend[0]["card_id"] == root
    assert pend[0]["sections"][0]["draft"] == "提议A"

    # ② dismiss(draft_id) ⇒ 删表＋回执；再驳幂等
    r = await client.post(f"/api/works/{wid}/rule/dismiss",
                          json={"draft_id": did1})
    assert r.status_code == 200 and r.json()["resolved"] is True
    assert r.json()["notified"] is True
    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert g["pending"] == []
    r2 = await client.post(f"/api/works/{wid}/rule/dismiss",
                           json={"draft_id": did1})
    assert r2.json()["resolved"] is False and r2.json()["notified"] is False

    # ③ 再起草 ⇒ commit(draft_id) 落盘 ⇒ pending 清
    d2 = await card_tools.execute_tool(
        wid, root, "draft_rule_edit",
        {"section": "sub_project", "content": "提议B"}, cards)
    did2 = d2["data"]["draft_id"]
    s = d2["data"]["sections"][0]
    r = await client.post(f"/api/works/{wid}/rule/commit",
                          json={"draft_id": did2, "section": s["section"],
                                "content": s["draft"],
                                "base_hash": s["base_hash"]})
    assert r.status_code == 200, r.text
    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert g["pending"] == []
    assert g["sections"]["sub_project"] == "提议B"

    # ④ 基底过期：起草后规则被别处改 ⇒ 死草案自动死、顺手清出表
    d3 = await card_tools.execute_tool(
        wid, root, "draft_rule_edit",
        {"section": "sub_project", "content": "提议C"}, cards)
    did3 = d3["data"]["draft_id"]
    rules = dict(_ws.get_rules(wid))
    rules["sub_project"] = "被别处改了"
    _ws.set_rules(wid, rules)
    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert g["pending"] == [], "stale 草案不得进 pending"
    assert rule_store.get_draft(wid, did3) is None, "死草案应被清出待决表"


# ---------------------------------------------------------------------------
# 块4 · [规则节点 2026-10-28 · 用户令「一个项目一个文件」「项目创建的同时建起来」]
# ---------------------------------------------------------------------------


async def test_create_work_creates_rules_file(sandbox):
    """建项目 ⇒ 规则文件就在项目文件夹里（结构事实，不靠调用方记得）。

    用户原话：「规则卡应该是跟项目一起建立的，项目创建的同时，规则就被同步
    建起来了」「它应该进的是项目文件夹」。
    """
    from src.storage import rule_store
    w = await work_store.create_work("规则随项目", project_class="rd_project")
    wid = w["work_id"]
    p = rule_store.rules_path(wid)
    assert p.exists(), "建项目必须同时建出 rules.md"
    assert p.parent.name == wid, "规则文件必须在项目文件夹里"
    assert p.name == "rules.md"
    sections = rule_store.read_sections(wid)
    assert "all" in sections and sections["all"].strip(), "总规则要有默认内容"
    # 默认值里不许再出现已废的旧口径
    blob = "\n".join(sections.values())
    assert "总裁策展" not in blob, "默认规则不能写已废的「总裁策展」"
    assert "只追加不改" not in blob, "默认规则不能写「只追加不改」（块是快照）"


async def test_rules_file_is_project_folder_not_card(sandbox):
    """规则不占 card_id、不进 cards 表——它是文件，不是卡。"""
    from src.storage import card_db, rule_store
    w = await work_store.create_work("规则不是卡", project_class="rd_project")
    wid = w["work_id"]
    assert rule_store.rules_path(wid).exists()
    n_rule_cards = card_db.read_sync(
        wid, lambda c: c.execute(
            "SELECT count(*) AS n FROM cards WHERE card_type='rule'").fetchone()["n"], -1)
    assert n_rule_cards == 0, "规则文件不该产生 rule 卡"


async def test_legacy_work_json_rules_migrated_once(sandbox):
    """work.json 里的旧 rules（2026-10-25 ～ 10-28 的形态）首次读到就搬进文件。"""
    from src.storage import rule_store
    w = await work_store.create_work("旧规则迁移", project_class="rd_project")
    wid = w["work_id"]
    rule_store.rules_path(wid).unlink()          # 造出「只有 work.json 有规则」的旧状态
    data = work_store.read_work(wid) or {}
    data["rules"] = {"sub_project": "旧形态规则X"}
    work_store.write_work(wid, data)

    got = rule_store.read_sections(wid)
    assert got.get("sub_project") == "旧形态规则X", "旧内容不能丢"
    # 迁移后：再次 ensure 会把它落成文件
    assert rule_store.ensure(wid) is True
    assert rule_store.rules_path(wid).exists()
    assert "旧形态规则X" in rule_store.rules_path(wid).read_text(encoding="utf-8")


def test_frontend_draft_sections_mirror_matches_backend():
    """草稿态分区镜像必须与后端一致（前端硬编码，靠这条防漂移）。"""
    src = (_ROOT / "static" / "esm" / "rule_panel.js").read_text(encoding="utf-8")
    m = re.search(r"DRAFT_RULE_SECTIONS\s*=\s*\[(.*?)\]", src, re.S)
    assert m, "找不到 DRAFT_RULE_SECTIONS"
    mirror = re.findall(r'"([a-z_]+)"', m.group(1))
    backend = context_regions.rule_section_names(card_store.load_type_registry())
    assert mirror == backend, f"前端镜像 {mirror} ≠ 后端 {backend}"


def test_rule_store_labels_cover_all_sections():
    """分区显示名要覆盖全部合法分区（面板靠它显示中文名）。"""
    from src.storage import rule_store
    names = context_regions.rule_section_names(card_store.load_type_registry())
    missing = [n for n in names if n not in rule_store.SECTION_LABELS]
    assert not missing, f"缺显示名：{missing}"


# ---------------------------------------------------------------------------
# 块4 · [规则节点 2026-10-28 · 用户令「一个项目一个文件」「项目创建的同时建起来」]
# ---------------------------------------------------------------------------


async def test_create_work_creates_rules_file(sandbox):
    """建项目 ⇒ 规则文件就在项目文件夹里（结构事实，不靠调用方记得）。

    用户原话：「规则卡应该是跟项目一起建立的，项目创建的同时，规则就被同步
    建起来了」「它应该进的是项目文件夹」。
    """
    from src.storage import rule_store
    w = await work_store.create_work("规则随项目", project_class="rd_project")
    wid = w["work_id"]
    p = rule_store.rules_path(wid)
    assert p.exists(), "建项目必须同时建出 rules.md"
    assert p.parent.name == wid, "规则文件必须在项目文件夹里"
    assert p.name == "rules.md"
    sections = rule_store.read_sections(wid)
    assert "all" in sections and sections["all"].strip(), "总规则要有默认内容"
    # 默认值里不许再出现已废的旧口径
    blob = "\n".join(sections.values())
    assert "总裁策展" not in blob, "默认规则不能写已废的「总裁策展」"
    assert "只追加不改" not in blob, "默认规则不能写「只追加不改」（块是快照）"


async def test_rules_file_is_project_folder_not_card(sandbox):
    """规则不占 card_id、不进 cards 表——它是文件，不是卡。"""
    from src.storage import card_db, rule_store
    w = await work_store.create_work("规则不是卡", project_class="rd_project")
    wid = w["work_id"]
    assert rule_store.rules_path(wid).exists()
    n_rule_cards = card_db.read_sync(
        wid, lambda c: c.execute(
            "SELECT count(*) AS n FROM cards WHERE card_type='rule'").fetchone()["n"], -1)
    assert n_rule_cards == 0, "规则文件不该产生 rule 卡"


async def test_legacy_work_json_rules_migrated_once(sandbox):
    """work.json 里的旧 rules（2026-10-25 ～ 10-28 的形态）首次读到就搬进文件。"""
    from src.storage import rule_store
    w = await work_store.create_work("旧规则迁移", project_class="rd_project")
    wid = w["work_id"]
    rule_store.rules_path(wid).unlink()          # 造出「只有 work.json 有规则」的旧状态
    data = work_store.read_work(wid) or {}
    data["rules"] = {"sub_project": "旧形态规则X"}
    work_store.write_work(wid, data)

    got = rule_store.read_sections(wid)
    assert got.get("sub_project") == "旧形态规则X", "旧内容不能丢"
    # 迁移后：再次 ensure 会把它落成文件
    assert rule_store.ensure(wid) is True
    assert rule_store.rules_path(wid).exists()
    assert "旧形态规则X" in rule_store.rules_path(wid).read_text(encoding="utf-8")


def test_frontend_draft_sections_mirror_matches_backend():
    """草稿态分区镜像必须与后端一致（前端硬编码，靠这条防漂移）。"""
    src = (_ROOT / "static" / "esm" / "rule_panel.js").read_text(encoding="utf-8")
    m = re.search(r"DRAFT_RULE_SECTIONS\s*=\s*\[(.*?)\]", src, re.S)
    assert m, "找不到 DRAFT_RULE_SECTIONS"
    mirror = re.findall(r'"([a-z_]+)"', m.group(1))
    backend = context_regions.rule_section_names(card_store.load_type_registry())
    assert mirror == backend, f"前端镜像 {mirror} ≠ 后端 {backend}"


def test_rule_store_labels_cover_all_sections():
    """分区显示名要覆盖全部合法分区（面板靠它显示中文名）。"""
    from src.storage import rule_store
    names = context_regions.rule_section_names(card_store.load_type_registry())
    missing = [n for n in names if n not in rule_store.SECTION_LABELS]
    assert not missing, f"缺显示名：{missing}"
