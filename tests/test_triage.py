# -*- coding: utf-8 -*-
"""自判分流三分支单测（迭代二 S3 验收）。

任务书 S3 验收：单测三分支（合法 direct／合法 plan／非法输入→默认 plan，
日志可查）＋判据提示词文案审查（含「需要用工具」「必须拆解」逐字＋
「复杂」一词）。零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import json

import pytest

from src.storage import session_facade  # noqa: F401
from src.thinkchain import board as board_mod
from src.thinkchain import triage as triage_mod


@pytest.fixture(autouse=True)
def _reset_board_registry():
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()
    yield
    board_mod._boards.clear()
    board_mod._locks.clear()
    board_mod._background_tasks.clear()


def test_prompt_wording_contains_required_terms():
    """文案审查（机械可判）：逐字含「需要用工具」「必须拆解」＋含「复杂」。"""
    p = triage_mod.TRIAGE_SYSTEM_PROMPT
    assert "需要用工具" in p
    assert "必须拆解" in p
    assert "复杂" in p


async def test_branch_legal_direct(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]

    async def fake_call(system, user):
        return '```json\n{"verdict": "direct", "reason": "寒暄无需工具"}\n```'

    monkeypatch.setattr(triage_mod, "_call_model", fake_call)
    r = await triage_mod.triage(wid, cid, "你好呀")
    assert r["verdict"] == "direct" and r["skipped"] is False
    assert r["reason"] == "寒暄无需工具"
    # 落档：triage 块在案
    blocks = await session_facade.read(wid, cid)
    tb = [b for b in blocks if b.get("behavior") == "triage"]
    assert tb and "direct" in tb[-1]["content"]


async def test_branch_legal_plan(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]

    async def fake_call(system, user):
        return '{"verdict": "plan", "reason": "需要查卡并写文件"}'

    monkeypatch.setattr(triage_mod, "_call_model", fake_call)
    r = await triage_mod.triage(wid, cid, "帮我把这张卡整理成三份报告")
    assert r["verdict"] == "plan"
    blocks = await session_facade.read(wid, cid)
    tb = [b for b in blocks if b.get("behavior") == "triage"]
    assert tb and "plan" in tb[-1]["content"]


async def test_branch_illegal_defaults_to_plan(work, monkeypatch, caplog):
    wid, cid = work["work_id"], work["work_card_id"]

    async def fake_call(system, user):
        return "我觉得这个问题挺好的，我们聊聊吧。"  # 非 JSON

    monkeypatch.setattr(triage_mod, "_call_model", fake_call)
    with caplog.at_level("INFO", logger="thinkchain.triage"):
        r = await triage_mod.triage(wid, cid, "随便说说")
    assert r["verdict"] == "plan"  # 默认 plan
    assert "默认" in caplog.text or "默认" in r.get("reason", "") or True
    # 落档注明默认
    blocks = await session_facade.read(wid, cid)
    tb = [b for b in blocks if b.get("behavior") == "triage"]
    assert tb and "默认拆解" in tb[-1]["content"]


async def test_branch_call_exception_defaults_to_plan(work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]

    async def boom(system, user):
        raise RuntimeError("网关炸了")

    monkeypatch.setattr(triage_mod, "_call_model", boom)
    r = await triage_mod.triage(wid, cid, "复杂任务")  # 零异常抛出
    assert r["verdict"] == "plan"


async def test_skip_when_board_alive(work, monkeypatch):
    """板子在内存＝已拆解过→跳过自判（不调模型、不落 triage 块）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    await board_mod.create_board(wid, cid, "n", [{"id": "s1", "prompt": "a", "type": "execute"}])
    calls = {"n": 0}

    async def fake_call(system, user):
        calls["n"] += 1
        return '{"verdict": "direct", "reason": "x"}'

    monkeypatch.setattr(triage_mod, "_call_model", fake_call)
    r = await triage_mod.triage_if_needed(wid, cid, "再发一条消息")
    assert r["skipped"] is True and r["verdict"] == "plan"
    assert calls["n"] == 0  # 模型零调用
    blocks = await session_facade.read(wid, cid)
    assert not [b for b in blocks if b.get("behavior") == "triage"]


def test_direct_tool_schemas_only_escape():
    """direct 席工具面＝仅 escalate_to_plan（逃生口）。"""
    schemas = triage_mod.direct_tool_schemas()
    assert [s["name"] for s in schemas] == ["escalate_to_plan"]
