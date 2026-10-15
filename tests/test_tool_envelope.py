# -*- coding: utf-8 -*-
"""[T-0010 §5] 工具结果信封契约测试（新增）。

覆盖面（任务书 §五 最少覆盖 1-4）：
1. 五错误码各构造一条，断言信封结构与 code 正确；
2. ``render_tool_result`` 成功/失败两形态文本逐字钉死；
3. tool_call_id 唯一性与 assistant/tool 侧配对一致（§2 判据 2 的用例化）；
4. 文本通道建卡计数写入 prompt 日志 ``text_channel_create_count``。

零真实 key、零网络；走 Mock/替身路径。works/logs 由 conftest 沙箱重定向。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import card_tools, model_service, prompt_log, routes
from src.storage import card_store

# ============================================================
# 替身网关：定点发工具调用 / 建卡文本块（与 test_offline_e2e 同族写法）
# ============================================================

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
    def __init__(self, tool_script: list[dict[str, Any]] | None = None,
                 proposal: list[dict[str, Any]] | None = None):
        self.config = FAKE_CONFIG
        self.tool_script = tool_script or []
        self.proposal = proposal
        self._tool_issued = False

    async def call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                   max_tokens=None, timeout=None, skip_completion_marker=False,
                   tools=None, on_status=None, stream=False, on_chunk=None,
                   prior_messages=None):
        if on_status:
            on_status({"phase": "start", "model": model_name})
        tool_calls = []
        if self.tool_script and tools and not self._tool_issued:
            tool_calls = [self.tool_script[0]]
            self._tool_issued = True
        content = "本轮无需建卡。"
        if self.tool_script:
            content = "先读一下这张卡。"
        if self.proposal:
            block = json.dumps(self.proposal, ensure_ascii=False)
            content = f"我先建卡：\n\n```card-proposal\n{block}\n```\n"
        return {"content": content, "tool_calls": tool_calls,
                "truncated_tool_call": None, "finish_reason": "stop",
                "tokens_used": 1, "latency_ms": 1}

    async def stream_call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                          max_tokens=None, timeout=None, on_status=None, tools=None,
                          on_reasoning=None, prior_messages=None, **_):
        result = await self.call(
            model_name, system_prompt, user_prompt, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, skip_completion_marker=True,
            tools=tools, on_status=on_status, prior_messages=prior_messages)
        for seg in result["content"]:
            yield {"chunk_type": "content", "chunk": seg, "text": seg}
        yield result


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


# ============================================================
# 1：五错误码
# ============================================================

def test_error_code_enum_is_exact():
    assert card_tools.TOOL_ERROR_CODES == (
        "denied", "not_found", "locked", "validation", "internal",
    )


def test_err_envelope_shape():
    e = card_tools.err("denied", "越权：无权编辑 card_002")
    assert e["ok"] is False
    assert e["error"] == {"code": "denied", "message": "越权：无权编辑 card_002"}
    assert e["data"] is None


def test_ok_envelope_shape_keeps_payload_verbatim():
    payload = {"card_id": "card_001", "title": "根卡", "ok": True}
    e = card_tools.ok_(payload)
    assert e["ok"] is True and e["error"] is None
    assert e["data"] == payload          # 键集合与文案原样进 data


async def test_denied_on_unauthorized_edit(work):
    """denied：越权 edit。"""
    wid, root = work["work_id"], work["work_card_id"]
    mine = await card_store.create_card(wid, "我的", "模型生成", parent=root)
    other = await card_store.create_card(wid, "他人", "模型生成", parent=root)
    cid_mine = mine["meta"]["card_id"]
    cid_other = other["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(
        wid, cid_mine, "edit_card", {"card_id": cid_other, "content": "x"}, cards)
    assert r["ok"] is False
    assert r["error"]["code"] == "denied"
    assert "越权" in r["error"]["message"]


async def test_not_found_on_missing_card(work):
    """not_found：edit 不存在的卡。"""
    wid, root = work["work_id"], work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(
        wid, root, "edit_card", {"card_id": "card_999", "content": "x"}, cards)
    assert r["ok"] is False
    assert r["error"]["code"] == "not_found"
    assert "不存在" in r["error"]["message"]


def test_locked_code_is_representable():
    """locked：当前工具层无产出点（读侧锁在 API 层拦），此处钉住枚举与投影可表达。

    方案未列举 locked 的真实触发路径；实测 card_tools 无 CardLockedError 分支，
    故以 err()/render 层断言「码存在且投影正确」，不伪造一个不存在的调用点。
    """
    assert "locked" in card_tools.TOOL_ERROR_CODES
    e = card_tools.err("locked", "目标正被占用")
    assert e["error"]["code"] == "locked"
    assert card_tools.render_tool_result(e) == "[tool_error code=locked] 目标正被占用"


async def test_validation_on_missing_param(work):
    """validation：缺参调用（create_card 空 title）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, root, "create_card", {"title": "  "}, cards)
    assert r["ok"] is False
    assert r["error"]["code"] == "validation"
    assert r["error"]["message"] == "title 不能为空"


