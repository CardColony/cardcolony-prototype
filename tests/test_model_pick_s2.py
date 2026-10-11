# -*- coding: utf-8 -*-
"""迭代五 S2 · 席位级模型配置单测（get_override / resolve_model）。

四态覆盖（任务书 S2 步骤⑦）：get_override（键在非空/键在空串/缺键留痕/
非法值归 resolve_model）＋ resolve_model（合法放行/非法回落 main＋warning/
缺键回落 main/键集获取失败回落 main）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.api import model_service
from src.thinkchain import model_pick

REPO = Path(__file__).resolve().parents[1]
PICK_DEFAULT = REPO / "config" / "model_pick.json"


def _stub_gateway(monkeypatch, model_keys: tuple[str, ...]) -> None:
    """stub 掉全局 gateway 单例，models 键集由测试自控（不依赖真实 yaml）。"""

    class _Stub:
        def __init__(self):
            self.config = {"models": {k: {"model": k} for k in model_keys}}

    monkeypatch.setattr(model_service, "_gateway", _Stub())


def _write_pick(monkeypatch, tmp_path, table: dict) -> Path:
    p = tmp_path / "model_pick.json"
    p.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(model_pick, "PICK_PATH", p)
    return p


# ══════════════ get_override 四态 ══════════════

def test_get_override_key_present_non_empty(monkeypatch, tmp_path):
    _write_pick(monkeypatch, tmp_path, {"synthesize": "fallback"})
    assert model_pick.get_override("synthesize") == "fallback"


def test_get_override_key_present_empty_string(monkeypatch, tmp_path, caplog):
    """键在但值空串＝钦定默认态 → None 且**不刷 warning**（R-I9 分级）。"""
    _write_pick(monkeypatch, tmp_path, {"synthesize": ""})
    with caplog.at_level(logging.WARNING, logger="thinkchain.model_pick"):
        assert model_pick.get_override("synthesize") is None
    warns = [r for r in caplog.records
             if r.levelno >= logging.WARNING and "未配置" in r.getMessage()]
    assert warns == []


def test_get_override_key_missing_warns(monkeypatch, tmp_path, caplog):
    """缺键 → None ＋ warning 留痕（R-I9 分级另一形态）。"""
    _write_pick(monkeypatch, tmp_path, {"synthesize": ""})
    with caplog.at_level(logging.WARNING, logger="thinkchain.model_pick"):
        assert model_pick.get_override("triage") is None
    assert any("triage" in r.getMessage() and "未配置" in r.getMessage()
               for r in caplog.records)


# ══════════════ resolve_model 四态 ══════════════

def test_resolve_model_legal_override(monkeypatch, tmp_path):
    _write_pick(monkeypatch, tmp_path, {"viewpoint_qwen": "fallback"})
    _stub_gateway(monkeypatch, ("main", "fallback", "viewpoint_qwen"))
    assert model_pick.resolve_model("viewpoint_qwen") == "fallback"


def test_resolve_model_illegal_falls_back_with_warning(monkeypatch, tmp_path, caplog):
    """非法模型名 → 回落 main ＋ warning 留痕，绝不 KeyError（R-I5）。"""
    _write_pick(monkeypatch, tmp_path, {"synthesize": "no_such_seat"})
    _stub_gateway(monkeypatch, ("main", "fallback"))
    with caplog.at_level(logging.WARNING, logger="thinkchain.model_pick"):
        assert model_pick.resolve_model("synthesize") == "main"
    assert any("非法模型" in r.getMessage() and "no_such_seat" in r.getMessage()
               for r in caplog.records)


def test_resolve_model_missing_key_falls_back(monkeypatch, tmp_path):
    """缺键（未配置）→ 回落 main（缺省行为逐字不变）。"""
    _write_pick(monkeypatch, tmp_path, {})
    _stub_gateway(monkeypatch, ("main", "fallback"))
    assert model_pick.resolve_model("synthesize") == "main"
    assert model_pick.resolve_model("triage") == "main"


def test_resolve_model_empty_value_falls_back(monkeypatch, tmp_path):
    """键在值空串（表内五键默认态）→ 回落 main。"""
    _write_pick(monkeypatch, tmp_path, {
        "viewpoint_minimax": "", "viewpoint_qwen": "", "viewpoint_deepseek": "",
        "synthesize": "", "triage": ""})
    _stub_gateway(monkeypatch, ("main", "fallback"))
    for role in ("viewpoint_minimax", "viewpoint_qwen", "viewpoint_deepseek",
                 "synthesize", "triage"):
        assert model_pick.resolve_model(role) == "main"


def test_resolve_model_gateway_failure_falls_back(monkeypatch, tmp_path):
    """键集获取失败（gateway 异常）→ 回落 main，不抛出。"""
    _write_pick(monkeypatch, tmp_path, {"synthesize": "fallback"})

    def _boom():
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(model_service, "get_gateway", _boom)
    assert model_pick.resolve_model("synthesize") == "main"


def test_model_pick_json_five_seats_present():
    """E9/E10 静态断言：五键在位、三席位名与 models.yaml:103 逐字一致、值全空串。"""
    import yaml
    data = json.loads(PICK_DEFAULT.read_text(encoding="utf-8"))
    yaml_models = yaml.safe_load((REPO / "config" / "models.yaml")
                                 .read_text(encoding="utf-8"))
    seats = list(yaml_models["simple"]["viewpoints"])
    for key in (*seats, "synthesize", "triage"):
        assert key in data, key
        assert data[key] == "", key
    # execute/think 两键不动（用户 13:32：只有执行/思考可被编排）
    assert data["execute"] == "main" and data["think"] == "main"
    assert data["default"] == "main"
