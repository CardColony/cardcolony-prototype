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

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import model_service
from ..storage import card_db, card_store, coordination_board, coordination_store

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


def _board_tail(appt: dict[str, Any], allowed: set[str]) -> list[dict[str, str]]:
    """当轮计划板投影——turn_tail 每次出站**一份**（含 rev，未完成按
    (priority,item_id) 序；blocked/doing 也算未完成；无待推进写明）。

    [2026-11-23 · 计划板任务书] 覆盖所有强模型唤醒口（任命/手动/弱模型
    提请/直接节点/到期回看）＋工具续跳＋用户对话。改板成功后的下一跳
    用保存回执的新 rev 重新投影——调用方负责刷新本返回值。
    """
    snap = coordination_board.board_snapshot(appt, allowed)
    text = ("统筹计划板（当前快照；未完成＝todo/doing/blocked，按优先级序）\n"
            + json.dumps(snap, ensure_ascii=False, separators=(",", ":")))
    return [{"role": "user", "content": text}]


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
    # [2026-11-23 · 计划板] 本批涉及事项的状态与下一结果——帮弱模型判断
    # 「这条变化对应的事项是否值得叫醒统筹」。全板只随强模型回合走。
    if item_ids:
        items_now = {it["item_id"]: it for it in coordination_board.visible_items(appt, allowed)}
        involved = sorted(str(x) for x in item_ids if str(x) in items_now)
        if involved:
            user += "\n\n== 本批涉及事项现状（板面，只读）==\n" + "\n".join(
                f"- {iid}（{items_now[iid]['status']}）："
                f"{items_now[iid]['next_result'] or '（未填下一结果）'}"
                for iid in involved)
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
    "每轮输出**一个** JSON 对象（可包 ```json 围栏），按下列协议选择动作：\n"
    "① 查证：{\"action\": \"coord_read_card\", \"card_id\": \"…\"}"
    " —— 读一张授权卡的使命/需求文档（含版本状态）/现状；\n"
    "        {\"action\": \"coord_read_events\", \"card_id\": \"…\", \"tail\": 10}"
    " —— 读一张授权卡的最近事件（进展与原始证据；从未发言的卡会返回空）。\n"
    "② 提交：{\"plan\": {…}, \"note\": \"给用户的说明（可选）\"}，plan 结构：\n"
    '  {"objective": 一句话总目标,\n'
    '   "requirements": [{"ref": "出处卡", "version": "查证坐标（state+内容指纹）",'
    ' "quote": "原句"}],\n'
    '   "wake_standards": [{"standard_id": "s1", "protect": "…", "ignore": "…",'
    ' "escalate": "…"}],\n'
    '   "direct_nodes": [], "next_check": "…"}\n'
    '如需直接唤醒节点，形状为 {"node_id":"n1","item_id":"事项标识",'
    '"card_id":"来源卡号","event_type":"worker_done","path":"result.status",'
    '"equals":"passed"}。path 指实际事件的 data 字段路径。\n'
    "**requirements[].version 必须填 coord_read_card 返回的版本坐标"
    "（requirements_state＋sha256 指纹）**——计划引用要能回指当时读到的需求"
    "版本，任何人可复核。计划必须落在查证到的事实上，不许编造；"
    "空计划（没有目标/事项/唤醒标准）不会被接受。查证充分后必须提交。\n"
    "开发事项通过正式指令交给域责任卡，由责任卡在本域决定是否建卡和如何派工。\n"
    "③ 发令（**每轮至多一条**，受理后本轮结束）："
    '{"action": "coord_submit_instruction", "target_card": "…",'
    ' "item_id": "事项标识", "instruction": "给域责任卡的指令",'
    ' "acceptance": "验收判据",'
    ' "evidence_refs": ["event:…"]}——宿主核对授权/证据后落信并给受理号；\n'
    "④ 问专家：{\"action\": \"coord_ask_function\", \"function_id\": \"fn_…\","
    " \"question\": \"…\"}——向职能卡发问询（不算发令）。\n"
    "⑤ 派 QA：{\"action\": \"coord_qa_request\", \"item_id\": \"…\","
    " \"target_card\": \"…\", \"acceptance\": \"验收条款\","
    " \"evidence_refs\": [\"event:…\"]}——走正式 QA 差事，编号回挂事项。\n"
    "⑥ 计划板（登记/调整后续工作——**已登板条目以此为准**，plan 提交里的"
    " items 不再回改）：\n"
    "    {\"action\": \"coord_plan_board\", \"args\": {\"op\": \"read\""
    " [, \"item_id\": \"…\"]}}——默认读未完成项（优先级升序）；指定"
    " item_id 可读已完成项及已核验回执。长板用 offset 和 limit 分页。\n"
    "    {\"action\": \"coord_plan_board\", \"args\": {\"op\": \"apply\","
    " \"reason\": \"本次调整理由\", \"changes\": [\n"
    "      {\"op\": \"add\", \"title\": \"…\", \"next_result\": \"…\","
    " \"priority\": 150, \"owner_card\": \"card_x\","
    " \"requirement_refs\": [\"read:1\"]},\n"
    "      {\"op\": \"update\", \"item_id\": \"…\", \"priority\": 50},\n"
    "      {\"op\": \"update\", \"item_id\": \"…\", \"status\": \"done\","
    " \"result_note\": \"完成依据\", \"evidence_refs\": [\"receipt:cmsg-…\"]}]}}\n"
    "    ——整批原子保存：任一条不合法全部不写入。add 的新 ID 由宿主"
    "分配（回执 allocated 给出）；priority 是正整数、小者优先；"
    "status=done 必须同批提交 result_note。保存回执给新 rev——此后"
    "本轮以新 rev 为准，续跳材料里的计划板也随之换新快照。\n"
    "    登记事项后另行用 ③ 发令给 owner 责任卡（发令仍每轮至多一条）。"
)


def _turn_system(work_id: str, appt: dict[str, Any],
                 instructions: str = _COORD_TURN_SYSTEM) -> str:
    """统筹轮 system：指令协议＋**任命职能的职责书**（identity_for，宿主
    职能身份机制的接入点——职责与授权随身份进上下文）。"""
    try:
        from . import function_room
        ident = function_room.identity_for(
            work_id, str(appt.get("function_id") or ""))
        charter = str((ident.get("charter") or {}).get("text") or "").strip()
        name = str(ident.get("function_name") or "")
        if charter:
            return (instructions
                    + f"\n\n== 你的职责与授权（任命职能：{name}"
                      f"｜{appt.get('function_id')}）==\n{charter}")
    except Exception:  # noqa: BLE001 职责书取不到不拦起轮
        pass
    return instructions


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


def _validate_submitted_plan(plan: Any, *, registered_items: list[dict]
                             ) -> tuple[Optional[dict[str, Any]], str]:
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
    if not registered_items and not standards:
        return None, "空计划：既没有 items 也没有 wake_standards"
    return {key: value for key, value in plan.items() if key != "items"}, ""


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


# ---------------------------------------------------------------- 发令受理/落信（任务四 · 返工版）

# [2026-11-21 · 用户实测返工] 受理与落信分账：
#   · 受理点只做核对+预留**稳定行动键**（宿主按任命及决策行动派键），
#     不落信、不产生最终受理号（避免"写入失败也拿到受理号"吞掉重试）；
#   · 落信点**同一事务重验授权**（含 mode=live、暂停/换人/版本）并落唯一收件
#     （action_key UNIQUE），复用正式 direct 通信面
#     （msg_directive + deliver_now 唤醒），不用裸 msg_notify 旁路；
#   · 受理/落信/唤醒分别落账（observations 三种 kind）。
_ACTIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_actions ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " work_id TEXT NOT NULL,"
    " plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL,"
    " action_key TEXT NOT NULL UNIQUE,"
    " turn_id TEXT NOT NULL,"
    " rev INTEGER NOT NULL,"
    " item_id TEXT,"
    " target_card TEXT NOT NULL,"
    " instruction TEXT NOT NULL,"
    " evidence_refs TEXT NOT NULL DEFAULT '[]',"
    " status TEXT NOT NULL DEFAULT 'accepted',"
    " acceptance TEXT NOT NULL DEFAULT '',"
    " receipt_id TEXT,"
    " msg_id TEXT,"
    " woke INTEGER NOT NULL DEFAULT 0,"
    " created_at TEXT, updated_at TEXT,"
    " UNIQUE(assignment_id, turn_id))"
)

#: 非开发卡（不接统筹指令的卡型/前缀）：职能卡、评审卡、差事伴生卡
NON_DEV_CARD_TYPES = ("function", "review", "qa_card")


def _ensure_actions(conn) -> None:
    """在当前事务内建表，并为旧收件保留重新核实投影的入口。"""
    conn.execute(_ACTIONS_DDL)
    columns = {col["name"] for col in conn.execute("PRAGMA table_info(coordination_actions)")}
    for col, ddl in (
            ("acceptance", "TEXT NOT NULL DEFAULT ''"),
            ("actor", "TEXT NOT NULL DEFAULT ''"),
            ("projected_session", "INTEGER NOT NULL DEFAULT 0"),
            ("projected_origin", "INTEGER NOT NULL DEFAULT 0"),
            ("projected_event", "INTEGER NOT NULL DEFAULT 0"),
            ("projection_verified", "INTEGER NOT NULL DEFAULT 0")):
        if col not in columns:
            conn.execute(f"ALTER TABLE coordination_actions ADD COLUMN {col} {ddl}")


# 执行证据绑定（联调任务书工作二）：原行动 + 目标回合正式回执 → 可打开证据
_EVIDENCE_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_evidence ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " work_id TEXT NOT NULL, plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL, action_key TEXT NOT NULL,"
    " item_id TEXT, target_card TEXT NOT NULL,"
    " msg_id TEXT NOT NULL,"
    " reply_msg_id TEXT NOT NULL,"
    " reply_to TEXT NOT NULL DEFAULT '',"
    " from_card TEXT NOT NULL DEFAULT '',"
    " content TEXT NOT NULL DEFAULT '',"
    " evidence_refs TEXT NOT NULL DEFAULT '[]',"
    " status TEXT NOT NULL DEFAULT 'verified',"
    " created_at TEXT)"
)


def _ensure_evidence(conn) -> None:
    conn.execute(_EVIDENCE_DDL)
    for col, ddl in (("reply_session_card", "TEXT NOT NULL DEFAULT ''"),
                     ("reply_seq", "INTEGER NOT NULL DEFAULT 0"),
                     ("reply_msg_type", "TEXT NOT NULL DEFAULT ''")):
        try:
            conn.execute(f"ALTER TABLE coordination_evidence ADD COLUMN {col} {ddl}")
        except Exception:  # noqa: BLE001
            pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS coordination_evidence_uq"
                     " ON coordination_evidence(work_id, msg_id, reply_msg_id)")
    except Exception:  # noqa: BLE001
        pass


# QA 差事记录（下一批 ②）：派驻编号＋结论回原事项
_QA_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_qa ("
    " qa_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,"
    " plan_card_id TEXT NOT NULL, assignment_id TEXT NOT NULL,"
    " rev INTEGER NOT NULL, item_id TEXT, target_card TEXT NOT NULL,"
    " acceptance TEXT NOT NULL, deployment_id INTEGER, qa_card TEXT NOT NULL DEFAULT '',"
    " status TEXT NOT NULL DEFAULT 'dispatched', verdict TEXT NOT NULL DEFAULT '',"
    " conclusion TEXT NOT NULL DEFAULT '', evidence_refs TEXT NOT NULL DEFAULT '[]',"
    " deployment_args TEXT NOT NULL DEFAULT '{}',"
    " created_at TEXT, updated_at TEXT)"
)


