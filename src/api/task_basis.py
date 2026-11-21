# -*- coding: utf-8 -*-
"""按原派工编号绑定生效条款，在模型请求入口装配任务依据。"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ..storage import card_db, task_store
from . import doc_locator

#: ECO_TASK_BASIS=0 暂停全部试点。工作域须在单独的试点名单内。
_SWITCH_ENV = "ECO_TASK_BASIS"


def _switch_on() -> bool:
    return os.environ.get(_SWITCH_ENV, "1") != "0"


def enabled(work_id: str) -> bool:
    allow = os.environ.get("ECO_TASK_BASIS_WORKS", "")
    return _switch_on() and work_id in {w.strip() for w in allow.split(",")}


def dispatch_spec(work_id: str, issuer: str, basis: Any) -> dict[str, Any]:
    """校验试点派工的验收条件和生效条款，再允许落信。"""
    if not enabled(work_id) or not isinstance(basis, dict):
        raise ValueError("此 work 未启用任务依据试点，或 task_basis 格式无效")
    anchors = basis.get("clauses")
    allowed = basis.get("allowed")
    deferred = basis.get("deferred", [])
    if not all(isinstance(items, list) and all(isinstance(s, str) and s.strip()
               for s in items) for items in (anchors, allowed, deferred)):
        raise ValueError("clauses、allowed、deferred 必须是字符串列表")
    doc = requirements_text(work_id, issuer)
    if not anchors or not allowed or set(_clause_map_of(doc, anchors)) != set(anchors):
        raise ValueError("请绑定发令卡生效正稿中存在的条款，并明确允许步骤")
    from . import task_evidence
    criteria = task_evidence.validate_criteria(basis.get("criteria"))
    return {"req_card": issuer, "doc_text": doc, "clauses": anchors,
            "allowed": allowed, "deferred": deferred, "criteria": criteria}


def requirements_text(work_id: str, req_card: str) -> str:
    """需求正稿正文（``cards.requirements`` 列；取不到 ⇒ 空串，调用方判）。"""
    if not req_card:
        return ""
    row = card_db.read_sync(
        work_id,
        lambda conn: conn.execute(
            "SELECT requirements FROM cards WHERE card_id=?",
            (req_card,)).fetchone(),
        None)
    return str(row["requirements"] or "") if row else ""


def _clause_map_of(text: str, anchors: list[str]) -> dict[str, dict[str, str]]:
    """条款号 → ``{r: 行段, fp: 内容指纹}``（``locate_clauses`` 返 DocRange 表）。

    改版判据比**内容指纹**而不是行坐标——兄弟条款删增导致的平移不算
    受影响（内容一字未动），条款丢锚或正文变了才算。
    """
    if not text or not anchors:
        return {}
    out: dict[str, dict[str, str]] = {}
    for r in doc_locator.locate_clauses(text, anchors):
        if getattr(r, "start", -1) < 0:
            continue
        out[r.anchor] = {"r": f"{r.start}-{r.end}",
                         "fp": doc_locator.doc_fingerprint(
                             str(getattr(r, "text", "") or ""))}
    return out


def register_dispatch(work_id: str, *, owner_card: str,
                      req_card: str = "", clauses: Optional[list[str]] = None,
                      allowed: Optional[list[str]] = None,
                      deferred: Optional[list[str]] = None,
                      accept: str = "",
                      criteria: Optional[list[Any]] = None,
                      target_card: str = "", dispatch_id: str = "",
                      source_msg_id: str = "", task_id: str = "",
                      doc_text: str = "") -> dict[str, Any]:
    """派工登记：按现状需求算版本指纹＋条款定位，落任务账。

    ``doc_text`` 缺席 ⇒ 自己读 ``req_card`` 的正稿（隔离测试可直喂）。
    """
    text = doc_text if doc_text else requirements_text(work_id, req_card)
    anchors = [str(c) for c in (clauses or []) if str(c or "")]
    fp = doc_locator.doc_fingerprint(text) if text else ""
    clause_map = _clause_map_of(text, anchors)
    return task_store.register_task(
        work_id, owner_card=owner_card, task_id=task_id,
        dispatch_id=dispatch_id, source_msg_id=source_msg_id,
        req_card=req_card, req_fp=fp, clause_map=clause_map,
        clauses=anchors, allowed=allowed, deferred=deferred,
        accept=accept, criteria=criteria, target_card=target_card)


def _task_id_of(scope_hint: dict[str, Any]) -> str:
    """本轮的任务身份：可信入口字段（source.task / task_binding），不读正文。"""
    src = dict(scope_hint.get("source") or {})
    task = src.get("task")
    if isinstance(task, dict):
        tid = str(task.get("task_id") or task.get("id") or "").strip()
        if tid:
            return tid
    b = scope_hint.get("task_binding")
    if isinstance(b, dict):
        return str(b.get("task_id") or "").strip()
    return ""


def basis_text(work_id: str, task: dict[str, Any]) -> str:
    """组装生效条款摘录、回读位置及交付判据。"""
    req_card = str(task.get("req_card") or "")
    text = requirements_text(work_id, req_card)
    lines = [f"〔任务依据 · {task['task_id']}〕"]
    if task.get("dispatch_id"):
        lines.append(f"原派工：{task['dispatch_id']}"
                     + (f"（原消息 {task['source_msg_id']}）"
                        if task.get("source_msg_id") else ""))
    lines.append(f"责任卡：{task['owner_card']}（本卡）")
    if req_card:
        cur_fp = doc_locator.doc_fingerprint(text) if text else ""
        ver = task.get("req_fp") or "未知"
        if not cur_fp or cur_fp != ver:
            raise ValueError("生效正稿与任务版本不一致")
        lines.append(f"需求：{req_card} 正稿 v{ver}")
        anchors = list(task.get("clauses") or [])
        if anchors:
            located = ({r.anchor: r
                        for r in doc_locator.locate_clauses(text, anchors)}
                       if text else {})
            refs: list[str] = []
            missing: list[str] = []
            for a in anchors:
                r = located.get(a)
                if r is not None and getattr(r, "start", -1) >= 0:
                    excerpt = str(r.text or "").strip()
                    if len(excerpt) > 480:
                        excerpt = excerpt[:480] + "…"
                    refs.append(f"{a}（行{r.start}-{r.end}）\n{excerpt}")
                else:
                    missing.append(a)
            if refs:
                lines.append("适用条款：" + "、".join(refs)
                             + doc_locator.range_read_hint(
                                 req_card, "requirements", 0, 0))
            if missing:
                lines.append("待对齐条款：" + "、".join(missing)
                             + "（请沿原派工回查，交付核证保留待核实）")
    if task.get("allowed"):
        lines.append("当前允许：" + "、".join(str(x) for x in task["allowed"]))
    if task.get("deferred"):
        lines.append("暂缓事项：" + "、".join(str(x) for x in task["deferred"]))
    if task.get("accept"):
        lines.append(f"验收判据：{task['accept']}")
    for item in task.get("criteria") or []:
        if isinstance(item, dict):
            lines.append(f"- {item['id']}：{item['text']}")
            evidence = item.get("evidence") or {}
            if evidence:
                lines.append(f"  核证探针 {evidence['tool']} "
                             + json.dumps(evidence["arguments"], ensure_ascii=False))
    lines.append("交付时按各判据挂宿主返回的证据编号，调用 task_deliver。")
    return "\n".join(lines)


def bound_task(work_id: str, card_id: str,
               scope_hint: dict[str, Any] | None) -> Optional[dict[str, Any]]:
    """可信任务绑定只作用于本卡的主会话。"""
    if not enabled(work_id) or not scope_hint:
        return None
    if scope_hint.get("act_as") or scope_hint.get("ctx_card"):
        return None
    task_id = _task_id_of(scope_hint)
    if not task_id:
        return None
    task = task_store.get_task(work_id, task_id)
    if not task or card_id != task["owner_card"] or task["status"] == "closed":
        return None
    return align_task(work_id, task)


def align_task(work_id: str, task: dict[str, Any]) -> dict[str, Any]:
    text = requirements_text(work_id, task["req_card"])
    new_fp = doc_locator.doc_fingerprint(text) if text else ""
    if not new_fp or new_fp == task["req_fp"]:
        return task
    anchors = task["clauses"]
    new_map = _clause_map_of(text, anchors)
    affected = any(_clause_digest(new_map.get(a))
                   != _clause_digest(task["clause_map"].get(a)) for a in anchors)
    updates = {"status": "realign", "inject_state": "pending"} if affected else {}
    task_store.set_task_state(work_id, task["task_id"], req_fp=new_fp,
                              clause_map=new_map, expect_fp=task["req_fp"], **updates)
    return task_store.get_task(work_id, task["task_id"]) or task


def _clause_digest(entry: Any) -> str:
    return str(entry.get("fp") or "") if isinstance(entry, dict) else ""


def realign_check(work_id: str, req_card: str = "") -> dict[str, Any]:
    """需求换版重排队：逐任务比对登记定位与**新版**定位。

    - 条款内容变了或丢了 ⇒ ``status='realign' + inject_state='pending'``
      （下一轮该任务的回合重注入新版依据）；
    - 条款内容不变 ⇒ 只更新版本和位置，原任务继续；
    - 正稿读不出 ⇒ 不动（读不出≠改版，误判重排队的代价是错对齐）。
    """
    out = {"checked": 0, "realigned": [], "unaffected": []}
    for task in task_store.open_tasks(work_id):
        if not task.get("req_card") or req_card and req_card != task["req_card"]:
            continue
        out["checked"] += 1
        aligned = align_task(work_id, task)
        if aligned["req_fp"] != task["req_fp"]:
            key = "realigned" if aligned["status"] == "realign" and \
                aligned["inject_state"] == "pending" else "unaffected"
            out[key].append(task["task_id"])
    return out
