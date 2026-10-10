# -*- coding: utf-8 -*-
"""迭代三·类型契约单测（S3 建板入口契约＋S4 档案扩值的新增用例承载件）。

S3 判据（任务书验收，逐条机械可执行）：
- 合法 type 建板成功／缺 type 被拒（错误信息含步序号）／非法值被拒；
- insert 非法被拒／insert 合法通过；
- 三动作零触碰：reorder/remove/modify 后各步 type 原值不变（含空串步仍空串）；
- 存量兼容：type 为空串的存量板 → 读取/恢复不报错、不拒绝、不升格；
- 读路径零校验：type 校验只存在于 create_board 与 apply_revision·insert
  两处写入口（grep 可验，见施工回报）；
- 工具面实测：think 步 tools 数量 == execute 步 tools 数量（无收窄）。

零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import model_service, routes
from src.storage import session_store
from src.thinkchain import board as board_mod
from src.thinkchain.board import VALID_STEP_TYPES

from tests.test_s4_seams import ScriptedGateway


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


def _legacy_plan_json(card_id: str, steps: list[dict[str, Any]]) -> str:
    """构造存量形态板 JSON（type 可为空串——迭代二真实落盘形态）。"""
    return json.dumps({"card_id": card_id, "name": "存量板", "steps": steps,
                       "current": 0, "revisions": [], "created_at": "2026-10-10T00:00:00"},
                      ensure_ascii=False)


def _write_legacy_plan(wid: str, cid: str, steps: list[dict[str, Any]]) -> None:
    """直接写盘（绕过 create_board 校验——模拟迭代二存量板）。"""
    p = board_mod.plan_path(wid, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_legacy_plan_json(cid, steps), encoding="utf-8")


# ------------------------------------------------- S3 建板正负例

async def test_create_plan_with_type_ok(work):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await board_mod.create_board(wid, cid, "契约计划", [
        {"id": "s1", "prompt": "查资料", "type": "execute"},
        {"id": "s2", "prompt": "整理结论", "type": "think"},
    ])
    assert r["ok"], r
    snap = r["board"]
    assert [s["type"] for s in snap["steps"]] == ["execute", "think"]


async def test_missing_type_rejected_with_step_index(work):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await board_mod.create_board(wid, cid, "n", [
        {"id": "s1", "prompt": "a", "type": "execute"},
        {"id": "s2", "prompt": "b"},           # 缺 type
    ])
    assert r["ok"] is False
    assert "第 2 步" in r["error"], r["error"]   # 错误信息含步序号


async def test_invalid_type_rejected(work):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await board_mod.create_board(wid, cid, "n", [
        {"id": "s1", "prompt": "a", "type": "think2"},
    ])
    assert r["ok"] is False
    assert "第 1 步" in r["error"] and "think2" in r["error"]


async def test_insert_invalid_type_rejected(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "n", [
        {"id": "s1", "prompt": "a", "type": "execute"}])
    b = await board_mod.get_board(wid, cid)
    r = await b.apply_revision("insert", {"after_id": "s1",
                                          "step": {"id": "s9", "prompt": "x", "type": "ghost"}})
    assert r["ok"] is False and "type" in r["error"]
    # 板面未被污染（拒绝发生在写入前）
    snap = (await board_mod.get_board(wid, cid)).to_snapshot()
    assert len(snap["steps"]) == 1


async def test_insert_valid_type_ok(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "n", [
        {"id": "s1", "prompt": "a", "type": "execute"}])
    b = await board_mod.get_board(wid, cid)
    r = await b.apply_revision("insert", {"after_id": "s1",
                                          "step": {"id": "s9", "prompt": "x", "type": "think"}})
    assert r["ok"], r
    snap = r["board"]
    assert snap["steps"][1]["type"] == "think"


# ------------------------------------------------- 三动作零触碰＋存量兼容

async def test_three_actions_preserve_type(work):
    """reorder/remove/modify 后各步 type 原值不变（含空串步仍空串）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    _write_legacy_plan(wid, cid, [
        {"id": "s1", "prompt": "a", "status": "todo", "type": "execute", "has_artifact": False},
        {"id": "s2", "prompt": "b", "status": "todo", "type": "", "has_artifact": False},
        {"id": "s3", "prompt": "c", "status": "todo", "type": "think", "has_artifact": False},
    ])
    b = await board_mod.get_board(wid, cid)

    r1 = await b.apply_revision("reorder", {"order": ["s3", "s1", "s2"]})
    assert r1["ok"], r1
    assert [s["type"] for s in r1["board"]["steps"]] == ["think", "execute", ""]

    r2 = await b.apply_revision("remove", {"id": "s3"})
    assert r2["ok"], r2
    assert [s["type"] for s in r2["board"]["steps"]] == ["execute", ""]

    r3 = await b.apply_revision("modify", {"id": "s2", "prompt": "改后的要求"})
    assert r3["ok"], r3
    assert [s["type"] for s in r3["board"]["steps"]] == ["execute", ""]