def _ensure_qa(conn) -> None:
    conn.execute(_QA_DDL)
    columns = {col["name"] for col in conn.execute("PRAGMA table_info(coordination_qa)")}
    for col, ddl in (("qa_card", "TEXT NOT NULL DEFAULT ''"),
                     ("verdict", "TEXT NOT NULL DEFAULT ''"),
                     ("deployment_args", "TEXT NOT NULL DEFAULT '{}'")):
        if col not in columns:
            conn.execute(f"ALTER TABLE coordination_qa ADD COLUMN {col} {ddl}")


# 专业问答：问题稳定标识 + 只读回合答复（下一批 ②）
_QUESTIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS coordination_questions ("
    " question_id TEXT PRIMARY KEY,"
    " work_id TEXT NOT NULL, plan_card_id TEXT NOT NULL,"
    " assignment_id TEXT NOT NULL, rev INTEGER NOT NULL,"
    " item_id TEXT, function_id TEXT NOT NULL,"
    " question TEXT NOT NULL, requirements_version TEXT NOT NULL DEFAULT '',"
    " evidence_refs TEXT NOT NULL DEFAULT '[]',"
    " status TEXT NOT NULL DEFAULT 'queued',"
    " answer TEXT NOT NULL DEFAULT '', answer_rev INTEGER,"
    " answered_at TEXT, created_at TEXT)"
)


def _ensure_questions(conn) -> None:
    conn.execute(_QUESTIONS_DDL)


def _content_hash(work_id: str, assignment_id: str, turn_id: str,
                  action_seq: str, target_card: str,
                  _reserved: list[str] | None = None) -> str:
    """稳定行动键：任命＋决策(turn_id)＋行动序号＋目标卡（不含内容，重试同键）。"""
    import hashlib
    payload = "\x00".join([work_id, assignment_id, turn_id, action_seq,
                           target_card])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dev_card_ok(work_id: str, target_card: str) -> tuple[bool, str]:
    """开发卡判据：卡存在、非职能/评审/差事伴生、非根卡。"""
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


def _gates(work_id: str, plan_card_id: str, assignment_id: str,
           rev: int, target_card: str, refs: list[str],
           conn=None, item_id: Optional[str] = None,
           items_allowed: Optional[set[str]] = None
           ) -> tuple[bool, str, dict[str, Any]]:
    """发令共用闸门（受理与落信**都**调用）：任命/active/rev/**mode=live**/
    观察∩发令范围/开发卡/证据引用（含 **work 前缀核对**）。

    返回 (ok, code+why, appt_row)。conn 给定时用该连接（落信同事务重验），
    否则走只读 panel。
    """
    import json as _json

    def _bad(code: str, why: str):
        return False, code, {"code": code, "why": why}

    if conn is not None:
        row = conn.execute(
            "SELECT * FROM coordination_appointments"
            " WHERE work_id=? AND plan_card_id=? AND assignment_id=?",
            (work_id, plan_card_id, str(assignment_id))).fetchone()
        appt = dict(row) if row else {}
    else:
        appt = _current_assignment(work_id, plan_card_id)  # grants 已拍平
    if not appt or str(appt.get("assignment_id") or "") != str(assignment_id):
        return _bad("stale_token", "旧令牌：任命标识不匹配（任命已换人或已撤回）")
    if appt.get("state") != "active":
        return _bad("stale_token", f"旧令牌：任命已{appt.get('state')}")
    if int(appt.get("rev") or 0) != int(rev):
        return _bad("stale_token",
                    f"旧令牌：读入版本 rev.{rev} 已过期（当前 rev.{appt.get('rev')}）")
    # [2026-11-21 · 用户令反例①] 旁观模式**零发令**
    mode = str(appt.get("mode") or "")
    if mode != "live":
        return _bad("observe_mode", "旁观模式（observe）不发令——只观察，不写入")
    # [下一批任务书] 事项必须属于本次任命读取/保存的计划版本
    if not str(item_id or "").strip():
        return _bad("missing_item", "发令必须绑定计划中的事项 item_id")
    if items_allowed is not None and str(item_id) not in items_allowed:
        return _bad("stale_item",
                    f"事项 {item_id} 不属于本次任命读入的计划版本")
    obs = _json.loads(appt.get("observe_scope") or "{}") \
        if isinstance(appt.get("observe_scope"), str) \
        else (appt.get("observe_scope") or {})
    ins = _json.loads(appt.get("instruct_scope") or "{}") \
        if isinstance(appt.get("instruct_scope"), str) \
        else (appt.get("instruct_scope") or {})
    obs_ids = set(obs.get("ids") or []) if obs.get("scope") == "cards" else None
    ins_ids = set(ins.get("ids") or []) if ins.get("scope") == "cards" else None
    ins_all_dev = ins.get("scope") == "work_development_cards"
    if obs_ids is not None and target_card not in obs_ids:
        return _bad("not_observed",
                    f"{target_card} 不在观察范围——统筹看不了现状的卡不受理")
    if ins_all_dev:
        dev_ok, why = _dev_card_ok(work_id, target_card)
        if not dev_ok:
            return _bad("not_developable", why)
    elif ins_ids is not None:
        if target_card not in ins_ids:
            return _bad("not_instructable", f"{target_card} 不在发令范围")
        dev_ok, why = _dev_card_ok(work_id, target_card)
        if not dev_ok:
            return _bad("not_developable", why)
    else:
        return _bad("validation", "任命缺发令范围")
    # 证据引用：**本 work 前缀** + 本任命 seen 坐标（反例⑤）
    for ref in refs:
        if not str(ref).startswith(f"event:{work_id}:"):
            return _bad("bad_evidence", f"证据引用不属于本 work: {ref}")
        if conn is not None:
            hit = conn.execute(
                "SELECT 1 FROM coordination_seen"
                " WHERE work_id=? AND plan_card_id=? AND assignment_id=?"
                " AND card_id=? AND seq=?",
                (work_id, plan_card_id, str(assignment_id),
                 _ref_card(ref), _ref_seq(ref))).fetchone()
        else:
            hit = None
            from ..storage import card_db as _cdb

            def _chk(c):
                return c.execute(
                    "SELECT 1 FROM coordination_seen"
                    " WHERE work_id=? AND plan_card_id=? AND assignment_id=?"
                    " AND card_id=? AND seq=?",
                    (work_id, plan_card_id, str(assignment_id),
                     _ref_card(ref), _ref_seq(ref))).fetchone()
            hit = _cdb.read_sync(work_id, _chk, None)
        if hit is None:
            return _bad("bad_evidence",
                        f"证据引用不属于本任命的观察事件: {ref}")
    return True, "", appt


async def coordination_intake(work_id: str, plan_card_id: str, *,
                              assignment_id: str, rev: int,
                              turn_id: str, item_id: Optional[str],
                              target_card: str, instruction: str,
                              evidence_refs: list[str],
                              action_seq: int = 0,
                              acceptance: str = "",
                              items_allowed: Optional[set[str]] = None
                              ) -> dict[str, Any]:
    """发令**受理**（同步）：七道闸核对＋预留**稳定行动键**；不落信、
    不产生最终受理号（写入失败可凭同动作键重试）。

    成功 ⇒ {ok, action_key}；同动作已提交收件 ⇒ {ok, replayed, receipt_id}；
    failed ⇒ 允许重试（同 action_key）；同轮已有别的动作 ⇒ turn_exhausted。
    """
    import uuid as _uuid
    instruction = str(instruction or "").strip()
    target_card = str(target_card or "").strip()
    refs = [str(x) for x in (evidence_refs or [])]
    acceptance = str(acceptance or "").strip()
    if not target_card or not instruction:
        return {"ok": False, "code": "validation",
                "why": "target_card 与 instruction 不能为空"}
    if not acceptance:
        return {"ok": False, "code": "missing_acceptance",
                "why": "验收判据 acceptance 不能为空"}
    if items_allowed is None:
        # 历史任命的 plan 没有事项清单 ⇒ 不硬拒，但落账标缺项（如实呈现）
        _items = ((_current_assignment(work_id, plan_card_id)
                   or {}).get("plan") or {}).get("items") or []
        items_allowed = ({str(it.get("item_id") or "")
                          for it in _items if isinstance(it, dict)}
                         if _items else None)

    def _fn(conn) -> dict[str, Any]:
        coordination_store._ensure(conn)
        _ensure_actions(conn)
        ok, code, gate = _gates(work_id, plan_card_id, str(assignment_id),
                                int(rev), target_card, refs, conn=conn,
                                item_id=item_id, items_allowed=items_allowed)
        if not ok:
            return {"ok": False, "code": code, "why": gate["why"]}
        # [2026-11-21 · 用户令] 稳定键＝任命＋决策(turn)＋行动序号(seq)
        # ——同动作重试同键，不随内容/引用改写换键（原内容 hash 会吞真实重试）
        action_key = _content_hash(work_id, str(assignment_id),
                                   str(turn_id or ""), str(action_seq),
                                   target_card, refs[:0])
        row = conn.execute(
            "SELECT * FROM coordination_actions WHERE action_key=?",
            (action_key,)).fetchone()
        if row is not None:
            if row["msg_id"]:
                return {"ok": True, "action_key": action_key,
                        "replayed": True,
                        "receipt_id": row["receipt_id"]}
            if row["status"] == "accepted":
                return {"ok": True, "action_key": action_key,
                        "replayed": True}  # 已受理待落信——不重复 spawn
            # failed ⇒ 允许按原动作键重试落信
        else:
            dup_turn = conn.execute(
                "SELECT 1 FROM coordination_actions"
                " WHERE assignment_id=? AND turn_id=?",
                (str(assignment_id), str(turn_id or ""))).fetchone()
            if dup_turn is not None:
                return {"ok": False, "code": "turn_exhausted",
                        "why": "本轮已受理过一条指令（每轮最多一条）"}
            from ..storage.coordination_store import _now
            conn.execute(
                "INSERT INTO coordination_actions"
                " (work_id,plan_card_id,assignment_id,action_key,turn_id,"
                "  rev,item_id,target_card,instruction,evidence_refs,"
                "  acceptance,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?)",
                (work_id, plan_card_id, str(assignment_id), action_key,
                 str(turn_id or ""), int(rev), item_id or None, target_card,
                 instruction, json.dumps(refs, ensure_ascii=False),
                 acceptance, _now(), _now()))
        return {"ok": True, "action_key": action_key, "replayed": False}

    try:
        result = await card_db.run_write(work_id, _fn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[coordination] 发令受理异常: %s", exc)
        return {"ok": False, "code": "error", "why": str(exc)}
    if not result.get("ok"):
        # [任务书 · 用户令] 「拟发」记录：旁观/越权/令牌失效等**零收件**
        # 的发令意图也要留痕（审计面；绝不落信）。
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=assignment_id,
            kind="instruction_draft", status="refused",
            item_id=item_id or None, evidence_refs=refs,
            detail={"code": result.get("code"), "why": result.get("why"),
                    "target_card": target_card,
                    "instruction": instruction[:300], "turn_id": turn_id,
                    "action_seq": action_seq})
    if result.get("ok") and not result.get("replayed"):
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=assignment_id,
            kind="instruction_accepted", status="accepted",
            item_id=item_id or None, evidence_refs=refs,
            detail={"action_key": result["action_key"],
                    "target_card": target_card, "turn_id": turn_id,
                    "instruction": instruction[:300]})
    return result



