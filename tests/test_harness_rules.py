# -*- coding: utf-8 -*-
"""[规则层 2026-10-26] 可任意增删 + 逐条校验 + 按事件索引。

R1 仓库里的规则表全部合法、且能按事件类型索引到
R2 坏条目只标记 invalid，不拖垮整表（其余规则照常可用）
R3 未登记事件类型 / 不可触发类型 / 非法 target / 非法 guard 都被拒
R4 改文件即生效（热加载），无需重启
R5 summary 体检数字正确
"""
from __future__ import annotations

import json
import os

import pytest

from src.infra import event_hub, harness_rules


@pytest.fixture(autouse=True)
def _clean():
    event_hub.reset_state()
    harness_rules.reset_cache()
    yield
    harness_rules.reset_cache()


def _write_rules(tmp_path, rules) -> str:
    p = tmp_path / "harness_rules.json"
    p.write_text(json.dumps({"_meta": {"version": 1}, "rules": rules},
                            ensure_ascii=False), encoding="utf-8")
    return str(p)


def _valid_rule(**over) -> dict:
    r = {
        "id": "t_rule",
        "enabled": True,
        "desc": "测试规则",
        "trigger": {"types": ["turn_end"]},
        "guard": [],
        "target": "self",
        "message": "提醒内容",
    }
    r.update(over)
    return r


# ---------------------------------------------------------------- R1
def test_repo_rules_are_valid(sandbox):
    rules = harness_rules.load_rules(include_invalid=True)
    bad = [r for r in rules if r.get("_errors")]
    assert not bad, f"仓库规则表有非法条目: {[(r.get('id'), r['_errors']) for r in bad]}"
    assert rules, "仓库规则表为空"


def test_rules_indexed_by_event_type(sandbox):
    rules = harness_rules.rules_for("done")
    assert any(r["id"] == "turn_end_without_report" for r in rules)
    # 未触发的事件类型取不到
    assert harness_rules.rules_for("tool_call") == []


# ---------------------------------------------------------------- R2
def test_bad_rule_does_not_break_table(tmp_path, monkeypatch, sandbox):
    p = _write_rules(tmp_path, [
        _valid_rule(id="good_one"),
        {"id": "broken", "trigger": {"types": ["不存在的类型"]}},   # 缺 message 等
    ])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()

    good = harness_rules.load_rules()
    assert [r["id"] for r in good] == ["good_one"], "坏条目不该进可用表"

    bad = harness_rules.invalid_rules()
    assert [r["id"] for r in bad] == ["broken"]


# ---------------------------------------------------------------- R3
@pytest.mark.parametrize("mutate,expect", [
    ({"trigger": {"types": ["不存在的事件"]}}, "未登记"),
    ({"trigger": {"types": ["tool_call"]}}, "不可触发"),      # triggerable=false
    ({"target": "不存在的目标"}, "target 非法"),
    ({"guard": [{"kind": "瞎写"}]}, "判据 kind 非法"),
    # [2026-10-26] 非事件判据一律拒绝（用户令：系统消息只允许跟事件挂钩）
    ({"guard": [{"kind": "card_state", "value": "completed"}]}, "判据 kind 非法"),
    ({"guard": [{"kind": "field_empty", "field": "mission"}]}, "判据 kind 非法"),
    ({"guard": [{"kind": "child_count", "op": ">=", "value": 1}]}, "判据 kind 非法"),
    ({"message": ""}, "缺 message"),
])
def test_invalid_rules_rejected(mutate, expect, tmp_path, monkeypatch, sandbox):
    p = _write_rules(tmp_path, [_valid_rule(**mutate)])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()
    bad = harness_rules.invalid_rules()
    assert len(bad) == 1, "非法条目应被标记"
    joined = " ".join(bad[0]["_errors"])
    assert expect in joined, joined


# ---------------------------------------------------------------- R4
def test_hot_reload_on_file_change(tmp_path, monkeypatch, sandbox):
    p = _write_rules(tmp_path, [_valid_rule(id="r1")])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()
    assert [r["id"] for r in harness_rules.load_rules()] == ["r1"]

    # 加一条
    _write_rules(tmp_path, [_valid_rule(id="r1"), _valid_rule(id="r2")])
    os.utime(p, None)
    harness_rules.reset_cache()          # mtime 精度兜底；不 reset 也应生效
    assert {r["id"] for r in harness_rules.load_rules()} == {"r1", "r2"}

    # 减一条
    _write_rules(tmp_path, [_valid_rule(id="r2")])
    os.utime(p, None)
    harness_rules.reset_cache()
    assert [r["id"] for r in harness_rules.load_rules()] == ["r2"]


# ---------------------------------------------------------------- R5
def test_summary_counts(tmp_path, monkeypatch, sandbox):
    p = _write_rules(tmp_path, [
        _valid_rule(id="on", enabled=True),
        _valid_rule(id="off", enabled=False),
        {"id": "bad"},
    ])
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, p)
    harness_rules.reset_cache()
    s = harness_rules.summary()
    assert s["total"] == 3
    assert s["enabled"] == 1
    assert s["invalid"] == 1
    assert s["trigger_types"] == ["turn_end"]
