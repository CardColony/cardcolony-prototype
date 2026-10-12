# -*- coding: utf-8 -*-
"""[卡片编辑机制改造 2026-10-12 · 块2] draft/commit 接口测试。

硬判据（任务书 §六 块2）：
1. draft 请求工具列表为空（不传 tools），返回 {draft, base_hash}。
2. commit base_hash 不一致 → 返回拒绝，卡面零改动。
3. commit 只写 requirements/status_block，传 mission → 拒绝（使命块不可经此口）。
4. 确认后影子 ## 迭代历史 新增一行（含旧/新 hash）。
5. BASE_PROMPT_VERSION == "t17-1"（grep 可验）。
"""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from src.api import card_draft, prompts, routes
from src.storage import card_store, shadow_store, work_store
from src.storage.hash_utils import parse_front_matter

app_state = {}


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
    return wdata


async def _mk_research_work():
    wdata = await work_store.create_work("研究树", project_class="research_project")
    assert "error" not in wdata, wdata
    return wdata


async def _mk_child(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


# ---------------------------------------------------------------- 判据5：版本号
# [工具模块整理 2026-10-12] 钉子由 t17-1 推进到 t18-1。
# t18-1 是「说明追现实」批：撤回 t17-1「edit_card/update_shadow 已收回」的不实
# 声称（两件实际仍在工具面第 4/6 位下发，见 docs/工具模块现状-20261012.md P-01），
# 改为如实说明「直改与起草两条写入路径并存」；另补 6 件零文档工具、修文件边界、
# 联网两件改条件提供。**本文件测的 draft/commit 机制本身未被 t18-1 触及**，
# 改的只是版本号这个钉子。


def test_base_prompt_version_is_t18_1():
    assert prompts.BASE_PROMPT_VERSION == "t18-1"


# ---------------------------------------------------------------- 判据1：draft

async def test_draft_returns_draft_and_base_hash_without_tools(sandbox, monkeypatch):
    """draft 返回 {draft, base_hash}；调用不传工具（tools 缺省 None）。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    # 捕获 call_main 收到的 tools 参数（断言为 None）
    captured = {}
    orig = card_draft.model_service.call_main

    async def spy(system_prompt, user_prompt, prior_messages=None, tools=None, model_name="main"):
        captured["tools"] = tools
        captured["system"] = system_prompt
        return await orig(system_prompt, user_prompt, prior_messages=prior_messages,
                          tools=tools, model_name=model_name)

    monkeypatch.setattr(card_draft.model_service, "call_main", spy)
    r = await card_draft.draft(wid, cid, "requirements")
    assert "error" not in r, r
    assert "draft" in r and "base_hash" in r
    assert captured["tools"] is None  # 工具列表为空（不传 tools）
    assert isinstance(r["base_hash"], str) and len(r["base_hash"]) == 64


async def test_draft_rejects_mission_kind(sandbox, monkeypatch):
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    r = await card_draft.draft(wid, cid, "mission")
    assert "error" in r


async def test_draft_http_endpoint(client, sandbox, monkeypatch):
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    resp = await client.post(
        f"/api/works/{wid}/cards/{cid}/draft",
        json={"kind": "requirements"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "draft" in data and "base_hash" in data


# ---------------------------------------------------------------- 判据2：hash 冲突

async def test_commit_hash_mismatch_rejected_no_card_change(sandbox, monkeypatch):
    """base_hash 不一致 → 拒绝，卡面零改动。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "requirements")
    assert "error" not in d, d

    # 卡面被外部改动（经 update_card 改 status_block，块 hash 不变——改的是另一块；
    # 直接改 requirements 对应的 rulings 键使第三块 hash 变化）
    await card_store.update_card(wid, cid, {"rulings": "外部改动"}, "外部")

    r = await card_draft.commit(wid, cid, "requirements", "新草稿", d["base_hash"])
    assert "error" in r
    assert r.get("code") == "hash_mismatch"
    # 卡面仍是外部改动的内容，不是「新草稿」
    raw = await card_store.read_card(wid, cid)
    assert "外部改动" in raw["body"]["rulings"]
    assert "新草稿" not in raw["body"]["rulings"]


# ---------------------------------------------------------------- 判据3：使命拒写