def _read_action(work_id: str, plan_card_id: str, action_key: str) -> dict[str, Any] | None:
    def _read(conn):
        try:
            row = conn.execute(
                "SELECT * FROM coordination_actions WHERE work_id=? AND plan_card_id=? AND action_key=?",
                (work_id, plan_card_id, action_key)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: coordination_actions" in str(exc):
                return None
            raise
        return dict(row) if row else None

    return card_db.read_sync(work_id, _read)


async def _combined_submit(work_id: str, plan_card_id: str, *,
                          assignment_id: str, rev: int,
                          turn_id: str, hop: int,
                          plan_raw: Any, instr_raw: Any,
                          note: str, model_used: str,
                          usage: dict[str, Any],
                          allowed_now: set[str]) -> dict[str, Any]:
    """同轮组合提交：先 save_plan（expected_rev＝读入版本），再用
    **保存回执的 rev** 走 intake→deliver（任务书下一批 ①）。

    模型填报的身份/版本/行动键无授权效力——全部取宿主读入值。
    保存成功但后续失败 ⇒ 计划保留，分别记「计划已保存、指令尚未受理」与
    「指令已受理、目标尚未确认」两账（不回滚计划）。
    """
    current = _current_assignment(work_id, plan_card_id)
    candidates = coordination_board.visible_items(current, allowed_now)
    plan, plan_err = _validate_submitted_plan(plan_raw, registered_items=candidates)
    if plan is None:
        return {"status": "unparsed", "detail": {"error": plan_err}}
    instr = instr_raw if isinstance(instr_raw, dict) else {}
    acceptance = str(instr.get("acceptance") or "").strip()
    item_id = instr.get("item_id")
    instruction = str(instr.get("instruction") or "").strip()
    target = str(instr.get("target_card") or "").strip()
    if not acceptance or not item_id or not instruction or not target:
        return {"status": "unparsed",
                "detail": {"error": "组合 instruction 缺 item_id/target_card/"
                                    "instruction/acceptance（验收判据非空）"}}
    if str(item_id) not in {str(it.get("item_id") or "") for it in candidates}:
        return {"status": "unparsed",
                "detail": {"error": f"事项 {item_id} 不在待保存计划内"}}
    try:
        saved = await coordination_store.save_plan(
            work_id, plan_card_id, assignment_id=assignment_id,
            expected_rev=rev, plan=plan, by=f"strong:{model_used}",
            source_cards=allowed_now)
    except Exception as exc:  # noqa: BLE001 保存冲突/暂停/撤回 ⇒ 全零收件
        return {"status": "unparsed",
                "detail": {"error": f"组合保存失败: {exc}", "saved": False}}
    new_rev = int((saved.get("appointment") or {}).get("rev") or 0)
    await coordination_store.add_observation(
        work_id, plan_card_id, assignment_id=assignment_id,
        kind="plan_saved", status="parsed",
        detail={"rev": new_rev, "note": note, "combined": True},
        model_used=model_used, usage=usage)
    items_allowed = {str(it.get("item_id") or "") for it in candidates}
    intake = await coordination_intake(
        work_id, plan_card_id, assignment_id=assignment_id, rev=new_rev,
        turn_id=turn_id, item_id=item_id, target_card=target,
        instruction=instruction, action_seq=hop,
        acceptance=acceptance,
        evidence_refs=[str(x) for x in (instr.get("evidence_refs") or [])],
        items_allowed=items_allowed)
    if not intake.get("ok"):
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=assignment_id,
            kind="instruction_pending_acceptance", status="pending",
            detail={"rev": new_rev, "code": intake.get("code"),
                    "why": intake.get("why"), "target_card": target,
                    "item_id": item_id, "plan_saved": True})
        return {"status": "parsed", "saved_rev": new_rev,
                "instruction": "pending_acceptance",
                "detail": {"error": intake.get("why")}}
    delivered = await coordination_deliver(
        work_id, plan_card_id, assignment_id=assignment_id, rev=new_rev,
        action_key=intake["action_key"])
    if not delivered.get("ok"):
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=assignment_id,
            kind="instruction_accepted_no_receipt", status="accepted",
            detail={"rev": new_rev, "code": delivered.get("code"),
                    "why": delivered.get("why"),
                    "action_key": intake["action_key"],
                    "target_card": target, "item_id": item_id})
        return {"status": "parsed", "saved_rev": new_rev,
                "instruction": "accepted_no_receipt",
                "detail": {"error": delivered.get("why")}}
    return {"status": "parsed", "saved_rev": new_rev,
            "instruction": "delivered",
            "receipt_id": delivered.get("receipt_id"),
            "msg_id": delivered.get("msg_id"),
            "woke": delivered.get("woke", False)}


async def coordination_qa_request(work_id: str, plan_card_id: str, *,
                                   assignment_id: str, rev: int,
                                   item_id: Optional[str],
                                   target_card: str, acceptance: str,
                                   evidence_refs: list[str]) -> dict[str, Any]:
    """[下一批 ②] 用**正式派驻入口**（card_tools.create_deploy_card，
    function=qa）对明确对象派 QA，差事编号回挂原事项。

    权限不足/派驻失败如实拒；同一事项+对象+验收条款稳定 qa_id（重放回读）。
    QA 的通过/失败/未验证与其原始证据由 qa_records() 从差事账与会话回读，
    统筹不代替 QA 下结论。
    """
    target = str(target_card or "").strip()
    acceptance = str(acceptance or "").strip()
    if not target or not acceptance:
        return {"ok": False, "code": "validation",
                "why": "QA 派驻需要 target_card 与非空验收条款"}
    # [反例③] 旁观模式零派驻（隔离测试断言派驻入口零调用）
    appt = _current_assignment(work_id, plan_card_id)
    if not appt or str(appt.get("assignment_id")) != str(assignment_id) \
            or int(appt.get("rev") or 0) != int(rev) \
            or appt.get("state") != "active":
        return {"ok": False, "code": "stale_token",
                "why": "派驻前任命/版本复核未通过"}
    if appt.get("mode") != "live":
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=assignment_id,
            kind="qa_draft", status="refused", item_id=item_id,
            detail={"target_card": target, "acceptance": acceptance[:300],
                    "why": "旁观模式不允许派驻 QA（拟发留痕、零调用）"})
        return {"ok": False, "code": "observe_mode",
                "why": "旁观模式不派驻 QA（只留拟发记录）"}
    qhash = _content_hash(work_id, str(assignment_id), str(item_id or ""),
                          target, acceptance)
    qa_id = f"qa_{qhash[:20]}"

    with _qa_request_lock:
        key = (work_id, qa_id)
        if key in _active_qa_requests:
            return {"ok": True, "qa_id": qa_id, "replayed": True, "in_progress": True}
        _active_qa_requests.add(key)
    try:
        return await _dispatch_qa(
            work_id, plan_card_id, assignment_id, rev, qa_id,
            item_id, target, acceptance, evidence_refs)
    finally:
        with _qa_request_lock:
            _active_qa_requests.discard(key)


_qa_request_lock = threading.Lock()
_active_qa_requests: set[tuple[str, str]] = set()


async def _dispatch_qa(work_id: str, plan_card_id: str, assignment_id: str,
                       rev: int, qa_id: str, item_id: Optional[str], target: str,
                       acceptance: str, evidence_refs: list[str]) -> dict[str, Any]:
    from . import card_tools
    from ..storage import function_store
    from ..storage.coordination_store import _now

    def _reserve(conn):
        _ensure_qa(conn)
        conn.execute(
            "INSERT OR IGNORE INTO coordination_qa"
            " (qa_id,work_id,plan_card_id,assignment_id,rev,item_id,"
            "  target_card,acceptance,status,evidence_refs,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,'reserving',?,?,?)",
            (qa_id, work_id, plan_card_id, assignment_id, rev, item_id or None,
             target, acceptance, json.dumps([str(ref) for ref in evidence_refs], ensure_ascii=False),
             _now(), _now()))
        return dict(conn.execute(
            "SELECT * FROM coordination_qa WHERE qa_id=?", (qa_id,)).fetchone())

    def _bind_card(conn, card_id, deployment_args):
        updated = conn.execute(
            "UPDATE coordination_qa SET qa_card=?, deployment_args=?, updated_at=?"
            " WHERE qa_id=? AND qa_card=''",
            (card_id, json.dumps(deployment_args, ensure_ascii=False), _now(), qa_id))
        if updated.rowcount != 1:
            raise RuntimeError("该验收已绑定差事卡，不能重复建卡")

    row = card_db.write_sync(work_id, _reserve)
    qa_card = str(row["qa_card"] or "")
    deployment_id = int(row["deployment_id"] or 0)
    replayed = bool(qa_card or deployment_id)
    try:
        # 差事账可以比统筹记录先提交，按原批次找回，即使差事已关账。
        deps = function_store._query_deployments(
            work_id, "AND venue_kind='deploy' AND batch_id=? AND delegator=? AND target_card=?"
            " AND (?='' OR venue_ref=?) AND (?=0 OR id=?)",
            (qa_id, plan_card_id, target, qa_card, qa_card, deployment_id, deployment_id),
            limit=1)
        if deps:
            dep = deps[0]
            qa_card, deployment_id = dep["venue_ref"], dep["id"]
            replayed = True
            wake = await card_tools.activate_and_wake_deployment(work_id, deployment_id)
        elif deployment_id:
            raise ValueError("原派驻编号找不到对应的差事账")
        elif qa_card and row["deployment_args"] != "{}":
            dep = await function_store.create_deployment(
                work_id, venue_ref=qa_card, **json.loads(row["deployment_args"]))
            deployment_id = dep["id"]
            wake = await card_tools.activate_and_wake_deployment(work_id, deployment_id)
        else:
            cards = card_store.iter_card_metas(work_id)
            if target not in cards:
                return {"ok": False, "code": "not_found", "qa_id": qa_id,
                        "why": f"验收对象不存在: {target}"}
            result = await card_tools._h_create_deploy_card(
                work_id, plan_card_id,
                {"function": "qa", "target": target,
                 "task": f"独立验收 {target}：{acceptance}",
                 "title": f"统筹验收·{target}", "batch_id": qa_id},
                cards, bind_deployment=_bind_card, resume_card_id=qa_card)
            if not result.get("ok"):
                raise ValueError(result.get("error") or result)
            qa_card, deployment_id = result["card_id"], result["deployment_id"]
            wake = result
        if not wake.get("ok"):
            raise ValueError(wake.get("error") or wake)
    except Exception as exc:  # noqa: BLE001 保留已有卡和差事，允许同键续办
        card_db.write_sync(work_id, lambda conn: conn.execute(
            "UPDATE coordination_qa SET status='failed', updated_at=?"
            " WHERE qa_id=? AND qa_card='' AND deployment_id IS NULL",
            (_now(), qa_id)))
        return {"ok": False, "code": "qa_dispatch_failed", "qa_id": qa_id,
                "why": str(exc)}

    def _record_dispatch(conn):
        _reserve(conn)
        changed = conn.execute(
            "UPDATE coordination_qa SET qa_card=?, deployment_id=?, status='dispatched',"
            " updated_at=? WHERE qa_id=? AND status<>'dispatched'",
            (qa_card, deployment_id, _now(), qa_id))
        return bool(changed.rowcount)

    if card_db.write_sync(work_id, _record_dispatch):
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=assignment_id,
            kind="qa_dispatched", status="dispatched", item_id=item_id,
            detail={"qa_id": qa_id, "qa_card": qa_card,
                    "target_card": target, "acceptance": acceptance[:300],
                    "deployment_id": deployment_id})
    return {"ok": True, "qa_id": qa_id, "replayed": replayed,
            "qa_card": qa_card, "deployment_id": deployment_id,
            "wake_status": wake.get("wake_status", "pending")}

