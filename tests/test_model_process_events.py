# -*- coding: utf-8 -*-
"""[模型过程数据 2026-10-12] 主通道过程事件**数据层**验收（判据 1-4）。

覆盖：
- 多步＋三席场景：judgment×1（plan）／plan_created×1（带完整 steps）／
  step_start×2／step_advanced×2（补 step_id/criteria_met/result/next_*）／
  tool_call·tool_result 携 step_id 且**同一次调用 step_id 一致**／seat_end×3；
- 直答场景：judgment×1（direct）且无任何步进事件、seat_end×0；
- seat_end.text 与 prompt 日志 viewpoints[].text 为同一份清洗后正文
  （HTTP 响应仍剔除 text，协议不变）。

通道与协议零改动：只查主 journal（journal_of）与落盘日志。零真实 works
触碰；不 import main。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import event_stream, model_service, routes
from src.thinkchain import board as board_mod
from tests.test_s4_seams import ScriptedGateway, _last_round_types, _post

# 三席齐全的假配置（vp1/vp2/vp3 全绑定 viewpoint）
THREE_SEATS_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
        "vp1": {"provider": "openai", "model": "fake-vp1", "temperature": 0.7},
        "vp2": {"provider": "openai", "model": "fake-vp2", "temperature": 0.7},
        "vp3": {"provider": "openai", "model": "fake-vp3", "temperature": 0.7},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main",
                 "viewpoint": ["vp1", "vp2", "vp3"]},
    "system": {"default_model": "main"},
    "simple": {"enabled": True},
}

_TWO_STEP_SCRIPT = [
    [{"name": "create_plan", "arguments": {
        "name": "两步计划",
        "steps": [
            {"id": "s1", "prompt": "做甲", "criteria": "甲的判据是文件存在",
             "deliverable": "甲产出", "type": "execute"},
            {"id": "s2", "prompt": "做乙", "criteria": "乙的判据是结论落板",
             "deliverable": "乙产出", "type": "think"}]}}],
    [{"name": "step_complete", "arguments": {"result": "甲做完了",
                                             "criteria_met": "判据满足：文件在"}}],
    [{"name": "step_complete", "arguments": {"result": "乙做完了",
                                             "criteria_met": "判据满足：结论在"}}],
    [],   # 收口后空手 → 提交轮三席
]


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


def _journal(wid: str, cid: str) -> list[dict[str, Any]]:
    """本回合事件段：journal 跨用例追加不清空（work_id 可复用），从最后一个
    run_start 起切片＝本回合（与 test_s4_seams._last_round_types 同法）。"""
    journal = event_stream.journal_of(wid, cid)
    for i in range(len(journal) - 1, -1, -1):
        if journal[i]["type"] == "run_start":
            return journal[i:]
    return list(journal)


def _evs(wid: str, cid: str, ev_type: str) -> list[dict[str, Any]]:
    return [e for e in _journal(wid, cid) if e["type"] == ev_type]


def _data(wid: str, cid: str, ev_type: str) -> list[dict[str, Any]]:
    return [e["data"] for e in _evs(wid, cid, ev_type)]


async def _run_planned(work, monkeypatch) -> dict:
    wid, cid = work["work_id"], work["work_card_id"]
    gw = ScriptedGateway(script=list(_TWO_STEP_SCRIPT), config=THREE_SEATS_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await _post(client, wid, cid, "帮我把甲乙两件事做成")


async def test_planned_run_full_process_data(work, monkeypatch, sandbox):
    wid, cid = work["work_id"], work["work_card_id"]
    body = await _run_planned(work, monkeypatch)

    # ---------- 判据 1：事件齐全 ----------
    assert body["reply"]
    jr = _data(wid, cid, "judgment")
    assert len(jr) == 1 and jr[0]["verdict"] == "plan"

    pc = _data(wid, cid, "plan_created")
    assert len(pc) == 1
    assert pc[0]["name"] == "两步计划" and pc[0]["total"] == 2
    steps = pc[0]["steps"]
    assert [s["id"] for s in steps] == ["s1", "s2"]
    assert steps[0]["prompt"] == "做甲"
    assert steps[0]["criteria"] == "甲的判据是文件存在"
    assert steps[0]["deliverable"] == "甲产出"
    assert steps[0]["type"] == "execute"
    assert steps[1]["type"] == "think"

    ss = _data(wid, cid, "step_start")
    assert len(ss) == 2
    assert ss[0]["step_id"] == "s1" and ss[0]["index"] == 1 and ss[0]["total"] == 2
    assert ss[0]["prompt"] == "做甲" and ss[0]["criteria"] == "甲的判据是文件存在"
    assert ss[1]["step_id"] == "s2" and ss[1]["index"] == 2

    sa = _data(wid, cid, "step_advanced")
    assert len(sa) == 2
    assert sa[0]["step_id"] == "s1"
    assert sa[0]["criteria_met"] == "判据满足：文件在"
    assert sa[0]["result"] == "甲做完了"
    assert sa[0]["next_prompt"] == "做乙"
    assert sa[0]["next_criteria"] == "乙的判据是结论落板"
    assert sa[0]["advanced"] is True and sa[0]["all_done"] is False
    assert sa[1]["step_id"] == "s2" and sa[1]["result"] == "乙做完了"
    assert sa[1]["all_done"] is True
    assert sa[1]["next_prompt"] == "" and sa[1]["next_criteria"] == ""

    se = _data(wid, cid, "seat_end")
    assert len(se) == 3
    assert all(m.get("ok") is True and m.get("text") for m in se)

    # ---------- 判据 2：顺序（判断在拆解前，拆解在步开始前） ----------
    def _pos(ev_type: str, pred=lambda d: True) -> int:
        for e in _journal(wid, cid):
            if e["type"] == ev_type and pred(e["data"]):
                return e["seq"]
        raise AssertionError(f"event not found: {ev_type}")

    assert _pos("judgment") < _pos("plan_created") < _pos(
        "step_start", lambda d: d["step_id"] == "s1") < _pos(
        "step_advanced", lambda d: d["step_id"] == "s1") < _pos(
        "step_start", lambda d: d["step_id"] == "s2")

    # ---------- 判据 3：tool_call/tool_result 同源 step_id ----------
    tcalls = _data(wid, cid, "tool_call")
    tresults = _data(wid, cid, "tool_result")
    assert len(tcalls) == len(tresults) and len(tcalls) >= 3
    for c, r in zip(tcalls, tresults):
        assert c["step_id"] == r["step_id"]          # 同一次调用不劈叉
        assert c["tool"] == r["tool"]
    by_tool: dict[str, set[str]] = {}
    for c in tcalls:
        by_tool.setdefault(c["tool"], set()).add(c["step_id"])
    # create_plan 执行前板还没上架 → 空串（无板口径）；交作业按当前步归属
    assert by_tool["create_plan"] == {""}
    assert by_tool["step_complete"] == {"s1", "s2"}

    # ---------- 判据 4：seat_end.text ≡ prompt 日志 viewpoints[].text ----------
    logged: list[str] = []
    for p in sorted(sandbox.logs.glob(f"prompt_{cid}_*.json")):
        payload = json.loads(p.read_text(encoding="utf-8"))
        for m in payload.get("viewpoints") or []:
            if m.get("ok") is True:
                logged.append(m.get("text") or "")
    assert sorted(logged) == sorted(m["text"] for m in se)
    # HTTP 响应仍剔除 text（协议不变）
    assert all("text" not in m for m in body["viewpoints_meta"])


async def test_direct_run_judgment_and_no_step_events(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    gw = ScriptedGateway(script=[[]], config=THREE_SEATS_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = await _post(client, wid, cid, "你好呀")

    assert body["viewpoints_meta"] == []
    jr = _data(wid, cid, "judgment")
    assert len(jr) == 1 and jr[0]["verdict"] == "direct"
    # 无板 → 零步进事件，工具事件 step_id 全空串
    assert _evs(wid, cid, "plan_created") == []
    assert _evs(wid, cid, "step_start") == []
    assert _evs(wid, cid, "step_advanced") == []
    assert _evs(wid, cid, "seat_end") == []
    assert _evs(wid, cid, "tool_call") == []
    assert "seats_enter" not in _last_round_types(wid, cid)


async def test_step_start_idempotent_within_step(work, monkeypatch):
    """同一板步内多次工具调用只发一次 step_start；换步才再发。"""
    wid, cid = work["work_id"], work["work_card_id"]
    board = await board_mod.create_board(wid, cid, "查证计划", [
        {"id": "s1", "prompt": "先查资料", "criteria": "资料判据",
         "deliverable": "资料产出", "type": "execute"},
        {"id": "s2", "prompt": "再写结论", "criteria": "结论判据",
         "deliverable": "结论产出", "type": "think"},
    ])
    assert board["ok"], board
    gw = ScriptedGateway(script=[
        [{"name": "read_card", "arguments": {"card_id": cid}},    # 步 1 内两次工具
         {"name": "read_card", "arguments": {"card_id": cid}}],
        [{"name": "step_complete", "arguments": {"result": "资料已查",
                                                 "criteria_met": "判据满足：资料在"}}],
        [{"name": "step_complete", "arguments": {"result": "结论已写",
                                                 "criteria_met": "判据满足：结论在"}}],
        [],
    ], config=THREE_SEATS_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _post(client, wid, cid, "开工")

    ss = _data(wid, cid, "step_start")
    assert [d["step_id"] for d in ss] == ["s1", "s2"]   # 每步恰一次
    # 判定只读 tool_calls 事实：本轮工具面里没有 create_plan → direct
    # （板是用例预建的，judgment 不看板——机械推断口径）
    assert _evs(wid, cid, "judgment")[0]["data"]["verdict"] == "direct"
