# -*- coding: utf-8 -*-
"""模型选择（收拢后）：pick_model / resolve_model 经 model_config 绑定解析。

收拢后不存在 model_pick.json（已并入 config/models.yaml 的 bindings 段）。
本件测薄包装新语义：角色→模型绑定（execute/think/synthesize/compress/draft→haiku，
condense→glm-flash；viewpoint→三席数组）+ 回落（未绑定角色→default_model）。
"""
from __future__ import annotations

from src.thinkchain.model_pick import pick_model, resolve_model


def test_pick_model_default_returns_default_model():
    assert pick_model() == "haiku"


def test_pick_model_execute_think_synthesize():
    assert pick_model("execute") == "haiku"
    assert pick_model("think") == "haiku"
    assert pick_model("synthesize") == "haiku"


def test_pick_model_condense():
    assert pick_model("condense") == "glm-flash"


def test_pick_model_unknown_falls_back():
    assert pick_model("no_such_role") == "haiku"


def test_resolve_model_synthesize():
    assert resolve_model("synthesize") == "haiku"


def test_resolve_model_unknown_falls_back():
    assert resolve_model("no_such_role") == "haiku"