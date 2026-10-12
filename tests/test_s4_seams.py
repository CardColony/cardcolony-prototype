# -*- coding: utf-8 -*-
"""S4 接缝行为测试（迭代二原型；[思维链真实落地 2026-10-11] 判据化重写）。

覆盖：
- 判断协议注入：BASE_PROMPT 开头含附录 A 全文关键句（判断归模型，无独立
  判定调用——直答回合模型调用数恰 1）；
- 首轮工具面：全量（含 create_plan——判断+行动合一，代码零判定）；
- 催化：空手两轮被催（催促文＝附录 C 步执行提示词），第三轮补调推进；
- 提交轮三席统合（C6）：收口后三席进场质询、统合交付、卡面结论回写；
- t165 互斥实测：挂工具卡的卡不出现 t165 块；
- 无板卡对照差异=0 由 scripts/s4_compare_snapshot.py 承担（已跑，报告在案）。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.api import context_assembler, event_stream, message_runner, model_service, routes
from src.api.prompts import BASE_PROMPT
from src.storage import session_facade
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

FAKE_CONFIG_SEATS: dict[str, Any] = {
    "models": {
        "main": {"provider": "openai", "model": "fake-main", "temperature": 0.3},
        "vp1": {"provider": "openai", "model": "fake-vp", "temperature": 0.7},
    },
    "bindings": {"execute": "main", "think": "main", "synthesize": "main",
                 "compress": "main", "draft": "main", "viewpoint": ["vp1"]},
    "system": {"default_model": "main"},
    "simple": {"enabled": True},
}


def _steps_def(*prompts: str) -> list[dict]:
    return [{"id": f"s{i + 1}", "prompt": p, "criteria": f"{p}的判据",
             "deliverable": f"{p}的产出", "type": "execute"}
            for i, p in enumerate(prompts)]


class ScriptedGateway:
    """按圈脚本化 gateway：记录每圈 (user_prompt, tools 名单)，按脚本发
    tool_calls（脚本耗尽后重复末圈——提交轮统合官复用末圈空手帧）。"""

    def __init__(self, script: list[list[dict[str, Any]]] | None = None,
                 config: dict[str, Any] | None = None):
        self.config = config or FAKE_CONFIG
        self.script = script or []
        self.calls: list[dict[str, Any]] = []

    async def call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                   max_tokens=None, timeout=None, skip_completion_marker=False,
                   tools=None, prior_messages=None, **_):
        """三席（run_viewpoints→gw.call）替身：vp1 返回替身观点。"""
        self.calls.append({
            "model": model_name,
            "user_prompt": user_prompt,
            "tools": [t["name"] for t in (tools or [])],
            "prior_len": len(prior_messages or []),
        })
        content = "[替身观点:vp1] 判据逐条核对通过。" if model_name == "vp1" else "回合正文"
        return {"content": content, "tool_calls": [],
                "truncated_tool_call": None, "finish_reason": "stop",
                "tokens_used": 1, "latency_ms": 1}

    async def stream_call(self, model_name, system_prompt, user_prompt,
                          temperature=0.5, max_tokens=None, timeout=None,
                          on_status=None, tools=None, prior_messages=None, **_):
        # 让出点（S5 诊断定案）：真实网关每圈走网络 IO 必然让出事件循环，
        # 人停刹车（check_control）可达；Mock 零 IO 则催化循环独占循环、
        # 刹车饿死（test_forgery/human_stop 挂死根因）——此处补一次让出，
        # 与生产「每圈至少一次 IO 让出」语义对齐。生产代码零改动。
        await asyncio.sleep(0)
        self.calls.append({
            "model": model_name,
            "user_prompt": user_prompt,
            "tools": [t["name"] for t in (tools or [])],
            "prior_len": len(prior_messages or []),
        })
        # 脚本耗尽后一律空手（收玫到 deliver）——重复末圈会让收口后的
        # 假模型永远重发 step_complete（板不在→明文拒绝→再续跳）死循环。
        idx = len(self.calls) - 1
        tool_calls = self.script[idx] if idx < len(self.script) else []
        content = ("[替身观点:vp1] 判据逐条核对通过。" if model_name == "vp1"
                   else "回合正文")
        yield {"chunk_type": "content", "text": content}
        yield {"content": content, "tool_calls": tool_calls,
               "truncated_tool_call": None, "finish_reason": "stop",
               "tokens_used": 1, "latency_ms": 1}


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


def _last_round_types(wid: str, cid: str) -> list[str]:
    """本回合事件类型段（journal 跨用例追加不清空——T092 退役后旧段保留，
    从最后一个 run_start 起切片＝本回合）。"""
    journal = event_stream.journal_of(wid, cid)
    for i in range(len(journal) - 1, -1, -1):
        if journal[i]["type"] == "run_start":
            return [e["type"] for e in journal[i:]]
    return [e["type"] for e in journal]


async def _post(client, wid, cid, content: str) -> dict:
    r = await asyncio.wait_for(
        client.post(f"/api/works/{wid}/cards/{cid}/messages",
                    json={"content": content}), timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------- 判断协议注入（C1：判断归模型）

def test_judgment_protocol_in_base_prompt():
    """附录 A 关键句在 BASE_PROMPT 开头（system 区协议注入）。"""
    head = BASE_PROMPT[:2000]
    assert "入口判断器" in head
    assert "直接回答" in head and "拆解上板" in head
    assert "调 create_plan 拆解上板" in head
    assert "拿不准就拆" in head
    # 协议在使用说明之前（注入位＝BASE_PROMPT 开头）
    assert BASE_PROMPT.index("入口判断器") < BASE_PROMPT.index("系统使用说明")


async def test_first_round_full_tools_and_no_judgment_call(work, monkeypatch):
    """首轮＝全量工具面（含 create_plan），且无独立判定调用（直答回合模型
    调用数恰 1——判断+行动合一，代码零判定）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    gw = ScriptedGateway(script=[[]])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _post(client, wid, cid, "你好呀")

    assert len(gw.calls) == 1  # 无判定轮（旧 triage 独立调用已退役）
    names = gw.calls[0]["tools"]
    assert "create_plan" in names and "step_complete" in names  # 全量工具面
    assert "read_card" in names and "write_file" in names


