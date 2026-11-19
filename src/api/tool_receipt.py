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

# 卡间通信类工具（绑定动作=「发指令」的机械判据集合）。
SEND_TOOLS: frozenset[str] = frozenset({
    "direct", "sync", "report_up", "msg_report", "send_card_message",
})

# 专用操作回合（op）各自要求成功的专用工具。
OP_REQUIRED_TOOLS: dict[str, frozenset[str]] = {
    "update_status": frozenset({"propose_status"}),
    "requirements_final": frozenset({"edit_requirements"}),
    "requirements_draft": frozenset({"edit_requirements"}),
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
    """当前轮短提示消息（随 turn_tail 下发，紧贴最新请求）。

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
    return {"role": "assistant", "content": text}


# ---------------------------------------------------------------------------
# 绑定动作与回执闸
# ---------------------------------------------------------------------------

def bound_action(state: Any) -> Optional[dict[str, Any]]:
    """本回合的**可验证执行要求**（机械判据，最小可信集）。

    「可信的任务步骤、专用操作和明确的结构化参数」三源：
      1. 专用操作回合（state.op）——按钮注入的专用任务，要求对应专用工具成功；
      2. 指令唤醒回合（source_kind="directive"）——卡间 direct 唤醒，
         要求卡间通信类工具成功；
      3. 板步在跑（活板当前步）——已有 await_step_tool 催促通道，不在此重复。

    普通话术（「去查一下」「改完告诉我」）**不**在此判定——系统无法仅凭
    一段自然语言可靠判定口头要求；那部分交给短提示对照实验去衡量。

    返回 None ＝ 本回合没有已绑定动作（收口不核对）。
    """
    op = str(getattr(state, "op", "") or "")
    required = OP_REQUIRED_TOOLS.get(op)
    if required:
        return {"kind": "op", "op": op, "required_tools": sorted(required)}
    if str(getattr(state, "source_kind", "") or "") == "directive":
        return {"kind": "directive", "required_tools": sorted(SEND_TOOLS)}
    return None


def receipt_verdict(state: Any) -> dict[str, Any]:
    """收口核对：绑定动作的回执事实（工具已派发 / 返回成功 / 结果已发生）。

    判据只读本回合 tool_events（执行记录），不读模型正文——模型自述
    不算回执。返回 {"bound": …|None, "dispatched": n, "ok": n,
    "satisfied": bool, "detail": …}；bound=None 时 satisfied 恒 True
    （没有要求就没有不满足）。
    """
    bound = bound_action(state)
    events = [e for e in (getattr(state, "tool_events", None) or [])
              if isinstance(e, dict)]
    dispatched = len(events)
    ok_events = [e for e in events if e.get("ok") is True]
    if bound is None:
        return {"bound": None, "dispatched": dispatched,
                "ok": len(ok_events), "satisfied": True, "detail": ""}
    required = {str(n) for n in bound.get("required_tools") or []}
    matched_ok = [e for e in ok_events if str(e.get("tool") or "") in required]
    satisfied = bool(matched_ok)
    detail = (f"要求工具 {sorted(required)} 中有成功回执"
              f"（派发 {dispatched} 次、成功 {len(ok_events)} 次、"
              f"匹配成功 {len(matched_ok)} 次）")
    return {"bound": bound, "dispatched": dispatched,
            "ok": len(ok_events), "satisfied": satisfied, "detail": detail}


def receipt_line(verdict: dict[str, Any]) -> str:
    """交付文本的系统回执行（虚报成功的对账锚——刷历史也能看见）。"""
    if verdict.get("bound") is None or verdict.get("satisfied"):
        return ""
    kind = str((verdict.get("bound") or {}).get("kind") or "")
    return (
        f"\n\n〔系统回执〕本回合绑定了执行要求（{kind}），"
        f"但执行记录中无匹配的成功回执（{verdict.get('detail')}）。"
        "上文正文中的完成表述未经执行通道证实，任务仍待执行。")


def tool_status_of(result: dict[str, Any]) -> str:
    """执行器信封 → 会话块结构化状态（存根投影的依据）。"""
    return "ok" if result.get("ok") is True else "error"
