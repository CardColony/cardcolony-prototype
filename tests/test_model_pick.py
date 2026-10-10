# -*- coding: utf-8 -*-
"""模型选择查表单测（迭代二 S3 验收＋迭代三 S2 验收）。

迭代二口径：恒返 main 且经查表。迭代三 S2 增补：表三行
（default/execute/think）均落 main、临时副本热加载实证、缺行留痕日志。
"""
from __future__ import annotations

import json

from src.thinkchain import model_pick
from src.thinkchain.model_pick import pick_model

# 六席位名集合（任务书 S2 验收口径）
SEAT_NAMES = {"main", "fallback", "condenser",
              "viewpoint_minimax", "viewpoint_qwen", "viewpoint_deepseek"}


def test_always_returns_main():
    assert pick_model() == "main"


def test_unknown_role_falls_back_to_default_row():
    assert pick_model("no_such_role") == "main"


def test_table_rows_default_execute_think():
    """S2 JSON 断言：键集恰 {default,execute,think}，三值均 ∈ 六席位名。"""
    data = json.loads(model_pick.PICK_PATH.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"default", "execute", "think"}
    for seat in data.values():
        assert seat in SEAT_NAMES, seat


def test_execute_think_resolve_main():
    """S2 单测：execute/think/unknown 三路均 main（表初值三行全 main）。"""
    assert pick_model("execute") == "main"
    assert pick_model("think") == "main"
    assert pick_model("unknown") == "main"


def test_missing_table_still_main(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pick, "PICK_PATH", tmp_path / "absent.json")
    assert pick_model() == "main"  # 缺表兜底


def test_hot_reload_on_tmp_copy(monkeypatch, tmp_path):
    """S2 热加载实证（任务书限定：临时副本上做，仓库文件零触碰零残留）。

    副本初值三行 → 改 think→viewpoint_qwen → pick_model 现读盘读到新值
    （:38 每次调用 _load_table，无缓存）→ 复原＝monkeypatch 作用域结束。
    仓库真表在 monkeypatch 前后快照一致（零残留判据）。
    """
    real_path = model_pick.PICK_PATH
    real_before = real_path.read_text(encoding="utf-8")
    copy = tmp_path / "model_pick_copy.json"
    copy.write_text(json.dumps(
        {"default": "main", "execute": "main", "think": "main"}),
        encoding="utf-8")
    monkeypatch.setattr(model_pick, "PICK_PATH", copy)
    assert pick_model("think") == "main"
    # 热改：只改副本
    copy.write_text(json.dumps(
        {"default": "main", "execute": "main", "think": "viewpoint_qwen"}),
        encoding="utf-8")
    assert pick_model("think") == "viewpoint_qwen", "改表后应热读到新值"
    # 复原：改回 main
    copy.write_text(json.dumps(
        {"default": "main", "execute": "main", "think": "main"}),
        encoding="utf-8")
    assert pick_model("think") == "main"
    # 仓库真表零触碰（monkeypatch 作用域外字节一致）
    assert real_path.read_text(encoding="utf-8") == real_before


def test_unconfigured_type_logs_warning(monkeypatch, tmp_path, caplog):
    """S2 日志断言：查未配置类型 → 留痕行「未配置落 default」。"""
    copy = tmp_path / "mp.json"
    copy.write_text(json.dumps({"default": "main"}), encoding="utf-8")
    monkeypatch.setattr(model_pick, "PICK_PATH", copy)
    import logging
    with caplog.at_level(logging.WARNING, logger="thinkchain.model_pick"):
        assert pick_model("ghost_type") == "main"
    assert any("未配置落 default" in r.message and "ghost_type" in r.message
               for r in caplog.records)
