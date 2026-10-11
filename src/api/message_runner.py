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
2. 三席触发条件：未命中确定性动作直答（`auto_direct` 为假）且本轮无
   tool_calls 且 `seats_done` 假；进场恰一次（`seats_done` 一次性闸）。
3. 拼接顺序：T14「分析在前、回执在后、`\n\n` 分隔」，落位在日志写之后、
   parse_proposals 之前。
4. 落盘时机：用户条在装配前；assistant 条在全部交付段之后；
   prompt 日志无论成败都写。
5. SSE 事件序：`run_start → context → round_start →
   (tool_call → tool_result/card_op)* → seats_enter → seat_* → deliver →
   proposal → done`，逐字见 `tests/_golden/s*_sse.log`。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..storage import card_store, memory_store, session_facade, session_ops, session_store, work_store
from ..thinkchain import board as thinkchain_board
from ..thinkchain import triage as thinkchain_triage
from ..thinkchain.model_pick import pick_model as _pick_model  # noqa: F401（既有路由位，五期禁直接调）
from ..thinkchain import model_pick
from ..toolcard import driver as toolcard_driver
from . import (
    card_msg,
    card_proposal,
    card_tools,
    control,
    context_assembler,
    context_regions,
    event_stream,
    model_service,
    progress,
    prompt_log,
    prompts,
    simple_service,
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
    # [T14-1-1] 带工具轮的分析正文采集（只读）：落盘型工作流的分析写在带工具轮
    # 的 assistant_content 里，末轮判定只取纯对话轮正文——分析结构性上不了屏。
    # 采集后与末轮回执拼接交付（分析在前、回执在后），不取代回执。
    analysis_parts: list[str] = field(default_factory=list)
    model_used: Optional[str] = None
    error: Optional[str] = None
    # [phase9-E] 观点元信息（含内部text）
    viewpoints_meta: list[dict[str, Any]] = field(default_factory=list)
    # [E-探针制] 三席一次进场闸（口径①：只进场一次；统合官若续查证不重问三席）
    seats_done: bool = False
    # [2026-10-11 两套机制拆除] 机械预检命中标记：确定性动作（删卡/建卡/改卡/
    # 查询）直答、跳三席与统合官（A97 用户裁定）——原快车道 mode 字段退役。
    auto_direct: bool = False
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

# d. 上下文组装（T11 分区装配线）：0 使用说明（BASE_PROMPT 指针）＋100 卡片清单＋150 当前卡。
# 装配只按 order 收区拼接、零内容判断（总表 #2/#3）；vp_system_prompt＝100＋150（职责分离裁定）。
# 无目标锚/血统链/管辖域/契约/charter/空位提示注入（全量审计定案：结构即引导）。
# [T12-SP-05] 凝练态（水位 mark＋entries）routes 层一次读取、结果传 200/300 区——
# context_regions 保持纯函数不做 IO（L17 硬约束）；开关关（RD-08 缺省）＝None＝逐字旧路。
# 300 区收窄为未凝段的切片属三期路线裁定后补（L387 本批不动，mark 读取基础件先落）。
# [信息流 Task3] 装配段埋点：context 秒现（V1）
def build_context(state: RunState) -> None:
    """装配 system_prompt / vp_system_prompt / 工具列 / 轮槽。"""
    # [T12-SP-05] 凝练态读取
    state.condense_state = (
        memory_store.load_memory(state.work_id, state.card_id)
        if memory_store.condense_enabled() else None
    )
    state.ctx_regions = context_assembler.build_context_regions(
        state.work_id, state.card_id, memory_state=state.condense_state,
    )
    state.neutral_context = context_regions.assemble_regions(
        [r["text"] for r in state.ctx_regions]
    )
    state.system_prompt = prompts.build_system_prompt(state.neutral_context)
    state.vp_system_prompt = prompts.build_viewpoint_system_prompt(state.neutral_context)
    event_stream.emit(state.work_id, state.card_id, "context", {
        "regions": [str(r.get("region", "")) for r in state.ctx_regions],
        "bytes": len(state.neutral_context.encode("utf-8")),
    })

    # [T15-6] 300 区收窄为未凝段（路线甲，CONDENSE_ROUTE_RULINGS 2026-10-05 闸开）：
    # 开关开→按水位 mark 切片（condense_state 上方一次读取——T12 SP-05 同一读取传
    # 200/300 区），mark 前已凝段由 200 区记忆代表（原文全量在会话文件，一条不删）；
    # 开关关（缺省）＝condense_state None→全量投影逐字旧路（V1/V5）。mark 越界防御
    # 回退全量（与会话追加式存储的半开区间口径一致）。
    if state.condense_state is not None:
        _condense_mark = int(state.condense_state.get("mark", 0) or 0)
        _hist = state.prior[_condense_mark:] \
            if 0 <= _condense_mark <= len(state.prior) else state.prior
    else:
        _hist = state.prior
    state.prior_for_gateway = [
        {"role": m["role"], "content": m["content"]} for m in _hist
        # [迭代二 S4·呈报项] 环节留痕块（behavior 非空：triage/plan/step/final/
        # revision 等，本迭代经 facade.append 落档的 actor=tool 形态）**不进
        # 对话投影**——①落盘 role 恒由 actor 映射（session_facade.py:511），
        # actor="tool" → role="tool" 的孤立工具消息进 OpenAI 兼容端点会被拒
        # （tool role 必须配 tool_call_id）；②环节块是留痕通道，模型上下文
        # 由装配区（100/150/165/200）承载，不靠对话回流。旧块无 behavior 键
        # → get 默认 "" → 零过滤，迭代一投影逐字不变（金样本对话安全）。
        if not m.get("behavior")
    ]
    # [T11-T6] 轮槽（请求局部对象）：包住同一条列表。折叠关时装配逐字同旧
    # （wire_messages 原样返回本列表）；首轮固化与抢救回注走 append_plain
    # （非轮、永不折叠——不折红线），assistant＋tool 结果走 begin_round/
    # append_tool_result（成轮，可折单元）。
    state.slot = tool_fold.ToolTurnSlot(state.prior_for_gateway)
    # [工具卡底座 T10 · EC-129] 传入卡上下文；[T4 权限收口] 同时传 cards。
    # 三类形参缺省时（无卡上下文）不过滤，既有直调行为逐字不变。
    state.tools = card_tools.build_tool_schemas(
        card_id=state.card_id,
        work_id=state.work_id,
        cards=card_tools._collect_card_meta(state.work_id),
    )
    # history_len 此刻一次算定（此后本列表会被轮槽就地 append 增长）
    state.history_len = len(state.prior_for_gateway)


# ============================================================
# 段③：工具循环 —— 单步推进
# ============================================================

def _pick_step_model(state: RunState) -> str:
    """[迭代三 S5] 当前步类型→模型席位路由。

    有活板 → snapshot_sync 读当前步 type 查表（存量空串/越界 → default）；
    无活板（直答）→ default。查得席位不在 models.yaml 的 models 键集 →
    显式报错（simple_service.py:183-185 先例形态，错误信息含席位名）——
    消费侧热读席位清单可行（gw.config 内存常驻），无需启动校验降级。
    每圈实取席位打日志（S5 验收「日志可查每步实取席位」）。
    """
    snap = thinkchain_board.snapshot_sync(state.work_id, state.card_id)
    role = "default"
    if snap is not None:
        steps = snap.get("steps") or []
        cur = snap.get("current", 0)
        if 0 <= cur < len(steps):
            role = steps[cur].get("type") or "default"
    seat = _pick_model(role)
    models_cfg = model_service.get_gateway().config.get("models") or {}
    if seat not in models_cfg:
        raise RuntimeError(
            f"models.yaml 无此模型条目: {seat}（model_pick 类型 {role!r} 配置指向了不存在的席位）")
    logger.info("[迭代三 S5] 类型路由 role=%s -> seat=%s work=%s card=%s",
                role, seat, state.work_id, state.card_id)
    return seat


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

    # [迭代二 S4] 自判分流（方案 §4.1 施工位）——step 入口判 rounds==0＝
    # 「step 循环外、首轮 step 之前」语义的等价落点（每回合恰一次；routes
    # 零动刀）。板在内存＝已拆解过→triage_if_needed 内部跳过（零模型调用）；
    # direct→工具面只留逃生口 escalate_to_plan（物理可调，同 :507 收窄精神）；
    # plan→保持 build_context 装的全量工具面。
    if state.rounds == 0:
        _tri = await thinkchain_triage.triage_if_needed(
            state.work_id, state.card_id, state.content)
        if _tri["verdict"] == "direct":
            if _tri.get("auto"):
                # [2026-10-11 两套机制拆除] 机械预检命中（确定性动作：删卡/建卡/
                # 改卡/查询）——A97 用户裁定保留：带全量工具直答、跳三席与统合官。
                state.auto_direct = True
            else:
                state.tools = thinkchain_triage.direct_tool_schemas()
        else:
            # [新流程接管 2026-10-11] plan 分支：判定+引导注入模型视野
            # （slot 非轮消息、永不折叠、首轮即进 prior_for_gateway），
            # 并 emit triage_verdict 供前端显示——判 plan 不再只是死信。
            _tri_note = (
                "系统判定：本消息需要拆解（理由：" +
                str(_tri.get("reason") or "—") +
                "）。请使用 create_plan 工具建立步骤计划并逐步执行；"
                "干完每步立刻调 step_complete，全部完成后系统会自动收口。"
            )
            state.slot.append_plain({
                "role": "assistant", "content": _tri_note,
            })
        # [2026-10-11 两套机制拆除] triage_verdict 统一事件：机械预检/模型判定
        # 都发一行（带 rule 区分），前端只渲染这一行判断——「快车道/全链」退役。
        event_stream.emit(state.work_id, state.card_id, "triage_verdict",
                          {"verdict": _tri["verdict"],
                           "reason": str(_tri.get("reason") or ""),
                           "rule": _tri.get("rule") or ""})

    state.rounds += 1
    if state.rounds == 1:
        # [E-探针制 2026-10-07] 首轮＝纯主模型带工具查证（原 call_simple
        # 观点前置编排退役；三席改在工具循环收尾处进场）。
        progress.set_stage(state.work_id, state.card_id, "started", "主模型查证中")
        event_stream.emit(state.work_id, state.card_id, "round_start",
                          {"round": state.rounds, "phase": "main"})
        state.model_calls += 1
        # [迭代三 S5] 首轮接线：按当前步 type 查表取席位（无活板回落 default）
        result, state.model_used = await model_service.call_main(
            state.system_prompt, state.user_prompt, state.prior_for_gateway,
            tools=state.tools,
            model_name=_pick_step_model(state),
        )
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
        result, state.model_used = await model_service.call_main(
            state.system_prompt, state.user_prompt, wire_messages, tools=state.tools,
            model_name=_pick_step_model(state),
        )
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

    # [迭代二 S4] 催化挂点一（主循环路径）：本卡有活板且本步未推进、本轮
    # **空手**（两条件缺一不可——判据＝tool_calls 事实，防嘴上伪造；带工具
    # 轮照常执行）→ 催促交作业续跳，不设上限。两道天然刹车：每圈入口
    # check_control（人停即刹）＋用户新消息＝新回合。空手正文不入槽（同
    # :316 抢救先例——催促文自带本步要求原文，信息自足）。
    if not tool_calls and thinkchain_board.has_live_board(state.work_id, state.card_id):
        _step_prompt = await thinkchain_board.current_step_prompt(
            state.work_id, state.card_id)
        if _step_prompt is not None:
            state.user_prompt = (
                f"{_step_prompt}\n（本步要求如上。干完立刻调 step_complete；"
                "未调之前这一步不算完成。）"
            )
            return StepOutcome("continue", "await_step_tool")

    if not tool_calls:
        state.final_reply = assistant_content
        if text_rescue_note:
            # 反馈兜底：疑似调用未被识别这个事实必须回到模型上下文，
            # 否则模型永远在等一个不会来的结果（等待黑洞）。
            # [T11] 经轮槽 append_plain 入槽（非轮消息，永不折叠）
            state.slot.append_plain({
                "role": "assistant", "content": state.final_reply,
            })
            state.user_prompt = text_rescue_note
            return StepOutcome("continue", "text_rescue")
        # [2026-10-11 两套机制拆除] 确定性动作直答（A97）：机械预检命中、
        # 带全量工具直答、跳三席与统合官，本轮即交付。
        if state.auto_direct:
            return StepOutcome("deliver", "auto_direct")
        # [E-探针制 2026-10-07] 三席后置进场（口径①：工具查证收尾后、交付前，
        # 全程恰一次）。
        if state.seats_done:
            # [E] 三席已进场过（统合官续查证后的收尾轮）：本轮即交付回执
            return StepOutcome("deliver", "seats_done")
        return StepOutcome("seats", "seats_enter")

    # —— 有工具调用：本轮执行工具，续跳 ——
    # [phase8.2] 第一次进入工具轮时，把用户原文固化进 prior，
    # 否则后续跳的 user_prompt 被替换后原文会从对话中消失
    # [T11] 经轮槽 append_plain 入槽（首轮用户原文固化＝不折红线之一）
    if state.rounds == 1:
        state.slot.append_plain({"role": "user", "content": state.content})

    # [T-0010 §2] run 级 tool_call_id：pair_id 前 8 位；无 pair_id 退 work_id 前 4 位。
    # 与「轮次＋序号」合成 ⇒ 同一份 messages 内全局唯一（B-07 配对协议），
    # 且 assistant 侧与 tool 侧取同一个值，配对不变量不动。
    _run_tag = (state.pair_id[:8] or state.work_id[:4])
    # 把 assistant 消息（含 tool_calls）+ tool 结果追加进 prior
    # [T11-T6] assistant 经 begin_round 入槽（成轮，可折单元）；
    # 消息构造与既有逐字一致（折叠关时槽即本列表，零变化）
    state.slot.begin_round({
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
    })
    # [T14-1-2] 只读采集：带工具轮的 assistant_content（含 L459 抢救处理后
    # 的正文）同步留一份进交付采集列表。不改 begin_round 入参、不额外入槽
    # （T11 轮槽消息条数与配对不变量不破坏，RD-09）；不限长不折叠不截断。
    if assistant_content:
        state.analysis_parts.append(assistant_content)

    # 收集卡片 meta 用于权限判定
    cards_meta = card_tools._collect_card_meta(state.work_id)

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
        event_stream.emit(state.work_id, state.card_id, "tool_call",
                          {"tool": name, "target": target_id})
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
        # [新流程接管 2026-10-11] 板子事件 emit（对话流可见拆解上板/步进/收口）：
        # create_plan 成功→plan_created；step_complete 成功→step_advanced
        # （all_done=True 时前端渲染收口行）。
        if ok and name == "create_plan":
            event_stream.emit(state.work_id, state.card_id, "plan_created", {
                "name": _data.get("plan_name") or "",
                "total": _data.get("total") or 0,
                "prompt": _data.get("prompt") or "",
            })
        elif ok and name == "step_complete":
            event_stream.emit(state.work_id, state.card_id, "step_advanced", {
                "advanced": bool(_data.get("advanced")),
                "all_done": bool(_data.get("all_steps_done")),
                "prompt": _data.get("prompt") or "",
            })
        # [迭代二 S4·呈报项] escalate 逃生口消费：任务书 S2 步骤 4「当轮
        # StepOutcome("deliver","escalate")＋档案补块＋下轮进拆解流——绝不
        # 静默换路」。工具返回 escalate 标记 → 当轮以 deliver 收束（正文=
        # 模型本次发言，用户看得见换路动作）；「下轮进拆解流」＝新回合
        # triage 自然重判（板不在→发起自判），零额外状态。
        # S4 §三未列此接缝位，按可施工最小位置落（工具执行循环内），呈报。
        if name == "escalate_to_plan" and ok and (_data.get("escalate") is True):
            state.final_reply = assistant_content or ""
            state.user_prompt = "工具结果已返回。"
            return StepOutcome("deliver", "escalate")
        # [信息流 Task4] 并表（A101）：确定性动作直答的卡操作落 card_op；
        # 常规链保持 tool_result。全日志打点（A96）。
        if state.auto_direct and name in (
            "create_card", "edit_card", "delete_card", "read_card",
        ):
            event_stream.emit(state.work_id, state.card_id, "card_op", {
                "op": name, "target": target_id, "ok": ok,
                "result": _data if ok else card_tools.render_tool_result(tool_result)[:200],
            })
            logger.info("[flow] 卡操作落地 op=%s target=%s ok=%s card=%s",
                        name, target_id, ok, state.card_id)
        else:
            event_stream.emit(state.work_id, state.card_id, "tool_result",
                              {"tool": name, "target": target_id, "ok": ok})
        # [T11-T6] tool 结果经 append_tool_result 入槽（归入当前轮）
        # [T-0010 §1.6] 回灌文本走单一投影函数（不各自 json.dumps）——
        # 否则失败信封的 {"code","message"} 会被当数据喂进模型上下文
        state.slot.append_tool_result({
            "role": "tool",
            "tool_call_id": f"call_{_run_tag}_{state.rounds}_{i}",
            "content": card_tools.render_tool_result(tool_result),
        })
        # [E-探针制] 全文累积（口径④）：三席后置注入用，不折叠不截断
        # [T-0010 §1.6] 同一投影函数，两个调用点禁止各自拼装
        state.tool_full_texts.append(
            f"{name}(target={target_id}) → " + card_tools.render_tool_result(tool_result)
        )

    state.user_prompt = "工具结果已返回。"
    # [信息流 Task2/打点 A92/A96] 同请求续跳：占位符只进模型不入用户流
    logger.info("[flow] 断点续跳 resume_kind=同请求续跳 no_user_visible=True round=%s card=%s",
                state.rounds, state.card_id)
    verdict = check_control(state)   # 中止缝：出口挂点（[A8/R3] 控制三态）
    if verdict == control.STOP:
        state.final_reply = state.final_reply if state.final_reply is not None else ""
        return StepOutcome("deliver", "human_stop")
    if verdict == control.DEFER_HUMAN:
        return StepOutcome("deliver", "policy_stop")
    return StepOutcome("continue", "tools_executed")


# ============================================================
# 段③b：三席后置＋统合（E-探针制口径①）
# ============================================================

async def run_seats(state: RunState) -> StepOutcome:
    """三席进场 → 统合官统合 → 交付。

    [E-探针制] 进场时机＝工具查证收尾后、交付前，全程恰一次。
    上下文回流铁则：观点只进统合官 user_prompt，不入槽/不入 session；
    三席prior 取纯对话历史切片（不含本轮工具机制消息，且只读不回写）。
    """
    state.seats_done = True
    # [信息流 Task3] 三席进场埋点＋每席起止转发（on_seat 缺省 None 语义不变）
    event_stream.emit(state.work_id, state.card_id, "seats_enter",
                      {"tool_results": len(state.tool_full_texts)})
    seats_prior = state.prior_for_gateway[:state.history_len]
    ok_metas, state.viewpoints_meta = await simple_service.run_viewpoints(
        state.vp_system_prompt,
        simple_service.build_informed_prompt(state.content, state.tool_full_texts),
        seats_prior,
        on_progress=lambda s, d, _w=state.work_id, _c=state.card_id:
            progress.set_stage(_w, _c, s, d),
        on_seat=lambda kind, meta, _w=state.work_id, _c=state.card_id:
            event_stream.emit(_w, _c, kind, meta),
    )
    if not ok_metas:
        # [E-口径②] 全败留痕不阻断：主模型携工具结果独自交付（草稿即回执）
        if state.viewpoints_meta:
            logger.warning("[E] 三席观点全败（%d 席尝试），主模型独自交付 card=%s",
                           len(state.viewpoints_meta), state.card_id)
        return StepOutcome("deliver", "seats_all_failed")
    # 统合官最终统合交付（合同不动 RD-03：同 call_main、带 tools）
    progress.set_stage(state.work_id, state.card_id, "synthesize", "三席观点统合中")
    logger.info("[E] 三席后置进场 工具结果=%d条 成功席=%d card=%s",
                len(state.tool_full_texts), len(ok_metas), state.card_id)
    synth_input = simple_service.build_viewpoint_prompt(state.content, ok_metas)
    event_stream.emit(state.work_id, state.card_id, "round_start",
                      {"round": state.rounds, "phase": "synthesize"})
    state.model_calls += 1
    # [迭代五 S2·E10] 统合官席位可配：model_pick.resolve_model("synthesize")，
    # 不配回落 main（缺省行为逐字不变）。
    result, state.model_used = await model_service.call_main(
        state.system_prompt, synth_input, state.prior_for_gateway, tools=state.tools,
        model_name=model_pick.resolve_model("synthesize"),
    )
    tool_calls = result.get("tool_calls") or []
    assistant_content = result.get("content") or ""
    # [迭代二 S4] 催化挂点二（统合官收口路径）：同挂点一——只挂 :310 则
    # 统合官路径空手收口、催促漏网（任务书 §二锚表原文）。判据同＝空手
    # （tool_calls 事实）＋活板＋步未推进；空手正文不入槽。
    if not tool_calls and thinkchain_board.has_live_board(state.work_id, state.card_id):
        _step_prompt = await thinkchain_board.current_step_prompt(
            state.work_id, state.card_id)
        if _step_prompt is not None:
            state.user_prompt = (
                f"{_step_prompt}\n（本步要求如上。干完立刻调 step_complete；"
                "未调之前这一步不算完成。）"
            )
            return StepOutcome("continue", "await_step_tool")
    if not tool_calls:
        state.final_reply = assistant_content
        return StepOutcome("deliver", "synthesize")
    # 统合官仍需查证：清空草稿、借道工具执行段续跑（无闸 RD-01）；
    # seats_done 保证三席不再有第二次。末轮草稿由统合稿取代。
    state.final_reply = None
    return StepOutcome("continue", "synthesize_needs_tools")


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
        system_prompt=state.system_prompt,
        prior_messages=state.prior_for_gateway,
        user_prompt=state.content,
        model_used=state.model_used if state.error is None else None,
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
    # [T10-1-4] 回复条携带与用户条逐字相同的 pair_id。
    # [A6b/T024] 写切换：门面追加（V1 文件现场升格；end 显式传入——正常回合
    # 缺省 model_done，human_stop/policy_stop 由控制面消费段写定）。
    await session_facade.append(state.work_id, state.card_id, {
        "role": "assistant", "content": state.clean_text,
        "model": state.model_used, "pair_id": state.pair_id,
        "end": state.end_reason or "model_done",
    })
    logger.info("[T10] 回复落盘 pair_id=%s card=%s end=%s",
                state.pair_id, state.card_id, state.end_reason or "model_done")
    # [T12-SP-04] 回合末凝练触发（condense_fold）：assistant 落盘之后异步触发、
    # 不阻塞对话（异步是工程手段非闸，RD-04）。开关缺省关（RD-08）＝零动作；
    # 开时三期管线仍挂起待路线裁定，触发只量测＋留痕（memory_store.condense_turn_end）。
    memory_store.maybe_schedule_turn_end(state.work_id, state.card_id)
    progress.set_stage(state.work_id, state.card_id, "done", "完成")
    # [信息流 Task3/打点 A96] done 事件＋确定性动作直答调用数打点
    event_stream.emit(state.work_id, state.card_id, "done",
                      {"rounds": state.rounds, "model_calls": state.model_calls})
    if state.auto_direct:
        logger.info("[flow] 确定性动作直答 calls=%s rounds=%s card=%s",
                    state.model_calls, state.rounds, state.card_id)
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
_CARD_MSG_PROMPT = (
    "上一轮之后，你收到了来自其他卡的消息（见上文以 card: 开头的条目）。"
    "请阅读这些消息并处理。"
)


def _log_trigger_task_done(task: "asyncio.Task") -> None:
    """调起任务异常回调：任务异常不得静默吞（卡间册 §4.4.3 P5）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("[K3] 卡间调起任务异常 target=%s: %s",
                     getattr(task, "_card_msg_target", "?"), exc)


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
                                      snapshot_ids: frozenset) -> None:
    """按注册表把 B 的最终回答回发给快照内的待回应卡间信。

    取块（步骤③）：读档案取**最后一块**，双条件（actor==assistant 且
    end==model_done——END_VALUES 枚举）——防 finish 落块后本函数醒来前
    新块插入顶掉最后一块的毫秒竞态；**绝不遍历找更早的 model_done 块**
    （否则人停回合命中上一轮旧块、转发旧话）。
    尾批全回（步骤④）：取该 assistant 块之前的 card: 块（从尾向前扫到
    上一条 assistant 为止——human 隔断不停；与「全部 card: 块＋id∈快照
    过滤」行为等价：快照集恰为调起前尾批），逐块判 id∈快照 且
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
    letters: list[dict] = []
    for blk in reversed(blocks[:-1]):
        actor = str(blk.get("actor") or "")
        if actor == "assistant":
            break
        if actor.startswith("card:"):
            letters.append(blk)
    for blk in letters:
        hdr = card_msg.parse_header(str(blk.get("content") or ""))
        if not hdr or not hdr.get("id"):
            continue
        if hdr["id"] not in snapshot_ids:
            continue  # 回合期间新到信——不回发、留给下个收口
        if not card_msg.reply_expected_of(hdr.get("type", "")):
            continue  # notify（收下即可）/answer（无需再答）——不回
        reply_type = card_msg.reply_type_of(hdr.get("type", ""))
        if not reply_type:
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "reply_type_missing", "msg_id": hdr["id"]})
            continue
        if not content.strip():
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "no_content", "msg_id": hdr["id"]})
            continue
        try:
            result = await card_tools.send_card_message(
                work_id, card_id, target_id=str(hdr["from"]),
                msg_type=reply_type, content=content, reply_to=hdr["id"])
        except Exception as exc:  # noqa: BLE001 薄壳异常按 send_failed 处置
            logger.error("[S4] 回发异常 wid=%s card=%s msg=%s: %s",
                         work_id, card_id, hdr["id"], exc)
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "send_failed", "msg_id": hdr["id"],
                                 "error": str(exc)[:120]})
            continue
        if isinstance(result, dict) and result.get("error"):
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "send_failed", "msg_id": hdr["id"],
                                 "error": str(result.get("error"))[:120]})
            continue
        session_ops.ops_log("card_msg_answer_sent", work_id, card_id,
                            {"msg_id": str((result or {}).get("msg_id", "")),
                             "target": str(hdr["from"]), "reply_to": hdr["id"]})


