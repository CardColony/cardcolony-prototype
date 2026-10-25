# -*- coding: utf-8 -*-
"""_run_message 编排内核（T-0009 基建二 / card_037）。

**为什么有这个模块**：`_run_message_inner` 原为 routes.py 内 479 行单体
（读卡→上下文→工具循环→三席→prompt 日志→拼接→建卡→落盘），六段混在一个
函数里，无法单步推进、无法在轮边界挂控制钩子。本模块把它拆成
「显式 state ＋ 单步 step ＋ 薄壳驱动」三层，**编排语义逐字不变**。

**分层与不变量**（B-05/B-14 收编口径，仅此而已，不超额交付）：
- `RunState`：显式状态对象，承载编排全程的可变数据（轮次/消息列/
  tool_events/T14 采集槽/三席状态/错误…）。**只做数据，不做编排决策。**
- `step(state) -> StepOutcome`：一次状态推进＝一个显式状态点。outcome 四值：
  `continue`（继续轮）、`seats`（本轮触发三席后置）、`deliver`（可交付）、
  `error`（出错，走错误返回）。
- `check_control(state) -> verdict`：**中止缝**。step 边界调用点。
  [A8/R3-T106-T124] 接入控制三态消费（人工停止 consume-once；工具卡驱动
  串行共存、§5-10 共存禁令）。仍不做 job 系统、不上队列。

**编排不变量（拆分必须逐字保持，动了就是行为漂移）**：
1. 轮次上限：**无**（`[无闸改造 2026-10-02]` 循环不设封顶，模型停即收）。
2. 三席触发条件：[思维链真实落地 2026-10-11] 仅提交轮——board 全部步骤
   done（收口）后、交付前，固定触发三席统合（C6：判据核对只在提交轮，
   非每步都三席）；直答回合零三席。
3. 拼接顺序：T14「分析在前、回执在后、`\n\n` 分隔」，落位在日志写之后、
   parse_proposals 之前。
4. 落盘时机：用户条在装配前；assistant 条在全部交付段之后；
   prompt 日志无论成败都写。
5. SSE 事件序：`run_start → context → round_start →
   (tool_call → tool_result)* → (收口后) seats_enter → seat_* → deliver →
   proposal → done`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..storage import card_db, card_store, memory_store, session_facade, session_ops, session_store, work_store
from ..thinkchain import board as thinkchain_board
from ..thinkchain import enabled as thinkchain_enabled  # [纯单模型 2026-10-17]
from ..thinkchain import seats_enabled as thinkchain_seats_enabled  # [规划驱动 2026-10-19]
from ..thinkchain.model_pick import pick_model as _pick_model  # 步类型路由消费点（_pick_step_model 调用）
from ..thinkchain import model_pick
from ..toolcard import driver as toolcard_driver
from . import (
    card_msg,
    card_proposal,
    card_tools,
    control,
    context_assembler,
    context_regions,
    current_turn,
    event_stream,
    model_config,
    model_service,
    progress,
    prompt_log,
    prompts,
    simple_service,
    steer,
    text_tool_rescue,
    tool_fold,
)

logger = logging.getLogger("api.message_runner")


# ============================================================
# 状态与结果
# ============================================================

@dataclass
class RunState:
    """编排全程的显式状态（只装数据，不做编排决策）。

    字段与原 `_run_message_inner` 的局部变量**一一对应**，逐字不改名，
    便于与金样本/旧代码对账。
    """

    # —— 入参 ——
    work_id: str
    card_id: str
    content: str
    pair_id: str = ""

    # —— 上下文与提示（段②产物）——
    condense_state: Optional[dict[str, Any]] = None
    system_prompt: str = ""
    vp_system_prompt: str = ""
    ctx_regions: list[dict[str, Any]] = field(default_factory=list)
    neutral_context: str = ""
    context_ctx: Optional[context_regions.RegionCtx] = None

    # —— 工具循环 ——
    prior: list[dict[str, Any]] = field(default_factory=list)
    prior_for_gateway: list[dict[str, Any]] = field(default_factory=list)
    # [T11-T6] history_len＝装配完成时 prior_for_gateway 的长度快照。
    # **必须一次算定**：轮槽就地 append 进prior_for_gateway（本列表），
    # 事后取 len() 会拿到增长后的值 ⇒ 三席 prior 切片与 log_regions 的
    # 新旧分界都会错。
    history_len: int = 0
    slot: Any = None                     # tool_fold.ToolTurnSlot（请求局部对象）
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    # [E-探针制] 工具结果全文累积（口径④）：供三席后置注入，不折叠不截断
    tool_full_texts: list[str] = field(default_factory=list)
    user_prompt: str = ""
    rounds: int = 0
    model_calls: int = 0
    final_reply: Optional[str] = None
    # [5715] 末轮**原始**正文（compose_delivery 拼接前存下）：收口那条 assistant_text
    # 事件只落它——各轮正文已按轮单独落事件（见 step() 工具轮分支），
    # 再落一遍 clean_text（拼接全文）就重复了。会话块内容仍是 clean_text（模型上下文不变）。
    final_only: str = ""
    # [T14-1-1] 带工具轮的分析正文采集（只读）：落盘型工作流的分析写在带工具轮
    # 的 assistant_content 里，末轮判定只取纯对话轮正文——分析结构性上不了屏。
    # 采集后与末轮回执拼接交付（分析在前、回执在后），不取代回执。
    analysis_parts: list[str] = field(default_factory=list)
    model_used: Optional[str] = None
    # [模型选择底座 2026-10-21] 本轮**实际请求**的模型名（_pick_step_model /
    # resolve_model 的返回值）。prompt 日志记它，而不是另算一次 default_model——
    # 原来日志记的是「默认模型」，与实际调用的角色模型（execute/think/synthesize）
    # 常常不是一个，看起来像「没按配置走」。
    model_requested: Optional[str] = None
    # [DeepSeek 思考回传 2026-10-15] 本轮模型调用产出的思考（reasoning_content）。
    # 只做调用链内部传递：拿到 result 后立刻写入、存 assistant 条时立刻取用并清空。
    # **不进对话内容、不进前端渲染**（展示走 emit_token 独立通道）。
    turn_reasoning: str = ""
    error: Optional[str] = None
    # [phase9-E] 观点元信息（含内部text）
    viewpoints_meta: list[dict[str, Any]] = field(default_factory=list)
    # [E-探针制] 三席一次进场闸（口径①：只进场一次；统合官若续查证不重问三席）
    seats_done: bool = False
    # [思维链真实落地 2026-10-11] 提交轮三席统合（C6）状态桶：
    # chain_completed＝本回合内 step_complete 报告 all_steps_done（链收口）；
    # chain_steps_record＝末次 step_complete 执行前的板快照 steps（含各步
    # criteria/deliverable/result——收口清理后板已不在，快照是提交轮唯一材料）；
    # chain_criteria_mets＝模型每次 step_complete 交来的 criteria_met 自述
    # （按调用序累积，与板步对齐）。
    chain_completed: bool = False
    chain_plan_name: str = ""
    chain_steps_record: list[dict[str, Any]] = field(default_factory=list)
    chain_criteria_mets: list[str] = field(default_factory=list)
    # T14 拼接结果（交付段写，供建卡段与返回体读）
    branch: str = ""
    # 交付后的建卡结果
    created_cards: list[dict[str, Any]] = field(default_factory=list)
    denied_cards: list[dict[str, str]] = field(default_factory=list)
    # [T-0010 §3.1] 文本协议块通道成功建卡计数（rescue 兼容层的真实用量观测）。
    # create_cards 段（段⑥）在 write_logs（段④）之后，故计数经 state 传递，
    # 落盘时由 create_cards 就地追加该键——不动「日志写一次」的不变量。
    text_channel_create_count: int = 0
    # 本次 prompt 日志落盘路径（段④ 写、段⑥ 追加键时用）
    prompt_log_path: Optional[str] = None
    clean_text: str = ""
    # 中止缝预留（B-14 软预算）：stop_requested 由外部控制面置位。
    # [A8/R3] 消费位已接入 check_control（控制三态串行消费，共存禁令 §5-10）。
    stop_requested: bool = False
    # [A8/R3-T124] 回合结束原因（落盘 end 字段）：""＝正常（门面缺省 model_done）；
    # human_stop＝人工停止；policy_stop＝「判不了交人」轮末闭合。
    end_reason: str = ""
    # [模型过程数据 2026-10-12] step_start 幂等键（"{板内位置}:{步 id}"）——
    # 当前步在本回合首次被观测时发一次 step_start，其后同步不重发（只做数据）。
    step_started_key: str = ""
    # [当前轮注入消息事件 2026-10-14] 当前轮注入（context_preamble/
    # context_injection）状态：
    #   turn_tail      —— round1 暂存的注入消息（随用户消息之后下发）；
    #   turn_tail_engaged —— 是否已把 turn_tail 固化进轮槽（幂等闸）；
    #   preamble_emitted —— 固定总领本 run 只发一次；
    #   injection_keys —— 已发动态注入的幂等键集合（同 run 同键不重发）。
    turn_tail: list[dict[str, Any]] = field(default_factory=list)
    # [5715] 首轮收到的插话暂存（独立于 turn_tail：turn_tail 会被 turn_tail_engaged
    # 幂等闸挡住，首轮之后插进来的就再也固化不了）。随 _engage_slot 一并固化。
    steer_tail: list[dict[str, Any]] = field(default_factory=list)
    turn_tail_engaged: bool = False
    preamble_emitted: bool = False
    injection_keys: set[str] = field(default_factory=set)
    # [判断协议改注入 2026-10-14] 判断协议注入（仅轮首、且本卡无活板时一条）：
    #   judgment_tail —— 暂存待随 round1 下发的判断协议消息（**独立于 turn_tail**，
    #   绝不固化进轮槽——这是「后续轮清掉」的全部机制依据，任务书 §2.3）。
    judgment_tail: list[dict[str, Any]] = field(default_factory=list)
    # [更新现状按钮改造 2026-10-16] 「更新现状」专用操作回合：
    #   op —— POST …/messages 请求体带来的操作标记（""＝普通回合；
    #         OP_UPDATE_STATUS＝「更新现状」专用回合）。它同时决定两件事：
    #         ① 轮首注入专用任务（_sync_status_task）；② 工具面临时追加专用工具
    #         （build_tool_schemas(op=…)）。普通回合两者都不发生，逐字不变。
    #   status_task_emitted —— 专用注入本 run 只发一次的幂等闸。
    op: str = ""
    status_task_emitted: bool = False


@dataclass(frozen=True)
class StepOutcome:
    """单步推进的结果。`kind` ∈ {continue, seats, deliver, error}。"""

    kind: str
    detail: str = ""


# ============================================================
# 中止缝（空实现——签名定死，函数体空）
# ============================================================

def check_control(state: RunState) -> Optional[str]:
    """step 边界的控制钩子。返回 verdict：control.STOP | control.DEFER_HUMAN | None。

    [T-0009 §2.3] 中止缝挂点。〔A8/R3-T106-T124〕中止逻辑本批接入：
    串行两问——① 工具卡驱动（既有，只读指针，零判断）；② 控制面 stop 置位
    （consume-once）。**共存禁令（§5-10）**：不以 on_step_boundary 返回值为停的
    依据——「看到了进度」≠「按进度判停」；停的依据只能是 control.check 的
    verdict（迭代一唯一真实来源＝人的手动停）。

    STOP → state.end_reason="human_stop"（消费打点）；DEFER_HUMAN →
    end_reason="policy_stop"（迭代一注册表恒空不会返回，路由保留、接口非空置）。
    """
    step_id = toolcard_driver.on_step_boundary(state)
    if step_id:
        logger.debug("step 边界 toolcard step=%s card=%s",
                     step_id, getattr(state, "card_id", ""))
    verdict = control.check(state.work_id, state.card_id)
    if verdict == control.STOP:
        state.end_reason = "human_stop"
        session_ops.ops_log("human_stop", state.work_id, state.card_id,
                            {"action": "consumed", "rounds": state.rounds})
        logger.warning("[A8] 人工停止命中 work=%s card=%s rounds=%s",
                       state.work_id, state.card_id, state.rounds)
        return verdict
    if verdict == control.DEFER_HUMAN:
        state.end_reason = "policy_stop"
        return verdict
    return None


# ============================================================
# 段②：上下文组装（＋凝练切片）
# ============================================================

# d. 上下文装配：注册表一次接管 system／history 与模型请求初始帧；动态 user/注入仍由
# 编排逻辑产出原始数据，再交 context base 按注册通道编入请求。
def build_context(state: RunState) -> None:
    """统一 context base 构建 system、对话历史投影、工具列与轮槽。"""
    state.condense_state = (
        memory_store.load_memory(state.work_id, state.card_id)
        if memory_store.condense_enabled() else None
    )
    bundle = context_assembler.build_context_bundle(
        state.work_id, state.card_id,
        memory_state=state.condense_state,
        history_source=state.prior,
    )
    state.context_ctx = bundle["context"]
    state.ctx_regions = bundle["regions"]
    state.neutral_context = bundle["main"]["neutral_context"]
    state.system_prompt = bundle["main"]["system_prompt"]
    state.vp_system_prompt = bundle["viewpoints"]["system_prompt"]
    state.prior_for_gateway = bundle["main"]["prior_messages"]
    event_stream.emit(state.work_id, state.card_id, "context", {
        "regions": [str(r.get("region", "")) for r in state.ctx_regions],
        "bytes": len(state.neutral_context.encode("utf-8")),
    })
    state.slot = tool_fold.ToolTurnSlot(state.prior_for_gateway)
    state.tools = card_tools.build_tool_schemas(
        card_id=state.card_id,
        work_id=state.work_id,
        cards=card_tools._collect_card_meta(state.work_id),
        op=state.op,   # [更新现状按钮改造] op 回合才追加专用工具（其余回合逐字不变）
    )
    # history_len 是注册表统一投影出来的历史条数快照；轮槽之后会就地追加。
    state.history_len = len(state.prior_for_gateway)


def compose_model_request(
    state: RunState,
    *,
    profile: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    prior_key: str = "history",
    user_content: Optional[str] = None,
    turn_once: Optional[list[dict[str, Any]]] = None,
    turn_tail: Optional[list[dict[str, Any]]] = None,
    seat_identity: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """所有主对话/三席请求都经 context registry 组合，不在调用点手拼通道。"""
    if state.context_ctx is None:
        # 兼容白盒直调（生产链路 build_context 一定先设 context_ctx）。
        return {
            "system_prompt": state.system_prompt if profile == "main" else state.vp_system_prompt,
            "user_prompt": user_content if user_content is not None else state.user_prompt,
            "prior_messages": prior_messages or [],
            "turn_once": turn_once or [],
            "turn_tail": turn_tail or [],
        }
    request = context_regions.compose_model_context(
        state.context_ctx,
        profile=profile,
        prior_messages=prior_messages,
        prior_key=prior_key,
        user_content=user_content if user_content is not None else state.user_prompt,
        turn_once=turn_once,
        turn_tail=turn_tail,
        seat_identity=seat_identity,
    )
    return request


# ============================================================
# 段③：工具循环 —— 单步推进
# ============================================================

# [思维链真实落地 2026-10-11] 步执行提示词模板（任务书附录 C 全文）单点
# 定义在 board.py（STEP_BRIEF_TEMPLATE/step_brief）：本件（催化挂点）、
# plan_tools（步引导）、step_region（165 区）三处消费，禁各自复制防漂移。

def _pick_step_model(state: RunState) -> str:
    """[迭代三 S5] 当前步类型→模型路由。

    有活板 → snapshot_sync 读当前步 type 查表（存量空串/越界 → default）；
    无活板（直答）→ default。查得模型不在 models.yaml 的 models 键集 →
    显式报错（simple_service.py:183-185 先例形态，错误信息含模型名）——
    消费侧热读模型清单可行（gw.config 内存常驻），无需启动校验降级。
    每圈实取模型打日志（S5 验收「日志可查每步实取模型」）。

    [模型设置按项目/卡片覆盖 2026-10-19] 带 work/card 作用域解析——
    card > work > global（model_pick.pick_model 内完成）。
    """
    if not thinkchain_enabled():   # [纯单模型 2026-10-17] 无步类型，直接 default 席
        return _pick_model("default", work_id=state.work_id, card_id=state.card_id)
    snap = thinkchain_board.snapshot_sync(state.work_id, state.card_id)
    role = "default"
    if snap is not None:
        steps = snap.get("steps") or []
        cur = snap.get("current", 0)
        if 0 <= cur < len(steps):
            role = steps[cur].get("type") or "default"
    seat = _pick_model(role, work_id=state.work_id, card_id=state.card_id)
    models_cfg = model_service.get_gateway().config.get("models") or {}
    if seat not in models_cfg:
        raise RuntimeError(
            f"models.yaml 无此模型条目: {seat}（步类型 {role!r} 的绑定指向了不存在的模型）")
    logger.info("[迭代三 S5] 类型路由 role=%s -> seat=%s work=%s card=%s",
                role, seat, state.work_id, state.card_id)
    return seat


# ============================================================
# [模型过程数据 2026-10-12] 过程事件数据源（只读板、只发事件，零判定）
# ============================================================

def _sync_step_start(state: RunState) -> str:
    """当前板步观测点：每步首次被观测时发 ``step_start``，返回该步 id。

    - 无板（直答查证）／快照不可读／current 越界 → 返回空串**不发事件**
      （前端拿空串不归步骤，工具调用平铺在判断块下）；
    - 幂等键＝``{板内位置}:{步 id}``——同一步在本回合内只发一次，改板换位/
      换 id 才再发；
    - ``index`` 是 1 起的展示位置号（①②③），``total`` 是步总数；板内
      0 起的 ``current`` 下标不外泄。

    调用点两处：①每个 tool_call 之前（保证 step_start 先于该步的工具行、
    且 call/result 取同一个 step_id）；②create_plan 成功之后（板刚上架＝
    第 1 步开始，紧随 plan_created）。
    """
    if not thinkchain_enabled():   # [纯单模型 2026-10-17] 关链：无步骤概念
        return ""
    snap = thinkchain_board.snapshot_sync(state.work_id, state.card_id)
    if not snap:
        return ""
    steps = snap.get("steps") or []
    cur = snap.get("current", 0)
    if not (isinstance(cur, int) and 0 <= cur < len(steps)):
        return ""
    step = steps[cur]
    if not isinstance(step, dict):
        return ""
    step_id = str(step.get("id") or "")
    key = f"{cur}:{step_id}"
    if state.step_started_key != key:
        state.step_started_key = key
        event_stream.emit(state.work_id, state.card_id, "step_start", {
            "step_id": step_id,
            "index": cur + 1,
            "total": len(steps),
            "prompt": step.get("prompt") or "",
            "criteria": step.get("criteria") or "",
            "deliverable": step.get("deliverable") or "",
        })
    return step_id


# ============================================================
# [判断协议改注入 2026-10-14] 判断协议注入同步（BASE_PROMPT → 轮首消息事件）
# ============================================================

def _sync_judgment(state: RunState) -> None:
    """判断协议注入点：**仅本回合首次调用（``rounds == 1``）且本卡无活板时**发一条。

    用户口径（2026-10-14 12:08）：「只要有板子在，就不需要注入判断」——判断早已
    做完（已上板在跑），再注入会让模型重新判一次、跟板打架。故有活板 → 不注入。

    承载通道＝``judgment_tail``（**独立于 turn_tail**）：只随 round1 下发、绝不
    固化进轮槽，故 round≥2 的 wire_messages 与轮槽里都看不到它（任务书 §2.3）。
    整段零阻断。
    """
    if not thinkchain_enabled():   # [纯单模型 2026-10-17] 关链：不判型、不注入判断协议
        return
    if state.rounds != 1:
        return
    # [更新现状按钮改造 2026-10-16] 「更新现状」专用回合**不注入判断协议**：
    # 本轮任务已由按钮定死（更新现状），不存在「直接答 or 拆解上板」的入口判断；
    # 注进去反而可能让模型去 create_plan 拆板，与专用任务打架（专用通道口径）。
    if state.op == card_tools.OP_UPDATE_STATUS:
        return
    try:
        if thinkchain_board.has_live_board(state.work_id, state.card_id):
            return   # 有活板 → 不注入判断（用户 2026-10-14 口径）
    except Exception as exc:  # noqa: BLE001 注入零阻断；查板异常＝保守不注入
        logger.warning("[判断协议] 查板失败（保守不注入）card=%s: %s", state.card_id, exc)
        return
    try:
        ev = current_turn.build_judgment_event()
        event_stream.emit(state.work_id, state.card_id, ev["type"], ev["data"])
        state.judgment_tail.append(current_turn.project_judgment_message())
    except Exception as exc:  # noqa: BLE001 注入零阻断
        logger.warning("[判断协议] 注入失败（不阻断）card=%s: %s", state.card_id, exc)


# ============================================================
# [更新现状按钮改造 2026-10-16] 「更新现状」专用回合 · 轮首任务注入
# ============================================================

def _sync_status_task(state: RunState) -> None:
    """专用操作注入点：**仅 op=update_status 回合、仅本回合首次调用（rounds==1）** 发一条。

    承载通道＝``turn_tail``（**与判断协议不同**：本注入是**整轮的任务指令**，模型在
    工具续跳轮仍须看得到「查完要调 propose_status 交差」——故走 turn_tail 随轮槽固化，
    而不是 judgment_tail 那种「round1 用完即弃」）。round1 随用户消息之后下发，
    rounds≥2 由 ``_engage_slot`` 固化进轮槽。整段零阻断。

    事件与投影：``current_turn.build_status_task_event``（含当前现状全文）＋
    ``project_status_task_message``（role=assistant、抬头〔现状更新〕）。
    """
    if state.op != card_tools.OP_UPDATE_STATUS:
        return
    if state.rounds != 1 or state.status_task_emitted:
        return
    state.status_task_emitted = True
    try:
        ev = current_turn.build_status_task_event(state.work_id, state.card_id)
        event_stream.emit(state.work_id, state.card_id, ev["type"], ev["data"])
        state.turn_tail.append(current_turn.project_status_task_message(ev["data"]))
    except Exception as exc:  # noqa: BLE001 注入零阻断
        logger.warning("[现状更新] 注入失败（不阻断）card=%s: %s", state.card_id, exc)



# ============================================================
# [当前轮注入消息事件 2026-10-14] 当前轮注入同步（160/165 → 消息事件）
# ============================================================

def _sync_injection(state: RunState) -> None:
    """注入事件同步点：**每个模型调用之前调一次**（见 step() 首部）。

    - 选源（工具卡优先，同改造前 160/165 互斥）由 current_turn 定；
    - 固定总领每 run 只发一次（``preamble_emitted`` 闸）；
    - 动态步骤按幂等键去重（同 run 同键不重发；步骤身份或内容变化才发新）；
    - 落地：``rounds<=1`` → 暂存 ``turn_tail``（首轮随用户消息之后下发）；
      ``rounds>=2`` → 直接 ``append_plain`` 进轮槽（位置＝上一步推进事件之后、
      下一步首个动作之前）。
    无来源 → 不发任何事件（没有来源就不发注入事件）。整段零阻断。
    """
    try:
        events = current_turn.build_injection_events(state.work_id, state.card_id)
    except Exception as exc:  # noqa: BLE001 注入零阻断
        logger.warning("[注入] 构建失败（不阻断）card=%s: %s", state.card_id, exc)
        return
    new_msgs: list[dict[str, Any]] = []
    for ev in events:
        ev_type = ev.get("type") or ""
        data = ev.get("data") or {}
        if ev_type == current_turn.TYPE_PREAMBLE:
            if state.preamble_emitted:
                continue
            state.preamble_emitted = True
        else:
            key = current_turn.injection_key(ev_type, data)
            if key in state.injection_keys:
                continue
            state.injection_keys.add(key)
        event_stream.emit(state.work_id, state.card_id, ev_type, data)
        new_msgs.append(current_turn.project_message(ev_type, data))
    if not new_msgs:
        return
    if state.rounds <= 1:
        state.turn_tail.extend(new_msgs)
    elif state.slot is not None:
        for m in new_msgs:
            state.slot.append_plain(m)


async def _land_round_reply(state: RunState, content: str) -> None:
    """[5715] 落**本轮**的 assistant_text 事件（每轮一条，round＝轮号）。

    与 live 路径同构：live 用 ``stream:{run}:{round}`` 节点，本函数让 disk 路径
    也有「每轮一个 assistant 节点」——**这是刷新前后时序一致的前提**。

    - 正文为空也落（那一轮只有思考、没有正文）⇒ 成为「思考-only 的一步」，
      与 dsh 的 assistant-step 语义一致；
    - 只进**事件日志**，不写会话块 ⇒ 模型上下文零变化（会话块仍由 finish 落
      clean_text 合并全文，供下一回合读）；
    - 落盘失败零阻断（事件日志不是主流程）。
    """
    try:
        await session_facade.emit_message_event(
            state.work_id, state.card_id, "assistant_text",
            str(content or ""),
            run_id=str(state.pair_id or ""), round_=int(state.rounds or 0),
            model=str(state.model_used or ""),
        )
    except Exception as exc:  # noqa: BLE001 事件落盘零阻断
        logger.warning("[5715] 本轮正文事件落盘失败（不阻断）card=%s round=%s: %s",
                       state.card_id, state.rounds, exc)


def _engage_slot(state: RunState) -> None:
    """把 round1 暂存的注入消息一次性固化进轮槽（幂等）。

    round1 的注入经 ``turn_tail`` 随用户消息之后下发；一旦本回合进入后续轮
    （轮槽被使用），必须把它们落进轮槽——否则 rounds>=2 的 ``wire_messages``
    会丢掉它们。**只固化注入消息**（``turn_tail`` 不含 user 原文，user 仍走
    既有 ``user_prompt``/``append_plain`` 路径，本函数不改它）。

    [5715] 插话消息（``steer_tail``）同机制固化：首轮收到的插话若暂存在
    ``turn_tail``，会被 ``turn_tail_engaged`` 幂等闸挡在门外（engaged 已是 True
    ⇒ 永远不被固化 ⇒ 丢失），故插话用**独立字段**、在本函数里一并固化。
    """
    if state.turn_tail_engaged:
        return
    state.turn_tail_engaged = True
    if state.slot is None:
        return
    for m in state.turn_tail:
        state.slot.append_plain(m)
    # [5715] 首轮插话：固化在**用户原文之后**（用户原文在同段先固化）⇒ 顺序正确。
    for m in state.steer_tail:
        state.slot.append_plain(m)
    state.steer_tail = []


def _apply_steer(state: RunState, into_slot: bool = False) -> int:
    """取走本卡的待投插话并**插进轮槽**——下一次模型调用即可见。

    [5715] 落点纪律：
    - ``into_slot=True``（**收口时**插话）：直接进轮槽 ⇒ 下一轮 prior 立刻带上它。
      收口时的插话是「模型这轮没看到、用户又补了一句」⇒ 必须真的进下一轮上下文。
      （若走 steer_tail，无工具轮的 steer_tail 永远不被固化 ⇒ 插话丢失——实测踩过。）
    - 轮首调用（默认）：rounds==1 ⇒ 暂存 ``steer_tail``，随 ``_engage_slot`` 固化
      （**在用户原文之后**，顺序与语义都对）；rounds>=2 ⇒ 直接进轮槽（续跳轮的
      ``wire_messages`` 直接带上它，tool 之后跟 user 是合法形态）。
    """
    msgs = steer.take_steer(state.work_id, state.card_id)
    if not msgs:
        return 0
    prepared = [{"role": "user", "content": steer.STEER_MARK + t} for t in msgs]
    if into_slot or state.rounds > 1:
        if state.slot is not None:
            for m in prepared:
                state.slot.append_plain(m)
    else:
        state.steer_tail.extend(prepared)
    logger.info("[steer] 插话入槽 card=%s round=%s 条数=%s into_slot=%s",
                state.card_id, state.rounds, len(prepared), into_slot)
    return len(prepared)


def _board_all_steps_done(work_id: str, card_id: str) -> bool:
    """板是否**全部步 done**（以板的事实为准，不看「谁交的最后一步」）。

    [5715 · 修] 为什么要有这个：链完成原先只认「**主模型**那次 step_complete 的
    返回值」里的 ``all_steps_done``。但末步可能��**板自己的执行器**交掉的（主模型
    随后拿到的是「本步结果已收过」的幂等回执）⇒ ``state.chain_completed`` 永远是
    False ⇒ 明明板已全 done，提交轮三席却**不会触发**——而末步的工具结果里明写
    「随后进入提交轮：三席将核对本步成果是否满足判据」⇒ **承诺落空**。

    ⇒ 判据改为：**板全 done ⇒ 链完成**（板是事实，工具返回值是回执）。
    """
    try:
        snap = thinkchain_board.snapshot_sync(work_id, card_id)
    except Exception:  # noqa: BLE001 读板失败＝按「未完成」处理，绝不抛
        return False
    if not snap:
        return False
    steps = snap.get("steps") or []
    if not steps:
        return False
    return all(
        str(s.get("status") or "") == "done"
        for s in steps if isinstance(s, dict)
    )


async def step(state: RunState) -> StepOutcome:
    """一次状态推进＝工具循环的一个显式状态点。

    outcome：
      continue —— 本轮执行了工具（或喂了抢救反馈），应继续下一轮
      seats    —— 本轮无工具且判定该进三席（caller 负责其后置流程）
      deliver  —— final_reply 已定，可交付
      error    —— 本轮出错（error 字段已填，走错误返回）
    """
    verdict = check_control(state)   # 中止缝：入口挂点（[A8/R3] 控制三态）
    if verdict == control.STOP:
        # 人工停止：回合就地闭合（不落半截轮——assistant 块 end=human_stop）
        state.final_reply = state.final_reply if state.final_reply is not None else ""
        return StepOutcome("deliver", "human_stop")
    if verdict == control.DEFER_HUMAN:
        return StepOutcome("deliver", "policy_stop")

    # [思维链真实落地 2026-10-11] 判断归模型（C1）：判断协议已随 BASE_PROMPT
    # 进 system 区，模型首轮带全量工具（含 create_plan）读完协议自主判断——
    # 直答（空手正文即答复）或调 create_plan 拆解上板。代码层零判定、零
    # 机械预检、无独立判定调用（旧 JSON 判定轮已退役）。

    state.rounds += 1
    # [5715] 插话入槽：**每次模型调用前**取一次（在 prior 装配之前）——
    # 续跳轮的 wire_messages 直接带上它 ⇒ 用户在上一轮跑的时候插的话，
    # 这一轮模型就能看到（不必等整轮收口）。
    _apply_steer(state)
    # [判断协议改注入 2026-10-14] 判断协议注入（仅轮首、且本卡无活板）——先于
    # 注入事件，使事件序＝user_message → judgment_protocol →（有板时）preamble/
    # injection → round_start。两者互斥（有板无判断、无板无注入来源），不会同现。
    _sync_judgment(state)
    # [更新现状按钮改造 2026-10-16] 「更新现状」专用回合的轮首任务注入（仅 op 回合）。
    # 放在 _sync_injection 之前 ⇒ turn_tail 里专用任务先于工作规范/流程注入。
    _sync_status_task(state)
    # [当前轮注入消息事件 2026-10-14] 模型调用前同步注入事件（160/165 消息化）：
    # 有来源才发；round1 暂存 turn_tail，后续轮直接进轮槽。放在 round_start 之前，
    # 使事件序＝user_message → preamble → injection → round_start/judgment/…
    _sync_injection(state)
    if state.rounds == 1:
        # [E-探针制 2026-10-07] 首轮＝纯主模型带工具查证（原 call_simple
        # 观点前置编排退役；三席改在工具循环收尾处进场）。
        progress.set_stage(state.work_id, state.card_id, "started", "主模型查证中")
        event_stream.emit(state.work_id, state.card_id, "round_start",
                          {"round": state.rounds, "phase": "main"})
        state.model_calls += 1
        # [迭代三 S5] 首轮接线：按当前步 type 查表取模型（无活板回落 default）
        request = compose_model_request(
            state, profile="main", prior_messages=state.prior_for_gateway,
            prior_key="history", user_content=state.user_prompt,
            turn_tail=state.turn_tail, turn_once=state.judgment_tail,
        )
        # [模型选择底座 2026-10-21] 座位只算一次，请求与日志同源
        _seat = _pick_step_model(state)
        state.model_requested = _seat
        result, state.model_used = await model_service.call_main(
            request["system_prompt"], request["user_prompt"], request["prior_messages"],
            tools=state.tools,
            model_name=_seat,
            # [层内兜底 2026-10-21] 座位挂了就换同一层的下一个（不跨层）
            fallbacks=model_config.layer_fallbacks(
                _seat, work_id=state.work_id, card_id=state.card_id),
            turn_tail=request["turn_tail"],
            turn_once=request["turn_once"],
        )
        # [DeepSeek 思考回传 2026-10-15] 暂存本轮思考，供存 assistant 条时回传
        state.turn_reasoning = result.get("reasoning_content") or ""
    else:
        # 工具续跳轮不重复咨询观点模型
        progress.set_stage(state.work_id, state.card_id, "tools",
                           f"第 {state.rounds - 1} 轮工具续跳")
        # [T11] 装配时折叠：折叠关原样返回本列表（逐字同旧）；
        # 折叠开对窗口外旧轮次压成【结论】＋【依据】（不改条数与配对）
        wire_messages = await state.slot.wire_messages()
        event_stream.emit(state.work_id, state.card_id, "round_start",
                          {"round": state.rounds, "phase": "tools"})
        state.model_calls += 1
        # [迭代三 S5] 续跳轮接线：同首轮（步中途 type 不变的板是常态；
        # 改板插步后以 current 步 type 为准）
        request = compose_model_request(
            state, profile="main", prior_messages=wire_messages,
            prior_key="tool_turn", user_content=state.user_prompt,
        )
        _seat = _pick_step_model(state)
        state.model_requested = _seat
        result, state.model_used = await model_service.call_main(
            request["system_prompt"], request["user_prompt"], request["prior_messages"],
            tools=state.tools,
            model_name=_seat,
            # [层内兜底 2026-10-21] 座位挂了就换同一层的下一个（不跨层）
            fallbacks=model_config.layer_fallbacks(
                _seat, work_id=state.work_id, card_id=state.card_id),
            turn_tail=request["turn_tail"],
            turn_once=request["turn_once"],
        )
    # [DeepSeek 思考回传 2026-10-15] 暂存本轮思考，供存 assistant 条时回传
    state.turn_reasoning = result.get("reasoning_content") or ""
    tool_calls = result.get("tool_calls") or []
    assistant_content = result.get("content") or ""

    # [等待黑洞修复 2026-10-02] 标准字段为空时，抢救正文中
    # 文本形态的工具调用（DeepSeek 原生标记泄漏，三次同族发作定案）。
    # 纯机械模式匹配：解析出的调用并入标准执行链（执行＋结果回注），
    # 模型收到真实反馈；疑似标记解析不出调用时，把「未识别未执行」
    # 这个机械事实回注下一轮——关闭等待黑洞。
    text_rescue_note = ""
    if not tool_calls and assistant_content:
        rescue_calls, unresolved, stripped = \
            text_tool_rescue.extract_text_tool_calls(assistant_content)
        if rescue_calls:
            tool_calls = rescue_calls
            assistant_content = stripped or assistant_content
            # stdlib logging 只吃位置参数（%s 占位）——本模块 logger 是
            # logging.getLogger 而非 infra.logging.get_logger（structlog），
            # kwargs 写法会 TypeError（2026-10-03 首次点火定案）。
            logger.info(
                "text_tool_calls_rescued count=%s names=%s",
                len(rescue_calls), [c["name"] for c in rescue_calls],
            )
            event_stream.emit(state.work_id, state.card_id, "rescue",
                              {"count": len(rescue_calls)})
        elif unresolved:
            text_rescue_note = text_tool_rescue.feedback_note()

    # [模型过程数据 2026-10-12] 判断事件（新增）：**首轮机械推断**——tool_calls
    # 含 create_plan → plan，否则 direct。只读 tool_calls 事实（含抢救并入的
    # 调用），不解析模型文本、不加独立判定调用；首轮之后不再重发。
    if state.rounds == 1 and thinkchain_enabled():   # [纯单模型 2026-10-17]
        event_stream.emit(state.work_id, state.card_id, "judgment", {
            "verdict": "plan" if any(tc.get("name") == "create_plan"
                                     for tc in tool_calls) else "direct",
        })

    # [迭代二 S4] 催化挂点一（主循环路径）：本卡有活板且本步未推进、本轮
    # **空手**（两条件缺一不可——判据＝tool_calls 事实，防嘴上伪造；带工具
    # 轮照常执行）→ 按附录 C 步执行提示词催促交作业续跳，不设上限。两道
    # 天然刹车：每圈入口 check_control（人停即刹）＋用户新消息＝新回合。
    # 空手正文不入槽（同 :316 抢救先例——催促文自带本步要求原文，信息自足）。
    if (thinkchain_enabled() and not tool_calls
            and thinkchain_board.has_live_board(state.work_id, state.card_id)):
        _snap = thinkchain_board.snapshot_sync(state.work_id, state.card_id)
        _step = None
        if _snap:
            _steps = _snap.get("steps") or []
            _cur = _snap.get("current", 0)
            if 0 <= _cur < len(_steps):
                _step = _steps[_cur]
        if _step is not None:
            state.user_prompt = (
                f"{thinkchain_board.step_brief(_step)}\n（干完立刻调 step_complete；"
                "未调之前这一步不算完成。）"
            )
            _engage_slot(state)   # 注入消息随轮槽带入后续轮
            return StepOutcome("continue", "await_step_tool")

    if not tool_calls:
        # [5715] 插话优先于收口：模型这一轮没调工具、正文已定（**正要收口**），
        # 但用户在模型跑的时候插了话 ⇒ 不收口，把插话插进轮槽、继续下一轮。
        # 否则那句话会被丢掉（回合收口即清扫残留）——用户说了话，却没人听见。
        if steer.pending_steer(state.work_id, state.card_id) > 0:
            _apply_steer(state, into_slot=True)
            state.user_prompt = "用户在你回答时插了话。先读那条插话，再继续。"
            return StepOutcome("continue", "steered")
        state.final_reply = assistant_content
        # [5715] 末轮也落一条（每轮一条 ⇒ disk 与 live 同构）。
        await _land_round_reply(state, assistant_content)
        if text_rescue_note:
            # 反馈兜底：疑似调用未被识别这个事实必须回到模型上下文，
            # 否则模型永远在等一个不会来的结果（等待黑洞）。
            _engage_slot(state)   # 注入消息随轮槽带入后续轮（先于本轮 assistant 回注）
            # [T11] 经轮槽 append_plain 入槽（非轮消息，永不折叠）
            state.slot.append_plain({
                "role": "assistant", "content": state.final_reply,
            })
            state.user_prompt = text_rescue_note
            return StepOutcome("continue", "text_rescue")
        # [思维链真实落地 2026-10-11] 提交轮三席统合（C6）：board 全部步骤
        # done（收口）后、交付前，固定触发三席质询＋统合交付。空手轮旁路
        # （旧「无工具即三席」）退役——直答回合零三席。
        if (thinkchain_enabled() and thinkchain_seats_enabled()
                and not state.seats_done):   # [规划驱动 2026-10-19] 三席另有开关
            # [5715 · 修] 链完成的判据以**板的真实状态**为准：主模型那次
            # step_complete 的返回值只是回执（末步若由板的执行器交掉，主模型拿到
            # 的是幂等回执 ⇒ 标志永远 False ⇒ 承诺的提交轮三席落空）。
            if not state.chain_completed:
                state.chain_completed = _board_all_steps_done(
                    state.work_id, state.card_id)
            if state.chain_completed:
                return StepOutcome("seats", "finalize_review")
        return StepOutcome("deliver", "chain_done" if state.chain_completed else "direct")

    # —— 有工具调用：本轮执行工具，续跳 ——
    # [phase8.2] 第一次进入工具轮时，把用户原文固化进 prior，
    # 否则后续跳的 user_prompt 被替换后原文会从对话中消失
    # [T11] 经轮槽 append_plain 入槽（首轮用户原文固化＝不折红线之一）
    if state.rounds == 1:
        state.slot.append_plain({"role": "user", "content": state.content})
    # [5715] 首轮收到的插话：紧接着用户原文固化（**语义顺序**：先说的话在前，
    # 后插的话在后）。走 steer_tail 暂存、在用户原文之后才进槽。
    if state.steer_tail and state.slot is not None:
        for m in state.steer_tail:
            state.slot.append_plain(m)
        state.steer_tail = []
    # [当前轮注入消息事件 2026-10-14] 首轮注入消息随之固化进轮槽（幂等）
    _engage_slot(state)

    # [T-0010 §2] run 级 tool_call_id：pair_id 前 8 位；无 pair_id 退 work_id 前 4 位。
    # 与「轮次＋序号」合成 ⇒ 同一份 messages 内全局唯一（B-07 配对协议），
    # 且 assistant 侧与 tool 侧取同一个值，配对不变量不动。
    _run_tag = (state.pair_id[:8] or state.work_id[:4])
    # 把 assistant 消息（含 tool_calls）+ tool 结果追加进 prior
    # [T11-T6] assistant 经 begin_round 入槽（成轮，可折单元）；
    # 消息构造与既有逐字一致（折叠关时槽即本列表，零变化）
    _asst_msg: dict[str, Any] = {
        "role": "assistant",
        "content": assistant_content,
        "tool_calls": [
            {
                "id": f"call_{_run_tag}_{state.rounds}_{i}",
                "type": "function",
                "function": {"name": tc["name"],
                             "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
            }
            for i, tc in enumerate(tool_calls)
        ],
    }
    # [DeepSeek 思考回传 2026-10-15] DeepSeek 系模型开思考时，多轮调用必须把
    # 本轮的 reasoning_content 原样回传（否则 400 "must be passed back"）。
    # 仅对 deepseek 系补——其他家不要求，多带字段可能被拒。
    if state.turn_reasoning and "deepseek" in (state.model_used or "").lower():
        _asst_msg["reasoning_content"] = state.turn_reasoning
    state.slot.begin_round(_asst_msg)
    # [T14-1-2] 只读采集：带工具轮的 assistant_content（含 L459 抢救处理后
    # 的正文）同步留一份进交付采集列表。不改 begin_round 入参、不额外入槽
    # （T11 轮槽消息条数与配对不变量不破坏，RD-09）；不限长不折叠不截断。
    if assistant_content:
        state.analysis_parts.append(assistant_content)

    # 收集卡片 meta 用于权限判定
    cards_meta = card_tools._collect_card_meta(state.work_id)

    # [工具结果落会话 2026-10-17] 本工具轮的会话块 B（工具结果）在内存收齐，
    # 循环后与块 A（assistant+tool_calls）一次性落盘（§0.1 疑问3：原子落盘，
    # 杜绝"A 已落、B 未落"的中间态——N1 红线：不得出现孤立 tool 块）。
    _round_tool_blocks: list[dict[str, Any]] = []

    try:
        for i, tc in enumerate(tool_calls):
            name = tc.get("name", "")
            args = tc.get("arguments", {}) or {}
            # [phase8.5] manuscript 工具没有 card_id：target_id 记 chapter 或 keyword，都没有则空串
            # [phase8.6-D] link_cards 记 target_id
            # [联网能力 2026-10-02] web_search 记 query、fetch_web 记 url——审计可见
            # [建卡工具化 2026-10-08] create_card 无 card_id/parent 入参（parent 可选），
            #   target_id 记 title（回执前唯一可定位的入参）
            target_id = (args.get("card_id") or args.get("target_id")
                         or args.get("chapter") or args.get("keyword")
                         or args.get("query") or args.get("url")
                         or (args.get("title") if name == "create_card" else "") or "")
            # [信息流 Task3] 工具执行前后埋点（V2 即时可见）
            # [模型过程数据 2026-10-12] step_id：执行前读当前板步（首次观测即发
            # step_start），call/result 共用同一个值——同一次调用的归属不许劈叉；
            # 无板（直答查证）→ 空串。
            _step_id = _sync_step_start(state)
            # [消息底座能力对齐 · 件3] 结构化落盘：入参原样（arguments 是 dict，
            # 不是拼好的字符串）——事件日志存的是事实，不是给模型看的投影。
            event_stream.emit(state.work_id, state.card_id, "tool_call",
                              {"tool": name, "target": target_id, "step_id": _step_id,
                               "arguments": args})
            # [提交轮材料（C6）] step_complete 执行**前**抓板快照——末步执行会
            # 同步触发收口（finalize 清理板），执行后取板已不可得。每次 step_complete
            # 前刷新快照，末步前抓到的即全量成果（含全部已推进步的 result）。
            _done_step_id = ""
            if name == "step_complete":
                snap = thinkchain_board.snapshot_sync(state.work_id, state.card_id)
                if snap and snap.get("steps"):
                    state.chain_steps_record = [
                        s for s in snap["steps"] if isinstance(s, dict)]
                    state.chain_plan_name = str(snap.get("name") or "")
                    # [模型过程数据] 被收作业的就是执行前的当前步（指针未动）——
                    # step_id 从这份快照取，与 criteria_met/result 同属一步。
                    _pre_steps = snap.get("steps") or []
                    _pre_cur = snap.get("current", 0)
                    if (isinstance(_pre_cur, int) and 0 <= _pre_cur < len(_pre_steps)
                            and isinstance(_pre_steps[_pre_cur], dict)):
                        _done_step_id = str(_pre_steps[_pre_cur].get("id") or "")
            tool_result = await card_tools.execute_tool(
                state.work_id, state.card_id, name, args, cards_meta)
            # [T-0010 §1.5] 成败读信封 ok 字段（改前是「结果里没有 error 键即算成功」）
            ok = tool_result.get("ok") is True
            _data = tool_result.get("data") or {}
            _ev: dict[str, Any] = {"tool": name, "card_id": target_id, "ok": ok}
            # [建卡工具化 2026-10-08] create_card 成功时把真身卡号/卡名/父带进
            # tool_events——交付段据此收拢工具通道建卡结果
            # [T-0010 §1] 成功载荷整体在 data 内，取值改走 data
            if name == "create_card" and ok:
                _ev["card_id"] = _data.get("card_id", "")
                _ev["title"] = _data.get("title", "")
                _ev["parent"] = _data.get("parent", "")
            state.tool_events.append(_ev)
            # [思维链真实落地 2026-10-11] 板子事件 emit（对话流可见拆解上板/步进/收口）：
            # create_plan 成功→plan_created；step_complete 成功→step_advanced
            # （all_done=True 时前端渲染收口行）。
            # [模型过程数据 2026-10-12] plan_created 补完整 steps 数组（建板成功后
            # 板上已是全字段步定义，emit 时就地取，不解析模型文本）。
            if ok and name == "create_plan":
                _bsnap = thinkchain_board.snapshot_sync(state.work_id, state.card_id) or {}
                _bsteps = _bsnap.get("steps") or []
                event_stream.emit(state.work_id, state.card_id, "plan_created", {
                    "name": _data.get("plan_name") or "",
                    "total": _data.get("total") or 0,
                    "prompt": _data.get("prompt") or "",
                    "steps": [
                        {"id": s.get("id", ""), "prompt": s.get("prompt", ""),
                         "criteria": s.get("criteria", ""),
                         "deliverable": s.get("deliverable", ""),
                         "type": s.get("type", "")}
                        for s in _bsteps if isinstance(s, dict)
                    ],
                })
                # 板刚上架＝第 1 步开始：紧随 plan_created 发 step_start（幂等键防重）
                _sync_step_start(state)
            elif ok and name == "step_complete":
                # [模型过程数据 2026-10-12] step_advanced 补步归属与成果：
                # step_id＝执行前快照的当前步；criteria_met/result＝step_complete
                # 入参（result 即落板的本步 result，同一份值）；next_prompt/
                # next_criteria＝工具返回的下一步要求与判据（末步收口时为空串）。
                event_stream.emit(state.work_id, state.card_id, "step_advanced", {
                    "advanced": bool(_data.get("advanced")),
                    "all_done": bool(_data.get("all_steps_done")),
                    "prompt": _data.get("prompt") or "",
                    "step_id": _done_step_id,
                    "criteria_met": str(args.get("criteria_met") or ""),
                    "result": str(args.get("result") or ""),
                    "next_prompt": _data.get("prompt") or "",
                    "next_criteria": _data.get("criteria") or "",
                })
                # [提交轮材料（C6）] criteria_met 自述按调用序累积（与板步对齐）；
                # all_steps_done＝链收口事实（提交轮触发依据）。
                _cm = args.get("criteria_met")
                if isinstance(_cm, str) and _cm.strip():
                    state.chain_criteria_mets.append(_cm.strip())
                if _data.get("all_steps_done"):
                    state.chain_completed = True
            # [信息流 Task4] 工具结果事件（A96 全日志打点）。card_op 并表随
            # 机械预检退役——全部工具统一 tool_result 一条通道。
            # [模型过程数据 2026-10-12] step_id 与本次 tool_call 同源（_step_id）。
            # [消息底座能力对齐 · 件3] result 落**结构化信封**（data/error 原样），
            # 不再把 render_tool_result 的文本投影当 data 落盘——落盘结构化、
            # 回灌字符串，两路各自独立（回灌侧仍走单一投影函数 render_tool_result）。
            _struct_result = ({"ok": True, "data": _data} if ok
                              else {"ok": False, "error": tool_result.get("error")})
            event_stream.emit(state.work_id, state.card_id, "tool_result",
                              {"tool": name, "target": target_id, "ok": ok,
                               "step_id": _step_id, "result": _struct_result})
            # [T11-T6] tool 结果经 append_tool_result 入槽（归入当前轮）
            # [T-0010 §1.6] 回灌文本走单一投影函数（不各自 json.dumps）——
            # 否则失败信封的 {"code","message"} 会被当数据喂进模型上下文
            _tool_call_id = f"call_{_run_tag}_{state.rounds}_{i}"
            # [工具结果注入有界 2026-10-25] 单一投影点统一截断（事件留痕不受限，只护模型上下文）
            _rendered_result = card_tools.bound_text(
                card_tools.render_tool_result(tool_result))
            state.slot.append_tool_result({
                "role": "tool",
                "tool_call_id": _tool_call_id,
                "content": _rendered_result,
            })
            # [工具结果落会话 2026-10-17] 会话块 B（工具结果）：在内存收齐，
            # 循环后与块 A 一次性落盘。tool_call_id 与 A 的调用一一配对。
            _round_tool_blocks.append({
                "actor": "tool",
                "tool_call_id": _tool_call_id,
                "content": _rendered_result,
            })
            # [E-探针制] 全文累积（口径④）：三席后置注入用，不折叠不截断
            # [T-0010 §1.6] 同一投影函数，两个调用点禁止各自拼装
            state.tool_full_texts.append(
                f"{name}(target={target_id}) → " + _rendered_result
            )

        # [工具结果落会话 2026-10-17] 本工具轮的会话块收齐后**一次性原子落盘**：
        #   块 A（actor=assistant）：content=""，tool_calls=本轮全部调用（与轮槽同源）；
        #   块 B×N（actor=tool）：tool_call_id 与 A 的调用一一配对，content=工具结果全文。
        # 顺序：A 在前、B 紧随。同锁同写 ⇒ 不存在"A 已落、B 未落"的中间态
        # （N1 红线：role=tool 必须紧跟其配对的 assistant(tool_calls)，否则端点 400）。
        # turn/pair 由门面按所在回合分配（工具块不闭合轮 ⇒ 与同轮用户/回复同 turn）。
    finally:
        # [落盘不保真修复 2026-10-22] **异常也要落**：原先这段在循环之后，
        # run 中途异常/中止就整轮工具结果丢失（实测 card_005 请求里有 110K 字符的
        # 工具结果，会话里同 tool_call_id 的块一条都没有）⇒ 落盘比真实请求少 3 倍，
        # 三尺永远对不齐。放进 finally 保证必落；落盘失败只记日志、不吞原异常。
        if _round_tool_blocks:
            try:
                await session_facade.append_many(state.work_id, state.card_id, [
                    {"actor": "assistant", "content": "",
                     "tool_calls": _asst_msg["tool_calls"]},
                    *_round_tool_blocks,
                ])
            except Exception as _exc:  # noqa: BLE001
                logger.error("[工具块落盘] 失败 card=%s: %s", state.card_id, _exc)
    # [5715 · 修] 本轮**单独**落一条 assistant_text 事件（只进事件日志，不写会话块）。
    #
    # 病根（用户亲报「长程调用链之后刷新，工具归工具、内容给内容、信息顺序全乱」）：
    # 带工具轮的正文只进 state.analysis_parts，收口时与末轮回执**合并成一条**落盘
    # ⇒ 该块必然排在**该 run 全部工具之后**；而**每轮各自的思考**在落盘路径下
    # 只能「并进最近一条 assistant」⇒ 同 run 内跨轮串、跨 run 更串。
    # 根子在：**disk 路径没有「每轮一个 assistant 节点」的概念**，而 live 路径有
    # （stream:{run}:{round}）⇒ 刷新前后两套归组规则不同源。
    #
    # 修法：让 disk 路径与 live 路径同构——**每轮一条 assistant_text 事件**
    # （round＝轮号）。收口那条（clean_text 合并全文）**不再落事件**（会话块照落
    # ⇒ 模型上下文零变化），免得与各轮正文重复。
    await _land_round_reply(state, assistant_content)
    # [工具轮去假 user 2026-10-21] 原来这里塞占位符「工具结果已返回。」填用户位——
    # 模型会把这句话当成**用户说的话**（实测每轮思考都在念它）。工具轮的 prior 末尾
    # 已是 role=tool 的工具结果，模型据此直接续写；用户位**留空**，不再编造发言。
    # （gateway._build_options 已改为「user_prompt 为空则不拼 user 消息」。）
    state.user_prompt = ""
    logger.info("[flow] 断点续跳 resume_kind=同请求续跳 no_user_message=True round=%s card=%s",
                state.rounds, state.card_id)
    verdict = check_control(state)   # 中止缝：出口挂点（[A8/R3] 控制三态）
    if verdict == control.STOP:
        state.final_reply = state.final_reply if state.final_reply is not None else ""
        return StepOutcome("deliver", "human_stop")
    if verdict == control.DEFER_HUMAN:
        return StepOutcome("deliver", "policy_stop")
    return StepOutcome("continue", "tools_executed")


# ============================================================
# 段③b：提交轮三席统合（C3/C6，思维链真实落地 2026-10-11）
# ============================================================

async def run_seats(state: RunState) -> StepOutcome:
    """提交轮：三席质询 → 统合官统合 → 交付。

    [思维链真实落地 2026-10-11] 触发时机唯一＝board 全部步骤 done（收口）
    后、交付前（C6：完成判据核对只在最终提交轮，非每步都三席；直答回合
    零三席——旧空手轮旁路退役）。三席输入＝全部步的 result ＋判据满足
    情况（chain_steps_record/chain_criteria_mets）＋本轮工具结果全文，
    质询「成果真满足判据了吗、解决用户问题了吗」；统合官收敛成最终交付。

    上下文回流铁则：观点只进统合官 user_prompt，不入槽/不入 session；
    三席 prior 取纯对话历史切片（不含本轮工具机制消息，且只读不回写）。

    [三席身份注入 2026-10-14] 三席身份提示词走注入（role=assistant 自述、抬头
    〔三席身份〕），经 turn_tail 随三席调用下发——三席 system 与 user 位质询文
    都不动；注入不进 session/prior。
    """
    state.seats_done = True
    # [信息流 Task3] 三席进场埋点＋每席起止转发（on_seat 缺省 None 语义不变）
    event_stream.emit(state.work_id, state.card_id, "seats_enter",
                      {"tool_results": len(state.tool_full_texts),
                       "steps": len(state.chain_steps_record)})
    # [三席身份注入 2026-10-14] 三席身份提示词——走注入（role=assistant 自述），
    # 随三席调用经 turn_tail 下发；三席 system（共享上下文）与 user 位质询文都不动。
    # 单列事件一类（不复用 context_preamble——语义与前端标签都不同）。
    seat_identity = simple_service.build_seat_identity_message()
    event_stream.emit(state.work_id, state.card_id, "context_seat_identity",
                      {"source_kind": "viewpoint",
                       "content": simple_service.SEAT_IDENTITY_NOTE})
    seats_prior = state.prior_for_gateway[:state.history_len]
    review_input = simple_service.build_review_prompt(
        state.content,
        state.chain_steps_record,
        state.chain_criteria_mets,
        state.tool_full_texts,
    )
    seat_request = compose_model_request(
        state, profile="viewpoint", prior_messages=seats_prior,
        prior_key="history", user_content=review_input,
        seat_identity=[seat_identity],
    )
    ok_metas, state.viewpoints_meta = await simple_service.run_viewpoints(
        seat_request["system_prompt"],
        seat_request["user_prompt"],
        seat_request["prior_messages"],
        on_progress=lambda s, d, _w=state.work_id, _c=state.card_id:
            progress.set_stage(_w, _c, s, d),
        on_seat=lambda kind, meta, _w=state.work_id, _c=state.card_id:
            event_stream.emit(_w, _c, kind, meta),
        turn_tail=seat_request["turn_tail"],
        # [模型设置按项目/卡片覆盖 2026-10-19] 三席按当前 work/card 作用域选模型
        work_id=state.work_id,
        card_id=state.card_id,
    )
    if not ok_metas:
        # [E-口径②] 全败留痕不阻断：主模型携步成果独自交付（草稿即回执）
        if state.viewpoints_meta:
            logger.warning("[提交轮] 三席观点全败（%d 席尝试），主模型独自交付 card=%s",
                           len(state.viewpoints_meta), state.card_id)
        return StepOutcome("deliver", "seats_all_failed")
    # 统合官最终统合交付（合同不动 RD-03：同 call_main、带 tools）
    progress.set_stage(state.work_id, state.card_id, "synthesize", "三席观点统合中")
    logger.info("[提交轮] 三席质询进场 步成果=%d条 工具结果=%d条 成功席=%d card=%s",
                len(state.chain_steps_record), len(state.tool_full_texts),
                len(ok_metas), state.card_id)
    synth_input = simple_service.build_viewpoint_prompt(state.content, ok_metas)
    event_stream.emit(state.work_id, state.card_id, "round_start",
                      {"round": state.rounds, "phase": "synthesize"})
    state.model_calls += 1
    synth_request = compose_model_request(
        state, profile="main", prior_messages=state.prior_for_gateway,
        prior_key="tool_turn", user_content=synth_input,
    )
    # [迭代五 S2·E10] 统合官模型可配：model_pick.resolve_model("synthesize")，
    # 未配置回落默认模型（model_config.default_model()，缺省行为逐字不变）。
    # [模型设置按项目/卡片覆盖 2026-10-19] 带 work/card 作用域解析。
    # [模型选择底座 2026-10-21] 座位只算一次：请求与日志同源。
    _synth_seat = model_pick.resolve_model("synthesize",
                                          work_id=state.work_id,
                                          card_id=state.card_id)
    state.model_requested = _synth_seat
    result, state.model_used = await model_service.call_main(
        synth_request["system_prompt"], synth_request["user_prompt"],
        synth_request["prior_messages"], tools=state.tools,
        model_name=_synth_seat,
        fallbacks=model_config.layer_fallbacks(
            _synth_seat, work_id=state.work_id, card_id=state.card_id),
        turn_tail=synth_request["turn_tail"],
        turn_once=synth_request["turn_once"],
    )
    tool_calls = result.get("tool_calls") or []
    assistant_content = result.get("content") or ""
    if not tool_calls:
        state.final_reply = assistant_content
        return StepOutcome("deliver", "synthesize")
    # 统合官仍需查证：清空草稿、借道工具执行段续跑（无闸 RD-01）；
    # seats_done 保证三席不再有第二次。末轮草稿由统合稿取代。
    state.final_reply = None
    return StepOutcome("continue", "synthesize_needs_tools")


async def _write_chain_summary(state: RunState) -> None:
    """提交轮统合完成后，收口结论回写卡面 status_block（一次）。

    内容四样：计划名／终态／统合结论／各步成果与判据满足摘要。fail-open
    （回写失败留运维日志，不阻断交付——final 块与 thinkchain_final 终态键
    已在收口时落盘，孤儿扫描兜底不依赖本回写）。
    """
    try:
        lines: list[str] = []
        for s in state.chain_steps_record:
            if not isinstance(s, dict):
                continue
            lines.append(
                f"- 步 {s.get('id', '')} [{s.get('status', '')}] "
                f"{s.get('prompt', '')}\n"
                f"  判据: {s.get('criteria', '')}\n"
                f"  成果: {(s.get('result') or '').strip()[:500]}")
        conclusion = (state.final_reply or "").strip()
        status_text = (
            f"【思维链收口】\n"
            f"计划名: {state.chain_plan_name}\n"
            f"终态: 已收口（提交轮三席统合）\n"
            f"结论: {conclusion[:2000] if conclusion else '（统合稿为空）'}\n"
            f"步成果:\n" + ("\n".join(lines) if lines else "（无步记录）")
        )
        result = await card_store.update_card(
            state.work_id, state.card_id, {"status_block": status_text},
            "思维链提交轮统合结论回写",
        )
        if isinstance(result, dict) and "error" in result:
            logger.warning("提交轮结论回写被拒 card=%s: %s",
                           state.card_id, result.get("error"))
    except Exception as exc:  # noqa: BLE001 回写失败不阻断交付
        logger.warning("提交轮结论回写异常 card=%s: %s", state.card_id, exc)


# ============================================================
# 段④：prompt 日志（无论成败都写）
# ============================================================

# [T11-T7] regions＝六区构成（只存指针与指纹）；fold_ops＝每轮折叠
# 原字节/折后字节/结果。两键非空才写入（约束 13）；prior_messages 仍落
# 原文全量（轮槽不改写原列表，折叠账在 fold_ops 对账）。
def write_logs(state: RunState) -> None:
    """写 prompt 日志＋viewpoints 追加。"""
    history_len = state.history_len
    log_regions = context_regions.build_log_regions(
        state.ctx_regions,
        state.prior_for_gateway[:history_len],
        state.prior_for_gateway[history_len:],
        state.content,
    )
    log_path = prompt_log.write_prompt_log(
        card_id=state.card_id,
        work_id=state.work_id,
        system_prompt=state.system_prompt,
        prior_messages=state.prior_for_gateway,
        user_prompt=state.content,
        model_used=state.model_used if state.error is None else None,
        # [模型选择底座 2026-10-21] 记**实际请求**的模型（座位），不是另算一次默认。
        model_requested=state.model_requested,
        # [T14-3] response 保末轮供审计（拼接在日志写之后，此时 final_reply 仍是
        # 末轮正文——prompt_log.response 语义不动，RD-08）；交付全文（分析＋回执）
        # 可经会话 assistant 条/prompt 日志 prior_messages 复核。
        response=state.final_reply if state.error is None else None,
        error=state.error,
        tool_events=state.tool_events,
        pair_id=state.pair_id,          # [T10-5-3] 日志关联
        regions=log_regions,
        fold_ops=state.slot.fold_ops or None,
    )
    # [T-0010 §3.1] 记住路径：段⑥ 文本通道计数完成后就地追加该键
    # （与 viewpoints 追加同一手法，不新增第二次写盘）
    state.prompt_log_path = str(log_path) if log_path is not None else None
    # [phase9-E] 日志追加 viewpoints 字段（观点全文数组，落盘可查）。
    # [白板证据位 2026-10-02] vp_system_prompt 一并落日志。
    # [phase9-E] 日志追加 viewpoints 字段（观点全文数组，落盘可查）。
    # [白板证据位 2026-10-02] vp_system_prompt 一并落日志：三思考席实际收到的
    # system prompt（应仅为中性卡片清单+当前卡，无使用说明/工具/建卡接口）。
    if log_path is not None:
        try:
            _json = json
            payload = _json.loads(log_path.read_text(encoding="utf-8"))
            if state.viewpoints_meta:
                payload["viewpoints"] = [
                    {"model": m.get("model"), "ok": m.get("ok"),
                     "latency_ms": m.get("latency_ms"), "chars": m.get("chars"),
                     "error": m.get("error"), "text": m.get("text", "")}
                    for m in state.viewpoints_meta
                ]
            payload["vp_system_prompt"] = state.vp_system_prompt
            log_path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception as exc:
            logger.warning("prompt 日志 viewpoints 追加失败（不中断）: %s", exc)


# ============================================================
# 段⑤：T14 拼接交付
# ============================================================

# [T14-2] 拼接交付全文：分析在前、回执在后、\n\n 分隔（待裁定 1）。落位命门：
# 在日志写（response 保末轮，RD-08）之后、parse_proposals 之前——解析/落盘/
# 返回三处共用拼接后的 final_reply（任务 4）。V7 三边界：
#   有分析有回执＝正常拼接；有分析无回执＝只交付中间轮；
#   皆空＝空回复；无分析有回执＝纯对话轮原样（回归不变）。
def compose_delivery(state: RunState) -> str:
    # [5715] 末轮原始正文先存下（此刻 state.final_reply 还是末轮原文；调用方随后会把它
    # 覆盖成拼接后的 delivered）。收口事件只落它，避免与「各轮正文按轮落的事件」重复。
    state.final_only = str(state.final_reply or "")
    if state.analysis_parts:
        if state.final_reply:
            state.branch = "normal"
            delivered = "\n\n".join(state.analysis_parts + [state.final_reply])
        else:
            state.branch = "analysis-only"
            delivered = "\n\n".join(state.analysis_parts)
    elif state.final_reply:
        state.branch = "reply-only"
        delivered = state.final_reply
    else:
        state.branch = "empty"
        delivered = ""
    logger.info(
        "[T14] 分析交付拼接 branch=%s 分析段数=%s 拼接后字节=%s 末轮字节=%s",
        state.branch, len(state.analysis_parts),
        len(delivered.encode("utf-8")),
        len((state.final_reply or "").encode("utf-8")),
    )
    # [信息流 Task3] 交付拼接埋点（deliver 与 POST 一致性锚，V4）
    event_stream.emit(state.work_id, state.card_id, "deliver",
                      {"branch": state.branch,
                       "bytes": len(delivered.encode("utf-8"))})
    return delivered


# ============================================================
# 段⑥：建卡（工具通道收拢＋文本协议块兜底）
# ============================================================

# [建卡工具化 2026-10-08] 建卡 · 工具通道 ＋ 文本通道并存，跨通道去重。
# 建卡主通道＝标准工具 create_card（卡片带回执进 tool_events，模型能确证建成）；
# 本段只做两件事：
#   ① 收拢工具通道已建的卡（tool_events 里 create_card 且ok 的项）
#   ② 文本协议块兜底（模型过渡期仍写 card-proposal）——按同轮工具通道
#      已建的卡名去重，防同一轮建两次；并施加同一权限口径（只能建自己的子卡片）
async def create_cards(state: RunState, final_reply: str) -> None:
    # [T14-4-1/6] 文本通道扫描输入＝拼接后全文（含分析正文）——照既有机制运行
    # （RD-11 不新增筛选不设闸）；观察点日志使「扫描面是否含工具轮正文」可事后审计。
    progress.set_stage(state.work_id, state.card_id, "cards", "建卡")
    state.created_cards = []
    for ev in state.tool_events:
        if ev.get("tool") == "create_card" and ev.get("ok"):
            state.created_cards.append({
                "card_id": ev.get("card_id", ""),
                "title": ev.get("title", ""),
                "parent": ev.get("parent", ""),
                "channel": "tool",
            })
    tool_titles = {c["title"] for c in state.created_cards if c.get("title")}
    logger.info(
        "[T14] 文本建卡扫描输入 分析段数=%s 交付分支=%s 扫描总字节=%s 工具通道已建=%d",
        len(state.analysis_parts), state.branch,
        len((final_reply or "").encode("utf-8")), len(state.created_cards),
    )
    clean_text, proposals = card_proposal.parse_proposals(final_reply or "")
    state.clean_text = clean_text
    text_created, state.denied_cards = await card_proposal.apply_proposals(
        state.work_id, state.card_id, proposals, exclude_titles=tool_titles,
    )
    for c in text_created:
        state.created_cards.append({**c, "channel": "text"})
    # [T-0010 §3.1] 文本通道成功建卡计数 → prompt 日志追加键（为 0 则不写，
    # 与 regions/fold_ops 同一「空则不同构」纪律）。rescue 兼容层退场后，
    # 该键自然恒不出现——这正是「通道收敛」的可观测收口。
    state.text_channel_create_count = len(text_created)
    if state.text_channel_create_count and state.prompt_log_path:
        try:
            from pathlib import Path as _Path
            _p = _Path(state.prompt_log_path)
            _payload = json.loads(_p.read_text(encoding="utf-8"))
            _payload["text_channel_create_count"] = state.text_channel_create_count
            _p.write_text(json.dumps(_payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            logger.info("[T-0010] 文本通道建卡计数入日志 count=%s file=%s",
                        state.text_channel_create_count, _p.name)
        except Exception as exc:  # noqa: BLE001 观测键失败不阻断建卡
            logger.warning("prompt 日志 text_channel_create_count 追加失败（不中断）: %s", exc)
    if state.denied_cards:
        # 越权/去重的项如实回给模型上下文——不静默丢卡
        _d = "；".join(f"{d['title']}（{d['reason']}）"
                       for d in state.denied_cards[:5])
        state.slot.append_plain({
            "role": "assistant",
            "content": f"[系统回执] 本轮有 {len(state.denied_cards)} 张卡未建成：{_d}。"
                       f"建卡只能建自己的子卡片（parent 须落在你的子树内，缺省＝当前卡自身）。",
        })
        logger.warning("[建卡未建] %d 项被拒 card=%s 明细=%s",
                       len(state.denied_cards), state.card_id, state.denied_cards)
    # [信息流 Task3] 建卡埋点（✚ 可见）
    event_stream.emit(state.work_id, state.card_id, "proposal", {
        "count": len(state.created_cards),
        "cards": [str(c.get("card_id", "")) for c in state.created_cards],
    })


# ============================================================
# 段⑧：assistant 落盘＋收尾
# ============================================================

async def finish(state: RunState) -> dict[str, Any]:
    """追加 assistant 消息＋回合末凝练触发＋done 埋点，返回响应体。"""
    # [收口结论回写（笔 3）] 统合稿并入提交轮输出：卡面 status_block 回写
    # （计划名＋终态＋统合结论＋各步成果摘要）。旧 finalize 卡面回写已挪
    # 至此——统合输出才是收口结论（任务书笔 3 ①）。落位在 finish（回合
    # 收尾唯一路径）＝统合官是否中途查证都回写（run_seats continue 分支
    # 曾漏，2026-10-12 真链路实测发现即修）。
    if state.chain_completed:
        await _write_chain_summary(state)
    # [T10-1-4] 回复条携带与用户条逐字相同的 pair_id。
    # [A6b/T024] 写切换：门面追加（V1 文件现场升格；end 显式传入——正常回合
    # 缺省 model_done，human_stop/policy_stop 由控制面消费段写定）。
    # [5715] 事件流：**不再落**这条（各轮正文已由 _land_round_reply 逐轮落了 ⇒
    # 这里再落一遍 clean_text 拼接全文＝同一段话出现两遍）。
    # **会话块 content 仍是 clean_text，一字未改** ⇒ 模型上下文零变化
    # （clean_text 的作用是把各轮正文补进会话文件供下一回合读——那部分完全保留）。
    await session_facade.append(state.work_id, state.card_id, {
        "role": "assistant", "content": state.clean_text,
        "model": state.model_used, "pair_id": state.pair_id,
        "end": state.end_reason or "model_done",
    }, emit_event=False)
    logger.info("[T10] 回复落盘 pair_id=%s card=%s end=%s",
                state.pair_id, state.card_id, state.end_reason or "model_done")
    # [手动停止板清理 2026-10-14] 中止态（human_stop/policy_stop）清理残留板：
    # 手动停止/判不了交人后，计划板不该残留——否则下次发消息 has_live_board=True
    # 会复活已停止的旧计划。error 不清理（checkpoint 保留，供下次恢复续跑）。
    if state.end_reason in ("human_stop", "policy_stop"):
        try:
            _b = await thinkchain_board.get_board(state.work_id, state.card_id)
            if _b is not None:
                await _b.cleanup_board()
        except Exception as _exc:  # noqa: BLE001 板清理失败不阻断落盘
            logger.warning("中止态板清理失败 card=%s: %s", state.card_id, _exc)
    # [T12-SP-04] 回合末凝练触发（condense_fold）：assistant 落盘之后异步触发、
    # 不阻塞对话（异步是工程手段非闸，RD-04）。开关缺省关（RD-08）＝零动作；
    # 开时三期管线仍挂起待路线裁定，触发只量测＋留痕（memory_store.condense_turn_end）。
    memory_store.maybe_schedule_turn_end(state.work_id, state.card_id)
    progress.set_stage(state.work_id, state.card_id, "done", "完成")
    # [信息流 Task3/打点 A96] done 事件
    event_stream.emit(state.work_id, state.card_id, "done",
                      {"rounds": state.rounds, "model_calls": state.model_calls})
    return {
        "reply": state.clean_text,
        "model": state.model_used,
        "created_cards": state.created_cards,
        "tool_events": state.tool_events,
        # [phase9-E] 观点元信息（剔除内部 text，观点全文只落 prompt 日志，
        # 不进 session、不进 prior_messages——上下文回流铁则）
        "viewpoints_meta": [
            {k: v for k, v in m.items() if k != "text"} for m in state.viewpoints_meta
        ],
    }


# ============================================================
# 错误收口（段③的 except 分支）
# ============================================================

def handle_error(state: RunState, exc: Exception) -> None:
    """工具循环异常收口（checkpoint ＋ error 埋点，不阻断错误返回）。

    [信息流 Task2·A93] 确须结束型的（上下文超限等）：checkpoint 落
    data_snapshot 定格现状，下条消息从现状续走而非全链重跑。
    """
    state.error = str(exc)
    progress.set_stage(state.work_id, state.card_id, "error", str(exc)[:120])
    logger.error("模型调用/工具循环失败 card=%s: %s", state.card_id, exc)
    if state.rounds > 0:
        try:
            from ..storage import data_snapshot
            snap = data_snapshot.snapshot_sync(
                reason=f"checkpoint:{type(exc).__name__}", work_id=state.work_id)
            logger.info("[flow] checkpoint 落位 type=%s work=%s batch=%s",
                        type(exc).__name__, state.work_id,
                        (snap or {}).get("dir", "") if isinstance(snap, dict)
                        else str(snap)[:80])
        except Exception as snap_exc:  # noqa: BLE001 checkpoint 失败不阻断
            logger.warning("[flow] checkpoint 落位失败（不阻断）: %s", snap_exc)
    event_stream.emit(state.work_id, state.card_id, "error",
                      {"error": state.error[:200], "rounds": state.rounds})

# ============================================================
# [迭代四 K3] 卡间来信调起（卡间册 §4.4.2/§4.4.3）。
# ============================================================
# 激励文（卡间册 §4.4.4 沿革）：只走当轮 user_prompt，绝不落收件方档案。
# [2026-10-11 11:44] 动态构造：带发件卡名称＋类型中文名（注册表现读、
# 零类型分支、新增类型不改代码）；解析/读卡失败回落本默认文、不阻断调起。
#
# [卡片底座第一步 2026-10-14] 消息**不再写进收件方对话块**（改落待处理收件箱），
# 故正文不再能靠「上文以 card: 开头的条目」被模型看到——**正文必须随激励文进
# 模型视野**（V4：模型在上下文明确看到待处理消息）。激励文＝底座提供的
# 「待处理消息」这个可组合素材（其最终形态待裁，见任务书 §七.3，本处只保证
# 素材送达）。本常量＝构造失败时的兜底前缀（仍会拼上消息正文）。
_CARD_MSG_PROMPT = (
    "上一轮之后，你收到了来自其他卡的消息。"
    "请阅读这些消息并处理。"
)


def _strip_msg_header(content: str) -> str:
    """剥掉卡间消息正文首行的结构化头行（机械位，不是内容）。"""
    return card_msg.strip_header(str(content or ""))


def _fallback_card_msg_prompt(pending: list[dict[str, Any]]) -> str:
    """兜底激励文：默认前缀 ＋ 各待处理消息正文（正文必达——V4）。"""
    bodies = [_strip_msg_header(it.get("content", "")) for it in pending]
    return _CARD_MSG_PROMPT + "\n消息内容：\n" + "\n".join(b for b in bodies if b)


async def _build_card_msg_prompt(work_id: str,
                                 pending: list[dict[str, Any]]) -> str:
    """按待处理消息构造激励文（带发件卡名＋类型中文名＋应对说明＋**消息正文**）。

    类型名/应对说明从注册表现读（display_of/entry_of，零类型分支、新增类型不改
    代码）。发件卡名读不到时回落卡号本身（不阻断）。整段任一处失败由调用方
    回落 ``_fallback_card_msg_prompt``（正文仍达）。
    """
    lines: list[str] = []
    for it in pending:
        raw = str(it.get("content") or "")
        hdr = card_msg.parse_header(raw) or {}
        from_card = str(it.get("from_card") or hdr.get("from") or "")
        mt = str(it.get("type") or hdr.get("type") or "")
        disp = card_msg.display_of(mt)
        note = str((card_msg.entry_of(mt) or {}).get("note") or "")
        body = _strip_msg_header(raw)
        card_meta = (await card_store.read_card(work_id, from_card)).get("meta") or {}
        title = str(card_meta.get("title") or from_card)
        lines.append(
            f"卡 {from_card}（《{title}》）给你发来一条消息（类型：{disp}）。{note}\n"
            f"消息内容：\n{body}"
        )
    return ("上一轮之后，你收到了来自其他卡的消息。\n"
            + "\n".join(lines)
            + "\n请阅读这些消息并处理。")


def _log_trigger_task_done(task: "asyncio.Task") -> None:
    """调起任务异常回调：任务异常不得静默吞（卡间册 §4.4.3 P5）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("[K3] 卡间调起任务异常 target=%s: %s",
                     getattr(task, "_card_msg_target", "?"), exc)


