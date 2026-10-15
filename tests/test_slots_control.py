# -*- coding: utf-8 -*-
"""控制三态与六槽回归（迭代一·会话底座 · S7/A8：STOP|CONTINUE|DEFER_HUMAN，T106-T124）。

判据（机械可判定，依据设计 4.7/4.9）：
1. request_stop 幂等：无在途回合＝no-op（不报错、不造轮、不置位，T123）；
2. 有在途 → 置位生效；check 消费即清（consume-once，T120/T124）；
3. check_control 消费：STOP → end_reason="human_stop"；无置位 → None 放行；
4. 端到端（有工具轮）：调用进行中 POST /stop → 工具轮出口消费 → 回合闭合
   end=human_stop、回复空串（§5-10 共存禁令：工具卡驱动照常串行，不作停依据）；
5. 端到端（无工具轮）：中途置位但回合再无步边界 → 收口清扫（control.
   discard_stop）→ 本回合正常交付、下一回合不受影响（T124「不影响下一回合」）；
6. R1 注册表（迭代三 S1 填充）：config/behavior_registry.json 两语义条目
   （execute/think）、judge 判据位恒空串 → check 恒不返回 DEFER_HUMAN
   （接口非空置，判据内容一件不写的线不变）。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import control, message_runner, model_service, routes
from src.storage import session_facade

FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main"},
    "system": {"default_model": "main"},
    "simple": {"enabled": False},
}


class HangingGateway:
    """首轮 stream_call 挂起（等 release），之后按脚本发一次工具调用。

    挂起窗口模拟「模型调用进行中」——人停端点置位的真实时点。
    契约对齐 model_service.call_main 消费面（2026-10-08 起主路径走
    stream_call）：逐字 chunk 事件＋流末终帧（content/tool_calls 同 call）。
    """

    def __init__(self, tool_script: list[dict[str, Any]] | None = None):
        self.config = FAKE_CONFIG
        self.tool_script = tool_script or []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._tool_issued = False

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None, **_):
        self.entered.set()
        await self.release.wait()
        if on_status:
            on_status({"phase": "start", "model": model_name})
            on_status({"phase": "success", "model": model_name})
        tool_calls = []
        if self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        yield {"chunk_type": "content", "text": "罐头"}     # 逐字流增量（不进 content）
        yield {"content": "罐头正文", "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(routes.router)
    return a


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------- 1/2：置位与消费单元 ----------

def test_request_stop_idempotent_noop(sandbox):
    r = control.request_stop("w", "c")
    assert r["stopped"] is False and r["idempotent"] is True and r["holder"] == ""
    assert control.pending_stop("w", "c") is False, "无在途不得造轮（T123）"


def test_request_stop_with_inflight_and_consume_once(sandbox):
    assert session_facade.turn_try_acquire("w", "c", "turn:x") is True
    r = control.request_stop("w", "c")
    assert r["stopped"] is True and r["holder"] == "turn:x"
    assert control.pending_stop("w", "c") is True
    assert control.check("w", "c") == control.STOP, "置位被步边界消费"
    assert control.check("w", "c") == control.CONTINUE, "consume-once：只终止当前回合"
    session_facade.turn_release("w", "c", "turn:x")


def test_discard_stop_clears_residual(sandbox):
    session_facade.turn_try_acquire("w", "c", "turn:x")
    control.request_stop("w", "c")
    control.discard_stop("w", "c")
    assert control.pending_stop("w", "c") is False, "回合收口清扫（T124 不误伤下一回合）"
    session_facade.turn_release("w", "c", "turn:x")


# ---------- 3：check_control 消费段 ----------

def test_check_control_sets_human_stop(sandbox):
    state = message_runner.RunState(work_id="w", card_id="c", content="x")
    session_facade.turn_try_acquire("w", "c", "turn:x")
    control.request_stop("w", "c")
    verdict = message_runner.check_control(state)
    assert verdict == control.STOP
    assert state.end_reason == "human_stop"
    session_facade.turn_release("w", "c", "turn:x")


def test_check_control_passes_when_no_stop(sandbox):
    state = message_runner.RunState(work_id="w", card_id="c", content="x")
    assert message_runner.check_control(state) is None
    assert state.end_reason == ""


# ---------- 4：端到端（有工具轮：出口消费 → human_stop 闭合） ----------

async def test_midflight_stop_closes_turn_human_stop(sandbox, work, client):
    wid, cid = work["work_id"], "card_001"
    gw = HangingGateway(tool_script=[{"name": "read_card",
                                      "arguments": {"card_id": cid}}])
    model_service._gateway = gw

    task = asyncio.create_task(client.post(
        f"/api/works/{wid}/cards/{cid}/messages", json={"content": "查一下"}))
    await asyncio.wait_for(gw.entered.wait(), 5)      # 模型调用进行中
    r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")
    assert r.status_code == 200 and r.json()["stopped"] is True
    gw.release.set()
    resp = await asyncio.wait_for(task, 10)

    assert resp.status_code == 200, resp.text
    # 人停回合：出口消费 → end=human_stop。
    # [直接停止 2026-10-15 裁定] 人停＝**立刻断流**（不再等模型吐完）＋**已产出的字
    # 照常交付**（本测试钉的原意「不丢已产出的字」保留）。断流点在本帧处理之后，
    # 故交付的是**已收到的增量**「罐头」——终帧「罐头正文」属未收到部分，不交付。
    assert resp.json()["reply"] == "罐头\n", resp.json()
    blocks = await session_facade.read(wid, cid)
    assert blocks[-1]["end"] == "human_stop", "回合以 human_stop 闭合（T124）"
    assert blocks[-1]["actor"] == "assistant"
    assert control.pending_stop(wid, cid) is False, "消费即清"


# ---------- 5：端到端（无工具轮：收口清扫、不误伤） ----------

async def test_no_tool_round_discards_stale_stop_and_next_turn_clean(sandbox, work, client):
    wid, cid = work["work_id"], "card_001"
    gw = HangingGateway()                             # 无脚本 → 无工具 → 直接交付
    model_service._gateway = gw

    task = asyncio.create_task(client.post(
        f"/api/works/{wid}/cards/{cid}/messages", json={"content": "说话"}))
    await asyncio.wait_for(gw.entered.wait(), 5)
    r = await client.post(f"/api/works/{wid}/cards/{cid}/stop")
    assert r.status_code == 200 and r.json()["stopped"] is True
    gw.release.set()
    resp = await asyncio.wait_for(task, 10)

    assert resp.status_code == 200, resp.text
    # [直接停止 2026-10-15 裁定] 无步边界可消费 → 本回合正常交付；
    # 交付内容＝断流前**已收到的增量**（「不丢已产出的字」口径不变）。
    assert resp.json()["reply"] == "罐头\n", "无步边界可消费 → 本回合正常交付"
    blocks = await session_facade.read(wid, cid)
    assert blocks[-1]["end"] == "model_done", "正常闭合（stop 未在步边界命中）"
    assert control.pending_stop(wid, cid) is False, "收口清扫：残留置位已作废"

    # 下一回合不受影响（T124）：正常 POST、正常交付
    gw2 = HangingGateway()
    gw2.release.set()          # 即时放行——本段只验证残留 stop 已清扫、不误伤
    model_service._gateway = gw2
    r2 = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                           json={"content": "再来"})
    assert r2.status_code == 200 and r2.json()["reply"] == "罐头正文\n", r2.text


# ---------- 6：端点与 R1 空壳 ----------

async def test_stop_endpoint_404(client):
    r = await client.post("/api/works/work_none/cards/card_001/stop")
    assert r.status_code == 404


def test_behavior_registry_two_types_judge_empty(sandbox):
    """R1：类型注册表迭代三填充（S1）——两语义条目、判据位仍空。

    迭代一原断言（version 1 空壳且 types 空 dict）随填充退役；本测试守的线
    不变：判据内容一件不写（judge 恒空串 → check 恒 CONTINUE，X4 线）。
    改名不改职责。
    """
    from pathlib import Path
    cfg = Path(__file__).resolve().parents[1] / "config" / "behavior_registry.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data.get("version") == 2
    types = data.get("types")
    assert isinstance(types, dict) and set(types.keys()) == {"execute", "think"}
    for name, entry in types.items():
        assert set(entry.keys()) == {"display", "judge", "note"}, name
        assert entry["display"], f"{name}.display 非空"
        assert entry["judge"] == "", f"{name}.judge 判据位必须空串（X4 线）"
        assert entry["note"], f"{name}.note 非空"
    assert control.check("w", "c") == control.CONTINUE, "判据位空 → 恒 CONTINUE"
