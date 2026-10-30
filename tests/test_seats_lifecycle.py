# -*- coding: utf-8 -*-
"""**三席（席卡质询 + 统合）生命周期网**（[5715]）。

## 为什么要有这张网

三席这条链路**有**测试（``test_s4_seams.py`` / ``test_seat_identity_injection.py`` /
``test_viewpoint_think.py`` / ``test_model_process_events.py``），但它们**全部是跳过的**：

    pytestmark = pytest.mark.skipif(not src.thinkchain.enabled(), ...)

2026-10-17 用户令「纯单模型模式」把 ``thinkchain.ENABLED`` 置 False ⇒
**10 个思维链测试文件一条都没真跑过**；而且全仓**没有任何一处**把
``SEATS_ENABLED`` 置 True ⇒ 即使只开 ``ENABLED``，``seats_enabled()`` 仍恒 False
⇒ 三席分支**根本不会被触发**。

⇒ 「三席有测试」是**假象**：那批用例是**沉睡**的（而且其中一条
``test_submit_round_seats_after_chain_done`` 一旦解除跳过就会红）。

这张网做两件事：
  ① **强制把两个开关打开**（monkeypatch 模块常量，运行期读取）⇒ 三席真的跑起来；
  ② 钉住三席的**接口约定**（不是实现细节）：席数、工具面、材料落点、事件序、
     观点不外泄。

零外网、零真实 works 触碰（沙箱同 ``tests/conftest.py``）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import event_stream, model_service, routes
from src.storage import card_store, event_log, session_facade
from src.thinkchain import board as board_mod

#: 两个开关都要开：ENABLED（思维链总开关）＋ SEATS_ENABLED（三席开关）。
#: 只开前者 seats_enabled() 仍恒 False（生产当前口径就是这样）。
#:
#: ★ 还要**补一次注册**：思维链六件（create_plan／step_complete…）是
#: **导入期**注册进 ``card_tools.TOOL_REGISTRY`` 的（``card_tools.py:2909``，
#: 注册函数自己再查一次开关）。⇒ 开关在导入**之后**才打开的话，注册表里根本
#: 没有 create_plan ⇒ 模型永远建不了板 ⇒ 三席面永远不触发。
#: ⇒ 这里按「开关为开时产品导入期会做的事」补做一次注册，收尾再撤掉。
@pytest.fixture(autouse=True)
async def _turn_seats_on(monkeypatch):
    import src.thinkchain as tc
    from src.api import card_tools
    from src.thinkchain import plan_tools

    monkeypatch.setattr(tc, "ENABLED", True)
    monkeypatch.setattr(tc, "SEATS_ENABLED", True)
    added = [t["schema"]["name"] for t in plan_tools.PLAN_TOOLS]
    n = plan_tools.register_into(card_tools.TOOL_REGISTRY, card_tools.ToolDef)
    assert n == len(added), f"思维链六件应注册成功：{n} != {len(added)}"

    await _drain_board_tasks()
    board_mod._boards.clear()
    board_mod._locks.clear()
    yield
    # ★ 收尾必须**等后台任务真的结束**：思维链的步执行是 create_task 起的，
    # 只清引用不等待 ⇒ 上一条测试的任务还在跑（握着板锁）⇒ 下一条测试建板
    # 失败 ⇒ 三席永远不触发（症状：前一条能跑、后一条空跑）。实测踩过。
    await _drain_board_tasks()
    for name in added:
        card_tools.TOOL_REGISTRY.pop(name, None)
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


async def _drain_board_tasks() -> None:
    """等思维链的后台任务收尾（最多 3 秒；不抛）。"""
    import contextlib

    deadline = 3.0
    step = 0.05
    while board_mod._background_tasks and deadline > 0:
        for t in list(board_mod._background_tasks):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(t), timeout=deadline)
        deadline -= step
        await asyncio.sleep(step)


FAKE_CONFIG: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "main", "temperature": 0.3},
        # ★ 席的**绑定键与模型名同名**（生产 models.yaml 就是这样：
        #   viewpoint: [qwen-max, glm-53, minimax-m31-flash-preview]）。
        #   原因：席的**模型调用**用绑定键，席**事件**的 model 字段用解析后的模型名
        #   ——生产里两者同一个字符串，测试里若不同名就会看起来像两套命名。
        "vp1": {"provider": "openai", "model": "vp1", "temperature": 0.7},
        "vp2": {"provider": "openai", "model": "vp2", "temperature": 0.7},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main", "viewpoint": ["vp1", "vp2"]},
    "system": {"default_model": "main"},
    "simple": {"enabled": True},
}

#: 席的输出正文：**故意不含模型名**（好断言「统合稿里是匿名段」）
SEAT_TEXT = "[席观点] 判据逐条核对通过。"


class RecordingGateway:
    """记录每次模型调用的替身（主模型与三席都记）。"""

    def __init__(self, script: list[list[dict[str, Any]]] | None = None,
                 config: dict[str, Any] | None = None, on_call=None):
        self.config = config or FAKE_CONFIG
        self.script = script or []
        self.calls: list[dict[str, Any]] = []
        #: 第 N 次调用时触发的回调（用于模拟「模型跑着的时候板被别人交完了」）
        self.on_call = on_call

    async def call(self, model_name, system_prompt, user_prompt, **kw):
        self.calls.append({
            "model": model_name,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prior": list(kw.get("prior_messages") or []),
            "tools": kw.get("tools"),
            "turn_tail": list(kw.get("turn_tail") or []),
        })
        return {"content": SEAT_TEXT, "tool_calls": [],
                "truncated_tool_call": None, "finish_reason": "stop",
                "tokens_used": 1, "latency_ms": 1}

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None,
                          turn_tail=None, turn_once=None, **_):
        await asyncio.sleep(0)   # 与生产网络 IO 的让出语义对齐（人停刹车可达）
        self.calls.append({
            "model": model_name,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prior": list(prior_messages or []),
            "tools": tools,
            "turn_tail": list(turn_tail or []),
        })
        if self.on_call is not None:
            cb, self.on_call = self.on_call, None
            await cb(self)
        idx = len(self.calls) - 1
        tool_calls = self.script[idx] if idx < len(self.script) else []
        is_seat = str(model_name).startswith("vp")
        if is_seat:
            # 席：带思考增量（带 model 标记），正文只回一句观点
            yield {"chunk_type": "reasoning", "text": "席的思考"}
            yield {"chunk_type": "content", "text": SEAT_TEXT}
        else:
            yield {"chunk_type": "content", "text": "主模型正文"}
        yield {"content": SEAT_TEXT if is_seat else "主模型正文",
               "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}


def _create_plan_call() -> dict[str, Any]:
    """建板那一次的 tool_call（两步计划）。"""
    return {"name": "create_plan", "arguments": {
        "name": "两步计划",
        "steps": [{"id": "s1", "prompt": "做甲", "criteria": "甲的判据是文件存在",
                   "deliverable": "甲产出", "type": "execute"},
                  {"id": "s2", "prompt": "做乙", "criteria": "乙的判据是结论落板",
                   "deliverable": "乙产出", "type": "think"}]}}


def _plan_script() -> list[list[dict[str, Any]]]:
    """建板 → 逐步完成 → 收口后空手（触发三席）。"""
    return [
        [_create_plan_call()],
        [{"name": "step_complete", "arguments": {"result": "甲做完了",
                                                 "criteria_met": "判据满足：文件在"}}],
        [{"name": "step_complete", "arguments": {"result": "乙做完了",
                                                 "criteria_met": "判据满足：结论在"}}],
        [],
    ]


async def _wait_for_seats(gw, timeout: float = 8.0) -> list[dict]:
    """等三席真的跑完。

    三席是**异步触发**的：板收口由后台任务推进 ⇒ 席位调用可能落在请求内、
    也可能落在请求之后。⇒ 网不能假设「POST 返回 ⇒ 席已跑完」，得等。
    （实测：不等的话，同一份网里有的用例能过、有的过不了——竞态，不是产品缺陷。）
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seats = _seat_calls(gw)
        if seats:
            return seats
        await asyncio.sleep(0.05)
    return _seat_calls(gw)