async def test_legacy_empty_type_board_read_ok_no_upgrade(work):
    """存量兼容：type 空串板读取/恢复不报错、不拒绝、不升格（读回仍空串）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    _write_legacy_plan(wid, cid, [
        {"id": "s1", "prompt": "a", "status": "todo", "type": "", "has_artifact": False},
    ])
    b = await board_mod.get_board(wid, cid)          # 恢复序零校验
    assert b is not None
    snap = b.to_snapshot()
    assert snap["steps"][0]["type"] == "", "空串不得被升格"
    # 读盘字节零改动（不升格的硬证据）
    disk = json.loads(board_mod.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["steps"][0]["type"] == ""
    # 推进也接得住（存量板可继续走完）
    adv = await b.mark_done_and_advance("存量步成果")
    assert adv["ok"] and adv.get("advanced") is True


# ------------------------------------------------- schema 契约

def test_create_plan_schema_has_type_enum_required():
    from src.thinkchain.plan_tools import PLAN_TOOLS
    create_plan = next(t for t in PLAN_TOOLS if t["schema"]["name"] == "create_plan")
    items = create_plan["schema"]["parameters"]["properties"]["steps"]["items"]
    tprop = items["properties"]["type"]
    assert tprop["enum"] == ["execute", "think"], tprop
    assert "type" in items["required"]
    desc = create_plan["schema"]["description"]
    for token in ("execute", "think", "不是不给工具调用权限",
                  "标错或漏标会被", "查证动手拿事实", "整理收敛"):
        assert token in desc, token


def test_plan_insert_schema_has_type_required():
    from src.thinkchain.plan_tools import PLAN_TOOLS
    ins = next(t for t in PLAN_TOOLS if t["schema"]["name"] == "plan_insert")
    step = ins["schema"]["parameters"]["properties"]["step"]
    assert step["properties"]["type"]["enum"] == ["execute", "think"]
    assert "type" in step["required"]


def test_valid_step_types_constant():
    assert VALID_STEP_TYPES == ["execute", "think"]


# ================================================== S4 档案行为值扩值

async def test_new_board_execute_and_think_step_blocks(work):
    """S4：新板 execute 步完成块落 execute_step、think 步落 think_step，
    执行期间零裸 "step"。"""
    from src.api.card_tools import execute_tool
    from src.storage import session_facade
    wid, cid = work["work_id"], work["work_card_id"]
    r = await execute_tool(wid, cid, "create_plan", {
        "name": "扩值计划",
        "steps": [{"id": "s1", "prompt": "查资料", "type": "execute"},
                  {"id": "s2", "prompt": "整理结论", "type": "think"}],
    }, {})
    assert r["ok"], r
    r1 = await execute_tool(wid, cid, "step_complete", {"result": "查到了"}, {})
    assert r1["ok"] and r1["data"]["advanced"] is True, r1
    r2 = await execute_tool(wid, cid, "step_complete", {"result": "结论是Z"}, {})
    assert r2["ok"] and r2["data"]["all_steps_done"] is True, r2

    blocks = await session_facade.read(wid, cid)
    behaviors = [b.get("behavior") for b in blocks]
    assert "execute_step" in behaviors, behaviors
    assert "think_step" in behaviors, behaviors
    assert "step" not in behaviors, "新板执行期间应零裸 step（决策 B）"


async def test_legacy_empty_type_falls_back_to_step(work, caplog):
    """S4：存量板（type 空串）步完成落 "step" 兜底＋日志留痕（不升格）。"""
    import logging

    from src.api.card_tools import execute_tool
    from src.storage import session_facade
    wid, cid = work["work_id"], work["work_card_id"]
    _write_legacy_plan(wid, cid, [
        {"id": "s1", "prompt": "a", "status": "todo", "type": "", "has_artifact": False},
    ])
    with caplog.at_level(logging.WARNING, logger="thinkchain.plan_tools"):
        r = await execute_tool(wid, cid, "step_complete", {"result": "存量步成果"}, {})
    assert r["ok"] and r["data"]["advanced"] is True, r
    blocks = await session_facade.read(wid, cid)
    behaviors = [b.get("behavior") for b in blocks]
    assert "step" in behaviors, "存量板兜底应落裸 step"
    assert any("兜底" in rec.message for rec in caplog.records), \
        [r.message for r in caplog.records]


async def test_session_read_does_not_rewrite_legacy_values(work):
    """S4 零迁移：含旧 "step" 值的会话文件读回原样、文件字节零改动。"""
    from src.storage import session_facade
    wid, cid = work["work_id"], work["work_card_id"]
    # 先落一个新块再手工改出旧值形态（构造存量样本——沙箱文件，非真实数据）
    await session_facade.append(wid, cid, {"role": "user", "content": "旧消息",
                                           "behavior": "step"})
    p = session_facade._session_path(wid, cid)
    assert p.exists(), f"会话文件应在盘: {p}"
    before = p.read_bytes()
    blocks = await session_facade.read(wid, cid)
    assert any(b.get("behavior") == "step" for b in blocks), "旧值原样可读"
    assert p.read_bytes() == before, "读操作不得改写文件（零迁移）"


# ================================================== S5 消费侧接线

async def _run_one_round(client, wid, cid, gw):
    """发一条消息、等催化圈转起来（≥1 次模型调用）后人停收口。"""
    task = asyncio.create_task(
        client.post(f"/api/works/{wid}/cards/{cid}/messages",
                    json={"content": "开始"}))
    for _ in range(400):
        if len(gw.calls) >= 1:
            break
        await asyncio.sleep(0.02)
    r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")
    assert r.status_code == 200
    return await asyncio.wait_for(task, timeout=30)


async def test_s5_route_log_execute_and_think(work, monkeypatch, caplog):
    """S5 日志实证：execute 步→seat=main；think 步热切后→seat=viewpoint_qwen。"""
    import logging

    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "路由计划", [
        {"id": "s1", "prompt": "整理一下", "type": "think"},
    ])
    gw = ScriptedGateway(script=[])
    gw.config = {"models": {"main": {"provider": "openai", "model": "fake-main"},
                            "viewpoint_qwen": {"provider": "openai", "model": "fake-qwen"}},
                 "simple": {"enabled": False, "viewpoints": []}}
    monkeypatch.setattr(model_service, "_gateway", gw)

    # 热切换：model_pick 表 think→viewpoint_qwen（临时副本，monkeypatch 隔离）
    import json as _json
    import tempfile
    from pathlib import Path
    from src.thinkchain import model_pick as mp
    hot_copy = Path(tempfile.mkdtemp(prefix="_s5hot_")) / "model_pick.json"
    hot_copy.write_text(_json.dumps(
        {"default": "main", "execute": "main", "think": "viewpoint_qwen"}),
        encoding="utf-8")
    monkeypatch.setattr(mp, "PICK_PATH", hot_copy)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.INFO, logger="api.message_runner"):
            body = await _run_one_round(client, wid, cid, gw)
    assert body.status_code == 200
    route_logs = [r.message for r in caplog.records
                  if "类型路由" in r.message]
    assert any("role=think" in m and "seat=viewpoint_qwen" in m
               for m in route_logs), route_logs


async def test_s5_bad_seat_name_explicit_error(work, monkeypatch):
    """S5 配错名实证：表指向不存在席位 → 运行时显式报错、信息含该名字。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "坏配置计划", [
        {"id": "s1", "prompt": "a", "type": "think"}])
    gw = ScriptedGateway(script=[])
    monkeypatch.setattr(model_service, "_gateway", gw)  # models 只有 main

    import json as _json
    import tempfile
    from pathlib import Path
    from src.thinkchain import model_pick as mp
    hot_copy = Path(tempfile.mkdtemp(prefix="_s5bad_")) / "model_pick.json"
    hot_copy.write_text(_json.dumps(
        {"default": "main", "execute": "main", "think": "ghost_seat"}),
        encoding="utf-8")
    monkeypatch.setattr(mp, "PICK_PATH", hot_copy)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await asyncio.wait_for(
            client.post(f"/api/works/{wid}/cards/{cid}/messages",
                        json={"content": "开始"}), timeout=30)
    assert r.status_code == 502, r.text
    assert "ghost_seat" in r.text, r.text   # 报错信息含该名字


