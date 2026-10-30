# -*- coding: utf-8 -*-
"""[harness 第 1 步 2026-10-28] 事件字段契约的**运行时**验收。

`test_event_catalog_coverage` 管的是「契约写得对不对」（静态），本文件管
「真跑起来带没带」（动态）——两半缺一不可：契约写得再全，产生点不带也是空。

三条：
  D1 真跑一个回合，本轮产出的每条事件都必须带齐自己的 `fields.part`；
  D2 卡间消息一条事件带全 发件人/收件方/类型/消息 id（改前被劈成两条且都不可用）；
  D3 系统提醒带 目标卡/有没有唤醒；总裁汇报带 kind。
"""
from __future__ import annotations

import pytest

from src.api import card_tools, message_runner, model_service, routes
from src.infra import event_hub, harness, harness_rules
from src.storage import card_db, card_store, event_log, session_facade, work_store


# ---------------------------------------------------------------- 公共
async def _settle():
    """等事件落盘（emit 走后台 append 时用例会读到半截）。"""
    await event_log.drain()


def _violations(events: list[dict]) -> list[str]:
    """逐条事件对契约，返回缺失零件清单。"""
    out: list[str] = []
    for e in events:
        t = str(e.get("type") or "")
        if not t or t not in event_hub.load_catalog():
            continue
        miss = event_hub.missing_parts(t, e.get("data"))
        if miss:
            out.append(f"{t}: 缺 {miss} → data={sorted((e.get('data') or {}).keys())}")
    return out


async def _seed_card(title="契约对账") -> tuple[str, str]:
    w = await work_store.create_work(title)
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(wid, "作业卡", "模型生成",
                                        card_type="sub_project",
                                        parent=root))["meta"]["card_id"]
    return wid, sub


# ---------------------------------------------------------------- D1
async def test_real_turn_events_satisfy_contract(sandbox, monkeypatch):
    """真跑一个回合：本轮产出的每条事件都要带齐自己的零件。

    这条是「报备」的实质——不是登记个名字就算报备，是**报备它必须带什么**，
    而且真跑起来必须真带上。
    """
    async def _fake_call_main(system_prompt, user_prompt, prior_messages=None,
                              tools=None, **_):
        return ({"content": "干完了。", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", _fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    wid, cid = await _seed_card()
    await routes._run_message(wid, cid, "把这件事做了")
    await _settle()

    events = event_log.read(wid, cid) or []
    assert events, "回合没产出任何事件"
    bad = _violations(events)
    assert not bad, "以下事件缺契约零件：\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------- D2
async def test_card_message_event_carries_all_parts(sandbox):
    """一条卡间消息＝一条事件带全五样，不再劈成两条。

    改前：`msg_*` 只有 content（没发件人），`card_message` 只有 from/type
    （没正文），且 `msg_*` 侧连 msg_id 都没有 ⇒ 两条事后 join 不起来。
    """
    wid, sub = await _seed_card()
    root = work_store.work_card_id_of(wid)
    await card_store.update_card(wid, sub, {"mission": "x"}, "init")

    rec = await session_facade.append_card_message(
        wid, sub, from_card=root, text="把这件事做了", msg_type="msg_directive")
    await _settle()

    evs = [e for e in (event_log.read(wid, sub) or [])
           if str(e.get("type")) == "msg_directive"]
    assert len(evs) == 1, f"一条消息应只落一条 msg_directive，实际 {len(evs)} 条"
    d = evs[0].get("data") or {}
    assert d.get("from") == root, f"发件人丢了: {d}"
    assert d.get("to") == sub, f"收件方丢了: {d}"
    assert d.get("msg_type") == "msg_directive"
    assert d.get("msg_id"), f"消息 id 丢了（两条消息将无法配对）: {d}"
    # 契约面复核
    assert not event_hub.missing_parts("msg_directive", d)


# ---------------------------------------------------------------- D3
async def test_harness_message_carries_target_and_wake(sandbox):
    """系统提醒要能回答「发给了谁」「有没有把人叫起来」。"""
    wid, sub = await _seed_card()
    mid = await harness.deliver(wid, sub, "你该汇报了", rule_id="r1", wake=False)
    await _settle()
    assert mid, "投递失败"

    evs = [e for e in (event_log.read(wid, sub) or [])
           if str(e.get("type")) == "harness_message"]
    assert evs, "没落 harness_message 事件"
    d = evs[-1].get("data") or {}
    assert d.get("to") == sub
    assert d.get("wake") is False
    assert d.get("rule_id") == "r1"


# ---------------------------------------------------------------- D4
# ---------------------------------------------------------------- D5
def test_missing_parts_detector_is_honest(sandbox):
    """检查器本身要能判出缺失（否则上面几条全是空转）。

    判据是「键在不在」，不是「值真假」——空串是合法零件值
    （"没带 kind"本身就是一条可判断的事实）。
    """
    assert event_hub.missing_parts("msg_sync", {}) == ["from", "to", "msg_type"]
    assert event_hub.missing_parts(
        "msg_sync", {"from": "", "to": "", "msg_type": ""}) == []
    assert event_hub.missing_parts("card_block_edited",
                                   {"card_type": "x", "block": "mission",
                                    "by": "a", "cause": "self"}) == []
