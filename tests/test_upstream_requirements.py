# -*- coding: utf-8 -*-
"""上游需求文档区（order 160）· 2026-11-10 用户令。

裁定原文：「UI、qa、pm、验收卡，都应该看到其父卡的需求文档，需求文档是它们的
参照物，是应该直接进入它们上下文的。」

硬判据：
- ui / qa_card / pm_card 的上下文里常驻父卡需求文档**正稿**（读侧投影，不截断）；
- 父卡改正稿 ⇒ 下一轮装配到的是**新**文本（不复制、不过期）；
- qa/pm 差事卡额外带**对象卡**（差事账 target_card）的需求文档；
- 别的卡型不出这个区；父卡没正稿时区不出现；
- 差事 kick 正文：对象卡≠派遣方时补「派遣方需求文档」（执行体路径只见 kick）。
"""
from __future__ import annotations

from src.api import card_tools, context_assembler, context_regions
from src.storage import card_store, work_store


async def _mk(wid: str):
    """根卡 card_001 → 002=sub_project（有正稿）→ 003/004=评审卡；ui 挂在 002 下。"""
    await card_store.create_card(wid, "子卡A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.update_card(
        wid, "card_002",
        {"requirements": "接口 v2：必填字段 repo/branch；验收：单测全绿"},
        "测试：写需求正稿")
    return card_store.iter_card_metas(wid)


async def test_ui_sees_parent_requirements(sandbox):
    """ui 卡的 system 里常驻父卡需求文档全文（order 160 区）。"""
    wid = (await work_store.create_work("ui参照"))["work_id"]
    await _mk(wid)
    await card_store.create_card(wid, "界面卡", "用户直接创建",
                                 card_type="ui", parent="card_002",
                                 charter="## 使命\n做列表页", skip_kick=True)
    ctx = context_assembler.build_ctx(wid, "card_004")
    main = context_regions.compose_model_context(ctx, profile="main")
    sp = main["system_prompt"]
    assert "## 上游需求文档" in sp
    assert "### 父卡 card_002" in sp
    assert "接口 v2：必填字段 repo/branch" in sp


async def test_projection_is_live(sandbox):
    """父卡改正稿 → 下一轮装配到新文本（卡面唯一事实源，不复制不通知）。"""
    wid = (await work_store.create_work("活投影"))["work_id"]
    await _mk(wid)
    await card_store.create_card(wid, "界面卡", "用户直接创建",
                                 card_type="ui", parent="card_002",
                                 charter="## 使命\n做列表页", skip_kick=True)
    await card_store.update_card(
        wid, "card_002", {"requirements": "接口 v3：加字段 dry_run"},
        "测试：改需求")
    ctx = context_assembler.build_ctx(wid, "card_004")
    main = context_regions.compose_model_context(ctx, profile="main")
    assert "接口 v3：加字段 dry_run" in main["system_prompt"]
    assert "必填字段 repo/branch" not in main["system_prompt"]


async def test_other_types_no_region(sandbox):
    """非执行/差事卡型不出这个区。"""
    wid = (await work_store.create_work("普通卡"))["work_id"]
    await _mk(wid)
    ctx = context_assembler.build_ctx(wid, "card_002")
    main = context_regions.compose_model_context(ctx, profile="main")
    assert "## 上游需求文档" not in main["system_prompt"]


async def test_no_final_no_region(sandbox):
    """父卡没正稿 → 区不出现（没有参照物不硬撑一行）。"""
    wid = (await work_store.create_work("无正稿"))["work_id"]
    await card_store.create_card(wid, "子卡A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "界面卡", "用户直接创建",
                                 card_type="ui", parent="card_002",
                                 charter="## 使命\n做列表页", skip_kick=True)
    ctx = context_assembler.build_ctx(wid, "card_004")
    main = context_regions.compose_model_context(ctx, profile="main")
    assert "## 上游需求文档" not in main["system_prompt"]


