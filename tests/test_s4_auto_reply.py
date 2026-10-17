# -*- coding: utf-8 -*-
"""迭代五 S4 · 流程自动回发验收网（E5/E6/E7 单测层）。

覆盖：
- E5 主场景：ask→B 成功回合→A 档案收到 card:B 应答块（reply_to 对准原 ask id）
  ＋ops card_msg_answer_sent；
- E5 子场景：human_stop/error/被顶（block_overtaken）不回发（ops failed 带 reason）；
- E6：notify→B 被调起但不回发（A 无新块、无 answer_sent）；
- E7：假类型零代码实证（临时注册表加 msg_ping→激励文带出→B 收到不崩溃、不回发）；
- 快照边界：回合期间新到信（id 不在快照）不回发、留给下个收口；
- 防御分支：reply_type_missing / no_content / send_failed。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.api import card_msg, card_tools, message_runner, model_service
from src.storage import card_db, card_store, session_facade, session_ops, work_store
from conftest import prime_card_session

REPO = Path(__file__).resolve().parents[1]


async def _make_pair(wid: str) -> tuple[str, str]:
    """根卡 card_001（create_work 自带）→ A=002（父001）/ B=003（父002，
    父子链——兄弟互发被双向判定挡死，锚点 card_tools.py:1078-1084）。"""
    await card_store.create_card(wid, "A卡", "用户直接创建",
                                 card_type="sub_project", parent="card_001")
    await card_store.create_card(wid, "B卡", "用户直接创建",
                                 card_type="sub_project", parent="card_002")
    # 按生产形态铺会话（建卡即铺；测试直建卡不带）——见 conftest.prime_card_session
    for _cid in ("card_002", "card_003"):
        prime_card_session(wid, _cid)
    return "card_002", "card_003"


def _ops_text() -> str:
    return session_ops.ops_path().read_text(encoding="utf-8")


async def _wait_for(predicate, timeout_s: float = 6.0) -> bool:
    """轮询等待；谓词可为同步或 async 函数。"""
    for _ in range(int(timeout_s / 0.05)):
        await asyncio.sleep(0.05)
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
    return False


# ══════════════ E5 主场景：ask→answer 端到端（单测层·mock 模型） ══════════════

async def test_s4_ask_auto_reply_end_to_end(sandbox, monkeypatch):
    wid = (await work_store.create_work("S4应答剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        if "请阅读这些消息并处理" in str(user_prompt):
            return ({"content": "B 的回答：现状是 42。", "tool_calls": []}, "fake")
        return ({"content": "A 侧回复。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    r = await card_tools.send_card_message(wid, a_id, target_id=b_id,
                                           msg_type="msg_ask", content="现状是多少？")
    ask_msg_id = r["msg_id"]
    # A 跑一回合触发收口 → B 被调起 → 成功（dict）→ 自动回发
    await message_runner.schedule_card_message_triggers(
        message_runner.RunState(work_id=wid, card_id=a_id, content="x"))
    ok = await _wait_for(lambda: "card_msg_answer_sent" in _ops_text())
    assert ok, "B 成功回合后应自动回发（ops card_msg_answer_sent）"

    # [卡片底座第一步 2026-10-14] 应答也落**收件箱**（不再写对话块）
    pending_a = await card_db.list_pending(wid, a_id)
    assert pending_a, "A 收件箱应出现应答"
    hdr = card_msg.parse_header(str(pending_a[-1].get("content") or ""))
    assert hdr and hdr["type"] == "msg_answer" and hdr["reply_to"] == ask_msg_id, hdr
    assert "B 的回答" in card_msg.strip_header(str(pending_a[-1].get("content") or ""))
    # A 的对话块不因来信/应答而增长
    assert not any(str(b.get("actor") or "").startswith("card:")
                   for b in await session_facade.read(wid, a_id))


# ══════════════ E5 子场景：被停/出错/被顶回合不回发 ══════════════

async def _prep_letter(wid: str, a_id: str, b_id: str, msg_type: str = "msg_ask") -> str:
    """直落一封信到 B 的**待处理收件箱**（不经薄壳——避免 TRIGGERS 副作用），
    返回 msg_id。

    [卡片底座第一步 2026-10-14] 来信数据源已从「对话块」改为「收件箱」——
    本 helper 随之改落收件箱（调起读收件箱）。
    """
    msg_id = card_msg.make_msg_id(a_id)
    await card_db.append_inbox(
        wid, msg_id=msg_id, target_card_id=b_id, from_card=a_id, type=msg_type,
        content=card_msg.build_header(msg_id, msg_type, a_id) + "\n信件正文")
    return msg_id


def _letter(msg_id: str, from_card: str = "card_002",
            msg_type: str = "msg_ask", body: str = "信件正文") -> dict:
    """构造一条收件箱记录形态的来信（``_auto_reply_pending_letters`` 的入参）。"""
    return {
        "msg_id": msg_id, "from_card": from_card, "type": msg_type,
        "content": card_msg.build_header(msg_id, msg_type, from_card) + "\n" + body,
    }


async def test_s4_human_stop_no_reply(sandbox, monkeypatch):
    """end=human_stop → failed(end_not_model_done)、不回发。"""
    wid = (await work_store.create_work("S4人停剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    await _prep_letter(wid, a_id, b_id)
    # B 档案最后一块＝被停回合的 assistant 块（互斥体已释放后的盘面）
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "写到一半被停",
        "model": "x", "end": "human_stop"})
    await message_runner._auto_reply_pending_letters(wid, b_id, [_letter("m-1")])
    assert "card_msg_answer_failed" in _ops_text()
    assert "end_not_model_done" in _ops_text()
    blocks_a = await session_facade.read(wid, a_id)
    assert not any(str(b.get("actor") or "").startswith("card:") for b in blocks_a)


async def test_s4_error_turn_no_reply(sandbox, monkeypatch):
    """end=error → failed(end_not_model_done)、不回发。"""
    wid = (await work_store.create_work("S4出错剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    await _prep_letter(wid, a_id, b_id)
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "崩了", "model": "x", "end": "error"})
    await message_runner._auto_reply_pending_letters(wid, b_id, [_letter("m-1")])
    assert "end_not_model_done" in _ops_text()


async def test_s4_block_overtaken_no_reply(sandbox):
    """最后一块非 assistant（竞态被顶掉）→ failed(block_overtaken)、不回发。"""
    wid = (await work_store.create_work("S4被顶剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    await _prep_letter(wid, a_id, b_id)
    # 模拟竞态：本回合 assistant 块之后又插入了新块（顶掉最后位）
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "正常回答", "model": "x", "end": "model_done"})
    await session_facade.append(wid, b_id, {
        "actor": f"card:{a_id}", "from": a_id,
        "content": card_msg.build_header("m-new", "msg_ask", a_id) + "\n回合中新信",
        "behavior": "", "end": ""})
    await message_runner._auto_reply_pending_letters(wid, b_id, [_letter("m-old")])
    assert "block_overtaken" in _ops_text()


async def test_s4_502_status_no_reply(sandbox, monkeypatch):
    """回合失败（_run_message 返回 502 响应对象）→ failed(non_success_status)、不回发。"""
    wid = (await work_store.create_work("S4五零二剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)

    async def boom(*a, **kw):
        raise RuntimeError("模型调用失败（模拟坏 api_key）")

    monkeypatch.setattr(model_service, "call_main", boom)
    monkeypatch.setenv("MOCK_LLM", "1")
    await _prep_letter(wid, a_id, b_id)
    card_msg.TRIGGERS.setdefault((wid, b_id), []).append("m-1")
    await message_runner.schedule_card_message_triggers(
        message_runner.RunState(work_id=wid, card_id=a_id, content="x"))
    ok = await _wait_for(lambda: "non_success_status" in _ops_text())
    assert ok, "502 回合应打 failed(non_success_status)"
    blocks_a = await session_facade.read(wid, a_id)
    assert not any(str(b.get("actor") or "").startswith("card:") for b in blocks_a)


# ══════════════ E6：notify→B 被调起但不回发 ══════════════

async def test_s4_notify_no_reply(sandbox, monkeypatch):
    wid = (await work_store.create_work("S4通知剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        if "请阅读这些消息并处理" in str(user_prompt):
            return ({"content": "B 收下了通知。", "tool_calls": []}, "fake")
        return ({"content": "A 侧回复。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")
    await card_tools.send_card_message(wid, a_id, target_id=b_id,
                                       msg_type="msg_notify", content="告知一件事")
    await message_runner.schedule_card_message_triggers(
        message_runner.RunState(work_id=wid, card_id=a_id, content="x"))

    async def _b_replied():
        return any(b.get("actor") == "assistant"
                   for b in await session_facade.read(wid, b_id))

    # B 确实被调起
    ok = await _wait_for(_b_replied)
    assert ok, "B 应被调起"
    # 但不回发：A 无新卡间块、无 answer_sent
    await asyncio.sleep(0.3)
    blocks_a = await session_facade.read(wid, a_id)
    assert not any(str(b.get("actor") or "").startswith("card:") for b in blocks_a)
    assert "card_msg_answer_sent" not in _ops_text()


# ══════════════ E7：假类型零代码实证 ══════════════

async def test_s7_fake_type_zero_code(sandbox, monkeypatch, tmp_path):
    """注册表临时加 msg_ping（note+reply_expected=false）→ 零代码 →
    激励文带出 display/note → B 收到不崩溃、不回发。"""
    ext = tmp_path / "registry_with_ping.json"
    base = json.loads((REPO / "config" / "message_type_registry.json")
                      .read_text(encoding="utf-8"))
    base["types"]["msg_ping"] = {
        "display": "探针", "note": "收下即可不回应。",
        "when_to_use": "测试假类型。", "reply_expected": False, "reply_type": ""}
    ext.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", ext)

    wid = (await work_store.create_work("S7假类型剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    captured: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):
        if "请阅读这些消息并处理" in str(user_prompt):
            captured.append(str(user_prompt))
            return ({"content": "B 收到假类型消息。", "tool_calls": []}, "fake")
        return ({"content": "A 侧回复。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")
    r = await card_tools.send_card_message(wid, a_id, target_id=b_id,
                                           msg_type="msg_ping", content="ping")
    assert "msg_id" in r, f"假类型应可发（type_codes 现读）：{r}"
    await message_runner.schedule_card_message_triggers(
        message_runner.RunState(work_id=wid, card_id=a_id, content="x"))

    async def _b_replied_ping():
        return any(b.get("actor") == "assistant"
                   for b in await session_facade.read(wid, b_id))

    ok = await _wait_for(_b_replied_ping)
    assert ok, "B 收到假类型消息应正常调起不崩溃"
    assert captured and "探针" in captured[0] and "收下即可不回应" in captured[0]
    assert "card_msg_answer_sent" not in _ops_text(), "reply_expected=false 不回发"


# ══════════════ 快照边界与防御分支 ══════════════

async def test_s4_snapshot_boundary_new_letter_not_replied(sandbox, monkeypatch):
    """回合期间新到信（不在调起前快照内）→ 不回发、留给下个收口。

    [卡片底座第一步 2026-10-14] 快照＝调起前取的**收件箱 pending 记录**；
    本用例把信落进收件箱但不放进快照（模拟「回合期间才到」）→ 不回发。
    """
    wid = (await work_store.create_work("S4快照边界剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    await _prep_letter(wid, a_id, b_id, msg_type="msg_ask")   # 信在 B 收件箱
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "最终回答", "model": "x", "end": "model_done"})
    # 快照为空（信不在调起前快照内）→ 不回发
    await message_runner._auto_reply_pending_letters(wid, b_id, [])
    assert "card_msg_answer_sent" not in _ops_text()
    assert (await card_db.list_pending(wid, a_id)) == []


async def test_s4_reply_type_missing_guard(sandbox, monkeypatch, tmp_path):
    """reply_expected=true 但 reply_type 空串 → failed(reply_type_missing)、不猜不发。"""
    ext = tmp_path / "registry_missing_reply_type.json"
    base = json.loads((REPO / "config" / "message_type_registry.json")
                      .read_text(encoding="utf-8"))
    base["types"]["msg_broken"] = {
        "display": "坏类型", "note": "n", "when_to_use": "w",
        "reply_expected": True, "reply_type": ""}
    ext.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(card_msg, "REGISTRY_PATH", ext)

    wid = (await work_store.create_work("S4缺类型剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    # 信类型＝msg_broken（monkeypatch 后注册表在册、六道门槛③通过；
    # reply_expected=true 但 reply_type 空串）
    msg_id = await _prep_letter(wid, a_id, b_id, msg_type="msg_broken")
    # 成功回合的 assistant 块（取块判据的盘面前提）
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "最终回答", "model": "x", "end": "model_done"})
    await message_runner._auto_reply_pending_letters(
        wid, b_id, [_letter(msg_id, msg_type="msg_broken")])
    assert "reply_type_missing" in _ops_text()


# ══════════════ G3 双回发修复（2089947 · BUG-AM-002） ══════════════
# 背景：G2 把发信交回 auto 卡（自主通信）后，同一问会走两条应答通道——
# ① 模型自己发 msg_answer（带 reply_to）；② 流程 _auto_reply_pending_letters
# 自动转发——A 收到两条重复应答。修法：转发前查账本「已答？」，只减不加。

async def test_g3_model_already_answered_no_duplicate_reply(sandbox, monkeypatch):
    """硬判据1：模型本轮已自己答过这封信 ⇒ 流程不再重复自动转发。"""
    wid = (await work_store.create_work("G3双回发剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    msg_id = await _prep_letter(wid, a_id, b_id)
    # B 档案最后一块＝正常交付的 assistant 块（满足回发的前置）
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "我自己答：应该先做甲。",
        "model": "x", "end": "model_done"})
    # 模型自己答过（落账本：from=b、type=msg_answer、reply_to=来信）
    await card_tools.send_card_message(
        wid, b_id, target_id=a_id, msg_type="msg_answer",
        content="我自己答：应该先做甲。", reply_to=msg_id)
    before = await card_db.list_inbox(wid, a_id)

    await message_runner._auto_reply_pending_letters(wid, b_id, [_letter(msg_id)])

    after = await card_db.list_inbox(wid, a_id)
    b_answers = [r for r in after
                 if str(r.get("from_card")) == b_id
                 and str(r.get("type")) == "msg_answer"]
    assert len(b_answers) == 1, f"应只有模型自己那一条应答，实得 {len(b_answers)}"
    assert len(after) == len(before), "流程不应再落一条转发"
    # 硬判据3：跳过打点可观测
    assert "card_msg_answer_skipped" in _ops_text()
    assert "model_already_answered" in _ops_text()


async def test_g3_model_not_answered_auto_reply_still_works(sandbox, monkeypatch):
    """硬判据2（只减不加的镜像）：模型没自己答 ⇒ 自动转发原样工作。"""
    wid = (await work_store.create_work("G3未自答剧本"))["work_id"]
    a_id, b_id = await _make_pair(wid)
    msg_id = await _prep_letter(wid, a_id, b_id)
    await session_facade.append(wid, b_id, {
        "role": "assistant", "content": "我只会说结论，不会自己发信。",
        "model": "x", "end": "model_done"})

    await message_runner._auto_reply_pending_letters(wid, b_id, [_letter(msg_id)])

    rows = await card_db.list_inbox(wid, a_id)
    b_answers = [r for r in rows
                 if str(r.get("from_card")) == b_id
                 and str(r.get("type")) == "msg_answer"]
    assert len(b_answers) == 1, "未自答时流程应照旧自动转发一条"
    assert str(b_answers[0].get("reply_to") or "") == msg_id
    assert "card_msg_answer_sent" in _ops_text()
    assert "card_msg_answer_skipped" not in _ops_text()
