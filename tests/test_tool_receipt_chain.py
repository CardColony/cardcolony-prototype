# -*- coding: utf-8 -*-
"""工具调用来源与真实回执 · 专项验收（2026-11-19）。

任务书：docs/工具调用与真实回执-任务书-20261119.md 的确定性验收表选点。
判据（每条对准表内一行）：
- 来源四分类：native / declared_text / pseudo_compat / 未派发，事件与台账可分；
- 原生协议失败：端点声称调了工具但网关没拿到有效调用 ⇒ 与「模型没调」分开
  处置——native_protocol_error 事件＋回注重发请求，坏项零副作用；
- 存根三态：新块按 status，旧块 [tool_error ⇒ error、空 ⇒ unknown
  （确定缺陷钉死：标准错误不再被历史投影成 ok）；
- 短提示：随实际端点协议生成、随首轮请求下发、不进轮槽（消息序列不变）；
- 按回执交付：绑定动作回合收口用执行记录核对，虚报成功的交付带系统回执行。
"""
from __future__ import annotations

import json

import pytest

from src.api import message_runner, routes, tool_receipt
from src.api.context_regions import _tool_stub_wire  # noqa: SLF001 投影内部件
from src.model_gateway.gateway import ModelGateway
from src.model_gateway.llm.assembler import BlockAssembler
from src.model_gateway.llm.types import (
    BlockEnd, BlockStart, FinishChunk, StopReason, TextDelta, ToolCallBlock,
    ToolCallDelta,
)
from src.storage import session_facade


# ---------------------------------------------------------------------------
# ① 网关交界：协议元数据与缺名分片计数
# ---------------------------------------------------------------------------

def _gateway_with(vendor_provider: str) -> ModelGateway:
    cfg = {
        "models": {
            "m1": {"model": "wire-m1", "provider": vendor_provider,
                   "vendor": vendor_provider, "base_url": "http://x",
                   "api_key_env": "", "retry": {"max_attempts": 1,
                                                "backoff_seconds": 0}},
        },
    }
    return ModelGateway(cfg)


def _tool_call_chunks(idx: int, cid: str, name: str, args: str) -> list:
    """按真实流序构造一个 tool-call 块（delta 装配进 assembler）。"""
    return [
        BlockStart(index=idx, block_type="tool-call"),
        ToolCallDelta(index=idx, id=cid, name=name, arguments_delta=args),
        BlockEnd(index=idx, block=ToolCallBlock(id=cid, name=name, arguments=args)),
    ]


def test_gateway_counts_dropped_nameless_shards(monkeypatch):
    """多工具轮缺名分片不再静默消失：native_dropped 计数留证。

    单工具轮缺名分片会被既有「单工具补名」逻辑归属（2026-10-30 修复，保留）；
    多工具轮无法机械归属 ⇒ 原实现直接丢弃（`if not name: continue`），改前
    循环侧只看到 tool_calls=[]、无法区分「模型没调」和「分片损坏」。
    """
    monkeypatch.delenv("MOCK_LLM", raising=False)
    gw = _gateway_with("deepseek")
    asm = BlockAssembler()
    asm.push(TextDelta(index=0, text="正文"))
    for c in _tool_call_chunks(1, "c1", "", '{"card_id":"card_009"}'):
        asm.push(c)   # 缺名分片（多工具轮无法归属）
    for c in _tool_call_chunks(2, "c2", "read_card", '{"card_id":"card_009"}'):
        asm.push(c)   # 有效调用
    asm.push(FinishChunk(reason=StopReason()))
    res = gw._result_from(
        asm, "m1", 0.0,
        tools=[{"name": "read_card"}, {"name": "search_files"}])
    assert len(res["tool_calls"]) == 1          # 有效调用照常进列表
    assert res["tool_calls"][0]["name"] == "read_card"
    pm = res["protocol_meta"]
    assert pm["native_dropped"] == 1            # 缺名分片单独计数
    assert pm["native_calls"] == 1
    assert pm["endpoint_protocol"] == "native"
    assert pm["native_finish_tool_calls"] is False   # finish=stop，非协议失败


