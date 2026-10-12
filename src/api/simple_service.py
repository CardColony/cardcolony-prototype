# -*- coding: utf-8 -*-
"""[phase9-E] simple 多模型编排器：观点并发 + 单模型统合。

[思维链真实落地 2026-10-11] 本件收敛为**提交轮三席统合**的席位层（C3/C6）：
三席（run_viewpoints）与统合官由 message_runner.run_seats 在 board 收口后
调用——三席输入＝全部步 result＋判据满足情况＋工具结果全文，质询「成果真
满足判据了吗、解决用户问题了吗」；统合官 call_main 统合交付。旧观点前置
编排（call_simple）与旧后置注入件（build_informed_prompt）随三席旁路机制
整体退役。

设计依据（用户设计哲学）：模型只生成假设，确定性代码负责验证与裁决；
多模型出观点=群体审议，统合模型是唯一建卡/工具出口。

- 观点阶段（run_viewpoints）：并发调 models.yaml simple.viewpoints 各模型，tools=None、
  跳过完成标记；单个失败记 meta 不中断；观点文本用 card_proposal
  剥离函数剥净协议块，另剥 <think> 推理块（MiniMax M 系实测全系带）。
- 注入（build_review_prompt）：步成果（result＋criteria＋criteria_met）
  与工具结果全文进三席输入（不折叠不截断）。
- 统合阶段：user_prompt 追加参考观点段（build_viewpoint_prompt），走
  call_main（带 tools）。
- enabled: false 或 viewpoints 空 → 三席零调用（run_viewpoints 返回 ([], [])）。
- 观点全失败 → 留痕（warning），主模型携步成果独自交付（口径②）。
- 上下文回流铁则：观点全文不进 session、不进 prior_messages（由 runner 保证）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

from . import model_service
from .card_proposal import parse_proposals
from ..thinkchain import model_pick

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


# ============================================================
# [思维链真实落地 2026-10-11] 提交轮三席质询输入（C3/C6）
# ============================================================

REVIEW_HEADER = "## 本轮计划执行材料"
REVIEW_TASK_NOTE = (
    "上面的任务已按计划逐步执行完毕。请独立质询三问：\n"
    "1. 每一步的成果真满足该步的完成判据了吗？（对照 criteria 逐条核）\n"
    "2. 全部成果合起来，真解决了用户的问题吗？\n"
    "3. 有没有谎报完成——声称已落盘/已完成，但材料不支持？\n"
    "对每问给出结论：通过／不通过＋一句理由。先读下面的执行材料再判断。"
)


def build_review_prompt(user_prompt: str,
                        steps_record: list[dict[str, Any]],
                        criteria_mets: list[str],
                        tool_results: list[str]) -> str:
    """提交轮三席质询输入组装（纯函数，可单测）。

    [C6] 输入＝用户原问题＋全部步的 result 与判据满足情况（steps_record
    每步含 prompt/criteria/deliverable/status/result；criteria_mets＝模型
    每次 step_complete 的 criteria_met 自述，按调用序与步对齐）＋本轮工具
    结果全文（不折叠不截断）。空表 → 原样返回 user_prompt。
    """
    if not steps_record and not tool_results:
        return user_prompt
    sections: list[str] = []
    for i, s in enumerate(steps_record):
        if not isinstance(s, dict):
            continue
        met = criteria_mets[i] if i < len(criteria_mets) else "（无自述）"
        sections.append(
            f"### 步 {s.get('id', i + 1)} [{s.get('status', '')}]\n"
            f"要求: {s.get('prompt', '')}\n"
            f"完成判据: {s.get('criteria', '')}\n"
            f"产出物声明: {s.get('deliverable', '')}\n"
            f"成果(result): {(s.get('result') or '').strip() or '（无）'}\n"
            f"模型自述判据满足: {met}")
    if tool_results:
        sections.append("### 本轮工具查证结果\n" + "\n\n".join(tool_results))
    return f"{user_prompt}\n\n{REVIEW_HEADER}\n{REVIEW_TASK_NOTE}\n" + "\n\n".join(sections)


async def run_viewpoints(
    vp_system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    on_progress: Optional[Any] = None,
    on_seat: Optional[Any] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """三席观点并发（可独立调用，不与 call_simple 绑定）。返回 (ok_metas, 全量 metas)。

    [E-探针制] 自 call_simple 拆出，供 routes 在工具循环收尾处调用——
    三席踩着工具事实出观点。单席语义不变：tools=None（10-02 零工具裁定，
    三席不出现 tools 入参）、skip_completion_marker、失败记 meta 不中断。
    [E-口径②] 全败留痕（warning）不阻断，由调用方决定独自交付。
    enabled: false 或 viewpoints 空 → ([], [])（零调用）。
    [信息流 Task3 2026-10-07] 增可选参 on_seat(kind, meta)（缺省 None 零行为
    变化）：每席起止转发事件流（seat_start/seat_end），只读不判定、异常静默。
    """
    gw = model_service.get_gateway()
    cfg = gw.config.get("simple") or {}
    viewpoints = list(cfg.get("viewpoints") or [])
    if not cfg.get("enabled") or not viewpoints:
        return [], []

    def _report(stage: str, detail: str = "") -> None:
        if on_progress:
            try:
                on_progress(stage, detail)
            except Exception:
                pass

    # [拆闸裁定 2026-10-02] 无 viewpoint_timeout / max_tokens：闸就是空观点的根因
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
                            prior_messages, on_done=_vp_done, on_seat=on_seat)
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
    if viewpoints and not ok_metas:
        logger.warning("[E] 三席观点全败（%d 席尝试，留痕不阻断）", len(metas))
    return ok_metas, metas


async def _call_viewpoint(
    gw: Any,
    name: str,
    system_prompt: str,
    user_prompt: str,
    prior_messages: Optional[list[dict[str, Any]]],
    on_done: Optional[Any] = None,
    on_seat: Optional[Any] = None,
) -> dict[str, Any]:
    """调单个观点模型。永不抛异常，失败记入 meta。

    meta: {model, ok, latency_ms, chars, error?, text?}——text 仅供
    统合与日志用，routes 返回响应前须剔除。
    [信息流 Task3 2026-10-07] on_seat(kind, meta) 每席起止转发（缺省 None
    零行为变化；异常静默）。
    """
    models_cfg = gw.config.get("models") or {}
    # [迭代五 S2·E9] 席位模型可配：resolve_model(name) 依 model_pick.json 取
    # 覆盖值（键集校验、非法值回落 main、缺省态返 main）。name 仍是 models.yaml
    # 的席位名（viewpoint_minimax 等），seat 是实际调用的模型席位——四处随 seat 走。
    seat = model_pick.resolve_model(name)
    # [S2 保留] 存在性检查转 seat 双保险（resolve_model 依法只返 main 或合法键，
    # 此检查照字面不再触发、保留无害）。
    label = (models_cfg.get(seat) or {}).get("model", seat)
    meta: dict[str, Any] = {"model": label, "ok": False, "latency_ms": 0, "chars": 0}
    if seat not in models_cfg:
        meta["error"] = f"models.yaml 无此模型条目: {seat}"
        return meta
    if on_seat:
        try:
            on_seat("seat_start", {"model": label})
        except Exception:
            pass
    t0 = time.monotonic()
    try:
        result = await gw.call(
            seat,
            system_prompt,
            user_prompt,
            temperature=float(models_cfg[seat].get("temperature", 0.7)),
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
        if on_seat:
            try:
                on_seat("seat_end", {"model": label, "ok": True,
                                     "latency_ms": meta["latency_ms"], "chars": meta["chars"]})
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
        if on_seat:
            try:
                on_seat("seat_end", {"model": label, "ok": False,
                                     "latency_ms": meta["latency_ms"],
                                     "error": meta.get("error", "")[:160]})
            except Exception:
                pass
        return meta
