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


def test_flash_next_dialect_uses_reasoning_effort_tiers():
    """flash-next（局域网 192.168.9.126:6006）：顶层 reasoning_effort，档位 none/low/medium/high。

    用户 2026-10-22 部署信息：端点接受 none/low/medium/high，没有 on/off 形态；
    重活建议 none 提速。models.yaml 里 `thinking: none` 即默认档位。
    """
    from src.model_gateway.llm_openai_compat.dialects import (
        ThinkingSpec, apply_thinking, dialect_for,
    )

    assert dialect_for("flash-next") is not dialect_for("nous"), "必须命中专属方言"

    body = {}
    apply_thinking(body, ThinkingSpec(vendor="flash-next", thinking="none"),
                   requested_effort=None, purpose=None)
    assert body.get("reasoning_effort") == "none", "默认档位来自 thinking: none"

    body2 = {}
    apply_thinking(body2, ThinkingSpec(vendor="flash-next", thinking="none"),
                   requested_effort="high", purpose=None)
    assert body2.get("reasoning_effort") == "high", "请求侧可覆盖"

    body3 = {}
    apply_thinking(body3, ThinkingSpec(vendor="flash-next"), requested_effort=None, purpose=None)
    assert "reasoning_effort" not in body3 and "thinking" not in body3, "未声明 ⇒ 不注入"


def test_flash_next_config_is_registered():
    """models.yaml 里必须真的登记了这台机器（provider/模型名/并发/占位鉴权头）。"""
    import io as _io
    from pathlib import Path

    import yaml

    cfg = yaml.safe_load(
        (_io.open(Path(__file__).resolve().parents[1] / "config" / "models.yaml",
                  encoding="utf-8")).read())
    prov = cfg["providers"]["flash-next"]
    assert prov["base_url"] == "http://192.168.9.126:6006/v1"
    assert prov["extra_headers"]["authorization"].startswith("Bearer "), \
        "免鉴权端点要给占位头（httpx 拒绝空 Bearer）"
    m = cfg["models"]["qwen38-flash-next"]
    assert m["model"] == "qwen3.8-flash-next-iq3_s"
    assert m["thinking"] == "none"
    assert cfg["concurrency"]["per_model"]["qwen38-flash-next"] == 2


def test_volcengine_coding_dialect_thinking_switch_and_effort_tiers():
    """volcengine-coding（方舟 coding 端点）：thinking 开关 + reasoning_effort 全档位。

    2026-10-25 实测（kimi-k3-1-pro-261024）：
      - thinking:{type:"disabled"} ⇒ reasoning_tokens 0、reasoning_content 消失（开关为真）
      - reasoning_effort：low/medium/high/max 全档位接受；bogus ⇒ 端点 InvalidParameter
      - 不声明 ⇒ 不发任何参数（端点默认思考开，行为不变）
    """
    from src.model_gateway.llm_openai_compat.dialects import (
        ThinkingSpec, apply_thinking, dialect_for,
    )
    from src.model_gateway.llm.errors import LlmError
    import pytest

    assert dialect_for("volcengine-coding") is not dialect_for("nous"), "必须命中专属方言"

    body = {}
    apply_thinking(body, ThinkingSpec(vendor="volcengine-coding", thinking="on",
                                      default_effort="high"),
                   requested_effort=None, purpose=None)
    assert body == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}

    for tier in ("low", "medium", "high", "max"):
        b = {}
        apply_thinking(b, ThinkingSpec(vendor="volcengine-coding", default_effort=tier),
                       requested_effort=None, purpose=None)
        assert b == {"thinking": {"type": "enabled"}, "reasoning_effort": tier}, tier

    off = {}
    apply_thinking(off, ThinkingSpec(vendor="volcengine-coding", thinking="off"),
                   requested_effort=None, purpose=None)
    assert off == {"thinking": {"type": "disabled"}}

    silent = {}
    apply_thinking(silent, ThinkingSpec(vendor="volcengine-coding"),
                   requested_effort=None, purpose=None)
    assert silent == {}, "不声明必须不表态（端点自然行为）"

    with pytest.raises(LlmError, match="不支持 reasoning effort"):
        apply_thinking({}, ThinkingSpec(vendor="volcengine-coding"),
                       requested_effort="bogus", purpose=None)
