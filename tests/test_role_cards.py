# -*- coding: utf-8 -*-
"""[角色机制 5710] 角色卡：离树、订阅同步、判词台账、沉默落账、警报、窄工具面。"""
from __future__ import annotations

import asyncio

import pytest

from src.api import card_tools, role_room
from src.permissions import policy
from src.storage import card_db, card_store, session_facade


@pytest.fixture(autouse=True)
def _reset_role_module_state():
    """角色模块有进程内节流/活跃位；用例之间同 work_id/role_id，必须清干净。"""
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


async def _mk_role(wid: str, root: str, watch: list[str], title: str = "一致性观察员") -> str:
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_role(wid, root, {
        "title": title,
        "charter": "盯住工作卡之间是否自相矛盾；发现矛盾先发建议，结构性矛盾发警报；无新增矛盾则沉默。",
        "watch_card_ids": watch,
    }, cards)
    assert "error" not in r, r
    return r["role_card_id"]


async def test_create_role_is_off_tree_and_subscribed(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    b = await _mk_child(wid, root, "乙")
    rid = await _mk_role(wid, root, [a])

    meta = card_store._meta_of(wid, rid)
    assert meta["card_type"] == "role"
    assert meta["parent"] == ""                      # 离树
    perms = set(meta[policy.PERM_KEY])
    assert {"read", "edit", "read_linked", "send_card_message", "shadow_write"} <= perms
    assert "create_child" not in perms and "delete" not in perms

    assert [m["card_id"] for m in role_room.watched_cards(wid, rid)] == [a]
    assert [m["card_id"] for m in role_room.roles_watching(wid, a)] == [rid]
    assert role_room.roles_watching(wid, b) == []
    # 角色卡不可有子卡（离树；挂子卡会被写门拒绝）
    bad = await card_store.create_card(
        wid, title="角色下挂子卡", source="用户直接创建", parent=rid)
    assert "error" in bad and "角色卡不可有子卡" in bad["error"]
    # 组阁是根卡专属
    cards = card_tools._collect_card_meta(wid)
    denied = await card_tools._h_create_role(wid, a, {
        "title": "越权角色", "charter": "x", "watch_card_ids": [],
    }, cards)
    assert denied["ok"] is False and denied["error"]["code"] == "denied"


async def test_role_toolface_is_narrow(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    cards = card_tools._collect_card_meta(wid)

    role_tools = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=rid, work_id=wid, cards=cards)}
    assert {"record_verdict", "raise_alarm", "send_card_message",
            "read_card", "read_linked"} <= role_tools
    assert "edit_card" not in role_tools
    assert "create_card" not in role_tools
    assert "bash" not in role_tools and "write_file" not in role_tools

    work_tools = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=a, work_id=wid, cards=cards)}
    assert "record_verdict" not in work_tools and "raise_alarm" not in work_tools


async def test_role_verdict_alarm_and_linked_message(work):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    b = await _mk_child(wid, root, "乙")
    rid = await _mk_role(wid, root, [a])
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(wid, rid, "record_verdict", {
        "decision": "speak", "reason": "甲的现状与乙冲突", "targets": [a],
    }, cards)
    assert r["ok"] is True
    recs = role_room.list_verdicts(wid, rid)
    assert recs[0]["decision"] == "speak" and recs[0]["reason"] == "甲的现状与乙冲突"

    r = await card_tools.execute_tool(wid, rid, "raise_alarm", {
        "severity": "critical", "title": "口径冲突", "detail": "合并会出错",
    }, cards)
    assert r["ok"] is True
    alarms = role_room.list_alarms(wid, "open")
    assert len(alarms) == 1 and alarms[0]["severity"] == "critical"
    assert role_room.ack_alarm(wid, rid, alarms[0]["id"]) is True
    assert role_room.list_alarms(wid, "open") == []

    # 角色 → 已关注的工作卡：允许（订阅边）
    r = await card_tools.execute_tool(wid, rid, "send_card_message", {
        "target_card_id": a, "msg_type": "msg_notify", "content": "请统一口径",
    }, cards)
    assert r["ok"] is True
    assert len(await card_db.list_pending(wid, a)) == 1
    # 未订阅的卡 → 角色：拒
    r = await card_tools.execute_tool(wid, b, "send_card_message", {
        "target_card_id": rid, "msg_type": "msg_notify", "content": "越权",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"


async def test_after_turn_syncs_delta_and_advances_cursor(work, monkeypatch):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])

    # 工作卡 A 的一轮（两块的增量）
    await session_facade.append(wid, a, {"role": "user", "content": "开始"})
    await session_facade.append(wid, a, {"role": "assistant", "content": "接口返回 UTC。"})

    calls: list[str] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, **kwargs):
        calls.append(str(user_prompt))
        if len(calls) == 1:
            return ({"content": "", "tool_calls": [{
                "name": "record_verdict",
                "arguments": {"decision": "speak", "reason": "A 用 UTC，需与 B 对齐",
                              "targets": [a]},
            }]}, "fake")
        return ({"content": "已记录。", "tool_calls": []}, "fake")

    monkeypatch.setattr("src.api.model_service.call_main", fake_call_main)
    await role_room.after_turn(wid, a, {"reply": "ok"})

    for _ in range(200):
        await asyncio.sleep(0.03)
        if role_room.list_verdicts(wid, rid):
            break
    await asyncio.sleep(0.2)
    recs = role_room.list_verdicts(wid, rid)
    assert recs and recs[0]["decision"] == "speak"
    assert recs[0]["sources"] == [a]
    assert role_room._cursor(wid, rid)[a] >= 1
    assert any("接口返回 UTC" in c for c in calls), "同步材料必须进角色上下文"


async def test_silence_is_mechanically_recorded(work, monkeypatch):
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    await session_facade.append(wid, a, {"role": "user", "content": "普通进展"})
    await session_facade.append(wid, a, {"role": "assistant", "content": "一切正常。"})

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, **kwargs):
        return ({"content": "（无需干预）", "tool_calls": []}, "fake")

    monkeypatch.setattr("src.api.model_service.call_main", fake_call_main)
    await role_room.after_turn(wid, a, {"reply": "ok"})
    for _ in range(200):
        await asyncio.sleep(0.03)
        if role_room.list_verdicts(wid, rid):
            break
    recs = role_room.list_verdicts(wid, rid)
    assert recs and recs[0]["auto"] is True
    assert "机械补记" in recs[0]["reason"]
