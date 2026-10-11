# -*- coding: utf-8 -*-
"""入口自判分流（迭代二 S3 / 方案 §4.1）。

职责：收到用户消息后、进入 step 循环前，一次独立轻量模型调用判定
direct（直答）｜plan（拆解上板）。解析失败/非法一律**默认 plan**——
代价不对称：该拆没拆会空手硬答，不该拆拆了只多花一次判定（任务书
§四·六件事①）。

判据提示词（本文件内定义，**不改任何既有提示词文件**——任务书 S3 约束）：
文案含「需要用工具」「必须拆解」逐字＋「复杂」一词（语义对齐用户
10:20③ 原话，不逐字）——S3 验收可文案审查。

分流（任务书 S3 步骤 1 逐字）：
  direct → 工具面只留逃生口 escalate_to_plan（物理可调，同 card_tools.py:507
           「思考席 tools=None」先例的收窄精神——direct 席不给全量工具）；
  plan   → 全量工具＋进拆解流。

判定落档：triage 块（behavior="triage"）。⚠️ 施工裁定（呈报项）：任务书
原文 end=direct_answer|plan_created——facade END_VALUES 四值
（model_done/policy_stop/human_stop/error）不含这两值，append 会 raise
（session_facade.py:472 非法 end 值检查）；本件取 end=""（不碰轮界）、
判定结果进 content 首行，环节语义零丢失。

板子在内存＝已拆解过→跳过自判：由 S4 接缝在调用本件前查板（任务书
S4 步骤 1「查板跳过/无板发起/分流置 tools」），本件专注判定本身。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..api.card_tools import TOOL_REGISTRY
from ..storage import session_facade
from . import board as board_mod

logger = logging.getLogger("thinkchain.triage")

BEHAVIOR_TRIAGE = "triage"

# 判据提示词（验收：含「需要用工具」「必须拆解」逐字＋「复杂」一词——文案审查可判）
TRIAGE_SYSTEM_PROMPT = (
    "你是推演工作台的入口判定器。对用户这条消息做一次分流判断，只输出 JSON。\n"
    "判据：\n"
    "1. 这条消息是否需要用工具（查卡、改卡、写文件、查资料等）才能如实回应？\n"
    "2. 这条消息是否复杂到必须拆解成多个步骤推进（一步答不完、要做多件事）？\n"
    "两条中任意一条成立，就该进拆解流（verdict=plan）；"
    "只是寒暄、闲聊、单一事实问答，直答即可（verdict=direct）。\n"
    "输出格式（不要输出任何其他内容）：\n"
    '{"verdict": "direct" 或 "plan", "reason": "一句话理由"}'
)

# direct 席工具面：只留逃生口（S4 接缝消费——置 state.tools）
def direct_tool_schemas() -> list[dict[str, Any]]:
    """direct 席工具清单＝仅 escalate_to_plan（任务书 S3 步骤 1 逐字）。"""
    return [TOOL_REGISTRY["escalate_to_plan"].schema]


async def _call_model(system_prompt: str, user_prompt: str) -> str:
    """独立轻量模型调用（模型席位经 model_pick.resolve_model("triage") 可配，
    不配回落 main——[迭代五 S2·E10]）。模块级函数——测试 monkeypatch 位。"""
    from ..api.model_service import call_main
    from . import model_pick

    result, _model_used = await call_main(
        system_prompt, user_prompt,
        model_name=model_pick.resolve_model("triage"))
    return result.get("content", "") or ""


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """从模型输出抽 JSON（容忍 markdown 围栏与前后杂文）。"""
    if not text:
        return None
    t = text.strip()
    if "```" in t:  # 围栏优先
        parts = t.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


async def triage(work_id: str, card_id: str, user_message: str) -> dict[str, Any]:
    """自判分流。返回 {"verdict": "direct"|"plan", "reason": str, "skipped": bool}。

    解析失败/非法 → 默认 plan＋日志留痕（零异常抛出——分流失败不允许
    卡死入口；plan 侧工具面是全量，容错方向安全）。
    """
    raw = ""
    verdict, reason, ok_parse = "plan", "", False
    try:
        raw = await _call_model(TRIAGE_SYSTEM_PROMPT, user_message)
        obj = _extract_json(raw)
        if obj and obj.get("verdict") in ("direct", "plan"):
            verdict = obj["verdict"]
            reason = str(obj.get("reason", ""))[:200]
            ok_parse = True
    except Exception as exc:  # noqa: BLE001 判定调用失败＝解析失败同待遇
        logger.warning("自判调用异常 card=%s: %s（默认 plan）", card_id, exc)

    if not ok_parse:
        logger.info("自判解析失败/非法，默认 plan card=%s raw=%r", card_id, (raw or "")[:120])

    # 判定落档（triage 块；end="" 不碰轮界——见模块头裁定）
    tag = "直答" if verdict == "direct" else "拆解"
    note = "" if ok_parse else "（解析失败，默认拆解）"
    try:
        await session_facade.append(work_id, card_id, {
            "content": f"判定: {verdict}（{tag}）{note}\n理由: {reason or '—'}",
            "actor": "tool",
            "end": "",
            "behavior": BEHAVIOR_TRIAGE,
        })
    except Exception as exc:  # noqa: BLE001 落档失败不阻断分流
        logger.warning("triage 块落盘失败 card=%s: %s", card_id, exc)

    return {"verdict": verdict, "reason": reason, "skipped": False,
            "auto": False, "rule": ""}


async def triage_if_needed(work_id: str, card_id: str, user_message: str) -> dict[str, Any]:
    """S4 接缝入口：板子在内存＝已拆解过→跳过自判（skipped=True，不调模型
    不落档）；无板→发起自判（先机械预检、再模型判定）。"""
    if board_mod.has_live_board(work_id, card_id):
        return {"verdict": "plan", "reason": "已有活动板，续推不重判", "skipped": True}
    # [2026-10-11 两套机制拆除] 机械预检：确定性动作（删卡/建卡/改卡/查询）
    # 直答——原快车道 route() 的 A97 功能保留，但不再独立成「快车道/全链」
    # 分流，并入本判定链为前置分支（判断只有一级，机械识别是它的分支不是
    # 独立机制）。延迟导入防循环：routes→message_runner→thinkchain.triage。
    from ..api.routes import _route_rule
    rule = _route_rule(user_message)
    if rule != "未命中":
        return {
            "verdict": "direct",
            "auto": True,
            "rule": rule,
            "reason": f"确定性动作：{rule}（机械预检命中，带工具直答）",
            "skipped": False,
        }
    return await triage(work_id, card_id, user_message)
