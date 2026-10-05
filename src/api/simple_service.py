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
import logging
import re
import time
from typing import Any, Optional

from . import model_service
from .card_proposal import parse_proposals

logger = logging.getLogger("api.simple_service")

# [无内容预设改造 2026-10-02] 统合头中性化：只陈述事实，不含"可采纳可反驳"等处理方式指令。
# [职责分离裁定 2026-10-02 15:07] 注入段去掉模型名（模型名诱发统合官的裁决者姿态），
# 观点按序号标注；模型名只留在 meta/log 供审计，不进统合官输入。
VIEWPOINT_HEADER = "## 其他模型的输出"

# [T10-6-1/6-2] 任务说明定稿句（逐字）：插在 VIEWPOINT_HEADER 之后、三段分析之前，
# 给统合官划清「参考材料」与「用户问题」的界线（V8 判据）。
# 段名「## 其他模型的输出」保持逐字不改（6-3 既有契约）；三段匿名形态不动（6-4，用户已裁定）。
VIEWPOINT_TASK_NOTE = "以下是三份独立分析的参考材料，不是用户的问题。你的任务是回答上面用户提出的问题。"

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
    """统合阶段 user_prompt 组装（纯函数，可单测）。ok_metas 空则原样返回。

    [职责分离裁定 2026-10-02] 观点按序号标注，不带模型名——
    模型名只留 meta/log 供审计，不进统合官输入（防裁决者姿态诱导）。
    """
    if not ok_metas:
        return user_prompt
    sections = "\n\n".join(
        f"### 输出 {i + 1}\n{m['text']}" for i, m in enumerate(ok_metas)
    )
    # [T10-6-1] 任务说明行插在 VIEWPOINT_HEADER 之后、三段分析之前
    return f"{user_prompt}\n\n{VIEWPOINT_HEADER}\n{VIEWPOINT_TASK_NOTE}\n{sections}"


async def _call_viewpoint(
    gw: Any,
    name: str,
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]],
    on_done: Optional[Any] = None,
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
        if on_done:
            try:
                on_done(meta)
            except Exception:
                pass
        return meta
    except Exception as exc:
        meta["latency_ms"] = int((time.monotonic() - t0) * 1000)
        meta["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        if on_done:
            try:
                on_done(meta)
            except Exception:
                pass
        return meta


async def call_simple(
    system_prompt: str,
    vp_system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    on_progress: Optional[Any] = None,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """simple 编排入口：返回 (gateway结果, 实际模型名, viewpoints_meta)。

    [职责分离裁定 2026-10-02 15:07] 三思考席只做分析：
    vp_system_prompt 为中性上下文（无使用说明、无工具、无建卡接口）；
    system_prompt（含使用说明与建卡接口）只给主模型——拆卡是主模型的工作。

    on_progress(stage, detail)：实时反馈钩子（纯展示层，不进模型输入）。

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

    def _report(stage: str, detail: str = "") -> None:
        if on_progress:
            try:
                on_progress(stage, detail)
            except Exception:
                pass

    # 观点阶段：并发，单个失败不中断
    # [拆闸裁定 2026-10-02] 原 viewpoint_timeout / viewpoint_max_tokens 已删除：
    # 思考型模型的额度被 max_tokens 烧光导致正文为空——闸就是空观点的根因
    _report("viewpoints", f"0/{len(viewpoints)}")

    done_count = [0]

    def _vp_done(meta: dict[str, Any]) -> None:
        done_count[0] += 1
        mark = "✓" if meta.get("ok") else "✗"
        secs = round((meta.get("latency_ms") or 0) / 1000, 1)
        _report("viewpoints", f"{done_count[0]}/{len(viewpoints)} · {mark}{meta.get('model','')} {secs}s")

    raw_metas = await asyncio.gather(
        *[
            _call_viewpoint(gw, n, vp_system_prompt, user_prompt,
                            prior_messages, on_done=_vp_done)
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
    if ok_metas:
        # [T10-6] 打点：统合官输入处——任务说明行已插入
        logger.info("统合官输入已插入任务说明行 viewpoints=%d", len(ok_metas))

    # 统合阶段：唯一建卡/工具出口；失败由 gateway 内部 main→fallback 兜底
    _report("synthesize", "统合推理中")
    result, model_used = await model_service.call_main(
        system_prompt, final_user_prompt, prior_messages, tools=tools
    )
    return result, model_used, metas
