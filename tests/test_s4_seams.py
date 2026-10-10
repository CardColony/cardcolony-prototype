# -*- coding: utf-8 -*-
"""S4 接缝行为测试（迭代二：自判分流＋催化＋t165 注入）。

任务书 S4 验收：
- 有板卡走新流：判据 1（直答/拆解分流）＋判据 8（催化两轮被催、第三轮补调
  推进）各真跑一次；
- t165 互斥实测：挂工具卡的卡不出现 t165 块；
- 无板卡对照差异=0 由 scripts/s4_compare_snapshot.py 承担（已跑，报告在案）。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import context_assembler, message_runner, model_service, routes
from src.storage import session_facade
from src.thinkchain import board as board_mod
from src.thinkchain import triage as triage_mod

FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
    },
    "simple": {"enabled": False, "viewpoints": []},
}


class ScriptedGateway:
    """按圈脚本化 gateway：记录每圈 (user_prompt, tools 名单)，按脚本发
    tool_calls（脚本耗尽后重复末圈——run_seats 统合官复用末圈空手帧）。"""

    def __init__(self, script: list[list[dict[str, Any]]] | None = None):
        self.config = FAKE_CONFIG
        self.script = script or []
        self.calls: list[dict[str, Any]] = []

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None, **_):
        # 让出点（S5 诊断定案）：真实网关每圈走网络 IO 必然让出事件循环，
        # 人停刹车（check_control）可达；Mock 零 IO 则催化循环独占循环、
        # 刹车饿死（test_forgery/human_stop 挂死根因）——此处补一次让出，
        # 与生产「每圈至少一次 IO 让出」语义对齐。生产代码零改动。
        await asyncio.sleep(0)
        self.calls.append({
            "user_prompt": user_prompt,
            "tools": [t["name"] for t in (tools or [])],
        })
        # 脚本耗尽后一律空手（收玫到 seats/deliver）——重复末圈会让收口后的
        # 假模型永远重发 step_complete（板不在→明文拒绝→再续跳）死循环。
        idx = len(self.calls) - 1
        tool_calls = self.script[idx] if idx < len(self.script) else []
        yield {"chunk_type": "content", "text": "回合正文"}
        yield {"content": "回合正文", "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}


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


# ------------------------------------------------- 判据 8：催化两轮被催、第三轮补调推进

async def test_catalyst_nags_until_step_complete(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await board_mod.create_board(wid, cid, "催促计划",
                                     [{"id": "s1", "prompt": "本步要求原文XYZ", "type": "execute"}])
    assert r["ok"], r
    gw = ScriptedGateway(script=[
        [],                                            # 圈1：空手 → 被催
        [],                                            # 圈2：仍空手 → 再催
        [{"name": "step_complete", "arguments": {"result": "成品X"}}],  # 圈3：补调
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = await _post(client, wid, cid, "开始干活")

    # 圈1：原文直入；圈2/圈3：催促文（本步要求原文＋催促语）
    assert gw.calls[0]["user_prompt"] == "开始干活"
    assert "本步要求原文XYZ" in gw.calls[1]["user_prompt"]
    assert "step_complete" in gw.calls[1]["user_prompt"]
    assert "未调之前这一步不算完成" in gw.calls[1]["user_prompt"]
    assert "本步要求原文XYZ" in gw.calls[2]["user_prompt"]  # 第二次催仍带原文
    # 圈4：工具结果回灌轮（占位符）
    assert gw.calls[3]["user_prompt"] == "工具结果已返回。"
    # 末步交作业→收口→无活板→空手正常交付（deliver）
    assert body["reply"] if "reply" in body else True
    # 板已收口清理
    assert not board_mod.plan_path(wid, cid).exists()
    assert not board_mod.has_live_board(wid, cid)
    # 档案：step 块＋final 块落档（[迭代三 S4 同步] 板步 type=execute → execute_step）
    blocks = await session_facade.read(wid, cid)
    behaviors = [b.get("behavior") for b in blocks]
    assert "execute_step" in behaviors and "final" in behaviors
    # 工具事实：step_complete 真执行（tool_events 有记录——防伪造判据的载体）
    step_evts = [e for e in body.get("tool_events", []) if e.get("tool") == "step_complete"]
    assert step_evts and step_evts[-1].get("ok") is True


# ------------------------------------------------- 判据 1：直答/拆解分流

async def test_triage_direct_limits_tools_to_escape(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]

    async def fake_call(system, user):
        return '{"verdict": "direct", "reason": "寒暄"}'

    monkeypatch.setattr(triage_mod, "_call_model", fake_call)
    gw = ScriptedGateway(script=[[]])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _post(client, wid, cid, "你好呀")

    # direct 席工具面＝仅 escalate_to_plan（逃生口）
    assert gw.calls[0]["tools"] == ["escalate_to_plan"]
    # triage 块落档
    blocks = await session_facade.read(wid, cid)
    tb = [b for b in blocks if b.get("behavior") == "triage"]
    assert tb and "direct" in tb[-1]["content"]


async def test_triage_plan_keeps_full_tools(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]

    async def fake_call(system, user):
        return '{"verdict": "plan", "reason": "需要拆解"}'

    monkeypatch.setattr(triage_mod, "_call_model", fake_call)
    gw = ScriptedGateway(script=[[]])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _post(client, wid, cid, "帮我做三件事")

    names = gw.calls[0]["tools"]
    assert "create_plan" in names and "step_complete" in names  # 全量工具面
    assert "escalate_to_plan" in names


async def test_board_alive_skips_triage_model_call(work, monkeypatch):
    """板在＝已拆解过→跳过自判（零模型调用、不落 triage 块）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    assert (await board_mod.create_board(
        wid, cid, "n", [{"id": "s1", "prompt": "a", "type": "execute"}]))["ok"]
    calls = {"n": 0}

    async def fake_call(system, user):
        calls["n"] += 1
        return '{"verdict": "direct", "reason": "x"}'

    monkeypatch.setattr(triage_mod, "_call_model", fake_call)
    gw = ScriptedGateway(script=[[
        {"name": "step_complete", "arguments": {"result": "直接做完"}},
    ]])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _post(client, wid, cid, "继续")

    assert calls["n"] == 0  # 自判模型零调用
    blocks = await session_facade.read(wid, cid)
    assert not [b for b in blocks if b.get("behavior") == "triage"]


