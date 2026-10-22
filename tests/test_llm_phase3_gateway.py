# -*- coding: utf-8 -*-
"""网关兼容壳 / 单适配器注册 / MOCK_LLM 金样本 / fallback 归因 / 补丁零残留。

对应《OpenAI 兼容直连适配器》任务书 §七：判据 1（单适配器）、判据 4（fallback 归因）、
判据 5（litellm/llm_deepseek 零残留）、判据 6（MOCK_LLM 金样本）。
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
    """具体适配器类＝ LlmAdapter(ABC) / MockAdapter / OpenAICompatAdapter / AnthropicMessagesAdapter。

    [2026-10-22 mavis 接入] 原判据字面是「只有三个类」，其**目的**是消灭
    litellm 时代「同一协议多套实现」的乱象（deepseek 适配器＋litellm 壳＋
    compat 三套并行讲同一种 OpenAI 协议）。mavis 网关（MiniMax Code）**只讲
    Anthropic Messages 协议**（chat/completions 形状实测 400），无法由
    OpenAICompatAdapter 承担，故按「**每个协议至多一个具体适配器**」的目的
    口径扩容：OpenAI 协议包一个、Anthropic 协议包一个。
    """
    names = []
    for path in (_ROOT / "src" / "model_gateway").rglob("*.py"):
        names += re.findall(r"^class (\w+Adapter)\(", path.read_text(encoding="utf-8"), re.M)
    assert sorted(names) == [
        "AnthropicMessagesAdapter",
        "LlmAdapter",
        "MockAdapter",
        "OpenAICompatAdapter",
    ]
    # 目的口径的加强版：任何协议包内不得出现两个以上具体适配器（防换皮 duplication）
    by_package: dict[str, list[str]] = {}
    for path in (_ROOT / "src" / "model_gateway").rglob("*.py"):
        found = re.findall(r"^class (\w+Adapter)\(", path.read_text(encoding="utf-8"), re.M)
        concrete = [n for n in found if n not in ("LlmAdapter", "MockAdapter")]
        if concrete:
            by_package.setdefault(str(path.parent), []).extend(concrete)
    for package, classes in by_package.items():
        assert len(classes) == 1, f"{package} 出现多个具体适配器: {classes}"


def test_sse_and_translate_have_single_copy():
    """sse.py / translate.py 在每个协议包内仅一份（全仓两份＝两个协议包各一）。

    [2026-10-22 mavis 接入] 原判据「全仓仅一份」的目的同样是防同协议多实现。
    Anthropic 的 SSE 事件与 OpenAI 的 choices[0].delta 是两套 wire，不存在
    共享解析；故按「**包内单份**」口径允许 llm_openai_compat/ 与
    llm_anthropic/ 各一份，但任何一包内不得重复、也不得出现在协议包之外。
    """
    allowed = {
        "sse.py": {
            str(_ROOT / "src" / "model_gateway" / "llm_openai_compat"),
            str(_ROOT / "src" / "model_gateway" / "llm_anthropic"),
        },
        "translate.py": {
            str(_ROOT / "src" / "model_gateway" / "llm_openai_compat"),
            str(_ROOT / "src" / "model_gateway" / "llm_anthropic"),
        },
    }
    for name, allowed_packages in allowed.items():
        hits: dict[str, list] = {}
        for path in (_ROOT / "src" / "model_gateway").rglob(name):
            hits.setdefault(str(path.parent), []).append(path)
        for package, files in hits.items():
            assert len(files) == 1, f"{package} 出现多份 {name}: {files}"
        assert set(hits) <= allowed_packages, f"{name} 出现在非协议包目录: {list(hits)}"


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


# ---------------- 模型设置 2026-10-19 裁定：无降级模型 ----------------

async def test_no_fallback_on_failure(monkeypatch):
    """无降级模型：主模型失败重试耗尽后直接上抛，不做 fallback 切换。"""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setenv("K", "sk-fake")
    gw = build_gateway({
        "models": {"main": _dead_model("zhipu", "glm-5.3")},
        "retry_grading": {"enabled": False},
    })
    with pytest.raises(ModelError) as exc:
        await gw.call("main", "S", "U")
    assert "重试" in str(exc.value)
    assert "降级" not in str(exc.value)


async def test_no_fallback_phase_event(monkeypatch):
    """无降级模型：失败事件流里不再出现 fallback 阶段。"""
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setenv("K", "sk-fake")
    gw = build_gateway({
        "models": {"main": _dead_model("zhipu", "glm-5.3")},
        "retry_grading": {"enabled": False},
    })
    seen: list[dict] = []
    with pytest.raises(ModelError):
        await gw.call("main", "S", "U", on_status=seen.append)
    phases = [e["phase"] for e in seen]
    assert "fallback" not in phases


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
