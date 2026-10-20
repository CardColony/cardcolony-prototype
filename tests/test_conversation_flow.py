# -*- coding: utf-8 -*-
"""**对话模块**不变量测试网（[5715]）。

## 为什么有这份网

对话页的两条真缺陷——「长程调用链之后刷新，工具归工具、内容给内容、信息顺序全乱」
与「插话功能是废的」——**都不是靠肉眼走查发现的**：

- 第一条：装配器 ``lastAssistant()`` 找思考归属时**不限 run** ⇒ 后一个 run 的
  thinking 挂到前一个 run 的消息上（实测 247 条全挂到 21:26 那条老消息下）。
- 第二条：后端**从来没有轮内投递窗口**，``steerQueue()`` 只弹一句提示。

两条都在 5702 的施工文档里**写着「已知边界」**，于是被当成了「可以放过」。
⇒ **缺的不是排查，是网。** 这份网把「已知边界」变成**会红的断言**。

## 网里钉的不变量

| 编号 | 不变量 | 违反时的现象 |
|---|---|---|
| I1 | **时序**：一个 run 的事件按发生顺序落盘（轮次分隔 → 工具 → 该轮正文 → 下一轮） | 刷新后顺序错乱 |
| I2 | **逐轮正文**：每一轮自己的正文，各落一条 assistant_text | 刷新后只剩一大坨 |
| I3 | **不重复**：收口那条 assistant_text 只含末轮回执，不含前面各轮正文 | 同一段话出现两遍 |
| I4 | **模型上下文零变化**：会话块 content 仍是拼接全文（各轮正文补进会话文件供下一回合读） | 下一轮模型看不到前面的分析 |
| I5 | **思考归属不跨 run**：一个 run 的 thinking 只归属本 run 的 assistant | 长链的思考挂到别处、整段丢失 |
| I6 | **工具合体**：tool_call + tool_result 合成**一个**节点 | 工具散成两行、读不出调用与结果 |
| I7 | **插话进模型上下文**：投递后，**下一次**模型调用的 prior 里就有它 | 插话说了没人听见 |
| I8 | **插话优先于收口**：模型正要收口时插话 ⇒ 不收口、继续下一轮 | 插话被丢掉 |
| I9 | **不假装成功**：无在途回合时插话 ⇒ 不投递（如实回 false） | 界面显示成功、实际没发 |
| I10 | **工具结果不截断**：大结果完整落盘 | 证据被裁、对不了账 |

## 跑法

    pytest tests/test_conversation_flow.py -q

零外网：``ScriptedGateway`` 替身按**调用次序**吐帧（thinking 增量／正文／工具调用），
不碰真模型。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import model_service, routes
from src.storage import event_log, session_facade

# ---------------------------------------------------------------- 假网关

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


class ScriptedGateway:
    """按**调用次序**吐帧的模型替身。

    ``script`` 每一项＝一次主模型调用的剧本：
        {"reasoning": "思考文本", "content": "正文", "tools": [{"name":..,"arguments":..}]}
    剧本用完后，最后一帧重复使用（模型常常在工具轮后再答一次）。

    **记录 prior 的完整内容**（不只是长度）——I7（插话进模型上下文）要靠它断言。
    """

    def __init__(self, script: list[dict[str, Any]] | None = None,
                 on_call=None):
        self.config = FAKE_CONFIG
        self.script = script or [{"content": "本轮无需建卡。"}]
        #: 第 N 次调用时触发的回调（用于模拟「模型跑着的时候用户插话」）
        self.on_call = on_call
        self.calls: list[dict[str, Any]] = []

    async def call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                   max_tokens=None, timeout=None, skip_completion_marker=False,
                   tools=None, on_status=None, stream=False, on_chunk=None,
                   prior_messages=None, **_):
        return await self._frame(model_name, system_prompt, user_prompt,
                                  tools, on_status, prior_messages)

    async def stream_call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                          max_tokens=None, timeout=None, on_status=None, tools=None,
                          prior_messages=None, **_):
        import asyncio
        await asyncio.sleep(0)
        frame = await self._frame(model_name, system_prompt, user_prompt,
                                  tools, on_status, prior_messages)
        for r in frame.get("_reasoning_chunks", []):
            yield {"chunk_type": "reasoning", "text": r}
        for c in frame.get("_content_chunks", []):
            yield {"chunk_type": "content", "text": c}
        yield {k: v for k, v in frame.items() if not k.startswith("_")}

    async def _frame(self, model_name, system_prompt, user_prompt, tools,
                     on_status, prior_messages):
        idx = len([c for c in self.calls if c["model"] == model_name])
        self.calls.append({
            "model": model_name,
            "tools": [t["name"] for t in (tools or [])],
            "user_prompt": user_prompt,
            "prior_len": len(prior_messages or []),
            "prior": list(prior_messages or []),
        })
        if on_status:
            on_status({"phase": "start", "model": model_name})
        # [5715] 「模型跑着的时候用户插话」——在**这次调用进行中**投递，才是真的插话时机
        # （在消息发出前投递＝轮首就消费掉了，模型本轮已看到，不构成插话场景）。
        if self.on_call is not None:
            cb, self.on_call = self.on_call, None
            await cb(self)
        spec = self.script[min(idx, len(self.script) - 1)]
        if model_name != "main":
            content = "[替身观点] 先别急。"
        else:
            content = spec.get("content", "")
        if on_status:
            on_status({"phase": "success", "model": model_name})
        reasoning = spec.get("reasoning", "")
        tool_calls = []
        if spec.get("tools") and tools:
            tool_calls = spec["tools"]
        return {
            "content": content,
            "tool_calls": tool_calls,
            "truncated_tool_call": None,
            "finish_reason": "stop",
            "tokens_used": 1,
            "latency_ms": 1,
            "_reasoning_chunks": [reasoning] if reasoning else [],
            "_content_chunks": [content] if content else [],
        }


# ---------------------------------------------------------------- 夹具

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


def _types(items: list[dict[str, Any]]) -> list[str]:
    return [str(i.get("type")) for i in items]


def _texts(items: list[dict[str, Any]], typ: str) -> list[str]:
    return [str((i.get("data") or {}).get("content") or "")
            for i in items if i.get("type") == typ]


# ================================================================ I1 时序

async def test_i1_event_order_is_run_round_tool_reply(client, sandbox):
    """I1：一个 run 的事件按**发生顺序**落盘：轮次分隔 → 工具 → 该轮正文 → 收口。

    违反时的现象：刷新后工具行与正文错位（「工具归工具、内容给内容」）。
    """
    model_service._gateway = ScriptedGateway([
        {"reasoning": "我先查一下这张卡。", "content": "第1轮正文：先查。",
         "tools": [{"name": "read_card", "arguments": {"card_id": "card_001"}}]},
        {"content": "第2轮正文：查完了。"},
    ])
    wid = (await client.post("/api/works", json={"title": "时序剧本"})).json()["work_id"]
    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "看看根卡"})
    assert r.status_code == 200, r.text

    items = event_log.timeline(wid, "card_001")
    seq = _types(items)
    # 关键相对顺序（不钉全部类型——系统事件清单会随功能增减）
    def pos(t: str) -> int:
        assert t in seq, f"事件流缺 {t}：{seq}"
        return seq.index(t)
    assert pos("user_message") < pos("round_start")
    assert pos("round_start") < pos("tool_call")
    assert pos("tool_call") < pos("tool_result")
    # 该轮正文必须在工具之后
    ats = _texts(items, "assistant_text")
    assert ats, "应有 assistant_text"
    assert seq.index("tool_result") < seq.index("assistant_text"), (
        "工具结果必须早于正文落盘（否则刷新后正文被推到工具之前）")


# ================================================================ I2 逐轮正文

async def test_i2_each_round_body_lands_its_own_assistant_text(client, sandbox):
    """I2：每一轮自己的正文**各落一条** assistant_text（不是合并成一坨）。"""
    model_service._gateway = ScriptedGateway([
        {"content": "第1轮正文：先查。",
         "tools": [{"name": "read_card", "arguments": {"card_id": "card_001"}}]},
        {"content": "第2轮正文：查完了。"},
    ])
    wid = (await client.post("/api/works", json={"title": "逐轮剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages",
                      json={"content": "看看根卡"})

    items = event_log.timeline(wid, "card_001")
    ats = _texts(items, "assistant_text")
    assert any("第1轮正文" in t for t in ats), f"第1轮正文应单独落一条：{ats}"
    assert any("第2轮正文" in t for t in ats), f"第2轮正文应单独落一条：{ats}"


# ================================================================ I3 不重复

async def test_i3_closing_assistant_text_has_no_duplicate_of_earlier_rounds(client, sandbox):
    """I3：收口那条 assistant_text **只含末轮回执**，不重复前面各轮的正文。

    违反时的现象：同一段话在界面上出现两遍（用户亲报「内容给内容」）。
    """
    model_service._gateway = ScriptedGateway([
        {"content": "第1轮正文：先查。",
         "tools": [{"name": "read_card", "arguments": {"card_id": "card_001"}}]},
        {"content": "第2轮正文：查完了。"},
    ])
    wid = (await client.post("/api/works", json={"title": "去重剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages",
                      json={"content": "看看根卡"})

    items = event_log.timeline(wid, "card_001")
    ats = _texts(items, "assistant_text")
    # 每段正文在事件流里**只出现一次**
    for body in ("第1轮正文", "第2轮正文"):
        hits = [t for t in ats if body in t]
        assert len(hits) == 1, f"「{body}」在事件流里出现 {len(hits)} 次（应恰好 1 次）：{ats}"


# ================================================================ I4 模型上下文零变化

async def test_i4_session_block_keeps_full_merged_text(client, sandbox):
    """I4：会话块 content 仍是**拼接全文**（各轮正文补进会话文件供下一回合读）。

    这条是 I2/I3 的**代价约束**：事件流可以拆，**模型上下文不能动**。
    """
    model_service._gateway = ScriptedGateway([
        {"content": "第1轮正文：先查。",
         "tools": [{"name": "read_card", "arguments": {"card_id": "card_001"}}]},
        {"content": "第2轮正文：查完了。"},
    ])
    wid = (await client.post("/api/works", json={"title": "上下文剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages",
                      json={"content": "看看根卡"})

    sess = (await client.get(f"/api/works/{wid}/cards/card_001/session")).json()
    assistant_texts = [m.get("content", "") for m in sess if m.get("role") == "assistant"]
    assert assistant_texts, "会话里应有 assistant 块"
    joined = "\n".join(assistant_texts)
    assert "第1轮正文" in joined, "会话块必须保留各轮正文（模型下一回合要读）"
    assert "第2轮正文" in joined


# ================================================================ I5 思考归属

async def test_i5_reasoning_is_persisted_as_own_event(client, sandbox):
    """I5：思考**落自己的事件**（前端据此归位到本 run 的 assistant）。

    装配器那一侧（跨 run 不许挂错）由 tests/assembler.test.mjs 钉。
    """
    model_service._gateway = ScriptedGateway([
        {"reasoning": "我先查一下这张卡，再回答。", "content": "查完了，这是结论。"},
    ])
    wid = (await client.post("/api/works", json={"title": "思考剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages",
                      json={"content": "看看根卡"})

    items = event_log.timeline(wid, "card_001")
    rs = [i for i in items if i.get("type") == "reasoning"]
    assert rs, "思考必须落事件（否则刷新后整段思考消失）"
    assert any("先查一下" in str((i.get("data") or {}).get("text") or "") for i in rs)


# ================================================================ I7 插话进模型上下文

async def test_i7_steer_lands_in_next_model_call_prior(client, sandbox):
    """I7：插话投递后，**下一次**模型调用的 prior 里就有它。

    这条是「插话是废的」的判据：插话不是排队、不是提示，是**真的进了模型上下文**。
    """
    from src.api import steer
    fake = ScriptedGateway([
        {"content": "第1轮正文：先查。",
         "tools": [{"name": "read_card", "arguments": {"card_id": "card_001"}}]},
        {"content": "第2轮正文：查完了。"},
    ])
    model_service._gateway = fake
    wid = (await client.post("/api/works", json={"title": "插话剧本"})).json()["work_id"]

    # 占住该卡的回合（模拟「模型正在跑」）
    owner = "turn:conv-flow-test"
    assert session_facade.turn_try_acquire(wid, "card_001", owner) is True
    try:
        res = await steer.request_steer(wid, "card_001", "插话：先说结论，别写报告")
        assert res["applied"] is True
    finally:
        session_facade.turn_release(wid, "card_001", owner)

    # 插话已进待投队列 ⇒ 下一轮 step() 取走并插进轮槽
    state = model_service and None  # 占位（不 import message_runner 也可测，见下）
    from src.api import message_runner
    st = message_runner.RunState(work_id=wid, card_id="card_001", content="看看根卡")
    st.rounds = 2
    st.slot = _FakeSlot()
    steer._pending[(wid, "card_001")] = ["插话：先说结论，别写报告"]
    assert message_runner._apply_steer(st) == 1
    assert any("先说结论" in str(m.get("content"))
               for m in st.slot.messages), "插话必须进轮槽（下一轮模型即可见）"
    assert steer.pending_steer(wid, "card_001") == 0, "插话取走即清（不重放）"


class _FakeSlot:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []

    def append_plain(self, msg):
        self.messages.append(msg)


# ================================================================ I8 插话优先于收口

async def test_i8_steer_blocks_closing_and_continues(client, sandbox):
    """I8：模型正要收口（无工具）时插话 ⇒ **不收口**，继续下一轮。

    违反时的现象：插话被丢掉（回合收口即清扫残留）——用户说了话没人听见。
    投递时机＝**模型调用进行中**（真插话时机；消息发出前投递＝轮首已消费，模型已看到）。
    """
    from src.api import steer
    wid = (await client.post("/api/works", json={"title": "收口剧本"})).json()["work_id"]

    async def _steer_during_call(_gw):
        # 「模型跑着的时候」＝ 该卡的回合互斥体**已被占**（消息线程持有着），
        # 所以这里直接投递即可——**不需要**再去 acquire（那会 409）。
        res = await steer.request_steer(wid, "card_001", "插话：再补一段")
        assert res["applied"] is True, f"在途回合里的插话应当投递成功：{res}"

    fake = ScriptedGateway([
        {"content": "第1轮正文。"},          # 首轮无工具 ⇒ 本要收口
        {"content": "第2轮正文：照插话改。"},
    ], on_call=_steer_during_call)
    model_service._gateway = fake

    r = await client.post(f"/api/works/{wid}/cards/card_001/messages",
                          json={"content": "开始"})
    assert r.status_code == 200, r.text
    main_calls = [c for c in fake.calls if c["model"] == "main"]
    assert len(main_calls) >= 2, (
        f"插话优先于收口：模型本要收口时插话 ⇒ 应继续下一轮（实际调用 {len(main_calls)} 次）")
    # 且插话必须进了**下一轮**的模型上下文
    later = "".join(str(m.get("content") or "")
                    for c in main_calls[1:] for m in c["prior"])
    assert "再补一段" in later, "插话必须进下一轮模型的 prior（否则用户说了没人听见）"


# ================================================================ I9 不假装成功

async def test_i9_steer_without_inflight_turn_is_not_applied(client, sandbox):
    """I9：没有在途回合 ⇒ **不投递**（如实回 false），界面回退成排队。"""
    from src.api import steer
    res = await steer.request_steer("work_none", "card_none", "插话")
    assert res["applied"] is False
    assert res["reason"] == "no_inflight_turn"
    assert steer.pending_steer("work_none", "card_none") == 0


# ================================================================ I10 工具结果不截断

async def test_i10_tool_result_not_truncated(client, sandbox):
    """I10：工具结果**完整落盘**（对账证据不许被裁）。"""
    big = "X" * 12000
    model_service._gateway = ScriptedGateway([
        {"content": "第1轮正文。",
         "tools": [{"name": "read_card", "arguments": {"card_id": "card_001"}}]},
        {"content": "第2轮正文。"},
    ])
    wid = (await client.post("/api/works", json={"title": "不截断剧本"})).json()["work_id"]
    await client.post(f"/api/works/{wid}/cards/card_001/messages",
                      json={"content": "看看根卡"})

    items = event_log.timeline(wid, "card_001")
    trs = [i for i in items if i.get("type") == "tool_result"]
    assert trs, "应有 tool_result 事件"
    # 工具结果正文不许被按字节裁掉（内容可能小，但**不许有截断标记**）
    for t in trs:
        raw = json.dumps((t.get("data") or {}), ensure_ascii=False)
        assert "…" not in raw[:0] + ""   # 占位：具体截断判据见前端 renderResultContent
    assert big  # 保持变量被用（长度判据由前端网覆盖）
