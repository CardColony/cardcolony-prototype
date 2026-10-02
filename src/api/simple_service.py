# -*- coding: utf-8 -*-
"""[phase9-E] simple 多模型编排器：观点并发 + 单模型统合。

设计依据（用户设计哲学）：模型只生成假设，确定性代码负责验证与裁决；
多模型出观点=群体审议，统合模型是唯一建卡/工具出口。

- 观点阶段：并发调 models.yaml simple.viewpoints 各模型，tools=None、
  跳过完成标记；单个失败记 meta 不中断；观点文本用 card_proposal
  剥离函数剥净协议块，另剥 <think> 推理块（MiniMax M 系实测全系带）。
- 统合阶段：user_prompt 追加参考观点段，走现有 call_main（带 tools）。
- enabled: false 或 viewpoints 空 → 直接 call_main（现行为，一行不多跑）。
- 观点全失败 → 统合独走原 user_prompt（等价现行为）。
- 上下文回流铁则：观点全文不进 session、不进 prior_messages（由 routes 保证）。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Optional

from . import model_service
from .card_proposal import parse_proposals

# [无内容预设改造 2026-10-02] 观点席不再追加任何身份/产出指令：
# 不用工具已由 tools=None 机械保证，协议块由 clean_viewpoint_text 机械剥离。
# 统合头中性化：只陈述事实，不含"可采纳可反驳"等处理方式指令。
VIEWPOINT_HEADER = "## 其他模型的输出"

# MiniMax M 系回复自带 <think>...</think> 推理块，确定性剥离
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def clean_viewpoint_text(text: str) -> str:
    """观点文本清洗（纯函数，可单测）：剥 <think> 块 + 剥建卡协议块。

    未闭合的 <think>（max_tokens 截断）整段不可用，返回空串。
    """
    text = _THINK_RE.sub("", text or "")
    if text.lstrip().startswith("<think>"):
        return ""
    clean, _proposals = parse_proposals(text)
    return clean.strip()


def build_viewpoint_prompt(user_prompt: str, ok_metas: list[dict[str, Any]]) -> str:
    """统合阶段 user_prompt 组装（纯函数，可单测）。ok_metas 空则原样返回。"""
    if not ok_metas:
        return user_prompt
    sections = "\n\n".join(
        f"### 观点·{m['model']}\n{m['text']}" for m in ok_metas
    )
    return f"{user_prompt}\n\n{VIEWPOINT_HEADER}\n{sections}"


async def _call_viewpoint(
    gw: Any,
    name: str,
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]],
) -> dict[str, Any]:
    """调单个观点模型。永不抛异常，失败记入 meta。

    meta: {model, ok, latency_ms, chars, error?, text?}——text 仅供
    统合与日志用，routes 返回响应前须剔除。
    """
    models_cfg = gw.config.get("models") or {}
    label = (models_cfg.get(name) or {}).get("model", name)
    meta: dict[str, Any] = {"model": label, "ok": False, "latency_ms": 0, "chars": 0}
    if name not in models_cfg:
        meta["error"] = f"models.yaml 无此模型条目: {name}"
        return meta
    t0 = time.monotonic()
    try:
        result = await gw.call(
            name,
            system_prompt,
            user_prompt,
            temperature=float(models_cfg[name].get("temperature", 0.7)),
            skip_completion_marker=True,
            tools=None,
            prior_messages=prior_messages or None,
        )
        meta["latency_ms"] = int(result.get("latency_ms") or (time.monotonic() - t0) * 1000)
        text = clean_viewpoint_text(result.get("content") or "")
        if not text:
            meta["error"] = "观点为空（内容缺失或全为思考/协议块）"
            return meta
        meta["ok"] = True
        meta["chars"] = len(text)
        meta["text"] = text
        return meta
    except Exception as exc:
        meta["latency_ms"] = int((time.monotonic() - t0) * 1000)
        meta["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return meta


async def call_simple(
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """simple 编排入口：返回 (gateway结果, 实际模型名, viewpoints_meta)。

    enabled: false 或 viewpoints 空时与 call_main 行为完全一致（meta=[]）。
    """
    gw = model_service.get_gateway()
    cfg = gw.config.get("simple") or {}
    viewpoints = list(cfg.get("viewpoints") or [])
    if not cfg.get("enabled") or not viewpoints:
        result, model_used = await model_service.call_main(
            system_prompt, user_prompt, prior_messages, tools=tools
        )
        return result, model_used, []

    # 观点阶段：并发，单个失败不中断
    # [拆闸裁定 2026-10-02] 原 viewpoint_timeout / viewpoint_max_tokens 已删除：
    # 思考型模型的额度被 max_tokens 烧光导致正文为空——闸就是空观点的根因
    raw_metas = await asyncio.gather(
        *[
            _call_viewpoint(gw, n, system_prompt, user_prompt,
                            prior_messages)
            for n in viewpoints
        ],
        return_exceptions=True,
    )
    metas: list[dict[str, Any]] = []
    for name, m in zip(viewpoints, raw_metas):
        if isinstance(m, BaseException):
            # _call_viewpoint 自吞异常，此处为 gather 双保险
            metas.append({"model": name, "ok": False, "latency_ms": 0,
                          "chars": 0, "error": f"{type(m).__name__}: {m}"})
        else:
            metas.append(m)

    ok_metas = [m for m in metas if m.get("ok")]
    final_user_prompt = build_viewpoint_prompt(user_prompt, ok_metas)

    # 统合阶段：唯一建卡/工具出口；失败由 gateway 内部 main→fallback 兜底
    result, model_used = await model_service.call_main(
        system_prompt, final_user_prompt, prior_messages, tools=tools
    )
    return result, model_used, metas
