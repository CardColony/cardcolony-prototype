# -*- coding: utf-8 -*-
"""网关兼容壳 / 单适配器注册 / MOCK_LLM 金样本 / 补丁零残留。

对应《OpenAI 兼容直连适配器》任务书 §七：判据 1（单适配器）、
判据 5（litellm/llm_deepseek 零残留）、判据 6（MOCK_LLM 金样本）。
（原判据 4「fallback 归因」随模型 fallback 机制于 2026-10-19 按用户裁定整体拆除。）
零真实调用、零网络（失败路径指向 127.0.0.1:1，连接立即被拒）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.infra.errors import ModelError
from src.model_gateway.gateway import ModelGateway, MockAdapter, build_gateway

_ROOT = Path(__file__).resolve().parents[1]


def _cfg(**models):
    return {"models": models, "retry_grading": {"enabled": False}}


def _dead_model(vendor="zhipu", wire="m"):
    return {
        "vendor": vendor,
        "provider": "openai",
        "model": wire,
        "api_key_env": "K",
        "base_url": "http://127.0.0.1:1/v1",
        "retry": {"max_attempts": 1, "backoff_seconds": 0},
    }


# ---------------- 判据 1：单适配器 ----------------

def test_only_one_concrete_adapter_class():
    """适配器类只有 LlmAdapter(ABC) / MockAdapter / OpenAICompatAdapter 三个。"""
    names = []
    for path in (_ROOT / "src" / "model_gateway").rglob("*.py"):
        names += re.findall(r"^class (\w+Adapter)\(", path.read_text(encoding="utf-8"), re.M)
    assert sorted(names) == ["LlmAdapter", "MockAdapter", "OpenAICompatAdapter"]


def test_sse_and_translate_have_single_copy():
    """sse.py / translate.py 全仓（model_gateway 下）各仅一份。"""
    for name in ("sse.py", "translate.py"):
        hits = [p for p in (_ROOT / "src" / "model_gateway").rglob(name)]
        assert len(hits) == 1, [str(h) for h in hits]


def test_legacy_packages_are_gone():
    assert not (_ROOT / "src" / "model_gateway" / "llm_deepseek").exists()
    assert not (_ROOT / "src" / "model_gateway" / "llm_litellm").exists()
    assert not (_ROOT / "src" / "model_gateway" / "stats.py").exists()


def test_no_litellm_import_residue():
    """判据 5：src/ 下无 litellm import 残留（注释/历史说明允许）。"""
    offenders = []
    for path in (_ROOT / "src").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "litellm" in stripped and not stripped.startswith("#") and "import" in stripped:
                offenders.append(f"{path}: {stripped}")
    assert offenders == []


def test_native_sse_patch_symbols_are_gone():
    banned = ("NATIVE_SSE_PROVIDERS", "_native_sse_chunks", "_is_native_sse_url")
    offenders = []
    for path in (_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path}:{s}" for s in banned if s in text]
    assert offenders == []


# ---------------- 注册面 ----------------

def test_adapters_registered_by_vendor(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    gw = build_gateway(_cfg(
        a={"vendor": "zhipu", "provider": "openai", "model": "glm-5.3", "api_key_env": "K"},
        b={"vendor": "aliyun", "provider": "openai", "model": "qwen3.8-max", "api_key_env": "K"},
        c={"vendor": "zhipu", "provider": "openai", "model": "glm-5.3-flash", "api_key_env": "K"},
    ))
    assert sorted(gw._runtime.registered_providers()) == ["aliyun", "zhipu"]
    # 同一 vendor 多模型共用一个适配器实例
    zhipu_adapter = gw._runtime._registration("zhipu").adapter
    assert gw._runtime._registration("aliyun").adapter is zhipu_adapter


def test_build_options_uses_vendor_as_provider(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    gw = build_gateway(_cfg(a={"vendor": "minimax", "provider": "openai", "model": "MiniMax-M3.1", "api_key_env": "K"}))
    opts = gw._build_options("a", "S", "U", 0.7, None, None, None, None, None, stream=True)
    assert opts.provider == "minimax"
    assert opts.model == "MiniMax-M3.1"


def test_build_gateway_keeps_config_surface(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    gw = build_gateway(_cfg(m={"vendor": "nous", "provider": "openai", "model": "fake/m", "api_key_env": "K"}))
    assert gw.config["models"]["m"]["model"] == "fake/m"
    assert gw.stats is gw._runtime.stats
    assert callable(gw.call) and callable(gw.stream_call)


# ---------------- 判据 4（已拆）：fallback 归因随机制拆除于 2026-10-19 ----------------
# 原两条测试（test_fallback_preserves_both_errors / test_fallback_status_event_still_emitted）
# 随 _fallback_or_raise 一并删除。重试耗尽即 ModelError 的行为由
# test_llm_errors / test_llm_retry 覆盖。


# ---------------- MOCK_LLM 金样本 ----------------

async def test_mock_llm_call_golden(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    gw = build_gateway(_cfg(main={"provider": "openai", "model": "fake-main"}))
    result = await gw.call("main", "SYS", "用户提问")
    assert result["content"] == "[mock:main] 用户提问"
    assert result["finish_reason"] == "stop"
    assert result["tool_calls"] == []


async def test_mock_llm_stream_call_golden(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    gw = build_gateway(_cfg(main={"provider": "openai", "model": "fake-main"}))
    events = [ev async for ev in gw.stream_call("main", "SYS", "问题")]
    chunks = [e for e in events if e.get("chunk_type") == "content"]
    assert "".join(c["text"] for c in chunks) == "[mock:main] 问题"
    assert all(c["chunk"] == c["text"] for c in chunks)
    assert events[-1]["content"] == "[mock:main] 问题"


def test_mock_adapter_is_a_llm_adapter():
    from src.model_gateway.llm.runtime import LlmAdapter

    assert isinstance(MockAdapter(), LlmAdapter)


# ---------------- 编译面（B：编译层不翻译） ----------------

def test_compile_no_longer_translates_thinking():
    from src.api import model_config

    compiled = model_config.compile(
        {
            "providers": {"minimax": {"protocol": "openai", "base_url": "https://api.minimaxi.com/v1"}},
            "models": {"mm": {"provider": "minimax", "model": "MiniMax-M3.1", "thinking": "on"}},
        }
    )
    entry = compiled["models"]["mm"]
    assert entry.get("vendor") == "minimax"
    assert "extra_body" not in entry
    assert "reasoning_effort" not in entry
    assert entry.get("thinking") == "on"
