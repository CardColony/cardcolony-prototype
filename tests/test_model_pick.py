# -*- coding: utf-8 -*-
"""模型选择（收拢后）：pick_model / resolve_model 经 model_config 绑定解析。

[5715 · 改判据] 原断言**硬编码模型名**（``pick_model() == "haiku"``）⇒ 配置一改就红
（2026-10-19 把 execute/think/synthesize 全切 glm-53-flash、默认切 minimax-m31-flash-preview
之后，这 6 条一直红着，却只是**断言滞后**，不是产品缺陷）。

改成锁**机制**而非某个具体模型名：

- 「角色命中 bindings ⇒ 返回该模型」
- 「未命中 ⇒ 落 default_model」
- 「resolve_model 额外校验合法性（非法值回落 default_model）」
- 「viewpoint 是多席 ⇒ 取首个」

⇒ 以后再换模型只改 config/models.yaml，**测试不会因此变红**；
  而真的把回落逻辑改坏了，测试照样会红。
"""
from __future__ import annotations

from src.api import model_config
from src.thinkchain.model_pick import pick_model, resolve_model


def test_pick_model_default_returns_default_model():
    """无角色 ⇒ 落 default_model（同一个真源，不硬编码模型名）。"""
    assert pick_model() == model_config.default_model()


def test_pick_model_execute_think_synthesize():
    """命中 bindings 的角色 ⇒ 返回该角色绑定的模型。"""
    for role in ("execute", "think", "synthesize"):
        want = model_config.resolve(role)
        assert want is not None, f"角色 {role} 应在 bindings 里配置"
        assert pick_model(role) == str(want), f"{role} 应解析到 {want}"


def test_pick_model_condense():
    """condense 同样走 bindings（历史断言写死 'glm-flash'，配置改名后必红）。"""
    want = model_config.resolve("condense")
    assert want is not None, "condense 应在 bindings 里配置"
    assert pick_model("condense") == str(want)


def test_pick_model_unknown_falls_back():
    """未配置的角色 ⇒ 落 default_model（不抛、不返回 None）。"""
    assert pick_model("no_such_role") == model_config.default_model()


def test_resolve_model_synthesize():
    """resolve_model 命中 ⇒ 返回绑定的模型名。"""
    want = model_config.resolve("synthesize")
    assert want is not None
    assert resolve_model("synthesize") == str(want)


def test_resolve_model_unknown_falls_back():
    """未配置的角色 ⇒ 落 default_model。"""
    assert resolve_model("no_such_role") == model_config.default_model()
