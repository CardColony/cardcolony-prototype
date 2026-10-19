# -*- coding: utf-8 -*-
"""模型设置·第二步 —— model_config 改动单测。

覆盖三件事（对应任务书步骤 2）：
1. save() 保头：头 1-13 行「拆闸铁律」保存后逐字未变（验收⑥的兜底）
2. compile() 公开包装可用（设置界面「先验后写」要靠它预检构造）
3. _NON_GATEWAY_KEYS 弹出：编译后模型条目键集无 thinking/note/label/models_url（验收⑦）
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import model_config  # noqa: E402

RAW = {
    "providers": {
        "nous": {
            "protocol": "openai",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "NOUS_API_KEY",
            "models_url": "https://example.invalid/v1/models",
            "note": "Nous Research（OpenAI 兼容协议）",
        },
        "deepseek": {
            "protocol": "deepseek",
            "api_key_env": "DEEPSEEK_API_KEY",
            "note": "DeepSeek 官方",
        },
    },
    "models": {
        "haiku": {
            "provider": "nous",
            "model": "anthropic/claude-haiku-5.5",
            "temperature": 0.3,
            "thinking": "auto",
            "note": "主模型",
            "label": "主模型",
            "retry": {"max_attempts": 5, "backoff_seconds": 2},
        },
        "minimax-m3": {
            "provider": "deepseek",
            "model": "MiniMax-M3",
            "temperature": 0.7,
            "thinking": "off",
        },
    },
    "bindings": {"execute": "haiku"},
    "system": {"default_model": "haiku"},
    "simple": {"enabled": True},
    "retry_grading": {"enabled": False},
}

HEADER = (
    "# ============================================================\n"
    "# 模型配置 · 唯一真源（模型设置 · 第一步收拢）\n"
    "#\n"
    "# 历史裁定（逐字保留，拆闸铁律）：\n"
    "#   2026-10-02 拆闸裁定：不允许设置任意 max_tokens，不允许墙钟类闸。\n"
    "#     - 所有 max_tokens / timeout 全部移除（None = 不传）\n"
    "# ============================================================\n"
)


# ---------- ① save() 保头 ----------


def test_split_header_cuts_before_first_top_key():
    text = HEADER + "providers:\n  nous:\n    protocol: openai\n"
    head, body = model_config._split_header(text)
    assert head == HEADER
    assert body.startswith("providers:")


def test_split_header_no_top_key_is_all_header():
    assert model_config._split_header(HEADER) == (HEADER, "")


def test_save_preserves_header_byte_for_byte(tmp_path, monkeypatch):
    """保头验收：保存后头部注释块与原文件逐字相同，且正文接在其后。"""
    cfg_path = tmp_path / "models.yaml"
    cfg_path.write_text(HEADER + "providers:\n  old:\n    protocol: openai\n", encoding="utf-8")
    monkeypatch.setattr(model_config, "CONFIG_PATH", cfg_path)

    model_config.save(RAW)

    after = cfg_path.read_text(encoding="utf-8")
    assert after.startswith(HEADER), "头部注释块必须逐字保留"
    head, body = model_config._split_header(after)
    assert head == HEADER
    assert "providers:" in body


def test_save_first_time_without_existing_file(tmp_path, monkeypatch):
    """无旧文件时（首次保存）不报错，照常写正文。"""
    cfg_path = tmp_path / "models.yaml"
    monkeypatch.setattr(model_config, "CONFIG_PATH", cfg_path)
    model_config.save(RAW)
    assert "providers:" in cfg_path.read_text(encoding="utf-8")


def test_save_roundtrips_structure(tmp_path, monkeypatch):
    cfg_path = tmp_path / "models.yaml"
    cfg_path.write_text(HEADER, encoding="utf-8")
    monkeypatch.setattr(model_config, "CONFIG_PATH", cfg_path)
    model_config.save(RAW)

    import yaml
    back = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert back["providers"].keys() == RAW["providers"].keys()
    assert back["models"]["haiku"]["model"] == "anthropic/claude-haiku-5.5"


# ---------- ② compile() 公开包装 ----------


def test_compile_public_wrapper_matches_load():
    cfg = model_config.compile(RAW)
    assert cfg["models"]["haiku"]["model"] == "anthropic/claude-haiku-5.5"
    assert cfg["simple"] == {"enabled": True}


# ---------- ③ 非网关字段不泄漏（验收⑦） ----------


def test_non_gateway_keys_absent_from_compiled_entries():
    cfg = model_config.compile(RAW)
    for mname, entry in cfg["models"].items():
        for leaked in model_config._NON_GATEWAY_KEYS:
            assert leaked not in entry, f"模型 {mname} 泄漏了 {leaked}"


def test_provider_level_ui_fields_do_not_leak_to_children():
    """供应商级 note/models_url 不该被继承进模型条目。"""
    cfg = model_config.compile(RAW)
    entry = cfg["models"]["haiku"]
    assert "note" not in entry
    assert "models_url" not in entry


def test_thinking_not_translated_at_compile_dialect_locks_behaviour():
    """[2026-10-16 裁定 B · 双断言] 编译层**不再翻译** thinking 方言（翻译归适配器方言表）；
    行为由 ``llm_openai_compat.dialects`` 锁住。"""
    from src.model_gateway.llm_openai_compat.dialects import ThinkingSpec, apply_thinking

    cfg = model_config.compile({
        "providers": {"minimax": {"protocol": "openai", "base_url": "https://x.invalid/v1",
                                  "api_key_env": "K"}},
        "models": {"minimax-m3": {"provider": "minimax", "model": "MiniMax-M3", "thinking": "off"}},
        "system": {},
    })
    entry = cfg["models"]["minimax-m3"]
    # ① 编译层不翻译：无 extra_body / reasoning_effort；thinking 声明与 vendor 原样带下
    assert "extra_body" not in entry
    assert "reasoning_effort" not in entry
    assert entry["thinking"] == "off"
    assert entry["vendor"] == "minimax"
    # ② 适配器层锁行为：minimax 方言产出 adaptive|disabled（官方文档形态）
    body: dict = {}
    apply_thinking(body, ThinkingSpec(vendor=entry["vendor"], thinking=entry["thinking"]))
    assert body["thinking"] == {"type": "disabled"}


def test_thinking_off_as_yaml_bool_dialect_still_off():
    """YAML 裸 off → False：编译层原样带下（不翻译），适配器方言仍判为「关」。"""
    from src.model_gateway.llm_openai_compat.dialects import ThinkingSpec, apply_thinking

    cfg = model_config.compile({
        "providers": {"aliyun": {"protocol": "openai", "base_url": "https://x.invalid/v1",
                                 "api_key_env": "K"}},
        "models": {"qwen-max": {"provider": "aliyun", "model": "qwen3.8-max", "thinking": False}},
        "system": {},
    })
    entry = cfg["models"]["qwen-max"]
    # ① 编译层不翻译
    assert "extra_body" not in entry
    assert entry["thinking"] is False
    assert entry["vendor"] == "aliyun"
    # ② 适配器层：qwen 方言 → enable_thinking False
    body: dict = {}
    apply_thinking(body, ThinkingSpec(vendor=entry["vendor"], thinking=entry["thinking"]))
    assert body["enable_thinking"] is False


def test_no_fallback_entry_compiled():
    """[2026-10-19 产品裁定] 无降级模型：编译产物里不应再有 "fallback" 条目。"""
    cfg = model_config.compile(RAW)
    assert "fallback" not in cfg["models"]


def test_real_config_file_compiles_clean():
    """对仓库真实 config/models.yaml 跑一遍：任何模型条目都不得带 UI 字段。"""
    real = model_config.BASE_DIR / "config" / "models.yaml"
    if not real.exists():
        pytest.skip("真实配置不存在")
    import yaml
    raw = yaml.safe_load(real.read_text(encoding="utf-8"))
    cfg = model_config.compile(raw)
    for mname, entry in cfg["models"].items():
        assert not (model_config._NON_GATEWAY_KEYS & set(entry)), f"{mname} 泄漏"