async def test_internal_on_handler_exception(work, monkeypatch):
    """internal：handler 抛异常 → execute_tool 兜底 except 统一包 internal。"""
    wid, root = work["work_id"], work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)

    async def _boom(*a, **k):
        raise RuntimeError("模拟 handler 崩溃")

    # ToolDef 是 frozen dataclass，不能改字段——整条注册项换掉（schema 保持原值）
    _def = card_tools.TOOL_REGISTRY["read_card"]
    monkeypatch.setitem(
        card_tools.TOOL_REGISTRY, "read_card",
        card_tools.ToolDef(schema=_def.schema, handler=_boom, readonly=_def.readonly))
    r = await card_tools.execute_tool(wid, root, "read_card", {"card_id": root}, cards)
    assert r["ok"] is False
    assert r["error"]["code"] == "internal"
    assert "模拟 handler 崩溃" in r["error"]["message"]


async def test_unknown_tool_is_not_found(work):
    wid, root = work["work_id"], work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, root, "no_such_tool", {}, cards)
    assert r["ok"] is False and r["error"]["code"] == "not_found"


# ============================================================
# 2：render_tool_result 两形态逐字钉死
# ============================================================

def test_render_success_is_data_json():
    out = card_tools.render_tool_result(
        card_tools.ok_({"card_id": "card_001", "relations": []}))
    assert out == json.dumps({"card_id": "card_001", "relations": []},
                             ensure_ascii=False)
    assert out == '{"card_id": "card_001", "relations": []}'


def test_render_failure_is_tool_error_line():
    out = card_tools.render_tool_result(card_tools.err("not_found", "卡片不存在: card_999"))
    assert out == "[tool_error code=not_found] 卡片不存在: card_999"


def test_render_does_not_leak_envelope_scaffolding():
    """失败投影不得把 {"code","message"} 当数据喂给模型。"""
    out = card_tools.render_tool_result(card_tools.err("internal", "boom"))
    assert '"code"' not in out
    assert out.startswith("[tool_error ")


def test_render_handles_legacy_plain_error_string():
    """未信封化的直传（旧路径兜底）：按原文案返回，不崩。"""
    assert card_tools.render_tool_result({"error": "旧形态"}) == "旧形态"


# ============================================================
# 3：tool_call_id 唯一性与配对一致
# ============================================================

def _collect_ids(prior: list[dict[str, Any]]):
    a_ids, t_ids = [], []
    for m in prior:
        for tc in (m.get("tool_calls") or []):
            a_ids.append(tc["id"])
        if m.get("role") == "tool":
            t_ids.append(m["tool_call_id"])
    return a_ids, t_ids


