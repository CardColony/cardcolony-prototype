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

# [销红 2026-10-12] 裁定1：钉 dev 现态 src/api/prompts.py:38。
# [判断协议改注入 2026-10-14] t17-1 → t18-1：判断协议从 BASE_PROMPT 摘出（改注入），
# 使用说明正文其余部分不变——文本变了，版本号随升（回归基准对账）。
# [卡片区域语义 v3 2026-10-14] t18-1 → t19-1：read_shadow 工具行去掉「上级卡查看子卡
# 影子」，改为「只读本卡自己的影子」。
# [worker 卡 2026-10-15] t19-1 → t20-1：工具节增 create_worker_card / report_done 两行，
# 新增「## worker 卡」使用说明节。
# [规则卡 2026-10-15] t20-1 → t21-1：新增「## 规则卡」使用说明节（根卡
# draft_rule_edit 起草＋用户审批落盘）。
def test_base_prompt_version_is_t20_1():
    # [依赖关联线 2026-10-16] t21-1 → t22-1。
    assert prompts.BASE_PROMPT_VERSION == "t27-19"  # [2026-11-01] 随现配置对齐


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

    async def spy(system_prompt, user_prompt, prior_messages=None, tools=None,
                  model_name="main", turn_tail=None, turn_once=None, fallbacks=None):
        captured["tools"] = tools
        captured["system"] = system_prompt
        return await orig(system_prompt, user_prompt, prior_messages=prior_messages,
                          tools=tools, model_name=model_name, fallbacks=fallbacks)

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
    # 直接改 requirements 对应的 requirements 键使第三块 hash 变化）
    await card_store.update_card(wid, cid, {"requirements": "外部改动"}, "外部")

    r = await card_draft.commit(wid, cid, "requirements", "新草稿", d["base_hash"])
    assert "error" in r
    assert r.get("code") == "hash_mismatch"
    # 卡面仍是外部改动的内容，不是「新草稿」
    raw = await card_store.read_card(wid, cid)
    assert "外部改动" in raw["body"]["requirements"]
    assert "新草稿" not in raw["body"]["requirements"]


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


async def test_commit_research_project_writes_requirements(sandbox, monkeypatch):
    """research_project 卡 commit kind=requirements 落 requirements 键（区头仍为 ## 定论）。"""
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
    assert "D01【裁定】研究结论" in raw["body"]["requirements"]


async def test_commit_rd_writes_requirements_block(sandbox, monkeypatch):
    """[卡片存储迁移 SQLite] rd_project 卡 commit 后：第三块内容落库，读侧以
    requirements 外露。

    原判据「盘上区头为 ## 需求文档」随 md 退役——库内第三块是**一列**
    （cards.requirements），「叫什么名」由读侧按本树 project_class 决定
    （routes._apply_block_set），不再有盘上区头可改。
    """
    os.environ["MOCK_LLM"] = "1"
    monkeypatch.setattr(card_draft.model_service, "_gateway", None)
    wdata = await _mk_rd_work()
    wid, root = wdata["work_id"], wdata["work_card_id"]
    cid = await _mk_child(wid, "子卡", root)

    d = await card_draft.draft(wid, cid, "requirements")
    assert "error" not in d, d
    r = await card_draft.commit(wid, cid, "requirements", "D01【裁定】研发需求", d["base_hash"])
    assert r.get("ok") is True, r

    card = await card_store.read_card(wid, cid)
    assert "D01【裁定】研发需求" in card["body"]["requirements"]
    filtered = routes._apply_block_set(card, wid)
    assert "D01【裁定】研发需求" in filtered["body"]["requirements"]


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
    # [需求文档正名 2026-10-28] 旧键 rulings 已彻底退役：读侧只有一个键 requirements
    assert "rulings" not in filtered["body"]


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

    async def spy(system_prompt, user_prompt, prior_messages=None, tools=None,
                  model_name="main", turn_tail=None, turn_once=None, fallbacks=None):
        captured["user"] = user_prompt
        return await orig(system_prompt, user_prompt, prior_messages=prior_messages,
                          tools=tools, model_name=model_name, fallbacks=fallbacks)

    monkeypatch.setattr(card_draft.model_service, "call_main", spy)
    r = await card_draft.draft(
        wid, cid, "requirements",
        feedback="第二条需求要改", prev_draft="第一版草稿内容",
    )
    assert "error" not in r, r
    assert "第一版草稿内容" in captured["user"]
    assert "第二条需求要改" in captured["user"]