# 调起任务**强引用**（防 GC 中途回收）。
#
# 事件循环对 Task 只持**弱引用**：`asyncio.create_task(...)` 的返回值若无人
# 引用，协程在挂起期间可能被垃圾回收 ⇒ 该任务永不完成（B 的回合静默消失、
# 表现为间歇性「B 未被调起」）。实测：本机跑本文件 5 次出现 1 次该现象
# （2026-10-14 卡片底座第一步施工期间复现）。故把两个任务（B 的回合＋观察位）
# 钉在模块级集合里，完成即移除。
_TRIGGER_TASKS: set["asyncio.Task"] = set()


# [自动模式 2026-10-17 · 回灌 5700 G4] 未回应信的**重新调起**上限（防打转：
# 模型持续不答时不能无限烧）。这是**防环兜底**语义（同 auto_driver 的
# _CARD_TREE_CYCLE_GUARD 口径），**不是轮次/墙钟/token 闸**——红线 1 不受
# 影响：它只限「同一收件方的连续追问重调起」，与「模型能跑多少轮」无关。
_RESPAWN_MAX = 3


def _spawn_tracked(coro) -> "asyncio.Task":
    """create_task ＋ 强引用登记（完成即自动移除）。"""
    t = asyncio.create_task(coro)
    _TRIGGER_TASKS.add(t)
    t.add_done_callback(_TRIGGER_TASKS.discard)
    return t