async def bind_execution_evidence(
        work_id: str, target_card: str, *,
        coordination_msg_id: str, reply_msg_id: str,
        reply_to: str = "", from_card: str = "",
        content: str = "", receipt_card: str = "",
        receipt_session: str = "") -> dict[str, Any]:
    """[联调任务书工作二] 把目标卡的**正式回报**绑定回原统筹行动。

    宿主核验（模型正文里的卡号/任务号/路径**无绑定效力**）：
      ① msg_id 在本 work 的 coordination_actions 里唯一命中（原行动）；
      ② from_card == 该行动的 target_card（错目标拒）；
      ③ 回执 reply_to 与 msg_id 一致（错行动拒；空 reply_to 用 source 绑定）；
      ④ 目标会话里该 msg_id 的事件坐标可读（坐标不可读拒）。
    成功落 coordination_evidence（唯一索引去重：重复回执返回原记录），
    board_summary 据此推进到「已取得执行证据」。
    """
    from ..storage.coordination_store import _now
    from ..storage import session_facade

    def _lookup(conn) -> dict[str, Any]:
        coordination_store._ensure(conn)
        _ensure_actions(conn)
        _ensure_evidence(conn)
        rows = conn.execute(
            "SELECT * FROM coordination_actions WHERE work_id=? AND msg_id=?",
            (work_id, str(coordination_msg_id))).fetchall()
        if len(rows) != 1:
            return {"ok": False, "code": "unlinked",
                    "why": f"消息号 {coordination_msg_id} 不唯一对应本 work 的统筹行动"}
        a = dict(rows[0])
        if str(a.get("target_card") or "") != str(target_card):
            return {"ok": False, "code": "wrong_target",
                    "why": "回报来源卡不是该统筹行动的目标卡"}
        # [返工①] reply_to 必须指回**原收件消息号**——指向别的行动拒
        if str(reply_to or "") != str(coordination_msg_id):
            return {"ok": False, "code": "wrong_reply_to",
                    "why": (f"回执 reply_to={reply_to!r} 未指向原消息号"
                            f" {coordination_msg_id}")}
        # 重复回执：同 msg_id + reply_msg_id 唯一
        dup = conn.execute(
            "SELECT * FROM coordination_evidence"
            " WHERE work_id=? AND msg_id=? AND reply_msg_id=?",
            (work_id, str(coordination_msg_id), str(reply_msg_id))).fetchone()
        if dup:
            return {"ok": True, "replayed": True,
                    "evidence_id": dup["id"], "action_key": a["action_key"]}
        return {"ok": True, "action": a}

    try:
        hit = await card_db.run_write(work_id, _lookup)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "error", "why": str(exc)}
    if not hit.get("ok") or hit.get("replayed"):
        return hit
    a = hit["action"]
    # ④ 坐标可读 + **回执实际存在**：reply_msg_id 必须在目标卡的会话块头
    # [[card-msg id=...]] 里真实存在（不是调用方编造的串）
    try:
        path = session_facade._session_path(work_id, target_card)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        if str(coordination_msg_id) not in raw:
            return {"ok": False, "code": "unreadable_evidence",
                    "why": "原统筹指令消息号在目标会话中不可读"}
        # 回报消息落在 report_up 的**收件人会话**（父卡/创建者），不在来源卡
        # ——扫本 work 各卡会话找 [[card-msg id=reply_msg_id]] 真实原件
        if not str(reply_msg_id):
            return {"ok": False, "code": "reply_not_found",
                    "why": "回执消息号为空"}
        # [返工①] 按**落盘消息自身**核验：块头 type/from/reply_to 必须
        # 分别是 msg_report / 目标卡 / 原消息号；记录收件会话坐标供打开原件
        import re as _re
        marker = _re.compile(
            r"\[\[card-msg id=" + _re.escape(str(reply_msg_id))
            + r"\s+type=([^\s\]]+)\s+from=([^\s\]]+)\s+"
              r"reply_to=([^\]\s]*)")
        found = False
        reply_session_card = ""
        try:
            from ..storage import session_store
            sess_dir = session_store.WORKS_DIR / work_id / "sessions"
            candidates = []
            if receipt_card:
                cp = session_facade._session_path(work_id, receipt_card)
                if cp.exists():
                    candidates.append(cp)
            if not candidates and sess_dir.exists():
                candidates = list(sess_dir.glob("*.md"))
            for path2 in candidates:
                m = marker.search(path2.read_text(encoding="utf-8"))
                if not m:
                    continue
                msg_type, msg_from, msg_reply_to = m.group(1), m.group(2), m.group(3)
                if msg_type != "msg_report":
                    return {"ok": False, "code": "wrong_reply_type",
                            "why": f"回执原件类型是 {msg_type}，不是 msg_report"}
                if msg_from != str(target_card):
                    return {"ok": False, "code": "wrong_reply_sender",
                            "why": f"回执原件发件卡 {msg_from} 不是目标卡 {target_card}"}
                if msg_reply_to != str(coordination_msg_id):
                    return {"ok": False, "code": "wrong_reply_to",
                            "why": (f"回执原件 reply_to={msg_reply_to} "
                                    f"未指向原消息号 {coordination_msg_id}")}
                reply_session_card = str(receipt_card or path2.stem)
                found = True
                break
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": "unreadable_evidence", "why": str(exc)}
        if not found:
            return {"ok": False, "code": "reply_not_found",
                    "why": f"回报消息 {reply_msg_id} 不在本 work 会话原件中"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "unreadable_evidence", "why": str(exc)}

    def _save(conn):
        _ensure_evidence(conn)
        conn.execute(
            "INSERT OR IGNORE INTO coordination_evidence"
            " (work_id,plan_card_id,assignment_id,action_key,item_id,"
            "  target_card,msg_id,reply_msg_id,reply_to,from_card,"
            "  reply_session_card,reply_msg_type,"
            "  content,evidence_refs,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'verified',?)",
            (work_id, a["plan_card_id"], a["assignment_id"],
             a["action_key"], a.get("item_id"), target_card,
             str(coordination_msg_id), str(reply_msg_id), str(reply_to or ""),
             str(from_card or ""),
             str(receipt_card or reply_session_card), "msg_report",
             str(content or "")[:2000], "[]", _now()))
    card_db.write_sync(work_id, _save)
    await coordination_store.add_observation(
        work_id, a["plan_card_id"], assignment_id=a["assignment_id"],
        kind="execution_evidence", status="verified",
        item_id=a.get("item_id"),
        detail={"action_key": a["action_key"], "msg_id": coordination_msg_id,
                "reply_msg_id": reply_msg_id, "target_card": target_card,
                "from_card": from_card})
    return {"ok": True, "action_key": a["action_key"],
            "plan_card_id": a["plan_card_id"], "item_id": a.get("item_id"),
            "replayed": False}


def read_execution_evidence(work_id: str, plan_card_id: str,
                            action_key: str) -> list[dict[str, Any]]:
    """面板只读回读：某行动的可打开执行证据。"""
    def _q(conn):
        try:
            rows = conn.execute(
                "SELECT * FROM coordination_evidence"
                " WHERE work_id=? AND plan_card_id=? AND action_key=?"
                " ORDER BY created_at",
                (work_id, plan_card_id, action_key)).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [dict(r) for r in rows]
    return card_db.read_sync(work_id, _q, [])


def board_summary(work_id: str, plan_card_id: str) -> dict[str, Any]:
    """面板只读汇总（③）：行动状态、专业答复、QA 结论。"""
    def _actions(conn) -> list[dict[str, Any]]:
        try:
            rows = conn.execute(
                "SELECT * FROM coordination_actions"
                " WHERE work_id=? AND plan_card_id=? ORDER BY created_at",
                (work_id, plan_card_id)).fetchall()
        except Exception:  # noqa: BLE001
            return []
        out = []
        for r in rows:
            d = dict(r)
            status = str(d.get("status") or "")
            if status == "accepted":
                label = "待投"
            elif status == "failed":
                label = "被拒"
            elif status in ("committed", "projected"):
                # [反例②] 三投影未完成（或 projection_verified 未落）⇒ 待补投影
                done = (d.get("projected_session") and d.get("projected_origin")
                        and d.get("projected_event")
                        and d.get("projection_verified"))
                label = ("已唤醒" if d.get("woke") else "已落信") if done \
                    else "待补投影"
            else:
                label = status
            if label in ("已落信", "已唤醒"):
                ev = read_execution_evidence(work_id, plan_card_id,
                                             str(d.get("action_key") or ""))
                if ev:
                    label = "已取得执行证据"
            out.append({"action_key": d.get("action_key"),
                        "target_card": d.get("target_card"),
                        "item_id": d.get("item_id"),
                        "acceptance": d.get("acceptance") or "（缺项）",
                        "rev": d.get("rev"), "receipt_id": d.get("receipt_id"),
                        "msg_id": d.get("msg_id"), "status": label,
                        "execution_evidence": [
                            {"reply_msg_id": e["reply_msg_id"],
                             "from_card": e["from_card"],
                             "reply_session_card": e["reply_session_card"],
                             "reply_seq": e["reply_seq"],
                             "receipt_session": e["reply_session_card"],
                             "content": e["content"][:300],
                             "created_at": e["created_at"]}
                            for e in read_execution_evidence(
                                work_id, plan_card_id,
                                str(d.get("action_key") or ""))],
                        "projected": {"session": bool(d.get("projected_session")),
                                      "origin": bool(d.get("projected_origin")),
                                      "event": bool(d.get("projected_event"))},
                        "woke": bool(d.get("woke"))})
        return out

    def _questions(conn) -> list[dict[str, Any]]:
        try:
            rows = conn.execute(
                "SELECT * FROM coordination_questions"
                " WHERE work_id=? AND plan_card_id=? ORDER BY created_at",
                (work_id, plan_card_id)).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [{"question_id": d["question_id"],
                 "function_id": d["function_id"], "item_id": d["item_id"],
                 "status": d["status"],
                 "answer": str(d.get("answer") or "")[:400],
                 "requirements_version": d.get("requirements_version"),
                 "answer_rev": d.get("answer_rev")}
                for d in (dict(r) for r in rows)]

    return {"actions": card_db.read_sync(work_id, _actions, []),
            "questions": card_db.read_sync(work_id, _questions, []),
            "qa": qa_records(work_id, plan_card_id)}


