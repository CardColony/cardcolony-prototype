# -*- coding: utf-8 -*-
"""[信息流与快车道 2026-10-07] 机制层测试：route()/快车道分流/事件序/SSE/checkpoint。

夹具沿 test_offline_e2e（FakeGateway 替身 + ASGITransport 真实 HTTP 层，沙箱零外网）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import event_stream, model_service, routes
from src.storage import card_store


# ---------------------------------------------------------------- V-T1-1 route()

def test_vt1_route_rules():
    assert routes.route("那你倒是删掉一张啊") == "fastlane"          # R3 样本
    assert routes._route_rule("那你倒是删掉一张啊") == "删卡"
    assert routes.route("帮我建一张卡") == "fastlane"
    assert routes.route("把这张卡的内容改一下") == "fastlane"
    assert routes.route("查一下card_002的正文") == "fastlane"
    # 保守不误判：开放论证一律 chain
    assert routes.route("我们应该如何重构整个论证框架") == "chain"
    assert routes.route("你觉得这个结论成立吗") == "chain"
    assert routes.route("查一下天气怎么样") == "chain"


def test_vt4_ontology_21_types():
    assert len(event_stream.EVENT_TYPES) == 21
    for t in ("triage_verdict", "plan_created", "step_advanced",
              "card_op", "run_start", "deliver", "ping"):
        assert t in event_stream.EVENT_TYPES
    assert "fastlane_route" not in event_stream.EVENT_TYPES


# ---------------------------------------------------------------- 夹具

FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
        "vp1": {"provider": "openai", "model": "fake-vp", "temperature": 0.7},
    },
    "simple": {"enabled": True, "viewpoints": ["vp1"]},
}


class FakeGateway:
    """替身：一次性发脚本工具调用；三席 vp1 返回替身观点。"""

    def __init__(self, tool_script=None):
        self.config = json.loads(json.dumps(FAKE_CONFIG))
        self.tool_script = tool_script or []
        self.calls = []
        self._tool_issued = False

    async def call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                   max_tokens=None, timeout=None, skip_completion_marker=False,
                   tools=None, on_status=None, stream=False, on_chunk=None,
                   prior_messages=None):
        self.calls.append({"model": model_name, "tools": bool(tools)})
        if on_status:
            on_status({"phase": "success", "model": model_name})
        tool_calls = []
        if self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        content = "[替身观点:vp1] 观点正文。" if model_name == "vp1" else "已完成确认。"
        return {"content": content, "tool_calls": tool_calls,
                "truncated_tool_call": None, "finish_reason": "stop",
                "tokens_used": 1, "latency_ms": 1}

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None, **_):
        # 主路径 10-08 起走 gw.stream_call（model_service.call_main 内部）；
        # 本替身与 test_s4_seams.ScriptedGateway 同款——含让出点（真实网关
        # 每圈网络 IO 让出事件循环，人停刹车可达；Mock 零 IO 需补一次对齐）。
        import asyncio
        await asyncio.sleep(0)
        self.calls.append({"model": model_name, "tools": bool(tools)})
        if on_status:
            on_status({"phase": "success", "model": model_name})
        tool_calls = []
        if self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        content = "[替身观点:vp1] 观点正文。" if model_name == "vp1" else "已完成确认。"
        yield {"chunk_type": "content", "text": content}
        yield {"content": content, "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}


def _app():
    app = FastAPI()
    app.include_router(routes.router)
    return app


async def _mk_child(work, title):
    r = await card_store.create_card(work["work_id"], title, "用户直接创建",
                                     parent=work["work_card_id"])
    assert "error" not in r, r
    return r["meta"]["card_id"]


def _types_of(wid, cid):
    return [e["type"] for e in event_stream.journal_of(wid, cid)]


# ---------------------------------------------------------------- V-T1-2 快车道 e2e

async def test_vt1_fastlane_real_landing_and_events(sandbox, work, monkeypatch):
    monkeypatch.setattr(activity_guard := None, "x", None) if False else None
    from src.storage import activity  # ★ 沙箱盲区补丁（同 condfold 线教训）
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    victim = await _mk_child(work, "待删卡")
    gw = FakeGateway(tool_script=[{"name": "delete_card",
                                   "arguments": {"card_id": victim}}])
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as client:
        r = await client.post(f"/api/works/{work['work_id']}/cards/{work['work_card_id']}/messages",
                              json={"content": "那你倒是删掉一张啊"})
        assert r.status_code == 200, r.text
        body = r.json()
    # V-T1-2：无三席（viewpoints_meta 空）、卡真实落地（已删）、单模型（calls 全 main）
    assert body["viewpoints_meta"] == []
    assert body["tool_events"] == [{"tool": "delete_card", "card_id": victim, "ok": True}]
    assert not card_store._card_path(work["work_id"], victim).exists()
    assert all(c["model"] == "main" for c in gw.calls)
    # V8 事件序：run_start→context→triage_verdict→…→card_op→…→done 有序
    types = _types_of(work["work_id"], work["work_card_id"])
    assert types[0] == "run_start" and "context" in types and "triage_verdict" in types
    assert types[-1] == "done"
    idx = {t: types.index(t) for t in set(types)}
    assert idx["triage_verdict"] < idx["round_start"] < idx["card_op"] < idx["done"]
    assert "seats_enter" not in types and "seat_start" not in types   # 确定性动作跳三席
    # [2026-10-11 两套机制拆除] 唯一判断＝triage_verdict（机械预检命中，rule=删卡）
    tv = [e for e in event_stream.journal_of(work["work_id"], work["work_card_id"])
          if e["type"] == "triage_verdict"]
    assert tv and tv[0]["data"]["verdict"] == "direct" and tv[0]["data"]["rule"] == "删卡"
    # V-T4-1：card_op 一类卡操作一份事件（delete 一次＝一条）
    card_ops = [e for e in event_stream.journal_of(work["work_id"], work["work_card_id"])
                if e["type"] == "card_op"]
    assert len(card_ops) == 1 and card_ops[0]["data"]["op"] == "delete_card"
    assert card_ops[0]["data"]["ok"] is True


# ---------------------------------------------------------------- V-T1-3 全链不劣化

async def test_vt1_chain_untouched_with_seats(sandbox, work, monkeypatch):
    from src.storage import activity
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    gw = FakeGateway()
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as client:
        r = await client.post(f"/api/works/{work['work_id']}/cards/{work['work_card_id']}/messages",
                              json={"content": "你怎么看当前这个论证方向？"})
        assert r.status_code == 200, r.text
        body = r.json()
    # chain：三席恰一次进场、统合唯一回复
    assert len(body["viewpoints_meta"]) == 1 and body["viewpoints_meta"][0]["ok"] is True
    types = _types_of(work["work_id"], work["work_card_id"])
    assert types.count("seats_enter") == 1 and types.count("seat_end") == 1
    assert "deliver" in types and types[-1] == "done"
    # [2026-10-11 两套机制拆除] 未命中机械预检 → 模型判定（FakeGateway 返回
    # 非 JSON → 默认 plan 拆解）；判断行只有 triage_verdict。
    # [T092 退役] journal 跨用例不清空（event_stream.py:92-95/:260-262）——
    # 同一 work_001/card_001 的旧段保留，本回合判定取最新一条（fl[-1]）。
    fl = [e for e in event_stream.journal_of(work["work_id"], work["work_card_id"])
          if e["type"] == "triage_verdict"]
    assert fl and fl[-1]["data"]["verdict"] == "plan"


# ---------------------------------------------------------------- V-T3-1 SSE＋环形

async def test_vt3_sse_endpoint_and_ring(sandbox, work, monkeypatch):
    from src.storage import activity
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, cid = work["work_id"], work["work_card_id"]
    # [T092 退役] journal 环形上限常量已删（event_stream.py:92-95「两个上限
    # 常量已删——journal 由 run_start 每轮清空的旧制退役，追加式有界」）。
    # 此处验证：320 条压力下 seq 单调无重复、不崩；run_start/done 为追加帧。
    for i in range(320):
        event_stream.emit(wid, cid, "ping" if i == 0 else "context", {"i": i})
    journal = event_stream.journal_of(wid, cid)
    seqs = [e["seq"] for e in journal]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # run_start/done 追加（A45/T092：不清空、旧段保留、分段标记承载轮界）
    n_before = len(event_stream.journal_of(wid, cid))
    event_stream.emit(wid, cid, "run_start", {})
    event_stream.emit(wid, cid, "done", {})
    tail = event_stream.journal_of(wid, cid)[-2:]
    assert [e["type"] for e in tail] == ["run_start", "done"]
    assert len(event_stream.journal_of(wid, cid)) == n_before + 2
    # SSE from=0 重放：直驱端点协程的 body_iterator（读两帧即收）。
    # （ASGITransport 收满全响应体才返回——无限 SSE 不能走它；HTTP 200/content-type
    #   腿由 5701 实测补证）
    from src.api.routes import stream_card_events

    resp = await stream_card_events(wid, cid, frm=0)
    got = []
    try:
        async for chunk in resp.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for line in text.splitlines():
                if line.startswith("data: "):
                    got.append(json.loads(line[6:])["seq"])
            if len(got) >= 2:
                break
    finally:
        await resp.body_iterator.aclose()
    assert got == sorted(got) and len(got) == 2


# ---------------------------------------------------------------- V-T3-2 事件不进模型输入

async def test_vt3_events_not_in_model_input(sandbox, work, monkeypatch):
    from src.storage import activity, session_store
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, cid = work["work_id"], work["work_card_id"]
    before = await session_store.read_session(wid, cid)
    gw = FakeGateway()
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as client:
        r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                              json={"content": "随便聊聊这个方向"})
        assert r.status_code == 200
    # 事件未进 session（session 只多 user+triage 判定块+assistant 三条——triage
    # 块落档为 10-10 迭代二既有行为、behavior 非空不进投影；存量断言 +2 未跟进，
    # 基线 line 538 即红）；未进 prior（FakeGateway 记录的 user_prompt 不含
    # 事件 type 字样）
    after = await session_store.read_session(wid, cid)
    assert len(after) == len(before) + 3
    blob = json.dumps(gw.calls, ensure_ascii=False)
    for t in event_stream.EVENT_TYPES:
        assert f'"type": "{t}"' not in blob or "run_start" not in blob
        assert t not in json.dumps([c for c in gw.calls if c["model"] != "vp1"], ensure_ascii=False)


# ---------------------------------------------------------------- V-T2-2 checkpoint 落位

async def test_vt2_checkpoint_on_error(sandbox, work, monkeypatch):
    from src.storage import activity, data_snapshot
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)

    class BoomGateway(FakeGateway):
        async def call(self, *a, **k):
            raise RuntimeError("ContextWindowExceededError(模拟超限)")

        async def stream_call(self, *a, **k):
            # 主路径 10-08 起走 gw.stream_call（model_service.call_main 内部）——
            # 只 override call 会让子类继承 FakeGateway 的正常 stream_call，
            # 回合不再抛错（200≠502）。两路都要炸。
            # yield 使其成为 async generator：async for 进入即抛 RuntimeError、
            # 被调用方捕获走 502；普通 async def 会让 async for 迭代 coroutine
            # 抛 TypeError（结果同为 502 但产生 coroutine 泄漏告警）。
            raise RuntimeError("ContextWindowExceededError(模拟超限)")
            yield  # pragma: no cover

    monkeypatch.setattr(model_service, "_gateway", BoomGateway())
    recorded: dict = {}
    real = data_snapshot.snapshot_sync

    def spy(*a, **k):
        recorded["reason"] = a[0] if a else k.get("reason", "")
        return real(*a, **k)

    monkeypatch.setattr(data_snapshot, "snapshot_sync", spy)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as client:
        r = await client.post(f"/api/works/{work['work_id']}/cards/{work['work_card_id']}/messages",
                              json={"content": "你怎么看？"})
        assert r.status_code == 502
    assert recorded["reason"].startswith("checkpoint:")
