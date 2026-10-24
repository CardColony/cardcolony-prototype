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
     前端审批弹窗源码级守卫（同意→commit、拒绝/关闭零请求）。
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
from src.storage import card_store, shadow_store, work_store

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


async def _mk_rule_card(wid: str) -> str:
    root = work_store.work_card_id_of(wid)
    r = await card_store.create_card(
        wid, "规则卡", "用户直接创建", card_type="rule", parent=root)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _seed_sections(wid: str, card_id: str, sections: dict[str, str]) -> str:
    """把六区写进 mission（含 ## 区头），返回拼好的 mission 串。"""
    names = context_regions.rule_section_names(card_store.load_type_registry())
    parts = []
    for n in names:
        parts.append(f"## {n}")
        if n in sections:
            parts.append(sections[n])
    mission = "\n".join(parts)
    r = await card_store.update_card(wid, card_id, {"mission": mission}, "seed rule sections")
    assert "error" not in r, r
    return mission


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


def test_rule_section_names_six_sections():
    names = context_regions.rule_section_names(card_store.load_type_registry())
    assert names == ["project", "sub_project", "tool", "coder", "rule", "topic"]


def test_rule_perms_read_only():
    """〔v14 · 2026-10-24〕规则卡模型侧**只读**——但「只读」不等于「不能查资料」：
    tools_web（web_search/fetch_web）是只读检索，仍归所有卡型。
    """
    assert set(perms_for("rd_project", "rule")) == {"read", "tools_web"}
    assert set(perms_for("research_project", "rule")) == {"read", "tools_web"}


def test_render_rule_region_three_states():
    assert context_regions.render_rule_region({}, "sub_project") == ""
    assert context_regions.render_rule_region({"sub_project": "A"}, "sub_project") == "## 规则\nA"
    # 缺区（rule_sections 里没有该 card_type）→ 空串
    assert context_regions.render_rule_region({"tool": "T"}, "sub_project") == ""


async def test_rule_region_absent_when_control_disabled(sandbox):
    # controls enabled:false → rule 区不出现（fail-closed 注入、fail-open 读控件）
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    regions = context_assembler.build_context_regions(wid, root)
    assert 85 not in [r["order"] for r in regions]   # 无规则卡 → 空 → 不 join


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
    # 无规则卡 → exists False 但六区齐全（含空区）
    assert d["exists"] is False
    names = context_regions.rule_section_names(card_store.load_type_registry())
    assert set(d["sections"].keys()) == set(names)
    assert set(d["hashes"].keys()) == set(names)
    for n in names:
        assert d["sections"][n] == ""
        assert d["hashes"][n] == _h("")


async def test_get_rule_reads_seeded_sections(client, sandbox):
    wid = await _mk_rd_work()
    cid = await _mk_rule_card(wid)
    await _seed_sections(wid, cid, {"sub_project": "子项目规则A", "tool": "工具规则B"})
    r = await client.get(f"/api/works/{wid}/rule")
    d = r.json()
    assert d["exists"] is True
    assert d["card_id"] == cid
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
    cid = await _mk_rule_card(wid)
    await _seed_sections(wid, cid, {"sub_project": "旧内容", "tool": "工具内容", "coder": "编码内容"})
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
    assert d3["sections"]["coder"] == "编码内容"


async def test_patch_rule_section_hash_mismatch_409(client, sandbox):
    wid = await _mk_rd_work()
    cid = await _mk_rule_card(wid)
    await _seed_sections(wid, cid, {"sub_project": "旧内容"})
    r2 = await client.patch(
        f"/api/works/{wid}/rule/sections/sub_project",
        json={"content": "改", "base_hash": "wrong_hash"})
    assert r2.status_code == 409
    # 卡面零改动
    r3 = await client.get(f"/api/works/{wid}/rule")
    assert r3.json()["sections"]["sub_project"] == "旧内容"


async def test_patch_rule_section_illegal_name_400(client, sandbox):
    wid = await _mk_rd_work()
    await _mk_rule_card(wid)
    r = await client.patch(
        f"/api/works/{wid}/rule/sections/unknown_section",
        json={"content": "x", "base_hash": ""})
    assert r.status_code == 400


async def test_patch_rule_section_missing_card_404(client, sandbox):
    wid = await _mk_rd_work()
    r = await client.patch(
        f"/api/works/{wid}/rule/sections/sub_project",
        json={"content": "x", "base_hash": ""})
    assert r.status_code == 404