def qa_records(work_id: str, plan_card_id: str) -> list[dict[str, Any]]:
    """正式回读：事项的 QA 结论（通过/失败/未验证）＋原始证据。"""
    from ..storage import function_store as _fs

    def _q(conn):
        try:
            rows = conn.execute(
                "SELECT * FROM coordination_qa WHERE work_id=? AND plan_card_id=?"
                " ORDER BY created_at", (work_id, plan_card_id)).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [dict(r) for r in rows]
    rows = card_db.read_sync(work_id, _q, [])
    out = []
    for r in rows:
        r.pop("deployment_args", None)
        dep = None
        if r.get("deployment_id"):
            try:
                dep = _fs.get_deployment(work_id, int(r["deployment_id"]))
            except Exception:  # noqa: BLE001
                dep = None
        status = str((dep or {}).get("status") or "")
        conclusion = str(r.get("conclusion") or (dep or {}).get("conclusion") or "")
        verdict = str(r.get("verdict") or "")
        # [反例] done 只说明差事跑完，**不是通过**——没有明确结论就未验证
        if not verdict:
            qa_card = str(r.get("qa_card") or "")
            if qa_card:
                try:
                    import asyncio as _as
                    # qa_records 是同步读路径：用会话文件原文判断真实结论
                    from . import session_facade as _sf
                    path = _sf._session_path(work_id, qa_card)
                    raw = path.read_text(encoding="utf-8") if path.exists() else ""
                    tail = raw[-2000:]
                    if any(k in tail for k in ("未通过", "验收失败", "verdict=failed")):
                        verdict = "failed"
                    elif any(k in tail for k in ("验收通过", "verdict=passed")):
                        verdict = "passed"
                except Exception:  # noqa: BLE001
                    pass
            if not verdict:
                verdict = "unverified"
        out.append({**r, "verdict": verdict, "deployment_status": status,
                    "wake_status": (dep or {}).get("wake_status", "pending"),
                    "conclusion": conclusion,
                    "evidence_refs": json.loads(r.get("evidence_refs") or "[]")})
    return out


async def ingest_qa_report(work_id: str, deployment_id: int,
                           content: str,
                           explicit_verdict: str = "") -> dict[str, Any]:
    """[反例①] 正式 QA 回执（card_tools._h_report_deploy）按**差事编号**
    桥回原事项：解析通过/失败/未验证与证据，落 coordination_qa。
    测试直调 qa_verdict 不算正式回执——本函数只由正式入口调用。
    """
    from ..storage.coordination_store import _now

    def _find(conn):
        try:
            row = conn.execute(
                "SELECT * FROM coordination_qa WHERE work_id=?"
                " AND deployment_id=?", (work_id, int(deployment_id))).fetchone()
        except Exception:  # noqa: BLE001
            return None
        return dict(row) if row else None
    rec = card_db.read_sync(work_id, _find, None)
    if not rec:
        return {"ok": False, "why": f"没有差事编号 {deployment_id} 对应的统筹 QA 记录"}

    text = str(content or "")
    # [反例③] 只认**显式结论**：verdict= / 结论：开头行；说明性文字
    # （如"验收通过的标准"）不得被当成通过——无明确结论＝未验证。
    import re as _re
    explicit = str(explicit_verdict or "").strip().lower()
    if explicit not in ("passed", "failed", "unverified"):
        explicit = ""
    # ① 任意位置的机器枚举 verdict=passed/failed/unverified（全词匹配）
    if not explicit:
        m = _re.search(r"\bverdict\s*=\s*(passed|failed|unverified)\b",
                       text, _re.I)
        if m:
            explicit = m.group(1).lower()
    # ② 中文「结论：」行的**完整枚举**（"通过率尚未统计"不是通过）
    if not explicit:
        fail_words = {"failed", "fail", "失败", "不通过", "未通过",
                      "验收失败", "验收未通过"}
        pass_words = {"passed", "pass", "通过", "验收通过", "全部通过"}
        for line in text.splitlines():
            low = line.strip().lower()
            if not low.startswith("结论："):
                continue
            val = low.split("：", 1)[-1].strip()
            head = _re.split(r"[。；;，,（(\s—]", val, 1)[0]
            if head in fail_words:
                explicit = "failed"
            elif head in pass_words:
                explicit = "passed"
            elif head in ("unverified", "未验证"):
                explicit = "unverified"
            break
    verdict = explicit or "unverified"
    conclusion = text[:1200]

    def _save(conn):
        coordination_store._ensure(conn)
        _ensure_qa(conn)
        conn.execute(
            "UPDATE coordination_qa SET verdict=?, conclusion=?,"
            " status=?, updated_at=? WHERE qa_id=?",
            (verdict, conclusion,
             "answered" if verdict != "unverified" else "dispatched",
             _now(), rec["qa_id"]))
    card_db.write_sync(work_id, _save)
    await coordination_store.add_observation(
        work_id, rec["plan_card_id"], assignment_id=rec["assignment_id"],
        kind="qa_report", status=verdict, item_id=rec.get("item_id"),
        detail={"qa_id": rec["qa_id"], "deployment_id": int(deployment_id),
                "conclusion": conclusion[:500],
                "source": "formal_report_deploy"})
    return {"ok": True, "qa_id": rec["qa_id"], "verdict": verdict,
            "conclusion": conclusion}


async def qa_verdict(work_id: str, qa_id: str, *,
                     verdict: str, conclusion: str,
                     evidence_refs: Optional[list[str]] = None) -> dict[str, Any]:
    """真实 QA 回执回传（通过/失败/未验证）写入原事项记录。"""
    from ..storage.coordination_store import _now

    def _fn(conn):
        coordination_store._ensure(conn)
        _ensure_qa(conn)
        cur = conn.execute(
            "UPDATE coordination_qa SET verdict=?, conclusion=?,"
            " evidence_refs=?, status=?, updated_at=? WHERE qa_id=?",
            (str(verdict), str(conclusion),
             json.dumps([str(x) for x in (evidence_refs or [])],
                        ensure_ascii=False), _now(), _now(), qa_id))
        return cur.rowcount > 0
    ok = bool(card_db.write_sync(work_id, _fn))
    return {"ok": ok, "qa_id": qa_id}


async def coordination_ask(work_id: str, plan_card_id: str, *,
                           assignment_id: str, rev: int,
                           item_id: Optional[str], function_id: str,
                           question: str, requirements_version: str,
                           evidence_refs: list[str],
                           turn_id: str, model_used: str) -> dict[str, Any]:
    """[下一批 ②] 指定职能的一次**只读**回合：稳定问题标识、职责书授权、
    入账前授权复核；已答复回读原答案（重试/并发同键）。

    宿主绑定 work/计划卡/任命/读入版本/事项/问题/需求版本/证据引用；
    专家只拿按当前授权组装的材料，**零工具**（tools=None），正文里的调用
    指令不触发写入。专家无权改计划、下令或替用户改承诺。
    """
    from . import function_room
    from ..storage import function_store as _fs

    fid = str(function_id or "").strip()
    text = str(question or "").strip()
    if not fid.startswith("fn_") or not _fs.function_exists(work_id, fid) \
            or not text:
        return {"ok": False, "code": "invalid_function",
                "why": f"职能不存在或问题为空: {fid}"}
    # 自问拒绝（统筹本人担任该职能）
    appt = _current_assignment(work_id, plan_card_id)
    if not appt or str(appt.get("assignment_id")) != str(assignment_id) \
            or int(appt.get("rev") or 0) != int(rev) \
            or appt.get("state") != "active":
        return {"ok": False, "code": "stale_token",
                "why": "启动前任命/版本复核未通过"}
    if str(appt.get("function_id") or "") == fid:
        return {"ok": False, "code": "self_ask",
                "why": "统筹本人担任该职能，自问自答由宿主拒绝"}
    ident = function_room.identity_for(work_id, fid)
    charter = str((ident.get("charter") or {}).get("text") or "").strip()
    if not charter:
        return {"ok": False, "code": "no_charter",
                "why": f"职能 {fid} 缺职责书（不启动专家回合）"}
    # 需求版本/证据可见性：引用须本 work + seen（与发令同口径）
    allowed_refs = []
    allowed_cards = _allowed_card_ids(work_id, (appt.get("observe_scope")
                                                or appt.get("grants", {})
                                                .get("observe") or {}))
    for ref in evidence_refs:
        ref_s = str(ref)
        if not ref_s.startswith(f"event:{work_id}:"):
            return {"ok": False, "code": "bad_evidence",
                    "why": f"证据不属于本 work: {ref_s}"}
        card_id, seq = _ref_card(ref_s), _ref_seq(ref_s)
        if card_id not in allowed_cards:
            return {"ok": False, "code": "bad_evidence",
                    "why": f"证据来源卡不在授权观察范围: {ref_s}"}

        def _seen(conn):
            try:
                return bool(conn.execute(
                    "SELECT 1 FROM coordination_seen WHERE work_id=?"
                    " AND plan_card_id=? AND assignment_id=? AND card_id=? AND seq=?",
                    (work_id, plan_card_id, str(assignment_id), card_id, seq)
                ).fetchone())
            except Exception:  # noqa: BLE001
                return False
        if not card_db.read_sync(work_id, _seen, False):
            return {"ok": False, "code": "bad_evidence",
                    "why": f"证据不是本任命的观察事件: {ref_s}"}
        allowed_refs.append(ref_s)
    # 稳定标识：**不含版本**（同一问题跨版本重试同键，回读原答复）
    qhash = _content_hash(work_id, str(assignment_id), str(item_id or ""),
                          fid, text)
    question_id = f"q_{qhash[:20]}"

    def _claim(conn) -> dict[str, Any]:
        coordination_store._ensure(conn)
        _ensure_questions(conn)
        row = conn.execute(
            "SELECT * FROM coordination_questions WHERE question_id=?",
            (question_id,)).fetchone()
        if row is not None:
            d = dict(row)
            if d["status"] == "answered":
                # [反例③] 重放前核验原答复所用证据**仍获当前授权**：
                # 撤权后的旧答复留作历史（问题行不动），但不回流模型上下文
                refs = json.loads(d.get("evidence_refs") or "[]")
                cur = _current_assignment(work_id, plan_card_id)
                allowed = _allowed_card_ids(
                    work_id, (cur or {}).get("observe_scope") or {})
                for ref in refs:
                    cid, seq = _ref_card(str(ref)), _ref_seq(str(ref))
                    if cid not in allowed:
                        return {"ok": False, "code": "evidence_revoked",
                                "why": (f"原答复证据已不在当前授权观察范围: {ref}"
                                        "（旧答复保留在历史，不回流）")}
                    hit = conn.execute(
                        "SELECT 1 FROM coordination_seen WHERE work_id=?"
                        " AND plan_card_id=? AND assignment_id=? AND card_id=? AND seq=?",
                        (work_id, plan_card_id, str(assignment_id), cid, seq)
                    ).fetchone()
                    if not hit:
                        return {"ok": False, "code": "evidence_revoked",
                                "why": (f"原答复证据已不是本任命的观察事件: {ref}"
                                        "（旧答复保留在历史，不回流）")}
                return {"ok": True, "question_id": question_id,
                        "replayed": True, "answer": d["answer"],
                        "function_id": d["function_id"], "item_id": d["item_id"]}
            return {"ok": False, "code": "in_flight",
                    "why": f"问题已在处理中（{d['status']}）"}
        from ..storage.coordination_store import _now
        conn.execute(
            "INSERT INTO coordination_questions"
            " (question_id,work_id,plan_card_id,assignment_id,rev,item_id,"
            "  function_id,question,requirements_version,evidence_refs,"
            "  status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,'queued',?)",
            (question_id, work_id, plan_card_id, str(assignment_id), int(rev),
             item_id or None, fid, text, str(requirements_version or ""),
             json.dumps(allowed_refs, ensure_ascii=False), _now()))
        return {"ok": True, "question_id": question_id, "replayed": False,
                "function_id": fid, "item_id": item_id}

    try:
        claim = await card_db.run_write(work_id, _claim)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "error", "why": str(exc)}
    if not claim.get("ok") or claim.get("replayed"):
        return claim

    # 只读回合（空工具面）：职责书+问题+需求版本+证据引用
    system = (f"你是 {ident.get('function_name') or fid}（{fid}），按职责书回答统筹"
              f"提出的一个问题。职责书：\n{charter}\n\n"
              "你只能回答问题本身——**没有工具**，不许改计划、不许发令、"
              "不许替用户改承诺。输出一个 JSON 对象："
              '{"answer": "结论（含承诺/验收/分歧或技术方案/验证办法）"}')
    body = (f"事项：{item_id or '（无）'}\n问题：{text}\n"
            f"需求版本：{requirements_version or '（未标注）'}\n"
            f"证据引用：{', '.join(allowed_refs[:5]) or '（无）'}")
    try:
        result, used = await model_service.call_main(
            system, body, model_name=str((appt.get("models") or {})
                                         .get("strong") or ""),
            tools=None, caller="coordination_expert", work_id=work_id,
            card_id=plan_card_id,
            before_send=_make_send_guard(work_id, plan_card_id,
                                         str(assignment_id), int(rev)))
        parsed = json.loads(_strip_json_block(str(result.get("content") or "")))
        answer = str(parsed.get("answer") or "").strip()
        if not answer:
            raise ValueError("专家答复为空")
    except Exception as exc:  # noqa: BLE001
        from ..storage.coordination_store import _now
        card_db.write_sync(
            work_id,
            lambda c: c.execute(
                "UPDATE coordination_questions SET status='error' WHERE question_id=?",
                (question_id,)))
        await coordination_store.add_observation(
            work_id, plan_card_id, assignment_id=assignment_id,
            kind="ask", status="error", item_id=item_id,
            detail={"question_id": question_id, "function_id": fid,
                    "why": str(exc)}, model_used=model_used)
        return {"ok": False, "code": "expert_error", "why": str(exc)}

    # 入账前再核任命/版本；晚到旧版本只存原记录、不覆盖当前事项
    now_appt = _current_assignment(work_id, plan_card_id)
    stale = (not now_appt
             or str(now_appt.get("assignment_id")) != str(assignment_id)
             or int(now_appt.get("rev") or 0) != int(rev))
    from ..storage.coordination_store import _now

    def _save(conn) -> None:
        coordination_store._ensure(conn)
        _ensure_questions(conn)
        conn.execute(
            "UPDATE coordination_questions SET status=?, answer=?,"
            " answer_rev=?, answered_at=? WHERE question_id=?",
            ("stale" if stale else "answered", answer, int(rev),
             _now(), question_id))
    card_db.write_sync(work_id, _save)
    await coordination_store.add_observation(
        work_id, plan_card_id, assignment_id=assignment_id,
        kind="ask_answered", status="stale" if stale else "answered",
        item_id=item_id, detail={
            "question_id": question_id, "function_id": fid,
            "answer": answer[:500], "answer_rev": rev,
            "requirements_version": requirements_version,
            "evidence_refs": allowed_refs},
        model_used=str(used or model_used))
    return {"ok": True, "question_id": question_id, "replayed": False,
            "function_id": fid, "item_id": item_id, "answer": answer,
            "stale": stale}


