# -*- coding: utf-8 -*-
"""llm_openai_compat · 方言表单测（每家一条「thinking 声明 → 请求体形态」断言）。

判据 2。qwen/glm 按派发端实测形态；minimax 按官方文档形态；nous 按 1 次真实请求探明结论。
零真实调用、零网络。
"""
from __future__ import annotations

from src.model_gateway.llm.types import GenerateOptions, TextBlock
from src.model_gateway.llm_openai_compat.dialects import ThinkingSpec, apply_thinking, dialect_for
from src.model_gateway.llm_openai_compat.serialize import RequestDefaults, serialize_request


def _body(vendor, thinking=None, default_effort=None, **kw):
    body = {"model": "m", "messages": []}
    apply_thinking(body, ThinkingSpec(vendor=vendor, thinking=thinking, default_effort=default_effort), **kw)
    return body


# ---------------- aliyun（qwen）：顶层 enable_thinking: bool（实测） ----------------

def test_qwen_dialect_uses_top_level_enable_thinking():
    assert _body("aliyun", "on")["enable_thinking"] is True
    assert _body("aliyun", "off")["enable_thinking"] is False
    # 未表态（auto）→ 不注入（端点自然行为）
    assert "enable_thinking" not in _body("aliyun", "auto")


# ---------------- zhipu（glm）：顶层 thinking: {type}（实测） ----------------

def test_glm_dialect_uses_thinking_type():
    assert _body("zhipu", "on")["thinking"] == {"type": "enabled"}
    assert _body("zhipu", "off")["thinking"] == {"type": "disabled"}
    assert "thinking" not in _body("zhipu", "auto")


# ---------------- minimax：顶层 thinking: {type: adaptive|disabled}（官方文档） ----------------

def test_minimax_dialect_uses_adaptive_not_enabled():
    """官方文档只有 adaptive/disabled（**无 enabled**）；adaptive = 模型自判是否思考。"""
    assert _body("minimax", "on")["thinking"] == {"type": "adaptive"}
    assert _body("minimax", "off")["thinking"] == {"type": "disabled"}
    assert "thinking" not in _body("minimax", "auto")


# ---------------- nous（haiku）：顶层 reasoning_effort 档位 ----------------

def test_nous_dialect_passes_reasoning_effort():
    # thinking 声明本身就是档位（models.yaml: thinking: max）
    assert _body("nous", "max")["reasoning_effort"] == "max"
    # 无 thinking 档位时回落到 default_effort
    assert _body("nous", None, default_effort="medium")["reasoning_effort"] == "medium"
    # 不注入 thinking 键
    assert "thinking" not in _body("nous", "max")


# ---------------- opencode/deepseek：顶层 thinking + reasoning_effort ----------------

def test_deepseek_dialect_uses_thinking_and_effort():
    body = _body("opencode", "on", default_effort="high")
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert _body("opencode", "off")["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in _body("opencode", "off")


def test_unknown_vendor_falls_back_to_deepseek_dialect():
    assert dialect_for("nope") is dialect_for("opencode")
    assert _body("nope", "on")["thinking"] == {"type": "enabled"}


# ---------------- 会话标题用途强制关思考 ----------------

def test_session_title_forces_thinking_off():
    assert _body("opencode", "on", purpose="session-title")["thinking"] == {"type": "disabled"}
    assert _body("aliyun", "on", purpose="session-title")["enable_thinking"] is False
    assert "reasoning_effort" not in _body("nous", "max", purpose="session-title")


# ---------------- serialize_request 端到端（方言经 RequestDefaults 生效） ----------------

def _options(**kw):
    return GenerateOptions(provider="aliyun", model="qwen3.8-max", messages=(), **kw)


def test_serialize_request_applies_dialect_from_defaults():
    body = serialize_request(
        _options(), RequestDefaults(vendor="aliyun", thinking="on")
    )
    assert body["enable_thinking"] is True
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_serialize_request_glm_and_minimax_shapes():
    glm = serialize_request(_options(), RequestDefaults(vendor="zhipu", thinking="on"))
    assert glm["thinking"] == {"type": "enabled"}
    mm = serialize_request(_options(), RequestDefaults(vendor="minimax", thinking="on"))
    assert mm["thinking"] == {"type": "adaptive"}
    nous = serialize_request(_options(), RequestDefaults(vendor="nous", thinking="max"))
    assert nous["reasoning_effort"] == "max"