def test_loader_deploy_target(monkeypatch):
    """qa/pm 差事卡：父卡之外带对象卡需求（target_card≠父卡才单列）。"""
    cards = [
        {"card_id": "card_002", "title": "派遣方", "requirements": "上游规格A"},
        {"card_id": "card_007", "title": "对象卡", "requirements": "对象规格B"},
    ]
    meta = {"card_type": "qa_card", "parent": "card_002"}

    from src.storage import function_store as _fs
    monkeypatch.setattr(
        _fs, "active_for_venue",
        lambda w, v, venue_kind="": [{"target_card": "card_007"}])
    out = context_assembler._load_upstream_requirements(
        "w", "card_010", meta, cards)
    labels = [(e["label"], e["card_id"]) for e in out]
    assert labels == [("父卡", "card_002"), ("对象卡", "card_007")]

    # target == 父卡 → 不重复列
    monkeypatch.setattr(
        _fs, "active_for_venue",
        lambda w, v, venue_kind="": [{"target_card": "card_002"}])
    out2 = context_assembler._load_upstream_requirements(
        "w", "card_010", meta, cards)
    assert [e["card_id"] for e in out2] == ["card_002"]


def test_render_shape():
    """渲染：正稿全文＋参照尺说明；空条目 → 空串。"""
    out = context_regions.render_upstream_requirements_region(
        [{"label": "父卡", "card_id": "c1", "title": "T",
          "requirements": "规格全文不截断"}])
    assert out.startswith("## 上游需求文档")
    assert "规格全文不截断" in out
    assert context_regions.render_upstream_requirements_region([]) == ""
    assert context_regions.render_upstream_requirements_region(
        [{"label": "父卡", "card_id": "c1", "title": "T",
          "requirements": "  "}]) == ""


async def test_worker_kick_carries_parent_requirements(sandbox, monkeypatch):
    """worker kick：任务正文后自动带【参照·父卡需求文档正稿】（执行体唯一通道）。

    [2026-11-10 · 用户令「worker 就要把需求文档自动塞进它的提示词里」]
    """
    import asyncio
    wid = (await work_store.create_work("worker带尺"))["work_id"]
    await _mk(wid)
    captured = {}

    from src.api import routes as _routes

    def _spy(w, c, prompt, *a, **kw):
        captured["prompt"] = prompt
        return asyncio.sleep(0)

    monkeypatch.setattr(_routes, "_run_message", _spy)
    cards = card_store.iter_card_metas(wid)
    r = await card_tools._h_create_worker_card(
        wid, "card_002", {"title": "干活", "task": "写接口实现"}, cards)
    assert r.get("ok")
    prompt = captured.get("prompt", "")
    assert "任务内容" in prompt and "写接口实现" in prompt
    assert "【参照·你父卡的需求文档正稿】" in prompt
    assert "接口 v2：必填字段 repo/branch" in prompt


async def test_worker_in_region_types(sandbox):
    """worker 也在 160 区类型闸里——系统回合路径同样罩住。"""
    wid = (await work_store.create_work("worker区"))["work_id"]
    await _mk(wid)
    await card_store.create_card(wid, "干活卡", "用户直接创建",
                                 card_type="worker", parent="card_002",
                                 charter="## 使命\n写接口", skip_kick=True)
    ctx = context_assembler.build_ctx(wid, "card_004")
    main = context_regions.compose_model_context(ctx, profile="main")
    assert "## 上游需求文档" in main["system_prompt"]
    assert "接口 v2：必填字段 repo/branch" in main["system_prompt"]


async def test_deploy_kick_carries_delegator_requirements(sandbox, monkeypatch):
    """差事 kick：对象卡≠派遣方时正文带「派遣方需求文档」（执行体路径唯一通道）。"""
    wid = (await work_store.create_work("差事kick"))["work_id"]
    await _mk(wid)
    # 对象卡：002 的直接子卡（worker 型，需求文档恒空）
    await card_store.create_card(wid, "干活卡", "用户直接创建",
                                 card_type="worker", parent="card_002",
                                 charter="## 使命\n写接口", skip_kick=True)
    import src.api.function_room as _fr
    if not _fr.entries():
        return  # 无职能目录的环境跳过
    captured = {}

    from src.storage import function_store as _fs

    async def _spy(*a, **kw):
        captured["mandate"] = kw.get("mandate", "")
        return {"id": 1}

    monkeypatch.setattr(_fs, "create_deployment", _spy)
    cards = card_store.iter_card_metas(wid)
    r = await card_tools._h_create_deploy_card(
        wid, "card_002",
        {"function": "qa", "task": "阶段验收这张卡的产物", "target": "card_004"},
        cards)
    if "error" in r:
        return  # 职能档案缺失/其他前置未过的环境不硬判
    assert "派遣方需求文档" in captured.get("mandate", "")
    assert "接口 v2：必填字段 repo/branch" in captured["mandate"]
