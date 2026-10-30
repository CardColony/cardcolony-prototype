# -*- coding: utf-8 -*-
"""评审卡（review）类型注册断言。

[伴生评审卡 2026-10-27] 评审卡是 sub_project / topic 的**伴生卡**：
一对一、系统派生、随子卡归档、不进任务树。

本文件只钉**注册表事实**（P5：能断言的靠断言，不靠人看）。
流程行为（派生时机、批次、approve、版本作废）另在功能测试里钉。
"""
from __future__ import annotations

import json
from pathlib import Path

REGISTRY = Path(__file__).resolve().parents[1] / "config" / "card_type_registry.json"


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _types(pc: str) -> list[dict]:
    return list((_registry()["l1_vocabularies"][pc]["types"]))


def _review(pc: str) -> dict:
    for t in _types(pc):
        if t.get("code") == "review":
            return t
    raise AssertionError(f"{pc} 词表里没有 review")


def test_review_in_both_vocabularies():
    for pc in ("rd_project", "research_project"):
        assert _review(pc)["label"] == "评审卡"


def test_review_is_off_tree_and_childless():
    for pc in ("rd_project", "research_project"):
        rv = _review(pc)
        assert rv["off_tree"] is True, "评审卡必须离树（不进任务树/画布/统计）"
        assert rv["can_have_children"] is False, "评审卡不可有子卡"


def test_default_child_type_not_regressed():
    """加类型不许改变默认子卡类型。

    `_default_child_type` 取词表里**第一个** requires_owner=false 的类型，
    故 review 必须排在 sub_project / topic 之后。
    """
    assert _types("rd_project")[0]["code"] == "sub_project"
    assert _types("research_project")[0]["code"] == "topic"


def test_review_not_in_model_templates():
    """评审卡由系统派生，不参与模型建卡的模板分发。"""
    for pc in ("rd_project", "research_project"):
        assert "default_template" not in _review(pc)