# ---------------------------------------------------------------------------
# [迭代五 S4] 流程自动回发（E5/E6/E7，本迭代唯一改运行行为件）。
# B 的卡间调起回合**成功结束**后（判据＝_run_message 返回 dict，即 finish
# 响应体；routes 成功路径返 dict、错误路径返带 status_code 的响应对象——
# 「非 409」不是成功判据），流程代码按注册表自动回发应答——模型全程不碰
# 发信工具（2026-10-11 12:34 基础设施裁定：模型只产内容，应答发送由流程
# 调薄壳接口 send_card_message 完成）。
# 被停（human_stop/policy_stop）/出错（error）/被顶（竞态）回合不回发；
# 回合期间新到的信不在调起前快照内，不回发、留给下个收口（防答非所问的
# 自动转发）。
# ---------------------------------------------------------------------------
async def _auto_reply_pending_letters(work_id: str, card_id: str,
                                      pending: list[dict[str, Any]]) -> None:
    """按注册表把 B 的最终回答回发给本轮**待处理收件箱快照**内的待回应信。

    取块（步骤③）：读档案取**最后一块**，双条件（actor==assistant 且
    end==model_done——END_VALUES 枚举）——防 finish 落块后本函数醒来前
    新块插入顶掉最后一块的毫秒竞态；**绝不遍历找更早的 model_done 块**
    （否则人停回合命中上一轮旧块、转发旧话）。
    [卡片底座第一步 2026-10-14] **来信数据源改**：不再从对话块扫 card: 块，
    改用调起时取的**待处理收件箱记录快照**（``pending``）。逐条判
    reply_expected_of(type)——不是只按最后一条 pending 判（否则尾批
    [ask, notify] 让 ask 静默死、撞「不许沉默」）。
    回发（步骤⑤）：reply_type 空串/正文空 → 打点不发、不猜类型；经薄壳
    send_card_message 落信（六道门槛一条不减）；全程不抛出。
    """
    try:
        blocks = await session_facade.read(work_id, card_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("[S4] 回发读档失败 wid=%s card=%s: %s", work_id, card_id, exc)
        return
    if not blocks:
        session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                            {"reason": "block_overtaken"})
        return
    last = blocks[-1]
    if str(last.get("actor") or "") != "assistant":
        # 最后一块非 assistant（竞态被顶掉）——本回合内容不被自动转发
        session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                            {"reason": "block_overtaken"})
        return
    if str(last.get("end") or "") != "model_done":
        # human_stop/policy_stop/error ＝被停/出错回合，内容不被自动转发
        session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                            {"reason": "end_not_model_done"})
        return
    content = str(last.get("content") or "")
    for it in pending:
        raw = str(it.get("content") or "")
        hdr = card_msg.parse_header(raw) or {}
        msg_id = str(it.get("msg_id") or hdr.get("id") or "")
        if not msg_id:
            continue
        mt = str(it.get("type") or hdr.get("type") or "")
        from_card = str(it.get("from_card") or hdr.get("from") or "")
        if not card_msg.reply_expected_of(mt):
            continue  # notify（收下即可）/answer（无需再答）——不回
        # [自动模式 2026-10-17 · 回灌 5700 G3（2089947）· BUG-AM-002] 模型本轮
        # 已**自己**答过这封信 ⇒ 流程不再重复自动转发。背景：2026-10-11 裁定下
        # 模型不能发信，S4 自动回发是唯一出口，不存在重复；G2 把发信交回 auto 卡
        # （自主通信）后，两条通道会各发一条 ⇒ 同一问收到两答。
        # 判据＝收件箱里已存在「from_card＝本卡 且 type＝msg_answer 且
        # reply_to＝该信」的行（inbox 不删行，天然可查）。**只减不加**：模型
        # 没自己答 ⇒ 行为逐字不变（G3 前的通道原样走）。
        try:
            _own_rows = await card_db.list_inbox(work_id)
        except Exception as exc:  # noqa: BLE001 查不到⇒按原行为回发（不因判据失败而静默）
            logger.warning("[S4] 查自发应答失败（按原行为回发）wid=%s card=%s: %s",
                           work_id, card_id, exc)
            _own_rows = []
        if any(str(x.get("from_card") or "") == card_id
               and str(x.get("type") or "") == "msg_answer"
               and str(x.get("reply_to") or "") == msg_id
               for x in _own_rows or []):
            session_ops.ops_log("card_msg_answer_skipped", work_id, card_id,
                                {"reason": "model_already_answered", "msg_id": msg_id})
            continue
        reply_type = card_msg.reply_type_of(mt)
        if not reply_type:
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "reply_type_missing", "msg_id": msg_id})
            continue
        if not content.strip():
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "no_content", "msg_id": msg_id})
            continue
        try:
            result = await card_tools.send_card_message(
                work_id, card_id, target_id=from_card,
                msg_type=reply_type, content=content, reply_to=msg_id)
        except Exception as exc:  # noqa: BLE001 薄壳异常按 send_failed 处置
            logger.error("[S4] 回发异常 wid=%s card=%s msg=%s: %s",
                         work_id, card_id, msg_id, exc)
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "send_failed", "msg_id": msg_id,
                                 "error": str(exc)[:120]})
            continue
        if isinstance(result, dict) and result.get("error"):
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "send_failed", "msg_id": msg_id,
                                 "error": str(result.get("error"))[:120]})
            continue
        session_ops.ops_log("card_msg_answer_sent", work_id, card_id,
                            {"msg_id": str((result or {}).get("msg_id", "")),
                             "target": from_card, "reply_to": msg_id})