async def _project_receipt(work_id: str, plan_card_id: str,
                           action_key: str) -> dict[str, Any]:
    from ..storage import origin_store, session_facade, session_store

    row = _read_action(work_id, plan_card_id, action_key)
    if not row or not row["msg_id"]:
        return {"ok": False, "why": "收件行不存在或未定稿"}
    target = row["target_card"]
    actor = row["actor"]
    msg_id = row["msg_id"]

    def _write_session() -> dict[str, Any]:
        projection = session_facade.append_directive_sync(
            work_id, target, actor, _instruction_body(row), msg_id)
        block = projection.get("block")
        if not block:
            raise OSError("会话未返回收件块")
        block = {**block, "event_type": "msg_directive"}
        if origin_store.write_blocks_sync(work_id, target, [block]) != 1:
            raise OSError("原文账未确认写入")
        return block

    async def _project_session() -> dict[str, Any]:
        async with session_store._session_lock(work_id, target):
            return await asyncio.to_thread(_write_session)

    try:
        # 协程被取消时，写线程仍持有会话锁，直到落盘完成。
        task = asyncio.create_task(_project_session())
        try:
            block = await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise
        event = await session_facade._emit_block_event(
            work_id, target, block, "msg_directive",
            origin={"from_card": actor, "msg_id": msg_id}, once_per_message=True)
        if not event:
            raise OSError("事件流未确认写入")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[coordination] 收件投影待重试: %s", exc)
        return {"ok": False, "why": str(exc)}
    return {"ok": True}


_active_wakes: set[tuple[str, str]] = set()


def _wake_gate(conn, action: dict[str, Any]) -> tuple[bool, str]:
    work_id, plan_card = action["work_id"], action["plan_card_id"]
    ok, _, gate = _gates(
        work_id, plan_card, action["assignment_id"], action["rev"],
        action["target_card"], json.loads(action["evidence_refs"]), conn=conn,
        item_id=action.get("item_id") or "")
    if not ok:
        return False, gate["why"]
    if gate["function_id"] != action["actor"]:
        return False, "原任命职能身份不符"
    code, why = _directive_permission(work_id, action["actor"], action["target_card"])
    return not code, why


def _wake_status_tx(conn, action: dict[str, Any], status: str, why: str = "") -> None:
    coordination_store.add_observation_tx(
        conn, action["work_id"], action["plan_card_id"],
        assignment_id=action["assignment_id"], kind="instruction_woke", status=status,
        detail={"receipt_id": action["receipt_id"], "action_key": action["action_key"],
                "woke": status == "woke", "why": why})


def _admit_wake(work_id: str, plan_card_id: str, action_key: str, target: str) -> bool:
    """目标属主在实际接收唤醒时，原子确认原任命和行动键。"""
    def _fn(conn):
        row = conn.execute(
            "SELECT * FROM coordination_actions WHERE work_id=? AND plan_card_id=?"
            " AND action_key=? AND target_card=? AND projection_verified=1 AND woke=0",
            (work_id, plan_card_id, action_key, target)).fetchone()
        if not row:
            return False
        action = dict(row)
        ok, why = _wake_gate(conn, action)
        if not ok:
            _wake_status_tx(conn, action, "denied", why)
            return False
        conn.execute(
            "UPDATE coordination_actions SET woke=1, updated_at=? WHERE action_key=?",
            (coordination_store._now(), action_key))
        _wake_status_tx(conn, action, "woke")
        return True

    return bool(card_db.write_sync(work_id, _fn))


def _directive_permission(work_id: str, actor: str, target: str) -> tuple[str, str]:
    from ..permissions import policy

    cards = card_store.iter_card_metas(work_id)
    if not actor or not policy.can(actor, target, policy.SWITCH_READ, cards):
        return "role_denied", f"角色授面拒绝：职能 {actor} 无权向 {target} 发指令"
    try:
        policy.check_linked(actor, target, cards)
    except PermissionError as exc:
        return "no_link", str(exc)
    return "", ""