async def test_commit_mission_kind_rejected(sandbox, monkeypatch):
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)
    r = await card_draft.commit(wid, cid, "mission", "改使命", "x" * 64)
    assert "error" in r
    assert "使命" in r["error"] or "kind" in r["error"]


# ---------------------------------------------------------------- 判据4：影子迭代历史

async def test_commit_writes_shadow_history_line(sandbox, monkeypatch):
    """确认写入后影子 ## 迭代历史 新增一行（含旧/新 hash）。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "requirements")
    assert "error" not in d, d
    r = await card_draft.commit(wid, cid, "requirements", "D01【裁定】确认后的需求文档", d["base_hash"],
                                feedback_rounds=2)
    assert r.get("ok") is True, r

    # 影子 ## 迭代历史 应有新行
    raw = await shadow_store.read_shadow_raw(wid, cid)
    assert raw is not None
    assert "需求文档确认" in raw
    assert r["old_hash"][:16] in raw
    assert r["new_hash"][:16] in raw
    assert "反馈轮数=2" in raw


async def test_commit_status_block(sandbox, monkeypatch):
    """kind=status 写 status_block。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "status")
    assert "error" not in d, d
    r = await card_draft.commit(wid, cid, "status", "已核实的新现状快照", d["base_hash"])
    assert r.get("ok") is True, r
    raw = await card_store.read_card(wid, cid)
    assert "已核实的新现状快照" in raw["body"]["status_block"]


async def test_commit_research_project_writes_rulings(sandbox, monkeypatch):
    """research_project 卡 commit kind=requirements 落 rulings 键（区头仍为 ## 定论）。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_research_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "requirements")
    assert "error" not in d, d
    r = await card_draft.commit(wid, cid, "requirements", "D01【裁定】研究结论", d["base_hash"])
    assert r.get("ok") is True, r
    raw = await card_store.read_card(wid, cid)
    assert "D01【裁定】研究结论" in raw["body"]["rulings"]


async def test_commit_rd_writes_requirements_section_heading(sandbox, monkeypatch):
    """[设计偏差修正] rd_project 卡 commit 后盘上区头为「## 需求文档」（非「## 定论」）。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "requirements")
    assert "error" not in d, d
    r = await card_draft.commit(wid, cid, "requirements", "D01【裁定】研发需求", d["base_hash"])
    assert r.get("ok") is True, r

    # 盘上正文区头应为 ## 需求文档（不是 ## 定论）
    card_path = card_store._card_path(wid, cid)
    raw = card_path.read_text(encoding="utf-8")
    _, body = parse_front_matter(raw)
    assert "## 需求文档" in body, f"盘上应有 ## 需求文档 区头，实际 body:\n{body}"
    assert "## 定论" not in body, f"盘上不应有 ## 定论 区头，实际 body:\n{body}"


async def test_commit_rd_read_side_shows_requirements(sandbox, monkeypatch):
    """rd_project 卡 commit 后读侧 body.requirements 有内容。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "requirements")
    assert "error" not in d, d
    r = await card_draft.commit(wid, cid, "requirements", "D01【裁定】需求正文", d["base_hash"])
    assert r.get("ok") is True, r

    # 读侧应走 requirements 键（_apply_block_set 过滤后）
    raw_card = await card_store.read_card(wid, cid)
    filtered = card_draft._apply_block_set(raw_card, wid)
    assert "D01【裁定】需求正文" in filtered["body"]["requirements"]
    assert "rulings" not in filtered["body"]  # rd 卡不外露 rulings


# ---------------------------------------------------------------- 反馈轮次上下文连续

async def test_draft_with_feedback_and_prev_draft(sandbox, monkeypatch):
    """反馈重起草：prev_draft + feedback 进上下文（连续性）。"""
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    captured = {}
    orig = card_draft.model_service.call_main

    async def spy(system_prompt, user_prompt, prior_messages=None, tools=None, model_name="main"):
        captured["user"] = user_prompt
        return await orig(system_prompt, user_prompt, prior_messages=prior_messages,
                          tools=tools, model_name=model_name)

    monkeypatch.setattr(card_draft.model_service, "call_main", spy)
    r = await card_draft.draft(
        wid, cid, "requirements",
        feedback="第二条需求要改", prev_draft="第一版草稿内容",
    )
    assert "error" not in r, r
    assert "第一版草稿内容" in captured["user"]
    assert "第二条需求要改" in captured["user"]
