# -*- coding: utf-8 -*-
"""思维链工具层（迭代二 S2 原型；[思维链真实落地 2026-10-11] 判据化重写）。

六工具：create_plan／plan_reorder／plan_insert／plan_remove／plan_modify／
step_complete——全部走**结构性拒绝、零内容判断**（driver.py:156-160 风格）。
escalate_to_plan（直答席逃生口）随直答席机制整体退役：判断归模型（C1，
判断协议在 system 区），直答中发现做不完模型直接调 create_plan（工具面
全量），逃生口冗余且其返回文案依赖已退役的判定轮。

handler 统一签名 ``(work_id, current_id, arguments, cards)``——与
card_tools.TOOL_REGISTRY 的查表调用契约一致（execute_tool :1381）。cards
参数六工具一律不用（板子不碰卡表），仅为签名对齐保留。

信封：自建 ``_ok/_err``（三键同形 ``{"ok","error","data"}``——execute_tool
的 ``_is_envelope`` 判三键齐全即原样放行）。**不 import card_tools**：
card_tools 注册本件会成环，信封三键形态固定，自建同形即等价。

档案块：五类块（plan/step/revision/…）直接调 session_facade.append
（behavior 填环节名常量，actor="tool"，end 恒空串——不碰轮界语义）。

step_complete 末步触发 finalize 流（收口自动，钉死）；finalize 属提交轮
链路（收口三件：final 块落档＋终态键＋清理），函数内延迟导入防施工序耦合。

create_plan description＝任务书附录 B 全文（一字不改——「每步必填四字段：
prompt/criteria/deliverable/type」）；步引导文案＝board.STEP_BRIEF_TEMPLATE
（附录 C，单点定义防漂移）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import board as board_mod
from ..permissions import policy
from ..storage import session_facade

logger = logging.getLogger("thinkchain.plan_tools")

# 档案块环节名常量（behavior 取值）
BEHAVIOR_PLAN = "plan"
BEHAVIOR_STEP = "step"
BEHAVIOR_REVISION = "revision"
# 步完成块按步类型扩值：execute→execute_step／think→think_step；存量板
# （type 空串）→ 保持 BEHAVIOR_STEP 兜底＋留痕。
BEHAVIOR_EXECUTE_STEP = "execute_step"
BEHAVIOR_THINK_STEP = "think_step"


# ---------------------------------------------------------------- 信封（同形 card_tools.ok_/err_，不 import 防成环）

def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "error": None, "data": data}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}, "data": None}


def _plan_gate(current_id: str,
               cards: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    """思维链六件的执行层关口：这张卡开了 ``plan`` 开关吗？

    [工具面补齐 2026-10-12] 此前这六件**既没有开关、也没有任何关口**——工具面
    不拦、执行层也不拦，等于裸奔。现在两层都接上：
      · 工具面 —— card_tools.TOOL_SWITCH 把六件都映射到 ``plan`` 开关，
        卡上没有该开关 ⇒ 模型看不见；
      · 执行层 —— 本函数，卡上没有该开关 ⇒ 调下去也拒。
    非树类开关（不直接改卡结构之外的树关系）⇒ 只判当前卡自己的类型分权。
    """
    if policy.has_perm((cards or {}).get(current_id), policy.SWITCH_PLAN):
        return None
    return _err("denied", "无权使用计划工具（plan 开关未授权）")


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


# ---------------------------------------------------------------- 工具 handler（六件）

async def _h_create_plan(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """create_plan：拆解上板。返回值＝第 1 步 prompt 原文（代码零加工），
    另带第 1 步判据与产出物（附录 C 引导）。"""
    denied = _plan_gate(current_id, cards)
    if denied is not None:
        return denied
    name = arguments.get("name", "")
    steps_def = arguments.get("steps")
    r = await board_mod.create_board(work_id, current_id, name, steps_def)
    if not r.get("ok"):
        return _err("invalid", str(r.get("error", "建板失败")))
    snap = r["board"]
    first = snap["steps"][0]
    await _append_block(
        work_id, current_id, BEHAVIOR_PLAN,
        f"拆解上板: {snap['name']}（{len(snap['steps'])} 步，每步带完成判据）\n"
        f"首步要求: {first['prompt']}\n首步判据: {first['criteria']}",
    )
    return _ok({
        "prompt": first["prompt"],        # 第 1 步 prompt 原文（验收：与参数逐字相等）
        "criteria": first["criteria"],
        "deliverable": first["deliverable"],
        "plan_name": snap["name"],
        "total": snap["total"],
        "message": "计划已上板。请立刻开始第 1 步；干完立刻调 step_complete 交作业"
                   "（result 写成果、criteria_met 写判据如何满足）。",
    })


async def _h_plan_reorder(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await _do_revise(work_id, current_id, "reorder",
                            {"order": arguments.get("order")}, arguments, cards)


async def _h_plan_insert(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    step = arguments.get("step") or {}
    return await _do_revise(work_id, current_id, "insert",
                            {"after_id": arguments.get("after_id"),
                             "step": {"id": step.get("id"), "prompt": step.get("prompt"),
                                      "criteria": step.get("criteria"),
                                      "deliverable": step.get("deliverable"),
                                      "type": step.get("type")}},
                            arguments, cards)


async def _h_plan_remove(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await _do_revise(work_id, current_id, "remove",
                            {"id": arguments.get("id")}, arguments, cards)


async def _h_plan_modify(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    detail: dict[str, Any] = {"id": arguments.get("id")}
    for key in ("prompt", "criteria", "deliverable", "status"):
        if arguments.get(key) is not None:
            detail[key] = arguments.get(key)
    return await _do_revise(work_id, current_id, "modify", detail, arguments, cards)


async def _do_revise(work_id: str, current_id: str, change: str,
                     detail: dict[str, Any], arguments: dict[str, Any],
                     cards: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """改板四动作共用件：关口 → 查板（无板明文）→ apply_revision → 返回新板面摘要。

    reason 可选参数（留空不拒）随 detail 进 revision 留痕与 ops。
    """
    # [工具面补齐 2026-10-12] 改板四动作的 plan 开关关口（公共点，一次覆盖四件）
    denied = _plan_gate(current_id, cards)
    if denied is not None:
        return denied
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
    """step_complete：交作业（result＝本步成果，criteria_met＝判据如何满足）。
    六防护全程零异常抛出——任何状态不对转明文返回。

    防护对照：①缺板→明文已终结 ②内存无盘上有→get_board 静默重建后继续
    ③result 空→「未推进」＋催促文 ④板已清理（旧引用）→明文已终结
    ⑤断尾重建失败→明文报错 ⑥锁超时→明文稍后重试。
    """
    # [工具面补齐 2026-10-12] plan 开关关口（六件里唯一不走 _do_revise 的）
    denied = _plan_gate(current_id, cards)
    if denied is not None:
        return denied
    result = arguments.get("result")
    criteria_met = arguments.get("criteria_met")
    try:
        b = await board_mod.get_board(work_id, current_id)
    except board_mod.BoardRestoreError as exc:
        return _err("board_broken", f"板文件损坏，无法推进: {exc}")   # 防护⑤
    if b is None:
        return _ok({"plan_active": False,                            # 防护①
                    "message": "本计划已终结，无可推进。请直接回应用户。"})
    if not isinstance(result, str) or not result.strip():            # 防护③
        snap = board_mod.snapshot_sync(work_id, current_id)
        prompt = snap.get("current_prompt") if snap else None
        return _ok({"advanced": False,
                    "current_prompt": prompt,
                    "message": "未推进：本步交来的结果为空。请把本步的实际成果"
                               "写进 result 再调 step_complete；干完立刻调，"
                               "未调之前这一步不算完成。"})
    if not isinstance(criteria_met, str) or not criteria_met.strip():
        return _ok({"advanced": False,
                    "message": "未推进：criteria_met 缺失——请写明「判据如何满足」"
                               "（对照本步完成判据逐条说明），与 result 一起再调 "
                               "step_complete。"})
    try:
        r = await b.mark_done_and_advance(result)
    except board_mod.BoardRestoreError:                              # 防护④
        return _ok({"plan_active": False,
                    "message": "本计划已终结，无可推进。请直接回应用户。"})
    except board_mod.BoardBusyError:                                 # 防护⑥
        return _err("board_busy", "板正被其他操作占用，请稍后重试调 step_complete")

    if r.get("advanced"):
        # 步完成块 behavior 按步类型扩值：新板落 execute_step/think_step；
        # 存量板（type 空串）落 "step" 兜底＋留痕。
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
            f"步完成: {r['step']}\n成果: {result}\n判据满足: {criteria_met}",
        )
        nxt = r.get("next_step") or {}
        if r.get("all_steps_done"):
            fin_note = await _trigger_finalize(work_id, current_id, result)
            return _ok({"advanced": True, "all_steps_done": True,
                        "message": "全部步骤已完成（收口中）。" + fin_note})
        return _ok({"advanced": True,
                    "prompt": r["prompt"],
                    "criteria": nxt.get("criteria", ""),
                    "deliverable": nxt.get("deliverable", ""),
                    "message": "已推进。下一步要求如下，请立刻开始；干完立刻调 "
                               "step_complete，未调之前这一步不算完成。\n\n"
                               + board_mod.step_brief(nxt)})
    # 防护②家族（同锁重入幂等）：本步已交
    return _ok({"advanced": False, "idempotent": True, "prompt": r.get("prompt"),
                "message": "本步结果已收过（勿重复提交）。当前步要求如下，请按此继续。"})


async def _trigger_finalize(work_id: str, card_id: str, conclusion: str = "") -> str:
    """末步触发收口流（收口三件，钉死自动）。延迟导入：函数内延迟导入防
    施工序耦合。conclusion＝末步交来的 result（收口结论素材）。"""
    try:
        from . import finalize as _finalize
    except ImportError:
        logger.warning("finalize 件未就位，收口流未触发 card=%s", card_id)
        return ""
    try:
        note = await _finalize.run_finalize(work_id, card_id, conclusion)
        return note if isinstance(note, str) else ""
    except Exception as exc:  # noqa: BLE001 收口失败不吞推进事实
        logger.warning("收口流异常 card=%s: %s", card_id, exc)
        return ""


# ---------------------------------------------------------------- 注册（card_tools.TOOL_REGISTRY 消费）

PLAN_TOOLS: list[dict[str, Any]] = [
    {
        "schema": {
            "name": "create_plan",
            "description": "把一件做不完的事拆成可执行的步骤计划并上板。\n\n"
                           "每步必填四字段：\n"
                           "- prompt：这一步要做什么（一句话任务要求）\n"
                           "- criteria：完成判据——这一步\"怎么算做完\"，必须落在可观测的产出物上"
                           "（文件存在/内容含 X/命令输出 Y），禁止\"完成\"\"做好\"式虚判\n"
                           "- deliverable：这一步的产出物（落盘路径或明确数据/结论名）\n"
                           "- type：execute（要调工具执行）/ think（思考整理）\n\n"
                           "拆解要求：\n"
                           "- 先想清目标：这件事最终要达成什么、什么算成功\n"
                           "- 步骤按先后依赖排序，前置步在前\n"
                           "- 拆完自检一遍：每步的 criteria 可观测吗？deliverable 能落盘吗？"
                           "有没有依赖环？\n"
                           "- 步骤数按需，不虚拆也不硬凑",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "计划名（一句话）"},
                    "steps": {
                        "type": "array",
                        "description": "步骤列表（至少一步；id 唯一、四字段必填）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "步 id（计划内唯一）"},
                                "prompt": {"type": "string", "description": "这一步要做什么（一句话任务要求）"},
                                "criteria": {"type": "string",
                                             "description": "完成判据——这一步\"怎么算做完\"，必须落在"
                                                            "可观测的产出物上（文件存在/内容含 X/命令输出 Y），"
                                                            "禁止\"完成\"\"做好\"式虚判"},
                                "deliverable": {"type": "string",
                                                "description": "这一步的产出物（落盘路径或明确数据/结论名）"},
                                "type": {"type": "string", "enum": ["execute", "think"],
                                         "description": "execute（要调工具执行）/ think（思考整理）"},
                            },
                            "required": ["id", "prompt", "criteria", "deliverable", "type"],
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
                            "criteria": {"type": "string", "description": "新步完成判据（可观测产出物，必填）"},
                            "deliverable": {"type": "string", "description": "新步产出物（必填）"},
                            "type": {"type": "string", "enum": ["execute", "think"],
                                     "description": "新步类型：execute／think（必填，同 create_plan 口径）"},
                        },
                        "required": ["id", "prompt", "criteria", "deliverable", "type"],
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
            "description": "修改计划中某步的字段：prompt（要求原文）/criteria（完成判据）/"
                           "deliverable（产出物）/status（状态，只能取 pending/doing/done/"
                           "blocked/dropped）。至少给一个字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "目标步 id"},
                    "prompt": {"type": "string", "description": "新的要求原文（可省）"},
                    "criteria": {"type": "string", "description": "新的完成判据（可省）"},
                    "deliverable": {"type": "string", "description": "新的产出物声明（可省）"},
                    "status": {"type": "string",
                               "enum": ["pending", "doing", "done", "blocked", "dropped"],
                               "description": "新的步状态（可省；受阻标 blocked、弃做标 dropped）"},
                    "reason": {"type": "string", "description": "改板理由（可省）"},
                },
                "required": ["id"],
            },
        },
        "handler": _h_plan_modify,
        "readonly": False,
    },
    {
        "schema": {
            "name": "step_complete",
            "description": "完成当前计划步并交作业：result 写本步实际成果（非空），"
                           "criteria_met 写「判据如何满足」（对照本步完成判据逐条说明）。"
                           "提交后自动推进到下一步并返回下一步要求（含判据与产出物）；"
                           "全部步完成会自动收口并进入提交轮三席统合。",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": "本步的实际成果（非空）"},
                    "criteria_met": {"type": "string",
                                     "description": "判据如何满足——对照本步完成判据逐条说明（非空）；"
                                                    "判据不满足就如实写「未满足＋原因」，不许谎报"},
                },
                "required": ["result", "criteria_met"],
            },
        },
        "handler": _h_step_complete,
        "readonly": False,
    },
]


def register_into(registry: dict[str, Any], tooldef_cls: type) -> int:
    """把六工具注册进指定注册表（card_tools.TOOL_REGISTRY 单点注册表法）。

    tooldef_cls＝card_tools.ToolDef（由注册方传入，本件不 import 防成环）。
    返回注册件数（验收：工具清单数量 +6 可机械核对）。
    """
    # [纯单模型模式 2026-10-17] 总开关关时**不注册**——防线放在被调方，
    # 即使调用方（card_tools）被并行作业重写，只要还调本函数就拦得住。
    from . import enabled as _tc_enabled
    if not _tc_enabled():
        logger.info("plan_tools 注册跳过：纯单模型模式（thinkchain.ENABLED=False）")
        return 0
    n = 0
    for item in PLAN_TOOLS:
        registry[item["schema"]["name"]] = tooldef_cls(
            schema=item["schema"], handler=item["handler"], readonly=item["readonly"],
        )
        n += 1
    logger.info("plan_tools 注册完成: +%d 件（create_plan/plan_reorder/plan_insert/"
                "plan_remove/plan_modify/step_complete）", n)
    return n
