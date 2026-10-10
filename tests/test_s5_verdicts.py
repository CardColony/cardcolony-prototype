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
from src.thinkchain import triage as triage_mod
from tests.test_s4_seams import FAKE_CONFIG, ScriptedGateway


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


# ------------------------------------------------- 判据 4：逃生口真调

async def test_escape_hatch_real_call(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]

    async def fake_triage(system, user):
        return '{"verdict": "direct", "reason": "初判直答"}'

    monkeypatch.setattr(triage_mod, "_call_model", fake_triage)
    gw = ScriptedGateway(script=[
        [{"name": "escalate_to_plan", "arguments": {"reason": "实际需要多步工具"}}],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = await _post(client, wid, cid, "帮我看看这个")

    # direct 席工具面只留逃生口，且模型真调了它
    assert gw.calls[0]["tools"] == ["escalate_to_plan"]
    evts = [e for e in body.get("tool_events", []) if e.get("tool") == "escalate_to_plan"]
    assert evts and evts[-1].get("ok") is True
    # 档案补块在盘（绝不静默换路的留痕面）
    blocks = await session_facade.read(wid, cid)
    esc = [b for b in blocks if b.get("behavior") == "escalate"]
    assert esc and "实际需要多步工具" in esc[-1]["content"]


# ------------------------------------------------- 判据 9：防伪造

async def test_forgery_claim_still_nagged(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    assert (await board_mod.create_board(
        wid, cid, "防伪计划",
        [{"id": "s1", "prompt": "本步要求原文Q", "type": "execute"}]))["ok"]
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
            assert "本步要求原文Q" in c["user_prompt"]
            assert "step_complete" in c["user_prompt"]
        r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")
        assert r.status_code == 200
        resp = await asyncio.wait_for(task, timeout=30)

    assert resp.status_code == 200


# ------------------------------------------------- 判据 10：人停刹车

async def test_human_stop_brakes_nag_loop(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    assert (await board_mod.create_board(
        wid, cid, "刹车计划", [{"id": "s1", "prompt": "长任务步", "type": "execute"}]))["ok"]
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