async def test_s5_no_board_falls_back_default(work, monkeypatch, caplog):
    """S5 无活板路径：直答回落 default，行为与改动前一致（200 交付）。"""
    import logging

    async def fake_triage(system, user):
        return '{"verdict": "direct", "reason": "寒暄"}'

    from src.thinkchain import triage as triage_mod
    monkeypatch.setattr(triage_mod, "_call_model", fake_triage)
    gw = ScriptedGateway(script=[[]])
    monkeypatch.setattr(model_service, "_gateway", gw)

    wid, cid = work["work_id"], work["work_card_id"]
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.INFO, logger="api.message_runner"):
            r = await asyncio.wait_for(
                client.post(f"/api/works/{wid}/cards/{cid}/messages",
                            json={"content": "你好呀"}), timeout=30)
    assert r.status_code == 200, r.text
    route_logs = [rec.message for rec in caplog.records if "类型路由" in rec.message]
    assert any("role=default" in m and "seat=main" in m for m in route_logs), route_logs
    assert gw.calls[0]["tools"] == ["escalate_to_plan"]  # 直答行为与改动前一致


# ------------------------------------------------- 工具面实测（think 无收窄）

async def test_think_step_tools_same_as_execute(work, monkeypatch):
    """think 步 tools 数量 == execute 步 tools 数量（全链路催化圈对比）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "工具面对比", [
        {"id": "s1", "prompt": "思考一下", "type": "think"},
    ])
    gw = ScriptedGateway(script=[])
    monkeypatch.setattr(model_service, "_gateway", gw)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(f"/api/works/{wid}/cards/{cid}/messages",
                        json={"content": "开始"}))
        for _ in range(200):
            if len(gw.calls) >= 1:
                break
            await asyncio.sleep(0.02)
        r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")
        assert r.status_code == 200
        await asyncio.wait_for(task, timeout=30)

    think_tools = gw.calls[0]["tools"]
    # 对照：execute 步同链路（同卡清板重建，逐一对比）
    board_mod._boards.clear()
    await board_mod.create_board(wid, cid, "工具面对照", [
        {"id": "s1", "prompt": "查一下", "type": "execute"},
    ])
    gw2 = ScriptedGateway(script=[])
    monkeypatch.setattr(model_service, "_gateway", gw2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(f"/api/works/{wid}/cards/{cid}/messages",
                        json={"content": "开始"}))
        for _ in range(200):
            if len(gw2.calls) >= 1:
                break
            await asyncio.sleep(0.02)
        r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")
        assert r.status_code == 200
        await asyncio.wait_for(task, timeout=30)

    execute_tools = gw2.calls[0]["tools"]
    assert len(think_tools) == len(execute_tools)
    assert set(think_tools) == set(execute_tools)
    assert "step_complete" in think_tools  # 全量工具面在位
