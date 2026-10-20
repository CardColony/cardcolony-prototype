# -*- coding: utf-8 -*-
"""[角色机制 5710 v2] 独立角色卡结构：档案/六节/信号同步/判词/警报更正/值班态。"""
from __future__ import annotations

import asyncio

import pytest

from src.api import card_tools, role_room
from src.permissions import policy
from src.storage import card_db, card_store, role_store, session_facade


@pytest.fixture(autouse=True)
def _reset_role_module_state():
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()
    role_room._LOCKS.clear()
    yield
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()
    role_room._LOCKS.clear()


async def _mk_child(wid: str, root: str, title: str) -> str:
    r = await card_store.create_card(
        wid, title=title, source="用户直接创建", card_type="sub_project",
        parent=root, charter=f"## 使命\n{title}",
    )
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _mk_role(wid: str, root: str, watch: list[str], *,
                   title: str = "一致性观察员",
                   signals: list[str] | None = None) -> str:
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_role(wid, root, {
        "title": title,
        "charter": "盯住工作卡之间是否自相矛盾；发现矛盾先发建议，结构性矛盾发警报；无新增矛盾则沉默。",
        "jurisdiction": {"domain": "timeline_contract", "statement": "接口层时间口径"},
        "watch_card_ids": watch,
        "signals": signals or ["turn_completed"],
    }, cards)
    assert "error" not in r, r
    return r["role_card_id"]


async def _wait_verdicts(wid: str, rid: str, n: int = 1) -> list[dict]:
    for _ in range(300):
        await asyncio.sleep(0.02)
        recs = await role_room.list_verdicts(wid, rid)
        if len(recs) >= n:
            return recs
    return await role_room.list_verdicts(wid, rid)


async def test_create_role_has_independent_structure_and_is_off_tree(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    b = await _mk_child(wid, root, "乙")
    rid = await _mk_role(wid, root, [a])

    meta = card_store._meta_of(wid, rid)
    assert meta["card_type"] == "role"
    assert meta["parent"] == ""
    perms = set(meta[policy.PERM_KEY])
    assert {"read", "edit", "read_linked", "send_card_message", "shadow_write"} <= perms
    assert "create_child" not in perms and "delete" not in perms

    # 独立档案：不是普通卡三块
    prof = await role_room.get_profile(wid, rid)
    assert prof["jurisdiction"]["domain"] == "timeline_contract"
    assert prof["jurisdiction"]["statement"] == "接口层时间口径"
    assert prof["watch"]["signals"] == ["turn_completed"]
    assert prof["speak_contract"]["silence_default"] is True
    assert prof["duty"]["state"] == "active"
    assert prof["charter"]["version"] == 1
    hist = await role_store.charter_history(wid, rid)
    assert hist and hist[0]["version"] == 1

    summary = await role_room.role_summary(wid, rid)
    for token in ("## 管辖", "## 关注", "## 发言契约", "## 升级线",
                  "## 值班", "## 判断簿", "## 警报簿", "## 职责书"):
        assert token in summary["text"]

    assert [m["card_id"] for m in await role_room.watched_cards(wid, rid)] == [a]
    assert [m["card_id"] for m in await role_room.roles_watching(wid, a)] == [rid]
    assert await role_room.roles_watching(wid, b) == []

    bad = await card_store.create_card(
        wid, title="角色下挂子卡", source="用户直接创建", parent=rid)
    assert "error" in bad and "角色卡不可有子卡" in bad["error"]

    cards = card_tools._collect_card_meta(wid)
    denied = await card_tools._h_create_role(wid, a, {
        "title": "越权角色", "charter": "x", "watch_card_ids": [],
    }, cards)
    assert denied["ok"] is False and denied["error"]["code"] == "denied"

    # 模型读角色卡：返回独立结构，不是三块
    r = await card_tools.execute_tool(wid, rid, "read_card",
                                      {"card_id": rid}, cards)
    assert r["ok"] is True
    assert r["data"]["card_type"] == "role"
    assert r["data"]["role"]["jurisdiction"]["domain"] == "timeline_contract"
    assert "## 管辖" in r["data"]["text"]


async def test_role_toolface_is_narrow_and_lifecycle_is_root_only(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    cards = card_tools._collect_card_meta(wid)

    role_tools = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=rid, work_id=wid, cards=cards)}
    assert {"record_verdict", "raise_alarm", "correct_alarm",
            "send_card_message", "read_card", "read_linked"} <= role_tools
    assert "edit_card" not in role_tools
    assert "create_card" not in role_tools
    assert "update_role_profile" not in role_tools
    assert "role_set_duty" not in role_tools
    assert "bash" not in role_tools and "write_file" not in role_tools

    work_tools = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=a, work_id=wid, cards=cards)}
    assert "record_verdict" not in work_tools
    assert "raise_alarm" not in work_tools
    assert "correct_alarm" not in work_tools
    assert "update_role_profile" not in work_tools
    assert "role_set_duty" not in work_tools

    root_tools = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=root, work_id=wid, cards=cards)}
    assert {"create_role", "role_watch_cards",
            "update_role_profile", "role_set_duty"} <= root_tools


