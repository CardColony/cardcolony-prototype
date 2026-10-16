# -*- coding: utf-8 -*-
"""卡片底座第一步 · 沿关系的数据访问与通信（V1–V5 验收网）。

覆盖任务书验收标准：
- V1 A 沿通信线能读 B 的正文／影子／对话；断线（unlink 后）read_via 返回「无通信线」；
- V2 read_via 未授 read_linked 时被 policy 拒（denied 信封）；
- V3 A 发消息 → 收件箱出现 status=pending；事件流照旧有 msg_* 事件；
- V4 B 回合被调起、模型在激励文里明确看到待处理消息正文、回复；
     回复后 msg_id 置 consumed（不删行）；
- V5 新消息不进对话块（session 无新 card: 块）；B 的 assistant 回复仍进对话块。

「复用不重做」的判据：读的三路各映射到现成读工具（read_card / read_shadow_raw /
event_log.timeline）——本文件只验「沿边寻址」这一步加上去之后的行为。
"""
from __future__ import annotations

import asyncio
import json

from src.api import card_tools, card_msg, message_runner, model_service
from src.storage import (card_db, card_store, event_log, session_facade,
                         work_store)
from conftest import prime_card_session


# ══════════════ 夹具 ══════════════

async def _tree(wid: str) -> dict:
    """根卡 card_001（create_work 自带）→ 002=子卡A（父001）→ 003=孙卡A1（父002）。"""
    await card_store.create_card(wid, "子卡A", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "孙卡A1", "用户直接创建",
                                 card_type="sub_project", parent="card_002")
    # 按生产形态铺会话（建卡即铺；测试直建卡不带）——见 conftest.prime_card_session
    for _cid in ("card_002", "card_003"):
        prime_card_session(wid, _cid)
    return card_store.iter_card_metas(wid)


async def _wait_for(predicate, timeout_s: float = 6.0) -> bool:
    for _ in range(int(timeout_s / 0.05)):
        await asyncio.sleep(0.05)
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
    return False


# ══════════════ V1 · 沿通信线读（正文／影子／对话）＋ 断线失效 ══════════════

async def test_v1_read_via_three_parts_and_unlink(sandbox):
    """A(card_002) 连线到 B(card_003) 后，三路可读；unlink 后返回「无通信线」。"""
    wid = (await work_store.create_work("V1沿读剧本"))["work_id"]
    cards = await _tree(wid)
    a_id, b_id = "card_002", "card_003"

    # 建立通信线 A → B（link_cards 需 link 权限 ＋ 目标在 A 子树内：003 是 002 的子）
    r = await card_tools._h_link_cards(
        wid, a_id, {"target_id": b_id, "type": "关联"}, cards)
    assert "error" not in r, r

    # 无连线前：未连的目标 → 无通信线（换一张没有边的卡验）
    out0 = await card_tools.read_via(wid, a_id, "card_004", "正文")
    assert out0["ok"] is False and "无通信线" in out0["message"]

    # ① 正文（复用 card_store.read_card 三块）
    out = await card_tools.read_via(wid, a_id, b_id, "正文")
    assert out["ok"] is True and out["part"] == "正文"
    assert set(out["value"]) == {"title", "mission", "status_block", "rulings"}

    # ② 影子（复用 shadow_store.read_shadow_raw）
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "B 的既有话", "end": "model_done"})
    out = await card_tools.read_via(wid, a_id, b_id, "影子")
    assert out["ok"] is True and isinstance(out["value"], str)

    # ③ 对话（复用 event_log.timeline）
    out = await card_tools.read_via(wid, a_id, b_id, "对话")
    assert out["ok"] is True and isinstance(out["value"], list)

    # part 非法 → unknown_part（不抛）
    out = await card_tools.read_via(wid, a_id, b_id, "天书")
    assert out["ok"] is False and out["reason"] == "unknown_part"

    # 断线：unlink 后 read_via 返回「无通信线」
    r = await card_tools._h_unlink_cards(wid, a_id, {"target_id": b_id}, cards)
    assert "error" not in r, r
    out = await card_tools.read_via(wid, a_id, b_id, "正文")
    assert out["ok"] is False and out["reason"] == "no_link"
    assert "无通信线" in out["message"]


# ══════════════ V2 · 未授 read_linked 被拒（denied 信封） ══════════════

async def test_v2_read_linked_denied_without_perm(sandbox):
    """目标卡 perms 不含 read_linked → handler 回 denied 信封（同现状口径）。"""
    wid = (await work_store.create_work("V2越权剧本"))["work_id"]
    cards = {
        "card_002": {"card_id": "card_002", "card_type": "sub_project",
                     "parent": "card_001", "perms": ["read"]},
        "card_003": {"card_id": "card_003", "card_type": "sub_project",
                     "parent": "card_002", "perms": ["read"]},
    }
    r = await card_tools._h_read_linked(
        wid, "card_002", {"target_id": "card_003", "part": "正文"}, cards)
    assert r["error"]["code"] == "denied", r