def test_gateway_marks_declared_text_endpoint(monkeypatch):
    """wb-cli 端点 ⇒ declared_text（宿主声明协议），与原生端点在来源面可分。"""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    gw = _gateway_with("wb-cli")
    asm = BlockAssembler()
    asm.push(TextDelta(index=0, text="纯文本"))
    asm.push(FinishChunk(reason=StopReason()))
    res = gw._result_from(asm, "m1", 0.0)
    assert res["protocol_meta"]["endpoint_protocol"] == "declared_text"
    assert res["protocol_meta"]["declared_text"] is True


def test_gateway_truncated_call_counted(monkeypatch):
    """参数截断（坏 JSON）⇒ native_truncated=1，坏项不进可执行列表。"""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    gw = _gateway_with("deepseek")
    asm = BlockAssembler()
    for c in _tool_call_chunks(1, "c1", "write_file", '{"path":"a'):
        asm.push(c)
    asm.push(FinishChunk(reason=StopReason()))
    res = gw._result_from(asm, "m1", 0.0)
    assert res["tool_calls"] == []
    assert res["truncated_tool_call"]["name"] == "write_file"
    assert res["protocol_meta"]["native_truncated"] == 1


# ---------------------------------------------------------------------------
# ② 短提示
# ---------------------------------------------------------------------------

def _tool(n):
    return {"type": "function", "function": {"name": n, "parameters": {}}}


def test_turn_hint_follows_endpoint_protocol(monkeypatch):
    """提示随实际端点协议生成：原生讲 tool_calls 字段，声明端点讲正文块。"""
    monkeypatch.setenv("TOOL_TURN_HINT", "1")
    h_native = tool_receipt.build_turn_hint("native", [_tool("read_card")])
    h_text = tool_receipt.build_turn_hint("declared_text", [_tool("read_card")])
    assert "tool_calls" in h_native["content"]
    assert "不会被执行" in h_native["content"]
    assert "antml" in h_text["content"]
    # 无工具不出（纯对话轮）
    assert tool_receipt.build_turn_hint("native", []) is None


def test_turn_hint_switch_off(monkeypatch):
    monkeypatch.setenv("TOOL_TURN_HINT", "0")
    assert tool_receipt.build_turn_hint("native", [_tool("read_card")]) is None


# ---------------------------------------------------------------------------
# ③ 存根三态（确定缺陷钉死）
# ---------------------------------------------------------------------------

def test_stub_new_block_uses_structured_status():
    """新块带 status ⇒ 照结构化状态渲染（执行时事实，最可信）。"""
    names = {"t1": "search_files"}
    ok_blk = {"actor": "tool", "tool_call_id": "t1", "content": '{"hits": []}',
              "status": "ok"}
    err_blk = {"actor": "tool", "tool_call_id": "t1", "content": '{"hits": []}',
               "status": "error"}
    assert "· ok" in _tool_stub_wire(ok_blk, names)["content"]
    assert "· error" in _tool_stub_wire(err_blk, names)["content"]


def test_stub_legacy_tool_error_is_error_not_ok():
    """确定缺陷钉死：旧块 [tool_error code=not_found] 投影为 error。

    改前只认 `"ok": false` 子串——render_tool_result 的错误渲染是
    `[tool_error code=…] message`，不含该子串 ⇒ 标准错误被历史投影成 ok。
    """
    names = {"t1": "read_card"}
    blk = {"actor": "tool", "tool_call_id": "t1",
           "content": "[tool_error code=not_found] target missing"}
    wire = _tool_stub_wire(blk, names)
    assert "· error" in wire["content"]
    assert "· ok" not in wire["content"]


