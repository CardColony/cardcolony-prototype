# -*- coding: utf-8 -*-
"""[harness 第 2 步 2026-10-28] 通用条件求值器。

改前只能问「某类事件在本轮/本卡有没有发生过，且某个**顶层**字段**等于**某个值」。
现在条件是 all/any/not/event/count/field 的任意嵌套树。
"""
from __future__ import annotations

import pytest

from src.infra import harness_cond as hc, policy_set as ps


def _ev(t: str, data: dict | None = None, seq: int = 0) -> dict:
    return {"seq": seq, "type": t, "data": data or {}}


def _ctx(events: list[dict], **kw) -> dict:
    """events 传**旧→新**，内部反转成新→旧（与 build_ctx 同口径）。"""
    base = {"work_id": "work_001", "card_id": "card_002", "card_type": "sub_project",
            "card_title": "作业卡", "parent": "card_001", "state": "active",
            "mission": "", "status_block": "", "requirements": "",
            "trigger_type": "done", "trigger_data": {},
            "events": list(reversed(events))}
    base.update(kw)
    return base


# ---------------------------------------------------------------- 基本节点
def test_all_any_not():
    ctx = _ctx([])
    assert hc.eval_cond({"all": []}, ctx) is True
    assert hc.eval_cond({"any": []}, ctx) is False
    assert hc.eval_cond({"not": {"any": []}}, ctx) is True
    assert hc.eval_cond({"all": [True, True]}, ctx) is True
    assert hc.eval_cond({"any": [False, True]}, ctx) is True


def test_event_presence_and_types():
    ctx = _ctx([_ev("run_start"), _ev("tool_result", {"tool": "read_card", "ok": True})])
    assert hc.eval_cond({"event": {"types": ["tool_result"]}}, ctx) is True
    assert hc.eval_cond({"event": {"types": ["msg_ask"]}}, ctx) is False


# ---------------------------------------------------------------- where
def test_where_nested_path():
    """where 支持点号路径——旧实现只读顶层。"""
    ctx = _ctx([_ev("tool_result", {"tool": "x", "result": {"ok": True, "code": "E1"}})])
    assert hc.eval_cond({"event": {"types": ["tool_result"],
                                   "where": {"result.ok": True}}}, ctx) is True
    assert hc.eval_cond({"event": {"types": ["tool_result"],
                                   "where": {"result.code": "E1"}}}, ctx) is True
    assert hc.eval_cond({"event": {"types": ["tool_result"],
                                   "where": {"result.code": "E2"}}}, ctx) is False


@pytest.mark.parametrize("where,expect", [
    ({"rounds": {"gt": 3}}, True),
    ({"rounds": {"ge": 5}}, True),
    ({"rounds": {"lt": 5}}, False),
    ({"rounds": {"le": 5}}, True),
    ({"rounds": {"ne": 4}}, True),
    ({"rounds": {"in": [1, 5, 9]}}, True),
    ({"rounds": {"exists": True}}, True),
    ({"missing": {"exists": False}}, True),
    ({"err": {"contains": "timeout"}}, True),
    ({"err": {"matches": r"time\w+"}}, True),
])
def test_comparators(where, expect):
    ctx = _ctx([_ev("error", {"rounds": 5, "err": "connect timeout"})])
    assert hc.eval_cond({"event": {"types": ["error"], "where": where}}, ctx) is expect


def test_dollar_ctx_substitution():
    """``$parent`` 取本卡字段——规则表不必写死卡号。"""
    ctx = _ctx([_ev("msg_directive", {"from": "card_001"})])
    assert hc.eval_cond({"event": {"types": ["msg_directive"],
                                   "where": {"from": "$parent"}}}, ctx) is True
    ctx2 = _ctx([_ev("msg_directive", {"from": "card_099"})])
    assert hc.eval_cond({"event": {"types": ["msg_directive"],
                                   "where": {"from": "$parent"}}}, ctx2) is False


# ---------------------------------------------------------------- count
def test_count_node():
    ctx = _ctx([_ev("tool_result", {"tool": "report_up", "ok": True}) for _ in range(3)])
    assert hc.eval_cond({"count": {"types": ["tool_result"]},
                         "cmp": "ge", "value": 3}, ctx) is True
    assert hc.eval_cond({"count": {"types": ["tool_result"]},
                         "cmp": "eq", "value": 2}, ctx) is False


# ---------------------------------------------------------------- field 节点
def test_field_node_reads_ctx():
    ctx = _ctx([], state="blocked")
    assert hc.eval_cond({"field": "state", "cmp": "eq", "value": "blocked"}, ctx) is True
    assert hc.eval_cond({"field": "state", "cmp": "ne", "value": "active"}, ctx) is True