async def coordination_deliver(work_id: str, plan_card_id: str, *,
                               assignment_id: str, rev: int,
                               action_key: str) -> dict[str, Any]:
    """由目标属主提交唯一收件，再用原消息号补写和唤醒。"""
    from . import message_runner
    from ..infra import loops, procs

    action = _read_action(work_id, plan_card_id, action_key)
    if not action:
        return {"ok": False, "code": "not_found", "why": "行动键不存在（先受理）"}
    target = action["target_card"]
    if procs.is_web() or (procs.is_runner()
                          and procs.owner_index_of(work_id, target) != procs.runner_index()):
        result = await procs.forward_ctl(work_id, target, "/ctl/coordination_deliver", {
            "work_id": work_id, "card_id": plan_card_id, "assignment_id": assignment_id,
            "rev": rev, "action_key": action_key,
        })
        if "error" in result:
            # [反例①] 属主暂不可达：受理行保持 accepted，登记待投账——
            # recover_deliveries 按原行动键重投（不新建行动、不换消息号）
            from ..storage.coordination_store import _now
            await coordination_store.add_observation(
                work_id, plan_card_id, assignment_id=assignment_id,
                kind="instruction_pending_delivery", status="queued",
                detail={"action_key": action_key,
                        "why": str(result["error"]),
                        "owner": str(procs.owner_index_of(work_id, target))})
            return {"ok": False, "code": "owner_unreachable",
                    "why": result["error"]}
        return result
    lane = loops.runner_lane_for(work_id, target) or loops.main_loop()
    if lane is not None and lane is not asyncio.get_running_loop():
        return await message_runner.await_spawned(message_runner._spawn_tracked(
            coordination_deliver(work_id, plan_card_id, assignment_id=assignment_id,
                                 rev=rev, action_key=action_key),
            work_id=work_id, card_id=target))

    def _tx(conn):
        if "projection_verified" not in action:
            _ensure_actions(conn)
        row = dict(conn.execute(
            "SELECT * FROM coordination_actions WHERE id=?", (action["id"],)).fetchone())
        row["replayed"] = bool(row["msg_id"])
        if row["replayed"]:
            if not row["actor"]:
                original = conn.execute(
                    "SELECT function_id FROM coordination_appointments WHERE work_id=?"
                    " AND plan_card_id=? AND assignment_id=?",
                    (work_id, plan_card_id, row["assignment_id"])).fetchone()
                if not original:
                    return {"ok": False, "code": "projection_failed", "why": "无法核实原发令身份"}
                row["actor"] = original["function_id"]
                conn.execute("UPDATE coordination_actions SET actor=? WHERE id=?",
                             (row["actor"], row["id"]))
            return row
        if (row["assignment_id"], row["rev"]) != (assignment_id, rev):
            return {"ok": False, "code": "stale_token", "why": "令牌与原行动不符"}
        ok, code, gate = _gates(
            work_id, plan_card_id, row["assignment_id"], row["rev"], target,
            json.loads(row["evidence_refs"]), conn=conn,
            item_id=row["item_id"] or "")
        if not ok:
            conn.execute("UPDATE coordination_actions SET status='failed', updated_at=? WHERE id=?",
                         (coordination_store._now(), row["id"]))
            return {"ok": False, "code": code, "why": gate["why"]}
        actor = gate["function_id"]
        code, why = _directive_permission(work_id, actor, target)
        if code:
            return {"ok": False, "code": code, "why": why}
        row.update(status="committed", actor=actor, msg_id=f"cmsg-{uuid.uuid4().hex[:12]}",
                   receipt_id=f"rcp_{uuid.uuid4().hex[:12]}")
        conn.execute(
            "UPDATE coordination_actions SET status=?, actor=?, msg_id=?, receipt_id=?,"
            " updated_at=? WHERE id=?",
            (row["status"], row["actor"], row["msg_id"], row["receipt_id"],
             coordination_store._now(), row["id"]))
        return row

    try:
        claim = await card_db.run_write(work_id, _tx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[coordination] 收件事务异常: %s", exc)
        return {"ok": False, "code": "error", "why": str(exc)}
    if claim.get("ok") is False:
        return claim

    reply = {"ok": True, "receipt_id": claim["receipt_id"], "msg_id": claim["msg_id"],
             "target_card": target, "actor": claim["actor"], "item_id": claim["item_id"] or "",
             "body": _instruction_body(claim), "replayed": claim["replayed"], "woke": bool(claim["woke"])}
    if not claim["projection_verified"]:
        projection = await _project_receipt(work_id, plan_card_id, action_key)
        if not projection["ok"]:
            return {**reply, "ok": False, "code": "projection_failed", "why": projection["why"]}

        def _finish(conn):
            updated = conn.execute(
                "UPDATE coordination_actions SET status='projected', projection_verified=1,"
                " projected_session=1, projected_origin=1, projected_event=1, updated_at=?"
                " WHERE action_key=? AND projection_verified=0",
                (coordination_store._now(), action_key))
            if not updated.rowcount:
                return
            # 已有回执的旧行先核实缺失投影，再保留原投递账。
            if claim["status"] == "projected" and conn.execute(
                    "SELECT 1 FROM coordination_observations WHERE work_id=? AND plan_card_id=?"
                    " AND kind='instruction_delivered' AND json_extract(detail, '$.action_key')=?",
                    (work_id, plan_card_id, action_key)).fetchone():
                return
            coordination_store.add_observation_tx(
                conn, work_id, plan_card_id, assignment_id=claim["assignment_id"],
                kind="instruction_delivered", status="delivered", item_id=claim["item_id"],
                detail={"receipt_id": claim["receipt_id"], "action_key": action_key,
                        "target_card": target, "msg_id": claim["msg_id"],
                        "late": claim["replayed"]})

        try:
            await card_db.run_write(work_id, _finish)
        except Exception as exc:  # noqa: BLE001
            return {**reply, "ok": False, "code": "projection_failed", "why": str(exc)}

    def _check_wake(conn):
        row = dict(conn.execute(
            "SELECT * FROM coordination_actions WHERE id=?", (claim["id"],)).fetchone())
        if row["woke"]:
            return "woke"
        ok, why = _wake_gate(conn, row)
        if not ok:
            _wake_status_tx(conn, row, "denied", why)
            return "denied"
        return "pending"

    key = (work_id, action_key)
    if key in _active_wakes:
        return {**reply, "wake_claimed_by_other": True}
    _active_wakes.add(key)
    try:
        wake_status = card_db.write_sync(work_id, _check_wake)
        if wake_status != "pending":
            return {**reply, "woke": wake_status == "woke", "wake_denied": wake_status == "denied"}

        def _admit() -> bool:
            return _admit_wake(work_id, plan_card_id, action_key, target)

        try:
            # 普通 direct 使用同一唤醒入口。带行动键时由接收方确认唤醒，
            # 调用方断连或未收到回执也不会让原行动再次起轮。
            await message_runner.deliver_now(
                work_id, target,
                text=f"你收到一条来自统筹职能 {claim['actor']} 的指令，正文已在上文（卡间指令）。",
                steer_text=f"统筹职能 {claim['actor']} 给你发来指令：\n{reply['body']}\n请按其执行，完成后用 report_up 汇报。",
                source_kind="directive",
                source={"kind": "card_msg", "from": claim["actor"], "require_report": True,
                        "coordination_msg_id": claim["msg_id"]},
                admission=_admit, action_key=action_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[coordination] 落信成功但唤醒待重试: %s", exc)
        def _settle_attempt(conn):
            row = dict(conn.execute(
                "SELECT * FROM coordination_actions WHERE id=?", (claim["id"],)).fetchone())
            if row["woke"]:
                return "woke"
            ok, why = _wake_gate(conn, row)
            if not ok:
                _wake_status_tx(conn, row, "denied", why)
                return "denied"
            queued = conn.execute(
                "SELECT 1 FROM coordination_observations WHERE work_id=? AND plan_card_id=?"
                " AND kind='instruction_woke' AND status='queued'"
                " AND json_extract(detail, '$.action_key')=?",
                (work_id, plan_card_id, action_key)).fetchone()
            if not queued:
                _wake_status_tx(conn, row, "queued")
            return "queued"

        wake_status = card_db.write_sync(work_id, _settle_attempt)
        reply["woke"] = wake_status == "woke"
        if wake_status == "denied":
            reply["wake_denied"] = True
        return reply
    finally:
        _active_wakes.discard(key)


async def recover_deliveries(work_id: str) -> None:
    """服务 tick 补齐已提交收件，以及仍有原授权的未确认唤醒。"""
    def _pending(conn):
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordination_actions'"
                ).fetchone():
            return []
        if not any(col["name"] == "projection_verified"
                   for col in conn.execute("PRAGMA table_info(coordination_actions)")):
            return None
        return conn.execute(
            "SELECT a.plan_card_id,a.assignment_id,a.rev,a.action_key FROM coordination_actions a"
            " LEFT JOIN coordination_appointments p ON p.assignment_id=a.assignment_id"
            " AND p.work_id=a.work_id AND p.plan_card_id=a.plan_card_id"
            " WHERE a.work_id=? AND a.status IN ('accepted','committed','projected') AND ("
            " a.projection_verified=0 OR (a.woke=0 AND p.state='active'"
            " AND p.mode='live' AND p.rev=a.rev)) ORDER BY a.updated_at,a.id",
            (work_id,)).fetchall()

    pending = card_db.read_sync(work_id, _pending, [])
    if pending is None:
        await card_db.run_write(work_id, _ensure_actions)
        pending = card_db.read_sync(work_id, _pending, [])
    for row in pending:
        await coordination_deliver(work_id, row["plan_card_id"],
                                   assignment_id=row["assignment_id"], rev=row["rev"],
                                   action_key=row["action_key"])


def _instruction_body(row: Any) -> str:
    """收件正文（事项/依据/指令）——权威行与重放共用同一拼装。"""
    body = (f"【事项 {row['item_id']}】\n" if row["item_id"] else "")
    body += row["instruction"]
    acceptance = str(row.get("acceptance") or "").strip()
    body += f"\n验收判据:{acceptance}" if acceptance else "\n验收判据:（缺项）"
    try:
        refs = json.loads(row["evidence_refs"] or "[]")
    except (TypeError, ValueError):
        refs = []
    if refs:
        body += f"\n依据：{', '.join(refs[:3])}"
    return body


def _ref_card(coord: str) -> str:
    parts = str(coord).split(":")
    return parts[2] if len(parts) >= 4 else "__bad__"


def _ref_seq(coord: str) -> int:
    parts = str(coord).split(":")
    try:
        return int(parts[3]) if len(parts) >= 4 else -1
    except ValueError:
        return -1


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


def _coord_history_lines(blocks: list[dict], allowed: set[str]) -> list[str]:
    """旧块仅留审计。模型只续用宿主知道来源且仍在授权范围内的记录。"""
    lines = []
    for block in blocks:
        scope = block.get("coordination_scope")
        cards = scope.get("card_ids") if isinstance(scope, dict) else None
        body = str(block.get("content") or "").strip()
        if (body and isinstance(cards, list)
                and scope.get("kind") != "user_chat"
                and all(isinstance(cid, str) and cid in allowed for cid in cards)):
            lines.append(f"- {body[:400]}")
    return lines


async def _read_coord_history(work_id: str, sid: str, allowed: set[str]) -> str:
    from ..storage import session_facade

    try:
        blocks = await session_facade.read(work_id, sid)
    except Exception as exc:
        logger.debug("[coordination] 读专属会话历史失败 sid=%s: %s", sid, exc)
        return ""
    return "\n".join(_coord_history_lines(blocks, allowed))


def _user_chat_blocks(blocks: list[dict], assignment_id: str) -> list[dict]:
    return [block for block in blocks
            if block.get("actor") in ("human", "assistant")
            and (scope := block.get("coordination_scope"))
            and isinstance(scope, dict)
            and scope.get("kind") == "user_chat"
            and scope.get("assignment_id") == assignment_id]


async def read_user_chat(work_id: str, plan_card_id: str) -> list[dict]:
    from ..storage import session_facade

    panel = coordination_store.panel(work_id, plan_card_id)
    if not panel.get("assignment_id"):
        return []
    blocks = await session_facade.read(work_id, _session_key(plan_card_id))
    return _user_chat_blocks(blocks, panel["assignment_id"])


async def run_user_chat(work_id: str, plan_card_id: str, *, assignment_id: str,
                        expected_rev: int, content: str) -> None:
    """Use the appointed strong route for conversation in the plan session."""
    from ..storage import session_facade

    error = coordination_store.CoordinationError
    content = content.strip()
    if not content:
        raise error("validation", "请输入消息")
    appt = _current_assignment(work_id, plan_card_id, require_active=True)
    if not appt:
        raise error("denied", "请先任命或恢复统筹")
    if appt["assignment_id"] != assignment_id or appt["rev"] != expected_rev:
        raise error("conflict", "统筹任命或计划版本已变化，请刷新后发送")

    sid = _session_key(plan_card_id)
    owner = f"coord_chat:{uuid.uuid4().hex}"
    if not session_facade.turn_try_acquire(work_id, sid, owner):
        raise error("conflict", "统筹正在回复上一条消息")
    guard = _make_send_guard(work_id, plan_card_id, assignment_id, expected_rev)
    try:
        allowed = _allowed_card_ids(work_id, appt["observe_scope"])
        await _ensure_session_registered(work_id, plan_card_id, sid, appt["function_id"])
        blocks = await session_facade.read(work_id, sid)
        prior = []
        for block in _user_chat_blocks(blocks, assignment_id)[-20:]:
            sources = block["coordination_scope"].get("card_ids")
            if isinstance(sources, list) and all(cid in allowed for cid in sources):
                prior.append({"role": "user" if block["actor"] == "human" else "assistant",
                              "content": str(block["content"])[:4000]})
        instructions = (
            "你是项目统筹，正在与用户讨论这份计划。用自然语言回复。"
            "根据所给计划和查证记录回答，缺少证据时说明。"
            "可以讨论调整方案，但本次对话不会保存计划、发令或创建卡片。"
        )
        system = _turn_system(work_id, appt, instructions)
        # [2026-11-23 · 计划板] 条目走 turn_tail 当轮板投影——不再把
        # 旧事项列表拼进 system（同轮只出一份当前板）。
        _plan_view = {k: v for k, v in plan_for_model(appt, allowed).items()
                      if k != "items"}
        system += "\n\n当前计划\n" + json.dumps(_plan_view, ensure_ascii=False)
        history = _coord_history_lines(blocks, allowed)[-20:]
        if history:
            system += "\n\n已有统筹记录\n" + "\n".join(history)
        try:
            result, model = await model_service.call_main(
                system, content, prior_messages=prior or None,
                tools=None, model_name=appt["models"]["strong"],
                caller="coordination_chat", work_id=work_id, card_id=plan_card_id,
                turn_tail=_board_tail(appt, allowed),
                before_send=guard)
        except Exception as exc:
            try:
                await guard()
            except RuntimeError as stale:
                raise error("conflict", str(stale)) from exc
            logger.warning("[coordination] 对话调用失败 card=%s: %s", plan_card_id, exc)
            raise error("model_unavailable", "强模型暂未返回，请重试") from exc
        try:
            await guard()
        except RuntimeError as exc:
            raise error("conflict", str(exc)) from exc
        answer = str(result.get("content") or "").strip()
        if not answer:
            raise error("model_unavailable", "强模型没有返回可显示的回复")
        scope = {"kind": "user_chat", "assignment_id": assignment_id,
                 "card_ids": sorted(allowed)}
        await session_facade.append_many(work_id, sid, [
            {"actor": "human", "content": content, "coordination_scope": scope},
            {"actor": "assistant", "content": answer, "coordination_scope": scope,
             "model": model},
        ])
    finally:
        session_facade.turn_release(work_id, sid, owner)


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
        # [2026-11-23 · 计划板] 条目走当轮 turn_tail 板投影——预拼的整份
        # 计划不再附一份旧事项列表（同轮只出一份当前板）。
        current = {k: v for k, v in current.items() if k != "items"}
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

    # [2026-11-23 · 计划板] 当轮板快照：每跳出站 turn_tail 带**一份**
    # （改板成功后按保存回执 rev 换新——见 coord_plan_board 分支）。
    tail = _board_tail(appt, allowed_now)

    # 查证预算：2×卡数（每卡查证两件）＋8 跳冗余——统筹轮是宿主自含循环
    # （非卡回合，卡回合零闸纪律不适用），预算防模型失控烧钱，有结构依据。
    budget = max(16, 2 * len(cards) + 8)
    hops: list[dict[str, Any]] = []       # 每跳审计：action/参数/结果摘要
    reads: dict[str, dict[str, Any]] = {}
    board_requirement_reads: dict[str, dict] = {}
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
                # [2026-11-23 · 计划板] 每次出站带一份当轮板（续跳同款；
                # 改板后换新 rev 快照）
                turn_tail=tail,
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

        if parsed.get("plan") is not None and \
                parsed.get("instruction") is not None:
            if is_review:
                status, err_text = "unparsed", "独立回看轮只读，拒绝组合发令"
                break
            combo = await _combined_submit(
                work_id, plan_card_id, assignment_id=aid, rev=snap_rev,
                turn_id=turn_id, hop=hop, plan_raw=parsed.get("plan"),
                instr_raw=parsed.get("instruction"),
                note=str(parsed.get("note") or ""),
                model_used=model_used, usage=usage_acc,
                allowed_now=allowed_now)
            status = combo["status"]
            err_text = str((combo.get("detail") or {}).get("error") or "")
            combo_result = combo
            hops.append({"hop": hop, "action": "coord_submit_combined",
                         **{k: v for k, v in combo.items()
                            if k in ("saved_rev", "instruction",
                                     "receipt_id", "msg_id")}})
            break

        if parsed.get("plan") is not None:
            # 提交路径：完整校验（空计划/劣质形状不记成功）
            visible = coordination_board.visible_items(
                _current_assignment(work_id, plan_card_id), allowed_now)
            plan, plan_err = _validate_submitted_plan(
                parsed.get("plan"), registered_items=visible)
            if plan is None:
                status = "unparsed"
                err_text = f"第 {hop} 跳提交被拒：{plan_err}"
                break
            submitted = plan
            submit_note = str(parsed.get("note") or "")
            status = "parsed"
            err_text = ""
            hops.append({"hop": hop, "action": "coord_submit_plan",
                         "items": len(visible),
                         "standards": len(plan.get("wake_standards") or [])})
            break

        action = str(parsed.get("action") or "")
        if action not in ("coord_read_card", "coord_read_events",
                          "coord_submit_instruction", "coord_ask_function",
                          "coord_qa_request", "coord_plan_board"):
            status = "unparsed"
            err_text = (f"第 {hop} 跳指令无效: {action!r}"
                        "（只接受 coord_read_card/coord_read_events/"
                        "coord_submit_instruction/coord_ask_function/"
                        "coord_qa_request/coord_plan_board/提交 plan）")
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
        # [2026-11-21 · 用户令反例②] 独立回看轮**只读**——发令/问询一律拒绝
        if is_review and action in ("coord_submit_instruction",
                                    "coord_ask_function", "coord_qa_request"):
            status = "unparsed"
            err_text = "独立回看轮是只读回看，不允许发令/问询（零写入）"
            break
        if action == "coord_plan_board":
            # [2026-11-23 · 计划板] 读写同一工具：宿主把任命/读入版本/授权
            # 绑到调用——read 默认未完成项、显式 item_id 可读已完成；
            # apply 整批原子保存，回执给新 rev。
            op = str(args.get("op") or "read")
            if op == "read":
                offset, limit = args.get("offset"), args.get("limit", 20)
                if (offset is not None and type(offset) is not int) or type(limit) is not int:
                    out = {"error": "offset 和 limit 必须为整数"}
                else:
                    out = coordination_board.read_board(
                        now_appt2, live_allowed,
                        item_id=str(args.get("item_id") or "") or None, offset=offset, limit=limit)
                    if out.get("item"):
                        out.update(coordination_store.board_item_receipts(
                            work_id, plan_card_id, aid, out["item"]["item_id"], live_allowed))
                result_txt = ("计划板读取结果：\n"
                              + json.dumps(out, ensure_ascii=False))
            elif op == "apply":
                if is_review:
                    status, err_text = "unparsed", \
                        "独立回看轮只读计划板——apply 一律拒绝"
                    break
                try:
                    rec = await coordination_store.board_apply(
                        work_id, plan_card_id, assignment_id=aid,
                        expected_rev=snap_rev,
                        changes=args.get("changes"),
                        reason=str(args.get("reason") or ""), by=f"model:{model_used}",
                        source_cards=live_allowed, requirement_reads=board_requirement_reads)
                except coordination_store.CoordinationError as exc:
                    # 冲突/校验失败都如实回给模型（整批零写入——让它
                    # 按当前版本重读后重试，不误报成功）
                    result_txt = (
                        f"计划板保存被拒（{exc.code}）：{exc.message}\n"
                        + json.dumps(exc.extra, ensure_ascii=False))
                    hops.append({"hop": hop, "action": "coord_plan_board",
                                 "op": "apply", "code": exc.code})
                else:
                    # 用保存回执的新 rev 刷新本轮材料与版本闸——
                    # 后续 _recheck/send_guard 以新读入版本为准。
                    snap_rev = int(rec["rev"])
                    appt = _current_assignment(work_id, plan_card_id)
                    tail = _board_tail(appt, live_allowed)
                    result_txt = (
                        f"计划板已保存（rev.{snap_rev}）：\n"
                        + json.dumps(rec, ensure_ascii=False))
                    await _hop_log(
                        f"改板 {rec['changed']} → rev.{snap_rev}", "计划板")
                    hops.append({"hop": hop, "action": "coord_plan_board",
                                 "op": "apply", "rev": snap_rev,
                                 "changed": rec["changed"],
                                 "allocated": rec["allocated"]})
            else:
                result_txt = (f"coord_plan_board 的 op 只接受 read/apply："
                              f"{op!r}")
                hops.append({"hop": hop, "action": "coord_plan_board",
                             "op": op, "code": "validation"})
            history.append({"role": "assistant", "content": content})
            current_user = result_txt + "\n继续按协议输出下一个 JSON。"
            history.append({"role": "user", "content": current_user})
            continue
        if action == "coord_submit_instruction":
            # [任务四 · 返工] 受理（只给稳定行动键）→ 落信（同事务重验授权、
            # 唯一收件、正式 direct 通信面）→ 三账分离；受理后本轮终结。
            intake = await coordination_intake(
                work_id, plan_card_id, assignment_id=aid,
                rev=snap_rev, turn_id=turn_id,
                item_id=args.get("item_id"),
                target_card=str(args.get("target_card") or ""),
                instruction=str(args.get("instruction") or ""),
                evidence_refs=[str(x) for x in
                               (args.get("evidence_refs") or [])],
                action_seq=hop,
                acceptance=str(args.get("acceptance") or ""),
                items_allowed={it["item_id"] for it in
                               coordination_board.visible_items(now_appt2, live_allowed)})
            if not intake.get("ok"):
                status = "unparsed"
                err_text = (f"第 {hop} 跳发令受理被拒（{intake.get('code')}）："
                            f"{intake.get('why')}")
                break
            # 受理账已由 coordination_intake 落（落信/唤醒由 deliver 分账）
            delivered = await coordination_deliver(
                work_id, plan_card_id, assignment_id=aid, rev=snap_rev,
                action_key=intake["action_key"])
            if not delivered.get("ok"):
                status = "unparsed"
                err_text = (f"第 {hop} 跳发令落信失败（{delivered.get('code')}）："
                            f"{delivered.get('why')}（可按原动作重试）")
                break
            receipt_id = delivered["receipt_id"]
            await _hop_log(f"发令落信 {receipt_id} → {args.get('target_card')}",
                           "发令", cards={str(args.get("target_card") or "")})
            hops.append({"hop": hop, "action": "coord_submit_instruction",
                         "receipt_id": receipt_id,
                         "action_key": intake["action_key"],
                         "replayed": delivered.get("replayed", False),
                         "woke": delivered.get("woke", False)})
            status = "parsed"
            err_text = ""
            return {"status": "parsed", "receipt_id": receipt_id,
                    "model_used": model_used, "session": sid,
                    "hops": len(hops), "instruction": delivered}
        if action == "coord_qa_request":
            qa = await coordination_qa_request(
                work_id, plan_card_id, assignment_id=aid, rev=snap_rev,
                item_id=args.get("item_id"),
                target_card=str(args.get("target_card") or ""),
                acceptance=str(args.get("acceptance") or ""),
                evidence_refs=[str(x) for x in
                               (args.get("evidence_refs") or [])])
            if not qa.get("ok"):
                status = "unparsed"
                err_text = (f"第 {hop} 跳 QA 派驻被拒（{qa.get('code')}）："
                            f"{qa.get('why')}")
                break
            hops.append({"hop": hop, "action": "coord_qa_request",
                         "qa_id": qa["qa_id"],
                         "replayed": qa.get("replayed", False)})
            history.append({"role": "assistant",
                            "content": content})
            current_user = (f"QA 已派驻：{qa['qa_id']}（差事 {qa.get('qa_card')}）。"
                            "继续按协议输出下一个 JSON。")
            history.append({"role": "user", "content": current_user})
            continue
        if action == "coord_ask_function":
            # [下一批 ②] 指定职能的只读回合 + 答复回原事项
            ask = await coordination_ask(
                work_id, plan_card_id, assignment_id=aid, rev=snap_rev,
                item_id=args.get("item_id"),
                function_id=str(args.get("function_id") or ""),
                question=str(args.get("question") or ""),
                requirements_version=str(args.get("requirements_version") or ""),
                evidence_refs=[str(x) for x in
                               (args.get("evidence_refs") or [])],
                turn_id=turn_id, model_used=model_used)
            if not ask.get("ok"):
                err_text = (f"第 {hop} 跳问询未完成（{ask.get('code')}）："
                            f"{ask.get('why')}")
                if ask.get("code") in ("invalid_function", "self_ask",
                                       "no_charter", "bad_evidence",
                                       "stale_token"):
                    status = "unparsed"
                    break
                # 专家模型故障不影响统筹本轮继续
                history.append({"role": "assistant", "content": content})
                current_user = f"问询未完成：{err_text}\n继续按协议输出。"
                history.append({"role": "user", "content": current_user})
                hops.append({"hop": hop, "action": "coord_ask_function",
                             "code": ask.get("code")})
                continue
            hops.append({"hop": hop, "action": "coord_ask_function",
                         "question_id": ask["question_id"],
                         "function_id": ask["function_id"],
                         "replayed": ask.get("replayed", False)})
            # 答复随下一跳回到统筹上下文（回原事项由问题行+账本承载）
            history.append({"role": "assistant", "content": content})
            current_user = (f"职能 {ask['function_id']} 的答复"
                            f"（问题 {ask['question_id']}）：\n{ask['answer']}\n"
                            "答复已回写原事项。继续按协议输出下一个 JSON。")
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
            if action == "coord_read_card" and result_data.get("requirements"):
                board_requirement_reads[ref] = {
                    **reads[ref], "requirements": result_data["requirements"]}
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

    if "combo_result" in locals():
        return {"status": "parsed", "session": sid,
                "hops": len(hops), "combined": combo_result}

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