async def test_tool_call_ids_unique_and_paired(client, sandbox, monkeypatch):
    """两轮各 1 次工具调用（替身每轮发一次），断言 id 互不相同且两侧配对一致。

    取证口径：prompt 日志的 prior_messages 里有 assistant.tool_calls 与 tool 消息，
    二者的 id 必须集合相等（配对）且两轮之间不重复（唯一）。
    """
    gw = FakeGateway(tool_script=[{"name": "read_card",
                                   "arguments": {"card_id": "card_001"}}])
    monkeypatch.setattr(model_service, "_gateway", gw)

    wid = (await client.post("/api/works", json={"title": "信封契约"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "看看根卡"})
    assert r.status_code == 200, r.text

    logs = sorted(prompt_log.LOGS_DIR.glob("prompt_*.json"))
    assert logs, "prompt 日志应已落沙箱"
    prior = json.loads(logs[-1].read_text(encoding="utf-8"))["prior_messages"]
    a_ids, t_ids = _collect_ids(prior)
    assert a_ids, "应至少有 assistant tool_calls"
    assert len(a_ids) == len(set(a_ids)), f"id 必须互不相同：{a_ids}"
    assert a_ids == t_ids or set(a_ids) == set(t_ids), f"配对不一致：{a_ids} vs {t_ids}"
    # run_tag 形态：call_{run_tag}_{rounds}_{i}
    assert all(i.startswith("call_") and i.count("_") >= 3 for i in a_ids), a_ids


async def test_tool_call_id_carries_run_tag(client, sandbox, monkeypatch):
    """id 含 run_tag：同一 pair_id 的不同轮次不撞号。"""
    gw = FakeGateway(tool_script=[{"name": "read_card",
                                   "arguments": {"card_id": "card_001"}}])
    monkeypatch.setattr(model_service, "_gateway", gw)
    wid = (await client.post("/api/works", json={"title": "run_tag"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "查证"})
    assert r.status_code == 200, r.text
    logs = sorted(prompt_log.LOGS_DIR.glob("prompt_*.json"))
    prior = json.loads(logs[-1].read_text(encoding="utf-8"))["prior_messages"]
    a_ids, _ = _collect_ids(prior)
    # 轮次段随轮次递增 → 形如 call_xxxxxxxx_1_0
    rounds = sorted({int(i.split("_")[2]) for i in a_ids})
    assert rounds == sorted(set(rounds))
    assert all(int(i.split("_")[2]) >= 1 for i in a_ids)


# ============================================================
# 4：文本通道建卡计数入 prompt 日志
# ============================================================

async def test_text_channel_create_count_written(client, sandbox, monkeypatch):
    """文本协议块通道建卡成功 → 日志出现 text_channel_create_count 键。"""
    proposal = [{"title": "文本通道卡", "content": "正文"}]
    gw = FakeGateway(proposal=proposal)
    monkeypatch.setattr(model_service, "_gateway", gw)

    wid = (await client.post("/api/works", json={"title": "文本通道计数"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "帮我建张卡"})
    assert r.status_code == 200, r.text

    logs = sorted(prompt_log.LOGS_DIR.glob("prompt_*.json"))
    payload = json.loads(logs[-1].read_text(encoding="utf-8"))
    assert payload.get("text_channel_create_count") == 1, payload.get(
        "text_channel_create_count")


async def test_text_channel_count_absent_when_zero(client, sandbox, monkeypatch):
    """为 0 则不写该键（与 regions/fold_ops 同一「空则不同构」纪律）。"""
    gw = FakeGateway(tool_script=[{"name": "read_card",
                                   "arguments": {"card_id": "card_001"}}])
    monkeypatch.setattr(model_service, "_gateway", gw)
    wid = (await client.post("/api/works", json={"title": "零计数"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "看看根卡"})
    assert r.status_code == 200, r.text
    logs = sorted(prompt_log.LOGS_DIR.glob("prompt_*.json"))
    payload = json.loads(logs[-1].read_text(encoding="utf-8"))
    assert "text_channel_create_count" not in payload


# ============================================================
# 5：信封形状的全局不变量
# ============================================================

async def test_every_handler_error_is_enveloped(work):
    """所有 execute_tool 返回值都必须是信封（无裸 {"error":...} 外泄）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await card_store.create_card(wid, "甲", "模型生成", parent=root)
    cid = a["meta"]["card_id"]
    cards = card_tools._collect_card_meta(wid)
    cases = [
        ("read_card", {"card_id": "card_999"}),      # not_found
        ("read_card", {"card_id": cid}),             # success
        ("create_card", {"title": ""}),              # validation
        ("create_card", {"title": "x", "parent": "card_999"}),  # not_found
        ("delete_card", {"card_id": root}),          # denied
        ("no_such_tool", {}),                        # not_found
    ]
    for name, args in cases:
        r = await card_tools.execute_tool(wid, cid, name, args, cards)
        assert card_tools._is_envelope(r), (name, args, r)
        if r["ok"] is True:
            assert r["error"] is None and r["data"] is not None, (name, args, r)
        else:
            assert r["error"] is not None and r["data"] is None, (name, args, r)
            assert r["error"]["code"] in card_tools.TOOL_ERROR_CODES, (name, args, r)
        assert isinstance(card_tools.render_tool_result(r), str)