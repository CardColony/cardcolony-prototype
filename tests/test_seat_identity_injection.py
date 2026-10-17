# -*- coding: utf-8 -*-
"""三席身份注入（2026-10-14）· 回归网。

规格：三席身份提示词**走注入**（`role=assistant`、第一人称自述、抬头〔三席身份〕），
经 `gw.stream_call` 的 `turn_tail` 随三席调用下发；**三席 system（共享卡片上下文）
与 user 位质询文（REVIEW_TASK_NOTE 三问）都不动**。事件单列一类
`context_seat_identity`（`source_kind="viewpoint"`）。

沙箱纪律同 conftest：零真实 works 触碰、不 import main。
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import context_assembler, event_stream, model_service, prompts, routes, simple_service
from src.model_gateway.gateway import ModelGateway
from src.storage import event_log, session_store
from src.thinkchain import board as board_mod

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

FAKE_CONFIG_SEATS: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
        "vp1": {"provider": "openai", "model": "fake-vp1", "temperature": 0.7},
        "vp2": {"provider": "openai", "model": "fake-vp2", "temperature": 0.7},
        "vp3": {"provider": "openai", "model": "fake-vp3", "temperature": 0.7},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main", "viewpoint": ["vp1", "vp2", "vp3"]},
    "system": {"default_model": "main"},
    "simple": {"enabled": True},
}


def _steps_def(*prompts_: str) -> list[dict]:
    return [{"id": f"s{i + 1}", "prompt": p, "criteria": f"{p}的判据",
             "deliverable": f"{p}的产出", "type": "execute"}
            for i, p in enumerate(prompts_)]


class SeatRecordingGateway:
    """记录每次 stream_call 入参（含 turn_tail）的替身网关——主模型与三席都记。"""

    def __init__(self, script: list[list[dict[str, Any]]] | None = None,
                 config: dict[str, Any] | None = None):
        self.config = config or FAKE_CONFIG_SEATS
        self.script = script or []
        self.calls: list[dict[str, Any]] = []

    async def call(self, *a, **k):
        raise AssertionError("主路径应走 stream_call")

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None,
                          turn_tail=None, turn_once=None, **_):
        await asyncio.sleep(0)   # 与生产网络 IO 的让出语义对齐
        self.calls.append({
            "model": model_name,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prior": list(prior_messages or []),
            "turn_tail": list(turn_tail or []),
        })
        idx = len(self.calls) - 1
        tool_calls = self.script[idx] if idx < len(self.script) else []
        content = ("[替身观点] 判据逐条核对通过。" if model_name.startswith("vp")
                   else "回合正文")
        yield {"chunk_type": "content", "text": content}
        yield {"content": content, "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}


@pytest.fixture(autouse=True)
def _reset_state():
    event_stream._journals.clear()
    event_stream._subscribers.clear()
    event_stream._seq_counters.clear()
    event_stream._token_journals.clear()
    event_stream._token_subscribers.clear()
    event_stream._anchors.clear()
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(routes.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _post(client, wid: str, cid: str, content: str) -> dict:
    r = await asyncio.wait_for(
        client.post(f"/api/works/{wid}/cards/{cid}/messages",
                    json={"content": content}), timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def _seat_calls(gw: SeatRecordingGateway) -> list[dict[str, Any]]:
    return [c for c in gw.calls if str(c["model"]).startswith("vp")]


async def _run_submit_round(work, monkeypatch):
    """建板 → 两步收口 → 提交轮三席进场。返回 (wid, cid, gw, body)。"""
    wid, cid = work["work_id"], work["work_card_id"]
    gw = SeatRecordingGateway(script=[
        [{"name": "create_plan", "arguments": {
            "name": "两步计划", "steps": _steps_def("做甲", "做乙")}}],
        [{"name": "step_complete", "arguments": {
            "result": "甲做完了", "criteria_met": "甲的判据满足"}}],
        [{"name": "step_complete", "arguments": {
            "result": "乙做完了", "criteria_met": "乙的判据满足"}}],
        [],   # 收口后空手 → 提交轮触发
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with _client() as client:
        body = await _post(client, wid, cid, "帮我把甲乙两件事做成")
    await event_log.drain()
    return wid, cid, gw, body


# ---------------------------------------------------------------------------
# V1＋V4：三席调用 turn_tail 含身份注入；三席各拿到
# ---------------------------------------------------------------------------


async def test_v1_v4_every_seat_gets_identity_in_turn_tail(sandbox, work, monkeypatch):
    wid, cid, gw, body = await _run_submit_round(work, monkeypatch)
    seats = _seat_calls(gw)
    assert len(seats) == 3, [c["model"] for c in gw.calls]     # V4：三席各一次
    for c in seats:                                            # V1：每次入参都带
        assert len(c["turn_tail"]) == 1
        m = c["turn_tail"][0]
        assert m["role"] == "assistant"
        assert simple_service.SEAT_IDENTITY_MARK in m["content"]
        assert simple_service.SEAT_IDENTITY_NOTE in m["content"]


# ---------------------------------------------------------------------------
# V2＋N1＋N6：三席 system 逐字不变（＝共享上下文，零指令）
# ---------------------------------------------------------------------------


async def test_v2_v_n1_seat_system_unchanged(sandbox, work, monkeypatch):
    wid, cid, gw, body = await _run_submit_round(work, monkeypatch)
    seats = _seat_calls(gw)
    assert seats
    sys0 = seats[0]["system_prompt"]
    # 三席 system 同一份共享上下文
    assert all(c["system_prompt"] == sys0 for c in seats)
    # 是共享的卡片上下文（含卡片清单），且**零指令**——身份不进 system
    assert "## 卡片清单" in sys0
    assert simple_service.SEAT_IDENTITY_MARK not in sys0
    assert simple_service.SEAT_IDENTITY_NOTE not in sys0
    assert "观点审议席" not in sys0
    # 与改前的共享上下文逐字相等（vp_system_prompt＝中性上下文原样）
    assert sys0 == context_assembler.build_neutral_context(wid, cid)


def test_v2_viewpoint_system_prompt_is_pure_context():
    """vp_system_prompt 生成器零指令（身份/文案不进 system 的根因）。"""
    assert prompts.build_viewpoint_system_prompt("CTX") == "CTX"
    assert prompts.build_viewpoint_system_prompt("") == ""


# ---------------------------------------------------------------------------
# V3：注入文案 ＝ 定稿文案（逐字）
# ---------------------------------------------------------------------------


def test_v3_identity_text_verbatim():
    assert simple_service.SEAT_IDENTITY_NOTE == (
        "我是本轮的三位观点审议席之一。三席各自独立并行，我看不到另外两席的输出。\n"
        "\n"
        "我的任务：基于共享的卡片上下文与执行材料，给出我自己的意见。\n"
        "- 我独立判断，不附和；\n"
        "- 我有依据地说，指出依据在哪；\n"
        "- 我的结论明确（通过／不通过、同意／不同意）。"
    )
    msg = simple_service.build_seat_identity_message()
    assert msg == {"role": "assistant",
                   "content": "〔三席身份〕\n" + simple_service.SEAT_IDENTITY_NOTE}


# ---------------------------------------------------------------------------
# V5：事件 emit 一次：type=context_seat_identity、source_kind=viewpoint
# ---------------------------------------------------------------------------


async def test_v5_event_emitted_once(sandbox, work, monkeypatch):
    wid, cid, gw, body = await _run_submit_round(work, monkeypatch)
    recs = [r for r in event_log.timeline(wid, cid)
            if r["type"] == "context_seat_identity"]
    assert len(recs) == 1
    assert recs[0]["data"]["source_kind"] == "viewpoint"
    assert recs[0]["data"]["content"] == simple_service.SEAT_IDENTITY_NOTE
    # 单列一类——不冒充 context_preamble
    types = [r["type"] for r in event_log.timeline(wid, cid)]
    assert "context_seat_identity" in event_stream.EVENT_TYPES


# ---------------------------------------------------------------------------
# V6＋N3：注入不进 session/prior；观点仍不进
# ---------------------------------------------------------------------------


async def test_v6_injection_not_in_session_nor_prior(sandbox, work, monkeypatch):
    wid, cid, gw, body = await _run_submit_round(work, monkeypatch)
    raw = session_store._session_path(wid, cid).read_text(encoding="utf-8")
    assert "〔三席身份〕" not in raw
    assert simple_service.SEAT_IDENTITY_NOTE not in raw
    # 三席 prior 是纯对话历史切片，不含身份注入
    for c in _seat_calls(gw):
        assert all("〔三席身份〕" not in str(m.get("content") or "")
                   for m in c["prior"])
    # 观点回流铁则不破：三席观点不进 session
    assert "[替身观点]" not in raw


# ---------------------------------------------------------------------------
# V7：主模型路径零影响；缺省 turn_tail=None ⇒ 三席请求逐字同旧
# ---------------------------------------------------------------------------


async def test_v7_main_model_never_gets_seat_identity(sandbox, work, monkeypatch):
    wid, cid, gw, body = await _run_submit_round(work, monkeypatch)
    for c in gw.calls:
        if str(c["model"]).startswith("vp"):
            continue
        for m in c["turn_tail"]:
            assert "〔三席身份〕" not in str(m.get("content") or "")


async def test_v7_default_none_yields_empty_turn_tail(sandbox, monkeypatch):
    """缺省不传 turn_tail ⇒ 三席调用 turn_tail 为空（零行为变化）。"""
    gw = SeatRecordingGateway()
    monkeypatch.setattr(model_service, "_gateway", gw)
    await simple_service.run_viewpoints("SYS", "USER", None)
    seats = _seat_calls(gw)
    assert len(seats) == 3
    assert all(c["turn_tail"] == [] for c in seats)


# ---------------------------------------------------------------------------
# N2：身份不进 user 位质询文（REVIEW_TASK_NOTE 等不变）
# ---------------------------------------------------------------------------


async def test_n2_identity_not_in_user_prompt(sandbox, work, monkeypatch):
    wid, cid, gw, body = await _run_submit_round(work, monkeypatch)
    for c in _seat_calls(gw):
        assert simple_service.REVIEW_TASK_NOTE in c["user_prompt"]     # 三问不变
        assert "〔三席身份〕" not in c["user_prompt"]
        assert simple_service.SEAT_IDENTITY_NOTE not in c["user_prompt"]


# ---------------------------------------------------------------------------
# N4：gateway.stream_call 签名未改（turn_tail 已存在，够用）
# ---------------------------------------------------------------------------


def test_n4_gateway_stream_call_signature_untouched():
    params = inspect.signature(ModelGateway.stream_call).parameters
    assert "turn_tail" in params
    # 只加了「可选」参数——没有新增必填参数（缺省路径逐字同旧）
    assert params["turn_tail"].default is None


# ---------------------------------------------------------------------------
# 前端：stream.js 有该 case（否则 default:break → 过程区不渲染）
# ---------------------------------------------------------------------------


def test_frontend_has_seat_identity_case():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static" / "stream.js").read_text(
        encoding="utf-8")
    assert 'case "context_seat_identity"' in js