def test_stub_legacy_empty_is_unknown():
    """旧块空结果/残缺 ⇒ unknown（无法核实就如实显示未知）。"""
    names = {"t1": "read_card"}
    blk = {"actor": "tool", "tool_call_id": "t1", "content": ""}
    assert "· unknown" in _tool_stub_wire(blk, names)["content"]


def test_stub_legacy_ok_payload_is_ok():
    """旧块成功渲染产物（json.dumps(data)）⇒ ok（系统渲染管道，形态可判定）。"""
    names = {"t1": "read_card"}
    blk = {"actor": "tool", "tool_call_id": "t1", "content": '{"card_id": "c"}'}
    assert "· ok" in _tool_stub_wire(blk, names)["content"]


# ---------------------------------------------------------------------------
# ④ 循环：strict 拒伪 / declared 执行 / 协议失败回注 / 回执闸
# ---------------------------------------------------------------------------

def _tc(name, args):
    return {"name": name, "arguments": args}


def _asst_antml(name, args):
    """正文里的 antml 调用块（声明文本协议的合法形态）。"""
    params = "".join(
        f'<antml:parameter name="{k}">{v}</antml:parameter>'
        for k, v in args.items())
    return (f'<antml:function_calls><antml:invoke name="{name}">{params}'
            "</antml:invoke></antml:function_calls>")