async def test_verdict_alarm_correction_and_linked_message(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    b = await _mk_child(wid, root, "乙")
    rid = await _mk_role(wid, root, [a])
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(wid, rid, "record_verdict", {
        "decision": "speak", "reason": "甲的现状与乙冲突",
        "targets": [a], "evidence": ["甲#现状", "乙#现状"],
    }, cards)
    assert r["ok"] is True
    recs = await role_room.list_verdicts(wid, rid)
    assert recs[0]["decision"] == "speak"
    assert recs[0]["evidence"] == ["甲#现状", "乙#现状"]

    r = await card_tools.execute_tool(wid, rid, "raise_alarm", {
        "severity": "warn", "title": "口径冲突", "detail": "合并会出错",
    }, cards)
    assert r["ok"] is True
    alarm_id = r["data"]["alarm_id"]
    alarms = role_room.list_alarms(wid, "open", rid)
    assert len(alarms) == 1 and alarms[0]["severity"] == "warn"
    assert await role_room.ack_alarm(wid, alarm_id) is True
    assert role_room.list_alarms(wid, "open", rid) == []

    # 更正：新警报 supersedes 旧警报，旧警报置 corrected
    r = await card_tools.execute_tool(wid, rid, "raise_alarm", {
        "severity": "info", "title": "更正", "detail": "上一条判据不足",
        "supersedes": alarm_id,
    }, cards)
    assert r["ok"] is True
    all_alarms = role_room.list_alarms(wid, "", rid)
    old = [x for x in all_alarms if x["id"] == alarm_id][0]
    assert old["status"] == "corrected"
    assert old["corrected_by"] == r["data"]["alarm_id"]

    # 角色 → 已关注工作卡：允许；未订阅 → 拒
    r = await card_tools.execute_tool(wid, rid, "send_card_message", {
        "target_card_id": a, "msg_type": "msg_notify", "content": "请统一口径",
    }, cards)
    assert r["ok"] is True
    assert len(await card_db.list_pending(wid, a)) == 1
    r = await card_tools.execute_tool(wid, b, "send_card_message", {
        "target_card_id": rid, "msg_type": "msg_notify", "content": "越权",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"


async def test_after_turn_syncs_by_signal_and_advances_cursor(work, monkeypatch):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a], signals=["status_changed"])

    await session_facade.append(wid, a, {"role": "assistant", "content": "只有对话，无现状变化。"})
    await role_room.after_turn(wid, a, {"reply": "ok"})
    assert role_room.pending_batch(wid, rid) == []

    await card_store.update_card(wid, a, {"status_block": "接口返回 UTC。"}, "A 现状")
    await role_room.after_turn(wid, a, {"reply": "ok"})
    assert [p["source"] for p in role_room.pending_batch(wid, rid)] == [a]

    calls: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, **kwargs):
        calls.append(str(user_prompt))
        if len(calls) == 1:
            return ({"content": "", "tool_calls": [{
                "name": "record_verdict",
                "arguments": {"decision": "speak", "reason": "A 用 UTC",
                              "evidence": ["card_002#现状"]},
            }]}, "fake")
        return ({"content": "已记录。", "tool_calls": []}, "fake")

    monkeypatch.setattr("src.api.model_service.call_main", fake_call_main)
    await role_room.after_turn(wid, a, {"reply": "ok"})
    recs = await _wait_verdicts(wid, rid)
    assert recs and recs[0]["decision"] == "speak"
    assert recs[0]["sources"] == [a]
    assert any("接口返回 UTC" in c for c in calls), "同步材料必须进角色上下文"
    for _ in range(100):
        await asyncio.sleep(0.02)
        if role_room.pending_batch(wid, rid) == []:
            break
    assert role_room.pending_batch(wid, rid) == []
    last = await role_room._last_seq(wid, a)
    assert role_store.last_consumed_seq(wid, rid, a) >= last


async def test_silence_is_mechanically_recorded(work, monkeypatch):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    await session_facade.append(wid, a, {"role": "assistant", "content": "一切正常。"})

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, **kwargs):
        return ({"content": "（无需干预）", "tool_calls": []}, "fake")

    monkeypatch.setattr("src.api.model_service.call_main", fake_call_main)
    await role_room.after_turn(wid, a, {"reply": "ok"})
    recs = await _wait_verdicts(wid, rid)
    assert recs and recs[0]["auto"] is True
    assert "机械补记" in recs[0]["reason"]


async def test_duty_suspend_resume_archive(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])

    await role_room.set_duty(wid, rid, "suspended", "测试")
    await session_facade.append(wid, a, {"role": "assistant", "content": "停职期间的新增。"})
    await role_room.after_turn(wid, a, {"reply": "ok"})
    assert role_room.pending_batch(wid, rid) == []

    await role_room.set_duty(wid, rid, "active", "测试")
    await role_room.after_turn(wid, a, {"reply": "ok"})
    assert [p["source"] for p in role_room.pending_batch(wid, rid)] == [a]

    await role_room.set_duty(wid, rid, "archived", "测试")
    assert await role_room.watched_cards(wid, rid) == []
    prof = await role_room.get_profile(wid, rid)
    assert prof["duty"]["state"] == "archived"
    # 撤岗保留判断/警报台账
    assert prof["charter"]["version"] == 1


async def test_profile_update_versions_charter_and_rewires_watch(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    b = await _mk_child(wid, root, "乙")
    rid = await _mk_role(wid, root, [a])

    res = await card_tools.execute_tool(wid, root, "update_role_profile", {
        "role_card_id": rid,
        "jurisdiction": {"domain": "timeline_contract", "statement": "时间口径 v2"},
        "watch_card_ids": [b],
        "signals": ["status_changed"],
        "charter": "新版职责书：只盯时间口径。",
    }, card_tools._collect_card_meta(wid))
    assert res["ok"] is True
    prof = await role_room.get_profile(wid, rid)
    assert prof["jurisdiction"]["statement"] == "时间口径 v2"
    assert prof["watch"]["cards"] == [b]
    assert prof["watch"]["signals"] == ["status_changed"]
    assert prof["charter"]["version"] == 2
    assert [m["card_id"] for m in await role_room.watched_cards(wid, rid)] == [b]
    hist = await role_store.charter_history(wid, rid)
    assert [h["version"] for h in hist] == [2, 1]
