# -*- coding: utf-8 -*-
"""部署执行器（``then.deploy`` 的投递侧）。

[harness 第四手段 2026-11-01] message/inject/continue 都是"送话"；deploy 是
"派差事"——写一行差事账（function_deployments）并把职能带进场跑一轮。
机制层不管"派谁"，只管把策略文件里批过的差事**落成账、叫进场、留回执**：

  1. 解析职能（目录 code / fn_NNN 都行）→ ``identity_for`` 取人格（职责书）；
  2. 场授面 ``grant_for``＝挂载能力 ∩ 场型授面 → 本场实发工具清单；
  3. ``create_deployment`` 落账——**幂等**（同 function/venue_kind/venue_ref/
     batch_id/mandate 的在派差事已存在 ⇒ 返回既有行，重启重演不双开）；
  4. 唤起＝ ``routes._run_message`` 的 act_as 通道（与评审席位同一条路，
     不另造执行体系）：执行锚＝场卡会话与回合锁，身份锚＝席位；
  5. 回执＝ ``harness_message`` 事件带 ``action_id``——重启去重与审计共用。

两段式回执同 message+wake 口径：落账（持久）+ 唤起（易失）。差事行已建
但场叫不起来（停止/忙/模型挂）⇒ 动作记 failed，重启 resume 只补唤起不
重建账。差事本身的生命周期（办完关账）走 deployments 账与关账事件，
不在本执行器里跟踪——台账只保"这一派发没发出去"。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def execute(work_id: str, *, spec: dict[str, Any], venue: str,
                  mandate: str = "", action_id: str = "",
                  policy_id: str = "") -> dict[str, Any]:
    """执行一次 deploy：解职能 → 落差事账 → 激活 → 带 mandate 进场跑一轮。

    返回 ``{"ok": bool, "deployment_id": int, "reason": str, ...}``；
    ``ok=False`` 由台账标 failed（resume 会重试——差事账幂等，重复执行
    只补唤起）。任何一步都不许抛：失败本身是回执的一部分。
    """
    from . import event_stream, function_room, routes  # 延迟：反向引用链
    from ..storage import function_store

    venue = str(venue or "").strip()
    fn_key = str(spec.get("function_id") or spec.get("function") or "").strip()
    vk = str(spec.get("venue_kind") or "deploy").strip() or "deploy"
    receipt: dict[str, Any] = {"ok": False, "deployment_id": 0,
                             "function": fn_key, "venue_kind": vk,
                             "venue": venue}
    if not venue:
        receipt["reason"] = "no_venue"
        return receipt
    if not fn_key:
        receipt["reason"] = "no_function"
        return receipt

    # ---- 1. 人格与授面 ------------------------------------------------
    try:
        ident = function_room.identity_for(work_id, fn_key, vk)
    except Exception as exc:  # noqa: BLE001 供给侧坏了记失败不抛
        logger.warning("[deploy] identity_for 失败 %s: %s", fn_key, exc)
        ident = {}
    fn_id = str(ident.get("function_id") or "")
    # identity_for 对查不到的职能回显输入串——做实存校验，不认"解析成功"假象。
    try:
        exists = bool(fn_id) and function_store.function_exists(work_id, fn_id)
    except Exception:  # noqa: BLE001 查不了按不存在处理（宁缺勿滥）
        exists = False
    if not exists:
        receipt["reason"] = "function_resolve_failed"
        return receipt
    receipt["function_id"] = fn_id
    try:
        tools = sorted(set(function_room.grant_for(work_id, fn_id, vk) or []))
    except Exception as exc:  # noqa: BLE001 授面算不出 ⇒ 空面进场（宁缺勿滥）
        logger.warning("[deploy] grant_for 失败 %s: %s", fn_id, exc)
        tools = []

    # ---- 2. 差事文 -----------------------------------------------------
    text = str(mandate or "").strip()
    if not text:
        tpl = str(spec.get("mandate_template") or vk)
        try:
            text = function_room.render_mandate(
                tpl, function=fn_id,
                review_card_id=venue, batch=str(spec.get("batch_id") or ""),
                target=str(spec.get("target_card") or venue),
                delegator=f"harness:{policy_id}", question="")
        except Exception:  # noqa: BLE001 模板坏了用原文兜底
            text = ""
    if not text:
        text = str(spec.get("mandate") or "").strip()
    if not text:
        receipt["reason"] = "no_mandate"
        return receipt

    # ---- 3. 落账（幂等）-------------------------------------------------
    try:
        dep = await function_store.create_deployment(
            work_id, fn_id, venue_kind=vk, venue_ref=venue,
            mandate=text, delegator=f"harness:{policy_id}",
            batch_id=str(spec.get("batch_id") or ""),
            target_card=str(spec.get("target_card") or venue),
            tools=tools,
            exit_kind=str(spec.get("exit_kind") or "explicit"),
            exit_spec=str(spec.get("exit_spec") or ""),
            venue_label=str(spec.get("venue_label") or ""),
            meta={"policy_id": policy_id, "action_id": action_id})
    except Exception as exc:  # noqa: BLE001 落账失败＝义务没接住，记 failed
        logger.warning("[deploy] 落账失败 fn=%s venue=%s: %s", fn_id, venue, exc)
        receipt["reason"] = "deploy_create_failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        return receipt
    if not dep or not dep.get("id"):
        receipt["reason"] = "deploy_create_failed"
        return receipt
    did = int(dep["id"])
    receipt["deployment_id"] = did
    try:
        await function_store.activate_deployment(work_id, did)
    except Exception as exc:  # noqa: BLE001 状态位落不上不阻断唤起
        logger.warning("[deploy] 激活失败 dep=%s: %s", did, exc)

    # ---- 4. 唤起：act_as 场席位（wake=False ⇒ 只落账，不叫人）-------------
    if not bool(spec.get("wake", True)):
        receipt["ok"] = True
        receipt["reason"] = "deployed_no_wake"
        _emit_receipt(work_id, venue, policy_id, action_id, did, receipt)
        return receipt
    ctx_card = str(spec.get("ctx") or venue)
    result: Any = None
    err = ""
    try:
        result = await routes._run_message(
            work_id, venue, text,
            append_user=False,
            source={"kind": "deploy", "deployment_id": did,
                    "policy": policy_id, "action_id": action_id},
            act_as=fn_id, act_meta=ident, ctx_card=ctx_card,
            tool_allow=set(tools))
    except Exception as exc:  # noqa: BLE001 唤起异常＝一次没落，差事账还在
        err = f"{type(exc).__name__}: {exc}"
        logger.warning("[deploy] 唤起异常 dep=%s venue=%s: %s", did, venue, exc)
    ok = isinstance(result, dict) and not result.get("error")
    if isinstance(result, dict) and result.get("error"):
        err = str((result.get("error") or {}).get("message")
                  or result.get("error"))
    receipt["ok"] = ok
    receipt["reason"] = "woken" if ok else "wake_failed"
    if err:
        receipt["error"] = err
    _emit_receipt(work_id, venue, policy_id, action_id, did, receipt)
    return receipt


def _emit_receipt(work_id: str, venue: str, policy_id: str,
                  action_id: str, deployment_id: int,
                  receipt: dict[str, Any]) -> None:
    """部署回执事件：``already_delivered`` 按 action_id 查重，审计按 dep_id 追差事。"""
    try:
        from . import event_stream
        event_stream.emit(work_id, venue, "harness_message", {
            "rule_id": str(policy_id or ""), "to": venue,
            "wake": bool(receipt.get("ok")),
            "action_id": str(action_id or ""), "mode": "deploy",
            "deployment_id": int(deployment_id or 0),
            "reason": str(receipt.get("reason") or ""),
            "error": str(receipt.get("error") or ""),
        })
    except Exception as exc:  # noqa: BLE001 回执事件失败不吞主结果
        logger.warning("[deploy] 回执事件失败 dep=%s: %s",
                       deployment_id, exc)
