# -*- coding: utf-8 -*-
"""B 批任务核证。正文只提供索引，证据由宿主工具结果或卡间事件给出。"""
from __future__ import annotations

import json
import re
from typing import Any

from ..storage import card_db, event_log, task_store
from . import task_basis

PASSED = "passed"
FAILED = "failed"
PENDING = "pending"
UNCOVERED = "uncovered"

_ANCHOR_RE = re.compile(r"^(rc|seq|msg):(.+)$")


def _clause_version(task: dict[str, Any]) -> dict[str, str]:
    return {key: str(val["fp"]) for key, val in (task.get("clause_map") or {}).items()
            if isinstance(val, dict) and val.get("fp")}


def validate_criteria(raw: Any) -> list[dict[str, Any]]:
    """试点只核验有明确预期输出的宿主执行探针。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("请给至少一项可核证的结构化判据")
    keys: set[str] = set()
    probes: set[tuple[str, str]] = set()
    criteria = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("判据必须是对象")
        key = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        evidence = item.get("evidence")
        if not key or not text or key in keys or not isinstance(evidence, dict):
            raise ValueError("每项判据需要唯一 id、说明和 evidence")
        tool, args = evidence.get("tool"), evidence.get("arguments")
        expected = evidence.get("stdout")
        param = {"python": "code", "bash": "command"}.get(str(tool))
        if (not param or not isinstance(args, dict) or set(args) != {param}
                or not isinstance(args[param], str) or not args[param].strip()
                or not isinstance(expected, str) or not expected.strip()):
            raise ValueError("证据判据需要指定 bash/python、确切入参和非空预期 stdout")
        probe = (str(tool), args[param])
        if probe in probes:
            raise ValueError("不同判据需要各自的核证探针")
        keys.add(key)
        probes.add(probe)
        criteria.append({"id": key, "text": text,
                         "evidence": {"tool": tool, "arguments": args, "stdout": expected}})
    return criteria


def proof_for_result(task: dict[str, Any], tool: str, arguments: dict[str, Any],
                     result: dict[str, Any], origin: str) -> dict[str, Any] | None:
    """只由执行器在实际工具完成后为对应判据盖章。"""
    if origin not in ("native", "declared_text"):
        return None
    for criterion in task.get("criteria") or []:
        spec = criterion.get("evidence") or {}
        if spec.get("tool") != tool or spec.get("arguments") != arguments:
            continue
        data = result.get("data") or {}
        ok = (result.get("ok") is True and data.get("exit_code") == 0
              and not data.get("truncated")
              and str(data.get("stdout") or "").strip() == spec["stdout"].strip())
        return {"task_id": task["task_id"], "req_fp": task["req_fp"],
                "target": task["target_card"], "criterion": criterion["id"],
                "clauses": _clause_version(task), "tool": tool, "ok": ok}
    return None


def resolve_anchor(work_id: str, card_id: str, anchor: str, *,
                   target: str = "") -> dict[str, Any]:
    m = _ANCHOR_RE.fullmatch(str(anchor or "").strip())
    if not m:
        return {"found": False, "why": "证据编号格式无效"}
    kind, ref = m.group(1), m.group(2).strip()
    if kind == "msg":
        if target:
            for event in reversed(event_log.timeline(work_id, target)):
                data = event.get("data") or {}
                if (event["type"] in {"msg_report", "worker_done", "msg_directive", "msg_sync"}
                        and data.get("msg_id") == ref):
                    return {"found": True, "ref": f"msg:{ref}", "target": target,
                            "from": data.get("from"), "reply_to": data.get("reply_to"),
                            "why": "消息已落账，执行结果须查对应工具或运行记录"}
        return {"found": False, "why": "当前目标的事件账找不到该消息"}

    def find(conn: Any) -> Any:
        if ref.isdecimal():
            return conn.execute("SELECT seq, type, data FROM session_blocks"
                                " WHERE card_id=? AND seq=?",
                                (card_id, int(ref))).fetchone()
        if kind == "rc" and ref.startswith("call_"):
            return conn.execute("SELECT seq, type, data FROM session_blocks"
                                " WHERE card_id=? AND type='tool_result'"
                                " AND json_extract(data,'$.tool_call_id')=?",
                                (card_id, ref)).fetchone()
        return None

    row = card_db.read_sync(work_id, find)
    if not row:
        return {"found": False, "why": "找不到原会话记录"}
    try:
        data = json.loads(row["data"] or "{}")
        proof = data.get("task_proof")
        if isinstance(proof, str):
            proof = json.loads(proof)
    except (TypeError, ValueError, AttributeError):
        proof = None
    return {"found": True, "ref": f"{card_id}#seq{row['seq']}",
            "proof": proof if row["type"] == "tool_result" else None}


def judge(task: dict[str, Any], claims: list[dict[str, Any]],
          resolved: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_criterion = {str(claim.get("criterion") or ""): claim for claim in claims}
    verdicts = []
    for criterion in task.get("criteria") or []:
        item = criterion if isinstance(criterion, dict) else {"id": str(criterion), "text": str(criterion)}
        key = str(item.get("id") or "")
        entry = {"criterion": key, "text": item.get("text", "")}
        claim = by_criterion.get(key)
        if claim is None:
            verdicts.append({**entry, "verdict": UNCOVERED, "why": "无证据认领"})
            continue
        anchor = str(claim.get("anchor") or "").strip()
        rec = resolved.get(anchor) or {}
        proof = rec.get("proof")
        entry.update(anchor=anchor, ref=rec.get("ref", ""))
        if not anchor or not rec.get("found") or not isinstance(proof, dict):
            verdict, why = PENDING, rec.get("why") or "没有宿主核证的执行凭据"
        elif (proof.get("target") != task["target_card"]
              or proof.get("task_id") != task["task_id"] or proof.get("criterion") != key):
            verdict, why = FAILED, "凭据的目标、任务或判据不匹配"
        elif (not proof.get("req_fp") or not task["req_fp"]
              or set(task["clauses"]) != set(_clause_version(task))
              or proof.get("clauses") != _clause_version(task)):
            verdict, why = PENDING, "凭据版本与当前生效依据不一致"
        elif proof.get("ok") is not True:
            verdict, why = FAILED, "实际执行结果未满足派工预期"
        else:
            verdict, why = PASSED, "宿主执行记录匹配任务、版本与验收判据"
        verdicts.append({**entry, "verdict": verdict, "why": why})
    return verdicts


def deliver_task(work_id: str, card_id: str, task_id: str,
                 claims: list[dict[str, Any]]) -> dict[str, Any]:
    task = task_store.get_task(work_id, task_id)
    if task is None:
        return {"ok": False, "error": "not_found", "why": "任务账不存在"}
    if task["owner_card"] != card_id or task["status"] == "closed":
        return {"ok": False, "error": "denied", "why": "任务未授权给当前执行卡"}
    task = task_basis.align_task(work_id, task)
    anchors = {str(cl.get("anchor") or "").strip() for cl in claims}
    resolved = {a: resolve_anchor(work_id, card_id, a, target=task["target_card"])
                for a in anchors if a}
    verdicts = judge(task, claims, resolved)
    status = (PASSED if verdicts and all(v["verdict"] == PASSED for v in verdicts)
              else FAILED if any(v["verdict"] == FAILED for v in verdicts)
              else PENDING)
    delivery = task_store.record_delivery(
        work_id, task_id=task_id, card_id=card_id, claims=claims,
        verdicts=verdicts, status=status, req_fp=task["req_fp"])
    if delivery is None:
        return {"ok": False, "error": "unverified", "why": "交付时任务依据变化或台账不可用"}
    return {"ok": True, "delivery_id": delivery["delivery_id"], "task_id": task_id,
            "status": status, "req_fp": task["req_fp"], "verdicts": verdicts}


def task_status(work_id: str, ref: str) -> dict[str, Any]:
    task = task_store.by_dispatch(work_id, ref) or task_store.get_task(work_id, ref)
    if task is None:
        return {"found": False, "ref": str(ref)}
    text = task_basis.requirements_text(work_id, task["req_card"])
    current_fp = task_basis.doc_locator.doc_fingerprint(text) if text else ""
    delivery = task_store.get_delivery(work_id, task["task_id"])
    if delivery:
        delivery["current_version"] = bool(current_fp and delivery.get("req_fp") == current_fp)
    return {"found": True, "task": task, "delivery": delivery}
