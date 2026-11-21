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
import concurrent.futures
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..storage import card_db, card_store, memory_store, session_facade, session_ops, session_store, work_store
from ..thinkchain import board as thinkchain_board
from ..thinkchain import enabled as thinkchain_enabled  # [纯单模型 2026-10-17]
from ..thinkchain import seats_enabled as thinkchain_seats_enabled  # [规划驱动 2026-10-19]
from ..thinkchain.model_pick import pick_model as _pick_model  # 步类型路由消费点（_pick_step_model 调用）
from ..thinkchain import model_pick
from ..toolcard import driver as toolcard_driver
from ..infra import intervention_store
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
    tool_receipt,
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
    #: [排队语义 2026-10-23] 收口时队列里仍有未处理消息 ⇒ 置 True。
    #: 回合**自己不起下一回合**（那时互斥体还没摘，起了必被拒）；
    #: 由 routes 在 `turn_release()` 之后统一捞队列（deliver_now）接续。
    requeue_pending: bool = False
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
    # [worker 完工未报兜底 2026-11-10] 本回合已执行调用总数——card_tools
    # 的完工回执补发判据用它（纯应答回合不补）。
    turn_tool_call_count: int = 0
    #: [worker 完工未报兜底 2026-11-10] 回合开始时刻（epoch 秒）——判重锚：
    #: _CODER_DONE_SENT_AT ≥ 它 ⇒ 本回合已有回执发出，兜底跳过。
    turn_start_ts: float = 0.0
    # [正文伪调用抢救捎话] rescue_note_pending＝正文伪调用被替执行 ⇒
    # 首条工具结果前附纠正句（让它知道自己写歪了，防复发）。
    rescue_note_pending: bool = False
    # [抢救升惩罚 2026-11-11] 本回合已替执行正文伪调用的次数——达
    # _RESCUE_EVENT_CAP 后不再代执行，转回注「非法形式不执行」让惩罚
    # 真实落地（否则伪调用拿真结果＝正奖励，格式漂移永远治不了）。
    rescue_events: int = 0
    # [异常正文隔离 2026-11-20] 本回合检测到"调用写进正文且未派发"的
    # 原因标记（pseudo_refused_strict/pseudo_over_cap/pseudo_unparsed）——
    # 收口落回复块时打 quarantine＋ctx_content：原文留档案面，供模型
    # 使用的内容换成剥掉坏片段的残部＋宿主状态行。先置先记（同一回合
    # 可能先 refused 后 unparsed——首个原因是定性锚）。
    anomaly_quarantine: str = ""
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
    # [需求确定按钮改造 2026-10-29] 【需求确定】两种专用回合（根卡改正稿／其他卡拟草稿
    # 后开评审）的任务注入幂等闸——与 status_task_emitted 同款，本 run 只发一次。
    requirements_task_emitted: bool = False
    # [场/席位回合 2026-10-31 · venue] 一张卡当"场"，借**另一张卡的身份**跑一轮。
    # 三向锚点分离：执行锚＝card_id（会话/锁/事件流/停止都在它），身份锚＝act_as
    # （145区职能档案、工具面、模型绑定、工具调用者、回复块 from），上下文锚＝
    # ctx_card（bundle/历史/记忆按它取——评审席拿的是被评子卡的语境，用户令
    # 「评审席拿到的应该和子卡的上下文基本一致」）。
    #   · act_as     —— 席位卡 id；空＝普通回合，下面各键全不生效；
    #   · act_meta   —— 145区身份覆盖（评审职责书≠日常 charter，由调用方给；
    #                  None＝function_meta_for(act_as) 现查）；
    #   · ctx_card   —— 上下文锚卡；空＝card_id；
    #   · tool_allow —— 场授面（∩席位能力上限）：非 None 时工具面再收窄到该名单；
    #   · profile    —— compose_model_request 的 profile（席位＝viewpoint）；
    #   · prior_extra—— 追加进历史区的额外块（评审现场发言），由调用方组装。
    act_as: str = ""
    act_meta: Optional[dict[str, Any]] = None
    ctx_card: str = ""
    tool_allow: Optional[set] = None
    profile: str = ""
    prior_extra: list[dict[str, Any]] = field(default_factory=list)
    # [票落＝回合止 2026-11-15 · 用户令「approve/reject 之后最多一次调用」]
    #   · source        —— 本回合来源标记（_run_message 透传，判 review_step）；
    #   · seat_voted_end—— 本回合内票已落账：下一次模型调用是**最后一次**
    #                     （空工具面只够写收尾正文；它吐出什么都不再执行）。
    source: Optional[dict[str, Any]] = None
    # [批二·按任务缩量 2026-11-20] **本轮可信任务绑定**（显式）：
    # ``{"kind": "card"|"jurisdiction", "card_id": ...}``——由**知道任务的人**
    # 给（差事投递、目标驱动、回合入口）。没有它 ⇒ 范围判不出来 ⇒ 完整材料。
    task_binding: Optional[dict[str, Any]] = None
    task_contract: Optional[dict[str, Any]] = None
    seat_voted_end: bool = False
    # [席位会话 2026-11-18 · 用户令「一卡多会话」] 附加会话＝卡面＋职能人格：
    #   · session_id —— 会话锚（会话文件/历史原文/context_board/凝练/回合锁/
    #     叙事事件流/逐字流全部按它寻址）；空＝主会话（＝card_id，逐字不变）；
    #   · persona    —— 人格锚（145区身份按职能档案注入、回复块 from 归因）；
    #                  **只借身份**——工具面/模型层仍按 card_id（用户令「拥有和
    #                  子卡完全一样的权力」「模型同卡面」），与 act_as 的区别：
    #                  act_as 连工具面+模型层一起换成席位的。
    session_id: str = ""
    persona: str = ""
    # [调用来源与真实回执 2026-11-19] 本回合来源标记（routes 透传）：
    # directive＝卡间指令唤醒的回合（绑定动作判定用它）；human/internal 等原样。
    source_kind: str = ""
    # 忙卡收到的新指令按发令卡记录首次可用的回执位置。
    report_after: dict[str, int] = field(default_factory=dict)
    # 本回合端点协议快照（首轮模型返回的 protocol_meta.endpoint_protocol；
    # 供收口核对与短提示分类用）。
    endpoint_protocol: str = ""
    # 本回合派发台账的来源分类累计（native/declared_text/pseudo_compat 计数）。
    origin_counts: dict = field(default_factory=dict)
    # [按回执交付 2026-11-19] 收口核对结果（compose_delivery 写；finish 落
    # 会话块 receipt 字段——刷新后的历史与最终回复用同一 verdict）。
    receipt: dict = field(default_factory=dict)
    # [定向补救续跳 2026-11-19] 绑定动作回合零派发时的定向续跳已用掉一次
    # 的标记（一次封顶——续跳后仍零调用即收口，防循环）。
    remediation_done: bool = False
    # [2026-11-20 返修] 补救已裁定但未真发的标记（占位失败/去重命中）——
    # 延后的 pseudo_call_unparsed 凭它带 handled=runner，压 Harness 二次唤醒。
    remediation_adjudicated: bool = False
    # [异常事实延后emit 2026-11-20] 本跳检测到的伪调用事实载荷
    # （step 内不落事件——收口点知道本跳被续跳/裁定后才发，带 handled 标记；
    # 调用循环（routes step 循环）是唯一定点）。
    hop_unparsed: Optional[dict] = None
    # [B3 请求对账 2026-11-20] 本跳正文解析出的候选调用数（原生调用在场时
    # 被丢弃不入执行链的也要记——候选数与实际派发数分开记账）。
    hop_text_candidates: int = 0
    # [B3 请求对账 2026-11-20] 本跳请求级对账载荷（step 内建好，
    # 调用循环补真实派发数后落事件＋台账）。
    hop_accounting: Optional[dict] = None
    hop_transport: dict[str, Any] = field(default_factory=dict)
    # [B3 请求对账 2026-11-20] 本跳**进入执行器前**的派发尝试数
    # （execute_tool 调用前 +1）——派发后异常/取消时
    # attempts > executed 的差值就是「发出去了但没跑完」的实证。
    hop_dispatch_attempts: int = 0
    # [B2返修·请求边界 2026-11-20] 在途补救占位的 action_id 与绑定——
    # 占位 pending 到「补救模型请求真正发出」才写回 sent、回包到才
    # delivered；决定点不预支 delivered（中断在请求前 ⇒ 恢复对账
    # 凭请求记录核销，不会假 delivered）。
    remediation_action_id: str = ""
    remediation_bound: dict = field(default_factory=dict)
    # [系统结果 2026-11-19 · 超长会话漏调专项] 汇报唤醒回合的执行事实呈现
    # （compose_delivery 写：status/items/text；finish 落会话块 receipt.result）。
    system_result: dict = field(default_factory=dict)


def _sid(state: "RunState") -> str:
    """会话锚：席位会话→自己的 session key；否则＝本卡（主会话）。"""
    return str(getattr(state, "session_id", "") or "") \
        or str(getattr(state, "card_id", "") or "")


def _remediation_request_boundary(state: "RunState") -> None:
    """适配器确认发送，或收到真实终帧反证已发，才登记 sent。"""
    aid = str(getattr(state, "remediation_action_id", "") or "")
    if not aid:
        return
    phase = "tools" if int(getattr(state, "rounds", 0) or 0) > 1 else "first"
    tool_receipt.ledger({
        "kind": "remediation_request",
        "work_id": state.work_id, "card_id": state.card_id,
        "pair_id": state.pair_id, "round": state.rounds,
        "action_id": aid, "phase": phase, **state.hop_transport})
    intervention_store.store().mark_action(
        state.work_id, aid, "sent",
        receipt={"via": "in_turn_continue", "pair_id": state.pair_id,
                 "round": state.rounds, "phase": phase})


def _remember_hop_transport(state: "RunState", facts: dict[str, Any]) -> None:
    first_dispatch = not state.hop_transport
    state.hop_transport = facts
    if first_dispatch:
        _remediation_request_boundary(state)


def _hop_accounting_error(state: "RunState", exc: BaseException) -> dict[str, Any]:
    """[B3返修·失败链路] 模型调用抛错时的请求级对账载荷——端点报错/
    断流/重试耗尽的请求同样要留「请求座/实际座/端点事实/错误类」的
    可复查记录，候选与派发数如实记零（请求未产出可结算回包）。
    routes 异常路径会把它发成 hop_accounting 事件＋台账。"""
    _ep = state.hop_transport
    return {
        "pair_id": state.pair_id, "round": state.rounds,
        "requested_model": str(getattr(state, "model_requested", "") or ""),
        "model_used_entry": _ep.get("model_used_entry") or "",
        "vendor": _ep.get("vendor") or "",
        "api_base": _ep.get("api_base") or "",
        "endpoint_model": _ep.get("endpoint_model") or "",
        "request_model": _ep.get("request_model") or "",
        "response_model": _ep.get("response_model") or "",
        "model_used": _ep.get("request_model") or "",
        "trace_id": _ep.get("trace_id") or "",
        "transport": _ep.get("transport") or "",
        "endpoint_protocol": _ep.get("endpoint_protocol") or "",
        "declared_text": _ep.get("endpoint_protocol") == "declared_text",
        "finish_reason": "",
        "native_calls": 0, "native_dropped": 0, "native_truncated": 0,
        "native_finish_tool_calls": False,
        "native_candidates": 0, "text_candidates": 0, "candidates": 0,
        "admitted": 0, "refused": 0,
        "executor_attempts": 0, "completed": 0, "succeeded": 0,
        "executed_tools": [],
        "latency_ms": None, "tokens_used": None,
        "anomaly": "model_call_failed",
        "failed": True,
        "error": f"{type(exc).__name__}: {exc}"[:300],
    }