async def _post(client, wid: str, cid: str, content: str) -> dict:
    r = await asyncio.wait_for(
        client.post(f"/api/works/{wid}/cards/{cid}/messages",
                    json={"content": content}), timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def _seat_model_names() -> set[str]:
    """席调用用的是**解析后的模型名**（绑定键 vp1 → models.vp1.model = fake-vp1），
    不是绑定键本身——断言要按解析后的名字认席。"""
    return {FAKE_CONFIG["models"][k]["model"]
            for k in FAKE_CONFIG["bindings"]["viewpoint"]}


def _seat_calls(gw) -> list[dict]:
    names = _seat_model_names()
    return [c for c in gw.calls if str(c["model"]) in names]


# ================================================================ 触发判据

async def test_seats_not_triggered_without_plan(client, work, monkeypatch):
    """没建板（直答）⇒ **零三席**（三席只在思维链收口后进场）。"""
    gw = RecordingGateway(script=[[]], config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    body = await _post(client, work["work_id"], work["work_card_id"], "直接答我")
    assert not _seat_calls(gw), f"直答不该有席位调用：{_seat_calls(gw)}"
    metas = body.get("viewpoints_meta") or []
    assert not metas, f"直答不该有 viewpoints_meta：{metas}"


async def test_seats_triggered_after_chain_done(client, work, monkeypatch):
    """板全 done ⇒ 收口后进三席，且**席数 == 绑定列表长度**、每席各调一次。"""
    gw = RecordingGateway(script=_plan_script(), config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    body = await _post(client, work["work_id"], work["work_card_id"], "帮我把甲乙两件事做成")

    seats = await _wait_for_seats(gw, timeout=20.0)
    assert seats, "三席必须真被调（板全 done ⇒ 提交轮三席）"
    assert len(seats) == len(FAKE_CONFIG["bindings"]["viewpoint"]), \
        f"席数应等于 viewpoint 绑定数：{len(seats)} vs {FAKE_CONFIG['bindings']['viewpoint']}"
    assert sorted(c["model"] for c in seats) == sorted(_seat_model_names()), f"每席各调一次：{seats}"
    metas = body.get("viewpoints_meta") or []
    assert len(metas) == 2, f"响应体应带每个席的元信息：{metas}"
    assert all(m.get("ok") is True for m in metas), f"席位都应成功：{metas}"


# ================================================================ 席的工具面与材料

async def test_seats_get_no_tools_and_history_only_prior(client, work, monkeypatch):
    """席的调用面：**无工具**、prior 是**纯历史切片**（不喂工具消息）。"""
    gw = RecordingGateway(script=_plan_script(), config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    await _post(client, work["work_id"], work["work_card_id"], "帮我把甲乙两件事做成")

    seats = await _wait_for_seats(gw)
    assert seats, "三席应真被调"
    for c in seats:
        assert not c["tools"], f"席不得带工具：{c['model']} 带了 {c['tools']}"
        for m in c["prior"]:
            assert m.get("role") != "tool", f"席的 prior 不得含工具消息：{m}"


async def test_seat_prompt_carries_chain_material(client, work, monkeypatch):
    """席的质询文必须带上**板里的步成果与判据**（不然席无从核对）。"""
    gw = RecordingGateway(script=_plan_script(), config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    await _post(client, work["work_id"], work["work_card_id"], "帮我把甲乙两件事做成")

    seats = await _wait_for_seats(gw)
    assert seats, "三席应真被调"
    prompt = seats[0]["user_prompt"]
    assert "成果(result): 甲做完了" in prompt, f"席的质询文必须含步成果：{prompt[:400]}"
    assert "做甲" in prompt, f"席的质询文必须含步要求：{prompt[:400]}"


# ================================================================ 统合官

async def test_synthesizer_gets_anonymous_seat_sections(client, work, monkeypatch):
    """统合官拿到的是**匿名席段**（## 其他模型的输出 ＋ ### 输出 N），**不含席模型名**。

    理由（产品口径）：统合官要综合观点，不该被席的名字带跑（投票式偏袒）。
    """
    gw = RecordingGateway(script=_plan_script(), config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    await _post(client, work["work_id"], work["work_card_id"], "帮我把甲乙两件事做成")

    seats = await _wait_for_seats(gw, timeout=20.0)
    assert seats, "三席必须真被调"
    synth = [c for c in gw.calls if "其他模型的输出" in str(c["user_prompt"])]
    assert synth, f"统合官应被调并收到席材料：{[c['model'] for c in gw.calls]}"
    prompt = synth[0]["user_prompt"]
    assert "### 输出" in prompt, f"席材料应是分段的匿名段：{prompt[:300]}"
    assert SEAT_TEXT in prompt, "席正文必须传给统合官"
    assert not any(n in prompt for n in _seat_model_names()), \
        f"统合稿里不该出现席模型名：{prompt[:300]}"


async def test_seat_content_never_enters_session(client, work, monkeypatch):
    """席的原文**不进会话块**（只进统合官的输入）——否则下一轮模型会读到原始观点，
    把「多模型质询」变成「模型自说自话」。"""
    gw = RecordingGateway(script=_plan_script(), config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    await _post(client, work["work_id"], work["work_card_id"], "帮我把甲乙两件事做成")

    blocks = await session_facade.read(work["work_id"], work["work_card_id"])
    for b in blocks:
        assert SEAT_TEXT not in str(b.get("content") or ""), \
            f"席原文漏进会话块了：{b}"


# ================================================================ 事件序

async def test_seat_events_order_and_shape(client, work, monkeypatch):
    """事件序：``seats_enter`` 恰一次 → 每席 ``seat_start``/``seat_end`` → ``deliver``。

    ``seat_start`` 带 model（前端据此分桶），``seat_end`` 带正文（成功时）。
    """
    gw = RecordingGateway(script=_plan_script(), config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    await _post(client, work["work_id"], work["work_card_id"], "帮我把甲乙两件事做成")
    assert await _wait_for_seats(gw, timeout=20.0), "三席必须真被调"
    await event_log.drain()

    items = event_log.timeline(work["work_id"], work["work_card_id"])
    types = [i.get("type") for i in items]
    assert types.count("seats_enter") == 1, f"seats_enter 恰一次：{types}"
    starts = [i for i in items if i.get("type") == "seat_start"]
    ends = [i for i in items if i.get("type") == "seat_end"]
    assert len(starts) == 2, f"每席一条 seat_start：{types}"
    assert len(ends) == 2, f"每席一条 seat_end：{types}"
    assert {str((i.get("data") or {}).get("model")) for i in starts} == {"vp1", "vp2"}, \
        "seat_start 必须带 model（前端据此分桶）"
    for i in ends:
        assert (i.get("data") or {}).get("text"), "seat_end 成功时要带正文"
    assert types.index("seats_enter") < types.index("seat_start"), \
        f"seats_enter 必须在席位之前：{types}"
    assert types.index("seat_end") < types.index("deliver"), \
        f"席位必须先于交付：{types}"


# ================================================================ 卡面回写

async def test_chain_completed_judged_by_board_state_not_tool_receipt(
        client, work, monkeypatch):
    """〔P15 回归〕链完成的判据＝**板的事实**（全步 done），不是「主模型那次
    step_complete 的回执」。

    病根：末步若由**板的执行器**交掉，主模型随后拿到的是幂等回执（没有
    ``all_steps_done``）⇒ ``state.chain_completed`` 永远是 False ⇒ 板明明全 done，
    提交轮三席**不触发**——而末步的工具结果里明写「随后进入提交轮：三席将核对
    本步成果是否满足判据」⇒ **承诺落空**。

    本条直接钉判据本身（不构造「在途请求里外部交板」那种假状态——那会让板的后台
    任务持锁，与提交轮互等而死锁，是测试造出来的状态，不是产品会到的状态）。
    """
    from src.api import message_runner

    wid, cid = work["work_id"], work["work_card_id"]

    # ① 没有板 ⇒ 未完成
    assert message_runner._board_all_steps_done("work_none", "card_none") is False

    # ② 建板（直接用板 API，不走模型回合——本条只钉判据，不牵扯流程）
    r = await board_mod.create_board(wid, cid, "两步计划", [
        {"id": "s1", "prompt": "做甲", "criteria": "甲的判据", "deliverable": "甲产出",
         "type": "execute"},
        {"id": "s2", "prompt": "做乙", "criteria": "乙的判据", "deliverable": "乙产出",
         "type": "think"},
    ])
    assert r.get("ok"), f"建板应成功：{r}"

    # ③ 板还有没交的步 ⇒ 未完成
    assert message_runner._board_all_steps_done(wid, cid) is False

    # ④ 板全 done（末步由别人交掉）⇒ **完成**（判据只看板的事实，不看谁交的）
    b = await board_mod.get_board(wid, cid)
    assert b is not None
    for st in b._data["steps"]:
        st["status"] = "done"
    b._data["current"] = len(b._data["steps"])
    assert message_runner._board_all_steps_done(wid, cid) is True


async def test_chain_summary_written_back_to_card(client, work, monkeypatch):
    """三席收口后卡面要留痕（思维链收口 ＋ 计划名 ＋ 步成果）——不留痕等于
    「模型干了活，档案上什么都没有」。"""
    gw = RecordingGateway(script=_plan_script(), config=FAKE_CONFIG)
    monkeypatch.setattr(model_service, "_gateway", gw)
    await _post(client, work["work_id"], work["work_card_id"], "帮我把甲乙两件事做成")
    assert await _wait_for_seats(gw, timeout=20.0), "三席必须真被调"

    card = await card_store.read_card(work["work_id"], work["work_card_id"])
    body = card["body"]
    text = "\n".join([
        body.get("mission", ""), body.get("status_block", ""),
        body.get("requirements", ""), *(body.get("history") or []),
    ])
    assert "两步计划" in text, f"卡面应留计划名：{text[:300]}"
    assert "甲做完了" in text, f"卡面应留步成果：{text[:300]}"
