# -*- coding: utf-8 -*-
"""当前轮注入消息事件回归（[当前轮注入消息事件 2026-10-14]）。

覆盖任务书验收判据：
- V1 本轮首条是 user_message；固定总领紧随其后；
- V2/V3 两类事件可辨、来源可追溯（工具卡 runtime/剧本/提示词/产物；思维链板快照/模板）；
- V4 当前步骤事件在该步首个动作之前；步骤推进后新事件排在推进事件之后；
- V5 同 run 重复模型调用不复制注入；步骤身份变化才发新；
- V6 无来源不发注入；
- V7 模型请求消息序列有序、role 合法、工具调用/结果配对完整；
- V8 两类事件进 timeline，类型可辨。

沙箱纪律同 conftest：零真实 works 触碰、不 import main。
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import current_turn, event_stream, message_runner, model_service, routes
from src.model_gateway import gateway as gateway_mod
from src.storage import event_log
from src.thinkchain import board as board_mod

FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main"},
    "system": {"default_model": "main"},
    "simple": {"enabled": False},
}


def _steps_def(*prompts: str) -> list[dict]:
    return [{"id": f"s{i + 1}", "prompt": p, "criteria": f"{p}的判据",
             "deliverable": f"{p}的产出", "type": "execute"}
            for i, p in enumerate(prompts)]


class RecordingGateway:
    """记录每圈 (system_prompt/user_prompt/prior/turn_tail) 的替身网关。"""

    def __init__(self, script: list[list[dict[str, Any]]] | None = None,
                 config: dict[str, Any] | None = None):
        self.config = config or FAKE_CONFIG
        self.script = script or []
        self.calls: list[dict[str, Any]] = []

    async def call(self, *a, **k):
        raise AssertionError("主路径应走 stream_call")

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None,
                          turn_tail=None, turn_once=None, **_):
        await asyncio.sleep(0)   # 每圈一次让出（与生产网络 IO 语义对齐）
        self.calls.append({
            "model": model_name,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prior": list(prior_messages or []),
            "turn_tail": list(turn_tail or []),
            "turn_once": list(turn_once or []),
        })
        idx = len(self.calls) - 1
        tool_calls = self.script[idx] if idx < len(self.script) else []
        content = "回合正文"
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
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _post(client, wid: str, cid: str, content: str) -> dict:
    r = await asyncio.wait_for(
        client.post(f"/api/works/{wid}/cards/{cid}/messages",
                    json={"content": content}), timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def _inj_msgs(msgs: list[dict]) -> list[dict]:
    """注入投影消息（抬头〔工作规范〕/〔当前流程〕——[当前轮注入改assistant自述]）。"""
    return [m for m in msgs
            if "〔工作规范〕" in str(m.get("content") or "")
            or "〔当前流程〕" in str(m.get("content") or "")]


# ============================================================
# V7：网关消息序列（真实网关构造，伪 litellm 捕获 messages）
# ============================================================

class _FakeMessage:
    content = "ok"
    tool_calls = None
    reasoning_content = None


class _FakeChoice:
    def __init__(self):
        self.message = _FakeMessage()
        self.finish_reason = "stop"


class _FakeResp:
    def __init__(self):
        self.choices = [_FakeChoice()]
        self.usage = types.SimpleNamespace(total_tokens=1)


async def test_gateway_message_order_default_and_turn_tail(monkeypatch):
    """turn_tail 默认 None ⇒ 逐字同旧；给了 ⇒ 追加在**用户消息之后**。"""
    captured: dict[str, Any] = {}

    class FakeLitellm:
        @staticmethod
        async def acompletion(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResp()

    monkeypatch.setitem(sys.modules, "litellm", FakeLitellm)
    monkeypatch.setenv("FAKE_KEY", "x")
    cfg = {"models": {"main": {"provider": "openai", "model": "m",
                               "api_key_env": "FAKE_KEY"}},
           "retry_grading": {"enabled": False}}
    gw = gateway_mod.ModelGateway(cfg)

    await gw.call("main", "SYS", "USER", prior_messages=[{"role": "assistant", "content": "H"}])
    assert [m["role"] for m in captured["messages"]] == ["system", "assistant", "user"]
    assert captured["messages"][-1]["content"] == "USER"

    await gw.call("main", "SYS", "USER", prior_messages=[],
                  turn_tail=[{"role": "user", "content": "INJ1"},
                             {"role": "user", "content": "INJ2"}])
    assert [m["content"] for m in captured["messages"]] == ["SYS", "USER", "INJ1", "INJ2"]


# ============================================================
# 工具卡注入样例（V3：runtime/剧本/prompt_ref/产物 可追溯）
# ============================================================

async def test_toolcard_injection_data_and_render(tmp_path, monkeypatch, work):
    from src.toolcard import artifact_store, driver, script_loader, step_prompt

    scripts_root = tmp_path / "toolcard_scripts"
    (scripts_root / "prompts").mkdir(parents=True)
    (scripts_root / "方案链.json").write_text(json.dumps({
        "name": "方案链",
        "steps": [{"id": "p1", "prompt_ref": "prompts/p1.txt"},
                  {"id": "p2", "prompt_ref": "prompts/p2.txt"}],
    }, ensure_ascii=False), encoding="utf-8")
    (scripts_root / "prompts" / "p2.txt").write_text("这是 p2 的提示词原文。", encoding="utf-8")
    monkeypatch.setattr(script_loader, "SCRIPTS_ROOT", scripts_root)

    wid, cid = work["work_id"], work["work_card_id"]
    driver._runtimes[cid] = driver.CardRuntime(
        card_id=cid, script_name="方案链", step_id="p2", round=2)
    # 登记一条「上一步产物」（代码自己落过的文件名）
    artifact_store._manifest[cid] = [
        {"序号": 1, "stepId": "p1", "文件": "01-p1-任务书读取-01.md", "写入成功": True}]

    try:
        data = step_prompt.build_injection_data(wid, cid)
        assert data["source_kind"] == "toolcard"
        assert data["source_ref"] == {"script_name": "方案链", "step_id": "p2"}
        assert data["step_id"] == "p2"
        assert data["content"]["prompt"] == "这是 p2 的提示词原文。"
        assert data["content"]["artifacts"] == ["01-p1-任务书读取-01.md"]
        text = step_prompt.render_injection(data)
        assert "我正在按「方案链」推进，当前在 p2。" in text
        assert "本步要求：这是 p2 的提示词原文。" in text
        assert "我已落盘的文件：01-p1-任务书读取-01.md" in text
        # N1：旧第三视角标签零残留（仅注入投影侧）
        assert "【当前剧本】" not in text and "【上一步产物】" not in text
        # [v2.1 §0.1] 分家：工具结果 instruction 仍是第三人称原样，≠ 注入投影
        instr = step_prompt.build_instruction(wid, cid)
        assert instr == ("【当前剧本】方案链\n【当前步】p2\n"
                         "【本步要求】这是 p2 的提示词原文。\n"
                         "【上一步产物】01-p1-任务书读取-01.md")
        assert instr != text
    finally:
        driver._runtimes.pop(cid, None)
        artifact_store._manifest.pop(cid, None)


# ============================================================
# V1/V4/V5/V8：续轮（活板已在）——注入紧随用户消息、续调不复制
# ============================================================

async def test_continuation_turn_injects_then_no_duplicate(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "续轮计划", _steps_def("第一步"))
    gw = RecordingGateway(script=[
        [{"name": "read_card", "arguments": {"card_id": cid}}],   # 圈1：调工具
        [{"name": "step_complete", "arguments": {                 # 圈2：推进收口
            "result": "一步成果", "criteria_met": "第一步判据满足"}}],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)

    async with _client() as client:
        await _post(client, wid, cid, "你好")
    await event_log.drain()

    # 圈1：注入随用户消息之后下发（turn_tail），user_prompt 仍是用户原话
    c1 = gw.calls[0]
    assert c1["user_prompt"] == "你好"
    assert c1["prior"] == []
    tail = c1["turn_tail"]
    assert len(tail) == 2
    assert "〔工作规范〕" in tail[0]["content"] and "我的执行纪律" in tail[0]["content"]
    assert "〔当前流程〕" in tail[1]["content"] and "本步要求：第一步" in tail[1]["content"]
    # V1：两条注入 role 均钉死 assistant（不靠常量，防常量被回改）
    assert all(m["role"] == "assistant" for m in tail)
    # N2：不补任何合成 user 消息（末条即 assistant，甲1）
    assert all(m["role"] != "user" for m in tail)

    # 圈2：注入已固化进轮槽（用户消息之后、工具轮之前），尾部不再重复
    c2 = gw.calls[1]
    assert c2["turn_tail"] == []
    roles = [m["role"] for m in c2["prior"]]
    # 甲3：接受相邻 assistant（注入×2 + 工具调用轮），不做归一
    assert roles == ["user", "assistant", "assistant", "assistant", "tool"]
    assert _inj_msgs(c2["prior"]) == tail                 # 内容一致、位置在 user 之后
    # 工具调用/结果配对完整（V7）
    assert c2["prior"][3]["tool_calls"][0]["id"] == c2["prior"][4]["tool_call_id"]

    # V1/V8：事件序与 timeline
    types = [r["type"] for r in event_log.timeline(wid, cid)]
    assert types[0] == "user_message"
    assert types.index("context_preamble") < types.index("context_injection")
    assert types.index("context_injection") < types.index("tool_call")
    assert types.count("context_preamble") == 1
    assert types.count("context_injection") == 1


# ============================================================
# 新拆解轮：板在 round1 工具结果之后才存在 → 注入落在其后的轮
# ============================================================

async def test_fresh_plan_turn_injects_after_plan_created(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    gw = RecordingGateway(script=[
        [{"name": "create_plan", "arguments": {
            "name": "新计划", "steps": [
                {"id": "s1", "prompt": "唯一一步", "criteria": "判据X",
                 "deliverable": "产出Y", "type": "execute"}]}}],   # 圈1：建板
        [{"name": "step_complete", "arguments": {
            "result": "做完了", "criteria_met": "判据X满足"}}],     # 圈2：推进收口
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)

    async with _client() as client:
        await _post(client, wid, cid, "拆解它")
    await event_log.drain()

    # 圈1：建板之前无来源 → 不发注入
    assert gw.calls[0]["turn_tail"] == []
    # 圈2：板已上架 → 注入已随轮槽进入（排在 create_plan 工具结果之后）
    c2 = gw.calls[1]
    inj = _inj_msgs(c2["prior"])
    assert len(inj) == 2
    assert "本步要求：唯一一步" in inj[1]["content"]
    assert c2["prior"].index(inj[0]) > next(
        i for i, m in enumerate(c2["prior"]) if m.get("role") == "tool")

    types = [r["type"] for r in event_log.timeline(wid, cid)]
    assert types.index("plan_created") < types.index("context_preamble")
    assert types.count("context_preamble") == 1


# ============================================================
# V6：无来源不发注入（直答回合）
# ============================================================

async def test_direct_answer_emits_no_injection(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    gw = RecordingGateway(script=[[]])
    monkeypatch.setattr(model_service, "_gateway", gw)

    async with _client() as client:
        await _post(client, wid, cid, "直接答我")
    await event_log.drain()

    assert gw.calls[0]["turn_tail"] == []
    types = [r["type"] for r in event_log.timeline(wid, cid)]
    assert "context_preamble" not in types
    assert "context_injection" not in types


# ============================================================
# V5：步骤推进 → 发新动态注入（幂等键随步骤身份变化）
# ============================================================

async def test_step_advance_emits_new_injection(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "两步计划", _steps_def("第一步", "第二步"))
    gw = RecordingGateway(script=[
        [{"name": "step_complete", "arguments": {                 # 圈1：推进到 s2
            "result": "一步成果", "criteria_met": "第一步判据满足"}}],
        [{"name": "step_complete", "arguments": {                 # 圈2：推进收口
            "result": "二步成果", "criteria_met": "第二步判据满足"}}],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)

    async with _client() as client:
        await _post(client, wid, cid, "做两步")
    await event_log.drain()

    recs = [r for r in event_log.timeline(wid, cid)
            if r["type"] == "context_injection"]
    assert len(recs) == 2, "步骤推进后发新动态注入"
    assert recs[0]["data"]["step_id"] == "s1"
    assert recs[1]["data"]["step_id"] == "s2"
    # 固定总领只发一次
    assert sum(1 for r in event_log.timeline(wid, cid)
               if r["type"] == "context_preamble") == 1
    # 新注入排在推进事件之后（V4）
    tl = event_log.timeline(wid, cid)
    seq_adv = next(r["seq"] for r in tl if r["type"] == "step_advanced")
    assert seq_adv < recs[1]["seq"], "新注入排在推进事件之后（V4）"
    # 幂等键随步骤身份变化
    k1 = current_turn.injection_key("context_injection", recs[0]["data"])
    k2 = current_turn.injection_key("context_injection", recs[1]["data"])
    assert k1 != k2


# ============================================================
# 负向判据 N1-N7 + 边界 V5（[当前轮注入改assistant自述 2026-10-14] v2 §3.3/§3.2）
# 最容易糊弄的地方：逐条断言「不许出现」。
# ============================================================

async def test_negative_no_legacy_mark_and_no_synthetic_user(work, monkeypatch):
    """N1/N2：注入 role 全 assistant、〔上下文注入 零残留；不补任何合成 user 消息。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "负向计划", _steps_def("第一步"))
    gw = RecordingGateway(script=[
        [{"name": "read_card", "arguments": {"card_id": cid}}],
        [{"name": "step_complete", "arguments": {
            "result": "一步成果", "criteria_met": "第一步判据满足"}}],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with _client() as client:
        await _post(client, wid, cid, "你好")
    await event_log.drain()

    # N1：全序列无 〔上下文注入 残留；注入消息 role 全 assistant
    for call in gw.calls:
        for m in list(call["prior"]) + list(call["turn_tail"]):
            content = str(m.get("content") or "")
            assert "〔上下文注入" not in content
            if "〔工作规范〕" in content or "〔当前流程〕" in content:
                assert m["role"] == "assistant"
    # N2：round1 的 turn_tail 里没有 user（未补合成收尾）
    assert all(m["role"] != "user" for m in gw.calls[0]["turn_tail"])
    # 圈2 的 prior 里恰一条 user（真实用户原话），无第二条合成 user
    assert sum(1 for m in gw.calls[1]["prior"] if m["role"] == "user") == 1


async def test_negative_injection_not_in_session(work, monkeypatch):
    """N3：注入不进会话文件（sessions/*.md 只收真实对话）。"""
    from src.storage import session_facade
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "会话计划", _steps_def("第一步"))
    gw = RecordingGateway(script=[
        [{"name": "step_complete", "arguments": {
            "result": "一步成果", "criteria_met": "第一步判据满足"}}],
        [],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with _client() as client:
        await _post(client, wid, cid, "你好")
    await event_log.drain()

    blocks = await session_facade.read(wid, cid)
    text = json.dumps(blocks, ensure_ascii=False)
    for token in ("〔工作规范〕", "〔当前流程〕", "我的执行纪律", "我已落盘的文件"):
        assert token not in text


def test_negative_step_brief_template_unchanged():
    """N4：board.STEP_BRIEF_TEMPLATE 文本本身未动（只动渲染）。"""
    assert "【步骤执行】你现在执行计划的一步，只做本步的事。" in board_mod.STEP_BRIEF_TEMPLATE
    assert "执行纪律：" in board_mod.STEP_BRIEF_TEMPLATE
    assert "（read_file/search_files/read_card）" in board_mod.STEP_BRIEF_TEMPLATE
    assert 'criteria_met 写"判据如何满足"' in board_mod.STEP_BRIEF_TEMPLATE


async def test_negative_event_layer_keys_and_idempotent_key(work):
    """N5：事件 data 键集与幂等键不变（事件层零改动）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "键集计划", _steps_def("第一步"))
    events = current_turn.build_injection_events(wid, cid)
    assert [e["type"] for e in events] == ["context_preamble", "context_injection"]
    pre = events[0]["data"]
    assert set(pre.keys()) == {"source_kind", "source_ref", "run_context", "content"}
    inj = events[1]["data"]
    assert set(inj.keys()) == {
        "source_kind", "source_ref", "plan_name", "step_id", "index", "total",
        "completed_steps", "current_step"}
    assert set(inj["current_step"].keys()) == {"prompt", "criteria", "deliverable", "type"}
    # 幂等键稳定：同 data 两次算同值
    assert (current_turn.injection_key("context_injection", inj)
            == current_turn.injection_key("context_injection", inj))


def test_negative_frontend_cases_untouched():
    """N6：前端 stream.js 两 case 未改（按事件 type 渲染，与 wire role 无关）。"""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static" / "stream.js").read_text(
        encoding="utf-8")
    assert 'case "context_preamble"' in js
    assert 'case "context_injection"' in js


def test_negative_preamble_items_parity():
    """N7：自述工作规范与模板的编号条目一一对应、无增无删（各 4 条）。"""
    import re
    from src.thinkchain import step_region

    def _n_items(text: str) -> int:
        return len(re.findall(r"(?m)^\s*\d+\.\s", text))

    assert _n_items(board_mod.step_brief_fixed()) == 4
    assert _n_items(step_region.PREAMBLE_SELF_NARRATIVE) == 4
    # 判据语义未改：仍谈「不谎报」与 step_complete
    assert "不谎报" in step_region.PREAMBLE_SELF_NARRATIVE
    assert "step_complete" in step_region.PREAMBLE_SELF_NARRATIVE


def test_boundary_empty_sections_skipped():
    """V5：无已完成步→「我已经完成：」整行跳过；artifacts 空→「我已落盘的文件：」整行跳过。"""
    from src.thinkchain import step_region
    from src.toolcard import step_prompt

    data = {"plan_name": "P", "index": 1, "total": 2, "completed_steps": [],
            "current_step": {"prompt": "甲", "criteria": "乙", "deliverable": "丙"}}
    text = step_region.render_injection(data)
    assert "我已经完成" not in text

    tdata = {"source_ref": {"script_name": "S", "step_id": "p1"}, "step_id": "p1",
             "content": {"prompt": "原文", "artifacts": []}}
    ttext = step_prompt.render_injection(tdata)
    assert "我已落盘的文件" not in ttext


# ============================================================
# v2.1 §0.1：工具结果（run_tool_card.instruction）第三人称原样 + 两路分家
# ============================================================

async def test_v11_run_tool_card_instruction_third_person(work, tmp_path, monkeypatch):
    """V11：run_tool_card 返回的 instruction 与改造前逐字一致（第三人称标签文本）。"""
    from src.api import card_tools
    from src.permissions import policy
    from src.toolcard import driver, script_loader

    root = tmp_path / "tc_scripts"
    (root / "prompts").mkdir(parents=True)
    (root / "方案链.json").write_text(json.dumps({
        "name": "方案链",
        "steps": [{"id": "p1", "prompt_ref": "prompts/p1.txt"}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "prompts" / "p1.txt").write_text("第一步提示词。", encoding="utf-8")
    monkeypatch.setattr(script_loader, "SCRIPTS_ROOT", root)

    wid, cid = work["work_id"], work["work_card_id"]
    cards = {cid: {"card_id": cid, "card_type": "sub_project",
                   "perms": policy.perms_for("rd_project", "sub_project")}}
    before = set(driver._runtimes)
    try:
        out = await card_tools._h_run_tool_card(
            wid, cid, {"script_name": "方案链"}, cards)
        instr = out["instruction"]
        # 与改造前逐字一致（第三人称；无第一人称字样）
        assert instr == ("【当前剧本】方案链\n【当前步】p1\n"
                         "【本步要求】第一步提示词。")
        assert "我正在按" not in instr
    finally:
        for k in set(driver._runtimes) - before:
            driver._runtimes.pop(k, None)


def test_n9_build_instruction_does_not_reuse_render_injection():
    """N9：build_instruction 不得复用 render_injection（两路分家，防再次连坐）。

    用 AST 只查**函数体内的调用**（docstring 里的字面量不算），避免误判。
    """
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "toolcard"
           / "step_prompt.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "build_instruction")
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "render_injection" not in called        # 不复用注入投影渲染
    assert "render_instruction" in called          # 走第三人称专用渲染


# ============================================================
# [判断协议改注入 2026-10-14] 判断协议：轮首注入 · 无板才发 · 后续轮清掉
# 判据 V1-V6/V7/V8/V11 + N1/N2/N3/N4
# ============================================================

def test_judgment_not_in_base_prompt():
    """V1：BASE_PROMPT 不再含判断协议（前 200 字＝使用说明正文）；协议原文仍在。"""
    from src.api import prompts
    assert "入口判断器" not in prompts.BASE_PROMPT
    assert prompts.BASE_PROMPT[:200].lstrip().startswith("你在一个卡片系统内工作。")
    assert "你是推演工作台的入口判断器。" in prompts.JUDGMENT_PROTOCOL


def test_judgment_self_narrative_only_person_changes():
    """V5/N4：自述版＝协议原文**只改三处人称**，其余逐字不变。"""
    from src.api import prompts
    sn = prompts.JUDGMENT_PROTOCOL_SELF_NARRATIVE
    assert sn == (prompts.JUDGMENT_PROTOCOL
                  .replace("你是推演工作台的入口判断器。", "我是推演工作台的入口判断器。")
                  .replace("，先判断该", "，我先判断该")
                  .replace("先问自己：", "我先问自己："))
    # 四种判据（①②③④）、直答/拆解条件、收尾句一字不改
    for token in ("①", "②", "③", "④", "能做完 → direct", "做不完 → plan",
                  "调 create_plan 拆解上板", "拿不准就拆"):
        assert token in sn, token


def test_judgment_event_ontology_and_frontend():
    """V6/V8：EVENT_TYPES 27 类且含 judgment_protocol；stream.js 有该 case。"""
    from pathlib import Path
    # [三席身份注入 2026-10-14] 本体论 26 → 27（+context_seat_identity）
    assert len(event_stream.EVENT_TYPES) == 27
    assert "judgment_protocol" in event_stream.EVENT_TYPES
    js = (Path(__file__).resolve().parents[1] / "static" / "stream.js").read_text(
        encoding="utf-8")
    assert 'case "judgment_protocol"' in js


def test_judgment_message_role_mark_and_keys():
    """V11/N3：投影消息 role=assistant、抬头〔判断协议〕、键集只 {role,content}。"""
    msg = current_turn.project_judgment_message()
    assert set(msg.keys()) == {"role", "content"}
    assert msg["role"] == "assistant"
    assert msg["content"].startswith("〔判断协议〕")
    assert "我是推演工作台的入口判断器。" in msg["content"]
    assert "我先判断该" in msg["content"]
    assert "我先问自己：" in msg["content"]


def test_viewpoint_system_prompt_has_no_protocol():
    """V7：三席 system（中性上下文）与主模型 system 都不含判断协议。"""
    from src.api import prompts
    ctx = "## 卡片清单\ncard_001 · 根（根）"
    vp = prompts.build_viewpoint_system_prompt(ctx)
    assert "入口判断器" not in vp and vp == ctx
    assert "入口判断器" not in prompts.build_system_prompt(ctx)


async def test_judgment_injected_first_round_no_board(work, monkeypatch):
    """V2/V3/V4/N1/N2：无活板时——轮首注入判断协议一次；round≥2 与轮槽都没有。"""
    wid, cid = work["work_id"], work["work_card_id"]
    gw = RecordingGateway(script=[
        [{"name": "create_plan", "arguments": {"name": "新计划", "steps": [
            {"id": "s1", "prompt": "唯一一步", "criteria": "判据X",
             "deliverable": "产出Y", "type": "execute"}]}}],
        [{"name": "step_complete", "arguments": {
            "result": "做完了", "criteria_met": "判据X满足"}}],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with _client() as client:
        await _post(client, wid, cid, "拆解它")
    await event_log.drain()

    # 圈1（轮首、无板）：判断协议随 turn_once 下发，role/人称正确
    c1 = gw.calls[0]
    assert len(c1["turn_once"]) == 1
    jm = c1["turn_once"][0]
    assert jm["role"] == "assistant" and "〔判断协议〕" in jm["content"]
    assert "我是推演工作台的入口判断器。" in jm["content"]
    assert "我先判断该" in jm["content"] and "我先问自己：" in jm["content"]
    # 轮首轮槽里没有它（判断协议不进 turn_tail、不进 prior）
    assert c1["turn_tail"] == []
    assert all("入口判断器" not in str(m.get("content") or "") for m in c1["prior"])
    # 圈2起：判断协议彻底不出现（turn_once 空、轮槽/注入都没有）
    for c in gw.calls[1:]:
        assert c["turn_once"] == []
        assert all("入口判断器" not in str(m.get("content") or "")
                   for m in list(c["prior"]) + list(c["turn_tail"]))
    # 事件：judgment_protocol 恰一条，且在首个 tool_call 之前
    tl = event_log.timeline(wid, cid)
    types = [r["type"] for r in tl]
    assert types.count("judgment_protocol") == 1
    assert types.index("judgment_protocol") < types.index("tool_call")


async def test_judgment_not_injected_when_board_live(work, monkeypatch):
    """有活板 → 轮首也不注入判断协议（用户 2026-10-14 12:08 口径）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "续轮计划", _steps_def("第一步"))
    gw = RecordingGateway(script=[
        [{"name": "read_card", "arguments": {"card_id": cid}}],
        [{"name": "step_complete", "arguments": {
            "result": "r", "criteria_met": "c"}}],
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)
    async with _client() as client:
        await _post(client, wid, cid, "你好")
    await event_log.drain()
    for c in gw.calls:
        assert c["turn_once"] == []
        assert all("入口判断器" not in str(m.get("content") or "")
                   for m in list(c["prior"]) + list(c["turn_tail"]))
    assert "judgment_protocol" not in [r["type"] for r in event_log.timeline(wid, cid)]
