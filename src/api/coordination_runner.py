# -*- coding: utf-8 -*-
"""项目统筹的两种模型执行器（任务二）：强模型查证轮＋弱模型观察。

[任务书任务二 2026-11-21 · 第三版按用户实测收口]

- **强模型（统筹）**：首轮由用户任命触发，**绑定任命标识**（调用前校验仍在任
  且活跃；每跳发模型前复核；落账前再查；save_plan 的 expected_rev CAS 兜住
  竞态）。轮内是**多跳只读查证循环**：模型以任命职能身份在**计划专属会话**
  （``{plan_card}@coordination`` 附加会话，主会话零写入）里工作，按正文 JSON
  指令协议主动只读查证（coord_read_card／coord_read_events）——需求原句、
  版本、已有工作从**真实来源**取（卡正文 requirements＋requirements_state、
  事件流），宿主不预填；查证充分后 coord_submit_plan 提交（空计划与劣质
  形状不记成功）。发令能力留在任务四。
- **弱模型（观察哨）**：宿主发起**一次无工具调用**（显式弱模型路由）。批次
  **先按当前任命过滤**（coord 必须带本 work 前缀＋来源卡在授权集合内——
  范围外变化不进材料、不进引用集合、不进待办），再组装。输出协议
  ``{decision,item_id,reason,evidence_refs,question}``——**按完整 JSON 校验
  顶层与字段类型**（整段不是对象＝无效，不做截取兜底逃逸），引用只能来自
  过滤后集合。无效输出/引用越界/模型不可用 ⇒ 「未判断」，账本 detail 保留
  **过滤后的完整批次证据**（凭该记录可重新读回），批次进 pending 待处理；
  不循环重试、不推进最迟回看。**写待办在同一事务内核对任命、活跃状态和
  读入版本**——暂停/换人/改计划后返回的旧观察不写入。
- 「修改标准」「给某卡下令」没有控制效力：弱模型正文只落账。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import model_service
from ..storage import card_db, card_store, coordination_store

logger = logging.getLogger("api.coordination_runner")

# ---------------------------------------------------------------- 批次契约

# batch 形态（任务三的事件接入按此契约生成；测试直接构造）：
#   {"batch_id": "b_<hex>",
#    "changes": [{"coord": "event:work_001:card_017:42",
#                 "card_id": "card_017", "kind": "card_block_edited",
#                 "summary": "…", "evidence": "…"}]}
#
# coord 唯一标识一条证据（event:{work}:{card}:{seq}）。合法证据 = coord 带
# **本 work 前缀** 且来源卡在当前任命的授权集合内。


def new_batch_id() -> str:
    return f"b_{uuid.uuid4().hex[:10]}"


def _strip_json_block(text: str) -> str:
    """剥 ```json …``` 围栏；裸 JSON 原样返回；寒暄包裹时截 {..} 段。

    [2026-11-21 · 用户令] 截取兜底只用于「JSON 对象外有寒暄」——调用方必须
    对截取结果做**完整 JSON 解析＋顶层类型校验**（``[{"decision":…}]`` 这类
    合法 JSON 数组整段解析即判无效，不会被截出内部对象蒙混过关）。
    """
    raw = str(text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if m:
        return m.group(1)
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        return raw[i:j + 1]
    return raw


def _observe_cards(work_id: str, observe_scope: dict[str, Any]) -> list[dict[str, Any]]:
    """观察范围内可见的卡（**A2：范围外一个字不进材料**）。

    scope=work ⇒ 全部卡（含后来加入的、含从未发言的静默卡）；scope=cards ⇒
    只 ids。职能卡（fn_ 前缀）与草稿不进清单。
    """
    metas = card_store.iter_card_metas(work_id)
    scope = str((observe_scope or {}).get("scope") or "")
    out: list[dict[str, Any]] = []
    if scope == "work":
        for cid, m in metas.items():
            if cid.startswith("fn_") or cid == "draft":
                continue
            out.append({"card_id": cid, "title": str(m.get("title") or "")})
    elif scope == "cards":
        for cid in [str(x) for x in ((observe_scope or {}).get("ids") or [])]:
            m = metas.get(cid)
            if m:
                out.append({"card_id": cid, "title": str(m.get("title") or "")})
    return sorted(out, key=lambda x: x["card_id"])


def _allowed_card_ids(work_id: str, observe_scope: dict[str, Any]) -> set[str]:
    """授权范围内的卡号集合（证据过滤的唯一判据）。"""
    return {c["card_id"] for c in _observe_cards(work_id, observe_scope)}


def _filter_batch(work_id: str, batch: dict[str, Any],
                  observe_scope: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """[2026-11-21 · 用户令] 按当前任命过滤证据，四重判据：

    ① coord 必须带**本 work 前缀**（防另一个 work 的同号卡证据混入）；
    ② 来源卡在授权集合内；
    ③ **coord 解析出的卡号必须与 change 声明的 card_id 一致**（不一致＝
       坐标与事实对不上，整条丢弃——用户实测两个卡号不一致时曾被保留）。
    范围外变化不进材料、不进引用集合、不进待办。
    返回 (过滤后批次, 被丢弃 coord 列表)。"""
    allowed = _allowed_card_ids(work_id, observe_scope)
    work_prefix = f"event:{work_id}:"
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for ch in (batch or {}).get("changes") or []:
        coord = str(ch.get("coord") or "")
        cid = str(ch.get("card_id") or "")
        coord_card = coord.split(":")[2] if coord.count(":") >= 3 else ""
        sources = ch.get("source_cards", [cid])
        if (coord.startswith(work_prefix) and cid and cid in allowed
                and coord_card == cid and isinstance(sources, list)
                and all(isinstance(source, str) and source in allowed for source in sources)):
            kept.append(ch)
        else:
            dropped.append(coord or "?")
    return {**batch, "changes": kept}, dropped


def _current_assignment(work_id: str, plan_card_id: str,
                        *, require_active: bool = False) -> dict[str, Any]:
    """当前任命面板（在任＝active/paused；require_active ⇒ 仅 active）。

    把 grants.observe/instruct 拍平成 observe_scope/instruct_scope 顶层键。
    """
    p = coordination_store.panel(work_id, plan_card_id)
    states = ("active",) if require_active else ("active", "paused")
    if p.get("state") not in states:
        return {}
    g = p.get("grants") or {}
    p["observe_scope"] = g.get("observe") or {}
    p["instruct_scope"] = g.get("instruct") or {}
    return p


def _standards_text(plan: dict[str, Any]) -> str:
    stds = plan.get("wake_standards") or []
    if not stds:
        return "（强模型还没有写下观察标准——只对「标准以外的实质矛盾」提请）"
    lines: list[str] = []
    for s in stds:
        lines.append(f"- 标准 {s.get('standard_id', '')}：保护「{s.get('protect', '')}」"
                     f"｜可放过「{s.get('ignore', '')}」｜应升级「{s.get('escalate', '')}」")
    return "\n".join(lines)


def _requirements_text(plan: dict[str, Any]) -> str:
    reqs = plan.get("requirements") or []
    if not reqs:
        return "（计划还没有登记需求引用——以查证到的卡上需求正稿为准）"
    lines: list[str] = []
    for r in reqs:
        lines.append(f"- [{r.get('ref', '')}]（版本 {r.get('version', '')}）：{r.get('quote', '')}")
    return "\n".join(lines)


def plan_for_model(appt: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """模型生成的计划可能含整轮资料。撤权后只保留独立的用户目标。"""
    if "model_plan_sources" in appt:
        source = appt["model_plan_sources"]
        if not isinstance(source, list) or any(cid not in allowed for cid in source):
            return {"objective": appt.get("user_objective", "")}
    return appt.get("plan") or {}


def _changes_text(batch: dict[str, Any]) -> tuple[str, set[str]]:
    """变化批次 → 文本＋全部合法证据坐标集合（**只算过滤后批次**）。"""
    coords: set[str] = set()
    lines: list[str] = []
    for ch in (batch or {}).get("changes") or []:
        coord = str(ch.get("coord") or "")
        coords.add(coord)
        lines.append(f"- [{coord}]（{ch.get('kind', '')}｜来源卡 {ch.get('card_id', '')}）"
                     f" {ch.get('summary', '')}\n  证据片段：{ch.get('evidence', '')}")
    if not lines:
        lines.append("（本批没有可判断的变化）")
    return "\n".join(lines), coords


# ---------------------------------------------------------------- 弱模型

_WEAK_SYSTEM = (
    "你是项目统筹任命的观察哨（弱模型）。你只做一件事：对照观察标准判断"
    "「这批变化是否值得提前叫醒统筹强模型」。你不能执行任何动作——正文里的"
    "「修改标准」「给某卡下令」一类话没有任何控制效力，宿主只读你的结论字段。\n"
    "输出**一个** JSON 对象（可包 ```json 围栏），字段固定：\n"
    '{"decision": "wake" 或 "pass",\n'
    ' "item_id": 关联的事项标识（没有对应事项时用 null 报新风险）,\n'
    ' "reason": 一句话依据（引用了哪条变化/哪条标准）,\n'
    ' "evidence_refs": ["event:work:card:seq", ...]（必须来自输入材料里给出的坐标）,\n'
    ' "question": 需要强模型决定什么}\n'
    "只对值得升级的变化给 wake；普通进展、重复汇报给 pass（pass 也保留理由供抽查）。"
)


def weak_input_pack(work_id: str, plan_card_id: str, batch: dict[str, Any],
                    appt: dict[str, Any]) -> dict[str, Any]:
    """弱模型的**全部**输入材料（计划版本/观察标准/需求原句/变化/证据）。

    ``batch`` 必须是已过滤批次（run_weak_observation 负责）。
    """
    allowed = _allowed_card_ids(work_id, appt.get("observe_scope") or {})
    plan = plan_for_model(appt, allowed)
    changes_text, coords = _changes_text(batch)
    item_ids = {change.get("item_id") for change in batch.get("changes", [])} - {None, ""}
    if item_ids:
        plan = dict(plan, requirements=[r for r in plan.get("requirements", [])
                                        if not r.get("item_id") or r["item_id"] in item_ids])
    user = (
        f"计划载体卡：{plan_card_id}｜计划版本 rev.{appt.get('rev', 0)}"
        f"｜任命 {appt.get('assignment_id', '')}\n"
        f"== 当前观察标准（强模型写的）==\n{_standards_text(plan)}\n\n"
        f"== 关联需求原句 ==\n{_requirements_text(plan)}\n\n"
        f"== 本批变化（batch_id={batch.get('batch_id', '')}，共 "
        f"{len((batch or {}).get('changes') or [])} 条）==\n{changes_text}\n\n"
        "按 system 里的协议输出 JSON。evidence_refs 只能引用上面 [event:…] "
        "坐标；引用不存在或不属于本批 ⇒ 宿主会记「未判断」。"
    )
    return {"system": _WEAK_SYSTEM, "user": user, "coords": coords}


def _parse_weak_output(content: str) -> tuple[Optional[dict[str, Any]], str]:
    """[2026-11-21 · 用户令] 完整 JSON 校验：先整段解析＋顶层类型，再逐字段。

    解析顺序：① 围栏内整体 ② 整段——**任一候选解析成功但顶层不是对象
    ⇒ 直接判无效**（``[{"decision":…}]`` 这类合法数组到此为止，不会被截出
    内部对象蒙混过关）；③ 两个候选都解析失败才尝试寒暄截取 {..}。
    返回 (fields, error)。任何一步不过 ⇒ (None, error)——无效输出记
    「未判断」。
    """
    raw = str(content or "").strip()
    candidates: list[str] = []
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(raw)

    def _check_fields(parsed: Any) -> tuple[Optional[dict[str, Any]], str]:
        if not isinstance(parsed, dict):
            return None, (f"顶层必须是 JSON 对象，实际是 "
                          f"{type(parsed).__name__}")
        decision = parsed.get("decision")
        if not isinstance(decision, str) or decision not in ("wake", "pass"):
            return None, f"decision 非法: {decision!r}（只接受 wake/pass）"
        item_id = parsed.get("item_id")
        if item_id is not None and not isinstance(item_id, str):
            return None, "item_id 必须是字符串或 null"
        reason = parsed.get("reason")
        if reason is not None and not isinstance(reason, str):
            return None, "reason 必须是字符串"
        refs = parsed.get("evidence_refs")
        if refs is None:
            refs = []
        if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
            return None, "evidence_refs 必须是字符串数组"
        question = parsed.get("question")
        if question is not None and not isinstance(question, str):
            return None, "question 必须是字符串"
        return {"decision": decision,
                "item_id": item_id or "",
                "reason": reason or "",
                "evidence_refs": [str(x) for x in refs],
                "question": question or ""}, ""

    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except (ValueError, TypeError):
            continue
        return _check_fields(parsed)   # 解析成功即定论（含顶层非对象）

    # ③ 寒暄兜底：整段/围栏都不是合法 JSON 时才截取 {..}
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        try:
            parsed = json.loads(raw[i:j + 1])
        except (ValueError, TypeError) as exc:
            return None, f"非法 JSON: {exc}"
        return _check_fields(parsed)
    return None, "非法 JSON: 找不到 JSON 对象"


async def run_weak_observation(work_id: str, plan_card_id: str,
                               batch: dict[str, Any]) -> dict[str, Any]:
    """弱模型观察：宿主发起**一次无工具调用**（显式弱路由），解析协议并落账。

    入口即按当前任命过滤证据（work 前缀＋授权卡集合双闸）。返回
    {status: wake|passed|unjudged, ...}；任何失败路径都把（过滤后）批次保留
    待处理并把**过滤后证据全文**写进账本 detail（凭该记录可重新读回），
    不循环重试、不推进最迟回看。
    """
    appt = _current_assignment(work_id, plan_card_id)
    if not appt:
        raise coordination_store.CoordinationError(
            "validation", "这张计划卡没有活跃任命，不跑观察")
    if (batch.get("assignment_id") and batch["assignment_id"] != appt["assignment_id"]
            or appt.get("state") != "active"):
        raise coordination_store.CoordinationError(
            "validation", "暂停中的任命不跑观察")
    models = appt.get("models") or {}
    weak_route = str(models.get("weak") or "")
    if not weak_route:
        raise coordination_store.CoordinationError(
            "validation", "任命缺弱模型路由，不能跑观察")

    # 先按当前任命过滤证据，再组装材料（work 前缀＋授权卡集合双闸）
    batch_id = str(batch.get("batch_id") or new_batch_id())
    batch, dropped = _filter_batch(work_id, batch, appt.get("observe_scope") or {})
    batch = {**batch, "batch_id": batch_id}
    snap_rev = int(appt.get("rev", 0))

    pack = weak_input_pack(work_id, plan_card_id, batch, appt)
    send_guard = _make_send_guard(work_id, plan_card_id,
                                  appt["assignment_id"], snap_rev)
    try:
        result, model_used = await model_service.call_main(
            pack["system"], pack["user"],
            model_name=weak_route, tools=None,
            caller="coordination_weak", work_id=work_id,
            card_id=plan_card_id,
            # [2026-11-21 · 用户令] 发送边界守卫：排队放行后、适配器发送前
            # 再核对任命/活跃/读入版本——暂停后放行的队列请求不出站。
            before_send=send_guard)
        usage = result.get("usage") or {}
        content = str(result.get("content") or "")
    except Exception as exc:  # noqa: BLE001 模型不可用 ⇒ 未判断
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=appt["assignment_id"],
            kind="weak_observation", batch_id=batch_id, status="unjudged",
            detail={"error": str(exc), "stage": "call",
                    "dropped_out_of_scope": dropped,
                    "filtered_batch": batch},
            model_used=weak_route)
        await _keep_batch_pending(work_id, plan_card_id,
                                  appt["assignment_id"], batch_id,
                                  f"弱模型不可用：{exc}", snap_rev)
        return {"status": "unjudged", "batch_id": batch_id,
                "detail": {"error": str(exc), "stage": "call"}}

    # 完整 JSON 校验（整段解析＋顶层类型＋逐字段类型）
    fields, err = _parse_weak_output(content)
    if fields is None:
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=appt["assignment_id"],
            kind="weak_observation", batch_id=batch_id, status="unjudged",
            detail={"error": err, "content": content[:2000],
                    "dropped_out_of_scope": dropped,
                    "filtered_batch": batch},
            model_used=str(model_used), usage=usage)
        await _keep_batch_pending(work_id, plan_card_id,
                                  appt["assignment_id"], batch_id,
                                  f"弱模型输出无效（{err}），该批保留待处理",
                                  snap_rev)
        return {"status": "unjudged", "batch_id": batch_id,
                "detail": {"error": err}}

    decision = fields["decision"]
    item_id = fields["item_id"]
    reason = fields["reason"]
    refs = fields["evidence_refs"]
    question = fields["question"]

    # 引用核对：每条 evidence_refs 必须属于**过滤后**的本批坐标
    bad_refs = [r for r in refs if r not in pack["coords"]]
    if bad_refs:
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=appt["assignment_id"],
            kind="weak_observation", batch_id=batch_id, status="unjudged",
            decision=decision, reason=reason, evidence_refs=refs,
            question=question,
            detail={"error": f"引用不属于过滤后的本批输入: {bad_refs}",
                    "dropped_out_of_scope": dropped,
                    "filtered_batch": batch},
            model_used=str(model_used), usage=usage)
        await _keep_batch_pending(work_id, plan_card_id,
                                  appt["assignment_id"], batch_id,
                                  "弱模型引用了输入之外的证据，该批保留",
                                  snap_rev)
        return {"status": "unjudged", "batch_id": batch_id,
                "detail": {"error": "引用不存在", "bad_refs": bad_refs}}

    # 合法结论落账：wake 提请进待办（同事务核对任命/活跃/版本）；pass 留理由
    status = "wake" if decision == "wake" else "passed"
    if decision == "wake":
        written = await _append_pending(
            work_id, plan_card_id, appt["assignment_id"],
            {"kind": "weak_wake", "batch_id": batch_id,
             "item_id": item_id or None, "reason": reason,
             "evidence_refs": refs, "question": question, "at": None,
             "source_cards": sorted({cid for ch in batch.get("changes", [])
                                     for cid in ch.get("source_cards", [ch["card_id"]])})},
            expected_rev=snap_rev)
        if not written:
            # 写待办时任命/版本已变 ⇒ 观察不生效，改记未判断（批次仍保留）
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=appt["assignment_id"],
                kind="weak_observation", batch_id=batch_id, status="unjudged",
                decision=decision, reason=reason, evidence_refs=refs,
                question=question,
                detail={"error": "写待办时任命/版本已变（同事务核对未过）",
                        "filtered_batch": batch},
                model_used=str(model_used), usage=usage)
            return {"status": "unjudged", "batch_id": batch_id,
                    "detail": {"error": "写待办时任命/版本已变"}}
    await coordination_store.add_observation(
        work_id, plan_card_id, assignment_id=appt["assignment_id"],
        kind="weak_observation", batch_id=batch_id, decision=decision,
        item_id=item_id, status=status, reason=reason,
        evidence_refs=refs, question=question,
        model_used=str(model_used), usage=usage)
    return {"status": status, "batch_id": batch_id, "item_id": item_id,
            "reason": reason, "evidence_refs": refs, "question": question,
            "model_used": str(model_used), "dropped_out_of_scope": dropped}


async def _append_pending(work_id: str, plan_card_id: str,
                          assignment_id: str, entry: dict[str, Any],
                          expected_rev: Optional[int] = None) -> bool:
    """往任命的 pending 里追加一条。

    [2026-11-21 · 用户令] **同一事务内**核对：任命标识匹配、状态活跃、
    读入版本未变（expected_rev 给出时）——暂停/换人/改计划后返回的旧观察
    不写入。写入成功返回 True；条件不满足返回 False（零写入）。
    """
    import sqlite3

    def _fn(conn: sqlite3.Connection) -> bool:
        coordination_store._ensure(conn)
        sql = ("SELECT * FROM coordination_appointments"
               " WHERE work_id=? AND plan_card_id=? AND assignment_id=?"
               " AND state='active'")
        args: list[Any] = [work_id, plan_card_id, assignment_id]
        if expected_rev is not None:
            sql += " AND rev=?"
            args.append(int(expected_rev))
        row = conn.execute(sql, tuple(args)).fetchone()
        if row is None:
            return False  # 任命不在任/已暂停/版本已变 ⇒ 旧任务零写入
        pending = coordination_store._loads(row["pending"], [])
        new_item = dict(entry)  # 换名避免闭包变量重绑定坑
        if new_item.get("batch_id") and any(
                item.get("kind") == new_item["kind"]
                and item.get("batch_id") == new_item["batch_id"] for item in pending):
            return True
        if not new_item.get("at"):
            from ..storage.coordination_store import _now
            new_item["at"] = _now()
        pending.append(new_item)
        conn.execute(
            "UPDATE coordination_appointments SET pending=?, updated_at=?"
            " WHERE id=?",
            (coordination_store._dumps(pending),
             new_item.get("at") or "", row["id"]))
        return True

    from ..storage import card_db
    return bool(await card_db.run_write(work_id, _fn))


async def _keep_batch_pending(work_id: str, plan_card_id: str,
                              assignment_id: str, batch_id: str,
                              why: str,
                              expected_rev: Optional[int] = None) -> None:
    """未判断 ⇒ 保留（过滤后）批次待处理；同事务核对任命/活跃/版本。"""
    await _append_pending(work_id, plan_card_id, assignment_id,
                          {"kind": "unjudged_batch", "batch_id": batch_id,
                           "reason": why, "at": None},
                          expected_rev=expected_rev)


# ---------------------------------------------------------------- 强模型（查证轮）

_COORD_TURN_SYSTEM = (
    "你是本计划任命的项目统筹（强模型），以所任职能身份在**计划专属会话**里"
    "工作。执行锚＝计划载体卡；你维护唯一施工计划，依据现行需求判偏、协调与"
    "安排开发。用户目标、授权和授权卡清单已给你，"
    "**需求原句、需求版本、各卡已有工作由你自己只读查证**（下面的指令协议），"
    "查证充分后提交一份可执行计划＋给观察哨（弱模型）的唤醒标准。\n"
    "每轮输出**一个** JSON 对象（可包 ```json 围栏），二选一：\n"
    "① 查证：{\"action\": \"coord_read_card\", \"card_id\": \"…\"}"
    " —— 读一张授权卡的使命/需求文档（含版本状态）/现状；\n"
    "        {\"action\": \"coord_read_events\", \"card_id\": \"…\", \"tail\": 10}"
    " —— 读一张授权卡的最近事件（进展与原始证据；从未发言的卡会返回空）。\n"
    "② 提交：{\"plan\": {…}, \"note\": \"给用户的说明（可选）\"}，plan 结构：\n"
    '  {"objective": 一句话总目标,\n'
    '   "requirements": [{"ref": "出处卡", "version": "查证坐标（state+内容指纹）",'
    ' "quote": "原句"}],\n'
    '   "items": [{"item_id": "稳定标识", "title": "标题", "owner_card": "卡号",\n'
    '              "depends": [], "next_result": "下一项可验证结果", "status": "todo"}],\n'
    '   "wake_standards": [{"standard_id": "s1", "protect": "…", "ignore": "…",'
    ' "escalate": "…"}],\n'
    '   "direct_nodes": [], "next_check": "…"}\n'
    '如需直接唤醒节点，形状为 {"node_id":"n1","item_id":"事项标识",'
    '"card_id":"来源卡号","event_type":"worker_done","path":"result.status",'
    '"equals":"passed"}。path 指实际事件的 data 字段路径。\n'
    "**requirements[].version 必须填 coord_read_card 返回的版本坐标"
    "（requirements_state＋sha256 指纹）**——计划引用要能回指当时读到的需求"
    "版本，任何人可复核。计划必须落在查证到的事实上，不许编造；"
    "空计划（没有目标/事项/唤醒标准）不会被接受。查证充分后必须提交。"
    "不要输出指令之外的请求——**发令能力尚未接通（任务四）**，本轮没有"
    "任何写卡或发令工具。"
    "③ 发令（**每轮至多一条**，受理后本轮结束）："
    '{"action": "coord_submit_instruction", "target_card": "…",'
    ' "item_id": "…或 null", "instruction": "给受理卡的一句话指令",'
    ' "evidence_refs": ["event:…"]}——宿主核对授权/证据后落信并给受理号；\n'
    "④ 问专家：{\"action\": \"coord_ask_function\", \"function_id\": \"fn_…\","
    " \"question\": \"…\"}——向职能卡发问询（不算发令）。"
)


def _turn_system(work_id: str, appt: dict[str, Any]) -> str:
    """统筹轮 system：指令协议＋**任命职能的职责书**（identity_for，宿主
    职能身份机制的接入点——职责与授权随身份进上下文）。"""
    try:
        from . import function_room
        ident = function_room.identity_for(
            work_id, str(appt.get("function_id") or ""))
        charter = str((ident.get("charter") or {}).get("text") or "").strip()
        name = str(ident.get("function_name") or "")
        if charter:
            return (_COORD_TURN_SYSTEM
                    + f"\n\n== 你的职责与授权（任命职能：{name}"
                      f"｜{appt.get('function_id')}）==\n{charter}")
    except Exception:  # noqa: BLE001 职责书取不到不拦起轮
        pass
    return _COORD_TURN_SYSTEM


def strong_first_input_pack(work_id: str, plan_card_id: str,
                            appt: dict[str, Any]) -> dict[str, Any]:
    """强模型首轮首跳材料：目标、授权、卡清单＋查证指令协议。

    [2026-11-21 · 用户令] **不预填计划**——需求原句、版本、已有工作不在
    材料里，由模型用只读查证指令从真实来源（卡正文/事件流）取。
    """
    g = appt.get("grants") or {}
    obs = g.get("observe") or {}
    ins = g.get("instruct") or {}
    cards = _observe_cards(work_id, obs)
    plan = plan_for_model(appt, {card["card_id"] for card in cards})
    obs_txt = "整个 work（含后来加入的卡）" if obs.get("scope") == "work" \
        else f"卡集合 {obs.get('ids', [])}"
    ins_txt = "全部开发卡" if ins.get("scope") == "work_development_cards" \
        else f"卡集合 {ins.get('ids', [])}"
    user = (
        f"你以任命职能身份进入本计划（执行锚＝计划载体卡 {plan_card_id}"
        f"，任命 {appt.get('assignment_id', '')}，当前 rev.{appt.get('rev', 0)}，"
        f"运行模式 {appt.get('mode', '')}）。\n\n"
        f"== 用户目标 ==\n{(plan.get('objective') or '').strip() or '（未填——按任命时 objective 拟）'}\n\n"
        f"== 授权 ==\n观察范围：{obs_txt}｜发令范围：{ins_txt}"
        f"（本轮无发令工具——发令能力任务四接通）\n\n"
        f"== 授权卡清单（含从未发言的卡；用 coord_read_card / coord_read_events"
        f" 逐卡查证）==\n"
        + "\n".join(f"- {c['card_id']}《{c['title']}》" for c in cards) + "\n\n"
        "按 system 里的指令协议开始查证；查证充分后提交 plan。"
    )
    return {"system": _turn_system(work_id, appt), "user": user}


def _validate_submitted_plan(plan: Any) -> tuple[Optional[dict[str, Any]], str]:
    """[2026-11-21 · 用户令] 空计划与劣质计划不记成功：完整结构校验。

    必须是对象；objective 非空；items/wake_standards/direct_nodes 是数组且
    **元素形状有效**（items 每项含非空 item_id/title；standards 每项含非空
    standard_id）——items:[{}] 这类占位不通过。
    """
    if not isinstance(plan, dict):
        return None, "计划必须是 JSON 对象"
    objective = str(plan.get("objective") or "").strip()
    if not objective:
        return None, "空计划：没有 objective"
    items = plan.get("items") or []
    standards = plan.get("wake_standards") or []
    nodes = plan.get("direct_nodes") or []
    if not isinstance(items, list) or not isinstance(standards, list) \
            or not isinstance(nodes, list):
        return None, "items/wake_standards/direct_nodes 必须是数组"
    for it in items:
        if not isinstance(it, dict) \
                or not str(it.get("item_id") or "").strip() \
                or not str(it.get("title") or "").strip():
            return None, (f"items 元素形状无效（每项需要非空 item_id/title）："
                          f"{json.dumps(it, ensure_ascii=False)[:120]}")
    for st in standards:
        if not isinstance(st, dict) \
                or not str(st.get("standard_id") or "").strip():
            return None, (f"wake_standards 元素形状无效（每项需要非空 standard_id）："
                          f"{json.dumps(st, ensure_ascii=False)[:120]}")
    if not items and not standards:
        return None, "空计划：既没有 items 也没有 wake_standards"
    return plan, ""


def _exec_coord_read(work_id: str, action: str, args: dict[str, Any],
                     allowed: set[str]) -> str:
    """只读查证指令执行（coord_read_card / coord_read_events）。

    范围闸：card_id 不在授权集合 ⇒ 拒绝并说明（不泄露范围外任何内容）。
    读卡走 card_db 同步只读（cards 表行列齐全）——**不在同步执行器里等
    async 协程**（run_coroutine_threadsafe 回自己的循环会死锁，实测踩过）。
    """
    cid = str((args or {}).get("card_id") or "").strip()
    if cid not in allowed:
        return f"查证被拒：{cid} 不在授权观察范围内。"
    if action == "coord_read_card":
        from ..storage import card_db
        row = card_db.read_sync(work_id, lambda c: _select_card(c, cid), None)
        if row is None:
            return f"查证失败：读卡 {cid} 出错（卡不存在或库不可读）。"
        req_text = str(row.get("requirements") or "")
        # [2026-11-21 · 用户令] 可核对的**需求版本坐标**：状态＋正文内容指纹
        # （sha256 前 12 位）＋卡面更新时间——任何人可重算指纹复核计划引用。
        req_fp = ""
        if req_text:
            import hashlib
            req_fp = hashlib.sha256(req_text.encode("utf-8")).hexdigest()[:12]
        return json.dumps({
            "card_id": cid,
            "title": str(row.get("title") or ""),
            "card_type": str(row.get("card_type") or ""),
            "mission": str(row.get("mission") or ""),
            "requirements": req_text,
            "requirements_state": str(row.get("requirements_state") or "")
            or "（无版本状态）",
            "requirements_version": {
                "state": str(row.get("requirements_state") or ""),
                "sha256": req_fp or None,
                "updated_at": str(row.get("updated_at") or ""),
            },
            "status_block": str(row.get("status_block") or ""),
        }, ensure_ascii=False)
    if action == "coord_read_events":
        from ..storage import event_log
        try:
            tail = int((args or {}).get("tail") or 10)
        except (TypeError, ValueError):
            tail = 10
        tail = max(1, min(tail, 20))
        items = event_log.timeline(work_id, cid, tail=tail) or []
        if not items:
            return json.dumps({"card_id": cid, "events": [],
                               "note": "（静默卡——从未发过消息）"},
                              ensure_ascii=False)
        evs = [{"seq": it.get("seq"), "type": it.get("type"),
                "data": json.dumps(it.get("data") or {}, ensure_ascii=False)[:300]}
               for it in items]
        return json.dumps({"card_id": cid, "events": evs}, ensure_ascii=False)
    return f"未知查证动作: {action}"


def _select_card(conn, cid: str) -> Optional[dict[str, Any]]:
    """cards 行的只读投影（查证用；列缺失时按 OperationalError 由上层兜）。"""
    try:
        row = conn.execute(
            "SELECT card_id, title, card_type, mission,"
            " requirements, requirements_state, status_block, updated_at"
            " FROM cards WHERE card_id=?", (cid,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return dict(row) if row is not None else None


def _session_key(plan_card_id: str) -> str:
    """计划专属会话键（附加会话，与席位 card@N 同构——主会话零写入）。"""
    return f"{plan_card_id}@coordination"


# ---------------------------------------------------------------- 发令受理（任务四）

# 发令受理表：每轮最多一条（UNIQUE assignment+turn）＋同内容重放同一受理号
# （UNIQUE content_hash）。核对全部在**同一事务**内完成（任务书：幂等统一
# 受理号同事务查重；调权在受理点完成）。
_INSTRUCTIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_instructions ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " work_id TEXT NOT NULL,"
    " plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL,"
    " turn_id TEXT NOT NULL,"
    " rev INTEGER NOT NULL,"
    " item_id TEXT,"
    " target_card TEXT NOT NULL,"
    " instruction TEXT NOT NULL,"
    " evidence_refs TEXT NOT NULL DEFAULT '[]',"
    " content_hash TEXT NOT NULL,"
    " receipt_id TEXT NOT NULL UNIQUE,"
    " created_at TEXT,"
    " UNIQUE(assignment_id, turn_id),"
    " UNIQUE(content_hash))"
)

#: 非开发卡（不接统筹指令的卡型/前缀）：职能卡、评审卡、差事伴生卡
NON_DEV_CARD_TYPES = ("function", "review", "qa_card")


def _ensure_instructions(conn) -> None:
    """事务内幂等建受理表（不能用 executescript——隐式提交事务）。"""
    conn.execute(_INSTRUCTIONS_DDL)


def _content_hash(assignment_id: str, item_id: str, target_card: str,
                  instruction: str, refs: list[str]) -> str:
    import hashlib
    payload = "\x00".join([assignment_id, item_id or "", target_card,
                           instruction, json.dumps(refs, ensure_ascii=False)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dev_card_ok(work_id: str, target_card: str) -> tuple[bool, str]:
    """开发卡判据：卡存在、非职能/评审/差事伴生、非根卡、非统筹计划卡自身。"""
    metas = card_store.iter_card_metas(work_id)
    meta = metas.get(target_card)
    if not meta:
        return False, f"受理卡不存在: {target_card}"
    ctype = str(meta.get("card_type") or "")
    if target_card.startswith("fn_") or ctype in NON_DEV_CARD_TYPES:
        return False, f"{target_card} 不是开发卡（职能/评审/伴生卡不接统筹指令）"
    if not str(meta.get("parent") or ""):
        return False, f"{target_card} 是根卡，不接统筹指令"
    return True, ""


async def coordination_intake(work_id: str, plan_card_id: str, *,
                              assignment_id: str, rev: int,
                              turn_id: str, item_id: Optional[str],
                              target_card: str, instruction: str,
                              evidence_refs: list[str]) -> dict[str, Any]:
    """发令**同步受理点**（任务书任务四）：核对、幂等、受理号——全在同一事务。

    核对清单（任一不过 ⇒ {ok: False, code, why}，零写入）：
    ① 任命标识匹配且 active（旧令牌＝标识不匹配 ⇒ 拒）；
    ② 读入版本 rev 匹配（旧令牌＝版本过期 ⇒ 拒）；
    ③ target ∈ 观察范围（统筹看不了现状的卡不受理）；
    ④ target ∈ 发令范围（work_development_cards 或显式卡集）；
    ⑤ target 是开发卡（职能卡/评审卡/差事伴生卡/根卡不接统筹指令）；
    ⑥ 每个证据引用 ∈ 本任命的观察事件（coordination_seen）；
    ⑦ 幂等：同轮（assignment+turn）已有指令 ⇒ 拒（每轮最多一条）；
       同内容重放 ⇒ 返回**原受理号**（不重复落信）。

    受理成功 ⇒ 生成受理号并落受理表；落信（统筹身份写受理卡会话）与
    回执由调用方完成。"""
    import sqlite3
    import uuid as _uuid
    instruction = str(instruction or "").strip()
    target_card = str(target_card or "").strip()
    refs = [str(x) for x in (evidence_refs or [])]
    if not target_card or not instruction:
        return {"ok": False, "code": "validation",
                "why": "target_card 与 instruction 不能为空"}

    def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
        coordination_store._ensure(conn)
        _ensure_instructions(conn)
        # ① 任命匹配 + active + ② 读入版本
        row = conn.execute(
            "SELECT * FROM coordination_appointments"
            " WHERE work_id=? AND plan_card_id=? AND assignment_id=?"
            " AND state='active' AND rev=?",
            (work_id, plan_card_id, str(assignment_id), int(rev))).fetchone()
        if row is None:
            row_any = conn.execute(
                "SELECT * FROM coordination_appointments"
                " WHERE work_id=? AND plan_card_id=? AND assignment_id=?",
                (work_id, plan_card_id, str(assignment_id))).fetchone()
            if row_any is None:
                return {"ok": False, "code": "stale_token",
                        "why": "旧令牌：任命标识不匹配（任命已换人或已撤回）"}
            if row_any["state"] != "active":
                return {"ok": False, "code": "stale_token",
                        "why": f"旧令牌：任命已{row_any['state']}"}
            return {"ok": False, "code": "stale_token",
                    "why": (f"旧令牌：读入版本 rev.{rev} 已过期"
                            f"（当前 rev.{row_any['rev']}）——请按最新版本重新提交")}
        # 幂等（先查重再核对其余——重放直接回原受理号）
        chash = _content_hash(str(assignment_id), item_id or "", target_card,
                              instruction, refs)
        dup = conn.execute(
            "SELECT receipt_id FROM coordination_instructions"
            " WHERE content_hash=?", (chash,)).fetchone()
        if dup is not None:
            return {"ok": True, "receipt_id": dup["receipt_id"],
                    "replayed": True}
        dup_turn = conn.execute(
            "SELECT receipt_id FROM coordination_instructions"
            " WHERE assignment_id=? AND turn_id=?",
            (str(assignment_id), str(turn_id or ""))).fetchone()
        if dup_turn is not None:
            return {"ok": False, "code": "turn_exhausted",
                    "why": "本轮已受理过一条指令（每轮最多一条）"}
        # ③④ 范围双闸（观察＋发令）
        import json as _json
        obs = _json.loads(row["observe_scope"] or {})
        ins = _json.loads(row["instruct_scope"] or {})
        obs_ids = (set(obs.get("ids") or [])
                   if obs.get("scope") == "cards" else None)  # None＝整个 work
        ins_ids = (set(ins.get("ids") or [])
                   if ins.get("scope") == "cards" else None)
        ins_is_all_dev = ins.get("scope") == "work_development_cards"
        if obs_ids is not None and target_card not in obs_ids:
            return {"ok": False, "code": "not_observed",
                    "why": f"{target_card} 不在观察范围——统筹看不了现状的卡不受理"}
        if ins_is_all_dev:
            dev_ok, dev_why = _dev_card_ok(work_id, target_card)
            if not dev_ok:
                return {"ok": False, "code": "not_developable", "why": dev_why}
        elif ins_ids is not None:
            if target_card not in ins_ids:
                return {"ok": False, "code": "not_instructable",
                        "why": f"{target_card} 不在发令范围"}
        else:
            return {"ok": False, "code": "validation",
                    "why": "任命缺发令范围"}
        if not ins_is_all_dev:
            dev_ok, dev_why = _dev_card_ok(work_id, target_card)
            if not dev_ok:
                return {"ok": False, "code": "not_developable", "why": dev_why}
        # ⑥ 证据引用 ∈ 本任命的观察事件
        for ref in refs:
            hit = conn.execute(
                "SELECT 1 FROM coordination_seen"
                " WHERE work_id=? AND plan_card_id=? AND assignment_id=?"
                " AND card_id=? AND seq=?",
                (work_id, plan_card_id, str(assignment_id),
                 _ref_card(ref), _ref_seq(ref))).fetchone()
            if hit is None:
                return {"ok": False, "code": "bad_evidence",
                        "why": f"证据引用不属于本任命的观察事件: {ref}"}
        # 受理：生成受理号落表
        receipt_id = f"rcp_{_uuid.uuid4().hex[:12]}"
        from ..storage.coordination_store import _now
        conn.execute(
            "INSERT INTO coordination_instructions"
            " (work_id,plan_card_id,assignment_id,turn_id,rev,item_id,"
            "  target_card,instruction,evidence_refs,content_hash,"
            "  receipt_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_id, plan_card_id, str(assignment_id), str(turn_id or ""),
             int(rev), item_id or None, target_card, instruction,
             json.dumps(refs, ensure_ascii=False), chash, receipt_id,
             _now()))
        return {"ok": True, "receipt_id": receipt_id, "replayed": False}

    try:
        result = await card_db.run_write(work_id, _fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[coordination] 发令受理异常: %s", exc)
        return {"ok": False, "code": "error", "why": str(exc)}
    # [任务书任务四] 落信是受理流程的一部分：受理成功（非重放）⇒ 以统筹身份
    # 写受理卡会话并给回执；**重放不重复落信**（幂等语义）。
    if result.get("ok") and not result.get("replayed"):
        try:
            fn_id = ""
            try:
                p = coordination_store.panel(work_id, plan_card_id)
                fn_id = str(p.get("function_id") or "")
            except Exception:  # noqa: BLE001
                fn_id = ""
            await _deliver_instruction(
                work_id, plan_card_id, fn_id, result["receipt_id"],
                target_card, item_id, instruction, refs)
        except Exception as exc:  # noqa: BLE001 落信失败不影响受理号
            logger.warning("[coordination] 落信失败 receipt=%s: %s",
                           result.get("receipt_id"), exc)
    return result


def _ref_card(coord: str) -> str:
    parts = str(coord).split(":")
    return parts[2] if len(parts) >= 4 else "__bad__"


def _ref_seq(coord: str) -> int:
    parts = str(coord).split(":")
    try:
        return int(parts[3]) if len(parts) >= 4 else -1
    except ValueError:
        return -1


async def _deliver_instruction(work_id: str, plan_card_id: str,
                               function_id: str, receipt_id: str,
                               target_card: str, item_id: Optional[str],
                               instruction: str,
                               evidence_refs: list[str]) -> None:
    """落信：以统筹身份写**受理卡**的会话（回复统筹的引用＝回执）。"""
    from ..storage import session_facade
    prefix = f"【统筹发令·{function_id}｜{receipt_id}】"
    if item_id:
        prefix += f"（事项 {item_id}）"
    if evidence_refs:
        prefix += f"（依据 {', '.join(evidence_refs[:3])}）"
    await session_facade.append_card_message(
        work_id, target_card, from_card=plan_card_id,
        text=f"{prefix} {instruction[:1500]}",
        msg_type="msg_notify")


def _make_send_guard(work_id: str, plan_card_id: str,
                     assignment_id: str, snap_rev: int):
    """[2026-11-21 · 用户令] 网关**发送边界守卫**：复用 call_main 的
    before_send 通道——网关在**取得并发额度后、适配器发送前**调用它
    （含同座重试/换座 attempt），核对任命标识、活跃状态、读入版本。
    runner 层的复核管不住「检查通过→请求排队→用户暂停→队列放行」的窗口；
    before_send 正卡在队列放行之后、真实出站之前 ⇒ 拦下后适配器 0 调用。"""
    async def _guard() -> None:
        p = coordination_store.panel(work_id, plan_card_id)
        if str(p.get("assignment_id") or "") != str(assignment_id):
            raise RuntimeError(
                f"发送守卫：任命已换人或已撤回（本回合 {assignment_id}，"
                f"当前 {p.get('assignment_id') or '无'}）")
        if p.get("state") != "active":
            raise RuntimeError(
                f"发送守卫：任命已{p.get('state')}，停止出站")
        if int(p.get("rev") or 0) != int(snap_rev):
            raise RuntimeError(
                f"发送守卫：读入版本 rev.{snap_rev} 已过期"
                f"（当前 rev.{p.get('rev')}）")
    return _guard


async def _ensure_session_registered(work_id: str, plan_card_id: str,
                                     sid: str, function_id: str) -> None:
    """把计划专属会话登记进 cards.sessions（正式读取入口的数据源）。

    幂等：已登记跳过。登记后 GET .../sessions 能查到、
    GET .../timeline?session=<sid> 能正式读回查证留痕。
    """
    try:
        light = await card_store.read_card_sessions_light(work_id, plan_card_id)
        if light is None:
            return
        entries = [dict(e) for e in (light.get("sessions") or [])]
        entry = next((e for e in entries if e.get("session_id") == sid), None)
        if entry is not None:
            if entry.get("persona") == function_id:
                return
            entry["persona"] = function_id
        else:
            entries.append({"session_id": sid, "persona": function_id,
                            "label": "统筹会话", "created_at": _now_iso()})
        await card_store.update_card(
            work_id, plan_card_id, {"sessions": entries},
            f"[统筹] 登记计划专属会话 {sid}（人格 {function_id}）")
    except Exception as exc:  # noqa: BLE001 登记失败不拦起轮
        logger.warning("[coordination] 会话登记失败 card=%s: %s",
                       plan_card_id, exc)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat()


async def _log_hop(work_id: str, sid: str, plan_card_id: str, text: str,
                   tag: str, cards: set[str]) -> None:
    """在专属会话中写宿主来源字段。消息正文不能声明自己的来源。"""
    from ..storage import session_facade

    try:
        await session_facade.append(work_id, sid, {
            "actor": f"card:{plan_card_id}", "from_card": plan_card_id,
            "event_type": "msg_notify", "content": f"【{tag}】{text[:1200]}",
            "coordination_scope": {"card_ids": sorted(cards)},
        })
    except Exception as exc:
        logger.warning("[coordination] 跳留痕失败 card=%s: %s", plan_card_id, exc)


async def _read_coord_history(work_id: str, sid: str, allowed: set[str]) -> str:
    """旧块仅留审计。模型只续用宿主知道来源且仍在授权范围内的记录。"""
    from ..storage import session_facade

    try:
        blocks = await session_facade.read(work_id, sid)
    except Exception as exc:
        logger.debug("[coordination] 读专属会话历史失败 sid=%s: %s", sid, exc)
        return ""
    lines = []
    for block in blocks:
        scope = block.get("coordination_scope")
        cards = scope.get("card_ids") if isinstance(scope, dict) else None
        body = str(block.get("content") or "").strip()
        if (body and isinstance(cards, list)
                and all(isinstance(cid, str) and cid in allowed for cid in cards)):
            lines.append(f"- {body[:400]}")
    return "\n".join(lines)


def authorized_wake_reason(work_id: str, appt: dict[str, Any],
                           pending: dict[str, Any]) -> Optional[dict[str, Any]]:
    allowed = _allowed_card_ids(work_id, appt.get("observe_scope") or {})
    refs = pending.get("evidence_refs") or []
    sources = pending.get("source_cards")
    if not refs or not isinstance(sources, list) or not sources:
        return None
    if any(cid not in allowed for cid in sources):
        return None
    return {key: pending[key] for key in ("item_id", "reason", "question", "evidence_refs")
            if key in pending}


def _review_receipt(value: Any, reads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("到期回看须提交独立的 review 记录")
    refs = value.get("evidence_refs")
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or ref not in reads for ref in refs)):
        raise ValueError("回看须引用本轮宿主发出的查证坐标")
    evidence = [reads[ref] for ref in dict.fromkeys(refs)]
    requirement_reads = [r for r in evidence if r["action"] == "coord_read_card"]
    if not requirement_reads:
        raise ValueError("回看须包含实际读取需求或需求读取失败的回执")
    conclusion = value.get("conclusion") or ""
    missing = value.get("open_items") or []
    if (not isinstance(conclusion, str) or not isinstance(missing, list)
            or any(not isinstance(item, str) or not item.strip() for item in missing)
            or not (conclusion.strip() or missing)):
        raise ValueError("回看须记录结论或尚缺事项")
    requirements = [{"card_id": r["card_id"], "version": r["version"], "ref": r["ref"]}
                    for r in requirement_reads if r.get("version", {}).get("sha256")]
    if not requirements and not missing:
        raise ValueError("未取得需求原文时须记录尚缺事项")
    try:
        next_at = datetime.fromisoformat(str(value.get("next_review_at") or ""))
    except ValueError:
        raise ValueError("下次回看须给带时区的绝对时间") from None
    if next_at.tzinfo is None or next_at <= datetime.now(timezone.utc):
        raise ValueError("下次回看须给将来的带时区绝对时间")
    return {"requirements": requirements, "evidence": evidence,
            "conclusion": conclusion.strip(), "open_items": missing,
            "next_review_at": next_at.isoformat()}


async def run_strong_first_turn(work_id: str, plan_card_id: str, *,
                                assignment_id: str = "",
                                trigger: str = "appoint",
                                reason: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """强模型首轮：**多跳只读查证循环**，绑定任命标识。

    [2026-11-21 · 用户令] 调用前校验仍在任且活跃；**每跳发模型前再复核**
    （取材料/查证期间暂停 ⇒ 不再发起调用）；提交落账前复查＋save_plan 的
    expected_rev CAS 兜住竞态。解析失败/预算用尽/空计划只落账（unparsed/
    error），不重试。留痕只进计划专属附加会话。
    """
    appt = _current_assignment(work_id, plan_card_id)
    if not appt:
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=str(assignment_id or ""),
            kind="strong_turn", status="skipped", detail={
                "trigger": trigger, "why": "没有在任任命"})
        return {"status": "skipped", "detail": {"why": "没有在任任命"}}
    if assignment_id and str(appt.get("assignment_id")) != str(assignment_id):
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=str(assignment_id),
            kind="strong_turn", status="skipped", detail={
                "trigger": trigger,
                "why": f"任命已不是本任务所属（当前 {appt.get('assignment_id')}）"})
        return {"status": "skipped", "detail": {"why": "任命已换人或已撤回"}}
    if appt.get("state") != "active":
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=appt["assignment_id"],
            kind="strong_turn", status="skipped", detail={
                "trigger": trigger, "why": f"任命状态 {appt.get('state')}，不启动"})
        return {"status": "skipped", "detail": {"why": f"任命 {appt.get('state')}"}}
    models = appt.get("models") or {}
    strong_route = str(models.get("strong") or "")
    if not strong_route:
        raise coordination_store.CoordinationError(
            "validation", "任命缺强模型路由，不能起轮")

    cards = _observe_cards(work_id, appt.get("observe_scope") or {})
    allowed_now = {c["card_id"] for c in cards}
    pack = strong_first_input_pack(work_id, plan_card_id, appt)
    snap_rev = int(appt.get("rev", 0))
    aid = str(appt["assignment_id"])
    sid = _session_key(plan_card_id)
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"   # 发令每轮一条的查重键（任务四）
    is_review = trigger == "review_due"
    if trigger != "appoint":
        pack["user"] += f"\n\n== 本轮缘由 ==\n{trigger}"
        if reason:
            pack["user"] += "\n" + json.dumps(reason, ensure_ascii=False)
        current = plan_for_model(appt, allowed_now)
        review_context = appt.get("last_review") or {}
        review_sources = review_context.get("source_cards")
        if not isinstance(review_sources, list) or not set(review_sources).issubset(allowed_now):
            review_context = {}
        pack["user"] += "\n当前计划与上次回看：\n" + json.dumps({
            "plan": current, "last_review": review_context,
        }, ensure_ascii=False)
    if is_review:
        try:
            interval = max(60, int(os.environ.get("COORD_REVIEW_INTERVAL_SEC", "86400")))
        except ValueError:
            interval = 86400
        suggested = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
        pack["system"] += (
            '\n到期回看必须查证后输出 {"review":{"evidence_refs":["read:1"],'
            '"conclusion":"…","open_items":[],"next_review_at":"带时区的将来绝对时间"}}。'
            '证据坐标必须由本轮宿主查证返回，至少查一次需求。此轮只提交 review。'
            f'按回看间隔建议下次时间 {suggested}，有依据时可提前。')

    # [2026-11-21 · 用户令] 专属会话接正式入口：登记进 cards.sessions
    #（GET .../sessions 可查、timeline?session= 可正式读回）；
    # 下一轮起轮时把专属会话历史读回（**按当前授权过滤**——被撤卡的内容
    # 不回流），上下文不再从空历史组装。
    await _ensure_session_registered(work_id, plan_card_id, sid,
                                     str(appt.get("function_id") or ""))
    coord_history = await _read_coord_history(work_id, sid, allowed_now)
    if coord_history:
        pack["user"] = (f"== 上一轮统筹会话记录（从专属会话读回）==\n"
                        f"{coord_history}\n\n" + pack["user"])

    # 查证预算：2×卡数（每卡查证两件）＋8 跳冗余——统筹轮是宿主自含循环
    # （非卡回合，卡回合零闸纪律不适用），预算防模型失控烧钱，有结构依据。
    budget = max(16, 2 * len(cards) + 8)
    hops: list[dict[str, Any]] = []       # 每跳审计：action/参数/结果摘要
    reads: dict[str, dict[str, Any]] = {}
    review = None
    # 会话历史累积：[{role:user,材料},{role:assistant,out},{role:user,结果}…]
    history: list[dict[str, str]] = [{"role": "user", "content": pack["user"]}]
    current_user = pack["user"]
    submitted: Optional[dict[str, Any]] = None
    submit_note = ""
    status = "unparsed"
    err_text = "查证预算用尽（未提交计划）"
    model_used = strong_route
    usage_acc: dict[str, Any] = {}

    def _acc_usage(acc: dict[str, Any], new: Any) -> dict[str, Any]:
        """[2026-11-21 · 用户令] 多跳用量**累计**：数值键相加，其余键保留最后。"""
        acc = dict(acc or {})
        for k, v in (new or {}).items():
            if isinstance(v, (int, float)) and isinstance(acc.get(k), (int, float)):
                acc[k] = acc[k] + v
            elif isinstance(v, (int, float)):
                acc[k] = v
            else:
                acc[k] = v
        return acc

    async def _recheck(hop: int, phase: str) -> Optional[dict[str, Any]]:
        """[2026-11-21 · 用户令 · 最优先] 撤权/暂停/换人/版本推进 ⇒ 旧回合
        立即停止：核对任命标识、活跃状态、**读入版本**三项（set_grant 收窄
        授权、pause/resume、save_plan 都推进 rev）。返回 None＝通过；
        非 None＝已落账并返回的 skipped 响应。"""
        now_appt = _current_assignment(work_id, plan_card_id)
        if not now_appt or str(now_appt.get("assignment_id")) != aid:
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=aid,
                kind="strong_turn", status="skipped", detail={
                    "trigger": trigger, "hop": hop, "phase": phase,
                    "why": f"{phase}复核：任命已换人或已撤回",
                    "hops": hops})
            return {"status": "skipped",
                    "detail": {"why": f"{phase}复核未通过（任命已换人或已撤回）",
                               "hop": hop, "phase": phase}}
        if now_appt.get("state") != "active":
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=aid,
                kind="strong_turn", status="skipped", detail={
                    "trigger": trigger, "hop": hop, "phase": phase,
                    "why": f"{phase}复核：任命 {now_appt.get('state')}",
                    "hops": hops})
            return {"status": "skipped",
                    "detail": {"why": f"{phase}复核未通过（任命已暂停）",
                               "hop": hop, "phase": phase}}
        if int(now_appt.get("rev") or 0) != snap_rev:
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=aid,
                kind="strong_turn", status="skipped", detail={
                    "trigger": trigger, "hop": hop, "phase": phase,
                    "why": (f"{phase}复核：读入版本已过期"
                            f"（起轮 rev.{snap_rev}，当前 rev.{now_appt.get('rev')}）"),
                    "hops": hops})
            return {"status": "skipped",
                    "detail": {"why": f"{phase}复核未通过（读入版本 rev.{snap_rev}"
                                      f" 已过期，当前 rev.{now_appt.get('rev')}）",
                               "hop": hop, "phase": phase}}
        return None

    async def _hop_log(text: str, tag: str,
                       cards: Optional[set[str]] = None) -> None:
        """每跳留痕进**计划专属附加会话**（历史可读回；主会话零写入）。

        块头带关联卡集标注（读回按当前授权过滤）：统筹输出块＝起轮时授权集
        快照（授权变化 ⇒ 旧输出整块不回流，安全侧）；查证块＝该次查证的卡。
        """
        await _log_hop(work_id, sid, plan_card_id,
                       f"【{tag}｜hop{len(hops)}｜rev.{snap_rev}】{text}",
                       tag, cards if cards is not None else allowed_now)

    for hop in range(budget):
        # ① 出站前复核（任命/活跃/读入版本）——旧回合不再发模型
        blocked = await _recheck(hop, "出站前")
        if blocked:
            return blocked
        try:
            result, model_used = await model_service.call_main(
                pack["system"], current_user,
                prior_messages=history[:-1] if hop > 0 else None,
                model_name=strong_route, tools=None,
                caller="coordination_strong", work_id=work_id,
                card_id=plan_card_id,
                # [2026-11-21 · 用户令] 发送边界守卫：每跳出站同样过网关
                # 队列放行后的最后复核（与弱模型同一机制）。
                before_send=_make_send_guard(work_id, plan_card_id,
                                             aid, snap_rev))
            usage_acc = _acc_usage(usage_acc, result.get("usage") or {})
            content = str(result.get("content") or "")
        except Exception as exc:  # noqa: BLE001
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=aid,
                kind="strong_turn", status="error", detail={
                    "error": str(exc), "stage": "call", "trigger": trigger,
                    "hop": hop, "hops": hops},
                model_used=model_used, usage=usage_acc)
            return {"status": "error", "detail": {"error": str(exc)}}
        await _hop_log(content, "统筹输出")

        parsed = None
        try:
            parsed = json.loads(_strip_json_block(content))
        except (ValueError, TypeError) as exc:
            status, err_text = "unparsed", f"第 {hop} 跳输出非法 JSON: {exc}"
            break
        if not isinstance(parsed, dict):
            status = "unparsed"
            err_text = (f"第 {hop} 跳顶层必须是 JSON 对象"
                        f"（实际 {type(parsed).__name__}）")
            break

        if is_review and ("review" in parsed or "plan" in parsed):
            try:
                review = _review_receipt(parsed.get("review"), reads)
            except ValueError as exc:
                status, err_text = "unparsed", str(exc)
            else:
                status, err_text = "parsed", ""
            break

        if parsed.get("plan") is not None:
            # 提交路径：完整校验（空计划/劣质形状不记成功）
            plan, plan_err = _validate_submitted_plan(parsed.get("plan"))
            if plan is None:
                status = "unparsed"
                err_text = f"第 {hop} 跳提交被拒：{plan_err}"
                break
            submitted = plan
            submit_note = str(parsed.get("note") or "")
            status = "parsed"
            err_text = ""
            hops.append({"hop": hop, "action": "coord_submit_plan",
                         "items": len(plan.get("items") or []),
                         "standards": len(plan.get("wake_standards") or [])})
            break

        action = str(parsed.get("action") or "")
        if action not in ("coord_read_card", "coord_read_events",
                          "coord_submit_instruction", "coord_ask_function"):
            status = "unparsed"
            err_text = (f"第 {hop} 跳指令无效: {action!r}"
                        "（只接受 coord_read_card/coord_read_events/"
                        "coord_submit_instruction/coord_ask_function/提交 plan）")
            break
        # ② 查证前复核（同三核对）＋**授权集合每次现算**——权限收窄即时生效，
        # 旧回合读不到刚撤的卡；被拒结果如实回给模型（不给编造空间）。
        blocked = await _recheck(hop, "查证前")
        if blocked:
            return blocked
        now_appt2 = _current_assignment(work_id, plan_card_id)
        live_allowed = _allowed_card_ids(work_id,
                                         now_appt2.get("observe_scope") or {})
        args = parsed.get("args", parsed)
        if not isinstance(args, dict):
            status, err_text = "unparsed", "查证参数必须是 JSON 对象"
            break
        if action == "coord_submit_instruction":
            # [任务四] 发令动作：走同步受理点（授权/证据/幂等全核对），
            # 受理成功 ⇒ 落信＋审计＋**本轮终结**（每轮最多一条指令）。
            intake = await coordination_intake(
                work_id, plan_card_id, assignment_id=aid,
                rev=snap_rev, turn_id=turn_id,
                item_id=args.get("item_id"),
                target_card=str(args.get("target_card") or ""),
                instruction=str(args.get("instruction") or ""),
                evidence_refs=[str(x) for x in
                               (args.get("evidence_refs") or [])])
            if not intake.get("ok"):
                status = "unparsed"
                err_text = (f"第 {hop} 跳发令被拒（{intake.get('code')}）："
                            f"{intake.get('why')}")
                break
            receipt_id = intake["receipt_id"]   # 落信已由 intake 统一完成
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=aid,
                kind="instruction", status="delivered",
                item_id=str(args.get("item_id") or "") or None,
                evidence_refs=[str(x) for x in
                               (args.get("evidence_refs") or [])],
                detail={"receipt_id": receipt_id,
                        "target_card": args.get("target_card"),
                        "instruction": str(args.get("instruction") or "")[:500],
                        "turn_id": turn_id, "replayed": intake.get("replayed",
                                                                  False)},
                model_used=model_used, usage=usage_acc)
            await _hop_log(f"发令受理 {receipt_id} → {args.get('target_card')}",
                           "发令", cards={str(args.get("target_card") or "")})
            hops.append({"hop": hop, "action": "coord_submit_instruction",
                         "receipt_id": receipt_id,
                         "replayed": intake.get("replayed", False)})
            status = "parsed"
            err_text = ""
            return {"status": "parsed", "receipt_id": receipt_id,
                    "model_used": model_used, "session": sid,
                    "hops": len(hops),
                    "instruction": intake}
        if action == "coord_ask_function":
            # [任务四] 问专家：向职能卡发问询（不算发令；职能必须存在）
            fid_ask = str(args.get("function_id") or "").strip()
            q = str(args.get("question") or "").strip()
            from ..storage import function_store as _fs
            if not fid_ask.startswith("fn_") or not _fs.function_exists(
                    work_id, fid_ask):
                out_text = f"问询被拒：{fid_ask} 不是有效的职能。"
                await coordination_store.add_observation(
                    work_id, plan_card_id, assignment_id=aid,
                    kind="ask", status="refused",
                    detail={"function_id": fid_ask, "why": out_text},
                    model_used=model_used, usage=usage_acc)
                hops.append({"hop": hop, "action": "coord_ask_function",
                             "refused": True})
                history.append({"role": "assistant", "content": content})
                current_user = f"问询结果：\n{out_text}\n\n继续按协议输出。"
                history.append({"role": "user", "content": current_user})
                continue
            try:
                from ..storage import session_facade
                await session_facade.append_card_message(
                    work_id, fid_ask, from_card=plan_card_id,
                    text=f"【统筹问询·{appt.get('function_id')}｜{aid}】{q[:800]}",
                    msg_type="msg_notify")
                ask_ok = True
            except Exception as exc:  # noqa: BLE001
                ask_ok = False
                logger.warning("[coordination] 问询落信失败: %s", exc)
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=aid,
                kind="ask", status="delivered" if ask_ok else "error",
                detail={"function_id": fid_ask,
                        "question": q[:400], "turn_id": turn_id},
                model_used=model_used, usage=usage_acc)
            hops.append({"hop": hop, "action": "coord_ask_function",
                         "function_id": fid_ask})
            history.append({"role": "assistant", "content": content})
            current_user = ("问询已送出（专家回复将以事件回流）。继续按协议"
                            "输出下一个 JSON（查证/问专家/发令/提交）。")
            history.append({"role": "user", "content": current_user})
            continue
        out_text = _exec_coord_read(work_id, action, args, live_allowed)
        hops.append({"hop": hop, "action": action,
                     "args": json.dumps(parsed.get("args") or parsed,
                                        ensure_ascii=False)[:300],
                     "result_head": out_text[:200]})
        read_card_id = str(args.get("card_id") or "").strip()
        ref = ""
        if read_card_id in live_allowed:
            ref = f"read:{len(reads) + 1}"
            result_data = json.loads(out_text) if out_text.startswith("{") else {}
            reads[ref] = {"ref": ref, "card_id": read_card_id, "action": action,
                          "status": "ok" if result_data else "failed",
                          "version": result_data.get("requirements_version", {}),
                          "event_seqs": [e["seq"] for e in result_data.get("events", [])],
                          "detail": out_text[:400] if not result_data else ""}
        await _hop_log(f"查证 {action} → {out_text[:400]}", "查证结果",
                       cards={read_card_id} if read_card_id else set())
        history.append({"role": "assistant", "content": content})
        current_user = (f"查证结果 {ref}：\n{out_text}\n\n继续：按协议输出下一个"
                        " JSON（查证或提交）。")
        history.append({"role": "user", "content": current_user})

    if status != "parsed":
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=aid,
            kind="strong_turn", status=status, detail={
                "error": err_text, "trigger": trigger, "hops": hops},
            model_used=model_used, usage=usage_acc)
        return {"status": status, "detail": {"error": err_text}}

    if is_review:
        closed = await coordination_store.review_closure(
            work_id, plan_card_id, assignment_id=aid, expected_rev=snap_rev,
            due_at=appt["next_review_at"],
            receipt={**review, "source_cards": sorted(allowed_now)})
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=aid, kind="strong_turn",
            status="parsed" if closed["ok"] else "skipped",
            model_used=model_used, usage=usage_acc,
            detail={"trigger": trigger, "hops": hops, **closed})
        return {"status": "parsed" if closed["ok"] else "skipped", **closed}

    # 落账前复查：任命仍是在任本人且活跃（查证期间暂停 ⇒ 计划不写入）
    appt_now = _current_assignment(work_id, plan_card_id)
    if not appt_now or str(appt_now.get("assignment_id")) != aid:
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=aid,
            kind="strong_turn", status="skipped", detail={
                "trigger": trigger,
                "why": "落账前复查：任命已换人或已撤回，计划不写入",
                "hops": hops})
        return {"status": "skipped", "detail": {"why": "任命已换人或已撤回"}}
    if appt_now.get("state") != "active":
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=aid,
            kind="strong_turn", status="skipped", detail={
                "trigger": trigger,
                "why": f"落账前复查：任命 {appt_now.get('state')}，计划不写入",
                "hops": hops})
        return {"status": "skipped", "detail": {"why": "任命已暂停"}}

    try:
        saved = await coordination_store.save_plan(
            work_id, plan_card_id, assignment_id=aid,
            expected_rev=snap_rev, plan=submitted,
            by=f"strong:{model_used}", source_cards=allowed_now)
    except coordination_store.CoordinationError as exc:
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=aid,
            kind="strong_turn", status="unparsed", detail={
                "error": f"保存冲突: {exc.message}", "trigger": trigger,
                "hops": hops},
            model_used=model_used, usage=usage_acc)
        return {"status": "unparsed", "detail": {"error": "保存冲突"}}

    new_rev = int((saved.get("appointment") or {}).get("rev") or 0)
    await coordination_store.add_observation(
        work_id, plan_card_id, assignment_id=aid,
        kind="strong_turn", status="parsed", detail={
            "trigger": trigger, "note": submit_note, "new_rev": new_rev,
            "items": len(submitted.get("items") or []),
            "standards": len(submitted.get("wake_standards") or []),
            "hops": hops},
        model_used=model_used, usage=usage_acc)
    # 以任命职能身份把结论留痕到**计划专属附加会话**（主会话零写入）
    try:
        from ..storage import session_facade
        fn_name = str(appt.get("function_id") or "")
        await session_facade.append_card_message(
            work_id, sid, from_card=plan_card_id,
            text=f"【统筹首轮·{fn_name}｜{aid}】查证 {len(hops)} 跳后提交计划"
                 f"（rev.{new_rev}）：目标「{(submitted.get('objective') or '')[:120]}」"
                 f"，事项 {len(submitted.get('items') or [])} 条，唤醒标准 "
                 f"{len(submitted.get('wake_standards') or [])} 条。"
                 f"{('说明：' + submit_note[:300]) if submit_note else ''}",
            msg_type="msg_notify")
    except Exception as exc:  # noqa: BLE001 留痕失败不拦结果
        logger.warning("[coordination] 首轮留痕失败 work=%s card=%s: %s",
                       work_id, plan_card_id, exc)
    return {"status": "parsed", "rev": new_rev, "note": submit_note,
            "model_used": model_used, "session": sid, "hops": len(hops)}