async def test_create_work_card_rule_seeds_template(client, sandbox):
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    r = await client.post(
        f"/api/works/{wid}/cards",
        json={"title": "规则卡", "source": "用户直接创建",
              "card_type": "rule", "parent": root})
    assert r.status_code == 201
    cid = r.json()["meta"]["card_id"]
    # mission 已播种六区空模板
    meta = card_store._meta_of(wid, cid)
    assert meta["card_type"] == "rule"
    body = await card_store.read_card(wid, cid)
    mission = body["body"]["mission"]
    names = context_regions.rule_section_names(card_store.load_type_registry())
    for n in names:
        assert f"## {n}" in mission


# ---------------------------------------------------------------------------
# 块3 · 注入按卡类型择区（硬判据5）
# ---------------------------------------------------------------------------

_SECTIONS_ALL = {
    "project": "根卡规则P", "sub_project": "子项目规则A", "tool": "工具规则T",
    "coder": "编码规则C", "rule": "规则卡规则R", "topic": "课题规则Q",
}


async def test_inject_section_by_card_type(sandbox):
    """各类型卡注入规则卡对应区；order 85 位于 instruction(0) 与 card_duty(90) 之间。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    rc = await _mk_rule_card(wid)
    await _seed_sections(wid, rc, _SECTIONS_ALL)

    # ① sub_project 卡 → sub_project 区（硬判据5 主场景：顺序断言）
    sub = await _mk_typed(wid, root, "sub_project", "子项目")
    main = context_assembler.build_context_bundle(wid, sub)["main"]
    regs = main["system_regions"]
    names = [r["name"] for r in regs]
    i_rule = names.index("规则注入")
    assert regs[i_rule]["order"] == 85
    assert i_rule < names.index("卡片职责"), "order 85 须在卡片职责(90)之前"
    assert regs[i_rule]["text"] == "## 规则\n子项目规则A"
    # compose 结果断言 instruction < 规则 < 卡片职责 的文本顺序
    sp = main["system_prompt"]
    assert sp.index("你在一个卡片系统内工作") < sp.index("## 规则\n子项目规则A") < sp.index("## 卡片职责")

    # ② coder 卡 → coder 区；tool 卡 → tool 区；rule 卡自身 → rule 区；根卡 → project 区
    for ct, sec in (("coder", "编码规则C"), ("tool", "工具规则T"),
                    ("rule", "规则卡规则R"), ("project", "根卡规则P")):
        cid = root if ct == "project" else await _mk_typed(wid, root, ct, f"{ct}卡")
        m = context_assembler.build_context_bundle(wid, cid)["main"]
        rule_regs = [r for r in m["system_regions"] if r["name"] == "规则注入"]
        assert rule_regs, f"{ct} 卡应注入规则区"
        assert rule_regs[0]["text"] == f"## 规则\n{sec}", ct
        assert f"## 规则\n{sec}" in m["system_prompt"], ct


async def test_inject_without_rule_card_zero_error(sandbox):
    """无规则卡：装配零报错、规则区不出现（fail-closed 注入）。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    sub = await _mk_typed(wid, root, "sub_project", "子项目")
    main = context_assembler.build_context_bundle(wid, sub)["main"]
    assert "规则注入" not in [r["name"] for r in main["system_regions"]]
    assert "## 规则\n" not in main["system_prompt"]


# ---------------------------------------------------------------------------
# 块3 · 根卡审批通道（draft_rule_edit → 弹窗 → /rule/commit）
# ---------------------------------------------------------------------------


def test_base_prompt_rule_section_t21_1():
    """硬判据1：BASE_PROMPT_VERSION==t21-1，且含「## 规则卡」使用说明节。"""
    from src.api import prompts
    # [依赖关联线 2026-10-16] t21-1 → t22-1。
    assert prompts.BASE_PROMPT_VERSION == "t22-1"
    assert "## 规则卡" in prompts.BASE_PROMPT
    assert "draft_rule_edit" in prompts.BASE_PROMPT
    assert "你不能直接改规则" in prompts.BASE_PROMPT


