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
- `check_control(state) -> None`：**中止缝（空实现）**。step 边界调用点已就位，
  函数体为空——为执行环与 B-14 软预算预留挂点。**不做 job 系统、不上队列、
  不实现任何真实中止逻辑**（§2.3 明令）。

**编排不变量（拆分必须逐字保持，动了就是行为漂移）**：
1. 轮次上限：**无**（`[无闸改造 2026-10-02]` 循环不设封顶，模型停即收）。
2. 三席触发条件：`mode != fastlane` 且本轮无 tool_calls 且 `seats_done` 假；
   进场恰一次（`seats_done` 一次性闸）。
3. 拼接顺序：T14「分析在前、回执在后、`\n\n` 分隔」，落位在日志写之后、
   parse_proposals 之前。
4. 落盘时机：用户条在装配前；assistant 条在全部交付段之后；
   prompt 日志无论成败都写。
5. SSE 事件序：`run_start → context → fastlane_route → round_start →
   (tool_call → tool_result/card_op)* → seats_enter → seat_* → deliver →
   proposal → done`，逐字见 `tests/_golden/s*_sse.log`。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..storage import card_store, memory_store, session_store, work_store
from . import (
    card_proposal,
    card_tools,
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
    # [信息流 Task1] 快车道分流判定结果（由装配段算好写入）
    mode: str = "chain"
    matched_rule: str = "未命中"
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
    # 中止缝预留（B-14 软预算）：stop_requested 由外部控制面置位，
    # 本模块**不置位、不消费**——check_control 为空实现。
    stop_requested: bool = False


@dataclass(frozen=True)
class StepOutcome:
    """单步推进的结果。`kind` ∈ {continue, seats, deliver, error}。"""

    kind: str
    detail: str = ""


# ============================================================
# 中止缝（空实现——签名定死，函数体空）
# ============================================================

def check_control(state: RunState) -> None:
    """step 边界的控制钩子（**空实现**）。

    [T-0009 §2.3] 留中止缝：函数存在、签名定死、函数体空，为执行环与
    B-14 软预算预留挂点。**当前不做 job 系统、不上队列、不实现任何真实
    中止逻辑**——调用点在 step 入口/出口各一处（见 `step`），行为恒为 no-op。
    """
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
    ]
    # [T11-T6] 轮槽（请求局部对象）：包住同一条列表。折叠关时装配逐字同旧
    # （wire_messages 原样返回本列表）；首轮固化与抢救回注走 append_plain
    # （非轮、永不折叠——不折红线），assistant＋tool 结果走 begin_round/
    # append_tool_result（成轮，可折单元）。
    state.slot = tool_fold.ToolTurnSlot(state.prior_for_gateway)
    state.tools = card_tools.build_tool_schemas()
    # history_len 此刻一次算定（此后本列表会被轮槽就地 append 增长）
    state.history_len = len(state.prior_for_gateway)


# ============================================================
# 段③：工具循环 —— 单步推进
# ============================================================

async def step(state: RunState) -> StepOutcome:
    """一次状态推进＝工具循环的一个显式状态点。

    outcome：
      continue —— 本轮执行了工具（或喂了抢救反馈），应继续下一轮
      seats    —— 本轮无工具且判定该进三席（caller 负责其后置流程）
      deliver  —— final_reply 已定，可交付
      error    —— 本轮出错（error 字段已填，走错误返回）
    """
    check_control(state)          # 中止缝：入口挂点（空实现）

    state.rounds += 1
    if state.rounds == 1:
        # [E-探针制 2026-10-07] 首轮＝纯主模型带工具查证（原 call_simple
        # 观点前置编排退役；三席改在工具循环收尾处进场）。
        progress.set_stage(state.work_id, state.card_id, "started", "主模型查证中")
        event_stream.emit(state.work_id, state.card_id, "round_start",
                          {"round": state.rounds, "phase": "main"})
        state.model_calls += 1
        result, state.model_used = await model_service.call_main(
            state.system_prompt, state.user_prompt, state.prior_for_gateway,
            tools=state.tools,
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
        result, state.model_used = await model_service.call_main(
            state.system_prompt, state.user_prompt, wire_messages, tools=state.tools,
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
        # [信息流 Task1] 快车道＝单模型直答（A97）：跳三席与统合官，
        # 本轮即交付（chain 路径完全不受影响——mode=="chain" 不进此支）。
        if state.mode == "fastlane":
            return StepOutcome("deliver", "fastlane")
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
        # [信息流 Task4] 并表（A101）：快车道的卡操作落 card_op；
        # chain 保持 tool_result。全日志打点（A96）。
        if state.mode == "fastlane" and name in (
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
    check_control(state)      # 中止缝：出口挂点（空实现）
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
    result, state.model_used = await model_service.call_main(
        state.system_prompt, synth_input, state.prior_for_gateway, tools=state.tools,
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
    # [T10-1-4] 回复条携带与用户条逐字相同的 pair_id（V1）；打点含标识
    await session_store.append_message(state.work_id, state.card_id, "assistant",
                                       state.clean_text, model=state.model_used,
                                       pair_id=state.pair_id)
    logger.info("[T10] 回复落盘 pair_id=%s card=%s", state.pair_id, state.card_id)
    # [T12-SP-04] 回合末凝练触发（condense_fold）：assistant 落盘之后异步触发、
    # 不阻塞对话（异步是工程手段非闸，RD-04）。开关缺省关（RD-08）＝零动作；
    # 开时三期管线仍挂起待路线裁定，触发只量测＋留痕（memory_store.condense_turn_end）。
    memory_store.maybe_schedule_turn_end(state.work_id, state.card_id)
    progress.set_stage(state.work_id, state.card_id, "done", "完成")
    # [信息流 Task3/打点 A96] done 事件＋快车道调用数打点
    event_stream.emit(state.work_id, state.card_id, "done",
                      {"rounds": state.rounds, "model_calls": state.model_calls})
    if state.mode == "fastlane":
        logger.info("[flow] 快车道调用 lane=fastlane calls=%s rounds=%s card=%s",
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