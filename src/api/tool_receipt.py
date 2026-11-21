# -*- coding: utf-8 -*-
"""工具调用来源与真实回执（2026-11-19 · 通用工具链修复）。

方案：docs/工具调用与真实回执-修复方案-20261119.md。
本模块是「调用来源分类 / 派发台账 / 当前轮短提示 / 绑定动作回执闸」的
单一收口，message_runner 与 gateway 各自只消费这里的函数，禁止各处
复制判据。

四个来源（互斥，一次调用的 origin 有且只有一个）：

======================  =====================================================
origin                  含义
======================  =====================================================
``native``              合法原生 tool_calls（API 标准字段进入可执行列表）
``declared_text``       声明文本协议端点按宿主声明格式提交的正文调用
                        （wb-cli/devin-cli：系统提示词声明 <antml:invoke>）
``pseudo_compat``       原生端点把调用写进正文，由旧版兼容层代执行
                        （实跑计入 rescue 通道，单列来源）
``none``                没有调用（含协议失败/伪调用拒绝——未派发）
======================  =====================================================

开关（环境变量）：

- ``TOOL_CALL_STRICT``   验收模式：原生端点正文伪调用**不代执行**——
                         记录格式不符、反馈实际调用通道，当前动作仍待执行。
                         声明文本协议端点不受影响（那是它的工作协议）。
                         缺省关（生产保留兼容代执行，但来源单列）。
- ``TOOL_TURN_HINT``     当前轮短提示：首轮请求在最新请求附近注入一条
                         随实际端点协议生成的调用方式提示。缺省开。
- ``TOOL_RECEIPT_GATE``  按回执交付：已绑定动作的回合收口时，用工具回执
                         核对完成条件；无匹配回执 ⇒ 交付文本追加系统回执行。
                         缺省开。

台账：``logs/tool_dispatch_ledger.jsonl``（append-only）。最小关联字段：
工作、会话、回合、调用、工具、目标、来源、派发与结果。区分「收到请求 /
实际派发 / 执行结果」——dispatched=false 的行是未派发（协议失败、伪调用
拒绝、零调用欠动作），它们与执行行一样进台账，虚报成功才有对账基准。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

_LEDGER_PATH = Path("logs") / "tool_dispatch_ledger.jsonl"

# 卡间通信类工具（「发信」类别呈现的机械判据集合；指令回合绑定的收口动作
# 面——direct 的机制级收口是 report_up 汇报发令卡，通信类任一命中即算）。
SEND_TOOLS: frozenset[str] = frozenset({
    "direct", "sync", "report_up", "msg_report", "send_card_message",
})

# 专用操作回合（op）各自要求成功的专用工具——按注册表**真实工具名**绑定。
# [2026-11-20 修] requirements_final 的专用口是 write_requirements_final
# （OP_ONLY_TOOLS，根卡需求正稿），此前误绑 edit_requirements ⇒ 根卡成功
# 写正稿仍被判未完成（用户复验实录）。requirements_draft 回合的合法完成
# 面＝写草稿（edit_requirements）或开评审（open_review）。
OP_REQUIRED_TOOLS: dict[str, frozenset[str]] = {
    "update_status": frozenset({"propose_status"}),
    "requirements_final": frozenset({"write_requirements_final"}),
    "requirements_draft": frozenset({"edit_requirements", "open_review"}),
}


def strict_mode() -> bool:
    """验收模式：原生端点正文伪调用不代执行。"""
    return os.environ.get("TOOL_CALL_STRICT", "") == "1"


def turn_hint_enabled() -> bool:
    """当前轮短提示开关（缺省开）。"""
    return os.environ.get("TOOL_TURN_HINT", "1") == "1"


def receipt_gate_enabled() -> bool:
    """按回执交付开关（缺省开）。"""
    return os.environ.get("TOOL_RECEIPT_GATE", "1") == "1"


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------

def ledger(entry: dict[str, Any]) -> None:
    """追加一条派发台账。失败不阻断（台账是观测面，不是主流程）。"""
    try:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def ledger_tool(work_id: str, card_id: str, pair_id: str, round_: int,
                tool: str, target: str, origin: str, *,
                dispatched: bool, ok: Optional[bool] = None,
                call_id: str = "", error_code: str = "",
                note: str = "") -> None:
    """一次调用的最小关联字段行（成功/失败/未派发统一走这里）。"""
    ledger({
        "kind": "tool_dispatch",
        "work_id": work_id, "card_id": card_id, "pair_id": pair_id,
        "round": round_, "call_id": call_id, "tool": tool, "target": target,
        "origin": origin,
        "dispatched": bool(dispatched),
        "ok": ok,
        "error_code": error_code, "note": note,
    })


# ---------------------------------------------------------------------------
# 当前轮短提示
# ---------------------------------------------------------------------------

def endpoint_protocol_of(model_name: str) -> str:
    """模型名 → 实际端点协议（网关单点判据：vendor 显式登记，不猜模型名）。

    网关不可用（测试离线/未装配）⇒ 回落 ``"native"``——原生口径从严。
    """
    try:
        from . import model_service  # noqa: PLC0415 延迟破环
        return str(model_service.get_gateway()._endpoint_protocol(model_name))
    except Exception:  # noqa: BLE001
        return "native"


def endpoint_facts(entry_key: str) -> dict[str, str]:
    """路由座键 → {vendor, api_base, endpoint_model, request_model,
    endpoint_protocol}（B3 对账）。

    ``entry_key`` 是网关回报的座位键（models.yaml models 键）；vendor/
    base_url/线路模型名按编译配置直读，不猜名字。``request_model`` 与
    ``endpoint_model`` 同值（请求里实际写上的线路名）——分列是为了与
    ``response_model``（端点回包声明）对账时三个名字各就各位。
    读不到 ⇒ 空串字段如实返回。
    """
    facts = {"vendor": "", "api_base": "", "endpoint_model": "",
             "request_model": "",
             "endpoint_protocol": endpoint_protocol_of(entry_key)}
    try:
        from . import model_service  # noqa: PLC0415 延迟破环
        cfg = (model_service.get_gateway().config.get("models") or {})\
            .get(entry_key, {}) or {}
        facts["vendor"] = str(cfg.get("vendor") or cfg.get("provider") or "")
        facts["api_base"] = str(cfg.get("base_url") or "")
        facts["endpoint_model"] = str(cfg.get("model") or "")
        facts["request_model"] = facts["endpoint_model"]
    except Exception:  # noqa: BLE001
        pass
    return facts


def _hint_for_native(tool_names: list[str]) -> str:
    names = "、".join(tool_names[:24]) if tool_names else "（本轮无工具）"
    return (
        "〔工具调用提示·系统〕\n"
        "本端点走原生 tool_calls 通道：需要读取外部事实或改变状态时，"
        "必须把调用放进 API 的 tool_calls 字段真发，"
        "正文里写出任何调用样式的文本都不会被执行、也拿不到结果。\n"
        f"本次可用工具：{names}。"
        "调用后以系统返回的执行回执为准，不要在正文里自行宣布已执行。")


def _hint_for_declared_text(tool_names: list[str]) -> str:
    names = "、".join(tool_names[:24]) if tool_names else "（本轮无工具）"
    return (
        "〔工具调用提示·系统〕\n"
        "本通道没有原生 tool_calls 字段：需要读取外部事实或改变状态时，"
        "按系统提示词【工具调用协议】声明的 <antml:function_calls> 格式，"
        "在回复正文末尾输出调用块——宿主会解析并真实执行。\n"
        f"本次可用工具：{names}。"
        "调用后以【工具结果】回执为准，不要在正文里自行宣布已执行。")


def build_turn_hint(endpoint_protocol: str, tools: list) -> Optional[dict[str, str]]:
    """当前轮系统短提示，在最新用户消息之前下发。

    内容随**实际端点协议**与**本次可用工具**生成；不带任何权限授予或
    「已执行」证明——它只告诉模型怎样真调、以什么为准。无工具时不出
    （纯对话轮没有可调的东西，提示是噪声）。
    """
    if not turn_hint_enabled():
        return None
    tool_names = [
        str((t.get("function") or {}).get("name") or t.get("name") or "")
        for t in (tools or []) if isinstance(t, dict)
    ]
    tool_names = [n for n in tool_names if n]
    if not tool_names:
        return None
    if endpoint_protocol == "declared_text":
        text = _hint_for_declared_text(tool_names)
    else:
        text = _hint_for_native(tool_names)
    return {"role": "system", "content": text}


# ---------------------------------------------------------------------------
# 绑定动作与回执闸
# ---------------------------------------------------------------------------
# [2026-11-20 返修 · 用户复核裁定] **正文词表全部移除**（DONE_WORDS/
# claims_completion 退役）：指令回合引用「已完成」、写「我没有说已发」都
# 会命中子串；专用操作零调用但正文没用到词表措辞又会漏补——正文措辞既
# 不可信也不可靠。绑定只来自**可信入口**，回执核对按**具体动作+目标**
# 匹配本轮真实回执；确认未派发才定向补一次；已派发但待确认先查原记录
# 防重复写入。通用指令（研判类卡间消息）与汇报回合继续允许零调用研判。

def report_targets_from_source(source_kind: str,
                               source: Optional[dict[str, Any]]) -> tuple[str, ...]:
    """从宿主投递来源读取明确的汇报对象。"""
    source = source or {}
    if source_kind == "directive" and source.get("require_report") is True:
        sender = str(source.get("from") or "").strip()
        return (sender,) if sender else ()
    if source_kind == "internal" and source.get("kind") == "steer_remains":
        targets = source.get("report_targets")
        if isinstance(targets, (list, tuple)):
            return tuple(target for target in targets if isinstance(target, str) and target)
    return ()


def _report_offsets(state: Any) -> dict[str, int]:
    offsets = dict.fromkeys(report_targets_from_source(
        getattr(state, "source_kind", ""), getattr(state, "source", None)), 0)
    offsets.update(getattr(state, "report_after", {}))
    return offsets


def _task_binding(state: Any) -> Optional[dict[str, Any]]:
    """可信任务入口的任务绑定（原任务/版本/动作/目标/判据）。

    [2026-11-20 返修 · B2] 绑定只来自**可信入口**，模型正文与自报编号
    不算数：
      1. ``source["task"]`` —— 显式任务记录（明确提交记录：人类入口
         ``req.task``、派工入口随源携带），字段 task_id/rev/text/
         action/target/accept；
      2. ``source["kind"]=="goal"`` —— 目标驱动回合（goalId+revision
         版本印；动作未明的任务只作执行事实呈现，不触发补跳）；
      3. ``source["kind"]=="worker_dispatch"`` —— 原派工记录
         （task_id＝派工信锚）。
    ``action`` 缺席 ⇒ 只有原任务、下一步未明（方案表第 2 行）——事实
    呈现、正常收口；``action`` 在 ⇒ 欠该动作的到目标派发才补一次。
    """
    src = getattr(state, "source", None)
    if not isinstance(src, dict):
        return None
    task = src.get("task")
    if isinstance(task, dict):
        return {
            "task_id": str(task.get("task_id") or task.get("id") or ""),
            "rev": task.get("rev") if task.get("rev") is not None
                   else task.get("revision"),
            "text": str(task.get("text") or "")[:400],
            "action": str(task.get("action") or "").strip(),
            "target": str(task.get("target") or "").strip(),
            "accept": str(task.get("accept") or "")[:200],
            # [B2返修] 责任会话/责任卡——任务记录显式给谁负责就带谁，
            # 对账与补救提示据此呈现，不靠猜。
            "session": str(task.get("session") or task.get("owner")
                           or task.get("assignee") or "")[:80],
        }
    kind = str(src.get("kind") or "")
    if kind == "goal":
        return {"task_id": str(src.get("goalId") or ""),
                "rev": src.get("revision"), "text": "",
                "action": "", "target": "", "accept": ""}
    if kind == "worker_dispatch":
        return {"task_id": str(src.get("task_id") or ""),
                "rev": "", "text": "", "action": "", "target": "",
                "accept": ""}
    return None


def _event_targets(event: dict[str, Any]) -> set[str]:
    """一条执行记录的实际目标集合（发信按逐收件人，读写按目标字段）。"""
    out: set[str] = set()
    send = event.get("send") or {}
    for r in (send.get("recipients") or []):
        if isinstance(r, dict):
            to = str(r.get("to") or "").strip()
            if to:
                out.add(to)
    for k in ("card_id", "target"):
        v = str(event.get(k) or "").strip()
        if v:
            out.add(v)
    return out


def bound_action(state: Any) -> Optional[dict[str, Any]]:
    """优先返回专用操作与可信任务绑定；汇报义务由收口核对独立判。

    [2026-11-20 返修 · B2] ``require_report`` 只是「向发令卡汇报」的
    义务核对——它**不是**原任务本身（007 型用户直接交办的任务没有
    发令卡）。任务绑定经 ``_task_binding`` 从可信入口取：原任务、
    版本、动作、目标、判据五个字段。汇报义务仍按 ``_report_offsets``
    单独核对，两种绑定互不顶替。
    """
    op = str(getattr(state, "op", "") or "")
    required = OP_REQUIRED_TOOLS.get(op)
    if required:
        return {"kind": "op", "op": op, "required_tools": sorted(required),
                "targets": []}
    task = _task_binding(state)
    if task is not None and (task.get("task_id") or task.get("action")):
        return {"kind": "task", **task,
                "required_tools": ([task["action"]] if task.get("action")
                                   else []),
                "targets": ([task["target"]] if task.get("target") else [])}
    targets = _report_offsets(state)
    if targets:
        return {"kind": "directive", "required_tools": sorted(SEND_TOOLS),
                "targets": sorted(targets)}
    return None


def _report_state(target: str, events: list[dict[str, Any]]) -> str:
    unverified = failed = False
    for event in events:
        if event.get("tool") not in SEND_TOOLS:
            continue
        recipients = [r for r in ((event.get("send") or {}).get("recipients") or [])
                      if isinstance(r, dict)]
        if recipients:
            for recipient in recipients:
                actual = str(recipient.get("to") or "").strip()
                if actual == target:
                    if event.get("ok") is True and str(recipient.get("msg_id") or "").strip():
                        return "verified"
                    unverified = True
                elif not actual:
                    unverified = True
            continue

        # 无逐收件人回执时，显式指向别人的发信与本核对无关。
        requested = str(event.get("card_id") or event.get("target") or "").strip()
        if requested and requested != target:
            continue
        if event.get("ok") is False and event.get("error_code") in {"validation", "denied"}:
            failed = True
        else:
            # 例如落信之后抛出投递异常：工具失败，落账结果仍待核实。
            unverified = True
    if unverified:
        return "unverified"
    return "failed" if failed else "not_sent"


def receipt_verdict(state: Any) -> dict[str, Any]:
    """分别核对专用操作和各发令卡的执行证据。"""
    bound = bound_action(state)
    events = [e for e in (getattr(state, "tool_events", None) or [])
              if isinstance(e, dict)]
    dispatched = len(events)
    ok_events = [e for e in events if e.get("ok") is True]
    if bound is None:
        return {"bound": None, "dispatched": dispatched,
                "ok": len(ok_events), "satisfied": True, "detail": ""}
    reports = {target: _report_state(target, events[offset:])
               for target, offset in _report_offsets(state).items()}
    unresolved = set(reports.values()) - {"verified"}
    send_state = next(
        (status for status in ("unverified", "failed", "not_sent") if status in unresolved),
        "verified" if reports else "not_sent")
    required = set(bound["required_tools"])
    is_op = bound["kind"] == "op"
    op_satisfied = not is_op or any(e.get("tool") in required for e in ok_events)
    op_missing = is_op and not any(e.get("tool") in required for e in events)
    # [任务绑定核对 2026-11-20 · B2返修] task：要求动作到要求目标的
    # 真实派发逐项核，**「动作已派发」与「业务判据已满足」分开记**——
    #   missing    无任何满足目标的派发 ⇒ 补一次的判据；
    #   unverified 有派发但业务判据未证（发信收件人/锚/投递状态不符，
    #              或非发信动作失败/缺锚）⇒ 不重发，沿调用 ID 与业务
    #              记录核查；
    #   verified   动作已派发且业务判据满足 ⇒ 满足。
    # action 缺席 ⇒ 只有原任务、下一步未明 ⇒ 不算缺、不补跳（事实呈现）。
    # [B2返修·发信判据] 发信类动作不看「tool+目标」粗匹配——按
    # send.recipients 实际收件人＋消息锚（msg_id）＋投递状态逐项核：
    #   · 收件人不符/无收件人记录 ⇒ missing（要求的送达没发生，补一次）；
    #   · 收件人符但 msg_id 缺席 ⇒ 锚缺失 ⇒ unverified（不能证是那条）；
    #   · 投递状态 pending（落账未受理）⇒ unverified 待核实，不重发
    #     （重发会重复落信）；delivered/synced 才算受理。
    task_state, task_missing, task_satisfied = "", False, True
    task_dispatched = False
    task_note = ""
    if bound["kind"] == "task":
        _act = str(bound.get("action") or "")
        _tgt = str(bound.get("target") or "")
        if _act:
            _cands = [e for e in events if str(e.get("tool") or "") == _act]
            task_dispatched = bool(_cands)
            if _act in SEND_TOOLS:
                # [B2返修·收件人证据全扫] 收件人记录不只看 ok 事件——
                # 「落信成功、唤醒/后续步骤失败」的形态里 msg_id 已落账，
                # 落账证据不因事件 ok=False 而消失（防重复发信的第一道闸）。
                _recs: list[dict[str, str]] = []
                unknown_recipients = []
                for e in _cands:
                    recipients = (e.get("send") or {}).get("recipients") or []
                    if not recipients and e.get("ok") is True:
                        known_targets = _event_targets(e)
                        if not known_targets or not _tgt or _tgt in known_targets:
                            unknown_recipients.append("")
                    for r in recipients:
                        if not isinstance(r, dict):
                            continue
                        _to = str(r.get("to") or "").strip()
                        if not _to:
                            unknown_recipients.append(str(r.get("msg_id") or "").strip())
                            continue
                        if _tgt and _to != _tgt:
                            continue
                        _recs.append({
                            "to": _to,
                            "msg_id": str(r.get("msg_id") or ""),
                            "state": str(r.get("state") or "")})
                if not _recs:
                    # 无满足目标的收件人记录——含「发信工具成功但
                    # recipients 未记录」（msg_report/send_card_message
                    # 无 send 块）时退回 tool+目标粗核，锚缺如实降级。
                    if unknown_recipients:
                        task_state = "unverified"
                        task_satisfied = False
                        anchors = sorted({mid for mid in unknown_recipients if mid})
                        task_note = "收件人明细未记录，投递状态待核实"
                        if anchors:
                            task_note += f"（msg_id {'、'.join(anchors)}）"
                    else:
                        # [B2返修·失败分级] 「确认目标侧没有派发」才算
                        # missing 可补：失败事件全是**派发前拒绝**
                        # （not_found/denied/validation 等明确前置码）⇒
                        # 确认没落账 ⇒ missing；出现 internal/未知错
                        # ⇒ 落账结果不明 ⇒ unverified 不重发。
                        _PRE = {"not_found", "denied", "validation",
                                "forbidden", "perm_denied", "halted"}
                        _failed = [e for e in _cands
                                   if e.get("ok") is not True]
                        _unclear = [e for e in _failed
                                    if str(e.get("error_code") or "")
                                    not in _PRE]
                        if _unclear:
                            task_state, task_satisfied = "unverified", False
                            task_note = ("发信执行中断、落账结果不明"
                                         "——待核实，不重发")
                        else:
                            task_state, task_missing, task_satisfied = \
                                "missing", True, False
                else:
                    _accepted = [r for r in _recs
                                 if r["msg_id"]
                                 and r["state"] in ("delivered", "synced")]
                    _anchored = [r for r in _recs if r["msg_id"]]
                    if _accepted:
                        task_state = "verified"
                    else:
                        task_state, task_satisfied = "unverified", False
                        if not _anchored:
                            task_note = "消息锚（msg_id）缺失，投递对象待核实"
                        else:
                            task_note = ("已落信但投递待确认"
                                         "（收件人在账、受理未证）")
            else:
                # [B2返修·成功按目标逐条匹配] 「编辑 A 失败、编辑 B 成功」
                # 不得把 A 判通过——verified 要求**到目标的 ok 事件**，
                # 不是「同工具任何目标成功过」。
                matched = [e for e in _cands
                           if (not _tgt or _tgt in _event_targets(e))]
                if not matched:
                    task_state, task_missing, task_satisfied = \
                        "missing", True, False
                elif any(e.get("ok") is True for e in matched):
                    task_state = "verified"
                else:
                    task_state, task_satisfied = "unverified", False
        else:
            task_state = "open"
    satisfied = op_satisfied and task_satisfied and not unresolved
    detail = f"派发 {dispatched} 次、成功 {len(ok_events)} 次"
    if is_op:
        detail = f"要求工具 {sorted(required)}，{detail}"
    if bound["kind"] == "task" and bound.get("action"):
        detail = (f"任务 {bound.get('task_id') or '—'} 要求 "
                  f"{bound.get('action')}({bound.get('target') or '—'})，"
                  f"{detail}")
    return {"bound": bound, "dispatched": dispatched,
            "ok": len(ok_events), "satisfied": satisfied, "detail": detail,
            "op_satisfied": op_satisfied, "op_missing": op_missing, "send_state": send_state,
            "reports": reports,
            "task_state": task_state, "task_missing": task_missing,
            # [B2返修] 「动作已派发」与「业务判据已满足」分开记——
            # dispatched=True 只说明动作到过执行器，判据满足与否看
            # task_state/task_satisfied（收件人/锚/投递状态逐项核）。
            "action_dispatched": task_dispatched,
            "task_note": task_note}


def receipt_line(verdict: dict[str, Any]) -> str:
    """为未确认的核对项生成目标明确的系统回执。"""
    if verdict.get("bound") is None or verdict.get("satisfied"):
        return ""
    lines = []
    if not verdict.get("op_satisfied", True):
        lines.append(
            f"本回合绑定的专用操作缺少成功回执（{verdict['detail']}），任务仍待执行。")
    _bound = verdict.get("bound") or {}
    if _bound.get("kind") == "task":
        _ts = verdict.get("task_state")
        _act = str(_bound.get("action") or "")
        _tgt = str(_bound.get("target") or "—")
        _tid = str(_bound.get("task_id") or "未名")
        if _ts == "missing":
            lines.append(
                f"本回合绑定的任务（{_tid}）欠动作 {_act}（目标 {_tgt}）"
                "的真实派发记录，任务仍待执行。")
        elif _ts == "unverified":
            _note = str(verdict.get("task_note") or "")
            lines.append(
                f"任务 {_tid} 的动作 {_act} 已派发但结果待核实"
                f"（{_note or '沿调用 ID与业务记录核查'}，不重发）。")
    for target, status in verdict.get("reports", {}).items():
        if status == "unverified":
            lines.append(f"向发令卡 {target} 的汇报结果待核实。")
        elif status == "failed":
            lines.append(f"汇报调用失败，未获得发令卡 {target} 的有效回执。")
        elif status == "not_sent":
            lines.append(f"本回合尚未向发令卡汇报（{target}）。")
    return "".join(f"\n\n〔系统回执〕{line}" for line in lines)


def tool_status_of(result: dict[str, Any]) -> str:
    """执行器信封 → 会话块结构化状态（存根投影的依据）。"""
    return "ok" if result.get("ok") is True else "error"


# ---------------------------------------------------------------------------
# 汇报唤醒回合（超长会话漏调专项 2026-11-19）
# ---------------------------------------------------------------------------

def is_report_wake(state: Any) -> bool:
    """本回合是否卡间**汇报/来信**唤醒（非指令）：研判类卡间消息。

    可信区分（2026-11-20 修）：指令唤醒走 source_kind="directive"
    （_h_send_card_message 投递 msg_directive 时的真实形态）——它有自己
    的绑定与回执面，不是「收到汇报」；其余 card_msg（msg_report/msg_sync/
    msg_notify/msg_ask…）都是研判类卡间消息，走汇报路径。
    """
    if str(getattr(state, "source_kind", "") or "") == "directive":
        return False
    src = getattr(state, "source", None)
    return isinstance(src, dict) and str(src.get("kind") or "") == "card_msg"


def report_wake_hint(endpoint_protocol: str) -> str:
    """汇报唤醒回合的两条路径提示（随实际端点协议措辞）。

    只说明「可以怎么做」，不授予任何权限、不预设模型该选哪条路径；
    调用参数以工具 schema 为准（schema 已在工具面注入，不重复罗列）。
    """
    if endpoint_protocol == "declared_text":
        channel = "通过本通道声明的正文调用格式发出实际调用"
    else:
        channel = "通过标准 tool_calls 通道发出实际调用"
    return (
        "〔汇报回合提示·系统〕你刚收到一条卡间汇报。管理卡可以只回复判断"
        "（零调用收口是合法的），也可以采取行动：需要查证、修改或下达指令时，"
        f"先{channel}，看到真实执行结果再在正文里汇报；"
        "正文里写「已发/已完成」不会产生任何执行，界面状态以系统结果为准。")


def _classify_tool(tool: str) -> str:
    """工具名 → 动作类别（读/发信/写）——只读清单是注册表的权威名单。"""
    try:
        from . import card_tools  # noqa: PLC0415 延迟破环
        if tool in card_tools.READONLY_TOOLS:
            return "read"
    except Exception:  # noqa: BLE001
        pass
    if tool in SEND_TOOLS:
        return "send"
    return "write"


def system_result_of(state: Any) -> dict[str, Any]:
    """本轮执行事实的系统结果（交付面呈现的唯一来源，执行器生成）。

    五种呈现（对准方案表）：
      none（无派发）→「本轮未执行工具」；
      读取/搜索 → 显示资源与结果（空匹配如实）；
      拒绝/失败 → 显示失败原因及目标；
      写入/发送落账 → 显示目标与真实记录标识（direct 带 msg_id）；
      落信未受理 →「已写入，投递未确认」。
    判据只读本回合 tool_events（执行记录），模型正文不读——正文写「已发」
    改变不了系统结果。返回 {"status", "items", "text"}。
    """
    events = [e for e in (getattr(state, "tool_events", None) or [])
              if isinstance(e, dict)]
    items: list[dict[str, Any]] = []
    for e in events:
        tool = str(e.get("tool") or "")
        target = str(e.get("card_id") or e.get("target") or "")
        cat = _classify_tool(tool)
        ok = e.get("ok") is True
        item: dict[str, Any] = {"tool": tool, "target": target,
                                "cat": cat, "ok": ok, "round": e.get("round")}
        if not ok:
            item["note"] = str(e.get("error_code") or "失败")
        elif cat == "send":
            # 发信呈现按**实际收件人逐条**（2026-11-20 二次返修）：
            #   已写入并受理（msg_id …）→ to    ｜ 落账且运行态已受理
            #   已写入，投递未确认（msg_id …）  ｜ 落账但未受理
            #   结果待核实（无原记录锚）        ｜ msg_id 缺席＝落账事实无法核实
            # msg_id＝原记录锚（append_card_message 成功才有）。
            d = e.get("send") or {}
            recips = [r for r in (d.get("recipients") or [])
                      if isinstance(r, dict)]
            if not recips:
                item["note"] = "结果待核实（无原记录锚）"
            else:
                parts_r: list[str] = []
                for r in recips:
                    to = str(r.get("to") or "")
                    mid = str(r.get("msg_id") or "")
                    st = str(r.get("state") or "")
                    if not mid:
                        parts_r.append(f"{to}：结果待核实（无原记录锚）")
                    elif st == "synced":
                        # sync 落账不唤醒（对方下次回合自然看到）——不是
                        # 「投递未确认」，同步语义就是写完即达。
                        parts_r.append(f"{to}：已写入，同步不唤醒（msg_id {mid}）")
                    elif st == "delivered":
                        parts_r.append(f"{to}：已写入并受理（msg_id {mid}）")
                    else:
                        parts_r.append(f"{to}：已写入，投递未确认（msg_id {mid}）")
                item["note"] = "；".join(parts_r)
        items.append(item)
    if not items:
        return {"status": "none", "items": [], "text": "本轮未执行工具"}
    ok_n = sum(1 for i in items if i["ok"])
    fail_n = len(items) - ok_n
    parts: list[str] = []
    for i in items:
        verb = {"read": "读取", "send": "发信", "write": "写入"}.get(i["cat"], "调用")
        if i["ok"]:
            parts.append(f"{verb} {i['tool']}({i['target'] or '—'})：{i.get('note') or '成功'}")
        else:
            parts.append(f"{verb} {i['tool']}({i['target'] or '—'})：失败（{i.get('note')}）")
    text = (f"本轮执行 {len(items)} 项：成功 {ok_n}" +
            (f"、失败 {fail_n}" if fail_n else "") + "。"
            + "；".join(parts))
    status = "partial" if fail_n and ok_n else ("fail" if fail_n else "done")
    return {"status": status, "items": items, "text": text}


def result_line(system_result: dict[str, Any]) -> str:
    """交付文本的系统结果行（与〔系统回执〕区分：这是本轮执行事实呈现，
    不判定绑定动作是否满足——研判回合零调用也正常交付）。"""
    return f"\n\n〔系统结果〕{system_result.get('text')}"


def remediation_prompt(state: Any, bound: dict[str, Any],
                       verdict: Optional[dict[str, Any]] = None) -> str:
    """定向补救续跳提示：**按实际欠项**生成——已派发/已受理的部分如实
    排除，绝不让模型重做已完成的动作（「任务已发 B、只欠汇报 A」时
    提示只欠 A 的汇报，不说 B 没收到）。

    ``verdict``＝本回合 ``receipt_verdict`` 结果：task_state 区分
    missing（真欠动作）/unverified（已派发判据未证，先核查不重发）/
    verified（动作无欠项）；reports 逐目标列欠汇报。verdict 缺席时
    回落绑定原文（旧调用面）。
    """
    kind = str(bound.get("kind") or "")
    v = verdict or {}
    debts: list[str] = []
    if kind == "task":
        act = str(bound.get("action") or "")
        tgt = str(bound.get("target") or "—")
        tid = str(bound.get("task_id") or "未名")
        rev = f" rev {bound['rev']}" if bound.get("rev") else ""
        ts = str(v.get("task_state") or "")
        if ts == "missing":
            debts.append(
                f"欠任务动作 {act}（目标 {tgt}，任务 {tid}{rev}"
                "——本回合无到目标的真实派发记录）")
        elif act and not v:
            debts.append(
                f"欠任务动作 {act}（目标 {tgt}，任务 {tid}{rev}"
                "——本回合无到目标的真实派发记录）")
        # verified/open/空 ⇒ 任务动作无欠项，不列
    elif kind == "directive":
        # directive 的欠项本体就是汇报——由下方 reports 逐目标行生成
        # （verdict 缺席时回落绑定目标静态行）。
        if not (v.get("reports")):
            tg = "、".join(bound.get("targets") or [])
            debts.append(f"欠向发令卡 {tg} 的汇报（本回合尚无送出记录）")
    elif kind == "op" and (not v or v.get("op_missing")):
        req = "/".join(sorted(bound.get("required_tools") or []))
        debts.append(f"欠绑定动作 {req}（本回合无派发记录）")
    # 汇报义务独立核对：逐目标列实际欠项（与任务动作欠项可并存；
    # directive 的欠项即汇报本体，由此行承载，不再另起静态行）。
    reports = v.get("reports") or {}
    if reports:
        already_named = (bound.get("target") if v.get("task_missing")
                         and bound.get("action") == "report_up" else None)
        owed = sorted(t for t, status in reports.items()
                      if status == "not_sent" and t != already_named)
        if owed:
            debts.append(
                "欠汇报：" + "、".join(owed) + "——本回合尚无送出记录")
    missing = "；".join(debts) or "本回合绑定的执行要求存在未满足项"
    include_task = kind == "task" and (not v or v.get("task_missing"))
    if bound.get("text") and include_task:
        missing += f"；原任务：{bound['text']}"
    if bound.get("accept") and include_task:
        missing += f"；判据：{bound['accept']}"
    if bound.get("session") and include_task:
        missing += f"；责任会话：{bound['session']}"
    done: list[str] = []
    for e in (getattr(state, "tool_events", None) or [])[-8:]:
        if not isinstance(e, dict):
            continue
        t = str(e.get("tool") or "")
        tgt = str(e.get("card_id") or e.get("target") or "")
        mark = ("成功" if e.get("ok") is True
                else f"失败（{e.get('error_code') or '原因未知'}）")
        done.append(f"{t}({tgt or '—'}){mark}")
    done_txt = "；".join(done) if done else "无"
    return (
        f"[系统提示] 本回合绑定了执行要求（{kind}），{missing}。"
        f"本轮已完成动作：{done_txt}（结果原文见轮内回执，勿重复执行）。"
        "请只补上面明确列出的欠项，通过当前通道的标准调用方式执行；"
        "执行结果以系统回执为准。"
        "若研判后确认无需执行，请明说理由并收口。")
