# -*- coding: utf-8 -*-
"""[投递层 2026-10-26] 规则命中 → 沿连线送达目标卡。

D1 guard 各判据求值正确（状态/块空/事件有无/子卡数）
D2 target 解析（self / parent / ancestors / links / card:<id>）
D3 模板渲染占位符
D4 fire 端到端：完工未汇报 ⇒ 提醒落到该卡收件箱
D5 防重：同一条触发事件重复评估不会重复发
D6 判据不满足 ⇒ 不发（宁可不发，不误发）
"""
from __future__ import annotations

import json

import pytest

from src.infra import event_hub, harness, harness_rules
from src.storage import card_db, card_store, event_log, work_store


@pytest.fixture(autouse=True)
def _clean():
    event_hub.reset_state()
    harness_rules.reset_cache()
    yield
    event_hub.reset_state()
    harness_rules.reset_cache()


def _rules(tmp_path, rules) -> str:
    p = tmp_path / "harness_rules.json"
    p.write_text(json.dumps({"rules": rules}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _rule(**over) -> dict:
    r = {
        "id": "t_rule", "enabled": True, "desc": "测试",
        "trigger": {"types": ["turn_end"]},
        "guard": [], "target": "self", "message": "提醒:{card_id}",
    }
    r.update(over)
    return r


async def _seed(title="投递层"):
    w = await work_store.create_work(title)
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(wid, "子卡", "模型生成",
                                        parent=root))["meta"]["card_id"]
    return wid, root, sub


# ---------------------------------------------------------------- D1
@pytest.mark.parametrize("guard,expect", [
    ([{"kind": "no_event", "types": ["error"], "scope": "card"}], True),
    ([{"kind": "has_event", "types": ["error"], "scope": "card"}], False),
    ([{"kind": "no_event", "types": ["run_start"], "scope": "card"}], False),
    ([{"kind": "has_event", "types": ["run_start"], "scope": "card"}], True),
])
async def test_guard_kinds(guard, expect, sandbox):
    """判据只有事件类（用户令：系统消息只允许跟事件挂钩）。"""
    wid, root, sub = await _seed()
    from src.api import event_stream
    event_stream.emit(wid, sub, "run_start", {"pair_id": "p1"})
    await event_log.drain()
    ctx = await harness.build_ctx(wid, sub, "done")
    assert harness.evaluate_guard(guard, ctx) is expect


async def test_non_event_guard_rejected(sandbox):
    """非事件判据（读卡状态/块/子卡数）一律不放行。"""
    wid, root, sub = await _seed()
    ctx = await harness.build_ctx(wid, sub, "done")
    for bad in ({"kind": "card_state", "value": "completed"},
                {"kind": "field_empty", "field": "mission"},
                {"kind": "child_count", "op": ">=", "value": 1}):
        assert harness.evaluate_guard([bad], ctx) is False, bad


async def test_guard_event_presence(sandbox):
    wid, root, sub = await _seed()
    # 卡上没有任何 turn_end 事件
    ctx = await harness.build_ctx(wid, sub, "turn_end")
    assert harness.evaluate_guard(
        [{"kind": "no_event", "types": ["msg_notify"], "scope": "card"}], ctx) is True
    assert harness.evaluate_guard(
        [{"kind": "has_event", "types": ["msg_notify"], "scope": "card"}], ctx) is False

    # 造一条 msg_notify 事件后再判
    from src.api import event_stream
    event_stream.emit(wid, sub, "msg_notify", {"x": 1})
    await event_log.drain()
    await event_hub.flush_all()
    ctx2 = await harness.build_ctx(wid, sub, "turn_end")
    assert harness.evaluate_guard(
        [{"kind": "has_event", "types": ["msg_notify"], "scope": "card"}], ctx2) is True
    assert harness.evaluate_guard(
        [{"kind": "no_event", "types": ["msg_notify"], "scope": "card"}], ctx2) is False


# ---------------------------------------------------------------- D2
async def test_resolve_targets(sandbox):
    wid, root, sub = await _seed()
    grand = (await card_store.create_card(wid, "孙", "模型生成",
                                          parent=sub))["meta"]["card_id"]
    ctx = await harness.build_ctx(wid, grand, "turn_end")
    assert harness.resolve_targets("self", ctx) == [grand]
    assert harness.resolve_targets("parent", ctx) == [sub]
    assert harness.resolve_targets("ancestors", ctx) == [sub, root]
    assert harness.resolve_targets(f"card:{root}", ctx) == [root]


# ---------------------------------------------------------------- D3
async def test_render_placeholders(sandbox):
    wid, root, sub = await _seed()
    ctx = await harness.build_ctx(wid, sub, "turn_end")
    txt = harness.render_message({"message": "{card_id}/{card_title}/{trigger_type}"}, ctx)
    assert txt == f"{sub}/子卡/turn_end"


# ---------------------------------------------------------------- D4
async def test_fire_delivers_to_self(tmp_path, monkeypatch, sandbox):
    p = _rules(tmp_path, [_rule(
        id="done_no_report", guard=[],
        message="你完工了但没汇报:{card_id}")])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()

    wid, root, sub = await _seed()

    out = await harness.fire(wid, "turn_end", sub, {}, trigger_seq=1)
    assert out and out[0]["to"] == sub
    from src.storage import session_facade
    texts = [str(b.get("content") or "") for b in (await session_facade.read(wid, sub) or [])]
    assert any("没汇报" in t for t in texts), texts


async def test_fire_delivers_to_parent(tmp_path, monkeypatch, sandbox):
    p = _rules(tmp_path, [_rule(
        id="need_mission", target="parent", guard=[],
        message="填使命:{card_id}")])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()

    wid, root, sub = await _seed()
    out = await harness.fire(wid, "turn_end", sub, {}, trigger_seq=2)
    assert out and out[0]["to"] == root
    from src.storage import session_facade
    texts = [str(b.get("content") or "") for b in (await session_facade.read(wid, root) or [])]
    assert any("填使命" in t for t in texts), texts


# ---------------------------------------------------------------- D5
async def test_dedup_same_trigger(tmp_path, monkeypatch, sandbox):
    p = _rules(tmp_path, [_rule(id="r_dup", message="重复测试")])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()

    wid, root, sub = await _seed()
    first = await harness.fire(wid, "turn_end", sub, {}, trigger_seq=7)
    assert first, "首次应投递"
    second = await harness.fire(wid, "turn_end", sub, {}, trigger_seq=7)
    assert second == [], "同一条触发事件不该重复发"

    third = await harness.fire(wid, "turn_end", sub, {}, trigger_seq=8)
    assert third, "新触发事件应可再发"


# ---------------------------------------------------------------- D6
async def test_guard_false_no_delivery(tmp_path, monkeypatch, sandbox):
    p = _rules(tmp_path, [_rule(
        id="only_completed",
        guard=[{"kind": "card_state", "value": "completed"}],
        message="不该发")])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()

    wid, root, sub = await _seed()          # state=active
    out = await harness.fire(wid, "turn_end", sub, {}, trigger_seq=9)
    assert out == []
    pend = await card_db.list_pending(wid, sub)
    assert not any("不该发" in str(m.get("content")) for m in pend)


async def test_continue_only_for_review_cards(sandbox):
    """`continue` 暂时只对评审卡开放（[2026-10-29 用户令]）。

    「continue 和 message 本质上很像，所以 continue 你需要标注，暂时只对评审卡开放」
    —— 类型闸在 `_deliver_continue` 里，读不到卡也一律不续接（fail-safe）。
    """
    from src.infra import harness

    # 卡不存在 ⇒ 不续接（读不到类型就不放行）
    assert await harness._deliver_continue(
        "work_t_none", "card_nope", "（续接）继续") is None