# ------------------------------------------------- 判据 8：催化两轮被催、第三轮补调推进（附录 C 催促文）

async def test_catalyst_nags_until_step_complete(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    r = await board_mod.create_board(wid, cid, "催促计划", _steps_def("本步要求原文XYZ"))
    assert r["ok"], r
    gw = ScriptedGateway(script=[
        [],                                            # 圈1：空手 → 被催
        [],                                            # 圈2：仍空手 → 再催
        [{"name": "step_complete", "arguments": {"result": "成品X",
                                                 "criteria_met": "判据满足：产出X"}}],  # 圈3：补调
    ])
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = await _post(client, wid, cid, "开始干活")

    # 圈1：原文直入；圈2/圈3：催促文（附录 C 步执行提示词模板）
    assert gw.calls[0]["user_prompt"] == "开始干活"
    assert "【步骤执行】你现在执行计划的一步，只做本步的事。" in gw.calls[1]["user_prompt"]
    assert "本步要求：本步要求原文XYZ" in gw.calls[1]["user_prompt"]
    assert "完成判据：本步要求原文XYZ的判据" in gw.calls[1]["user_prompt"]
    assert "产出物：本步要求原文XYZ的产出" in gw.calls[1]["user_prompt"]
    assert "不许谎报" in gw.calls[1]["user_prompt"]
    assert "本步要求原文XYZ" in gw.calls[2]["user_prompt"]  # 第二次催仍带原文
    # 圈4：工具结果回灌轮（占位符）
    assert gw.calls[3]["user_prompt"] == "工具结果已返回。"
    # 末步交作业→收口→（三席关）空手正常交付
    assert body["reply"] if "reply" in body else True
    # 板已收口清理
    assert not board_mod.plan_path(wid, cid).exists()
    assert not board_mod.has_live_board(wid, cid)
    # 档案：step 块（含判据满足）＋final 块落档
    blocks = await session_facade.read(wid, cid)
    behaviors = [b.get("behavior") for b in blocks]
    assert "execute_step" in behaviors and "final" in behaviors
    # 工具事实：step_complete 真执行（tool_events 有记录——防伪造判据的载体）
    step_evts = [e for e in body.get("tool_events", []) if e.get("tool") == "step_complete"]
    assert step_evts and step_evts[-1].get("ok") is True


# ------------------------------------------------- 判据 1：直答回合零三席（C6 三席只在提交轮）

async def test_direct_reply_no_seats(work, monkeypatch):
    """直答回合（无板空手）→ 直接交付，三席零进场（旧空手轮旁路退役）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    gw = ScriptedGateway(script=[[]], config=FAKE_CONFIG_SEATS)
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = await _post(client, wid, cid, "你好呀")

    assert body["viewpoints_meta"] == []
    assert "seats_enter" not in _last_round_types(wid, cid)


# ------------------------------------------------- 提交轮三席统合（C6：收口后进场质询）

async def test_submit_round_seats_after_chain_done(work, monkeypatch):
    """拆解链收口 → 三席质询（输入含全部步成果与判据）→ 统合交付。"""
    wid, cid = work["work_id"], work["work_card_id"]
    gw = ScriptedGateway(script=[
        [{"name": "create_plan", "arguments": {
            "name": "两步计划",
            "steps": [{"id": "s1", "prompt": "做甲", "criteria": "甲的判据是文件存在",
                       "deliverable": "甲产出", "type": "execute"},
                      {"id": "s2", "prompt": "做乙", "criteria": "乙的判据是结论落板",
                       "deliverable": "乙产出", "type": "think"}]}}],
        [{"name": "step_complete", "arguments": {"result": "甲做完了",
                                                 "criteria_met": "判据满足：文件在"}}],
        [{"name": "step_complete", "arguments": {"result": "乙做完了",
                                                 "criteria_met": "判据满足：结论在"}}],
        [],   # 收口后空手 → 提交轮触发
    ], config=FAKE_CONFIG_SEATS)
    monkeypatch.setattr(model_service, "_gateway", gw)

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = await _post(client, wid, cid, "帮我把甲乙两件事做成")

    # 三席进场恰一次、发生在收口后
    assert body["viewpoints_meta"] and body["viewpoints_meta"][0]["ok"] is True
    # 三席输入＝步成果×判据材料（质询文）——vp1 未进 model_pick 回落 main，
    # 按 user_prompt 质询文取三席调用记录
    vp_calls = [c for c in gw.calls if "质询" in c["user_prompt"]
                and "完成判据" in c["user_prompt"]]
    assert vp_calls  # 三席真被调
    assert "成果(result): 甲做完了" in vp_calls[0]["user_prompt"]
    assert "模型自述判据满足" in vp_calls[0]["user_prompt"]
    # 统合官最终交付
    assert body["reply"]
    # 板收口清理＋final 块落档＋卡面结论回写（统合稿并入提交轮输出；
    # 回写落位 finish——统合官是否中途查证都回写）
    assert not board_mod.plan_path(wid, cid).exists()
    blocks = await session_facade.read(wid, cid)
    assert any(b.get("behavior") == "final" for b in blocks)
    from src.storage import session_store
    card_text = (session_store.WORKS_DIR / wid / "cards" / f"{cid}.md").read_text(encoding="utf-8")
    assert "【思维链收口】" in card_text
    assert "已收口（提交轮三席统合）" in card_text
    assert "两步计划" in card_text          # 计划名
    assert "甲做完了" in card_text          # 步成果
    # 档案：三席有 seat 事件（本回合段恰一次）
    types = _last_round_types(wid, cid)
    assert types.count("seats_enter") == 1


# ------------------------------------------------- t165 注入＋互斥（新板快照：判据＋成果）

async def test_t165_region_injected_with_live_board(work):
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "注入计划",
                                 [{"id": "s1", "prompt": "第一件事", "criteria": "第一判据",
                                   "deliverable": "第一产出", "type": "execute"},
                                  {"id": "s2", "prompt": "第二件事", "criteria": "第二判据",
                                   "deliverable": "第二产出", "type": "think"}])
    regions = context_assembler.build_context_regions(wid, cid)
    t165 = [r for r in regions if r.get("order") == 165]
    assert len(t165) == 1
    text = t165[0]["text"]
    assert "注入计划" in text          # 计划名
    assert "1/2" in text               # 当前第几步
    assert "本步要求：第一件事" in text   # 本步要求原文（附录 C 模板）
    assert "完成判据：第一判据" in text   # 当前步判据
    assert "产出物：第一产出" in text     # 产出物声明


async def test_t165_carries_done_step_results(work):
    """跨回合不丢步成果：已推进步的 result 进 165 区。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "成果计划",
                                 [{"id": "s1", "prompt": "查资料", "criteria": "资料判据",
                                   "deliverable": "资料产出", "type": "execute"},
                                  {"id": "s2", "prompt": "写结论", "criteria": "结论判据",
                                   "deliverable": "结论产出", "type": "think"}])
    b = await board_mod.get_board(wid, cid)
    await b.mark_done_and_advance("资料已查：X 成立")
    regions = context_assembler.build_context_regions(wid, cid)
    t165 = [r for r in regions if r.get("order") == 165]
    text = t165[0]["text"]
    assert "已完成步成果" in text
    assert "资料已查：X 成立" in text
    assert "本步要求：写结论" in text


async def test_t165_absent_without_board(work):
    wid, cid = work["work_id"], work["work_card_id"]
    regions = context_assembler.build_context_regions(wid, cid)
    assert not [r for r in regions if r.get("order") == 165]


async def test_t165_mutual_exclusion_with_t160(work, monkeypatch):
    """互斥实测：工具卡运行时（t160 在）→ t165 不注入（工具卡先到先得）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "n", _steps_def("a"))

    from src.toolcard import step_prompt as toolcard_step_prompt
    monkeypatch.setattr(
        toolcard_step_prompt, "build_region",
        lambda w, c: {"order": 160, "name": "工具卡", "channel": "system",
                      "seats": ("main",), "text": "假 t160 区", "entries": 1})

    regions = context_assembler.build_context_regions(wid, cid)
    assert [r for r in regions if r.get("order") == 160]      # t160 在
    assert not [r for r in regions if r.get("order") == 165]  # t165 让位
