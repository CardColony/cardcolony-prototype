# -*- coding: utf-8 -*-
"""llm_openai_compat · thinking 方言表（各厂思考参数形态）。

**这是全仓唯一一处「各厂差异」的代码落点**：加供应商 = 本表加一行 + models.yaml 加声明，
适配器/序列化代码结构零改动。

模板出处：
    - 原 `llm_deepseek/serialize.py` 的 `RequestDefaults` / `_resolve_thinking`（deepseek 方言）
    - 原 `src/api/model_config.py` 的 `_THINKING_DIALECT` / `_PROVIDER_DIALECT`
      （[2026-10-16 裁定 B] 期 3 从编译层迁走，真身现在这里）

各家形态与出处（派发端实测 / 官方文档）：

| vendor | 端点 | 思考参数形态 | 出处 |
|---|---|---|---|
| `opencode` / `deepseek` | opencode.ai / api.deepseek.com | 顶层 `thinking: {type:'enabled'\|'disabled'}` + `reasoning_effort` | llm_deepseek 原版（dsh）+ 10-16 实测 29-37 条 reasoning |
| `aliyun`（qwen） | token-plan…aliyuncs.com | 顶层 `enable_thinking: bool` | 派发端 10-16 实测（17 条 reasoning） |
| `zhipu`（glm） | open.bigmodel.cn | 顶层 `thinking: {type:'enabled'\|'disabled'}` | 派发端 10-16 实测（7 条 reasoning） |
| `minimax` | api.minimaxi.com | 顶层 `thinking: {type:'adaptive'\|'disabled'}` | **官方文档**（阿里云百炼 MiniMax-M3 页）：`adaptive`（默认，模型自判）/`disabled`；**无 `enabled` 取值** |
| `nous`（haiku） | inference-api.nousresearch.com | 顶层 `reasoning_effort` 档位 | **10-16 1 次真实请求**：HTTP 200，`reasoning_effort` 被接受；该次流仅 `delta.content`，无 `reasoning_content` |

★ minimax 结论：派发端「`thinking:{type:'adaptive'}` 未产思考」**不是形态错误**——
`adaptive` 是**模型自主判断**是否需要思考，简单提问即不思考（官方文档原文：
「自适应模式…模型自主判断是否需要思考，并输出推理过程（reasoning_content）」）。
故形态照文档锁 `adaptive`（开）/`disabled`（关）。

★ nous 结论：`reasoning_effort` 是合法顶层参数；原生思考字段名按**同构假设**取
`reasoning_content`（其余四家一致）。该次探明未触发思考，**字段名待端点支持后实测**
（已登记为遗留）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..llm.errors import LlmError

__all__ = ["ThinkingSpec", "DIALECTS", "dialect_for", "apply_thinking"]

_EFFORTS = ("off", "high", "max")


class ThinkingSpec:
    """一个模型的思考声明（来自 models.yaml，经编译 config 原样带下）。"""

    __slots__ = ("vendor", "thinking", "default_effort")

    def __init__(
        self,
        vendor: Optional[str] = None,
        thinking: Optional[str] = None,
        default_effort: Optional[str] = None,
    ) -> None:
        self.vendor = vendor
        self.thinking = _normalize(thinking)
        self.default_effort = default_effort


def _normalize(raw: Any) -> Optional[str]:
    """YAML 会把裸 ``off``/``on`` 解析成布尔；统一成字符串。非法值 → None（auto）。"""
    if raw is True:
        return "on"
    if raw is False:
        return "off"
    if isinstance(raw, str) and raw:
        return raw
    return None


def _is_effort(value: Optional[str]) -> bool:
    return value is not None and value not in ("auto", "on", "off")


# ---------------------------------------------------------------------------
# 各家方言：把思考意图写进请求体（顶层字段）
# ---------------------------------------------------------------------------
def _deepseek_fields(body: dict[str, Any], spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> None:
    """deepseek/opencode：顶层 ``thinking`` + ``reasoning_effort``。模板：llm_deepseek/serialize.py:37-53。"""
    if purpose == "session-title":
        body["thinking"] = {"type": "disabled"}
        return
    effort = spec.default_effort if requested is None else _validate_effort(requested)
    if spec.thinking in ("off", "disabled") and effort is not None and effort != "off":
        raise LlmError(
            f'DeepSeek deployment does not support reasoning effort "{effort}"',
            "UNSUPPORTED_REASONING_EFFORT",
        )
    if effort == "off":
        body["thinking"] = {"type": "disabled"}
    elif effort in ("high", "max"):
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = effort
    elif spec.thinking in ("on", "enabled"):
        body["thinking"] = {"type": "enabled"}
    elif spec.thinking in ("off", "disabled"):
        body["thinking"] = {"type": "disabled"}


def _qwen_fields(body: dict[str, Any], spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> None:
    """aliyun(qwen)：顶层 ``enable_thinking: bool``（实测有效）。"""
    on = _wants_thinking(spec, requested, purpose)
    if on is None:
        return
    body["enable_thinking"] = on


def _glm_fields(body: dict[str, Any], spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> None:
    """zhipu(glm)：顶层 ``thinking: {type: enabled|disabled}``（实测有效）。"""
    on = _wants_thinking(spec, requested, purpose)
    if on is None:
        return
    body["thinking"] = {"type": "enabled" if on else "disabled"}


def _minimax_fields(body: dict[str, Any], spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> None:
    """minimax：顶层 ``thinking: {type: adaptive|disabled}``（官方文档；**无 enabled**）。"""
    on = _wants_thinking(spec, requested, purpose)
    if on is None:
        return
    body["thinking"] = {"type": "adaptive" if on else "disabled"}


def _nous_fields(body: dict[str, Any], spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> None:
    """nous(haiku)：顶层 ``reasoning_effort`` 档位（10-16 1 次真实请求探明：被接受）。"""
    if purpose == "session-title":
        return
    effort = requested if requested is not None else (_specific_effort(spec.thinking) or spec.default_effort)
    if effort is not None and effort != "off":
        body["reasoning_effort"] = effort


def _flash_next_fields(body: dict[str, Any], spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> None:
    """flash-next（局域网 Flash-Next / Qwen 系）：顶层 ``reasoning_effort`` 档位。

    端点（192.168.9.126:6006，Strata + Flash-Next IQ3_S）接受
    ``none / low / medium / high``，没有 on/off 形态——关思考＝``none``（用户 2026-10-22
    部署信息：「重活建议 none 提速」）。默认档位写在 models.yaml 的
    ``thinking:`` / ``default_effort:`` 上。
    """
    if purpose == "session-title":
        return
    effort = requested if requested is not None else (
        _specific_effort(spec.thinking) or spec.default_effort)
    if effort in ("off", "auto", "on", "enabled", "disabled"):
        effort = "none"          # 本端点只有档位，没有开关；关思考＝none
    if effort:
        body["reasoning_effort"] = effort


#: 方舟 coding 端点（volcengine-coding）接受的 reasoning_effort 全档位。
_VOLCENGINE_EFFORTS = ("low", "medium", "high", "max")


def _volcengine_fields(body: dict[str, Any], spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> None:
    """volcengine-coding（方舟 coding 端点）：顶层 ``thinking`` 开关 ＋ ``reasoning_effort`` 全档位。

    2026-10-25 实测（kimi-k3-1-pro-261024， probes 全记录）：
      - 不传参数：默认思考开（reasoning_tokens≈283）
      - ``thinking:{type:"disabled"}``：reasoning_tokens=0、reasoning_content 消失——开关为真
      - ``reasoning_effort``：low/medium/high/max 全档位接受、效果递进（单样本
        low=160/medium=96/high=275/max=339，噪声存在但档位差可见）；非法值端点
        直接 InvalidParameter 报错（bogus 实测）⇒ 调用层传非法档位同样 fail-loud
      - ``thinking:{type:"enabled", budget_tokens:N}``：budget 不作硬卡（要 100 得 190）
        ⇒ 本方言不发 budget_tokens，强度只用品级档位表达
    """
    if purpose == "session-title":
        body["thinking"] = {"type": "disabled"}
        return
    if requested is not None and requested not in _VOLCENGINE_EFFORTS + ("off", "auto", "on", "enabled"):
        raise LlmError(
            f'方舟 coding 端点不支持 reasoning effort "{requested}"（可用: off/{"/".join(_VOLCENGINE_EFFORTS)}）',
            "UNSUPPORTED_REASONING_EFFORT",
        )
    effort = requested if requested is not None else (
        _specific_effort(spec.thinking) or spec.default_effort)
    if effort == "off":
        body["thinking"] = {"type": "disabled"}
    elif effort in _VOLCENGINE_EFFORTS:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = effort
    elif spec.thinking in ("off", "disabled"):
        body["thinking"] = {"type": "disabled"}
    elif spec.thinking in ("on", "enabled"):
        body["thinking"] = {"type": "enabled"}


def _validate_effort(effort: str) -> str:
    if effort in _EFFORTS:
        return effort
    raise LlmError(f'DeepSeek does not support reasoning effort "{effort}"', "UNSUPPORTED_REASONING_EFFORT")


def _specific_effort(thinking: Optional[str]) -> Optional[str]:
    """thinking 声明本身是具体档位（如 nous 的 ``max``）时返回它。"""
    return thinking if _is_effort(thinking) else None


def _wants_thinking(spec: ThinkingSpec, requested: Optional[str], purpose: Optional[str]) -> Optional[bool]:
    """bool 型方言的统一意图解析；None = 不表态（端点自然行为）。"""
    if purpose == "session-title":
        return False
    if requested == "off":
        return False
    if requested in ("high", "max"):
        return True
    if spec.thinking == "off":
        return False
    if spec.thinking in ("on", "enabled") or _is_effort(spec.thinking):
        return True
    if spec.thinking == "disabled":
        return False
    return None


DIALECTS: dict[str, Callable[[dict[str, Any], ThinkingSpec, Optional[str], Optional[str]], None]] = {
    "opencode": _deepseek_fields,
    "deepseek": _deepseek_fields,
    "aliyun": _qwen_fields,
    "zhipu": _glm_fields,
    "minimax": _minimax_fields,
    "nous": _nous_fields,
    # [局域网 Flash-Next 2026-10-23] 与 nous 同形（顶层 reasoning_effort），
    # 但档位取值是 none/low/medium/high；加供应商＝本表加一行 + models.yaml 加声明。
    "flash-next": _flash_next_fields,
    # [方舟 coding 2026-10-25] thinking 开关 + reasoning_effort 全档位（low/medium/
    # high/max），实测依据见 _volcengine_fields docstring。只注册 coding 端点——
    # token-plan 端点未探，不冒领。
    "volcengine-coding": _volcengine_fields,
}


def dialect_for(vendor: Optional[str]) -> Callable[..., None]:
    """按供应商取方言函数；未知/缺省 → deepseek 方言（模板行为）。"""
    return DIALECTS.get(str(vendor or ""), _deepseek_fields)


def apply_thinking(
    body: dict[str, Any],
    spec: ThinkingSpec,
    *,
    requested_effort: Optional[str] = None,
    purpose: Optional[str] = None,
) -> None:
    """把思考参数按供应商写进 ``body``（**顶层**，非 extra_body）。"""
    dialect_for(spec.vendor)(body, spec, requested_effort, purpose)
