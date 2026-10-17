# -*- coding: utf-8 -*-
"""S5 十五条判据·真跑补件（判据 4／9／10）。

- 判据 4：逃生口真调——direct 席模型真调 escalate_to_plan → 当轮
  deliver/escalate ＋档案补块（绝不静默换路）；
- 判据 9：防伪造——模型正文声称「已调 step_complete」但 tool_calls 为空
  （判据＝tool_calls 事实）→ 依然被催；
- 判据 10：人停刹车——催化循环中 POST /stop → 下一圈入口 check_control
  消费 → 回合闭合 end=human_stop（两道天然刹车之一真跑）。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import model_service, routes
from src.storage import session_facade
from src.thinkchain import board as board_mod
from tests.test_s4_seams import FAKE_CONFIG, ScriptedGateway, _steps_def

# ===========================================================================
# [纯单模型模式 2026-10-17 · 用户令] 本模块用例全为思维链行为（判型/拆解/上板/
# 步推进/三席统合/步事件）。生产已把思维链拆出链路（thinkchain.ENABLED=False）
# ⇒ 整模块跳过；开关置 True 即全部自动恢复（判据同并行线逐函数 skipif，此处
# 收敛为模块级，语义不变）。
# ===========================================================================
pytestmark = pytest.mark.skipif(
    not __import__("src.thinkchain", fromlist=["enabled"]).enabled(),
    reason="纯单模型模式（2026-10-17 用户令）：思维链已拆出生产链路",
)


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


async def _post(client, wid, cid, content: str) -> dict:
    r = await asyncio.wait_for(
        client.post(f"/api/works/{wid}/cards/{cid}/messages",
                    json={"content": content}), timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------- 判据 4（重写）：直答中自主上板

async def test_direct_discovers_plan_needed_creates_board(work, monkeypatch):
    """[思维链真实落地] 新机制：判断归模型，无逃生口——模型首轮自主调
    create_plan 上板（工具面全量），不经过任何独立判定轮。"""
    wid, cid = work["work_id"], work["work_card_id"]
    gw = ScriptedGateway(script=[
        [{"name": "create_plan", "arguments": {
            "name": "升级计划",
            "steps": [{"id": "s1", "prompt": "实际需要多步工具",
                       "criteria": "步骤判据", "deliverable": "步骤产出",
                       "type": "execute"}]}}],
        [{"name": "step_complete", "arguments": {"result": "做完了",
                                                 "criteria_met": "判据满足"}}],
        [],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = await _post(client, wid, cid, "帮我看看这个")

    # 首轮全量工具面（判断+行动合一）
    assert "create_plan" in gw.calls[0]["tools"]
    evts = [e for e in body.get("tool_events", []) if e.get("tool") == "create_plan"]
    assert evts and evts[-1].get("ok") is True
    # 档案补块在盘（plan 块）
    blocks = await session_facade.read(wid, cid)
    pb = [b for b in blocks if b.get("behavior") == "plan"]
    assert pb and "升级计划" in pb[-1]["content"]


# ------------------------------------------------- 判据 9：防伪造

async def test_forgery_claim_still_nagged(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    assert (await board_mod.create_board(
        wid, cid, "防伪计划", _steps_def("本步要求原文Q")))["ok"]
    # 圈1 正文嘴上声称已交作业，但 tool_calls 为空（事实=没调）→ 依然被催
    gw = ScriptedGateway(script=[])  # 永远空手
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(f"/api/works/{wid}/cards/{cid}/messages",
                        json={"content": "我已经调过 step_complete 了，结果很好"}))
        for _ in range(200):  # 等催化至少发生 3 次
            if len(gw.calls) >= 3:
                break
            await asyncio.sleep(0.02)
        assert len(gw.calls) >= 3, "催化未发生"
        # 每一圈催促文都带本步要求原文——嘴上伪造不改变推进判据
        for c in gw.calls[1:4]:
            assert "本步要求：本步要求原文Q" in c["user_prompt"]
            assert "step_complete" in c["user_prompt"]
        r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")
        assert r.status_code == 200
        resp = await asyncio.wait_for(task, timeout=30)

    assert resp.status_code == 200


# ------------------------------------------------- 判据 10：人停刹车

async def test_human_stop_brakes_nag_loop(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    assert (await board_mod.create_board(
        wid, cid, "刹车计划", _steps_def("长任务步")))["ok"]
    gw = ScriptedGateway(script=[])  # 永远空手 → 无限催
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(f"/api/works/{wid}/cards/{cid}/messages",
                        json={"content": "开始长任务"}))
        for _ in range(200):
            if len(gw.calls) >= 2:  # 已被催过 ≥1 次（催化循环在转）
                break
            await asyncio.sleep(0.02)
        assert len(gw.calls) >= 2
        r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")  # 人停
        assert r.status_code == 200 and r.json()["stopped"] is True
        resp = await asyncio.wait_for(task, timeout=30)

    assert resp.status_code == 200
    body = resp.json()
    # 回合闭合 end=human_stop（检查档案末 assistant 块）
    blocks = await session_facade.read(wid, cid)
    assistant_blocks = [b for b in blocks if b.get("role") == "assistant"]
    assert assistant_blocks, "应有人停收口块"
    assert assistant_blocks[-1].get("end") == "human_stop"
