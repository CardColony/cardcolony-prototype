# -*- coding: utf-8 -*-
"""harness 投递层单测（2026-10-31 两代并轨后）。

规则求值/派发的覆盖在 test_policy_set / test_policy_runner / test_harness_cond；
本文件只守投递层自己的面：目标解析（含 ``event:<路径>``）、消息渲染
（含 ``{event.*}``）、``continue`` 的评审卡闸门与 hint 透传。
"""
from __future__ import annotations

import pytest

from src.api import event_stream
from src.infra import harness
from src.storage import card_store, work_store


def _ctx(**kw) -> dict:
    base = {"work_id": "w", "card_id": "c1", "card_title": "子卡",
            "card_type": "worker", "parent": "p1",
            "trigger_type": "done", "trigger_data": {}, "events": []}
    base.update(kw)
    return base


# ---------------------------------------------------------------- 目标解析
def test_resolve_targets_static():
    ctx = _ctx(card_id="grand", parent="sub")
    assert harness.resolve_targets("self", ctx) == ["grand"]
    assert harness.resolve_targets("parent", ctx) == ["sub"]
    assert harness.resolve_targets("card:card_777", ctx) == ["card_777"]
    assert harness.resolve_targets("bogus", ctx) == []


async def test_resolve_targets_ancestors(sandbox):
    w = await work_store.create_work("目标解析")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(
        wid, "子项目", "模型生成", card_type="sub_project",
        parent=root))["meta"]["card_id"]
    grand = (await card_store.create_card(
        wid, "孙卡", "模型生成", card_type="worker",
        parent=sub))["meta"]["card_id"]
    ctx = _ctx(work_id=wid, card_id=grand, parent=sub)
    assert harness.resolve_targets("ancestors", ctx) == [sub, root]


def test_resolve_targets_event_path():
    """``event:<路径>`` 从触发事实取卡号——取不到/非字符串 ⇒ 解析为空。"""
    ctx = _ctx(trigger_data={"target": "card_009",
                             "nested": {"to": "card_010"}, "n": 42})
    assert harness.resolve_targets("event:target", ctx) == ["card_009"]
    assert harness.resolve_targets("event:nested.to", ctx) == ["card_010"]
    assert harness.resolve_targets("event:missing", ctx) == []
    assert harness.resolve_targets("event:n", ctx) == [], "非字符串不能当卡号"
    assert harness.resolve_targets("event:", ctx) == []
    assert harness.resolve_targets("event:target", _ctx()) == []


# ---------------------------------------------------------------- 消息渲染
def test_render_message_placeholders():
    txt = harness.render_message(
        {"message": "{card_id}/{card_title}/{trigger_type}"}, _ctx())
    assert txt == "c1/子卡/done"


def test_render_message_event_placeholders():
    ctx = _ctx(trigger_data={"batch": "rv_9", "seat": "fn_004",
                             "nested": {"v": "深"}})
    txt = harness.render_message(
        {"message": "批次 {event.batch}，席位 {event.seat}，"
                    "{event.nested.v}，{event.miss}"}, ctx)
    assert txt == "批次 rv_9，席位 fn_004，深，"


def test_render_message_bad_template_no_raise():
    txt = harness.render_message({"message": "{不合法"}, _ctx())
    assert txt == "{不合法"


# ---------------------------------------------------------------- continue 闸门
async def test_continue_is_review_card_only(monkeypatch):
    """continue 只放评审卡——别的卡来了直接不续接。"""
    async def _read(work_id, card_id):
        return {"meta": {"card_type": "worker"}}

    monkeypatch.setattr(card_store, "read_card", _read)
    assert await harness._deliver_continue("w", "card_x", "接着跑") is None


async def test_continue_passes_hint_to_run_once(monkeypatch):
    """continue 的提示词必须经 ``run_once(hint=…)`` 递到席位——
    评审回合不读 ``pending_inject``，inject 队列递不到。"""
    from src.api import review_runner

    async def _read(work_id, card_id):
        return {"meta": {"card_type": "review"}}

    got: dict = {}

    async def _once(work_id, review_id, *, hint=""):
        got.update(work=work_id, review=review_id, hint=hint)
        return {"continued": True, "reason": "spoken"}

    monkeypatch.setattr(card_store, "read_card", _read)
    monkeypatch.setattr(review_runner, "run_once", _once)
    monkeypatch.setattr(event_stream, "emit", lambda *a, **kw: None)

    out = await harness._deliver_continue("w1", "card_rv", "你没表态，接着说")
    assert out == "continue:1"
    assert got["review"] == "card_rv"
    assert got["hint"] == "你没表态，接着说"