def _remediation_response_boundary(state: "RunState", result: dict) -> None:
    """终帧完成才核销 delivered，合成收口按本跳发送证据核销。"""
    aid = str(getattr(state, "remediation_action_id", "") or "")
    if not aid:
        return
    if result.get("stopped") or not result.get("terminal_received", True):
        sent = bool(state.hop_transport)
        reason = "sent_no_terminal_frame" if sent else "no_transport_evidence"
        state.remediation_action_id = ""
        intervention_store.store().mark_action(
            state.work_id, aid, "unverified" if sent else "stale",
            receipt={"via": "in_turn_continue", "pair_id": state.pair_id,
                     "round": state.rounds,
                     "note": reason})
        tool_receipt.ledger({
            "kind": "remediation_incomplete",
            "work_id": state.work_id, "card_id": state.card_id,
            "pair_id": state.pair_id, "rounds": state.rounds,
            "action_id": aid, "reason": reason})
        return
    if not state.hop_transport:
        _remediation_request_boundary(state)
    state.remediation_action_id = ""
    intervention_store.store().mark_action(
        state.work_id, aid, "delivered",
        receipt={"via": "in_turn_continue", "pair_id": state.pair_id,
                 "round": state.rounds})
    event_stream.emit(state.work_id, _sid(state), "remediation_jump", {
        "bound": dict(getattr(state, "remediation_bound", {}) or {}),
        "rounds": state.rounds, "action_id": aid,
        "origin_counts": dict(state.origin_counts)})
    tool_receipt.ledger({
        "kind": "remediation_jump",
        "work_id": state.work_id, "card_id": state.card_id,
        "pair_id": state.pair_id, "rounds": state.rounds,
        "bound": getattr(state, "remediation_bound", {}) or {},
        "action_id": aid})


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
    verdict = control.check(state.work_id, _sid(state))
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
    import time as _t_perf
    _perf = _t_perf.perf_counter
    _t0 = _perf()          # 函数起点（memory 段起点）
    # [场/席位回合 2026-10-31 · venue] 三向锚点：执行锚仍是 state.card_id（它决定
    # 会话/锁/事件流），身份锚 act_as（145区/工具面），上下文锚 ctx_card
    # （bundle/历史/记忆——评审席拿的是被评子卡的语境）。普通回合三者同为 card_id。
    _actor_id = str(state.act_as or "") or state.card_id
    _ctx_id = str(state.ctx_card or "") or state.card_id
    # [席位会话] 凝练记忆按**会话锚**取——附加会话不继承卡的记忆
    # （用户令「不继承子卡自己的记忆、上下文」），它自己的记忆从空开始。
    history_id = str(getattr(state, "session_id", "") or "") or _ctx_id
    memory_snapshot = (memory_store.read_snapshot(state.work_id, history_id)
                       if memory_store.condense_enabled() else None)
    _t_mem1 = _perf()
    # [预设职能 2026-10-24] 145 区(你的职能)取数:独立职能卡(fn_NNN)或职能化的
    # 根卡(总裁)——此前 build_context_bundle 的 function_meta 参数在生产链路
    # 无人传 ⇒ 145 区从不渲染(职能不知道自己是谁)。fail-open:查不到传 None。
    # [venue] act_meta 非 None ⇒ 调用方给了身份覆盖（评审职责书），直接用它；
    # 否则按**身份锚**(act_as 或 persona 或本卡)现查职能档案——persona 只借
    # 身份（145区+from 归因），工具面/模型层在别处仍按 card_id。
    _identity_id = str(state.act_as or "") or str(state.persona or "") \
        or state.card_id
    _function_meta = state.act_meta
    if _function_meta is None:
        try:
            from . import function_room
            _function_meta = function_room.function_meta_for(state.work_id,
                                                             _identity_id)
        except Exception:  # noqa: BLE001 身份取不到 ⇒ 不出 145 区,不挡回合
            _function_meta = None
    # [批二·按任务缩量 2026-11-20] 工具面**先建**：本轮有没有 read_card 决定
    # 依赖正稿能不能只给坐标（读得到才给坐标，读不到必须给全文）。工具面不依赖
    # 装配产物（只用身份锚/卡面/场授名单），故前置不改变任何既有语义。
    _cards_for_tools = card_tools._collect_card_meta(state.work_id)
    if _actor_id != state.card_id and _actor_id not in _cards_for_tools:
        # [venue] 席位不在 cards 表（职能档案在 function_store）⇒ 合成卡面再
        # 建工具面，否则分类闸"认不出卡"把基础面清空、只剩末段追加的 approve。
        _cards_for_tools[_actor_id] = _synth_seat_meta(
            state.work_id, _actor_id, _cards_for_tools)
    state.tools = card_tools.build_tool_schemas(
        card_id=_actor_id,     # [venue] 工具面按身份锚（席位自己的能力清单）下发
        work_id=state.work_id,
        cards=_cards_for_tools,
        op=state.op,   # [更新现状按钮改造] op 回合才追加专用工具（其余回合逐字不变）
    )
    from . import task_basis
    if (not state.op and not state.act_as
            and state.tool_allow is None and _sid(state) == state.card_id):
        state.task_contract = task_basis.bound_task(
            state.work_id, state.card_id, _scope_hint_for(state))
        if state.task_contract and card_tools._actor_can_switch(
                _cards_for_tools.get(_actor_id), card_tools.policy.SWITCH_READ):
            state.tools.append(card_tools._schema_of(card_tools.TOOL_REGISTRY["task_deliver"]))
    # [venue] 场授面再收窄：席位能力 ∩ 场允许（评审＝只读＋投票＋台账）。
    # 两级过滤语义不变：先能力后场地，'在场'不等于'全能'。
    if state.tool_allow is not None:
        _allow = {str(n) for n in state.tool_allow}
        state.tools = [
            t for t in state.tools
            if str((t.get("function") or {}).get("name") or t.get("name") or "")
            in _allow
        ]
        # [对齐场 2026-11-11] 场授专用件补回：align_end 这类不进常规工具面
        # （VENUE_ONLY_TOOLS），唯一的下发通道就是场授名单点了名——收窄把
        # 它们连同别的件一起滤掉了，这里把名单里的场授件补回面里。
        _have = {str((t.get("function") or {}).get("name") or t.get("name") or "")
                 for t in state.tools}
        for _vn in sorted(_allow & card_tools.VENUE_ONLY_TOOLS):
            _def = card_tools.TOOL_REGISTRY.get(_vn)
            if _def is not None and _vn not in _have:
                state.tools.append(card_tools._schema_of(_def))
    bundle = context_assembler.build_context_bundle(
        state.work_id, _ctx_id,
        history_id=history_id,
        memory_snapshot=memory_snapshot,
        history_source=state.prior,
        history_extra=state.prior_extra,
        function_meta=_function_meta,
        # [本轮消息双重计数 2026-11-14] 本轮 user 块落盘即进板；按本回合
        # pair_id 从板行滤掉——它走 user 通道，不该再当历史投影一遍。
        current_pair_id=state.pair_id,
        # [2026-11-19 性能批次三] 回合开场不再渲染 viewpoints 剖面——席位进场
        # 时 run_seats 带 seat_identity 重新 compose 真正的三席请求（改前开场
        # 渲染从未作为模型输入，等于每回合白付一次全量分区渲染）。
        with_viewpoints=False,
        # [批二·按任务缩量 2026-11-20] 本轮任务范围判定输入：回合来源／身份锚／
        # 读卡能力。判定只用结构化字段；判不出来 ⇒ unknown ⇒ 逐字同旧。
        scope_hint=_scope_hint_for(state),
    )
    _t_ctx1 = _perf()
    state.context_ctx = bundle["context"]
    state.condense_state = state.context_ctx.memory_state
    state.ctx_regions = bundle["regions"]
    state.neutral_context = bundle["main"]["neutral_context"]
    state.system_prompt = bundle["main"]["system_prompt"]
    state.vp_system_prompt = (bundle["viewpoints"] or {}).get("system_prompt", "") \
        if bundle["viewpoints"] is not None else ""
    state.prior_for_gateway = bundle["main"]["prior_messages"]
    event_stream.emit(state.work_id, _sid(state), "context", {
        "regions": [str(r.get("region", "")) for r in state.ctx_regions],
        "bytes": len(state.neutral_context.encode("utf-8")),
    })
    try:
        from ..infra import observe as _obs
        # 〔2026-11-19 返工3·独立分段〕每段用自己的起点/终点（改前的共享
        # _t0 差值减法在跨线程委托场景会出负数——受控复验实测
        # build_ctx_render=-6546.2ms）。段定义：
        #   memory_load      = 凝练记忆和历史的同版本快照；
        #   build_ctx_render = build_context_bundle 整段（取数+分区渲染）。
        _obs.log_assembly(
            work_id=state.work_id, card_id=_ctx_id, profile="main",
            phase="build_context",
            ms={"memory_load": round(max(0.0, (_t_mem1 - _t0)) * 1000.0, 2),
                "build_ctx_render": round(max(0.0, (_t_ctx1 - _t_mem1)) * 1000.0, 2)},
            pair_id=str(state.pair_id or ""),
        )
    except Exception:  # noqa: BLE001 观测永不阻断装配
        pass
    # [批四 · 工具证据投影 2026-11-21] 试点闸：TOOL_EVIDENCE_WIRE=1 且本轮是
    # **普通 main 回合**（沿用批二的三条识别依据：act_as/ctx_card 身份锚、
    # source.kind）——评审席/对齐/代席身份一律走原投影。
    _sk = str((state.source or {}).get("kind") or "")
    _ev_wire = (not state.act_as or state.act_as == state.card_id) \
        and (not state.ctx_card or state.ctx_card == state.card_id) \
        and _sk not in ("review_step", "align_turn")
    state.slot = tool_fold.ToolTurnSlot(state.prior_for_gateway,
                                        evidence_wire=_ev_wire)
    # history_len 是注册表统一投影出来的历史条数快照；轮槽之后会就地追加。
    state.history_len = len(state.prior_for_gateway)


def _scope_hint_for(state: RunState) -> dict[str, Any]:
    """本轮**任务范围判定**的输入（批二·按任务缩量）。

    只给**结构化字段**：回合来源标记、身份锚、上下文锚、读卡能力、显式任务绑定。
    正文里的卡号一概不给（任务书：「正文里的卡号只供查漏」）。
    """
    names = {str((t.get("function") or {}).get("name") or t.get("name") or "")
             for t in (state.tools or [])}
    return {
        "source": dict(state.source or {}) if state.source else None,
        "source_kind": str(getattr(state, "source_kind", "") or ""),
        "act_as": str(state.act_as or ""),
        "ctx_card": str(state.ctx_card or ""),
        # [批二·按任务缩量 2026-11-20] **显式任务绑定**（知道任务的人给的）。
        "task_binding": (dict(state.task_binding)
                         if getattr(state, "task_binding", None) else None),
        # 没有 read_card ⇒ 依赖正稿不能只给坐标（读不到就是白给）⇒ 回退全文。
        "readback_available": "read_card" in names,
    }


def _synth_seat_meta(work_id: str, seat_id: str,
                     cards: dict[str, Any]) -> dict[str, Any]:
    """给不在 cards 表里的**职能席位**合成卡面（card_type=function）。

    职能档案在 function_store 不在 cards 表——能力闸/工具面查 `cards[seat]`
    会把席位当"认不出的卡"全拒。合成面与建卡同源：profile 实挂 abilities
    优先；空 ⇒ 按卡型现算（`_abilities_of`/`function_room._mounted_abilities`
    的同一条回退路，不是放宽——`abilities_for` 本身∩卡型模式上限∩perms）。
    """
    meta: dict[str, Any] = {"card_id": seat_id, "card_type": "function"}
    try:
        from ..storage import function_store as _fs
        prof = _fs.get_profile(work_id, seat_id) or {}
    except Exception:  # noqa: BLE001
        prof = {}
    ab = prof.get("abilities")
    if isinstance(ab, list) and ab:
        meta["abilities"] = [str(x) for x in ab]
        return meta
    try:
        from ..permissions import policy as _pol
        from ..infra import capability_registry as _cap
        pc = next((str((m or {}).get("project_class") or "")
                   for m in (cards or {}).values()
                   if str((m or {}).get("card_type") or "") == "project"), "")
        perms, _n = _pol.compute_perms(pc, "function")
        meta["abilities"] = list(_cap.abilities_for(perms, "manual", "function"))
    except Exception:  # noqa: BLE001 算不出来 ⇒ 交下游判（认不出＝拒，不放宽）
        pass
    return meta


