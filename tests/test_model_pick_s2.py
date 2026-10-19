# -*- coding: utf-8 -*-
"""模型选择（收拢后）：pick_model / resolve_model 回落语义（经 gateway.config 同源读）。

收拢后无 model_pick.json——读取源是 gateway.config.bindings。用 stub gateway
控制 bindings/models/system，测：命中放行 / 未命中回落 / 非法值回落 / 缺键集回落。
"""
from __future__ import annotations

import logging

from src.api import model_service
from src.thinkchain import model_pick


def _stub_gateway(monkeypatch, bindings: dict, models=("main", "backup"),
                  default: str = "main") -> None:
    """stub 全局 gateway 单例，models 键集 / bindings / default 由测试自控。"""

    class _Stub:
        def __init__(self):
            self.config = {
                "models": {k: {"model": k} for k in models},
                "bindings": bindings,
                "system": {"default_model": default},
            }

    monkeypatch.setattr(model_service, "_gateway", _Stub())


# ══════════════ resolve_model ══════════════

def test_resolve_model_legal(monkeypatch):
    _stub_gateway(monkeypatch, {"synthesize": "backup"})
    assert model_pick.resolve_model("synthesize") == "backup"


def test_resolve_model_illegal_falls_back(monkeypatch, caplog):
    """非法模型名（不在 models 键集）→ 回落 default_model + warning。"""
    _stub_gateway(monkeypatch, {"synthesize": "no_such_seat"})
    with caplog.at_level(logging.WARNING, logger="thinkchain.model_pick"):
        assert model_pick.resolve_model("synthesize") == "main"
    assert any("非法" in r.getMessage() for r in caplog.records)


def test_resolve_model_missing_falls_back(monkeypatch):
    _stub_gateway(monkeypatch, {})
    assert model_pick.resolve_model("synthesize") == "main"
    assert model_pick.resolve_model("triage") == "main"


def test_resolve_model_empty_falls_back(monkeypatch):
    _stub_gateway(monkeypatch, {"synthesize": ""})
    assert model_pick.resolve_model("synthesize") == "main"


def test_resolve_model_multi_takes_first(monkeypatch):
    _stub_gateway(monkeypatch, {"viewpoint": ["backup", "main"]})
    assert model_pick.resolve_model("viewpoint") == "backup"