# ------------------------------------------------- t165 注入＋互斥

async def test_t165_region_injected_with_live_board(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "注入计划",
                                 [{"id": "s1", "prompt": "第一件事", "type": "execute"},
                                  {"id": "s2", "prompt": "第二件事", "type": "think"}])
    regions = context_assembler.build_context_regions(wid, cid)
    t165 = [r for r in regions if r.get("order") == 165]
    assert len(t165) == 1
    text = t165[0]["text"]
    assert "注入计划" in text          # 计划名
    assert "1/2" in text               # 当前第几步
    assert "第一件事" in text          # 本步要求原文


async def test_t165_absent_without_board(work):
    wid, cid = work["work_id"], work["work_card_id"]
    regions = context_assembler.build_context_regions(wid, cid)
    assert not [r for r in regions if r.get("order") == 165]


async def test_t165_mutual_exclusion_with_t160(work, monkeypatch):
    """互斥实测：工具卡运行时（t160 在）→ t165 不注入（工具卡先到先得）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "n", [{"id": "s1", "prompt": "a", "type": "execute"}])

    from src.toolcard import step_prompt as toolcard_step_prompt
    monkeypatch.setattr(
        toolcard_step_prompt, "build_region",
        lambda w, c: {"order": 160, "name": "工具卡", "channel": "system",
                      "seats": ("main",), "text": "假 t160 区", "entries": 1})

    regions = context_assembler.build_context_regions(wid, cid)
    assert [r for r in regions if r.get("order") == 160]      # t160 在
    assert not [r for r in regions if r.get("order") == 165]  # t165 让位
