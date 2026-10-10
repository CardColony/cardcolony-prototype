# -*- coding: utf-8 -*-
"""思维链工具层（迭代二 S2 / 方案 §4.2＋§4.4）：七工具实现与注册。

七工具（任务书 S2 步骤逐字）：create_plan／plan_reorder／plan_insert／
plan_remove／plan_modify／step_complete／escalate_to_plan——全部走**结构性
拒绝、零内容判断**（driver.py:156-160 风格；screenplay-board 重排先例：
纯数组序变更、非法 id 才拒）。

handler 统一签名 ``(work_id, current_id, arguments, cards)``——与
card_tools.TOOL_REGISTRY 的查表调用契约一致（execute_tool :1381）。cards
参数七工具一律不用（板子不碰卡表），仅为签名对齐保留。

信封：自建 ``_ok/_err``（三键同形 ``{"ok","error","data"}``——execute_tool
的 ``_is_envelope`` 判三键齐全即原样放行）。**不 import card_tools**：
card_tools 注册本件会成环，信封三键形态固定，自建同形即等价。

档案块：五类块（plan/step/revision/…）直接调 session_facade.append
（12:24 版任务书——迭代一门面，behavior 填环节名常量，actor="tool"，
end 恒空串——END_VALUES 无本迭代环节值，**不碰轮界语义**）。

step_complete 末步触发 finalize 流（任务书 S2 步骤 3「末步→触发 finalize
流」）：finalize 属 S3，本件**函数内延迟导入**——S3 建成后自动生效，
缺席时记日志不阻塞推进返回。

escalate_to_plan 返回 ``{"escalate": True, ...}`` 标记：S4 接缝在 runner
侧识别后转 StepOutcome("deliver", "escalate")（任务书 S2 步骤 4——绝不
静默换路）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import board as board_mod
from ..storage import session_facade

logger = logging.getLogger("thinkchain.plan_tools")

# 档案块环节名常量（behavior 取值；五类块口径＝定稿 §四）
BEHAVIOR_PLAN = "plan"
BEHAVIOR_STEP = "step"
BEHAVIOR_REVISION = "revision"
BEHAVIOR_ESCALATE = "escalate"
# [迭代三 S4·决策 B] 步完成块按步类型扩值：execute→execute_step／
# think→think_step；存量板（type 空串）→ 保持 BEHAVIOR_STEP 兜底＋留痕。
# behavior 值集合 ⊆ {triage, plan, step, final, revision, execute_step,
# think_step}（线二·命名规范一次定稳）。
BEHAVIOR_EXECUTE_STEP = "execute_step"
BEHAVIOR_THINK_STEP = "think_step"


# ---------------------------------------------------------------- 信封（同形 card_tools.ok_/err_，不 import 防成环）

def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "error": None, "data": data}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}, "data": None}


# ---------------------------------------------------------------- 档案补块

async def _append_block(work_id: str, card_id: str, behavior: str,
                        content: str) -> None:
    """工具侧档案块：actor="tool"、end 空串（不碰轮界）。失败不阻断工具
    返回（档案是义务不是闸——与 ops 通道同纪律）。"""
    try:
        await session_facade.append(work_id, card_id, {
            "content": content,
            "actor": "tool",
            "end": "",
            "behavior": behavior,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("档案块落盘失败 behavior=%s card=%s: %s", behavior, card_id, exc)


def _reason_of(arguments: dict[str, Any]) -> str:
    r = arguments.get("reason", "")
    return r if isinstance(r, str) else ""


# ---------------------------------------------------------------- 工具 handler（七件）

async def _h_create_plan(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """create_plan：拆解上板。返回值＝第 1 步 prompt 原文（照 dsh——返回
    直接给下一步单点动作），代码零加工。"""
    name = arguments.get("name", "")
    steps_def = arguments.get("steps")
    r = await board_mod.create_board(work_id, current_id, name, steps_def)
    if not r.get("ok"):
        return _err("invalid", str(r.get("error", "建板失败")))
    snap = r["board"]
    first_prompt = snap["steps"][0]["prompt"]
    await _append_block(
        work_id, current_id, BEHAVIOR_PLAN,
        f"拆解上板: {snap['name']}（{len(snap['steps'])} 步）\n首步要求: {first_prompt}",
    )
    return _ok({
        "prompt": first_prompt,          # 第 1 步 prompt 原文（验收：与参数逐字相等）
        "plan_name": snap["name"],
        "total": snap["total"],
        "message": "计划已上板。请立刻开始第 1 步；干完立刻调 step_complete 交作业。",
    })


async def _h_plan_reorder(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await _do_revise(work_id, current_id, "reorder",
                            {"order": arguments.get("order")}, arguments)


async def _h_plan_insert(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    step = arguments.get("step") or {}
    return await _do_revise(work_id, current_id, "insert",
                            {"after_id": arguments.get("after_id"),
                             "step": {"id": step.get("id"), "prompt": step.get("prompt"),
                                      "type": step.get("type")}},
                            arguments)


async def _h_plan_remove(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await _do_revise(work_id, current_id, "remove",
                            {"id": arguments.get("id")}, arguments)


async def _h_plan_modify(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await _do_revise(work_id, current_id, "modify",
                            {"id": arguments.get("id"), "prompt": arguments.get("prompt")},
                            arguments)


async def _do_revise(work_id: str, current_id: str, change: str,
                     detail: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """改板四动作共用件：查板（无板明文）→ apply_revision → 返回新板面摘要。

    reason 可选参数（留空不拒）随 detail 进 revision 留痕与 ops。
    """
    try:
        b = await board_mod.get_board(work_id, current_id)
    except board_mod.BoardRestoreError as exc:
        return _err("board_broken", f"板文件损坏，无法改板: {exc}")
    if b is None:
        return _ok({"message": "本计划已终结（无活动板），改板无效", "plan_active": False})
    reason = _reason_of(arguments)
    if reason:
        detail = {**detail, "reason": reason}
    try:
        r = await b.apply_revision(change, detail)
    except board_mod.BoardBusyError:
        return _err("board_busy", "板正被其他操作占用，请稍后重试")
    if not r.get("ok"):
        return _err("invalid", str(r.get("error", "改板被拒")))
    snap = r["board"]
    await _append_block(
        work_id, current_id, BEHAVIOR_REVISION,
        f"改板: {change} {json.dumps(detail, ensure_ascii=False)}",
    )
    return _ok({
        "board": {k: snap[k] for k in ("name", "steps", "current", "total")},
        "message": "板已更新。当前步要求见 current_prompt 对应步。",
    })


async def _h_step_complete(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """step_complete：交作业。六防护全程零异常抛出——任何状态不对转明文
    返回（任务书 S2 步骤 3 逐字）。

    防护对照：①缺板→明文已终结 ②内存无盘上有→get_board 静默重建后继续
    ③result 空→「未推进」＋催促文 ④板已清理（旧引用）→明文已终结
    ⑤断尾重建失败→明文报错 ⑥锁超时→明文稍后重试。
    """
    result = arguments.get("result")
    try:
        b = await board_mod.get_board(work_id, current_id)
    except board_mod.BoardRestoreError as exc:
        return _err("board_broken", f"板文件损坏，无法推进: {exc}")   # 防护⑤
    if b is None:
        return _ok({"plan_active": False,                            # 防护①
                    "message": "本计划已终结，无可推进。请直接回应用户。"})
    if not isinstance(result, str) or not result.strip():            # 防护③
        prompt = await board_mod.current_step_prompt(work_id, current_id)
        return _ok({"advanced": False,
                    "current_prompt": prompt,
                    "message": "未推进：本步交来的结果为空。请把本步的实际成果"
                               "写进 result 再调 step_complete；干完立刻调，"
                               "未调之前这一步不算完成。"})
    try:
        r = await b.mark_done_and_advance(result)
    except board_mod.BoardRestoreError:                              # 防护④
        return _ok({"plan_active": False,
                    "message": "本计划已终结，无可推进。请直接回应用户。"})
    except board_mod.BoardBusyError:                                 # 防护⑥
        return _err("board_busy", "板正被其他操作占用，请稍后重试调 step_complete")

    if r.get("advanced"):
        # [迭代三 S4·决策 B] 步完成块 behavior 按步类型扩值：新板落
        # execute_step/think_step；存量板（type 空串）落 "step" 兜底＋留痕
        # （不迁移旧值、不把空串猜成 execute/think——那是代码判内容）。
        stype = r.get("type", "")
        if stype == "execute":
            behavior = BEHAVIOR_EXECUTE_STEP
        elif stype == "think":
            behavior = BEHAVIOR_THINK_STEP
        else:
            behavior = BEHAVIOR_STEP
            logger.warning(
                "步完成块兜底：步 %s type=%r 非 execute/think（存量板），"
                "behavior 落 %s", r.get("step"), stype, BEHAVIOR_STEP)
        await _append_block(
            work_id, current_id, behavior,
            f"步完成: {r['step']}\n成果: {result}",
        )
        if r.get("all_steps_done"):
            fin_note = await _trigger_finalize(work_id, current_id, result)
            return _ok({"advanced": True, "all_steps_done": True,
                        "message": "全部步骤已完成。" + fin_note})
        return _ok({"advanced": True, "prompt": r["prompt"],
                    "message": "已推进。下一步要求如下，请立刻开始；干完立刻调 "
                               "step_complete，未调之前这一步不算完成。"})
    # 防护②家族（同锁重入幂等）：本步已交
    return _ok({"advanced": False, "idempotent": True, "prompt": r.get("prompt"),
                "message": "本步结果已收过（勿重复提交）。当前步要求如下，请按此继续。"})


async def _trigger_finalize(work_id: str, card_id: str, conclusion: str = "") -> str:
    """末步触发收口流（S3 finalize，钉死自动）。延迟导入：S3 建成后自动
    生效；缺席记日志不阻塞——推进结果照常返回。conclusion＝末步交来的
    result（收口结论素材）。"""
    try:
        from . import finalize as _finalize  # S3 件（函数内延迟导入防施工序耦合）
    except ImportError:
        logger.warning("finalize 件未就位，收口流未触发 card=%s", card_id)
        return ""
    try:
        note = await _finalize.run_finalize(work_id, card_id, conclusion)
        return note if isinstance(note, str) else ""
    except Exception as exc:  # noqa: BLE001 收口失败不吞推进事实
        logger.warning("收口流异常 card=%s: %s", card_id, exc)
        return ""


async def _h_escalate_to_plan(work_id: str, current_id: str,
                              arguments: dict[str, Any],
                              cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """escalate_to_plan：直答席逃生口——升级进拆解流。绝不静默换路：
    档案补块＋escalate 标记（S4 runner 消费转 StepOutcome("deliver",
    "escalate")，下轮进拆解流）。"""
    reason = _reason_of(arguments) or "（未填理由）"
    await _append_block(
        work_id, current_id, BEHAVIOR_ESCALATE,
        f"升级拆解: {reason}",
    )
    return _ok({"escalate": True, "reason": reason,
                "message": "已升级进拆解流：下一轮将先判定是否需要拆解上板。"})


# ---------------------------------------------------------------- 注册（card_tools.TOOL_REGISTRY 消费）

PLAN_TOOLS: list[dict[str, Any]] = [
    {
        "schema": {
            "name": "create_plan",
            "description": "把任务拆解成多步计划并上板（仅当任务需要多步推进时使用）。"
                           "上板成功后返回第 1 步的要求，请立刻开始该步。"
                           "【类型定义】每步必须标注 type：execute＝执行步，"
                           "需要调用工具来执行的步骤（调研、查阅、搜索等执行行为）；"
                           "think＝思考步，不需要执行、偏向思考整理的步骤"
                           "（不是不给工具调用权限——think 可调工具，工具面与 execute "
                           "全量无差异，只是不以调工具为目的）。"
                           "【标型规则】每步必标 execute/think 其一；标错或漏标会被"
                           "结构性拒绝。"
                           "【性质提示】执行步强调查证动手拿事实；思考步强调整理收敛"
                           "出判断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "计划名（一句话）"},
                    "steps": {
                        "type": "array",
                        "description": "步骤列表（至少一步；id 唯一、prompt 非空、"
                                       "每步必标 type）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "步 id（计划内唯一）"},
                                "prompt": {"type": "string", "description": "该步的要求（给后续轮的自己看）"},
                                "type": {"type": "string", "enum": ["execute", "think"],
                                         "description": "该步类型：execute＝执行（调工具拿事实）／"
                                                        "think＝思考（整理收敛出判断，仍可调工具）"},
                                "has_artifact": {"type": "boolean", "description": "该步预期是否产出文件产物"},
                            },
                            "required": ["id", "prompt", "type"],
                        },
                    },
                },
                "required": ["name", "steps"],
            },
        },
        "handler": _h_create_plan,
        "readonly": False,
    },
    {
        "schema": {
            "name": "plan_reorder",
            "description": "调整计划步骤顺序（纯顺序变更，步骤状态不变）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order": {"type": "array", "items": {"type": "string"},
                              "description": "全部步 id 的新顺序"},
                    "reason": {"type": "string", "description": "改板理由（可省）"},
                },
                "required": ["order"],
            },
        },
        "handler": _h_plan_reorder,
        "readonly": False,
    },
    {
        "schema": {
            "name": "plan_insert",
            "description": "在计划的某步之后插入新步（after_id 省略＝插在最前）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "after_id": {"type": ["string", "null"], "description": "锚点步 id；null＝插在最前"},
                    "step": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "新步 id（不得与现有重复）"},
                            "prompt": {"type": "string", "description": "新步要求"},
                            "type": {"type": "string", "enum": ["execute", "think"],
                                     "description": "新步类型：execute／think（必填，同 create_plan 口径）"},
                        },
                        "required": ["id", "prompt", "type"],
                    },
                    "reason": {"type": "string", "description": "改板理由（可省）"},
                },
                "required": ["step"],
            },
        },
        "handler": _h_plan_insert,
        "readonly": False,
    },
    {
        "schema": {
            "name": "plan_remove",
            "description": "从计划中移除一步（当前步不可删）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "要移除的步 id"},
                    "reason": {"type": "string", "description": "改板理由（可省）"},
                },
                "required": ["id"],
            },
        },
        "handler": _h_plan_remove,
        "readonly": False,
    },
    {
        "schema": {
            "name": "plan_modify",
            "description": "修改计划中某步的要求原文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "目标步 id"},
                    "prompt": {"type": "string", "description": "新的要求原文"},
                    "reason": {"type": "string", "description": "改板理由（可省）"},
                },
                "required": ["id", "prompt"],
            },
        },
        "handler": _h_plan_modify,
        "readonly": False,
    },
    {
        "schema": {
            "name": "step_complete",
            "description": "完成当前计划步并交作业：把本步实际成果写进 result。"
                           "提交后自动推进到下一步并返回下一步要求；全部步完成会自动收口。",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": "本步的实际成果（非空）"},
                },
                "required": ["result"],
            },
        },
        "handler": _h_step_complete,
        "readonly": False,
    },
    {
        "schema": {
            "name": "escalate_to_plan",
            "description": "直答席逃生口：当前消息其实需要多步推进/工具拆解时，"
                           "调用它升级进拆解流（下一轮先判定是否上板）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "为什么直答不了、需要拆解"},
                },
                "required": ["reason"],
            },
        },
        "handler": _h_escalate_to_plan,
        "readonly": False,
    },
]


def register_into(registry: dict[str, Any], tooldef_cls: type) -> int:
    """把七工具注册进指定注册表（card_tools.TOOL_REGISTRY 单点注册表法）。

    tooldef_cls＝card_tools.ToolDef（由注册方传入，本件不 import 防成环）。
    返回注册件数（验收：工具清单数量 +7 可机械核对）。
    """
    n = 0
    for item in PLAN_TOOLS:
        registry[item["schema"]["name"]] = tooldef_cls(
            schema=item["schema"], handler=item["handler"], readonly=item["readonly"],
        )
        n += 1
    logger.info("plan_tools 注册完成: +%d 件（create_plan/plan_reorder/plan_insert/"
                "plan_remove/plan_modify/step_complete/escalate_to_plan）", n)
    return n