# ---------------------------------------------------------------- within
def test_within_turn_excludes_previous_turn():
    """本轮＝触发事件之后、上一个 turn_end 之前。上一轮的事件不算。"""
    events = [
        _ev("tool_result", {"tool": "report_up", "ok": True}),   # 上一轮
        _ev("turn_end", {"outcome": "ok"}),
        _ev("run_start"),
        _ev("tool_result", {"tool": "read_card", "ok": True}),   # 本轮
        _ev("done", {"card_type": "sub_project"}),               # 触发事件
    ]
    ctx = _ctx(events)
    cond = {"event": {"types": ["tool_result"],
                      "where": {"tool": "report_up"}, "within": "turn"}}
    assert hc.eval_cond(cond, ctx) is False, "上一轮的 report_up 不该算进本轮"
    cond2 = {"event": {"types": ["tool_result"],
                       "where": {"tool": "read_card"}, "within": "turn"}}
    assert hc.eval_cond(cond2, ctx) is True
    # within=card 时整卡历史都算
    assert hc.eval_cond({"event": {"types": ["tool_result"],
                                   "where": {"tool": "report_up"},
                                   "within": "card"}}, ctx) is True


def test_within_last_n():
    events = [_ev("tool_result", {"tool": "a"}) for _ in range(5)]
    events.append(_ev("done", {}))
    ctx = _ctx(events)
    assert hc.eval_cond({"event": {"types": ["tool_result"],
                                   "where": {"tool": "a"},
                                   "within": {"last": 2}}}, ctx) is True
    assert hc.eval_cond({"event": {"types": ["tool_result"],
                                   "where": {"tool": "a"},
                                   "within": {"last": 0}}}, ctx) is False


# ---------------------------------------------------------------- 时间当事实
def test_time_as_fact_not_timer():
    """ts 是普通字段：可以比较，但没有任何定时器在跑（求值即问即答）。"""
    ctx = _ctx([
        _ev("tool_result", {"tool": "report_up", "ok": True}),
    ])
    ctx["events"][0]["ts"] = "2026-10-28T01:00:00+08:00"
    cond = {"event": {"types": ["tool_result"],
                      "where": {"ts": {"gt": "2026-10-28T00:00:00+08:00"}}}}
    assert hc.eval_cond(cond, ctx) is True
    cond2 = {"event": {"types": ["tool_result"],
                       "where": {"ts": {"gt": "2026-10-29T00:00:00+08:00"}}}}
    assert hc.eval_cond(cond2, ctx) is False


# ---------------------------------------------------------------- 兼容
def test_legacy_guard_translates_identically():
    """旧写法（no_event/has_event）翻译后语义逐字不变。

    事件按**生产顺序**造（run_start … tool_result … done，turn_end 在 done 之后
    才写）——这正是旧测试按 turn_end→done 的假顺序造事件时绕过的那条路径。
    """
    events = [_ev("run_start"), _ev("tool_result", {"tool": "report_up", "ok": True}),
              _ev("done", {})]
    ctx = _ctx(events)
    legacy_no = [{"kind": "no_event", "types": ["tool_result"],
                  "where": {"tool": "report_up", "ok": True}, "scope": "turn"}]
    legacy_has = [{"kind": "has_event", "types": ["tool_result"], "scope": "card"}]
    assert hc.eval_cond(hc.normalize_guard(legacy_no), ctx) is False
    assert hc.eval_cond(hc.normalize_guard(legacy_has), ctx) is True
    # 新写法等价
    assert hc.eval_cond({"not": {"event": {"types": ["tool_result"],
                                          "where": {"tool": "report_up", "ok": True},
                                          "within": "turn"}}}, ctx) is False


def test_turn_window_survives_done_before_turn_end():
    """生产顺序是 done → turn_end；窗口不能因此跨进上一轮。

    旧算法「跳过第一个 turn_end、遇第二个才停」在 done 触发时会把上一轮算进来
    （本轮 turn_end 还没写，遇到的第一个是上一轮的）⇒ 上一轮汇报过，本轮就被
    判成"已汇报"，该提醒的不提醒。
    """
    events = [
        _ev("run_start"),                                        # 上一轮
        _ev("tool_result", {"tool": "report_up", "ok": True}),   # 上一轮汇报了
        _ev("done", {}),
        _ev("turn_end", {}),
        _ev("run_start"),                                        # 本轮
        _ev("tool_result", {"tool": "read_card", "ok": True}),
        _ev("done", {}),                                         # 触发事件（本轮）
    ]
    ctx = _ctx(events)
    cond = {"not": {"event": {"types": ["tool_result"],
                              "where": {"tool": "report_up", "ok": True},
                              "within": "turn"}}}
    assert hc.eval_cond(cond, ctx) is True, "上一轮的汇报不许算进本轮"


