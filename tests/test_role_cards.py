# -*- coding: utf-8 -*-
"""[角色机制 5710 v2] 独立角色卡结构：档案/六节/信号同步/判词/警报更正/值班态。"""
from __future__ import annotations

import asyncio

import pytest

from src.api import card_tools, prompts, role_room
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

    # 同一排他域只允许一个角色
    dup = await card_tools._h_create_role(wid, root, {
        "title": "重复域角色",
        "charter": "重复域",
        "jurisdiction": {"domain": "timeline_contract", "statement": "重复"},
        "watch_card_ids": [b],
    }, cards)
    assert dup["ok"] is False
    assert "责任域" in str(dup["error"]["message"])

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
    assert "update_function_profile" not in role_tools
    assert "set_function_duty" not in role_tools
    assert "bash" not in role_tools and "write_file" not in role_tools

    work_tools = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=a, work_id=wid, cards=cards)}
    assert "record_verdict" not in work_tools
    assert "raise_alarm" not in work_tools
    assert "correct_alarm" not in work_tools
    assert "update_function_profile" not in work_tools
    assert "set_function_duty" not in work_tools

    root_tools = {t["name"] for t in card_tools.build_tool_schemas(
        card_id=root, work_id=wid, cards=cards)}
    assert {"create_function", "function_watch_cards", "list_functions",
            "update_function_profile", "set_function_duty"} <= root_tools
    # [card_031] 旧的五件 role 工具**已从模型工具面消失**（模型建的是职能、不是角色）
    assert not ({"create_role", "role_watch_cards", "list_roles",
                 "update_role_profile", "role_set_duty"} & root_tools)


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

    # 技术问题不能裸报警：缺决策字段直接拒
    r = await card_tools.execute_tool(wid, rid, "raise_alarm", {
        "severity": "warn", "title": "口径冲突", "detail": "合并会出错",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "validation"

    r = await card_tools.execute_tool(wid, rid, "raise_alarm", {
        "severity": "warn", "title": "口径冲突", "detail": "合并会出错",
        "question": "旧数据兼容 UTC 还是统一新口径？",
        "options": ["保留旧数据兼容", "统一新口径并迁移"],
        "recommendation": "统一新口径并迁移",
        "default_action": "按统一新口径执行，旧数据只读",
        "deadline": "2026-10-21T10:00:00+08:00",
    }, cards)
    assert r["ok"] is True
    alarm_id = r["data"]["alarm_id"]
    alarms = role_room.list_alarms(wid, "open", rid)
    assert len(alarms) == 1 and alarms[0]["severity"] == "warn"
    assert alarms[0]["question"] == "旧数据兼容 UTC 还是统一新口径？"
    assert alarms[0]["default_action"] == "按统一新口径执行，旧数据只读"
    assert alarms[0]["options"] == ["保留旧数据兼容", "统一新口径并迁移"]
    assert await role_room.ack_alarm(wid, alarm_id) is True
    assert role_room.list_alarms(wid, "open", rid) == []

    # 更正：新警报 supersedes 旧警报，旧警报置 corrected
    r = await card_tools.execute_tool(wid, rid, "raise_alarm", {
        "severity": "info", "title": "更正", "detail": "上一条判据不足",
        "question": "是否接受按统一口径继续？",
        "default_action": "继续按统一口径执行",
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

    # [card_031] 旧 role 卡路径改直调私有 handler——与 routes.py 的
    # PATCH /works/{w}/roles/{role_id} 端点（1427-1444）同一条执行体；
    # 模型工具面已无 update_role_profile（改接 function_*），但旧路径必须仍可用。
    res = await card_tools._h_update_role_profile(wid, root, {
        "role_card_id": rid,
        "jurisdiction": {"domain": "timeline_contract", "statement": "时间口径 v2"},
        "watch_card_ids": [b],
        "signals": ["status_changed"],
        "charter": "新版职责书：只盯时间口径。",
    }, card_tools._collect_card_meta(wid))
    # [card_031] 私有 handler 直接返回档案字典（不走 execute_tool 的信封化），
    # 成功判据＝无 error——与 routes.py 端点的用法一致。
    assert res.get("ok") is not False and "error" not in res, res
    prof = await role_room.get_profile(wid, rid)
    assert prof["jurisdiction"]["statement"] == "时间口径 v2"
    assert prof["watch"]["cards"] == [b]
    assert prof["watch"]["signals"] == ["status_changed"]
    assert prof["charter"]["version"] == 2
    assert [m["card_id"] for m in await role_room.watched_cards(wid, rid)] == [b]
    hist = await role_store.charter_history(wid, rid)
    assert [h["version"] for h in hist] == [2, 1]


async def test_startup_sweep_resumes_pending_sync(work, monkeypatch):
    """重启续跑：pending 同步在库里，startup_sweep_all 能重新唤醒角色。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    await session_facade.append(wid, a, {"role": "assistant", "content": "重启前的新增。"})
    last = await role_room._last_seq(wid, a)
    await role_store.add_sync(wid, rid, a, 0, last, ["turn_completed"])
    assert [p["source"] for p in role_room.pending_batch(wid, rid)] == [a]

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, **kwargs):
        return ({"content": "", "tool_calls": [{
            "name": "record_verdict",
            "arguments": {"decision": "silent", "reason": "重启后无新增风险"},
        }]}, "fake")

    monkeypatch.setattr("src.api.model_service.call_main", fake_call_main)
    started = await role_room.startup_sweep_all()
    assert started >= 1
    recs = await _wait_verdicts(wid, rid)
    assert recs and recs[0]["decision"] == "silent"
    assert "重启后无新增风险" in recs[0]["reason"]


async def test_role_turn_can_advise_and_alarm(work, monkeypatch):
    """真实角色回合：模型依次发建议、发警报、落判词，再收口。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    await session_facade.append(wid, a, {"role": "assistant", "content": "A 的现状变了。"})
    calls = {"n": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": "", "tool_calls": [{
                "name": "send_card_message",
                "arguments": {"target_card_id": a, "msg_type": "msg_notify",
                              "content": "请补齐时间口径。"},
            }]}, "fake")
        if calls["n"] == 2:
            return ({"content": "", "tool_calls": [{
                "name": "raise_alarm",
                "arguments": {"severity": "warn", "title": "时间口径冲突",
                              "detail": "两卡互斥",
                              "question": "旧数据兼容还是统一新口径？",
                              "default_action": "统一新口径"},
            }]}, "fake")
        if calls["n"] == 3:
            return ({"content": "", "tool_calls": [{
                "name": "record_verdict",
                "arguments": {"decision": "speak", "reason": "两卡时间口径互斥",
                              "evidence": ["card_002#现状"]},
            }]}, "fake")
        return ({"content": "已建议并报警。", "tool_calls": []}, "fake")

    monkeypatch.setattr("src.api.model_service.call_main", fake_call_main)
    await role_room.after_turn(wid, a, {"reply": "ok"})
    recs = await _wait_verdicts(wid, rid)
    assert recs and recs[0]["decision"] == "speak"
    for _ in range(100):
        await asyncio.sleep(0.02)
        if await card_db.list_pending(wid, a):
            break
    assert len(await card_db.list_pending(wid, a)) == 1
    alarms = role_room.list_alarms(wid, "open", rid)
    assert alarms and alarms[0]["title"] == "时间口径冲突"


async def test_role_cannot_write_other_cards_even_by_direct_tool_call(work):
    """防御纵深：edit 是树作用域开关；角色离树 ⇒ 只能写自己。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(wid, rid, "update_status", {
        "card_id": a, "content": "角色越权改别人现状",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"

    r = await card_tools.execute_tool(wid, rid, "append_ruling", {
        "card_id": a, "text": "角色越权给别人立定论",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"

    r = await card_tools.execute_tool(wid, rid, "edit_card", {
        "card_id": a, "content": "## 使命\n越权",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"

    # 写自己可以
    r = await card_tools.execute_tool(wid, rid, "update_status", {
        "content": "本轮巡检：无新增矛盾。",
    }, cards)
    assert r["ok"] is True


async def test_directive_is_mandatory_and_verifiable(work):
    """角色可给关注卡下指令；指令是消息类型、落指令簿、必须给验收判据、可验收。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    b = await _mk_child(wid, root, "乙")
    rid = await _mk_role(wid, root, [a])
    cards = card_tools._collect_card_meta(wid)

    r = await card_tools.execute_tool(wid, rid, "issue_directive", {
        "target_card_id": a,
        "instruction": "把现状里的 UTC 带 Z 改成统一口径，并补一条契约测试",
        "acceptance": "现状写明统一口径；测试文件含该用例",
    }, cards)
    assert r["ok"] is True, r
    did = r["data"]["directive_id"]
    pending = await card_db.list_pending(wid, a)
    assert pending and pending[-1]["type"] == "msg_directive"

    summary = await role_room.role_summary(wid, rid)
    assert summary["directives"][0]["id"] == did
    assert summary["directives"][0]["status"] == "issued"
    assert "## 指令簿" in summary["text"]

    # 未关注卡：拒
    r = await card_tools.execute_tool(wid, rid, "issue_directive", {
        "target_card_id": b, "instruction": "x", "acceptance": "y",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "denied"

    # 缺验收判据：拒
    r = await card_tools.execute_tool(wid, rid, "issue_directive", {
        "target_card_id": a, "instruction": "x", "acceptance": "",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "validation"

    # 验收：打回必须写证据
    r = await card_tools.execute_tool(wid, rid, "verify_directive", {
        "directive_id": did, "status": "rejected",
    }, cards)
    assert r["ok"] is False and r["error"]["code"] == "validation"
    r = await card_tools.execute_tool(wid, rid, "verify_directive", {
        "directive_id": did, "status": "rejected", "evidence": "测试文件还没有该用例",
    }, cards)
    assert r["ok"] is True and r["data"].get("rework")
    summary = await role_room.role_summary(wid, rid)
    by_id = {d["id"]: d for d in summary["directives"]}
    assert by_id[did]["status"] == "rework"
    assert "测试文件还没有该用例" in by_id[did]["evidence"]
    new_id = r["data"]["rework"]["directive_id"]
    assert by_id[new_id]["status"] == "issued"
    assert by_id[new_id]["attempt"] == 2


async def test_directive_blocks_completion_and_reject_auto_rework(work):
    """指令闸：未验收指令阻止收口；打回自动返工并接续旧指令。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    await card_store.update_card(wid, a, {"auto_mode": 1}, "测试开自动")
    cards = card_tools._collect_card_meta(wid)

    # 无指令时先不设前提（定论闸/占位骨架的基线不属本用例）
    r = await card_tools.execute_tool(wid, rid, "issue_directive", {
        "target_card_id": a,
        "instruction": "把现状改成统一 UTC+Z",
        "acceptance": "现状含 UTC+Z，且无 naive 表述",
    }, cards)
    did = r["data"]["directive_id"]
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "update_goal", {
        "action": "complete", "reason": "done"}, cards)
    assert r["ok"] is False and "未验收的角色指令" in str(r["error"]["message"])

    r = await card_tools.execute_tool(wid, rid, "verify_directive", {
        "directive_id": did, "status": "rejected",
        "evidence": "现状仍是 naive 本地时间",
    }, cards)
    assert r["ok"] is True and r["data"].get("rework"), r
    summary = await role_room.role_summary(wid, rid)
    by_id = {d["id"]: d for d in summary["directives"]}
    assert by_id[did]["status"] == "rework"
    new_id = r["data"]["rework"]["directive_id"]
    assert by_id[new_id]["status"] == "issued"
    assert by_id[new_id]["attempt"] == 2

    # 新指令仍阻塞
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "update_goal", {
        "action": "complete", "reason": "done"}, cards)
    assert r["ok"] is False and "未验收的角色指令" in str(r["error"]["message"])

    # 达标后解除阻塞（后续可能被定论闸拦，但不再是指令闸）
    r = await card_tools.execute_tool(wid, rid, "verify_directive", {
        "directive_id": new_id, "status": "complied",
        "evidence": "现状已改 UTC+Z",
    }, cards)
    assert r["ok"] is True
    summary = await role_room.role_summary(wid, rid)
    assert summary["open_directives"] == []
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, a, "update_goal", {
        "action": "complete", "reason": "done"}, cards)
    assert r["ok"] is True or "未验收" not in str((r.get("error") or {}).get("message") or "")


async def test_overdue_directive_blocks_completion(work):
    """超时指令：due_at 过期后置 overdue，仍阻塞收口；达标记 complied 解除。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a])
    await card_store.update_card(wid, a, {"auto_mode": 1}, "测试开自动")
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools.execute_tool(wid, rid, "issue_directive", {
        "target_card_id": a,
        "instruction": "补契约测试",
        "acceptance": "测试文件含时间口径用例",
        "due_at": "2000-01-01T00:00:00+08:00",
    }, cards)
    assert r["ok"] is True
    did = r["data"]["directive_id"]
    summary = await role_room.role_summary(wid, rid)
    by_id = {d["id"]: d for d in summary["directives"]}
    assert by_id[did]["status"] == "overdue"
    r = await card_tools.execute_tool(wid, a, "update_goal", {
        "action": "complete", "reason": "done"}, cards)
    assert r["ok"] is False and "未验收的角色指令" in str(r["error"]["message"])

    r = await card_tools.execute_tool(wid, rid, "verify_directive", {
        "directive_id": did, "status": "complied", "evidence": "已补测试",
    }, cards)
    assert r["ok"] is True
    summary = await role_room.role_summary(wid, rid)
    assert summary["open_directives"] == []


def test_root_prompt_has_no_role_guidance():
    """冻结版：根卡 prompt 不写角色行为引导，角色信息只在工具/角色自身说明。"""
    assert prompts.select_base_prompt(
        {"card_type": "project", "auto_mode": 0}) == prompts.BASE_PROMPT
    assert prompts.select_base_prompt(
        {"card_type": "project", "auto_mode": 1}) == prompts.AUTO_BASE_PROMPT
    assert prompts.select_base_prompt(
        {"card_type": "sub_project", "auto_mode": 0}) == prompts.BASE_PROMPT
    role = prompts.select_base_prompt({"card_type": "role", "auto_mode": 0})
    assert role == prompts.ROLE_CARD_BASE_PROMPT
    assert "角色卡" in role


async def test_role_info_lives_in_tools_not_root_prompt(work):
    """根卡工具面有角色工具与说明；根卡 prompt 不规定组阁动作。"""
    wid, root = work["work_id"], work["work_card_id"]
    cards = card_tools._collect_card_meta(wid)
    tools = {t["name"]: t for t in card_tools.build_tool_schemas(
        card_id=root, work_id=wid, cards=cards)}
    for name in ("create_function", "list_functions", "function_watch_cards",
                 "update_function_profile", "set_function_duty"):
        assert name in tools, name
    desc = tools["create_function"]["description"]
    # 意图逐字保留：**职能不是卡**（不占号、不进清单、不挂任务树、不干活），
    # 只是随改名换了措辞——「不挂任务树」这一条由「不进卡片清单与任务树」承担。
    assert "职能" in desc and "不是卡" in desc
    assert "不占卡片号" in desc and "任务树" in desc and "不干活" in desc
    assert "第一动作" not in desc and "先组阁" not in desc


async def test_root_can_list_roles_before_staffing(work):
    """根卡组阁前可看已有角色/责任域，避免重复组阁。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk_child(wid, root, "甲")
    rid = await _mk_role(wid, root, [a], title="时间口径守门人")
    cards = card_tools._collect_card_meta(wid)
    # [card_031] 旧 role 清单：直调私有 handler（同 routes.py GET /roles 端点，
    # 1381-1389 走 role_room.list_roles）；模型工具面已改为 list_functions。
    r = await card_tools._h_list_roles(wid, root, {}, cards)
    assert r.get("ok") is not False, r
    roles = r["roles"]
    assert roles and roles[0]["role_card_id"] == rid
    assert roles[0]["domain"] == "timeline_contract"
    assert roles[0]["watch_cards"] == [a]
