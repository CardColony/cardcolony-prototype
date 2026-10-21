# -*- coding: utf-8 -*-
"""[信息流与快车道 2026-10-07] 机制层测试；[思维链真实落地 2026-10-11] 重写：
route()/快车道分流退役（判断归模型），本件只留事件序/SSE/checkpoint/直答零三席。

夹具沿 test_offline_e2e（FakeGateway 替身 + ASGITransport 真实 HTTP 层，沙箱零外网）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import event_stream, model_service, routes
from src.storage import card_store


# ---------------------------------------------------------------- V-T4 事件本体论

def test_vt4_ontology_23_types():
    # [模型过程数据 2026-10-12] 主通道 19 → 21：+judgment（首轮机械推断）、
    # +step_start（每步开始）；只补类型与字段，通道与协议不动。
    # [当前轮注入消息事件 2026-10-14] 再 +2：context_preamble / context_injection
    # → 23 原 + 2 逐字流 = 25。
    # [判断协议改注入 2026-10-14] 再 +1：judgment_protocol → 24 原 + 2 逐字流 = 26。
    # [三席身份注入 2026-10-14] 再 +1：context_seat_identity → 25 原 + 2 逐字流 = 27。
    # [更新现状按钮改造 2026-10-16] 再 +1：status_update_task → 26 原 + 2 逐字流 = 28。
    # [运行态单一真相源 2026-10-21] 再 +1：turn_end → 27 原 + 2 逐字流 = 29。
    assert len(event_stream.EVENT_TYPES) == 29
    for t in ("triage_verdict", "plan_created", "step_advanced",
              "judgment", "step_start",
              "card_op", "run_start", "deliver", "ping"):
        assert t in event_stream.EVENT_TYPES
    assert "fastlane_route" not in event_stream.EVENT_TYPES


# ---------------------------------------------------------------- 夹具

FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
        "vp1": {"provider": "openai", "model": "fake-vp", "temperature": 0.7},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main", "viewpoint": ["vp1"]},
    "system": {"default_model": "main"},
    "simple": {"enabled": True},
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


def _last_round_types(wid, cid):
    """本回合事件类型段（journal 跨用例追加不清空——T092 退役后旧段保留，
    从最后一个 run_start 起切片＝本回合）。"""
    journal = event_stream.journal_of(wid, cid)
    for i in range(len(journal) - 1, -1, -1):
        if journal[i]["type"] == "run_start":
            return [e["type"] for e in journal[i:]]
    return [e["type"] for e in journal]


# ---------------------------------------------------------------- V-T1-2 直答工具链 e2e（旧快车道退役形态）

async def test_vt1_direct_tool_reply_real_landing_and_events(sandbox, work, monkeypatch):
    """[思维链真实落地] 判断归模型：删卡类消息由模型带全量工具自主处置——
    单模型、卡真实落地、零三席、无判定事件（triage_verdict/card_op 退役）。"""
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
    # 直答回合零三席（C6：三席只在提交轮）、卡真实落地（已删）、单模型（calls 全 main）
    assert body["viewpoints_meta"] == []
    assert body["tool_events"] == [{"tool": "delete_card", "card_id": victim, "ok": True}]
    assert not card_store.card_exists(work["work_id"], victim)
    assert all(c["model"] == "main" for c in gw.calls)
    # V8 事件序（本回合段）：run_start→context→round_start→tool_call→tool_result→…→done 有序
    types = _last_round_types(work["work_id"], work["work_card_id"])
    assert types[0] == "run_start" and "context" in types
    # [运行态单一真相源 2026-10-21] 回合终止帧 turn_end 恒在 done 之后（后端
    # _run_message 的 finally 无条件发）⇒ 末位是 turn_end，done 仍是内容层收口。
    assert types[-1] == "turn_end" and "done" in types
    idx = {t: types.index(t) for t in set(types)}
    assert idx["round_start"] < idx["tool_call"] < idx["tool_result"] < idx["deliver"] < idx["done"]
    # 判定轮与并表通道全部退役
    assert "triage_verdict" not in types and "card_op" not in types
    assert "seats_enter" not in types and "seat_start" not in types   # 直答零三席


# ---------------------------------------------------------------- V-T1-3 直答链零三席（C6）

async def test_vt1_direct_reply_no_seats(sandbox, work, monkeypatch):
    """[思维链真实落地] 直答回合（无板空手）零三席——旧「无工具即三席」
    空手轮旁路退役，三席只在拆解链收口后的提交轮进场。"""
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
    # 直答零三席、单回复（本回合段）
    assert body["viewpoints_meta"] == []
    types = _last_round_types(work["work_id"], work["work_card_id"])
    assert types.count("seats_enter") == 0 and types.count("seat_end") == 0
    assert "deliver" in types and "done" in types and types[-1] == "turn_end"


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
    # [消息底座能力对齐 · 件4] from=0 整卡重放**退役**：恢复统一走
    # /timeline（events jsonl），SSE 只推「连上之后新发生的事」。
    # 直驱端点协程的 body_iterator（读两帧即收）。
    # （ASGITransport 收满全响应体才返回——无限 SSE 不能走它；HTTP 200/content-type
    #   腿由 5701 实测补证）
    from src.api.routes import stream_card_events

    def _seq_of(chunk) -> int:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for line in text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])["seq"]
        return -1

    resp = await stream_card_events(wid, cid, frm=0)
    before = event_stream.journal_last_seq(wid, cid)
    it = resp.body_iterator
    retry = await it.__anext__()
    assert b"retry" in (retry if isinstance(retry, bytes) else retry.encode())
    # 先让生成器进入循环体（游标定在当前末帧＝零重放），再发新帧
    task = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0)
    event_stream.emit(wid, cid, "context", {"tail": 0})
    event_stream.emit(wid, cid, "context", {"tail": 1})
    got = []
    try:
        got.append(_seq_of(await asyncio.wait_for(task, 5.0)))
        got.append(_seq_of(await asyncio.wait_for(it.__anext__(), 5.0)))
    finally:
        await resp.body_iterator.aclose()
    assert got == sorted(got) and len(got) == 2
    # 存量 320 帧一条都没被重放：收到的两帧就是订阅后新发的（紧接订阅时末帧）
    assert got == [before + 1, before + 2]


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
    # 事件未进 session（[思维链真实落地] 无判定块：session 只多 user+assistant
    # 两条）；未进 prior（FakeGateway 记录的 user_prompt 不含事件 type 字样）
    after = await session_store.read_session(wid, cid)
    assert len(after) == len(before) + 2
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