def test_empty_guard_is_true():
    ctx = _ctx([])
    assert hc.eval_cond(hc.normalize_guard(None), ctx) is True
    assert hc.eval_cond(hc.normalize_guard([]), ctx) is True


def test_unknown_node_is_false():
    """认不出的形状 ⇒ 不成立（宁可少发，不误发）。"""
    ctx = _ctx([])
    assert hc.eval_cond({"nonsense": 1}, ctx) is False
    assert hc.eval_cond(hc.normalize_guard(
        [{"kind": "card_state", "value": "x"}]), ctx) is False


# ---------------------------------------------------------------- 校验
def test_validate_accepts_both_styles():
    """合法 when 不报错（校验走 PolicySet.load → harness_cond.validate_cond）。"""
    assert ps.PolicySet().load([{
        "id": "a", "watch": ["done"],
        "when": {"not": {"event": {"types": ["error"], "within": "turn"}}},
        "then": {"target": "self", "message": "m"},
    }]) == []
    assert ps.PolicySet().load([{
        "id": "b", "watch": ["done"],
        "when": {"all": [
            {"not": {"event": {"types": ["tool_result"],
                               "where": {"tool": "report_up", "ok": True},
                               "within": "turn"}}},
            {"count": {"types": ["tool_result"]}, "cmp": "ge", "value": 1},
        ]},
        "then": {"target": "self", "message": "m"},
    }]) == []


def test_validate_rejects_bad_shapes():
    errs = ps.PolicySet().load([{
        "id": "c", "watch": ["done"], "when": {"nope": 1},
        "then": {"target": "self", "message": "m"}}])
    assert any("认不出的条件节点" in e for e in errs)
    errs2 = ps.PolicySet().load([{
        "id": "d", "watch": ["done"],
        "when": {"event": {"types": ["x"], "within": "forever"}},
        "then": {"target": "self", "message": "m"}}])
    assert any("within 非法" in e for e in errs2)


# ---------------------------------------------------------------- 先后关系
def _cev(type_, **data):
    return {"type": type_, "data": data, "_t": 0.0}


def test_after_true_when_nothing_after_anchor():
    """最近一次 anchor 之后没有 expect ⇒ 成立（「干完了没汇报」）。"""
    ctx = {"events": [_cev("done"), _cev("tool_result", tool="x")]}   # 新→旧
    assert hc.eval_cond({"after": {
        "anchor": {"types": ["done"]},
        "expect": {"types": ["tool_result"], "where": {"tool": "report_up"}},
    }}, ctx) is True


def test_after_false_when_expect_after_anchor():
    ctx = {"events": [
        _cev("tool_result", tool="report_up", ok=True),
        _cev("done"),
        _cev("tool_result", tool="report_up", ok=True),   # 上一轮的汇报，不算
    ]}
    assert hc.eval_cond({"after": {
        "anchor": {"types": ["done"]},
        "expect": {"types": ["tool_result"], "where": {"tool": "report_up"}},
    }}, ctx) is False


def test_after_false_without_anchor():
    ctx = {"events": [_cev("tool_result", tool="x")]}
    assert hc.eval_cond({"after": {
        "anchor": {"types": ["done"]},
        "expect": {"types": ["tool_result"]},
    }}, ctx) is False


def test_after_ignores_expect_before_anchor():
    """anchor 之前的 expect 不算——否则上一轮汇报会把这一轮盖住。"""
    ctx = {"events": [_cev("done"), _cev("tool_result", tool="report_up")]}
    assert hc.eval_cond({"after": {
        "anchor": {"types": ["done"]},
        "expect": {"types": ["tool_result"], "where": {"tool": "report_up"}},
    }}, ctx) is True


def test_after_uses_latest_anchor():
    """多个 anchor 时只看**最近**那一次。"""
    ctx = {"events": [
        _cev("done"),
        _cev("tool_result", tool="report_up"),   # 更早的 done 之后汇报过
        _cev("done"),
    ]}
    assert hc.eval_cond({"after": {
        "anchor": {"types": ["done"]},
        "expect": {"types": ["tool_result"], "where": {"tool": "report_up"}},
    }}, ctx) is True


def test_after_validates_shape():
    assert hc.validate_cond({"after": "not-an-object"}, path="when")
    assert hc.validate_cond({"after": {"anchor": 1}}, path="when")
    assert hc.validate_cond({"after": {"anchor": {"types": ["done"]},
                                       "expect": {"types": ["x"]}}}, path="when") == []