async def test_v2_read_linked_granted_reads(sandbox):
    """目标卡 perms 含 read_linked ＋ 有连线 → 读到（正例，走 handler 全链）。"""
    wid = (await work_store.create_work("V2正例剧本"))["work_id"]
    cards = await _tree(wid)
    await card_tools._h_link_cards(
        wid, "card_002", {"target_id": "card_003", "type": "关联"}, cards)
    r = await card_tools._h_read_linked(
        wid, "card_002", {"target_id": "card_003", "part": "正文"}, cards)
    assert "error" not in r, r
    assert r["part"] == "正文" and "title" in r["value"]


# ══════════════ V3/V4/V5 · 通信闭环端到端 ══════════════

async def test_v3_v4_v5_loop_end_to_end(sandbox, monkeypatch):
    """A 发信 → 收件箱 pending ＋ msg_* 事件（V3）→ B 被调起、激励文带正文、
    回复、置 consumed（V4）→ 新消息不进对话块、B 回复进对话块（V5）。"""
    wid = (await work_store.create_work("V345闭环剧本"))["work_id"]
    await _tree(wid)
    a_id, b_id = "card_002", "card_003"
    captured: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **_):
        if "请阅读这些消息并处理" in str(user_prompt):
            captured.append(str(user_prompt))
            return ({"content": "B 的回答：第二节数字为 42。", "tool_calls": []}, "fake")
        return ({"content": "A 侧回复。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    # A 沿关系发消息
    r = await card_tools.send_card_message(
        wid, a_id, target_id=b_id, msg_type="msg_ask", content="第二节数字是多少？")
    msg_id = r["msg_id"]

    # V3：收件箱出现 status=pending
    pending = await card_db.list_pending(wid, b_id)
    assert len(pending) == 1
    assert pending[0]["msg_id"] == msg_id and pending[0]["status"] == "pending"
    # V3：事件流照旧有 msg_* 事件（收件方键）
    evs = [e for e in event_log.read(wid, b_id) if e.get("type") == "msg_ask"]
    assert evs, "事件流应照旧留痕 msg_*"
    assert msg_id in json.dumps(evs[-1], ensure_ascii=False)
    # V5：新消息不进对话块
    assert await session_facade.read(wid, b_id) == []

    # 触发 B 的回合（A 的回合收口）
    await message_runner.schedule_card_message_triggers(
        message_runner.RunState(work_id=wid, card_id=a_id, content="x"))

    async def _b_replied():
        return any(b.get("actor") == "assistant"
                   for b in await session_facade.read(wid, b_id))

    assert await _wait_for(_b_replied), "B 应被调起并回复"

    # V4：模型在激励文里明确看到待处理消息正文
    assert captured, "B 的回合应带激励文"
    assert "第二节数字是多少？" in captured[0], captured[0][:200]
    assert "请阅读这些消息并处理" in captured[0]

    # V4：msg_id 置 consumed（状态置位，不删行）
    rows = await card_db.run_read(
        wid, lambda c: [dict(x) for x in c.execute(
            "SELECT * FROM inbox WHERE msg_id = ?", (msg_id,)).fetchall()], [])
    assert rows, "consumed 是状态置位，行不删"
    assert rows[0]["status"] == "consumed" and rows[0]["consumed_at"]

    # V5：B 的 assistant 回复仍进对话块；对话块无 card: 块
    blocks_b = await session_facade.read(wid, b_id)
    assert any(b.get("actor") == "assistant"
               and "B 的回答" in str(b.get("content") or "") for b in blocks_b)
    assert not any(str(b.get("actor") or "").startswith("card:") for b in blocks_b)


# ══════════════ 工具面 · read_linked 注册与开关 ══════════════

def test_read_linked_tool_registered_and_readonly():
    assert "read_linked" in card_tools.TOOL_REGISTRY
    assert "read_linked" in card_tools.READONLY_TOOLS
    assert card_tools.TOOL_SWITCH["read_linked"] == "read_linked"
    # [编程卡 2026-10-15] tc-4 → tc-5（新增 report_done / create_coder_card 两件）。
    # [规则卡 2026-10-15] tc-5 → tc-6（新增 draft_rule_edit ＋第四段根卡闸）。
    # [依赖关联线 2026-10-16] tc-6 → tc-7（新增 link_rel / unlink_rel 两件，
    # 依赖关联线；通信线 relations→links 改名）。
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-8"