async def test_strict_mode_refuses_pseudo_calls(work, sandbox, monkeypatch):
    """验收模式：原生端点正文伪调用不代执行——事件留痕、零副作用、回注协议。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("TOOL_CALL_STRICT", "1")
    wid = work["work_id"]
    n = {"c": 0}
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": f"我先直接宣布结果。{_asst_antml('read_card', {'card_id': 'card_001'})}",
                     "tool_calls": [],
                     "protocol_meta": {"endpoint_protocol": "native",
                                       "declared_text": False,
                                       "native_calls": 0, "native_dropped": 0,
                                       "native_truncated": 0,
                                       "native_finish_tool_calls": False,
                                       "finish_reason": "stop"}},
                    "fake")
        return ({"content": "收到，重发完成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "按方案发指令给它")
    assert "error" not in result
    # 事件面：pseudo_call_refused 有、rescue 无（未代执行）
    kinds = [t for t, _ in events]
    assert "pseudo_call_refused" in kinds
    assert "rescue" not in kinds
    # 工具事件为零调用（无副作用）
    assert result["tool_events"] == []
    # 纠正轮请求带协议回注
    assert n["c"] == 2


async def test_declared_text_calls_execute_via_registry(work, sandbox, monkeypatch):
    """声明文本协议端点：正文 antml 调用走同一注册表真实执行（rescue 来源单列）。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_CALL_STRICT", raising=False)
    # 端点协议判定按 vendor 登记——测试模拟 wb-cli 声明端点，直接钉住判定函数
    monkeypatch.setattr(tool_receipt, "endpoint_protocol_of",
                        lambda model_name: "declared_text")
    wid = work["work_id"]
    n = {"c": 0}
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": f"查询开始。{_asst_antml('read_card', {'card_id': 'card_001'})}",
                     "tool_calls": [],
                     "protocol_meta": {"endpoint_protocol": "declared_text",
                                       "declared_text": True,
                                       "native_calls": 0, "native_dropped": 0,
                                       "native_truncated": 0,
                                       "native_finish_tool_calls": False,
                                       "finish_reason": "stop"}},
                    "fake")
        return ({"content": f"卡片内容：{prior_messages[-1]['content'][:40]}",
                 "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "看看卡上写了什么")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert "rescue" in kinds
    # rescue 事件带 origin=declared_text
    rescue_ev = next(d for t, d in events if t == "rescue")
    assert rescue_ev.get("origin") == "declared_text"
    # 真实执行：tool_events 有 read_card 且 ok
    te = [e for e in result["tool_events"] if e.get("tool") == "read_card"]
    assert te and te[0]["ok"] is True
    assert te[0].get("origin") == "declared_text"
    # 执行记录落台账
    ledger = (sandbox.logs / "tool_dispatch_ledger.jsonl").read_text(encoding="utf-8")
    assert '"tool": "read_card"' in ledger and '"origin": "declared_text"' in ledger


async def test_native_protocol_error_feedback(work, sandbox, monkeypatch):
    """端点声称调了工具（finish=tool_calls）但零有效调用 ⇒ native_protocol_error
    事件＋回注重发请求，与「模型没调」（finish=stop）分开处置。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_CALL_STRICT", raising=False)
    wid = work["work_id"]
    n = {"c": 0}
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "",
                     "tool_calls": [],
                     "protocol_meta": {"endpoint_protocol": "native",
                                       "declared_text": False,
                                       "native_calls": 0, "native_dropped": 1,
                                       "native_truncated": 0,
                                       "native_finish_tool_calls": True,
                                       "finish_reason": "tool_calls"}},
                    "fake")
        return ({"content": "重发成功。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "去查一下")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert "native_protocol_error" in kinds
    ev = next(d for t, d in events if t == "native_protocol_error")
    assert ev["native_dropped"] == 1 and ev["finish_reason"] == "tool_calls"
    # 未派发项落台账
    ledger = (sandbox.logs / "tool_dispatch_ledger.jsonl").read_text(encoding="utf-8")
    assert '"dispatched": false' in ledger
    assert '"error_code": "native_protocol"' in ledger
    assert n["c"] == 2   # 回注后纠正确实发生


async def test_receipt_gate_directive_zero_call(work, sandbox, monkeypatch):
    """指令唤醒回合零调用收口 ⇒ action_not_executed 事件＋系统回执行
    （虚报成功的交付被系统回执对账——card_007 失败轮形态的机制覆盖）。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_CALL_STRICT", raising=False)
    monkeypatch.delenv("TOOL_TURN_HINT", raising=False)
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        return ({"content": "已发（msg_id 4692，delivered: true），card_001 已唤醒。",
                 "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "按这个方案，发指令给它", source_kind="directive",
        source={"kind": "card_msg", "from": "card_888",
                "require_report": True})
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert "action_not_executed" in kinds
    # 交付文本带系统回执行：指令回合核对项＝「是否已向发令卡汇报」，
    # 模型自述「已发」与执行事实可区分；业务完成与否按各项回执。
    assert "〔系统回执〕" in result["reply"]
    assert "尚未向发令卡汇报" in result["reply"]
    # 指令回合系统结果行可见（零执行事实）
    assert "〔系统结果〕本轮未执行工具" in result["reply"]


async def test_receipt_gate_satisfied_no_line(work, sandbox, monkeypatch):
    """指令回合有匹配的成功回执（direct ok）⇒ 不附加系统回执行。

    direct 走真实 handler（通信线+验收判据门槛一条不减）：建子卡
    （父子默认通信线在建卡期补齐），根卡沿线下指令。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    from src.storage import card_store, work_store
    child = (await card_store.create_card(
        wid, "收令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": child,
                                       "instruction": "补错误处理",
                                       "acceptance": "单测全绿"})]}, "fake")
        return ({"content": "指令已通过执行通道发出。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "发指令",
                                       source_kind="directive")
    assert "error" not in result
    te = [e for e in result["tool_events"] if e.get("tool") == "direct"]
    assert te and te[0]["ok"] is True, result["tool_events"]
    assert te[0].get("origin") == "native"
    assert "〔系统回执〕" not in result["reply"]


async def test_receipt_gate_off_keeps_baseline(work, sandbox, monkeypatch):
    """TOOL_RECEIPT_GATE=0（四组对照 A/B 基线）⇒ 不核对、不附加回执行。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("TOOL_RECEIPT_GATE", "0")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        return ({"content": "已发（msg_id 4692）。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "发指令",
                                       source_kind="directive")
    assert "error" not in result
    assert "〔系统回执〕" not in result["reply"]


def test_bound_action_minimal_trust_set():
    """绑定动作＝最小可信集：专用 op 回合＋指令唤醒回合；普通话术不判。"""
    s = message_runner.RunState(work_id="w", card_id="c", content="x")
    assert tool_receipt.bound_action(s) is None          # 普通对话不判
    s.op = "update_status"
    assert tool_receipt.bound_action(s)["kind"] == "op"
    s2 = message_runner.RunState(work_id="w", card_id="c", content="x")
    s2.source_kind = "directive"
    s2.source = {"kind": "card_msg", "from": "card_888",
                 "require_report": True}
    assert tool_receipt.bound_action(s2)["kind"] == "directive"
    # 无汇报标志/无发令卡 ⇒ 不绑定（普通指令只展示执行事实）
    s2b = message_runner.RunState(work_id="w", card_id="c", content="x")
    s2b.source_kind = "directive"
    assert tool_receipt.bound_action(s2b) is None
    s2c = message_runner.RunState(work_id="w", card_id="c", content="x")
    s2c.source_kind = "directive"
    s2c.source = {"kind": "card_msg", "require_report": True}
    assert tool_receipt.bound_action(s2c) is None
    s3 = message_runner.RunState(work_id="w", card_id="c", content="去查一下")
    assert tool_receipt.bound_action(s3) is None         # 弱话术不误绑定


def test_receipt_verdict_ignores_unrelated_success():
    """一次成功的无关读取无法证明文件已写入——回执核对只认要求工具。"""
    s = message_runner.RunState(work_id="w", card_id="c", content="x")
    s.source_kind = "directive"
    s.source = {"kind": "card_msg", "from": "card_888",
                "require_report": True}
    s.tool_events = [{"tool": "read_card", "ok": True, "origin": "native"}]
    v = tool_receipt.receipt_verdict(s)
    assert v["satisfied"] is False   # read_card ≠ 卡间通信类工具
    s.tool_events.append({"tool": "direct", "ok": True, "origin": "native",
                          "send": {"recipients": [
                              {"to": "card_888", "msg_id": "cmsg-9",
                               "state": "delivered"}]}})
    v = tool_receipt.receipt_verdict(s)
    assert v["satisfied"] is True


# ---------------------------------------------------------------------------
# ⑤ 会话块结构化状态进存根（端到端一处）
# ---------------------------------------------------------------------------

async def test_tool_block_carries_status_to_session(work, sandbox, monkeypatch):
    """执行后的会话 tool 块带 status 结构化状态——存根三态的数据源在写侧生成。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("read_card", {"card_id": "card_001"})]}, "fake")
        return ({"content": "读取完成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "读卡")
    assert "error" not in result
    blocks = await session_facade.read(wid, "card_001")
    tool_blks = [b for b in blocks if str(b.get("actor") or "") == "tool"]
    assert tool_blks, "工具结果块应落会话"
    assert tool_blks[0].get("status") == "ok"
    # 再跑一次注定失败的目标（不存在卡）⇒ status=error
    n["c"] = 0

    async def fake_call_main2(system_prompt, user_prompt, prior_messages=None,
                              tools=None, model_name=None, turn_tail=None,
                              turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("read_card", {"card_id": "card_not_exists"})]}, "fake")
        return ({"content": "读取失败已知晓。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main2)
    await routes._run_message(wid, "card_001", "读不存在的卡")
    blocks = await session_facade.read(wid, "card_001")
    tool_blks = [b for b in blocks if str(b.get("actor") or "") == "tool"]
    assert tool_blks[-1].get("status") == "error"
    # 存根投影：该错误块在新投影下显示 error（不再 ok）
    names = {tool_blks[-1].get("tool_call_id"): "read_card"}
    assert "· error" in _tool_stub_wire(tool_blks[-1], names)["content"]