#: [排队保证 2026-10-22 · D24] 每卡自续轮的次数上限，防「消息永远处理不完」打转烧额度。
#: 水位机制保证正常情况下每次续轮都会推进水位，这个上限只兜异常（回合连续失败）。
_SELF_REQUEUE_MAX = 5
_SELF_REQUEUE: dict[tuple[str, str], int] = {}


async def deliver_now(work_id: str, card_id: str) -> bool:
    """**立即**把该卡未处理的消息投递成一轮。返回是否真的起了新回合。

    [发消息即唤醒 2026-10-22 · D20/D24/D25] 这是「通信」与「投递」的分界线：
      · 无未处理消息 ⇒ 什么都不做，返回 False
      · 对方**正在跑** ⇒ 不打断、不起新回合，返回 False
        （它本轮收口时会调 `schedule_card_message_triggers`，那里有自查续轮兜底）
      · 对方**空闲** ⇒ 立刻起一轮，返回 True

    为什么对方在跑时不自续轮：收回合锁会 409，硬起只会消耗一次尝试。
    交给它自己的收口钩子是更可靠的路径——那条路不依赖「谁还记得这封信」。
    """
    # [测试环境开关 2026-10-22] 既有测试大量是同步时序写的，自动投递会在发信
    # 瞬间起异步回合，与它们冲突并放大 Windows 会话文件锁抖动。测试里模型是 fake，
    # 立刻投递没有真实语义价值 ⇒ 仅测试环境关闭，真实链路不变。
    # 开关由 tests/conftest.py 设置，显式且只影响这一处。
    if os.environ.get("ECO_NO_AUTO_DELIVER") == "1":
        return False
    try:
        pending = await card_db.list_pending(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 读不到就当没有（信已落库，收口会兜）
        logger.warning("[通信] 读未处理消息失败 card=%s: %s", card_id, exc)
        return False
    if not pending:
        return False
    try:
        if session_facade.turn_holder(work_id, card_id):
            return False
    except Exception:  # noqa: BLE001
        pass
    from . import routes as _routes
    try:
        prompt = await _build_card_msg_prompt(work_id, pending)
    except Exception as exc:  # noqa: BLE001 组装失败退到兜底，不让通信断
        logger.warning("[通信] 组装消息提示词失败，用兜底 card=%s: %s", card_id, exc)
        prompt = _fallback_card_msg_prompt(pending)
    try:
        task = _spawn_tracked(
            _routes._run_message(work_id, card_id, prompt, None, append_user=False))
        task._card_msg_target = card_id  # type: ignore[attr-defined]
        task.add_done_callback(_log_trigger_task_done)
        session_ops.ops_log("card_msg_deliver_now", work_id, card_id,
                            {"n": len(pending)})
        return True
    except Exception as exc:  # noqa: BLE001 起不来不算通信失败（信还在）
        logger.error("[通信] 立即投递失败 card=%s: %s", card_id, exc)
        return False


async def schedule_card_message_triggers(state: RunState) -> None:
    """A 的回合收口时对本回合记下的全部 pending target 异步起 B 的回合。

    - 遍历 `card_msg.TRIGGERS`（K2 落信时记下的调起意图），读完即清（不累积）；
    - [卡片底座第一步 2026-10-14] **待处理判定改读收件箱**：`card_db.list_pending`
      （显式队列状态），不再用 `card_msg.find_pending` 从对话块顺序推导（脆弱约定）；
    - 有 pending → `asyncio.create_task` 起收件方回合（激励文 append_user=False），
      **非阻塞**——不 await 任务完成，异常经 done_callback 记日志；
    - 快速通道 vs 持久保证两层分工（写死）：create_task 可能因进程退出而丢；
      **收件箱里的信是持久保证**——丢任务不丢信，下次任一回合收口时重新判 pending
      补齐（本函数被每个回合收口调用，天然承担补齐）。
    - B 忙（turn_try_acquire 409 不排队）→ ops `card_msg_pending` 留痕，信不丢。
    """
    if not card_msg.TRIGGERS:
        return
    triggers = dict(card_msg.TRIGGERS)
    card_msg.TRIGGERS.clear()
    # 延迟 import：routes 在模块加载期 import 本模块，运行时反向引用是既有
    # 分层下的标准解法（_run_message 只在任务执行期才被解引用）。
    from . import routes as _routes

    for (work_id, target_id), msg_ids in triggers.items():
        # [编程卡 2026-10-15 · §六块3 静默判定②] 完工编程卡不被 K3 调起：
        # 只读 card_type/state 两字段的机械判定（红线 2），continue 且**不打
        # ops**；信留在收件箱 pending 作验收留档（下次收口重新判，仍跳过）。
        try:
            _tmeta = card_store._meta_of(work_id, target_id) or {}
        except Exception:  # noqa: BLE001 读不出 meta ⇒ 不在此处置，走既有链
            _tmeta = {}
        try:
            pending = await card_db.list_pending(work_id, target_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("[K3] 读收件箱失败 wid=%s target=%s: %s",
                         work_id, target_id, exc)
            continue
        if not pending:
            # 信已被先前某次收口调起并处理完（收件箱里已无 pending）——不再重复调起。
            continue
        # [编程卡 2026-10-15 · §六块3 静默判定②] 完工编程卡不被 K3 调起。
        # [coder 卡可继续派活 2026-10-21 · 用户令] 但**派活**要放行：主卡/人发
        # msg_ask 给一张完工 coder，就是「这条线还有后续」——先复位（state=active
        # ＋目标重开）再正常调起，而不是另建一张卡。纯回执类（coder_done 等）
        # 仍然跳过：否则完工回执会把它自己再叫起来一遍。
        if str(_tmeta.get("card_type") or "") == "coder" \
                and str(_tmeta.get("state") or "") == "completed":
            _is_dispatch = any(
                str((m or {}).get("type") or "") == "msg_ask" for m in pending)
            if not _is_dispatch:
                continue
            try:
                await _routes._revive_coder_card(work_id, target_id, "")
            except Exception as exc:  # noqa: BLE001 复位失败就不调起，信留 pending
                logger.error("[K3] 完工编程卡复位失败 target=%s: %s", target_id, exc)
                continue
        session_ops.ops_log("card_msg_deliver", work_id, target_id,
                            {"msg_id": str(pending[-1].get("msg_id") or ""),
                             "msg_ids": list(msg_ids)})
        # 激励文＝底座提供的「待处理消息」素材（带发件卡名＋类型中文名＋应对
        # 说明＋**消息正文**）。构造失败回落默认文、不阻断调起。
        try:
            prompt = await _build_card_msg_prompt(work_id, pending)
        except Exception as exc:  # noqa: BLE001 激励文失败不阻断调起
            logger.warning("[K3] 构造卡间激励文失败、回落默认文: %s", exc)
            prompt = _fallback_card_msg_prompt(pending)
        task = _spawn_tracked(
            _routes._run_message(work_id, target_id, prompt,
                                 None, append_user=False))
        task._card_msg_target = target_id  # type: ignore[attr-defined]
        task.add_done_callback(_log_trigger_task_done)

        # 忙时观察位：create_task 已起，若收件方在途，_run_message 内部
        # turn_try_acquire 失败返回 409 —— 由包裹协程转译为 ops card_msg_pending。
        # [迭代五 S4·步骤②] 触发判据：成功回合的返回值是 dict（finish 响应体，
        # 无 status_code 属性）→ 置 consumed ＋ 执行回发；具 status_code 的响应
        # 对象：409 保持既有打点；502/404/423 → 不回发＋failed 留痕；其余形态
        # 打日志不回发。「非 409」不是成功判据。
        async def _watch_pending(t: "asyncio.Task", _wid: str = work_id,
                                 _tid: str = target_id,
                                 _pending: list = list(pending)) -> None:
            try:
                resp = await t
            except Exception as exc:  # noqa: BLE001 已由 done_callback 记日志
                logger.error("[K3] 调起任务异常（watch）target=%s: %s", _tid, exc)
                return
            if isinstance(resp, dict):
                # [卡片底座第一步 2026-10-14] 回合收口＝本轮消息已处理：把快照内
                # 的 msg_id 移出待处理（status→consumed，**不删行**——留痕可回溯）。
                try:
                    for _it in _pending:
                        _mid = str(_it.get("msg_id") or "")
                        if _mid:
                            await card_db.mark_consumed(_wid, _mid)
                except Exception as exc:  # noqa: BLE001 状态置位失败不阻断回发
                    logger.error("[K3] 收件箱置 consumed 失败 target=%s: %s",
                                 _tid, exc)
                try:
                    await _auto_reply_pending_letters(_wid, _tid, _pending)
                except Exception as exc:  # noqa: BLE001 回发通道绝不抛出
                    logger.error("[S4] 自动回发通道异常 target=%s: %s", _tid, exc)
                # [自动模式 2026-10-17 · 回灌 5700 G4 · 61b83fb 修法②]
                # **收口闸（兜底层）**：回合已收口但收件箱**仍**有 reply_expected
                # 的待处理信（＝模型这轮没答；update_goal 的 complete 闸只拦
                # auto 卡，非 auto 卡没有 update_goal 可拦——这里是两类卡共用
                # 的兜底）⇒ 重新记调起意图（TRIGGERS），下一次任意回合收口即被
                # K3 补齐调起。依据：TRIGGERS 是请求级收集、schedule_card_message
                # _triggers 在每次回合收口都被调（routes._run_message 尾）⇒ 写入
                # 必然被消费；信在收件箱 pending 是持久保证，create_task 丢失也
                # 不丢信。防打转：仅对连续追问限次（_RESPAWN_MAX），超限转 ops
                # 留痕不再自动调起（交人工/发问方 pause 处置）——**限次是防环
                # 兜底，不是轮次/墙钟/token 闸**（红线 1 不受影响）。
                try:
                    still = await card_db.list_pending(_wid, _tid)
                except Exception as exc:  # noqa: BLE001
                    logger.error("[K3] 收口复查待处理失败 target=%s: %s", _tid, exc)
                    still = []
                _still_needs = [it for it in (still or [])
                                if card_msg.reply_expected_of(str((it or {}).get("type") or ""))]
                if _still_needs:
                    _n_before = len([m for k, ms in card_msg.TRIGGERS.items()
                                     if k[0] == _wid and k[1] == _tid for m in ms])
                    if _n_before >= _RESPAWN_MAX:
                        session_ops.ops_log("card_msg_unanswered_giveup", _wid, _tid,
                                            {"pending": [str((it or {}).get("msg_id") or "")
                                                         for it in _still_needs]})
                        logger.warning("[K3] 未回应信连续重调超限，转留痕 target=%s n=%d",
                                       _tid, _n_before)
                    else:
                        card_msg.TRIGGERS.setdefault((_wid, _tid), []).extend(
                            str((it or {}).get("msg_id") or "") for it in _still_needs)
                        session_ops.ops_log("card_msg_respawn", _wid, _tid,
                                            {"msg_ids": [str((it or {}).get("msg_id") or "")
                                                         for it in _still_needs],
                                             "attempt": _n_before + 1})
                        logger.info("[K3] 回合结束仍有待回应信，重新调起 target=%s n=%d",
                                    _tid, _n_before + 1)
                return
            status = getattr(resp, "status_code", None)
            if status == 409:
                session_ops.ops_log("card_msg_pending", _wid, _tid,
                                    {"reason": "receiver_busy_409"})
            elif status in (502, 404, 423):
                session_ops.ops_log("card_msg_answer_failed", _wid, _tid,
                                    {"reason": "non_success_status"})
            else:
                logger.warning("[K3] 调起回合返回形态未知 target=%s: %r", _tid, resp)

        _spawn_tracked(_watch_pending(task, work_id, target_id))

    # ============================================================
    # [排队保证 2026-10-22 · D24] 本轮收口时**查自己**还有没有未处理消息。
    #
    # 为什么必须有这一段：用户裁定「子卡→父卡是排队到轮末，但一定不是投递」。
    # 旧实现只有 TRIGGERS（记一笔等别人消费），若收口那一刻没有别的卡在跑，
    # 消息就永远躺着——那是投递。这一段让**本卡自己的收口**成为保证：
    # 只要有未处理消息，就立刻再起一轮，不依赖任何外部条件。
    #
    # 水位机制保证正常情况会收敛（每轮成功都会推进水位）；_SELF_REQUEUE_MAX
    # 只兜异常（回合连续失败导致水位不动），到顶后如实记 ops 并停止。
    # ============================================================
    try:
        _own = await card_db.list_pending(state.work_id, state.card_id)
    except Exception as exc:  # noqa: BLE001 自查失败不应影响本回合收口
        logger.warning("[排队] 自查未处理消息失败 card=%s: %s", state.card_id, exc)
        _own = []
    if _own:
        _key = (state.work_id, state.card_id)
        _n = _SELF_REQUEUE.get(_key, 0)
        if _n >= _SELF_REQUEUE_MAX:
            session_ops.ops_log("card_msg_self_requeue_giveup", state.work_id,
                                state.card_id,
                                {"n": _n, "left": [str((it or {}).get("msg_id") or "")
                                                   for it in _own]})
            logger.warning("[排队] 自续轮达上限，停止 card=%s n=%d 剩 %d",
                           state.card_id, _n, len(_own))
        else:
            _SELF_REQUEUE[_key] = _n + 1
            try:
                _prompt = await _build_card_msg_prompt(state.work_id, _own)
            except Exception:  # noqa: BLE001
                _prompt = _fallback_card_msg_prompt(_own)
            try:
                from . import routes as _routes
                _t2 = _spawn_tracked(_routes._run_message(
                    state.work_id, state.card_id, _prompt, None, append_user=False))
                _t2._card_msg_target = state.card_id  # type: ignore[attr-defined]
                _t2.add_done_callback(_log_trigger_task_done)
                session_ops.ops_log("card_msg_self_requeue", state.work_id,
                                    state.card_id,
                                    {"n": _n + 1, "msgs": len(_own)})
            except Exception as exc:  # noqa: BLE001
                logger.error("[排队] 自查续轮起不来 card=%s: %s", state.card_id, exc)
    else:
        _SELF_REQUEUE.pop((state.work_id, state.card_id), None)
