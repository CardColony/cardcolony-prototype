# -*- coding: utf-8 -*-
"""能力清单的守护网（[能力清单 2026-10-30 · 基础设施]）。

**为什么有这份测试**：能力清单会随版本一直长。靠"加工具的人记得去清单里登记"
必然漏——漏了以后没有任何现象，只是某项能力悄悄少一件工具，或者某个新功能
永远进不了清单。所以清单的完整性必须是**跑一遍不成立就红**，不是自觉。

四条不变量（与 ``config/capabilities.json`` 的 ``_meta.invariants`` 同源）：

1. 正向不漏：每件已注册工具，必须恰好属于 能力 / not_capabilities / pending 之一。
2. 反向不腐：能力指向的工具必须真实存在；同一件工具不得属于两项能力。
3. 对象不虚：``applies_to`` 必须是已知对象种类；``not_capabilities`` 每条要有理由。
4. 只减不增：``pending`` 是迁移期欠账，冻结在 2026-10-30 的基线，只能变小。

新增工具时会发生什么：不在清单里 ⇒ 第 1 条红 ⇒ 必须给它一个位置（归入某项能力，
或写进 not_capabilities 并给理由）。这就是"自主吸收"的机制面——不是自动归类，
而是**漏不掉**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api import card_tools
from src.infra import capability_registry as cap


#: 2026-10-30 建清单时的欠账基线。**只能减，不能加**——
#: 往 pending 里塞新工具等于把"漏掉"伪装成"待办"，本测试会红。
_PENDING_BASELINE_20261030: frozenset[str] = frozenset({
    "approve", "archive_card", "bash", "confirm_with_user", "continue_task",
    "correct_alarm", "create_card", "create_coder_card", "create_function",
    "create_goal", "curate_face", "decide", "delete_card", "delete_file",
    "design_policy", "direct", "draft_rule_edit", "edit_card", "edit_file",
    "edit_note", "edit_requirements", "edit_shadow", "edit_status", "fetch_web",
    "function_watch_cards", "get_goal", "glob_files", "link_cards", "link_rel",
    "list_files", "list_functions", "list_manuscript", "move_file",
    "open_review", "promote_draft", "propose_status", "python", "raise_alarm",
    "read_archive", "read_card", "read_file", "read_linked", "read_manuscript",
    "read_memory", "read_shadow", "record_verdict", "reject", "rename_file",
    "reparent_card", "report_done", "report_to_user", "report_up",
    "resume_child", "revoke_approve", "search_files", "search_manuscript",
    "set_child_mission", "set_criteria", "set_function_duty", "silence_card",
    "stage_open", "stop_card", "sync", "unarchive_card", "unlink_cards",
    "unlink_rel", "update_function_profile", "update_goal", "update_status",
    "web_search", "write_file", "write_ledger", "write_requirements_final",
})


def _tools() -> set[str]:
    return set(card_tools.TOOL_REGISTRY.keys())


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """把清单指到临时文件；用完后清缓存并还原环境变量。"""
    p = tmp_path / "capabilities.json"

    def _write(data: dict) -> Path:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        cap.reset_cache()
        return p

    monkeypatch.setenv(cap.ENV_PATH, str(p))
    cap.reset_cache()
    yield _write
    cap.reset_cache()


def _ok_registry(capabilities=None, not_capabilities=None, pending=None) -> dict:
    return {
        "_meta": {"version": 1},
        "capabilities": capabilities or [],
        "not_capabilities": not_capabilities or [],
        "pending": pending or [],
    }


def _cap_item(code: str, tools: list[str], applies_to=None) -> dict:
    return {"code": code, "name": code, "desc": "",
            "applies_to": applies_to or ["function"],
            "impl": {"kind": "tools", "tools": tools}}


# ---------------------------------------------------------------- 1 正向不漏
def test_every_tool_is_classified():
    """每件注册工具都必须有位置：能力 / 排除 / pending。"""
    data = cap.load()
    assert data["enabled"], f"清单不可用: {data.get('reason')}"
    covered = set(cap.tools_of(cap.codes()))
    covered |= {str(r.get("tool") or "") for r in cap.not_capabilities()}
    covered |= set(cap.pending())
    missing = sorted(_tools() - covered)
    assert not missing, f"这些工具没有归类（既不在能力里，也不在排除名单/pending）：{missing}"


def test_validate_reports_nothing():
    """清单与注册表整体一致（问题清单为空）。"""
    assert cap.validate(_tools()) == []


# ---------------------------------------------------------------- 2 反向不腐
def test_capability_tool_must_exist(registry):
    registry(_ok_registry(capabilities=[_cap_item("link", ["no_such_tool"])]))
    problems = cap.validate(_tools())
    assert any("不存在" in p and "no_such_tool" in p for p in problems), problems


def test_same_tool_in_two_capabilities_is_red(registry):
    registry(_ok_registry(capabilities=[
        _cap_item("a", ["read_card"]), _cap_item("b", ["read_card"])]))
    problems = cap.validate(_tools())
    assert any("同时属于两项能力" in p for p in problems), problems


# ---------------------------------------------------------------- 3 对象不虚
def test_not_capability_needs_reason(registry):
    registry(_ok_registry(not_capabilities=[{"tool": "bash", "reason": ""}]))
    problems = cap.validate(_tools())
    assert any("缺 reason" in p for p in problems), problems


def test_applies_to_must_be_known_kind(registry):
    registry(_ok_registry(capabilities=[
        _cap_item("x", ["read_card"], applies_to=["spaceship"])]))
    problems = cap.validate(_tools())
    assert any("未知对象种类" in p for p in problems), problems


def test_capability_needs_impl(registry):
    registry(_ok_registry(capabilities=[{"code": "x", "name": "x",
                                         "applies_to": ["function"]}]))
    problems = cap.validate(_tools())
    assert any("缺 impl" in p for p in problems), problems


# ---------------------------------------------------------------- 4 只减不增
def test_pending_only_shrinks():
    """pending 只能比基线小；塞新工具进去＝把漏掉伪装成待办。"""
    current = set(cap.pending())
    extra = sorted(current - _PENDING_BASELINE_20261030)
    assert not extra, (
        "pending 里出现了基线之外的工具，说明新工具被塞进待办而不是归类："
        f"{extra}。请把它归入某项能力，或写进 not_capabilities 并给理由。")


def test_pending_covers_at_most_baseline():
    assert set(cap.pending()) <= _PENDING_BASELINE_20261030


# ---------------------------------------------------------------- 新工具进来会怎样
def test_new_tool_without_classification_is_red():
    """模拟"加了一件工具但没动清单"：必须红。"""
    problems = cap.validate(_tools() | {"brand_new_tool"})
    assert any("brand_new_tool" in p and "未归类" in p for p in problems), problems


# ---------------------------------------------------------------- fail-open
def test_missing_file_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv(cap.ENV_PATH, str(tmp_path / "nope.json"))
    cap.reset_cache()
    data = cap.load()
    assert data["enabled"] is False
    assert cap.tools_of(["anything"]) == set()
    assert cap.codes() == []
    assert any("清单不可用" in p for p in cap.validate(_tools()))
    cap.reset_cache()


def test_broken_json_is_fail_open(tmp_path, monkeypatch):
    p = tmp_path / "capabilities.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv(cap.ENV_PATH, str(p))
    cap.reset_cache()
    assert cap.load()["enabled"] is False
    cap.reset_cache()


# ---------------------------------------------------------------- 挂载解析
def test_tools_of_and_filter_mounts(registry):
    registry(_ok_registry(capabilities=[
        _cap_item("link", ["link_cards", "unlink_cards"]),
        _cap_item("monitor", ["read_linked"]),
        _cap_item("card_only", ["edit_card"], applies_to=["card"]),
        _cap_item("both", ["read_card"], applies_to=["function", "card"]),
    ]))
    assert cap.tools_of(["link", "monitor"]) == {"link_cards", "unlink_cards", "read_linked"}
    # 未知 code 忽略、不适用的对象剔除、重复去重、保序
    assert cap.filter_mounts(["link", "nope", "card_only", "link"], "function") == ["link"]
    assert cap.filter_mounts(["link", "card_only", "both"], "card") == ["card_only", "both"]


def test_empty_mount_means_no_tools(registry):
    registry(_ok_registry(capabilities=[_cap_item("link", ["link_cards"])]))
    assert cap.tools_of([]) == set()