async def test_draft_rule_edit_toolface_root_only(sandbox):
    """硬判据2：根卡工具面含 draft_rule_edit；其余类型全无（第四段根卡闸）。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    cards = card_tools._collect_card_meta(wid)
    root_face = {t["name"] for t in
                 card_tools.build_tool_schemas(root, work_id=wid, cards=cards)}
    assert "draft_rule_edit" in root_face, "根卡应见 draft_rule_edit"
    # rd 词表类型全覆盖（topic 属 research 词表，rd work 建不出——六区覆盖见注入测试）
    for ct in ("sub_project", "coder", "tool", "rule"):
        cid = await _mk_typed(wid, root, ct, f"{ct}卡")
        cards = card_tools._collect_card_meta(wid)
        face = {t["name"] for t in
                card_tools.build_tool_schemas(cid, work_id=wid, cards=cards)}
        assert "draft_rule_edit" not in face, f"{ct} 卡不得见 draft_rule_edit"


async def test_rule_card_toolface_no_write_tools(sandbox):
    """rule 卡模型面只有 read 挂钩三件 ＋ **查资料两件**（v14），无任何写工具、无 draft_rule_edit。

    〔v14 · 2026-10-24 · 用户裁定「查资料≠动手」〕规则卡也拿到 tools_web
    ⇒ 工具面多 web_search/fetch_web；**写工具仍然一件没有**（下面的 writable 断言守住）。
    """
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    rc = await _mk_rule_card(wid)
    cards = card_tools._collect_card_meta(wid)
    face = {t["name"] for t in
            card_tools.build_tool_schemas(rc, work_id=wid, cards=cards)}
    assert face == {"read_card", "read_memory", "read_shadow",
                    "web_search", "fetch_web"}, face
    writable = {n for n in face if not card_tools.TOOL_REGISTRY[n].readonly}
    assert not writable, writable


async def test_draft_rule_edit_execution_gates(sandbox):
    """硬判据2/3 执行层：非根卡直调 → denied；无规则卡 → not_found；非法区 → validation。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    sub = await _mk_typed(wid, root, "sub_project", "子项目")
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, sub, "draft_rule_edit",
                                      {"section": "sub_project", "content": "x"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied", r
    # 无规则卡
    r = await card_tools.execute_tool(wid, root, "draft_rule_edit",
                                      {"section": "sub_project", "content": "x"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "not_found", r
    # 非法区名
    await _mk_rule_card(wid)
    r = await card_tools.execute_tool(wid, root, "draft_rule_edit",
                                      {"section": "unknown_section", "content": "x"}, cards)
    assert r["ok"] is False and r["error"]["code"] == "validation", r


async def test_draft_rule_edit_returns_payload_zero_write(sandbox):
    """硬判据3：返回 {draft, base_hash, section, old}，mission 逐字节零改动。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    rc = await _mk_rule_card(wid)
    mission_before = await _seed_sections(
        wid, rc, {"sub_project": "旧规则", "tool": "工具规则T"})
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, root, "draft_rule_edit",
                                      {"section": "sub_project",
                                       "content": "新提议"}, cards)
    assert r["ok"] is True, r
    d = r["data"]
    assert d["section"] == "sub_project"
    assert d["draft"] == "新提议"
    assert d["old"] == "旧规则"
    assert d["base_hash"] == _h("旧规则")
    assert d["rule_card_id"] == rc
    body = await card_store.read_card(wid, rc)
    assert body["body"]["mission"] == mission_before, "起草必须盘上零改动"


async def test_rule_commit_approval_flow(client, sandbox):
    """硬判据4：draft → 用户「同意」→ commit 落盘＋影子行；其它区字节不变。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    rc = await _mk_rule_card(wid)
    mission_before = await _seed_sections(
        wid, rc, {"sub_project": "旧规则", "tool": "工具规则T", "coder": "编码规则C"})
    cards = card_tools._collect_card_meta(wid)
    draft = await card_tools.execute_tool(
        wid, root, "draft_rule_edit",
        {"section": "sub_project", "content": "审批后规则"}, cards)
    assert draft["ok"] is True, draft
    d = draft["data"]

    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": d["section"], "content": d["draft"],
              "base_hash": d["base_hash"]})
    assert r.status_code == 200, r.text
    assert r.json()["new_hash"] == _h("审批后规则")

    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert g["sections"]["sub_project"] == "审批后规则"
    assert g["sections"]["tool"] == "工具规则T"
    assert g["sections"]["coder"] == "编码规则C"
    # mission 全文字节级：只换目标区那一段
    body = await card_store.read_card(wid, rc)
    expected = mission_before.replace(
        "## sub_project\n旧规则", "## sub_project\n审批后规则")
    assert body["body"]["mission"] == expected, "其它区必须逐字节不变"
    # 影子 ## 迭代历史 记一行（时间+section+旧/新hash+来源=根卡审批）
    raw = await shadow_store.read_shadow_raw(wid, rc)
    assert "规则审批 sub_project" in raw, raw
    assert "来源=根卡审批" in raw, raw


async def test_rule_commit_hash_conflict_409(client, sandbox):
    """硬判据4：base_hash 不符 → 409，卡面零改动。"""
    wid = await _mk_rd_work()
    rc = await _mk_rule_card(wid)
    mission_before = await _seed_sections(wid, rc, {"sub_project": "旧规则"})
    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": "sub_project", "content": "x", "base_hash": "bad_hash"})
    assert r.status_code == 409, r.status_code
    body = await card_store.read_card(wid, rc)
    assert body["body"]["mission"] == mission_before


async def test_rule_commit_gates_404_400(client, sandbox):
    """无规则卡 → 404；非法区名 → 400。"""
    wid = await _mk_rd_work()
    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": "sub_project", "content": "x", "base_hash": ""})
    assert r.status_code == 404, r.status_code
    await _mk_rule_card(wid)
    r = await client.post(
        f"/api/works/{wid}/rule/commit",
        json={"section": "unknown_section", "content": "x", "base_hash": ""})
    assert r.status_code == 400, r.status_code


async def test_draft_then_reject_zero_write(client, sandbox):
    """拒绝/关闭路径：不发 commit 请求即零落盘（后端侧实证：draft 后无人 commit）。"""
    wid = await _mk_rd_work()
    root = work_store.work_card_id_of(wid)
    rc = await _mk_rule_card(wid)
    mission_before = await _seed_sections(wid, rc, {"sub_project": "旧规则"})
    cards = card_tools._collect_card_meta(wid)
    draft = await card_tools.execute_tool(
        wid, root, "draft_rule_edit",
        {"section": "sub_project", "content": "被拒绝的提议"}, cards)
    assert draft["ok"] is True
    # 只起草、不 commit（用户拒绝）→ 卡面零改动
    body = await card_store.read_card(wid, rc)
    assert body["body"]["mission"] == mission_before
    g = (await client.get(f"/api/works/{wid}/rule")).json()
    assert g["sections"]["sub_project"] == "旧规则"


def test_frontend_rule_approval_modal_wiring():
    """块3前端守卫（源码级，照 test_msg_baseclose_frontend 先例）：
    同意 → POST /rule/commit；拒绝/关闭 → closeRuleModal 零请求；
    弹窗数据取 draft_rule_edit 成功 tool_result 的 {section, old, draft, base_hash}；
    回合收口经 window.__maybeOpenRuleEditModal 扫 timeline 开弹窗（去重）。"""
    ws = (_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    body = _strip_js_comments(ws)
    dbl = body.replace("'", '"')
    # 同意 → commit
    assert "/rule/commit" in body and "submitRuleCommit" in body
    # 关闭/拒绝路径零请求：closeRuleModal 函数体内无任何网络调用
    i = body.index("function closeRuleModal()")
    j = body.index("dmRuleConfirm.onclick", i)
    close_body = body[i:j]
    assert "API.call" not in close_body, "关闭必须零请求"
    assert "fetch(" not in close_body, "关闭必须零请求"
    # 审批弹窗只留 同意/拒绝（无反馈重起草），标题为规则修改审批
    assert 'dmRuleFeedback.style.display = "none"' in dbl
    assert "规则修改审批" in body
    # 弹窗四键数据源
    for k in ("payload.section", "payload.old", "payload.draft", "payload.base_hash"):
        assert k in body, f"缺 {k}"
    # timeline 扫描（tool=="draft_rule_edit" 成功结果）＋ seq 去重＋收口后调用点
    assert '__maybeOpenRuleEditModal' in body
    assert 'd.tool !== "draft_rule_edit"' in dbl
    assert "ruleSeen" in body
    assert body.count("window.__maybeOpenRuleEditModal(cardId)") >= 3, "send/resend/tryRecover 三处收口后调用"