async def schedule_card_message_triggers(state: RunState) -> None:
    """A 的回合收口时对本回合记下的全部 pending target 异步起 B 的回合。

    - 遍历 `card_msg.TRIGGERS`（K2 落信时记下的调起意图），读完即清（不累积）；
    - 逐 target 用 `card_msg.find_pending` 判 pending（零新增存储，从档案推导）；
    - 有 pending → `asyncio.create_task` 起收件方回合（激励文 append_user=False），
      **非阻塞**——不 await 任务完成，异常经 done_callback 记日志；
    - 快速通道 vs 持久保证两层分工（写死）：create_task 可能因进程退出而丢；
      档案里的信是持久保证——丢任务不丢信，下次任一回合收口时重新判 pending
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
        try:
            blocks = await session_facade.read(work_id, target_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("[K3] 读收件方档案失败 wid=%s target=%s: %s",
                         work_id, target_id, exc)
            continue
        pending = card_msg.find_pending(blocks)
        if pending is None:
            # 信已被先前某次收口调起并处理完（档案已有 assistant 块）——
            # consumed 的事实打点归 K4 端点侧，此处不再重复调起。
            continue
        session_ops.ops_log("card_msg_deliver", work_id, target_id,
                            {"msg_id": str(pending.get("content", ""))[:1] and
                             (card_msg.parse_header(str(pending.get("content") or ""))
                              or {}).get("id", ""),
                             "msg_ids": list(msg_ids)})
        # [用户 2026-10-11 11:44] 激励文带发件卡名称＋类型中文名；类型名
        # 从注册表现读（display_of 未命中回退类型码），全程零类型分支——
        # 新增类型不改代码。[迭代五 S3·方案 4.2.4] 模板升级：随消息提供该
        # 类型的应对说明（注册表 note——S1 起含应对半句，即「按说明应对」
        # 的数据源）；解析/读卡/entry 异常回落默认文、不阻断调起。
        # [迭代五 S4·步骤①] 调起前尾批 id 集快照：档案末尾向前扫到第一条
        # assistant 为止的全部 card: 块（含 pending 自身、越 human 块）——
        # 回合结束后 find_pending 必返 None（finish 落 assistant 块先于
        # _run_message 返回），不可事后取。快照＝回发边界：只回发 id∈快照
        # 的卡块，回合期间新到信不回发、留给下个收口。快照在激励文 try 外
        # 初始化（空集）——防解析失败路径 NameError。
        snapshot_ids: set[str] = set()
        for blk in reversed(blocks):
            _actor = str(blk.get("actor") or "")
            if _actor == "assistant":
                break
            if _actor.startswith("card:"):
                _hdr = card_msg.parse_header(str(blk.get("content") or ""))
                if _hdr and _hdr.get("id"):
                    snapshot_ids.add(str(_hdr["id"]))
        prompt = _CARD_MSG_PROMPT
        try:
            hdr = card_msg.parse_header(str(pending.get("content") or ""))
            if hdr and hdr.get("from"):
                card_meta = (await card_store.read_card(
                    work_id, hdr["from"])).get("meta") or {}
                title = str(card_meta.get("title") or hdr["from"])
                disp = card_msg.display_of(hdr.get("type", ""))
                entry = card_msg.entry_of(hdr.get("type", "")) or {}
                note = str(entry.get("note") or "")
                prompt = (
                    f"上一轮之后，卡 {hdr['from']}（《{title}》）给你发来一条消息"
                    f"（类型：{disp}）。{note} "
                    "见上文以 card: 开头的条目，请阅读这些消息并处理。"
                )
        except Exception as exc:  # noqa: BLE001 激励文失败不阻断调起
            logger.warning("[K3] 构造卡间激励文失败、回落默认文: %s", exc)
            prompt = _CARD_MSG_PROMPT
        task = asyncio.create_task(
            _routes._run_message(work_id, target_id, prompt,
                                 None, append_user=False))
        task._card_msg_target = target_id  # type: ignore[attr-defined]
        task._card_msg_snapshot_ids = frozenset(snapshot_ids)  # type: ignore[attr-defined]
        task.add_done_callback(_log_trigger_task_done)

        # 忙时观察位：create_task 已起，若收件方在途，_run_message 内部
        # turn_try_acquire 失败返回 409 —— 由包裹协程转译为 ops card_msg_pending。
        # [迭代五 S4·步骤②] 触发判据扩展：成功回合的返回值是 dict（finish
        # 响应体，无 status_code 属性——既有机制事实，:907 现状 getattr 对
        # dict 恒 None，恰是它只判 409 能工作的原因）→ 执行回发；具 status_code
        # 的响应对象：409 保持既有打点；502/404/423 → 不回发＋failed 留痕；
        # 其余形态打日志不回发。「非 409」不是成功判据。
        async def _watch_pending(t: "asyncio.Task", _wid: str = work_id,
                                 _tid: str = target_id,
                                 _snap: frozenset = frozenset(snapshot_ids)) -> None:
            try:
                resp = await t
            except Exception as exc:  # noqa: BLE001 已由 done_callback 记日志
                logger.error("[K3] 调起任务异常（watch）target=%s: %s", _tid, exc)
                return
            if isinstance(resp, dict):
                try:
                    await _auto_reply_pending_letters(_wid, _tid, _snap)
                except Exception as exc:  # noqa: BLE001 回发通道绝不抛出
                    logger.error("[S4] 自动回发通道异常 target=%s: %s", _tid, exc)
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

        asyncio.create_task(_watch_pending(task, work_id, target_id))