def compose_model_request(
    state: RunState,
    *,
    profile: str,
    prior_messages: Optional[list[dict[str, Any]]] = None,
    prior_key: str = "history",
    user_content: Optional[str] = None,
    turn_once: Optional[list[dict[str, Any]]] = None,
    turn_hint: Optional[list[dict[str, Any]]] = None,
    turn_tail: Optional[list[dict[str, Any]]] = None,
    seat_identity: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """所有主对话/三席请求都经 context registry 组合，不在调用点手拼区。"""
    if state.context_ctx is None:
        # 兼容白盒直调（生产链路 build_context 一定先设 context_ctx）。
        return {
            "system_prompt": state.system_prompt if profile == "main" else state.vp_system_prompt,
            "user_prompt": user_content if user_content is not None else state.user_prompt,
            "prior_messages": prior_messages or [],
            "turn_once": list((turn_once or []) + (turn_hint or [])),
            "turn_tail": turn_tail or [],
        }
    request = context_regions.compose_model_context(
        state.context_ctx,
        profile=profile,
        prior_messages=prior_messages,
        prior_key=prior_key,
        user_content=user_content if user_content is not None else state.user_prompt,
        turn_once=turn_once,
        turn_hint=turn_hint,
        turn_tail=turn_tail,
        seat_identity=seat_identity,
    )
    return request


# [2026-11-19 性能批次三] 装配/出站分段打点：每跳一行 assembly 观测。
# 序列化段＝轮槽折叠 + compose_model_request（本地读取不在段内——那次取数
# 在 build_context 已单独计）；出站段＝gateway 全程（含其内部序列化与重试）。
# 首跳 phase="first"、续跳 phase="tools"；凝练请求经 model_call 行的 caller
# （condense:*）区分，不在此重复。
from time import perf_counter as _perf_counter  # noqa: E402


def _log_model_hop(state: "RunState", t_ser0: float, t_out0: float,
                   seat: str, phase: str) -> None:
    """一跳的分段耗时落 observe（打点失败绝不影响回合）。"""
    try:
        from ..infra import observe as _obs
        _now = _perf_counter()
        _obs.log_assembly(
            work_id=state.work_id, card_id=state.card_id,
            profile=str(state.profile or "main"), phase=f"model_hop:{phase}",
            ms={"serialize": round((t_out0 - t_ser0) * 1000.0, 2),
                "outbound": round((_now - t_out0) * 1000.0, 2)},
            round=int(state.rounds or 0), seat=str(seat or ""),
            pair_id=str(state.pair_id or ""),
            model_used=str(getattr(state, "model_used", "") or ""),
        )
    except Exception:  # noqa: BLE001 观测永不阻断装配
        pass


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
    # [venue] 模型绑定按**身份锚**（席位自己的模型配置），板快照仍按执行锚
    # （板是场的工作面，不是席位的）。
    _actor = str(state.act_as or "") or state.card_id
    if not thinkchain_enabled():   # [纯单模型 2026-10-17] 无步类型，直接 default 席
        return _pick_model("default", work_id=state.work_id, card_id=_actor)
    snap = thinkchain_board.snapshot_sync(state.work_id, state.card_id)
    role = "default"
    if snap is not None:
        steps = snap.get("steps") or []
        cur = snap.get("current", 0)
        if 0 <= cur < len(steps):
            role = steps[cur].get("type") or "default"
    seat = _pick_model(role, work_id=state.work_id, card_id=_actor)
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
        event_stream.emit(state.work_id, _sid(state), "step_start", {
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
        event_stream.emit(state.work_id, _sid(state), ev["type"], ev["data"])
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
        event_stream.emit(state.work_id, _sid(state), ev["type"], ev["data"])
        state.turn_tail.append(current_turn.project_status_task_message(ev["data"]))
    except Exception as exc:  # noqa: BLE001 注入零阻断
        logger.warning("[现状更新] 注入失败（不阻断）card=%s: %s", state.card_id, exc)


# ============================================================
# [需求确定按钮改造 2026-10-29 · 用户令] 【需求确定】专用回合 · 轮首任务注入
# ============================================================

def _sync_requirements_task(state: RunState) -> None:
    """专用操作注入点：**仅 requirements_final / requirements_draft 回合、仅首次**发一条。

    与「更新现状」同族：承载通道＝``turn_tail``（整轮任务指令，续跳轮仍要看得到），
    不随轮丢弃。两种回合注入的正文不同——
    · 根卡（``OP_REQUIREMENTS_FINAL``）：这一轮可直接改需求文档**正稿**；
    · 其他卡（``OP_REQUIREMENTS_DRAFT``）：这一轮拟**草稿**，拟完开评审。
    用户 2026-10-29 原话：「两边的实现差不多，但注入的提示词不同，流程也有差异」。
    """
    if state.op not in card_tools.REQUIREMENTS_OPS:
        return
    if state.rounds != 1 or state.requirements_task_emitted:
        return
    state.requirements_task_emitted = True
    try:
        ev = current_turn.build_requirements_task_event(
            state.work_id, state.card_id,
            final=(state.op == card_tools.OP_REQUIREMENTS_FINAL))
        event_stream.emit(state.work_id, _sid(state), ev["type"], ev["data"])
        state.turn_tail.append(
            current_turn.project_requirements_task_message(ev["data"]))
    except Exception as exc:  # noqa: BLE001 注入零阻断
        logger.warning("[需求确定] 注入失败（不阻断）card=%s: %s", state.card_id, exc)


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
        event_stream.emit(state.work_id, _sid(state), ev_type, data)
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
            state.work_id, _sid(state), "assistant_text",
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


async def _apply_steer(state: RunState, into_slot: bool = False) -> list[str]:
    """取走本卡的待投插话并**插进轮槽**——下一次模型调用即可见。

    [5715] 落点纪律：
    - ``into_slot=True``（**收口时**插话）：直接进轮槽 ⇒ 下一轮 prior 立刻带上它。
      收口时的插话是「模型这轮没看到、用户又补了一句」⇒ 必须真的进下一轮上下文。
      （若走 steer_tail，无工具轮的 steer_tail 永远不被固化 ⇒ 插话丢失——实测踩过。）
    - 轮首调用（默认）：rounds==1 ⇒ 暂存 ``steer_tail``，随 ``_engage_slot`` 固化
      （**在用户原文之后**，顺序与语义都对）；rounds>=2 ⇒ 直接进轮槽（续跳轮的
      ``wire_messages`` 直接带上它，tool 之后跟 user 是合法形态）。

    返回被消费的原始条目（mark+body 成品）——调用方据此如实提示来源
    （〔用户插话〕／〔卡间消息〕／〔系统消息〕），不冒充人声。
    """
    entries = steer.take_steer_entries(state.work_id, state.card_id)
    if any(entry.review_source for entry in entries):
        from . import review_runner

        entries = [
            entry for entry in entries
            if not entry.review_source
            or not await review_runner.dispatch_block_reason(
                state.work_id, state.card_id, entry.review_source, phase="steer")
        ]
    if not entries:
        return []
    # 插话进入模型前的工具结果属于原任务，不能证明新指令已汇报。
    state.report_after.update(
        (target, len(state.tool_events))
        for entry in entries for target in entry.report_targets
    )
    msgs = [entry.text for entry in entries]
    # 队列里存的是 mark+body 成品（request_steer 置位时拼好——用户插话带
    # 〔用户插话〕、卡间消息带 〔卡间消息〕），这里取走即用不再拼标。
    prepared = [{"role": "user", "content": t} for t in msgs]
    if into_slot or state.rounds > 1:
        if state.slot is not None:
            for m in prepared:
                state.slot.append_plain(m)
    else:
        state.steer_tail.extend(prepared)
    logger.info("[steer] 插话入槽 card=%s round=%s 条数=%s into_slot=%s",
                state.card_id, state.rounds, len(prepared), into_slot)
    return msgs


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


# ============================================================
# [模型中断续跑 2026-10-23 · 用户令] 模型调用失败 ⇒ **接着跑这一轮**，不许判死整轮。
#
# 用户原话：「哪个模型断连了都会返回报错，你全部不处理，就设个钟」
#   ⇒ 断连是**立刻可知**的可恢复事件（socket 异常 → TRANSPORT），
#     不是「这一轮结束了」。原来的行为：异常一路抛到 routes，outcome=error，
#     disarm，整轮死；已完成的工具调用白做。
#
# 这层做的事：在**同一个位置重发这次模型调用**。
#   - 不重跑整轮：已落盘的 assistant/tool 块不动，重发的只是这一轮的请求；
#   - 上下文天然接上：工具结果已经在会话里，重发时模型看到的就是「做到一半」；
#   - 有界：按**次数**（不是时间）放弃，放弃时如实上抛并留痕是哪一步死的；
#   - 层内兜底（fallbacks）仍在更下层，本层只管「同一次调用再试」。
# ============================================================

#: 中断后同一位置最多再试几次（含首次＝总次数）。按次数计，不设墙钟。
_RESUME_MAX_ATTEMPTS = 3

# [抢救升惩罚 2026-11-11] 每回合代执行正文伪调用的上限——纠偏捎话给
# _RESCUE_EVENT_CAP 次机会，此后不再兑现（伪调用拿真结果＝正奖励）。
_RESCUE_EVENT_CAP = 3

# [伪调用重调起 · harness 化 2026-11-12 · 用户令] 每卡一条伪调用发射
# 时间账（deque）：节流计数**装进事实本体**（recent 字段）——策略
# 点火即撕本子，回执/事件累计都会被抹，只有随事件走的计数算数。
# 内存账即可：重启清零只是重放额度，上限照样兜住（防的就是死循环）。
_PSEUDO_RECENT: dict[tuple[str, str], "collections.deque[float]"] = {}
_PSEUDO_RECENT_WINDOW = 3600.0   # 近 1 小时


def _pseudo_recent(work_id: str, card_id: str) -> int:
    """记一笔并返回本卡近 _PSEUDO_RECENT_WINDOW 秒内的伪调用发射次数（含本次）。"""
    import collections
    import time as _time
    key = (work_id, card_id)
    dq = _PSEUDO_RECENT.setdefault(key, collections.deque())
    now = _time.time()
    while dq and dq[0] < now - _PSEUDO_RECENT_WINDOW:
        dq.popleft()
    dq.append(now)
    return len(dq)


async def _call_model_resumable(state: "RunState | None" = None, **kwargs: Any):
    """``model_service.call_main`` 的中断续跑包装。

    失败重发**同一次调用**（kwargs 原样），最多 ``_RESUME_MAX_ATTEMPTS`` 次。
    每次失败发一条 ``model_resume`` 事件，前端/日志能看见「断过、接着跑了」。

    [不设退避 2026-10-23 · 用户令「整个机制全部通过信息反馈」]
    原来这里 `await asyncio.sleep(2)`。那是**没有反馈时的瞎等**：
      · 连接被重置/对端关闭 ⇒ socket 已经死了，立刻重连才对，等 2 秒纯浪费；
      · 限流(429)该等多久，**端点会告诉你**（Retry-After / provider_retry_after_ms），
        不该由我拍一个数——而那个反馈要 gateway 透出（gateway 禁改，暂拿不到）。
    ⇒ 现在**零等待立即重发**；限流退避交给下层网关自己的重试（它已有退避）。
    等 gateway 能透出 Retry-After 时，再按端点给的时长等，仍不是拍数。
    """
    from ..infra.errors import ModelError

    _wid = getattr(state, "work_id", "") or ""
    _cid = getattr(state, "card_id", "") or ""
    _rnd = getattr(state, "rounds", 0) or 0
    last: Optional[BaseException] = None
    if state is not None:
        from ..goal import driver, shadow_eval
        source = getattr(state, "source", None) or {}
        driver.accept_model_request(_wid, _cid, source)
        kwargs["on_transport"] = lambda facts: _remember_hop_transport(state, facts)
    pending_task = None
    if state is not None and getattr(state, "task_contract", None):
        from . import task_basis
        from ..storage import task_store
        current = task_basis.bound_task(_wid, _cid, _scope_hint_for(state))
        if not current:
            raise RuntimeError("当前任务已变更，请重新核对原派工")
        if _rnd > 1 and current["inject_state"] == "pending":
            raise RuntimeError("任务依据已换版，请以新依据重新起轮")
        state.task_contract = current
        if current["inject_state"] == "pending":
            text = task_basis.basis_text(_wid, current)
            kwargs["turn_once"] = [*(kwargs.get("turn_once") or []),
                                   {"role": "system", "content": text}]
            pending_task = current
            session_ops.ops_log("task_basis_assembled", _wid, _cid,
                                {"task_id": current["task_id"], "req_fp": current["req_fp"],
                                 "pair_id": state.pair_id})
    for attempt in range(1, _RESUME_MAX_ATTEMPTS + 1):
        try:
            if state is not None:
                shadow_eval.model_request(_wid, _cid, source,
                                          pair_id=getattr(state, "pair_id", ""), attempt=attempt)
            if pending_task:
                session_ops.ops_log("task_basis_model_request", _wid, _cid,
                                    {"task_id": pending_task["task_id"],
                                     "req_fp": pending_task["req_fp"],
                                     "pair_id": state.pair_id, "attempt": attempt})
            # [批三返修③] 发送边界复核：装配到出站之间换批/推版/同稿次
            # 改稿 ⇒ 不拿旧材料出站（首跳、续跳、同位置重试每个边界都查）。
            if state is not None:
                _why = await card_tools.turn_material_stale_reason(
                    _wid, (source or {}).get("material"))
                if _why:
                    raise card_tools.MaterialStale(
                        _why, pair_id=getattr(state, "pair_id", "") or "")
            # [批三返修②] 网关内部重试/换座同样过这道闸：钩子挂进
            # call_main → stream_call → _stream_once，**每次** attempt
            # 真实出站前复核——材料在网关重试边界过期 ⇒ MaterialStale
            # 在 try 之外传播，不吞进重试、不当作模型错误，由席位回合
            # 按控制回执收口。
            _prior_guard = kwargs.get("before_send")

            async def _material_guard() -> None:
                if _prior_guard is not None:
                    await _prior_guard()
                if state is None:
                    return
                _gwhy = await card_tools.turn_material_stale_reason(
                    _wid, (source or {}).get("material"))
                if _gwhy:
                    raise card_tools.MaterialStale(
                        _gwhy, pair_id=getattr(state, "pair_id", "") or "")
            kwargs["before_send"] = _material_guard
            response = await model_service.call_main(**kwargs)
            if pending_task and task_store.mark_injected(
                    _wid, pending_task["task_id"], _cid, pending_task["req_fp"]):
                session_ops.ops_log("task_basis_admitted", _wid, _cid,
                                    {"task_id": pending_task["task_id"],
                                     "req_fp": pending_task["req_fp"],
                                     "pair_id": state.pair_id})
            return response
        except ModelError as exc:
            last = exc
            if attempt >= _RESUME_MAX_ATTEMPTS:
                break
            logger.warning(
                "[resume] 模型调用中断，同一位置立即重发 %d/%d card=%s round=%d 原因=%s",
                attempt, _RESUME_MAX_ATTEMPTS - 1, _cid, _rnd, str(exc))
            try:
                event_stream.emit(_wid, _sid(state), "model_resume", {
                    "attempt": attempt,
                    "max": _RESUME_MAX_ATTEMPTS - 1,
                    "round": _rnd,
                    "reason": str(exc),
                })
            except Exception:  # noqa: BLE001 事件发不出去不影响续跑
                pass
    assert last is not None
    logger.error("[resume] 中断重发耗尽（%d 次）card=%s round=%d 原因=%s",
                 _RESUME_MAX_ATTEMPTS, _cid, _rnd, str(last))
    raise last


def _norm_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """tool_calls 归一化成平铺 ``{"name","arguments"}``。

    网关/适配器可能给两种形态：平铺 ``{name, arguments}`` 或 OpenAI 线格式
    ``{"function": {"name","arguments"}, "id","type"}``——下游全部按平铺消费，
    嵌套形态原先会在装配 assistant 消息时 `tc["name"]` 直接崩。
    ``arguments`` 给 JSON 字符串时解析成 dict（解析失败按空 dict，把
    原文留在 ``__raw`` 便于日志定位）。
    """
    out: list[dict[str, Any]] = []
    for tc in (raw or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if isinstance(fn, dict) and not tc.get("name"):
            name = str(fn.get("name") or "")
            args = fn.get("arguments")
        else:
            name = str(tc.get("name") or "")
            args = tc.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:  # noqa: BLE001 坏 JSON 不当参数喂，留原文
                args = {"__raw": args}
        out.append({"name": name,
                    "arguments": args if isinstance(args, dict) else {}})
        # [调用来源 2026-11-19] 来源标透传（model_service 流终帧解析链打的
        # _src_origin；缺省＝native）。归一化只重建 name/arguments，来源标
        # 原样带走——派发台账与事件按它单列来源。
        _src = str(tc.get("_src_origin") or "")
        if _src:
            out[-1]["_src_origin"] = _src
    return out


def _seat_voted_this_turn(state: RunState) -> bool:
    """本回合内 approve/reject 有**执行成功**的（tool_events 里 ok=True）＝
    本席的票已落账。只读本回合的执行记录：旧批次的票、被闸拒掉的票
    （ok=False）都不算——判据天然是回合域。"""
    return any(str(e.get("tool") or "") in ("approve", "reject")
               and e.get("ok") is True
               for e in (state.tool_events or []))


async def step(state: RunState) -> StepOutcome:
    """一次状态推进＝工具循环的一个显式状态点。

    outcome：
      continue —— 本轮执行了工具（或喂了抢救反馈），应继续下一轮
      seats    —— 本轮无工具且判定该进三席（caller 负责其后置流程）
      deliver  —— final_reply 已定，可交付
      error    —— 本轮出错（error 字段已填，走错误返回）
    """
    state.hop_dispatch_attempts = 0
    state.hop_text_candidates = 0
    state.hop_transport = {}
    state.hop_accounting = None
    # [草稿回执减肥 2026-11-14] 本轮标记进 contextvar——工具派发的唯一收口，
    # edit_requirements 据此判「本轮草稿基线给没给过」（改前轮内一次）。
    card_tools.set_tool_run_id(state.pair_id)
    # [评审按阶段缩量·材料绑定 2026-11-20] 席位回合的材料身份随 contextvar
    # 进工具面——approve/reject 在写事务内复核批次/稿次/原文指纹（旧材料的
    # 票落不到新稿）；非评审回合 source.material 空 ⇒ 零闸。
    card_tools.set_tool_turn_material(
        (state.source or {}).get("material") or {})
    verdict = check_control(state)   # 中止缝：入口挂点（[A8/R3] 控制三态）
    if verdict == control.STOP:
        # 人工停止：回合就地闭合（不落半截轮——assistant 块 end=human_stop）
        state.final_reply = state.final_reply if state.final_reply is not None else ""
        return StepOutcome("deliver", "human_stop")
    if verdict == control.DEFER_HUMAN:
        return StepOutcome("deliver", "policy_stop")

    # [wb_exec 2026-10-23] **委派分支**：worker 卡的回合交给 WorkBuddy 执行体
    # （见 src/api/wb_exec/）。命中时本函数跑完外部执行体并直接返回
    # StepOutcome("deliver", …)，让既有交付路径（write_logs → compose_delivery
    # → create_cards → finish）原样跑完。
    # **不命中 ⇒ 返回 None ⇒ 本函数后续逐字不变**（配置默认 enabled=false，
    # 且 exec_backend="system" 时也不委派）。
    from . import wb_exec as _wb_exec
    _delegated = await _wb_exec.maybe_delegate(state)
    if _delegated is not None:
        return _delegated

    # [思维链真实落地 2026-10-11] 判断归模型（C1）：判断协议已随 BASE_PROMPT
    # 进 system 区，模型首轮带全量工具（含 create_plan）读完协议自主判断——
    # 直答（空手正文即答复）或调 create_plan 拆解上板。代码层零判定、零
    # 机械预检、无独立判定调用（旧 JSON 判定轮已退役）。

    state.rounds += 1
    # [5715] 插话入槽：**每次模型调用前**取一次（在 prior 装配之前）——
    # 续跳轮的 wire_messages 直接带上它 ⇒ 用户在上一轮跑的时候插的话，
    # 这一轮模型就能看到（不必等整轮收口）。
    await _apply_steer(state)
    # [判断协议改注入 2026-10-14] 判断协议注入（仅轮首、且本卡无活板）——先于
    # 注入事件，使事件序＝user_message → judgment_protocol →（有板时）preamble/
    # injection → round_start。两者互斥（有板无判断、无板无注入来源），不会同现。
    # [venue] 席位回合不注判断协议——拆解上板是场自己的活，不是借身份的席位的。
    if not state.act_as:
        _sync_judgment(state)
    # [更新现状按钮改造 2026-10-16] 「更新现状」专用回合的轮首任务注入（仅 op 回合）。
    # 放在 _sync_injection 之前 ⇒ turn_tail 里专用任务先于工作规范/流程注入。
    _sync_status_task(state)
    # [需求确定按钮改造 2026-10-29] 【需求确定】专用回合的任务注入（仅这两种 op）。
    _sync_requirements_task(state)
    # [当前轮注入消息事件 2026-10-14] 模型调用前同步注入事件（160/165 消息化）：
    # 有来源才发；round1 暂存 turn_tail，后续轮直接进轮槽。放在 round_start 之前，
    # 使事件序＝user_message → preamble → injection → round_start/judgment/…
    _sync_injection(state)
    # [票落＝回合止 2026-11-15 · 用户令「approve/reject 之后最多一次调用」]
    # 席位回合里本回合的票已落账（tool_events 里 approve/reject ok）⇒
    # 这次调用是**最后一次**：空工具面只够写收尾正文；它若仍吐 tool_calls，
    # 下方 seat_voted_end 闸直接收口不执行——回合止，下一席由场叫
    # （实案：fn_001 投完票原地 wait 烧 11 轮 20 分钟等一个被自己占住的空位）。
    if (state.act_as and not state.seat_voted_end
            and str((state.source or {}).get("kind") or "") == "review_step"
            and _seat_voted_this_turn(state)):
        state.seat_voted_end = True
        state.tools = []
    if state.rounds == 1:
        # [E-探针制 2026-10-07] 首轮＝纯主模型带工具查证（原 call_simple
        # 观点前置编排退役；三席改在工具循环收尾处进场）。
        progress.set_stage(state.work_id, _sid(state), "started", "主模型查证中")
        event_stream.emit(state.work_id, _sid(state), "round_start",
                          {"round": state.rounds, "phase": "main"})
        state.model_calls += 1
        # [调用来源与真实回执 2026-11-19] 座位先定——端点协议按**实际选中**
        # 的模型查（vendor 显式登记，不猜模型名），短提示随它生成。
        _seat = _pick_step_model(state)
        state.model_requested = _seat
        state.endpoint_protocol = tool_receipt.endpoint_protocol_of(_seat)
        # 当前轮短提示：紧贴最新请求。走 **turn_hint 独立区**（仅随本次请求
        # 下发，不进轮槽、不固化、不进会话；与判断协议分离——once 区在纯单
        # 模式下被 thinkchain 开关整体吞掉，实测提示到不了模型面前）。
        # 内容只讲「怎样真调、以什么为准」，不授予权限、不证明已执行；
        # 无工具（纯对话轮）不出协议提示。TOOL_TURN_HINT=0 可关（四组对照 A/C）。
        _hint = tool_receipt.build_turn_hint(state.endpoint_protocol, state.tools)
        _hint_msgs: list[dict[str, Any]] = []
        if _hint is not None:
            _hint_msgs.append(_hint)
        # [汇报唤醒两条路径 2026-11-19] 收到卡间汇报/来信的回合：管理卡可以
        # 只回复判断，也可以采取行动——需要行动时先真调看到结果再汇报。
        # 「零调用却说已发」复发（card_007 22:31/22:35 实录）的当前轮修复面。
        if (tool_receipt.turn_hint_enabled()
                and tool_receipt.is_report_wake(state)):
            _hint_msgs.append(
                {"role": "system",
                 "content": tool_receipt.report_wake_hint(state.endpoint_protocol)})
        # [迭代三 S5] 首轮接线：按当前步 type 查表取模型（无活板回落 default）
        _t_ser0 = _perf_counter()
        request = compose_model_request(
            state, profile=state.profile or "main",
            prior_messages=state.prior_for_gateway,
            prior_key="history", user_content=state.user_prompt,
            turn_tail=state.turn_tail, turn_once=state.judgment_tail,
            turn_hint=_hint_msgs,
        )
        # [venue] 层内兜底按**身份锚**查模型层——座位与兜底同源（席位自己的层）。
        _actor = str(state.act_as or "") or state.card_id
        # [模型中断续跑 2026-10-23] 经续跑包装：断连在同一位置重发，不判死整轮
        _t_out0 = _perf_counter()
        try:
            result, state.model_used = await _call_model_resumable(
                state,
                system_prompt=request["system_prompt"],
                user_prompt=request["user_prompt"],
                prior_messages=request["prior_messages"],
                tools=state.tools,
                model_name=_seat,
                # [层内兜底 2026-10-21] 座位挂了就换同一层的下一个（不跨层）
                fallbacks=model_config.layer_fallbacks(
                    _seat, work_id=state.work_id, card_id=_actor),
                turn_tail=request["turn_tail"],
                turn_once=request["turn_once"],
                # [F22 · 2026-10-31 审计补口] 观测因果链：model_call JSONL 的
                # caller/卡位来自这里（改前主链路不传 ⇒ 生产打点恒空）。
                work_id=state.work_id, card_id=state.card_id,
                caller=f"turn:{state.pair_id}:r{state.rounds}",
            )
        except BaseException as _exc:
            # [B3返修] 失败请求同样留对账——异常上抛前把已知事实落账。
            state.hop_accounting = _hop_accounting_error(state, _exc)
            raise
        _remediation_response_boundary(state, result)
        _log_model_hop(state, _t_ser0, _t_out0, _seat, "first")
        # [DeepSeek 思考回传 2026-10-15] 暂存本轮思考，供存 assistant 条时回传
        state.turn_reasoning = result.get("reasoning_content") or ""
    else:
        # 工具续跳轮不重复咨询观点模型
        progress.set_stage(state.work_id, _sid(state), "tools",
                           f"第 {state.rounds - 1} 轮工具续跳")
        # [T11] 装配时折叠：折叠关原样返回本列表（逐字同旧）；
        # 折叠开对窗口外旧轮次压成【结论】＋【依据】（不改条数与配对）
        _t_ser0 = _perf_counter()
        # [批二·按任务缩量 2026-11-20 · 任务书 §二.3] **刷新当轮事实**：本回合里
        # 子卡跑了一步／建了新卡 ⇒ 清单与子卡现状（tail 通道，每跳重渲染）必须
        # 看到当前版本，不能停在回合开始那一份快照。system 通道不动（稳定前缀）。
        if state.context_ctx is not None:
            try:
                context_assembler.refresh_tail_facts(
                    state.context_ctx, state.work_id, state.card_id)
            except Exception:  # noqa: BLE001 刷新失败＝沿用旧版本，不阻断回合
                pass
        wire_messages = await state.slot.wire_messages()
        event_stream.emit(state.work_id, _sid(state), "round_start",
                          {"round": state.rounds, "phase": "tools"})
        state.model_calls += 1
        # [迭代三 S5] 续跳轮接线：同首轮（步中途 type 不变的板是常态；
        # 改板插步后以 current 步 type 为准）
        request = compose_model_request(
            state, profile=state.profile or "main",
            prior_messages=wire_messages,
            prior_key="tool_turn", user_content=state.user_prompt,
        )
        _seat = _pick_step_model(state)
        state.model_requested = _seat
        # [调用来源与真实回执 2026-11-19] 续跳轮同首轮：端点协议快照随实际座位刷新。
        state.endpoint_protocol = tool_receipt.endpoint_protocol_of(_seat)
        # [venue] 续跳轮同首轮：兜底按身份锚（与首轮同一 _actor 口径）。
        _actor = str(state.act_as or "") or state.card_id
        # [模型中断续跑 2026-10-23] 续跳轮同样经续跑包装
        _t_out0 = _perf_counter()
        try:
            result, state.model_used = await _call_model_resumable(
                state,
                system_prompt=request["system_prompt"],
                user_prompt=request["user_prompt"],
                prior_messages=request["prior_messages"],
                tools=state.tools,
                model_name=_seat,
                # [层内兜底 2026-10-21] 座位挂了就换同一层的下一个（不跨层）
                fallbacks=model_config.layer_fallbacks(
                    _seat, work_id=state.work_id, card_id=_actor),
                turn_tail=request["turn_tail"],
                turn_once=request["turn_once"],
                # [F22 · 2026-10-31 审计补口] 同上：工具轮观测因果链
                work_id=state.work_id, card_id=state.card_id,
                caller=f"turn:{state.pair_id}:r{state.rounds}:tools",
            )
        except BaseException as _exc:
            # [B3返修] 失败请求同样留对账——异常上抛前把已知事实落账。
            state.hop_accounting = _hop_accounting_error(state, _exc)
            raise
        _remediation_response_boundary(state, result)
        _log_model_hop(state, _t_ser0, _t_out0, _seat, "tools")
    # [DeepSeek 思考回传 2026-10-15] 暂存本轮思考，供存 assistant 条时回传
    state.turn_reasoning = result.get("reasoning_content") or ""
    terminal_received = not result.get("stopped") and result.get("terminal_received", True)
    tool_calls = _norm_tool_calls(result.get("tool_calls") or []) if terminal_received else []
    for _tc in tool_calls:
        _tc.setdefault("_src_origin", "native")
    assistant_content = result.get("content") or ""
    raw_assistant_content = assistant_content
    hop_anomaly = ""
    text_rescue_note = ""
    # [B3 请求对账 2026-11-20] 本跳候选/拒绝计数——候选（原生＋正文解析）
    # 与实际进入执行器、真实派发的数量分开记账。
    # [B3返修] 候选按**真实来源**分账：``tool_calls`` 列表里混着
    # model_service 流终帧从正文解析的调用（_src_origin=pseudo_compat /
    # declared_text）——它们是**正文来源**候选，不是原生 tool_calls
    # 分片；原生候选只数 _src_origin=native（缺省）的条目。
    _native_candidates = sum(
        1 for _tc in tool_calls
        if str(_tc.get("_src_origin") or "native") == "native")
    _hop_refused = 0
    state.hop_text_candidates = len(tool_calls) - _native_candidates

    # [验收模式 2026-11-19] 原生端点正文伪调用（model_service 流终帧解析链
    # 产出、带 _src_origin=pseudo_compat）不代执行：与 rescue strict 分支同
    # 处置——逐项剔除（同批 native 项照常执行），记录格式不符、反馈实际
    # 调用通道，当前动作仍待执行。
    if tool_calls and tool_receipt.strict_mode():
        _kept: list[dict[str, Any]] = []
        for _tc in tool_calls:
            if str(_tc.get("_src_origin") or "") != "pseudo_compat":
                _kept.append(_tc)
                continue
            hop_anomaly = "pseudo_refused_strict"
            event_stream.emit(state.work_id, _sid(state), "pseudo_call_refused",
                              {"count": 1,
                               "names": [str(_tc.get("name") or "")],
                               "round": state.rounds,
                               "endpoint_protocol": state.endpoint_protocol,
                               "via": "stream_parse"})
            _hop_refused += 1
            tool_receipt.ledger_tool(
                state.work_id, state.card_id, state.pair_id, state.rounds,
                tool=str(_tc.get("name") or ""), target="",
                origin="pseudo_compat", dispatched=False,
                error_code="pseudo_refused_strict",
                note="验收模式：正文伪调用（流终帧解析）不代执行")
            _refused_note = (
                f"[系统提示] 你正文中写的调用 {_tc.get('name')} 已被记录为"
                "格式不符、未执行（本端点只接受标准 tool_calls 通道）。"
                "如需执行，请通过 API 的 tool_calls 字段重发。")
            if not text_rescue_note:
                text_rescue_note = _refused_note
        tool_calls = _kept

    # [等待黑洞修复 2026-10-02] 标准字段为空时，抢救正文中
    # 文本形态的工具调用（DeepSeek 原生标记泄漏，三次同族发作定案）。
    # 纯机械模式匹配：解析出的调用并入标准执行链（执行＋结果回注），
    # 模型收到真实反馈；疑似标记解析不出调用时，把「未识别未执行」
    # 这个机械事实回注下一轮——关闭等待黑洞。
    # [调用来源与真实回执 2026-11-19] rescue 调用按**实际端点协议**分类来源：
    #   · declared_text —— 宿主声明协议端点（wb-cli/devin-cli）的合法正文调用：
    #     这是该通道的正常工作方式，正常执行、不受代执行配额约束；
    #   · pseudo_compat —— 原生端点把调用写进正文：旧版兼容层代执行（实跑
    #     计入 rescue 通道、单列来源）；验收模式（TOOL_CALL_STRICT=1）不代
    #     执行——记录格式不符、反馈实际调用通道，当前动作仍待执行。
    _rescue_origin = ("declared_text"
                      if state.endpoint_protocol == "declared_text"
                      else "pseudo_compat")
    if terminal_received and not tool_calls and assistant_content:
        rescue_calls, unresolved, stripped = \
            text_tool_rescue.extract_text_tool_calls(
                assistant_content,
                # [实际路由为准 2026-11-20 · 复现修复] 只查网关回报的
                # 实际座位键（result["model_used"]；兜底回落＝请求座）。
                # 请求启用而实际回退到未启用路由时 ⇒ 只记识别事实。
                protocols=model_config.text_protocols(
                    str(result.get("model_used") or "")
                    or (state.model_requested or "")))
        # [新协议按路由启用 2026-11-20 · §四] 声明的正文形态试点只放行
        # 只读工具；写卡/发信/建卡等有副作用工具记「已识别、路由未启用」
        # 事实，不派发——副作用面验收前不开（识别事实与未派发事实分账）。
        state.hop_text_candidates += len(rescue_calls)
        if rescue_calls:
            _denied = [c for c in rescue_calls
                       if c.get("_origin") in text_tool_rescue.READONLY_PROTOCOL_ORIGINS
                       and str(c.get("name") or "")
                       not in card_tools.READONLY_TOOLS]
            _hop_refused += len(_denied)
            if _denied:
                rescue_calls = [c for c in rescue_calls if c not in _denied]
                for _tc in _denied:
                    event_stream.emit(
                        state.work_id, _sid(state), "text_proto_refused",
                        {"name": str(_tc.get("name") or ""),
                         "proto": str(_tc.get("_origin") or ""),
                         "round": state.rounds,
                         "endpoint_protocol": state.endpoint_protocol})
                    tool_receipt.ledger_tool(
                        state.work_id, state.card_id, state.pair_id,
                        state.rounds,
                        tool=str(_tc.get("name") or ""), target="",
                        origin=_rescue_origin, dispatched=False,
                        error_code="proto_side_effect_denied",
                        note="新协议试点只放行只读工具；副作用工具按路由显式启用")
                hop_anomaly = hop_anomaly or "proto_side_effect_denied"
        if (rescue_calls and _rescue_origin == "pseudo_compat"
                and tool_receipt.strict_mode()):
            # [验收模式 2026-11-19] 原生端点正文伪调用不代执行：旧入口被
            # 标准协议覆盖——「验收模式的原生通道以标准协议为准」（方案
            # 通用调用链表第 3 行）。事件留格式不符事实，台账记未派发。
            logger.warning(
                "pseudo_calls_refused_strict count=%s names=%s",
                len(rescue_calls), [c["name"] for c in rescue_calls],
            )
            # [异常正文隔离 2026-11-20] 被拒伪调用的原文仍随回复块落会话——
            # 打标让投影/板/凝练只投残部＋状态行，原文留档案面。
            hop_anomaly = hop_anomaly or "pseudo_refused_strict"
            _hop_refused += len(rescue_calls)
            event_stream.emit(state.work_id, _sid(state), "pseudo_call_refused",
                              {"count": len(rescue_calls),
                               "names": [str(c.get("name") or "") for c in rescue_calls],
                               "round": state.rounds,
                               "endpoint_protocol": state.endpoint_protocol})
            tool_receipt.ledger_tool(
                state.work_id, state.card_id, state.pair_id, state.rounds,
                tool=",".join(str(c.get("name") or "") for c in rescue_calls),
                target="", origin="pseudo_compat", dispatched=False,
                error_code="pseudo_refused_strict",
                note="验收模式：正文伪调用不代执行，待模型走标准通道重发")
            rescue_calls = []
            text_rescue_note = (
                "[系统提示] 你本轮正文中出现的工具调用样式文本已被记录为"
                "格式不符、未执行（本端点只接受标准 tool_calls 通道，正文"
                "调用不解析执行）。如需执行，请通过 API 的 tool_calls 字段"
                "重发同样的调用。")
        elif (rescue_calls and _rescue_origin == "pseudo_compat"
                and state.rescue_events >= _RESCUE_EVENT_CAP):
            # [抢救升惩罚 2026-11-11] 代执行有配额：纠偏捎话已给过
            # _RESCUE_EVENT_CAP 次机会，此后伪调用不再兑现——回注事实
            # 让它走标准通道重发（伪调用拿真结果＝正奖励，是漂移的温床）。
            # [2026-11-19] 配额只限 pseudo_compat：declared_text 是声明
            # 端点的正常通道，掐死它等于废掉该端点的工具能力。
            logger.warning(
                "text_tool_calls_over_cap count=%s names=%s（不再代执行）",
                len(rescue_calls), [c["name"] for c in rescue_calls],
            )
            hop_anomaly = hop_anomaly or "pseudo_over_cap"
            _hop_refused += len(rescue_calls)
            event_stream.emit(state.work_id, _sid(state), "rescue_refused",
                              {"count": len(rescue_calls),
                               "names": [str(c.get("name") or "") for c in rescue_calls]})
            tool_receipt.ledger_tool(
                state.work_id, state.card_id, state.pair_id, state.rounds,
                tool=",".join(str(c.get("name") or "") for c in rescue_calls),
                target="", origin="pseudo_compat", dispatched=False,
                error_code="pseudo_over_cap",
                note="代执行配额耗尽，待模型走标准通道重发")
            rescue_calls = []
            text_rescue_note = (
                f"[系统提示] 你本轮正文中又出现工具调用样式的标记"
                f"（本回合第 {state.rescue_events + 1} 次），"
                "系统不再代为执行正文形态的调用。"
                "如需执行，请用标准 tool_calls 通道重发同样的调用。")
        if rescue_calls:
            tool_calls = rescue_calls
            for _tc in tool_calls:
                _tc["_src_origin"] = _rescue_origin
            # 全文都是调用块时，空串也是有效的净化结果。
            assistant_content = stripped
            # stdlib logging 只吃位置参数（%s 占位）——本模块 logger 是
            # logging.getLogger 而非 infra.logging.get_logger（structlog），
            # kwargs 写法会 TypeError（2026-10-03 首次点火定案）。
            logger.info(
                "text_tool_calls_rescued count=%s names=%s origin=%s",
                len(rescue_calls), [c["name"] for c in rescue_calls],
                _rescue_origin,
            )
            event_stream.emit(state.work_id, _sid(state), "rescue",
                              # [harness 第 1 步 2026-10-28] 补 names：
                              # "模型把哪个工具写成了正文"是可判断的事实（某种工具
                              # 反复被写进正文 ⇒ 提示词或工具面有问题），只记个数
                              # 问不出来。
                              # [2026-11-19] 补 origin：声明协议通道与原生端点
                              # 伪调用的代执行，在事件面上从此可分。
                              {"count": len(rescue_calls),
                               "names": [str(c.get("name") or "") for c in rescue_calls],
                               "origin": _rescue_origin})
            if _rescue_origin == "pseudo_compat":
                # [正文伪调用抢救 2026-11-09] 捎话挂点：替它执行了伪调用这件事
                # 得让它知道，否则它永远不知道自己写歪了（复发治不了）。
                # declared_text 通道写正文调用是照章办事，不需要纠偏捎话。
                state.rescue_note_pending = True
                state.rescue_events += 1
        elif unresolved:
            # [伪调用重调起 · harness 化 2026-11-12 · 用户令] 「写了形似调用
            # 的文本但没解析出来」只落机械事实（pseudo_call_unparsed），
            # 不再在同轮回注纠正——正文原样交付（藏起来反而遮了真相）。
            # 再调起、纠偏措辞、节流全在 harness_policies：策略收事实后
            # 把卡叫醒让它走真通道重发。新方言层出不穷，"中途解析文本"
            # 这条方向本身撤了——已知方言照执行，未知方言走重调起。
            # [异常正文隔离 2026-11-20] 未解析正文随回复块落会话——打标，
            # 模型历史只见残部＋状态行（card_057 两万字符末跳被次轮原样
            # 抄进历史，此标即断那条链）。
            # [2026-11-20 返修 · runner/Harness 共用一次补救决定] 事实延到
            # 收口点发（hop_unparsed）：本跳被续跳/补救裁定 ⇒ handled=runner
            # 进事件，策略 pseudo_call_reinvoke 见此标记不再二次唤醒。
            hop_anomaly = hop_anomaly or "pseudo_unparsed"
            state.hop_unparsed = {
                "round": state.rounds,
                "chars": len(assistant_content),
                "origin": _rescue_origin,
                "proto_hint": text_tool_rescue
                .detect_protocol_hint(assistant_content),
                "recent": _pseudo_recent(
                    state.work_id, state.card_id),
                # [批五·同一异常身份] 原回合身份＋异常类别进事件——
                # runner/策略共用一次补救决定的关联键（正文片段只排障）。
                "pair_id": state.pair_id,
                "sid": _sid(state),
                "anomaly": hop_anomaly,
                "task_id": str((state.task_contract or {})
                               .get("task_id") or ""),
                "head": (assistant_content or "")[:160]}
        elif stripped != assistant_content:
            # [异常跨度隔离 2026-11-20 · 补书 §二.2] 剥离只命中可证供方
            # 残片（ds_safety 块/CDATAEND>/begin▁of▁sentence——card_007
            # 23:07 实录把 <ds_safety> 判成未知标签对引来重发提醒）。
            # 记正文异常、回复块打标出投影；残片不是调用候选 ⇒
            # **不发** pseudo_call_unparsed（零「请重发调用」提醒）。
            hop_anomaly = hop_anomaly or "provider_fragment"
    elif tool_calls and assistant_content:
        # [同跳混体 2026-11-20 · B1返修] 原生调用在场也要跑一遍正文清洗：
        # 伪调用块（中日文壳/错配壳/仿回执）从此退出模型上下文，且原生
        # 调用只执行一次——正文候选一律丢弃不入执行链，只记候选数对账。
        _m_calls, _m_unresolved, _m_stripped = \
            text_tool_rescue.extract_text_tool_calls(
                assistant_content,
                protocols=model_config.text_protocols(
                    str(result.get("model_used") or "")
                    or (state.model_requested or "")))
        state.hop_text_candidates += len(_m_calls)
        if _m_calls or _m_unresolved:
            hop_anomaly = hop_anomaly or "pseudo_mixed"
            state.hop_unparsed = {
                "round": state.rounds,
                "chars": len(assistant_content),
                "origin": _rescue_origin,
                "candidates": len(_m_calls),
                "proto_hint": text_tool_rescue
                .detect_protocol_hint(assistant_content),
                "recent": _pseudo_recent(
                    state.work_id, state.card_id),
                "pair_id": state.pair_id,
                "sid": _sid(state),
                "anomaly": hop_anomaly,
                "task_id": str((state.task_contract or {})
                               .get("task_id") or ""),
                "head": (assistant_content or "")[:160]}
        elif _m_stripped != assistant_content:
            # [异常跨度隔离 2026-11-20 · 补书 §二.2] 同跳真调用照常执行、
            # 回执完整保留；正文里仅剩的可证供方残片（card_648 23:11 的
            # begin▁of▁sentence）出投影——记正文异常即可，**不发**
            # pseudo_call_unparsed：没有未派发调用要它重发（零提醒）。
            hop_anomaly = hop_anomaly or "provider_fragment"

    # [调用来源与真实回执 2026-11-19 · 原生协议失败] 到这里仍然零可执行
    # 调用、且网关侧有「端点声称调了工具 / 分片损坏」的证据 ⇒ 「模型调了、
    # 网关没拿到有效调用」——与「模型根本没调」（finish=stop）分开计数、
    # 分开处置：坏项无副作用，回注协议事实让模型按当前协议重发未派发的项。
    # 判据限定原生端点（declared_text 的正文调用是其标准通道，rescue 已接）。
    if not tool_calls:
        _pm = result.get("protocol_meta") or {}
        _proto_fail = (
            int(_pm.get("native_dropped") or 0) > 0
            or int(_pm.get("native_truncated") or 0) > 0
            or (bool(_pm.get("native_finish_tool_calls"))
                and not bool(_pm.get("declared_text")))
        )
        if _proto_fail:
            _diag = {
                "round": state.rounds,
                "endpoint_protocol": str(_pm.get("endpoint_protocol") or ""),
                "finish_reason": str(_pm.get("finish_reason") or ""),
                "native_dropped": int(_pm.get("native_dropped") or 0),
                "native_truncated": int(_pm.get("native_truncated") or 0),
                "truncated_tool_call": result.get("truncated_tool_call") or None,
            }
            event_stream.emit(state.work_id, _sid(state),
                              "native_protocol_error", _diag)
            tool_receipt.ledger_tool(
                state.work_id, state.card_id, state.pair_id, state.rounds,
                tool=str((result.get("truncated_tool_call") or {}).get("name") or "?"),
                target="", origin="native", dispatched=False,
                error_code="native_protocol",
                note=json.dumps(_diag, ensure_ascii=False)[:400])
            text_rescue_note = (
                "[系统提示] 上一次的调用在协议层失败、没有任何调用被执行"
                f"（端点协议 {_pm.get('endpoint_protocol')}、停止原因 "
                f"{_pm.get('finish_reason')}、缺名分片 "
                f"{_pm.get('native_dropped')} 个、参数截断 "
                f"{_pm.get('native_truncated')} 个）。"
                "请按当前通道的标准调用格式重新发送同样的调用。")

    # [B3 请求对账 2026-11-20] 本跳请求级事实载荷——请求座/实际座/
    # 端点协议/finish/候选（原生＋正文）vs 入执行器数 vs 拒绝数；真实
    # 派发数由调用循环在 outcome 点按本 rounds 的 tool_events 补齐后发。
    _pm = result.get("protocol_meta") or {}
    _transport = state.hop_transport
    _entry_key = str(result.get("model_used") or _transport.get("model_used_entry") or "")
    _ep_facts = ({**tool_receipt.endpoint_facts(_entry_key), **_transport}
                 if _entry_key else _transport)
    state.hop_accounting = {
        "pair_id": state.pair_id, "round": state.rounds,
        "requested_model": str(state.model_requested or ""),
        "model_used_entry": _entry_key,
        "vendor": _ep_facts.get("vendor") or "",
        "api_base": _ep_facts.get("api_base") or "",
        "endpoint_model": _ep_facts.get("endpoint_model") or "",
        "request_model": _transport.get("request_model") or "",
        # [B3 返修] 端点回包声明的模型名——与请求模型名/配置线路名分账。
        "response_model": str(_pm.get("response_model") or _transport.get("response_model") or ""),
        "model_used": str(_transport.get("request_model") or (state.model_used if terminal_received else "") or ""),
        "trace_id": str(result.get("trace_id") or _transport.get("trace_id") or ""),
        "transport": _transport.get("transport") or "",
        "endpoint_protocol": str(_transport.get("endpoint_protocol") or _pm.get("endpoint_protocol") or ""),
        "declared_text": (_transport.get("endpoint_protocol") == "declared_text"
                          if _transport else bool(_pm.get("declared_text"))),
        "finish_reason": str(_pm.get("finish_reason")
                             or result.get("finish_reason") or ""),
        "native_calls": int(_pm.get("native_calls") or 0),
        "native_dropped": int(_pm.get("native_dropped") or 0),
        "native_truncated": int(_pm.get("native_truncated") or 0),
        "native_finish_tool_calls": bool(_pm.get("native_finish_tool_calls")),
        # [B3返修] 候选按来源分账：native＝原生 tool_calls 分片；
        # text＝正文解析（流终帧 pseudo_compat/declared_text＋rescue）。
        "native_candidates": _native_candidates,
        "text_candidates": state.hop_text_candidates,
        "candidates": _native_candidates + state.hop_text_candidates,
        "admitted": len(tool_calls),
        "refused": _hop_refused,
        "latency_ms": result.get("latency_ms"),
        "tokens_used": result.get("tokens_used"),
        "anomaly": hop_anomaly or "",
    }

    # [假结果治理 2026-11-20 · 复现修复] 仿回执只在**逐跳模型原始正文**
    # （assistant_content）上判——finish 的 clean_text 收口时已拼入宿主
    # 生成的真实系统结果行（〔系统结果〕本轮未执行/执行 N 项），在拼接
    # 文本上判会把真回执误标 pseudo_receipt、还给已派发回合加上
    # 「未派发执行」假状态。本跳有无 tool_calls 都查：同跳带了真调用
    # 也不能给模型自写回执背书。
    _pr = text_tool_rescue.detect_pseudo_receipt(raw_assistant_content)
    if _pr:
        hop_anomaly = hop_anomaly or "pseudo_receipt"
        event_stream.emit(state.work_id, _sid(state), "pseudo_receipt",
                          {"pair_id": state.pair_id,
                           "round": state.rounds,
                           "count": _pr})
    assistant_context = assistant_content
    if hop_anomaly:
        state.anomaly_quarantine = state.anomaly_quarantine or hop_anomaly
        assistant_content = raw_assistant_content
        assistant_context = text_tool_rescue.quarantine_view(
            raw_assistant_content, has_dispatch=bool(tool_calls),
            fragment=(state.anomaly_quarantine == "provider_fragment"))

    # [模型过程数据 2026-10-12] 判断事件（新增）：**首轮机械推断**——tool_calls
    # 含 create_plan → plan，否则 direct。只读 tool_calls 事实（含抢救并入的
    # 调用），不解析模型文本、不加独立判定调用；首轮之后不再重发。
    if state.rounds == 1 and thinkchain_enabled():   # [纯单模型 2026-10-17]
        event_stream.emit(state.work_id, _sid(state), "judgment", {
            "verdict": "plan" if any(tc.get("name") == "create_plan"
                                     for tc in tool_calls) else "direct",
        })

    # [迭代二 S4] 催化挂点一（主循环路径）：本卡有活板且本步未推进、本轮
    # **空手**（两条件缺一不可——判据＝tool_calls 事实，防嘴上伪造；带工具
    # 轮照常执行）→ 按附录 C 步执行提示词催促交作业续跳，不设上限。两道
    # 天然刹车：每圈入口 check_control（人停即刹）＋用户新消息＝新回合。
    # 空手正文不入槽（同 :316 抢救先例——催促文自带本步要求原文，信息自足）。
    # [venue] 板催化也不对席位回合发——板是场的工作面，催席位干活越界。
    if (not state.act_as and thinkchain_enabled() and not tool_calls
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

    # [票落＝回合止 2026-11-15] 限调轮里模型仍吐调用 ⇒ 不执行——票已落账，
    # 回合到此为止（它的正文照常落盘当收尾）。
    if tool_calls and state.seat_voted_end:
        state.final_reply = assistant_content
        await _land_round_reply(state, assistant_content)
        return StepOutcome("deliver", "seat_voted")

    if not tool_calls:
        # [5715] 插话优先于收口：模型这一轮没调工具、正文已定（**正要收口**），
        # 但用户在模型跑的时候插了话 ⇒ 不收口，把插话插进轮槽、继续下一轮。
        # 否则那句话会被丢掉（回合收口即清扫残留）——用户说了话，却没人听见。
        if _steer_items := await _apply_steer(state, into_slot=True):
            # [插话如实报名 2026-11-10] 队列里不一定是用户——卡间消息、
            # 系统提醒忙时都走这条通道（mark 在条目首）。提示照实报名，
            # 别让模型去找一条不存在的「用户插话」（card_061 实踩：
            # 三轮系统超限提醒被报成用户插话，模型烧两轮做无头侦察）。
            _marks = sorted({m.group(0) for t in _steer_items
                             for m in [re.match(
                                 r"^(?:用户插话|卡间消息|系统消息)：", t)]
                             if m}) or ["消息："]
            state.user_prompt = ("你在回答时有新消息插进来（"
                                 + "、".join(_marks)
                                 + "）。先读那条，再继续。")
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
                "role": "assistant", "content": assistant_context,
            })
            state.user_prompt = text_rescue_note
            return StepOutcome("continue", "text_rescue")
        # [定向补救续跳 2026-11-19 · 2026-11-20 二次返修 · B2 统一决定点]
        # 补救看**绑定的动作有没有派发**（不看本轮是否零调用、不看正文措辞）：
        #   · op 专用回合——绑定动作（专用工具）从未派发 ⇒ 定向补一次，
        #     即使本轮派发过别的调用（点更新现状却只 read_card 的复核实录：
        #     零调用判据会漏掉这种）；已派发或结果待确认 ⇒ 先核查不重发；
        #   · directive 指令回合——require_report 绑定的汇报从未送出
        #     （send_state=="not_sent"）⇒ 同样补一次（007 型零调用 stop
        #     复核实录）；failed/unverified 不擅自重做副作用，交付面由
        #     回执闸如实标注待核实；
        #   · 汇报/研判类卡间消息回合——不绑定，零调用研判合法。
        # 去重两道：内存标志（同回合）＋ intervention_store (fire_key,
        # target) 唯一占位（跨重启/重复事件同一事实只补一次）——占位登记
        # 在发额外请求之前；登记失败沿台账记待核实不阻断。
        _bound = tool_receipt.bound_action(state)
        if (_bound is not None
                and not getattr(state, "remediation_done", False)
                and not steer.pending_steer(state.work_id, state.card_id)
                and not control.pending_stop(state.work_id, state.card_id)):
            _kind = str(_bound.get("kind") or "")
            _verdict = tool_receipt.receipt_verdict(state)
            _missing = (
                _verdict.get("op_missing") or _verdict.get("task_missing")
                or "not_sent" in (_verdict.get("reports") or {}).values())
            if _missing:
                _sig = (str(_bound.get("op") or "") if _kind == "op"
                        else (",".join(_bound.get("targets") or [])
                              if _kind == "directive"
                              # 任务版本进签名：同任务换 rev 是新事实，
                              # 不许被旧占位的 fire_key 去重吞掉。
                              else f"{_bound.get('task_id')}@"
                                   f"{_bound.get('rev')}|"
                                   f"{_bound.get('action')}@"
                                   f"{_bound.get('target')}"))
                _fire_key = (f"remediation|{state.card_id}|{state.pair_id}"
                             f"|{_kind}:{_sig}")
                _aid, _fresh = intervention_store.store().record_action(
                    state.work_id,
                    policy_id="turn_remediation",
                    scope_key=str(state.card_id),
                    fire_key=_fire_key,
                    target=str(state.card_id),
                    mode="remediation",
                    wake=False,
                    message=json.dumps({"kind": _kind, "bound": _bound,
                                        "rounds": state.rounds},
                                       ensure_ascii=False)[:800])
                if not _aid:
                    # [2026-11-20 返修 · 用户复验] 占位落不下 ⇒ 补救决定记
                    # 「待核实」就地收口，**不发第二次模型请求**——占位是
                    # 唯一决定权的凭据，缺席时补跳等于无凭据重发（同一事实
                    # 可能已被别处裁定）。交付面由回执闸如实标注欠项。
                    state.remediation_done = True
                    state.remediation_adjudicated = True
                    event_stream.emit(state.work_id, _sid(state),
                                      "remediation_unverified",
                                      {"bound": _bound,
                                       "rounds": state.rounds})
                    tool_receipt.ledger({
                        "kind": "remediation_unverified",
                        "work_id": state.work_id, "card_id": state.card_id,
                        "pair_id": state.pair_id, "rounds": state.rounds,
                        "bound": _bound})
                elif not _fresh:
                    # 同一事实已补救过（重启/重复事件回放）——只记事实不重发
                    state.remediation_done = True
                    state.remediation_adjudicated = True
                    event_stream.emit(state.work_id, _sid(state),
                                      "remediation_deduped",
                                      {"bound": _bound, "rounds": state.rounds,
                                       "action_id": _aid})
                else:
                    state.remediation_done = True
                    # [2026-11-20 返修·请求边界] 占位**保持 pending**——
                    # 「已发」由下一跳模型请求的实际边界写回（sent），
                    # 回包到达才 delivered（见 _remediation_*_boundary）。
                    # 决定点预支 delivered 是旧病：中断在第二次请求前，
                    # 动作账已 delivered 而模型只收到首轮请求。
                    state.remediation_action_id = _aid
                    state.remediation_bound = dict(_bound or {})
                    _engage_slot(state)
                    state.slot.append_plain({
                        "role": "assistant", "content": assistant_context,
                    })
                    state.user_prompt = tool_receipt.remediation_prompt(
                        state, _bound, _verdict)
                    return StepOutcome("continue", "remediation")
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
        # [停止即闭合 2026-10-23] 停止信号可能在**本轮模型调用期间**到达并被流消费
        # （`_stop_raced` 让流立刻收口、保住已产出的字）。这条无工具轮的出口原先
        # 不再看控制面 ⇒ 停止被吞成 model_done，与 T124「人停＝human_stop 闭合」不符。
        # 这里补一次出口挂点（消费即清，与 1104 行同口径）。
        _verdict = check_control(state)
        if _verdict == control.STOP:
            state.final_reply = state.final_reply if state.final_reply is not None else ""
            return StepOutcome("deliver", "human_stop")
        if _verdict == control.DEFER_HUMAN:
            return StepOutcome("deliver", "policy_stop")
        return StepOutcome("deliver", "chain_done" if state.chain_completed else "direct")

    # —— 有工具调用：本轮执行工具，续跳 ——
    # [worker 完工未报兜底] 本回合调用计数——card_tools 完工回执判据用。
    state.turn_tool_call_count += len(tool_calls)
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
        "content": assistant_context,
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
    # [多轮思考回传 2026-10-23] 有的端点强制要求：assistant 消息带 tool_calls 时，
    # 必须同时带回它上一轮给出的 reasoning_content，否则 400
    # "Missing required parameter: 'messages[N].reasoning_content'"。
    # 判据取 models.yaml 的显式开关（model_config.reasoning_echo_fill）——
    # 改前是 `"deepseek" in model_used` 的字符串包含判断，把 commandcode 的
    # reflection/beam:free 漏在外面：它的思考字段叫 reasoning（不叫
    # reasoning_content，适配器当时也不认），于是既收不到思考、也不回传，
    # 带工具面的第二轮起必 400、重试 3 次后整轮判死（用户实测「跑不通」）。
    # ★ 带不带＋带什么由 gateway 统一判据（reasoning_echo_value）：有真思考
    #   原样回传；"real" 档（kimi 保留式思考）没真思考就**不带字段**——塞空串
    #   等于谎报「该轮无思考」（它恒在推理，官方文档口径）；DeepSeek 系空串
    #   （官方生产口径），beam 系必须非空（空串照样 400）。
    _att, _rv = model_config.reasoning_echo_value(
        state.model_used or "", _entry_key,
        "" if hop_anomaly else state.turn_reasoning)
    if _att and state.turn_reasoning and not hop_anomaly:
        _asst_msg["reasoning_content"] = _rv
        _asst_msg["_reasoning_model"] = str(state.model_used or "")
    state.slot.begin_round(_asst_msg)
    # [T14-1-2] 只读采集：带工具轮的 assistant_content（含 L459 抢救处理后
    # 的正文）同步留一份进交付采集列表。不改 begin_round 入参、不额外入槽
    # （T11 轮槽消息条数与配对不变量不破坏，RD-09）；不限长不折叠不截断。
    if assistant_content:
        state.analysis_parts.append(assistant_content)

    # [5715 · 修2 2026-11-05] 本轮 assistant_text 在**工具循环之前**落盘：
    # 节点位置＝模型产出处（思考→正文→工具调用），与 live 路径（流节点先于
    # 工具创建）同序。原先在循环尾落 ⇒ 无思考的轮（没有 reasoning 事件抢先
    # 建 stream:{run}:{round} 节点）把 assistant 节点钉到工具之后 ⇒ 刷新
    # 前后两套顺序、工具轮节点倒挂。每轮一条的语义不变（disk 与 live 同构
    # 的前提；正文为空也落 ⇒ 思考-only 的一步有归处）。
    await _land_round_reply(state, assistant_content)

    # 收集卡片 meta 用于权限判定
    cards_meta = card_tools._collect_card_meta(state.work_id)
    # [venue] 席位在评审场内获得全树**读**权（[2026-10-29 用户令]「卡面才是唯一
    # 事实源」）：瞬时快照上给席位 meta 打 _review_seat，写仍走原规则表。
    _seat_id = str(state.act_as or "")
    if _seat_id and _seat_id != state.card_id:
        _seat_meta = dict(cards_meta.get(_seat_id) or {})
        if not _seat_meta:
            # [venue] 职能不进 cards 表（function_store）⇒ 快照里没有它，
            # 执行闸会把席位当「认不出的卡」全拒。合成卡面与 build_context
            # 工具面同源（profile 实挂优先，空则按卡型现算）。
            _seat_meta = _synth_seat_meta(state.work_id, _seat_id, cards_meta)
        _seat_meta["_review_seat"] = True
        cards_meta[_seat_id] = _seat_meta

    # [工具结果落会话 2026-10-17] 本工具轮的会话块 B（工具结果）在内存收齐，
    # 循环后与块 A（assistant+tool_calls）一次性落盘（§0.1 疑问3：原子落盘，
    # 杜绝"A 已落、B 未落"的中间态——N1 红线：不得出现孤立 tool 块）。
    _round_tool_blocks: list[dict[str, Any]] = []

    try:
        for i, tc in enumerate(tool_calls):
            name = tc.get("name", "")
            args = tc.get("arguments", {}) or {}
            # [调用来源与真实回执 2026-11-19] 本次调用的来源（native /
            # declared_text / pseudo_compat），进事件与台账——收到请求、
            # 实际派发、执行结果共用同一标记，对账时三段可链。
            origin = str(tc.pop("_src_origin", "") or "native")
            state.origin_counts[origin] = state.origin_counts.get(origin, 0) + 1
            _call_id = f"call_{_run_tag}_{state.rounds}_{i}"
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
            event_stream.emit(state.work_id, _sid(state), "tool_call",
                              {"tool": name, "target": target_id, "step_id": _step_id,
                               # [harness 第 1 步 2026-10-28] 补 card_id 零件：
                               # `target` 是模糊字段（各工具含义不同：卡号/章节/查询词），
                               # 想按"对哪张卡动手"组合条件就必须有确定的 card_id。
                               "card_id": str(args.get("card_id") or ""),
                               # [调用来源 2026-11-19] 来源标记进事件。
                               "origin": origin,
                               "arguments": args})
            # [调用来源 2026-11-19] 派发台账：实际派发记录（执行结果在下一行追加）。
            tool_receipt.ledger_tool(
                state.work_id, state.card_id, state.pair_id, state.rounds,
                tool=name, target=str(target_id or ""), origin=origin,
                dispatched=True, call_id=_call_id)
            # [B3 请求对账返修] 进入执行器前的派发尝试计数——execute_tool
            # 抛异常/回合被取消时 attempts > executed 的差值是
            # 「派发出去但没跑完」的实证，不再被 completed 数掩盖。
            state.hop_dispatch_attempts += 1
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
                state.work_id, str(state.act_as or "") or state.card_id,
                name, args, cards_meta,
                # [执行层权限闸 2026-10-25] 工具面只是「看不看得见」，不是执行
                # 授权：模型硬调一个没下发给它的工具（提示词里写着 bash/python），
                # 原先会直接跑到 handler 真执行。此处置 True ⇒ 无开关即拒。
                # [venue] 席位回合里工具眼中的"当前卡"＝席位（approve 的
                # 投票人就是它）。
                perm_gate=True,
                **({"task_contract": state.task_contract} if state.task_contract else {}))
            # [T-0010 §1.5] 成败读信封 ok 字段（改前是「结果里没有 error 键即算成功」）
            ok = tool_result.get("ok") is True
            _data = tool_result.get("data") or {}
            _ev: dict[str, Any] = {"tool": name, "card_id": target_id, "ok": ok,
                                   # [调用来源 2026-11-19] 执行记录带来源——
                                   # 回执闸与四组对照按它区分「真调」与「代执行」。
                                   "origin": origin, "round": state.rounds}
            _err_code = str(((tool_result.get("error") or {}).get("code")
                             if isinstance(tool_result.get("error"), dict)
                             else "") or "")
            if not ok:
                # [系统结果 2026-11-19] 失败原因随执行记录走——交付面系统
                # 结果的「显示失败原因及目标」呈现事实来源。
                _ev["error_code"] = _err_code or "失败"
            # [建卡工具化 2026-10-08] create_card 成功时把真身卡号/卡名/父带进
            # tool_events——交付段据此收拢工具通道建卡结果
            # [T-0010 §1] 成功载荷整体在 data 内，取值改走 data
            if name == "create_card" and ok:
                _ev["card_id"] = _data.get("card_id", "")
                _ev["title"] = _data.get("title", "")
                _ev["parent"] = _data.get("parent", "")
            if ok and name in ("direct", "sync", "report_up"):
                # [系统结果 2026-11-19 · 2026-11-20 二次返修] 发信回执摘要按
                # **实际收件人逐条**记录（核对与呈现的唯一事实面）：
                #   · direct 布尔形态：delivered=true/false + 顶层 msg_id；
                #   · sync/report_up 列表形态：delivered/pending 各为
                #     {to, msg_id} 列表（report_up 收件人=父卡/创建者，
                #     由 handler 决定——参数里没有）。
                # state：delivered=运行态已受理；pending=落账未受理。
                # 落信与受理分开、每个收件人带自己的记录锚（msg_id）——
                # 回执核对与系统结果呈现都按它逐条判。
                _recips: list[dict[str, Any]] = []
                _dl = _data.get("delivered")
                _pd = _data.get("pending") or []
                if isinstance(_dl, list):
                    # [二次返修] sync 落账不唤醒（工具返回 delivered 列表=
                    # 落信成功，无投递概念）⇒ state=synced；direct/report_up
                    # 的 delivered=运行态已受理。
                    _st = "synced" if name == "sync" else "delivered"
                    for _r in _dl:
                        if isinstance(_r, dict):
                            _recips.append({"to": str(_r.get("to") or ""),
                                            "msg_id": str(_r.get("msg_id") or ""),
                                            "state": _st})
                else:
                    _recips.append({"to": str(_data.get("card_id") or ""),
                                    "msg_id": str(_data.get("msg_id") or ""),
                                    "state": "delivered" if _dl else "pending"})
                if isinstance(_pd, list):
                    for _r in _pd:
                        if isinstance(_r, dict):
                            _recips.append({"to": str(_r.get("to") or ""),
                                            "msg_id": str(_r.get("msg_id") or ""),
                                            "state": "pending"})
                _ev["send"] = {"recipients": _recips}
            state.tool_events.append(_ev)
            # [思维链真实落地 2026-10-11] 板子事件 emit（对话流可见拆解上板/步进/收口）：
            # create_plan 成功→plan_created；step_complete 成功→step_advanced
            # （all_done=True 时前端渲染收口行）。
            # [模型过程数据 2026-10-12] plan_created 补完整 steps 数组（建板成功后
            # 板上已是全字段步定义，emit 时就地取，不解析模型文本）。
            if ok and name == "create_plan":
                _bsnap = thinkchain_board.snapshot_sync(state.work_id, state.card_id) or {}
                _bsteps = _bsnap.get("steps") or []
                event_stream.emit(state.work_id, _sid(state), "plan_created", {
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
                event_stream.emit(state.work_id, _sid(state), "step_advanced", {
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
            event_stream.emit(state.work_id, _sid(state), "tool_result",
                              {"tool": name, "target": target_id, "ok": ok,
                               "step_id": _step_id, "origin": origin,
                               "result": _struct_result})
            # [调用来源 2026-11-19] 台账补执行结果行（同一 call_id 链到派发行）。
            tool_receipt.ledger_tool(
                state.work_id, state.card_id, state.pair_id, state.rounds,
                tool=name, target=str(target_id or ""), origin=origin,
                dispatched=True, ok=ok, call_id=_call_id,
                error_code=_err_code)
            # [T11-T6] tool 结果经 append_tool_result 入槽（归入当前轮）
            # [T-0010 §1.6] 回灌文本走单一投影函数（不各自 json.dumps）——
            # 否则失败信封的 {"code","message"} 会被当数据喂进模型上下文
            _tool_call_id = f"call_{_run_tag}_{state.rounds}_{i}"
            _rendered_result = card_tools.render_tool_result(tool_result)
            task_proof = None
            _ev_suffix = ""
            if state.task_contract:
                from . import task_evidence
                task_proof = task_evidence.proof_for_result(
                    state.task_contract, name, args, tool_result, origin)
                if task_proof:
                    _ev_suffix = (f"\n〔任务证据〕{task_proof['criterion']}"
                                  f" rc:{_tool_call_id}")
                    _rendered_result += _ev_suffix
            _ev_prefix = ""
            if state.rescue_note_pending:
                # [正文伪调用抢救 2026-11-09] 捎话：伪调用被替执行时
                # 把「你写歪了、系统替你执行了」捎给它——不然它永远不知道，
                # 下一轮照写（Cline 式纠正，点到即止）。
                _ev_prefix = (
                    "[系统提示] 你上一轮的调用写在正文里（形似调用的文本），"
                    "系统已按参数原样替你执行——结果如下。今后请走 tool_calls "
                    "通道真发调用；正文伪块不执行、等不到回执。\n\n")
                _rendered_result = _ev_prefix + _rendered_result
                state.rescue_note_pending = False
            # [批四 · 工具证据] 试点工具的成功结果附结构化证据入槽——
            # 宿主侧元数据（不落进消息体）；wire 投影时按它造可回读投影。
            _ev = None
            if ok and name in ("search_files", "read_file") \
                    and isinstance(_data, dict):
                _ev = {"tool": name, "data": _data,
                       "args": dict(args or {}),
                       "prefix": _ev_prefix, "suffix": _ev_suffix,
                       # [返修] 整文件版本指纹（信封层，不入 data）——
                       # 引用键凭它判换版，区间外改动也不再误判同版本。
                       "file_sha256": str(
                           tool_result.get("_file_sha256") or "")}
            state.slot.append_tool_result({
                "role": "tool",
                "tool_call_id": _tool_call_id,
                "content": _rendered_result,
            }, evidence=_ev)
            # [工具结果落会话 2026-10-17] 会话块 B（工具结果）：在内存收齐，
            # 循环后与块 A 一次性落盘。tool_call_id 与 A 的调用一一配对。
            # [调用来源 2026-11-19] 块带 **status 结构化状态**（ok/error）——
            # 历史存根投影（context_regions._tool_stub_wire）按它生成
            # 成功/失败/未知三态回执，不再从渲染文本猜。
            _round_tool_blocks.append({
                "actor": "tool",
                "tool_call_id": _tool_call_id,
                "content": _rendered_result,
                "status": tool_receipt.tool_status_of(tool_result),
                "origin": origin,
                **({"task_proof": task_proof} if task_proof else {}),
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
                # [venue] 场回合 append_user=False ⇒ 会话里没有 human 块，
                # pair 兜底会落成 'unknown'；回合 uuid 显式带上（前端按它归轮）。
                await session_facade.append_many(state.work_id, _sid(state), [
                    {"actor": "assistant", "content": "", "pair_id": state.pair_id,
                     "tool_calls": _asst_msg["tool_calls"]},
                    *[{"pair_id": state.pair_id, **b} for b in _round_tool_blocks],
                ])
            except Exception as _exc:  # noqa: BLE001
                logger.error("[工具块落盘] 失败 card=%s: %s", state.card_id, _exc)
    # [5715] 本轮的 assistant_text 事件已在工具循环之前落盘（修2：节点位置
    # ＝模型产出处）。收口那条（clean_text 合并全文）不落事件（会话块照落
    # ⇒ 模型上下文零变化），免得与各轮正文重复。
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
    event_stream.emit(state.work_id, _sid(state), "seats_enter",
                      {"tool_results": len(state.tool_full_texts),
                       "steps": len(state.chain_steps_record)})
    # [三席身份注入 2026-10-14] 三席身份提示词——走注入（role=assistant 自述），
    # 随三席调用经 turn_tail 下发；三席 system（共享上下文）与 user 位质询文都不动。
    # 单列事件一类（不复用 context_preamble——语义与前端标签都不同）。
    seat_identity = simple_service.build_seat_identity_message()
    event_stream.emit(state.work_id, _sid(state), "context_seat_identity",
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
    # [2026-11-19 性能批次三] 留痕改为**真实**席位请求（带身份注入的那份）——
    # 改前 state.vp_system_prompt 存的是回合开场的无身份渲染，不是席位实际收到的。
    state.vp_system_prompt = seat_request["system_prompt"]
    ok_metas, state.viewpoints_meta = await simple_service.run_viewpoints(
        seat_request["system_prompt"],
        seat_request["user_prompt"],
        seat_request["prior_messages"],
        on_progress=lambda s, d, _w=state.work_id, _c=_sid(state):
            progress.set_stage(_w, _c, s, d),
        on_seat=lambda kind, meta, _w=state.work_id, _c=_sid(state):
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
    progress.set_stage(state.work_id, _sid(state), "synthesize", "三席观点统合中")
    logger.info("[提交轮] 三席质询进场 步成果=%d条 工具结果=%d条 成功席=%d card=%s",
                len(state.chain_steps_record), len(state.tool_full_texts),
                len(ok_metas), state.card_id)
    synth_input = simple_service.build_viewpoint_prompt(state.content, ok_metas)
    event_stream.emit(state.work_id, _sid(state), "round_start",
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
    # [模型中断续跑 2026-10-23 · 修] 统合官这一次**也必须续跑**：
    # 它是产出最终答案的那一次调用，原来漏在包装外 ⇒ 一断连整轮就死，
    # 前面所有工具轮白做。用户实测「自动重试没生效」即此。
    result, state.model_used = await _call_model_resumable(
        state,
        system_prompt=synth_request["system_prompt"],
        user_prompt=synth_request["user_prompt"],
        prior_messages=synth_request["prior_messages"], tools=state.tools,
        model_name=_synth_seat,
        fallbacks=model_config.layer_fallbacks(
            _synth_seat, work_id=state.work_id, card_id=state.card_id),
        turn_tail=synth_request["turn_tail"],
        turn_once=synth_request["turn_once"],
        # [F22 · 2026-10-31 审计补口] 统合官观测因果链
        work_id=state.work_id, card_id=state.card_id,
        caller=f"turn:{state.pair_id}:synthesize",
    )
    state.turn_reasoning = result.get("reasoning_content") or ""
    tool_calls = _norm_tool_calls(result.get("tool_calls") or [])
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
                f"  成果: {(s.get('result') or '').strip()}")
        conclusion = (state.final_reply or "").strip()
        status_text = (
            f"【思维链收口】\n"
            f"计划名: {state.chain_plan_name}\n"
            f"终态: 已收口（提交轮三席统合）\n"
            f"结论: {conclusion if conclusion else '（统合稿为空）'}\n"
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
    # [按回执交付 2026-11-19 · 2026-11-20 返修] 绑定动作的回合收口核对：
    # 用本回合**执行记录**（tool_events，模型正文不读）按**动作+目标**匹配
    # 成功回执——要求发信时，改无关卡的成功回执满足不了任务核对（目标不符
    # 同理）；正文措辞（引用「已完成」/否定表述）不再参与任何判定（词表已
    # 退役）。无匹配 ⇒ 交付文本追加系统回执行（流式界面/最终回复/刷新后
    # 历史用同一 verdict 数据源；会话块另带 receipt 结构化字段）。
    # 已派发但结果待确认（send 摘要 delivered=false）时由系统结果的
    # 「已写入，投递未确认（msg_id …）」呈现原记录，不判未完成、不重发。
    # TOOL_RECEIPT_GATE=0 可关（四组对照 A/B 组共用基线）。
    state.receipt = tool_receipt.receipt_verdict(state)
    if (tool_receipt.receipt_gate_enabled()
            and state.receipt.get("bound") is not None
            and not state.receipt.get("satisfied")):
        receipt_text = tool_receipt.receipt_line(state.receipt)
        reports = state.receipt.get("reports", {})
        receipt_data = {
            "bound": state.receipt.get("bound"),
            "detail": state.receipt.get("detail"),
            "dispatched": state.receipt.get("dispatched"),
            "ok": state.receipt.get("ok"),
            "reports": reports,
            "text": receipt_text.strip(),
            "severity": "warn" if "unverified" in reports.values() else "fail",
            "rounds": state.rounds,
        }
        event_stream.emit(state.work_id, _sid(state), "action_not_executed", receipt_data)
        tool_receipt.ledger({
            **receipt_data,
            "kind": "action_not_executed",
            "work_id": state.work_id, "card_id": state.card_id,
            "pair_id": state.pair_id,
        })
        delivered = (delivered or "") + receipt_text
        state.branch = state.branch + "+receipt_gate"
    # [系统结果 2026-11-19 · 2026-11-20 二次返修] 交付面追加由**执行事实**
    # 生成的系统结果行：汇报唤醒回合与**指令回合**（含发信逐收件人状态——
    # 落账未受理/缺锚待核实）都显示；普通 human 聊天回合不显示。模型正文
    # 作为研判或草稿展示，正文写「三条已发」改变不了界面状态；零调用研判
    # 正常交付（系统结果＝「本轮未执行工具」）。流式/最终回复/刷新后历史
    # 同一来源：结构化 state.system_result 落会话块 receipt 字段（finish）。
    if (tool_receipt.is_report_wake(state)
            or str(getattr(state, "source_kind", "") or "") == "directive"
            or state.receipt.get("reports")
            or str(getattr(state, "op", "") or "")):
        state.system_result = tool_receipt.system_result_of(state)
        delivered = (delivered or "") + tool_receipt.result_line(
            state.system_result)
        state.branch = state.branch + "+system_result"
    logger.info(
        "[T14] 分析交付拼接 branch=%s 分析段数=%s 拼接后字节=%s 末轮字节=%s",
        state.branch, len(state.analysis_parts),
        len(delivered.encode("utf-8")),
        len((state.final_reply or "").encode("utf-8")),
    )
    # [信息流 Task3] 交付拼接埋点（deliver 与 POST 一致性锚，V4）
    event_stream.emit(state.work_id, _sid(state), "deliver",
                      {"branch": state.branch,
                       "bytes": len(delivered.encode("utf-8")),
                       # [系统结果 2026-11-19] 交付事件带执行事实摘要——
                       # 流式面与最终回复同源核对。
                       "system_result": (state.system_result or {}).get("text") or ""})
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
    progress.set_stage(state.work_id, _sid(state), "cards", "建卡")
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
    event_stream.emit(state.work_id, _sid(state), "proposal", {
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
    _reply_blk: dict[str, Any] = {
        "role": "assistant", "content": state.clean_text,
        "model": state.model_used, "pair_id": state.pair_id,
        "end": state.end_reason or "model_done",
    }
    # [异常正文隔离 2026-11-20 · 方案 §3] 本回合检出"调用写进正文未派发"
    # ⇒ 回复块带结构化标记：原文原样落会话（界面/原始记录照查），供模型
    # 使用的内容换成 quarantine_view（剥掉坏片段的残部＋宿主一行状态）。
    # 历史投影/缓存板/凝练同口径消费 ctx_content——异常不进后续模型请求。
    if state.anomaly_quarantine:
        _reply_blk["quarantine"] = state.anomaly_quarantine
        _reply_blk["ctx_content"] = text_tool_rescue.quarantine_view(
            state.clean_text, has_dispatch=bool(state.tool_events),
            fragment=(state.anomaly_quarantine == "provider_fragment"))
    # [保留式思考跨轮回传 2026-11-10] kimi-k3.1-pro 文档口径：多轮对话必须把
    # API 返回的完整 assistant message（含 reasoning_content）原样回传。
    # 会话块是次回合历史的唯一载体——不落这里，投影层（context_regions:857）
    # 就没东西可保，跨回合思考链全断。只落**真思考**（空串/占位是发送期兜底，
    # 不进档案）；登记过回传需求的端点才带（kind="" 的不引字段）。
    if state.turn_reasoning:
        _att, _rv = model_config.reasoning_echo_value(
            state.model_used or "", "",
            state.turn_reasoning)
        if _att:
            _reply_blk["reasoning_content"] = _rv
    # [venue] 席位回合的回复块带 from：页面/审计能认出"这句话是谁说的"，
    # 普通回合不带（行为逐字不变）。persona 会话同样带（人格归因）。
    if state.act_as or state.persona:
        _reply_blk["from"] = str(state.act_as or "") or str(state.persona or "")
    # [评审按阶段缩量·发言回读锚 2026-11-20 · 任务书 §二.2] 评审席位发言
    # 落块时把**实际送给席位的材料身份**锚在块上（评审卡/批次/席位/步骤/
    # 稿次/原文指纹——取装配时的 material 快照，不是落块时的最新稿；
    # 中途改稿的版本差异由锚如实保留，供补票选材核「这条意见属于哪一版」）。
    _src = state.source or {}
    if str(_src.get("kind") or "") == "review_step":
        _mat = dict(_src.get("material") or {})
        _reply_blk["review_anchor"] = {
            "review_id": str(_mat.get("review_id") or state.card_id),
            "batch": str(_src.get("batch") or _mat.get("batch") or ""),
            "seat": str(state.act_as or _mat.get("seat") or ""),
            "step": str(_src.get("step") or _mat.get("step") or ""),
            "draft_ver": int(_mat.get("draft_ver") or 0),
            "draft_fp": str(_mat.get("draft_fp") or ""),
        }
    # [按回执交付 2026-11-19] 回复块带收口核对结果（结构化）：刷新后的历史
    # 与最终回复共用同一 verdict；bound=None（无绑定动作）不落键（空则不同构）。
    if state.receipt.get("bound") is not None:
        _reply_blk["receipt"] = {
            "kind": str((state.receipt.get("bound") or {}).get("kind") or ""),
            "satisfied": bool(state.receipt.get("satisfied")),
            "dispatched": int(state.receipt.get("dispatched") or 0),
            "ok": int(state.receipt.get("ok") or 0),
        }
        if state.receipt.get("reports"):
            _reply_blk["receipt"]["reports"] = state.receipt["reports"]
    # [系统结果 2026-11-19 · 超长会话漏调专项] 汇报唤醒回合的执行事实随块
    # 落盘（status＋逐项明细）——刷新后的历史、流式交付、最终回复同一来源；
    # 模型正文里的「已发」字样只是正文，展示态以这个结构化字段为准。
    if state.system_result:
        _reply_blk["receipt"] = {
            **(_reply_blk.get("receipt") or {}),
            "result_status": str(state.system_result.get("status") or ""),
            "result_text": str(state.system_result.get("text") or ""),
            "result_items": list(state.system_result.get("items") or []),
        }
    await session_facade.append(state.work_id, _sid(state), _reply_blk,
                                emit_event=False)
    # [系统结果独立事件 2026-11-20 · 用户复验] 汇报唤醒回合的执行事实单独
    # 成事件——实时（SSE）与刷新（timeline from=0 重建）同一条渲染路都出
    # **独立的系统结果行**，不再藏在 deliver 的"交付拼接"行里（用户实测：
    # POST 回复与会话块有「本轮未执行工具」，卡片 timeline 正文却只有模型
    # 自述）。事件带结构化明细；模型正文里的同名标记只是正文，展示态以
    # 本事件＋会话块 receipt 为准。
    if state.system_result:
        event_stream.emit(state.work_id, _sid(state), "system_result", {
            "status": str(state.system_result.get("status") or ""),
            "text": str(state.system_result.get("text") or ""),
            "items": list(state.system_result.get("items") or []),
            "pair_id": str(state.pair_id or ""),
            "rounds": state.rounds,
        })
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
    # [F15 · 2026-10-31 时效回收接线（dsh maybeReclaim 挂 turn/end 同位）]：
    # 深层超 7d 未触达整子树沉没、顶层 done 超 3d 退常驻（标 sunk）。sunk 只影响
    # 凝练全景体积（render_condense_memory），上下文基线渲染照常（零误伤口径）。
    # 纯机械（无 LLM）、异常自吞（memory_store.reclaim_turn_end 内部零阻断）。
    memory_store.maybe_schedule_reclaim(state.work_id, state.card_id)
    progress.set_stage(state.work_id, _sid(state), "done", "完成")
    # [信息流 Task3/打点 A96] done 事件
    # [系统消息 2026-10-26 · 用户令「系统消息只允许跟事件挂钩」] 事件**自带**
    # card_type：规则层只从事件字段判断，不去读卡的状态快照。
    try:
        _ct = str(((await card_store.read_card(state.work_id, state.card_id))
                   .get("meta") or {}).get("card_type") or "")
    except Exception:  # noqa: BLE001 取不到类型不影响 done 事件本身
        _ct = ""
    event_stream.emit(state.work_id, _sid(state), "done",
                      {"rounds": state.rounds, "model_calls": state.model_calls,
                       "card_type": _ct,
                       # [harness 第 1 步 2026-10-28] 补 end_reason / pair_id：
                       # 「这轮是正常收口、人停、还是判不了交人」是最直接的判断依据，
                       # 改前只写进会话块的 end 字段，事件面上问不出来。
                       "end_reason": state.end_reason or "model_done",
                       "pair_id": str(state.pair_id or "")})
    return {
        "reply": state.clean_text,
        "model": state.model_used,
        "created_cards": state.created_cards,
        "tool_events": state.tool_events,
        "end_reason": state.end_reason or "model_done",
        # [批三返修①] 回合标识回传：旧回合清场按旧回合**自身的** pair_id
        # 点名——判据不落在「读回时谁还在」的可变状态上。
        "pair_id": str(state.pair_id or ""),
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
    """工具循环异常收口（error 埋点，不阻断错误返回）。

    [信息流 Task2·A93] 原在此落 data_snapshot checkpoint 供续走——
    [2026-11-04 裁定撤销]：checkpoint 按**模型失败次数**点火（429/限额
    是高频事件），实测一轮拷 62911 个文件跑 6 分钟、把双 loop 全冻死；
    而续跑读的是活库/会话、根本不消费快照。快照改为只留每日两次定时
    全备（见 main._scheduled_snapshots），错误路径不再碰盘。
    """
    state.error = str(exc)
    progress.set_stage(state.work_id, _sid(state), "error", str(exc))
    logger.error("模型调用/工具循环失败 card=%s: %s", state.card_id, exc)
    event_stream.emit(state.work_id, _sid(state), "error",
                      # [harness 第 1 步 2026-10-28] 补 error_type：异常类名是
                      # "能当判断依据"的零件（区分断连/超限/权限），整段 error 文本
                      # 只是载荷。改前两者混在一个字段里，条件面只能做子串匹配。
                      {"error": state.error,
                       "error_type": type(exc).__name__,
                       "rounds": state.rounds})

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


_LANE_SEEN: set = set()


def _log_lane_assignment(work_id: str, card_id: str, lane) -> None:
    """每卡首投记一行：实测分布可验（子树根哈希 → 槽环号）。"""
    key = (work_id, card_id)
    if key in _LANE_SEEN:
        return
    _LANE_SEEN.add(key)
    try:
        from ..infra import loops as _loops
        logger.info("[lanes] card=%s → runner lane %d",
                    card_id, _loops.runner_lane_index(lane))
    except Exception:  # noqa: BLE001
        pass


def _spawn_tracked(coro, *, work_id: str = "", card_id: str = ""):
    """任务起活 ＋ 强引用登记（完成即自动移除）。

    [runner 分槽环 2026-11-05] 带 ``card_id`` 的任务按**子树根亲和**投到
    对应槽环；无卡上下文投主环 A。跨环经 ``run_coroutine_threadsafe`` ⇒
    返回 ``concurrent.futures.Future``——``._card_msg_target`` 属性、
    ``add_done_callback``、``cancel`` 全兼容；**要 await 必须走
    ``await_spawned``**（CF Future 不能裸 await，需在调用方环上
    ``wrap_future``）。

    [双循环事故 2026-11-04] 若 spawn 的**发起方**落在服务环 B（emit 链
    污染），照旧落栈留痕——协程本身仍会被正确路由走，报警只为抓入口。
    """
    from ..infra import loops as _loops
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    lane = (_loops.runner_lane_for(work_id, card_id)
            if card_id else _loops.main_loop())
    if lane is None:
        lane = _loops.main_loop()
    if running is not None and running is _loops.serving_loop():
        _forensic_spawn_on_serving(coro)
    if lane is not None and running is not lane:
        t = asyncio.run_coroutine_threadsafe(coro, lane)
        if card_id:
            _log_lane_assignment(work_id, card_id, lane)
    else:
        t = asyncio.create_task(coro)
    _TRIGGER_TASKS.add(t)
    t.add_done_callback(_TRIGGER_TASKS.discard)
    return t


async def await_spawned(t):
    """await 一个 _spawn_tracked 返回值：CF Future ⇒ 绑当前环；Task ⇒ 直接等。"""
    if isinstance(t, concurrent.futures.Future):
        return await asyncio.wrap_future(t)
    return await t


def _forensic_spawn_on_serving(coro) -> None:
    try:
        import traceback as _tb, threading as _th, time as _time
        _stack = "".join(_tb.format_stack())
        logger.warning(
            "[loops] _spawn_tracked 发起于服务循环！coro=%s\n%s",
            getattr(coro, "__qualname__", coro), _stack)
        try:
            import os as _os
            with open(_os.path.join("logs", "spawn_forensics.log"),
                      "a", encoding="utf-8") as _f:
                _f.write(f"===== {_time.strftime('%H:%M:%S')} "
                         f"coro={getattr(coro, '__qualname__', coro)} "
                         f"thread={_th.current_thread().name} =====\n"
                         f"{_stack}\n")
        except OSError:
            pass
    except Exception:  # noqa: BLE001 报警自身绝不阻断 spawn
        pass


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
                                 "error": str(exc)})
            continue
        if isinstance(result, dict) and result.get("error"):
            session_ops.ops_log("card_msg_answer_failed", work_id, card_id,
                                {"reason": "send_failed", "msg_id": msg_id,
                                 "error": str(result.get("error"))})
            continue
        session_ops.ops_log("card_msg_answer_sent", work_id, card_id,
                            {"msg_id": str((result or {}).get("msg_id", "")),
                             "target": from_card, "reply_to": msg_id})


#: [排队保证 2026-10-22 · D24] 每卡自续轮的次数上限，防「消息永远处理不完」打转烧额度。
#: 水位机制保证正常情况下每次续轮都会推进水位，这个上限只兜异常（回合连续失败）。
_SELF_REQUEUE_MAX = 5
_SELF_REQUEUE: dict[tuple[str, str], int] = {}


async def deliver_now(work_id: str, card_id: str, *,
                      text: str = "",
                      steer_text: str = "",
                      source_kind: str = "internal",
                      source: dict[str, Any] | None = None,
                      msg_type: str = "") -> bool:
    """**立即**把该卡未处理的消息投递成一轮。返回是否真的送达（起轮或插话）。

    [发消息即唤醒 2026-10-22 · D20/D24/D25] 这是「通信」与「投递」的分界线：
      · 无未处理消息 ⇒ 什么都不做，返回 False
      · 对方**正在跑** ⇒ text 模式借 steer 通道把 ``steer_text``（缺省=text）
        插进在跑回合的轮槽——下一次模型调用即见，返回 True；
        无 text 的旧收件箱路径维持不打断、返回 False。
      · 对方**空闲** ⇒ 立刻起一轮，返回 True

    [2026-11-08 · 修「父卡在跑时 report_up 推不进」] 旧路在对方忙时只落
    False 就走——设计指望「收口时 schedule_card_message_triggers 自查续轮」，
    但那条链只认 TRIGGERS/收件箱，而 10-26 起消息改落会话块后两者都是空的
    ⇒ 兜底是死路；且在跑回合的 prior 是回合首拼的，本会话块要等**下一个
    回合**的 build_context 才进上下文 ⇒ 对整个在跑回合不可见。插话通道
    本来就是为此造的，直接用。
    """
    # [测试环境开关 2026-10-22] 既有测试大量是同步时序写的，自动投递会在发信
    # 瞬间起异步回合，与它们冲突并放大 Windows 会话文件锁抖动。测试里模型是 fake，
    # 立刻投递没有真实语义价值 ⇒ 仅测试环境关闭，真实链路不变。
    # 开关由 tests/conftest.py 设置，显式且只影响这一处。
    if os.environ.get("ECO_NO_AUTO_DELIVER") == "1":
        return False
    # [多进程拆分 2026-11-06] 卡不归本片——让属主 runner 自己判 pending/起回合。
    from ..infra import procs as _procs
    if (_procs.is_runner()
            and _procs.owner_index_of(work_id, card_id)
            != _procs.runner_index()):
        _r = await _procs.forward_ctl(work_id, card_id, "/ctl/deliver", {
            "work_id": work_id, "card_id": card_id, "text": text,
            "steer_text": steer_text, "source_kind": source_kind,
            "source": source, "msg_type": msg_type})
        return bool(_r.get("delivered"))
    review_source = None
    if source and source.get("kind") == "review_dispatch":
        from . import review_runner

        if await review_runner.dispatch_block_reason(
                work_id, card_id, source, phase="enqueue"):
            return False
        if review_runner.is_batch_dispatch(source):
            review_source = source
    # [增量投递 2026-10-23 · 用户令「读过的信息就从 box 里清掉」]
    # 用 **take_pending**（读 + 推水位同一事务）而不是 list_pending：投递即消费。
    # 改前只读不消费 ⇒ 水位永不前进 ⇒ 同一批消息被反复拼进 prompt（实测
    # card_029 一次 46 条占 prompt 98%，模型自己写「已是前一轮归档过的内容」）。
    # [沉默 2026-10-24] 沉默的卡不取件不唤醒——信留在收件箱 pending,
    # 解冻后按既有增量投递自然补上(取走就丢等于替它做主)。
    try:
        from ..storage import governance_store as _gov
        if _gov.is_silenced(work_id, card_id):
            return False
    except Exception:  # noqa: BLE001 治理表读不出 ⇒ 不挡投递(旧行为)
        pass
    # [停止前置 2026-11-10] halt 闸原在 _run_message 内部——起轮任务里返回
    # error dict，发送方永远只看见「已 spawn」的假回执（card_259 实证：direct
    # 落会话后被 halted_reject 静默吞掉）。前置检查后：非 directive 唤醒在
    # 停卡上直接 False——发送方如实拿到「没送达」；directive 放行，落到
    # _run_message 闸门里解除停止再跑（豁免判定唯一收口在闸门）。
    try:
        from . import control as _ctl
        if (source_kind != "directive"
                and _ctl.is_halted(work_id, card_id) is not None):
            session_ops.ops_log("card_halted_reject", work_id, card_id,
                                {"wake": str(source_kind), "via": "deliver_now"})
            return False
    except Exception:  # noqa: BLE001 停止表读不出 ⇒ 不挡投递(旧行为)
        pass
    # 回执在插话或起轮前先核对等待账。有效回执由目标驱动统一续跑，
    # 被拦下的回执仍保存在会话历史。
    if msg_type:
        try:
            from ..goal import wait_store as _ws
            if msg_type in _ws.RECEIPT_TYPES:
                _dec = await _ws.receipt_gate(work_id, card_id, receipt={
                    "type": msg_type, "from": (source or {}).get("from"),
                    "dispatch_task_id": (source or {}).get("dispatch_task_id")})
                if _dec == "driver":
                    from ..goal import driver as _gd
                    _gd.request_drive(work_id, card_id)
                    session_ops.ops_log(
                        "receipt_wait_release", work_id, card_id,
                        {"type": msg_type,
                         "from": str((source or {}).get("from") or "")})
                    return True
                if _dec == "hold":
                    session_ops.ops_log(
                        "receipt_wait_hold", work_id, card_id,
                        {"type": msg_type,
                         "from": str((source or {}).get("from") or "")})
                    return False
        except Exception:  # noqa: BLE001 等待账读不出 ⇒ 照常投递，不挡主链
            pass
    # [忙判定前置 2026-10-25] 收件卡在跑 ⇒ **先不取件**——改前 take_pending
    # (消费)在忙判定之前,B 忙时信被取走但投递 409 ⇒ 信既不在收件箱也不在
    # 任何人上下文。取走=承诺投递,投不出去就不该取;B 本轮收口的自查续轮
    # 会再走这里,信不丢。
    try:
        if session_facade.turn_holder(work_id, card_id):
            if (source or {}).get("basis_task"):
                # 在途回合绑定的是另一任务，派工留在原会话按原编号接续。
                return False
            # [轮内复投 2026-11-08] 对方在跑：text 模式的消息正文已在会话历史
            # （append_card_message 落块），但在跑回合的 prior 是回合首拼的——
            # 只落库，模型要等下一个回合才看得见（实测：父卡在跑时 report_up
            # 永远推不进）。借 steer 通道把通知插进轮槽：下一次模型调用即见，
            # 且不冒充人声（mark 标卡间/系统来源、不发 user_message 事件）。
            if not text:
                return False
            try:
                _st = await steer.request_steer(
                    work_id, card_id, str(steer_text or text),
                    mark=("卡间消息："
                          if (source or {}).get("kind") == "card_msg"
                          else "" if (source or {}).get("kind") == "steer_remains"
                          else "系统消息："),
                    emit_event=False,
                    report_targets=tool_receipt.report_targets_from_source(source_kind, source),
                    review_source=review_source)
            except Exception as exc:  # noqa: BLE001 插话通道故障不升级（信在库）
                logger.warning("[通信] 忙时插话失败 card=%s: %s", card_id, exc)
                return False
            if _st.get("applied"):
                session_ops.ops_log(
                    "card_msg_steered", work_id, card_id,
                    {"from": str((source or {}).get("from") or ""),
                     "kind": str((source or {}).get("kind") or "")})
                return True
            # 竞态：判忙后对方恰摘牌 ⇒ 不落 False 走人，回下方正常起轮路径。
    except Exception:  # noqa: BLE001
        pass
    # [2026-10-26 · 消息改落会话块后修唤醒] ``text`` 模式：消息已经写进
    # **会话历史**（append_card_message），不在收件箱里 ⇒ 不能走 take_pending
    # （取到空会直接 return False，唤醒失效）。此时只负责起一轮，正文由调用方
    # 给一句"你收到一条指令，请按上文执行"，真正的正文在历史里。
    if text:
        pending = []
        prompt = text
    else:
        try:
            pending = await card_db.take_pending(work_id, card_id)
        except Exception as exc:  # noqa: BLE001 取件失败就当没有（信仍在库，收口会兜）
            logger.warning("[通信] 取未处理消息失败 card=%s: %s", card_id, exc)
            return False
        if not pending:
            return False
        try:
            prompt = await _build_card_msg_prompt(work_id, pending)
        except Exception as exc:  # noqa: BLE001 组装失败退到兜底，不让通信断
            logger.warning("[通信] 组装消息提示词失败，用兜底 card=%s: %s", card_id, exc)
            prompt = _fallback_card_msg_prompt(pending)
    from . import routes as _routes
    try:
        # [不占用户通道 2026-10-26] 改前 append_user=True ⇒ 卡间来信的激励文被落成
        # role="user" 的会话块,前端当成「用户说的话」显示(线上实证:061 对话里
        # 出现整段「上一轮之后,你收到了来自其他卡的消息…」冒充人声)。K3 原本的
        # 设计就是 False(routes.py 段⑥注释:「append_user=False(卡间调起)时不落
        # user 块——激励文绝不进收件方档案」),deliver_now 这处传错了。
        # 消息的可见面走 events 的 msg_* 消息块(emit_message_event),不占用户通道。
        # [2026-10-26 · 记录补全] 带上**唤醒源**：这一轮是被哪张卡的消息叫起来的。
        _src = (dict(source) if source else
                {"kind": "card_msg",
                 # text 模式 pending 恒空——实跑踩过 list index out of range
                 "from": str(((pending[0] if pending else {}) or {})
                             .get("from_card") or "")})
        task = _spawn_tracked(
            _routes._run_message(work_id, card_id, prompt, None,
                                 append_user=False, source=_src,
                                 source_kind=source_kind),
            work_id=work_id, card_id=card_id)
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
    - 有 pending → `asyncio.create_task` 起收件方回合(激励文落档——评审/信件全程发生在对话里)，
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
        # [worker 卡 2026-10-15 · §六块3 静默判定②] 完工worker 卡不被 K3 调起：
        # 只读 card_type/state 两字段的机械判定（红线 2），continue 且**不打
        # ops**；信留在收件箱 pending 作验收留档（下次收口重新判，仍跳过）。
        try:
            # [沉默 2026-10-24] 沉默的收件卡跳过(信留 pending,不唤醒);
            # 完工worker 卡的既有静默判定在下一行,语义独立(那是天然状态,
            # 这是管理动作)。
            try:
                from ..storage import governance_store as _gov
                if _gov.is_silenced(work_id, target_id):
                    session_ops.ops_log("card_msg_silenced_skip", work_id,
                                        target_id, {})
                    continue
            except Exception:  # noqa: BLE001
                pass
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
        # [worker 卡 2026-10-15 · §六块3 静默判定②] 完工worker 卡不被 K3 调起。
        # [worker 卡可继续派活 2026-10-21 · 用户令] 但**派活**要放行：主卡/人发
        # msg_ask 给一张完工 worker，就是「这条线还有后续」——先复位（state=active
        # ＋目标重开）再正常调起，而不是另建一张卡。纯回执类（worker_done 等）
        # 仍然跳过：否则完工回执会把它自己再叫起来一遍。
        # [机制判断归harness 第3步] 完工判定走派生视图（事件权威＋列回落）。
        # [完工不影响功能 2026-11-08] 改前用 derive(delivery)==completed 把
        # 完工卡整段挡在门外，且只放行 msg_ask（该类型已退役 ⇒ 永不放行）。
        # 现在只按「这封信是派活还是纯回执」判：纯回执不叫醒（防完工回执
        # 自唤醒），其余照常调起；完工标记不参与判定。
        if str(_tmeta.get("card_type") or "") == "worker" and pending:
            _RECEIPT_ONLY = {"worker_done", "coder_done", "msg_report",
                             "msg_answer", "msg_notify", "msg_sync"}
            _types = {str((m or {}).get("type") or "") for m in pending}
            if _types and _types <= _RECEIPT_ONLY:
                continue
        session_ops.ops_log("card_msg_deliver", work_id, target_id,
                            {"msg_id": str(pending[-1].get("msg_id") or ""),
                             "msg_ids": list(msg_ids)})
        # [增量投递 2026-10-23 · 用户令「读过的信息就从 box 里清掉」]
        # 判定用 list_pending（上面worker 卡静默判定要「信留在 pending 作验收留档」），
        # **真正要投递时才 take**：读 + 推水位同一事务 ⇒ 这批消息立刻移出收件箱，
        # 下次只投递新的。改前只读不消费 ⇒ 同一批被反复投递。
        # [忙判定前置 2026-10-25] 收件卡在跑 ⇒ 先不取件(取走=承诺投递,
        # 投不出去就不该取);其本轮收口的自查续轮会再走这里,信不丢。
        try:
            if session_facade.turn_holder(work_id, target_id):
                session_ops.ops_log("card_msg_pending", work_id, target_id,
                                    {"reason": "receiver_busy"})
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            pending = await card_db.take_pending(work_id, target_id)
        except Exception as exc:  # noqa: BLE001 取件失败就不投（信仍在库，下次收口再试）
            logger.error("[K3] 取收件箱失败 wid=%s target=%s: %s",
                         work_id, target_id, exc)
            continue
        if not pending:
            continue
        # 激励文＝底座提供的「待处理消息」素材（带发件卡名＋类型中文名＋应对
        # 说明＋**消息正文**）。构造失败回落默认文、不阻断调起。
        try:
            prompt = await _build_card_msg_prompt(work_id, pending)
        except Exception as exc:  # noqa: BLE001 激励文失败不阻断调起
            logger.warning("[K3] 构造卡间激励文失败、回落默认文: %s", exc)
            prompt = _fallback_card_msg_prompt(pending)
        task = _spawn_tracked(
            _routes._run_message(work_id, target_id, prompt,
                                 None, append_user=True),
            work_id=work_id, card_id=target_id)
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
                resp = await await_spawned(t)
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

        _spawn_tracked(_watch_pending(task, work_id, target_id),
                       work_id=work_id, card_id=target_id)

    # ============================================================
    # [排队语义 2026-10-23 · 用户令] 本轮**不在回合内**起下一回合。
    #
    # 原来这里直接 `_spawn_tracked(_run_message(...))`：回合还没返回、互斥体还没摘，
    # 新回合去 turn_try_acquire 必然被拒（今天实测 82 次 mutex_reject，12 张卡）——
    # 消息就躺在队列里没人再捡。
    #
    # 正确做法（用户原话）：「所有消息排队等这一轮跑完，再下一个回合一起进」：
    #   · 回合内**只记一个标记**，什么都不起；
    #   · 由 routes 在 `turn_release()` **之后**统一捞队列（`deliver_now`）——
    #     那里已有正确判据：有 pending 才起、有人占着就等、多条一起进一个回合。
    # ⇒ 自续轮这个「回合内起回合」的入口整体退役。
    # ============================================================
    try:
        _own = await card_db.list_pending(state.work_id, state.card_id)
    except Exception as exc:  # noqa: BLE001 自查失败不应影响本回合收口
        logger.warning("[排队] 自查未处理消息失败 card=%s: %s", state.card_id, exc)
        _own = []
    if _own:
        # 只留痕、只打标记；真正调起交给摘牌后的统一入口（routes 侧）
        state.requeue_pending = True
        session_ops.ops_log("card_msg_requeue_pending", state.work_id, state.card_id,
                            {"msgs": len(_own),
                             "msg_ids": [str((it or {}).get("msg_id") or "") for it in _own]})
        logger.info("[排队] 收口时仍有 %d 条未处理消息 ⇒ 摘牌后由统一入口接续 card=%s",
                    len(_own), state.card_id)
    else:
        state.requeue_pending = False
        _SELF_REQUEUE.pop((state.work_id, state.card_id), None)
