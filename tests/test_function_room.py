# -*- coding: utf-8 -*-
"""[预设职能 2026-10-24] 职能目录 / 播种 / 身份注入 / 台账卡面的测试。

用户裁定:职能从「根卡自由组建」改为「目录预设」——总裁(根卡兼职能卡)、
产品经理、技术总监、测试验收总监、涌现守门,职责书/管辖/盯线/工具面/权限/
卡面区块全部由 ``config/function_catalog.json`` 定义(唯一事实源)。

本文件钉五件事:
T1 目录加载与 fail-open(读不到 ⇒ disabled,旧世界行为)
T2 播种幂等(总裁档案落根卡 id,其余建 fn 卡;二次播种全跳过)
T3 145 区身份按角色分界(总裁统筹执笔 vs 普通职能只盯不干;scope 盯线显示)
T4 职能卡面=台账动态拼装(职责书/管辖/指令簿；观点簿/警报簿 2026-10-31 退役)
T5 生产链路接线(build_context_bundle 带 function_meta ⇒ 145 区真渲染;
   POST /works 路由播种;存量 work 手动播种端点)

隔离:conftest 已把 FUNCTION_CATALOG_PATH 指向「关着」的临时文件——
要测播种的用例自己指回仓库真目录。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import card_tools, function_room
from src.api import context_assembler, context_regions
from src.api import routes as api_routes
from src.storage import card_store, function_store, work_store

REPO_CATALOG = function_room._DEFAULT_PATH


def _use_repo_catalog(monkeypatch) -> None:
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(REPO_CATALOG))
    function_room.reset_cache()


# ---------------------------------------------------------------- T1 目录

def test_catalog_loads_and_shape(sandbox, monkeypatch):
    _use_repo_catalog(monkeypatch)
    cat = function_room.load()
    assert cat["enabled"] is True
    # [2026-10-28 用户令「预设之外,还能创建」] 自由组建恢复：预设是起点不是边界
    assert cat["free_form_enabled"] is True
    codes = {e["code"] for e in cat["functions"]}
    assert codes == {"ceo", "pm", "cto", "qa", "emergence_keeper"}
    ceo = function_room.entry("ceo")
    assert ceo["lands_on"] == "root"
    # 其余四个都建独立卡
    assert all(function_room.entry(c)["lands_on"] == "card"
               for c in codes - {"ceo"})
    # 通用契约:职责书不得携带具体任务词(用户令:不因任务定制失去通用性)
    for e in cat["functions"]:
        for bad in ("人力", "预算", "人月", "Cardlo"):
            assert bad not in e["charter"], (e["code"], bad)


def test_catalog_fail_open(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH,
                       str(tmp_path / "nope.json"))
    function_room.reset_cache()
    cat = function_room.load()
    assert cat["enabled"] is False and cat["functions"] == []
    # 坏 JSON 同样当没有
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(bad))
    function_room.reset_cache()
    assert function_room.load()["enabled"] is False


# ---------------------------------------------------------------- T2 播种

async def test_seed_work_ceo_on_root_and_fn_cards(sandbox, monkeypatch):
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work("播种测试")
    assert "error" not in wdata
    wid = wdata["work_id"]
    root_id = work_store.work_card_id_of(wid)

    out = await function_room.seed_work(wid)
    assert out["enabled"] is True and not out["failed"], out
    seeded = {it["code"]: it["function_id"] for it in out["seeded"]}
    assert set(seeded) == {"ceo", "pm", "cto", "qa", "emergence_keeper"}
    # 总裁档案落在根卡 id 上(职能化),不占 fn 号段
    assert seeded["ceo"] == root_id
    ceo_prof = function_store.get_profile(wid, root_id)
    assert ceo_prof and ceo_prof["function_name"] == "总裁"
    assert function_room.catalog_code_of(ceo_prof) == "ceo"
    # 四张独立职能卡进了 cards 表(card_type='function')
    metas = card_tools._collect_card_meta(wid)
    fn_ids = [cid for cid, m in metas.items()
              if str(m.get("card_type") or "") == "function"]
    assert len(fn_ids) == 4
    # 幂等:再播一遍全部跳过,不重复建
    out2 = await function_room.seed_work(wid)
    assert out2["seeded"] == [] and len(out2["skipped"]) == 5


async def test_seed_work_disabled_catalog_noop(sandbox):
    # conftest 已指向关着的目录 ⇒ no-op 且不报错
    wdata = await work_store.create_work("关闸播种")
    wid = wdata["work_id"]
    out = await function_room.seed_work(wid)
    assert out["enabled"] is False and out["seeded"] == []
    assert function_store.list_profiles(wid) == []


# ---------------------------------------------------------------- T3 145 区

def _ctx_with(meta: dict) -> "context_regions.RegionCtx":
    return context_regions.RegionCtx(work_id="work_001", card_id="card_001",
                                     function_meta=meta)


def test_self_region_ceo_boundary_differs(sandbox):
    ceo_meta = {
        "function_id": "card_001", "function_name": "总裁",
        "charter": {"text": "统筹决策与对用户汇报"},
        "jurisdiction": {"catalog_code": "ceo", "domain": "全局统筹",
                         "scope": "all"},
    }
    txt = context_regions.render_function_self_region(_ctx_with(ceo_meta))
    assert "你的职能" in txt and "总裁" in txt
    assert "统筹决策" in txt                      # 总裁边界:统筹执笔
    assert "只盯" not in txt                      # 不是普通职能的边界文案

    pm_meta = {
        "function_id": "fn_002", "function_name": "产品经理",
        "charter": {"text": "需求与前提的持有者"},
        "jurisdiction": {"catalog_code": "pm", "scope": "all"},
    }
    txt2 = context_regions.render_function_self_region(_ctx_with(pm_meta))
    # [两源模型 2026-11-01] 边界句＝三种在场通道；watch/speak_contract
    # 字段是自动唤醒时代的死档案，不再渲染。
    assert "不干具体的活" in txt2
    assert "差事派你" in txt2 and "评审叫你" in txt2 and "用户直接找你" in txt2
    assert "只盯" not in txt2 and "发言契约" not in txt2 and "按范围" not in txt2


def test_self_region_injects_function_rules_sections(sandbox):
    """[两源模型 2026-11-01] QA 的两个事实源之一：rules.md 的
    `## function`（全体）＋`## function.<code>`（只对它）注入 145 区。"""
    qa_meta = {
        "function_id": "fn_qa", "function_name": "验收",
        "charter": {"text": "验产物"},
        "jurisdiction": {"catalog_code": "qa", "scope": "all"},
    }
    ctx = _ctx_with(qa_meta)
    ctx.rule_sections = {
        "function": "通用纪律：结论落场卡面",
        "function.qa": "验收准则：按需求文档逐条量",
        "function.pm": "PM 的前提纪律",
    }
    txt = context_regions.render_function_self_region(ctx)
    assert "你的规则" in txt
    assert "通用纪律" in txt                       # function 共享区
    assert "验收准则" in txt                       # function.qa 专属区
    assert "PM 的前提纪律" not in txt              # 别家职能的区不灌进来
    # 没挂规则的职能不出「你的规则」块
    ctx2 = _ctx_with(qa_meta)
    assert "你的规则" not in context_regions.render_function_self_region(ctx2)


def test_self_region_empty_meta_no_region(sandbox):
    assert context_regions.render_function_self_region(
        _ctx_with({})) == ""
    assert context_regions.render_function_self_region(
        context_regions.RegionCtx()) == ""


# ---------------------------------------------------------------- T4 卡面块

async def test_function_summary_blocks(sandbox):
    """[两源模型 2026-11-01 改写] 职能面＝身份块+差事账，一切私记台账区块退役。
    「指令簿」是 function_directives 派工留痕（机械工作记录，非事实源），保留。"""
    wdata = await work_store.create_work("职能卡面")
    wid = wdata["work_id"]
    prof = await function_store.create_function(
        wid, "测试职能",
        {"charter": {"version": 1, "text": "对什么负责:测试职责书"},
         "jurisdiction": {"domain": "测试域", "scope": "all"}})
    fid = prof["function_id"]
    s = function_room.summary(wid, fid)
    assert s["function_name"] == "测试职能"
    for sec in ("职责书", "管辖", "指令簿"):
        assert f"## {sec}" in s["text"], sec   # 非目录职能：旧默认块
    assert "测试职责书" in s["text"]
    for dead in ("观点簿", "警报簿", "决策簿", "汇报簿", "前提台账"):
        assert dead not in s["text"], dead
    assert "open_alarms" not in s["counts"]


# ---------------------------------------------------------------- T5 接线

async def test_bundle_renders_self_region_for_function_card(sandbox, monkeypatch):
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work("身份注入")
    wid = wdata["work_id"]
    root_id = work_store.work_card_id_of(wid)
    await function_room.seed_work(wid)

    # 根卡(总裁)的上下文应含 145 区:身份来自播种的 ceo 档案
    bundle = context_assembler.build_context_bundle(
        wid, root_id, function_meta=function_room.function_meta_for(wid, root_id))
    assert "你的职能" in bundle["main"]["system_prompt"]
    assert "总裁" in bundle["main"]["system_prompt"]
    # 不传 function_meta ⇒ 区不出现(fail-open,与旧世界一致)
    bundle2 = context_assembler.build_context_bundle(wid, root_id)
    assert "你的职能" not in bundle2["main"]["system_prompt"]


async def test_create_work_route_seeds(sandbox, monkeypatch):
    _use_repo_catalog(monkeypatch)
    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        resp = client.post("/api/works", json={"title": "路由播种"})
        assert resp.status_code == 201
        data = resp.json()
    assert data["functions_seeded"]["enabled"] is True
    assert len(data["functions_seeded"]["seeded"]) == 5
    wid = data["work_id"]
    ceo_prof = function_store.get_profile(wid, work_store.work_card_id_of(wid))
    assert ceo_prof and ceo_prof["function_name"] == "总裁"


async def test_seed_endpoint_for_existing_work(sandbox, monkeypatch):
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work("存量播种")
    wid = wdata["work_id"]
    app = FastAPI()
    app.include_router(api_routes.router)
    with TestClient(app) as client:
        resp = client.post(f"/api/works/{wid}/functions/seed")
        assert resp.status_code == 200
        out = resp.json()
    assert len(out["seeded"]) == 5
    # 再播 ⇒ 全跳过(幂等)
    with TestClient(app) as client:
        out2 = client.post(f"/api/works/{wid}/functions/seed").json()
    assert out2["seeded"] == [] and len(out2["skipped"]) == 5


async def test_read_function_card_returns_identity_face(sandbox, monkeypatch):
    """[两源模型 2026-11-01 改写] 读职能卡＝身份面（职责书/管辖/差事块），无台账。"""
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work("职能读卡")
    wid = wdata["work_id"]
    root_id = work_store.work_card_id_of(wid)
    await function_room.seed_work(wid)
    fn_metas = {cid: m for cid, m in card_tools._collect_card_meta(wid).items()
                if str(m.get("card_type") or "") == "function"}
    fid = sorted(fn_metas)[0]
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_read_card(wid, root_id, {"card_id": fid}, cards)
    assert r.get("card_type") == "function"
    assert "function" in r and "职责书" in r["text"]
    for dead in ("观点簿", "决策簿", "汇报簿", "台账"):
        assert dead not in r["text"]


# ---------------------------------------------------------------- P-A 工具面

async def _seeded_work(monkeypatch, title: str = "工具面") -> tuple[str, dict]:
    _use_repo_catalog(monkeypatch)
    wdata = await work_store.create_work(title)
    wid = wdata["work_id"]
    await function_room.seed_work(wid)
    return wid, card_tools._collect_card_meta(wid)


async def test_function_toolface_catalog_deny(sandbox, monkeypatch):
    """目录 deny 生效:预设职能不发信不派工(send_card_message/create_card 摘除),
    基础职能件(sync/approve)保留；判断/警报三件、write_ledger 均已退役。"""
    wid, cards = await _seeded_work(monkeypatch)
    fn_ids = [cid for cid, m in cards.items()
              if str(m.get("card_type") or "") == "function"]
    assert len(fn_ids) == 4
    tools = card_tools.build_tool_schemas(card_id=fn_ids[0], work_id=wid,
                                          cards=cards)
    names = {str(t.get("name")) for t in tools}
    # [四场景 2026-10-25] deny 只剩 curate_face(总裁执笔权);
    # 通信走场景件(sync),send_card_message 全局退役。
    assert "curate_face" not in names
    assert "send_card_message" not in names      # 全局退役
    # [伴生评审卡 2026-10-27] 评审换代：review_request/submit_review 下线，
    # 职能改为在评审卡上调 approve 表态。
    assert "review_request" not in names and "submit_review" not in names
    # [2026-10-31 用户令] 判断/警报退役：三件不再进任何工具面
    # [2026-11-01 两源模型] write_ledger 退役：私账不再进任何工具面
    assert {"sync", "approve"} <= names
    assert not ({"record_verdict", "raise_alarm", "correct_alarm",
                "write_ledger"} & names)


async def test_legacy_function_keeps_old_toolface(sandbox):
    """非目录职能(work_025 存量形态:档案无 catalog_code)⇒ 基础面逐字不变。"""
    wdata = await work_store.create_work("存量职能")
    wid = wdata["work_id"]
    prof = await function_store.create_function(
        wid, "自由组建的老职能", {"charter": {"version": 1, "text": "旧职责"}})
    fid = prof["function_id"]
    await card_tools.ensure_function_card(wid, fid, "自由组建的老职能")
    cards = card_tools._collect_card_meta(wid)
    tools = card_tools.build_tool_schemas(card_id=fid, work_id=wid, cards=cards)
    names = {str(t.get("name")) for t in tools}
    assert "send_card_message" not in names      # [四场景] 全局退役(老职能同)


async def test_root_can_create_function_beyond_presets(sandbox, monkeypatch, tmp_path):
    """[2026-10-28 用户令「预设之外,还能创建」]

    改前：目录 `free_form_enabled=false` ⇒ 建职能四件对**包括根卡在内**的所有卡收走，
    职能只能来自预设。现在放开：根卡能建预设之外的职能；ceo 条目的 allow 工具
    仍在注册后自动追加。
    """
    wid, cards = await _seeded_work(monkeypatch)
    root_id = work_store.work_card_id_of(wid)
    tools = card_tools.build_tool_schemas(card_id=root_id, work_id=wid, cards=cards)
    names = {str(t.get("name")) for t in tools}
    assert "create_function" in names, "根卡要能建预设之外的职能"
    assert "list_functions" in names

    # 假目录验证总裁追加机制:ceo allow 一件已注册工具(approve)⇒ 根卡面出现
    fake = tmp_path / "fake_catalog.json"
    fake.write_text(__import__("json").dumps({
        "_meta": {"version": 1, "seed_on_create": True,
                  "free_form_enabled": False},
        "functions": [
            {"code": "ceo", "name": "总裁", "lands_on": "root",
             "charter": "统筹", "jurisdiction": {"scope": "all"},
             "watch": {"scope": "all", "cards": []},
             "toolface": {"allow": ["approve"], "deny": []}},
        ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(fake))
    function_room.reset_cache()
    await function_room.seed_work(wid)            # 幂等:只补 ceo(pm 等已 skipped)
    tools2 = card_tools.build_tool_schemas(card_id=root_id, work_id=wid,
                                           cards=cards)
    names2 = {str(t.get("name")) for t in tools2}
    assert "approve" in names2                    # ceo allow 追加生效


def test_team_function_region_retired_when_catalog_on(sandbox, monkeypatch, tmp_path):
    """91 区(自由拆职能引导)在目录启用时退役;目录关着 ⇒ 旧文逐字回来。"""
    _use_repo_catalog(monkeypatch)
    ctx = context_regions.RegionCtx(
        work_card_id="card_001", card_id="card_001",
        cur_meta={"card_id": "card_001", "auto_mode": 1, "goal_rounds": 0})
    assert context_regions.render_team_function_instruction_region(ctx) == ""

    off = tmp_path / "off_catalog.json"
    off.write_text('{"seed_on_create": false, "functions": []}', encoding="utf-8")
    monkeypatch.setenv(function_room.ENV_CATALOG_PATH, str(off))
    function_room.reset_cache()
    assert context_regions.render_team_function_instruction_region(ctx) == \
        context_regions._TEAM_FUNCTION_INSTRUCTION_TEXT


# ---------------------------------------------------------------- P-B 上下文

def test_child_status_full_and_coder_skipped(sandbox):
    """[用户令] 现状块全文注入不截断(覆盖写·单一最新,块本身有界);
    coder 子卡没有现状块,不进本区。"""
    mk = context_regions.render_direct_child_status_region
    short = mk([{"card_id": "card_002", "title": "短卡",
                 "status_block": "已交付 A,等验收"}])
    assert "已交付 A,等验收" in short and "截断" not in short

    long_status = "x" * 5000
    out = mk([{"card_id": "card_004", "title": "长卡",
               "status_block": long_status}])
    assert out.count("x") == 5000                # 全文,一个字不砍
    assert "截断" not in out
    # coder 子卡：不给「现状」（它没有现状块），但活性与使命照给——
    # [2026-11-02 用户令] 父卡要知道编程卡跑没跑。
    out2 = mk([{"card_id": "card_009", "title": "编程卡",
                "status_block": "有东西也不注入", "card_type": "coder",
                "mission": "写排序模块", "activity": "运行中 · 已跑 3 轮"}])
    assert "### card_009 · 编程卡" in out2 and "运行中 · 已跑 3 轮" in out2
    assert "写排序模块" in out2
    assert "有东西也不注入" not in out2, "coder 的现状块不注入（用户令）"
    # 连活性/使命都没有（手工构造的空卡面）⇒ 不出现
    assert mk([{"card_id": "card_010", "title": "空卡", "card_type": "coder"}]) == ""


def test_region_guide_function_branch(sandbox):
    """职能卡的 155 区讲职责书+差事记录,不再讲三块;作业卡三块语义不变。"""
    g = context_regions.render_card_region_guide("function", "rd_project")
    assert "职能卡" in g and "职责书" in g and "差事" in g
    assert "三个块" not in g and "台账" not in g
    g2 = context_regions.render_card_region_guide("sub_project", "rd_project")
    assert "三个块" in g2                        # 作业卡照旧


# ---------------------------------------------------------------- 旧职能清理

def test_card_list_excludes_function_and_role(sandbox):
    """100 区卡片清单不列职能/角色卡(不是协作对象,不占上下文)。"""
    mk = context_regions.render_card_list_region
    cards = [
        {"card_id": "card_001", "title": "根", "parent": "", "card_type": "project"},
        {"card_id": "card_002", "title": "子", "parent": "card_001", "card_type": "sub_project"},
        {"card_id": "fn_004", "title": "产品经理", "parent": "", "card_type": "function"},
        {"card_id": "fn_001", "title": "旧职能", "parent": "", "card_type": "function"},
        {"card_id": "role_x", "title": "旧角色", "parent": "", "card_type": "role"},
    ]
    out = mk(cards, "card_001")
    assert "card_002" in out
    assert "fn_004" not in out and "fn_001" not in out and "role_x" not in out


async def test_nav_hides_ceo_and_archived(sandbox, monkeypatch):
    """nav 职能段:总裁(=根卡)不重复出条;归档(撤岗)的旧职能不显示。"""
    _use_repo_catalog(monkeypatch)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import routes as api_routes
    wdata = await work_store.create_work("清理验证")
    wid = wdata["work_id"]
    await function_room.seed_work(wid)
    # 补一个旧式职能并归档
    prof = await function_store.create_function(
        wid, "旧职能", {"charter": {"version": 1, "text": "旧"}})
    await function_store.set_duty_state(wid, prof["function_id"], "archived")
    root_id = work_store.work_card_id_of(wid)
    app = FastAPI(); app.include_router(api_routes.router)
    with TestClient(app) as client:
        nav = client.get("/api/nav").json()
    for proj in nav.get("projects", []):
        if proj.get("work_id") == wid:
            fids = [f["function_id"] for f in (proj.get("functions") or [])]
            assert root_id not in fids                    # 总裁不重复出条
            assert prof["function_id"] not in fids        # 归档不显示
            assert len(fids) == 4                          # 只剩四新职能
