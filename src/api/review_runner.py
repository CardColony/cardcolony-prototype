# -*- coding: utf-8 -*-
"""评审场执行器（[venue 重构 2026-10-31]）。

**评审卡＝场，不是机制。** 场拥有：会话（现场）、状态投影（卡面）、事件流、
回合锁。席位＝带着 mandate 进场的职能人格——每一次席位回合都是**普通卡回合**，
只是三向锚点分离：

  · 执行锚＝**评审卡**（会话/锁/事件流/停止都在它）；
  · 身份锚＝**席位卡**（act_as=fn_NNN：145区身份、工具面、模型、工具调用者、
    回复块 `from`）；
  · 上下文锚＝**被评子卡**（ctx_card：bundle/历史/记忆——用户令
    「评审席拿到的和子卡的上下文基本一致，只有身份职责区要替换」）。

**一次唤醒＝一步**（`_run_step`）：代码只读 `review_state` 事实、机械算出
下一个动作（叫一席／派活给正卡／停），做完发 `review_round_done` 就返回。
流程存续不归代码——harness 的 continue 策略按事件接力。

子卡改稿不再是评审侧代劳：派活＝给正卡发一条正经消息（落它自己的会话，
`from=review:<rid>` 带批次号），它在自己的普通回合里改稿、调
`submit_revision`／`promote_draft` 交回。它的页面、工具执行、会话同步
全部是它自己的回合自带的东西。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from ..storage import card_db, card_store, function_store, review_store, session_facade, session_ops, session_store
from . import event_stream

#: 评审职责书（每个角色**在评审席上**的职责；与日常职责 function_profiles.charter 分开）
REVIEW_CHARTERS = Path(__file__).resolve().parents[2] / "config" / "review_charters.json"

logger = logging.getLogger("api.review_runner")

#: 测试环境开关（照 ECO_NO_AUTO_DELIVER 先例）：置 1 则不起评审回合
ENV_NO_AUTO = "ECO_NO_AUTO_REVIEW"

#: 场授面里的**表态件**（席位能力上限之外、由场授予的那部分）。
#: 读件不在这里列——场授面＝「席位自己能力面 ∩ 只读件」∪ 这两件，能力上限
#: 由 build_tool_schemas(act_as) 自己收窄，场不再替它决定能读什么。
#: [两源模型 2026-11-01] write_ledger 移出授面——评审产出＝发言+票，
#: 不私记台账（卡面是唯一权威事实源）。
VENUE_GRANT: frozenset[str] = frozenset({"approve", "reject"})

#: 只读但**席位绝不需要**的件——从场授面剔除。
#: ``wait``：等下一席/等正卡交回是**场自己的事**（席位一个一个叫）；
#: 席位要等的东西只有它离席后才会发生，它等＝占住整场空转
#: （11-14 实案：fn_001 投完票原地 wait 烧 11 轮 20 分钟，等一个
#: 被它自己占住的空位）。
_SEAT_DENY: frozenset[str] = frozenset({"wait"})

_RUNNING: set[str] = set()
_STEP_RELEASED: dict[str, Future] = {}
_STEP_LOCK = Lock()


def _remote_review_owner(work_id: str, review_id: str) -> bool:
    from ..infra import procs

    return (procs.is_web() or
            (procs.is_runner() and procs.owner_index_of(work_id, review_id)
             != procs.runner_index()))


# ---------------------------------------------------------------- 一步＝一次唤醒
async def run_once(work_id: str, review_id: str, *,
                   hint: str = "",
                   expect: Optional[dict] = None) -> dict[str, Any]:
    """**跑一步**：机械读评审状态，做下一个动作（叫一席／派活／停）。

    ``hint``＝harness ``continue`` 送来的"为什么叫你"（一次性、不落历史），
    拼进这一步的 mandate 尾部。

    ``expect``＝台账登记的**步骤身份** ``{batch,draft,next}``（harness
    按策略 ``dedup_fields`` 解析值下发）——``_run_step`` 用**驱动本
    动作的同一份状态快照**复核：批次/稿次/下一步身份任一不符 ⇒
    ``stale_step`` 销账返回，**不执行**（批次A第三/四轮返工：rv_old
    的待投动作不得在 rv_new 批次里执行；身份检查与执行不再之间
    重读状态，换批窗口在快照内被机械裁掉）。跨 runner 转发时
    ``expect`` 随请求体到属主，不在边界丢。

    与旧 `run_round` 的差别：这里**没有循环**。每步结束落
    `review_round_done`（带 seat/voted/draft_ver 等事实），要不要续由
    策略看事件决定——"还有活"这个事实代码只如实上报。
    """
    if os.environ.get(ENV_NO_AUTO) == "1":
        return {"continued": False, "reason": "no_auto", "accepted": False}
    # [多进程 · 场卡属主分片 2026-11-08] 评审循环只能在**场卡属主**上跑：
    # 事件/journal/seq 按（进程×卡）分空间——循环在别处跑会把 review_* 事件
    # 写进**那个进程的**journal，与属主的回合事件两套 seq 撞车（文件里
    # seq 316 倒回 158 实证）；小窗/流订阅属主 ⇒ review_* 帧整组缺席，
    # 评审进度"全错"。与 _run_message 同一模子：web/非本片 runner ⇒
    # 透明 POST 属主 /ctl/review_step；属主收到后此处判据恒通过、不循环。
    try:
        from ..infra import procs as _procs
        if _remote_review_owner(work_id, review_id):
            return await _procs.forward_review_step(
                work_id, review_id, {"work_id": work_id,
                                     "review_id": review_id, "hint": hint,
                                     "expect": expect})
    except Exception as exc:  # noqa: BLE001 属主未知时不能在本片另起一步
        return {"continued": False, "accepted": False,
                "reason": "runner_unreachable", "error": str(exc)}
    blocked = _review_block_reason(work_id, review_id)
    if blocked:
        return {"continued": False, "accepted": False, "reason": blocked}
    key = f"{work_id}/{review_id}"
    with _STEP_LOCK:
        if key in _RUNNING:
            return {"continued": False, "reason": "already_running",
                    "accepted": False}
        _RUNNING.add(key)
    try:
        # [步骤身份复核 2026-11-20 · 批次A第四轮返工] 复核下沉进
        # ``_run_step``——与**驱动本动作的同一份快照**比对；入口预检
        # 会先读一套状态、执行再读一套，两次读之间换批就漏（TOCTOU）。
        r = await _run_step(work_id, review_id, hint=hint, expect=expect)
    except Exception as exc:  # noqa: BLE001 步内异常＝一步的事实，如实报不续
        err = f"{type(exc).__name__}: {exc}"
        logger.warning("[review] 步内异常 review=%s: %s", review_id, err)
        r = {"continued": False, "reason": "step_error", "error": err,
             "accepted": False,
             "_done": {"reason": "step_error", "continued": False,
                       "error": err}}
    finally:
        with _STEP_LOCK:
            _RUNNING.discard(key)
            released = _STEP_RELEASED.pop(key, None)
        if released is not None:
            released.set_result(None)
    # [受理显式回执 2026-11-20 · 批次A返工4] ``accepted`` 由评审入口说了算：
    # continue 投递层不再按 reason 猜受理——入口闸/步崩/投递失败 ⇒ False，
    # 台账记 failed 等 resume；到站/策略性唤不醒/已送达都是确定结局 ⇒ True。
    if isinstance(r, dict) and "accepted" not in r:
        r["accepted"] = _step_accepted(r)
    # **先放锁再报事实**——round_done 点火永远找不到占着的 _RUNNING。
    if isinstance(r, dict) and isinstance(r.get("_done"), dict):
        await _report(work_id, review_id, r.pop("_done"))
    return r


#: 「拒收」＝请求没被真正处理、重试可能改观的结局——只有这几种记
#: ``accepted=False``（台账 failed ⇒ resume 重投）：步内崩、派活/补唤醒
#: 投递真失败、对齐批成员缺。到站各 stop_reason、策略性唤不醒
#: （awaiting_halted/silenced/busy——信已在正卡会话，解停自查自接）、
#: seat_done/dispatch_*/wake_delivered/delivered_quiet/align_* 都是
#: 「评审逻辑跑完到确定结局」⇒ 受理；重试只会对同一状态空烧。
_STEP_REFUSED = frozenset({
    "step_error", "child_busy", "wake_failed", "advance_no_members",
    "stale_step"})


def _step_accepted(r: dict[str, Any]) -> bool:
    """``run_once`` 结果的受理判定——reason 不在拒收集即受理。"""
    return str((r or {}).get("reason") or "") not in _STEP_REFUSED


def _expect_diff(expect: Any, cur: dict[str, str]) -> dict[str, Any]:
    """``expect`` 登记的步骤身份 vs ``cur`` 当前身份的差异字段——
    空 dict＝一致（或无 expect 可核）。"""
    if not isinstance(expect, dict) or not expect:
        return {}
    exp = {str(k): str(v) for k, v in expect.items()}
    return {k: {"expect": exp.get(k), "current": cur.get(k)}
            for k in ("batch", "draft", "draft_fp", "next")
            if k in exp
            and str(cur.get(k) or "") != str(exp.get(k) or "")}


def _stale_step_result(diff: dict[str, Any], *,
                       expect: Any, cur: dict[str, str]) -> dict[str, Any]:
    """步骤过期销账结果——``stale`` 让投递层出 ``continue:stale``，
    ``accepted=False`` 兜底；``_done`` 不设 ⇒ 不报 review_round_done
    （这一步根本没跑）。"""
    logger.info("[review] 步骤过期销账 diff=%s", diff)
    return {"continued": False, "reason": "stale_step", "stale": True,
            "accepted": False,
            "expect": {str(k): str(v) for k, v in expect.items()}
            if isinstance(expect, dict) else expect,
            "current": cur, "diff": diff}


async def _run_step(work_id: str, review_id: str, *,
                    hint: str = "",
                    expect: Optional[dict] = None) -> dict[str, Any]:
    """`run_once` 的实体：**只算，不循环**。

    ``expect``＝台账登记的步骤身份——在**本函数读入的同一份状态
    快照**上核对（批次/稿次/``_classify`` 出的下一步），不符 ⇒
    ``stale_step`` 销账返回，动作一个不做。

    决策树（全部机械判据，无一是模型选择）：

      批次没了           → 到站
      全过 ∧ 有待裁增量  → 叫 **stale 批准席**跑「只评增量」确认轮（drift 口径）
      全过 ∧ 已请过定稿  → 等正卡调 promote_draft（到站，不重复派）
      全过               → 派活：请正卡调 promote_draft
      ────────────────────────────────────────────────
      成员未落座         → 叫一席发言（speak）
      落了座没表态       → 叫一席补投（vote_nudge）
      否过 ∧ 有新回应    → 叫一席重评（re_eval）
      否过 ∧ 已请回应    → 等正卡交回（到站）
      否过               → 派活：请正卡回应（revise）
    """
    st = await review_store.state_of(work_id, review_id)
    batch = str(st.get("batch") or "")
    st_now = await review_store.status(work_id, review_id)
    draft = str(int(st.get("draft") or 1))
    if not batch:
        # [步骤身份复核·快照绑定] 批次已不在 ⇒ next 必空；expect 的
        # 批次非空 ⇒ 登记步骤过期 ⇒ stale_step 销账（不是 no_batch
        # 受理——那笔账我们并没有替它干成）。
        diff = _expect_diff(expect, {"batch": "", "draft": draft,
                                     "next": ""})
        if diff:
            return _stale_step_result(diff, expect=expect,
                                      cur={"batch": "", "draft": draft,
                                           "next": ""})
        return {"continued": False, "reason": "no_batch", "started": False}
    spoken = set(review_store.spoken_at_draft(st))
    target = str(st.get("target") or "")
    facts = await _target_facts(work_id, target)
    identity = {"batch": batch, "draft": draft, "draft_fp": facts.get("draft_fp", "")}
    # 锁内先对账批次与材料，再解释点名席当前为何没有动作。
    state_expect = {k: v for k, v in expect.items() if k != "next"} \
        if isinstance(expect, dict) else {}
    if st_now.get("batch") != batch or str(st_now.get("draft") or 1) != draft:
        return _stale_step_result({"state": "changed_during_read"},
                                  expect=expect, cur=identity)
    if state_expect.get("draft_fp") and not identity["draft_fp"]:
        return {"continued": False, "reason": "material_unverified", "accepted": False}
    diff = _expect_diff(state_expect, identity)
    if diff:
        return _stale_step_result(diff, expect=expect, cur=identity)
    if (state_expect and identity["draft_fp"] and st.get("draft_hash")
            and identity["draft_fp"] != st["draft_hash"]):
        return {"continued": False, "reason": "awaiting_draft_sync", "accepted": False}
    promoted = bool(st_now.get("all_passed")) and facts["promoted"]
    tail_seq = _tail_seq(work_id, target)

    # [harness 驱动 · 2026-11-01] 挑谁/挑什么动作是评审卡内部的事（成员序）；
    # harness 只判"该不该点火"（runnable）。分类与 has_next 共用同一个
    # `_classify`——事件里上报的"还有活"和这里挑的"下一个活"同源。
    action, stop_reason = _classify(st, st_now, spoken, promoted=promoted,
                                    tail_seq=tail_seq,
                                    has_draft=facts["has_draft"])

    # [点名席位 2026-11-25 · 任务书] expect 里带 ``pin={"seat","kind"}``
    # ＝被评卡点名续跑某一席：不借自动挑席的成员序，改按**该席自己的**
    # 阶段判据（``_seat_step``）重算这一步——登记时是什么步、执行时
    # 该席还是不是这个步，照常在快照内核对。该席已无合法步骤（票已落/
    # 等正卡回应/补投已请未回/非成员）⇒ 如实收，不拿别的席顶账。
    pin = expect.get("pin") if isinstance(expect, dict) else None
    if isinstance(pin, dict) and str(pin.get("seat") or ""):
        p_seat = str(pin.get("seat") or "")
        action, stop_reason = _seat_step(
            p_seat, st, st_now, spoken,
            retry_missing_vote=bool(pin.get("retry_missing_vote")))

    # [步骤身份复核·快照绑定 2026-11-20 · 批次A第四轮返工] expect 与
    # **驱动本动作的同一份快照**比对——身份检查与实际执行之间没有
    # 第二次状态读，登记后换批在快照内被机械裁掉。
    identity["next"] = _next_tag(action)
    diff = _expect_diff(expect, identity)
    if diff:
        return _stale_step_result(diff, expect=expect, cur=identity)

    blocked = _review_block_reason(work_id, review_id)
    if blocked:
        return {"continued": False, "accepted": False, "reason": blocked}

    if action is None:
        return {"continued": False, "reason": stop_reason, "started": False,
                "_done": {"reason": stop_reason, "continued": False}}

    if action[0] == "advance":
        return await _advance_from_align(work_id, review_id, st)
    if action[0] == "veto":
        return await _terminate_align(work_id, review_id, st)

    if action[0] == "seat":
        _, seat, kind, *_wm = action   # 第 4 元＝触发水位，只进身份串
        seat_state = {**st, "_expected_draft_fp": state_expect.get("draft_fp")}
        if kind == "vote_nudge":
            seat_state["_vote_nudge_basis"] = _vote_nudge_basis(st, st_now)
        r = await _seat_turn(work_id, review_id, seat, seat_state,
                             kind=kind, hint=hint)
        if r.get("deferred"):
            # [批三·材料绑定] 原文指纹与已登记稿次对不上 ⇒ 这一步暂缓：
            # 不续跑（同一状态重跑同一份对不上的材料），等新事件
            # （子卡回合收口 → sync_draft → review 事件）自然接回。
            return {"continued": False, "accepted": False,
                    "reason": str(r.get("reason") or "awaiting_draft_sync"),
                    "seat": seat, "kind": kind,
                    "_done": r.get("_done") or {
                        "reason": "awaiting_draft_sync",
                        "seat": seat, "continued": False}}
        return _after_seat(work_id, review_id, batch, seat, r, st_now)

    if action[0] == "wake":
        # 纯唤醒：信已在正卡会话里，只再起一轮。正卡在跑＝信已"送达"
        # （它收口自查会接上），不算失败；空闲才叫 deliver_now。
        # ``why`` 是 classify 的诊断：唤醒文按状况点破（"信没读到" vs
        # "你上一轮读过了但没调"）——机械模板，不做内容判断。
        _, kind, why = action
        target = str(st.get("target") or "")
        ok = False
        # 停/缄默/在跑＝策略性唤不醒：信已在正卡会话，它解停/解冻/收口
        # 自查自然读到——与「送达」同待遇，不算失败也不烧 nudge 预算。
        refused = _wake_refusal(work_id, target)
        if refused:
            ok = True
        else:
            try:
                from . import message_runner as _mr
                ok = await _mr.deliver_now(
                    work_id, target,
                    text=_wake_kickoff(kind, why, st),
                    source={"kind": "review_dispatch", "from": review_id,
                            "batch": batch, "draft": draft,
                            "step": f"wake_{why}"})
            except Exception as exc:  # noqa: BLE001 唤醒异常＝一次没落
                logger.warning("[review] 补唤醒异常 kind=%s review=%s: %s",
                               kind, review_id, exc)
                ok = False
        if ok:
            _exp = {"batch": batch, "draft": draft}
            try:
                await review_store.clear_dispatch_fails(
                    work_id, review_id, expect=_exp)
            except Exception:  # noqa: BLE001
                pass
            if not refused:
                # 唤醒发出去＝记一次 nudge 并把 ask 锚重锚到此刻会话尾：
                # 下一次补唤醒只在「这之后又起过回合」时才发（一次唤醒对应
                # 一段新活动；anchor 不重锚会把同一段静默重复炸）。
                try:
                    await review_store.mark_wake_nudge(
                        work_id, review_id, kind,
                        at_seq=_tail_seq(work_id, target),
                        expect=_exp)
                except Exception:  # noqa: BLE001
                    pass
            reason = f"awaiting_{refused}" if refused else "wake_delivered"
            return {"continued": False, "reason": reason,
                    "_done": {"reason": reason, "continued": False}}
        try:
            await review_store.mark_dispatch_failed(
                work_id, review_id,
                expect={"batch": batch, "draft": draft})
        except Exception:  # noqa: BLE001
            pass
        return {"continued": False, "reason": "wake_failed",
                "error": "dispatch_failed",
                "_done": {"reason": "wake_failed", "continued": False,
                          "error": "dispatch_failed"}}

    # action[0] == "dispatch"
    _, kind, *_ = action
    target = str(st.get("target") or "")
    try:
        ok = await _dispatch_child(work_id, review_id, st, kind=kind)
    except Exception as exc:  # noqa: BLE001 派活异常＝一次没落，照计数闸走
        logger.warning("[review] 派活异常 kind=%s review=%s: %s",
                       kind, review_id, exc)
        ok = False
    if ok is True:
        return {"continued": False, "reason": f"dispatch_{kind}",
                "_done": {"reason": f"dispatch_{kind}", "continued": False}}
    if ok == "stale":
        # [写回核对 2026-11-20 · 批次A第五轮返工] 信已落会话但写回被
        # CAS 拒——快照身份与现状态已分叉（append 期间换批）。按过期
        # 销账返回，不谎报成送达（新批次自己的 classify 会重派）。
        cur_st = await review_store.state_of(work_id, review_id)
        return _stale_step_result(
            {"writeback": "identity_changed"},
            expect={"batch": batch, "draft": draft},
            cur={"batch": str(cur_st.get("batch") or ""),
                 "draft": str(int(cur_st.get("draft") or 1)),
                 "next": ""})
    # 没落分两种，判据＝**ask 记没记上账**（append+mark 在唤醒之前，
    # mark 动了 ⇒ 信已落正卡会话）：
    #   · 信落了但撞停/缄默/在跑 ⇒ 已送达未唤醒——它解停/收口自查自然
    #     读到，不算失败（[停卡不算失败 2026-11-15] 见 _wake_refusal）；
    #   · 信没进会话（append 没落／连信带唤醒整段异常）⇒ 真失败计数。
    try:
        st2 = await review_store.state_of(work_id, review_id)
        if kind == "promote":
            appended = (bool(st2.get("promote_asked"))
                        and not st.get("promote_asked"))
        else:
            appended = st2.get("asked_at_seq") != st.get("asked_at_seq")
    except Exception:  # noqa: BLE001 状态读不出按没落处理
        appended = False
    if appended:
        refused = _wake_refusal(work_id, target)
        if refused:
            return {"continued": False, "reason": "delivered_quiet",
                    "why": refused,
                    "_done": {"reason": "delivered_quiet", "why": refused,
                              "continued": False}}
    # 真没落（含信落了但空闲卡唤醒信道失败——后者仍计数，``unread``
    # 补唤醒链靠 ``dispatch_fails>0`` 驱动重试）。
    try:
        await review_store.mark_dispatch_failed(
            work_id, review_id,
            expect={"batch": batch, "draft": draft})
    except Exception:  # noqa: BLE001 计数失败不阻断
        pass
    return {"continued": False, "reason": "child_busy",
            "error": "dispatch_failed",
            "_done": {"reason": "child_busy", "continued": False,
                      "error": "dispatch_failed"}}


def _after_seat(work_id: str, review_id: str, batch: str, seat: str,
                r: dict[str, Any], st_before: dict[str, Any]) -> dict[str, Any]:
    """席位回合跑完的收尾：读回事实、发事件。**有没有活靠状态读，不靠猜**。

    回合出错（模型挂/锁冲突）⇒ `continued=False`：这一步的 round_done 已带
    error 落事实，再续只是空烧——等新事件（用户/回应/开批）再接。
    """
    failed = bool(r.get("error"))
    out = {"continued": not failed,
           "reason": (str(r.get("reason") or "seat_failed") if failed else "seat_done"),
           "accepted": bool(r.get("started", not failed)),
           "started": bool(r.get("started", not failed)),
           "seat": seat, "kind": r.get("kind"), "voted": bool(r.get("voted")),
           "vote": r.get("vote"), "vote_draft": r.get("vote_draft"),
           "error": str(r.get("error") or "")}
    if r.get("stale"):
        out["stale"] = True
    if r.get("_done") is not None:      # _seat_turn 的 round_done 载荷透传
        out["_done"] = r["_done"]
    return out


#: 派活连续失败封顶：超过就不再自动重试（持久 IO 故障不该烧循环），
#: 等新事件（发言/投票/回应/开批）自然再接。
_DISPATCH_FAIL_CAP = 6

#: 「ask 被消费但没成果」的补唤醒封顶：每次唤醒都烧正卡一个真回合，
#: 几次都叫不动就到站等外部事件（用户/开批/resume），不无限空烧。
_WAKE_NUDGE_CAP = 3


def _vote_nudge_basis(st: dict[str, Any], status: dict[str, Any]) -> list:
    votes = status.get("votes") or {}
    return [
        int(status.get("draft") or st.get("draft") or 1),
        int(st.get("responded_seq") or 0),
        [[role, vote.get("verdict"), vote.get("draft_ver")]
         for role, vote in sorted(votes.items())],
    ]


def _classify(st: dict[str, Any], st_now: dict[str, Any],
              spoken: set, *, promoted: bool = False,
              tail_seq: int | None = None,
              has_draft: bool = True) -> tuple[tuple | None, str]:
    """**挑下一个动作**——纯函数、全机械判据，成员序就是顺序。

    返回 ``(action, stop_reason)``：
      action = ("seat", seat, kind) | ("dispatch", kind) | None
      None ⇒ 到站原因（promoted/awaiting_promote/awaiting_target/stalled/
      dispatch_capped）。

    与 `_report` 的 ``has_next`` 同源——"还有活"不是推断，是这个函数
    会不会返回一个动作；至于现在能不能跑，`_report` 在 ``runnable``
    里另判（步结局等外部条件不算）。
    """
    members = [str(x) for x in (st_now.get("members") or [])]
    votes = dict(st_now.get("votes") or {})
    seats = dict(st.get("seats") or {})

    # [两轮评审 2026-11-14] 对齐批＝生死闸：总裁一席，通过直接晋级第二轮
    # 三席批、否决直接终局（问题退回正卡，不走修订循环）。它的票＝全场。
    # 拦截整个对齐型：关批后 status 的 all_passed 是「收口过」不是「通过」，
    # 掉给正常流程会被当成全员通过去派 promote——否决终局态必须静默。
    if str(st.get("type") or "") == "requirements_align":
        if not str(st_now.get("batch") or ""):
            return None, ""
        if st_now.get("all_passed"):
            return ("advance",), ""
        if any(str((votes.get(m) or {}).get("verdict") or "") == "reject"
               for m in members):
            return ("veto",), ""
    responded = int(st.get("responded_seq") or 0)
    asked = int(st.get("asked_seq") or 0)
    dispatch_fails = int(st.get("dispatch_fails") or 0)
    wake_nudges = int(st.get("wake_nudges") or 0)

    def _dispatchable() -> bool:
        return dispatch_fails < _DISPATCH_FAIL_CAP

    def _consumed(anchor: Any) -> bool:
        """ask 已被回合**消费**＝正卡会话在指令落点（anchor）之后还长了新块
        ⇒ 它起过回合读到了指令，却没交回结果。``tail_seq`` 读不出（V1/无
        会话）时按未消费处理——宁等勿炸。"""
        if tail_seq is None:
            return False
        a = -1 if anchor is None else int(anchor)
        return tail_seq > a

    if st_now.get("all_passed"):
        # ---- 收口区 ----
        # [drift 收窄口径 2026-10-31] 只在收口瞬间判：promote_draft 拒收时已把
        # drift_pending 挂上；stale 席（批过旧版的）补「只评增量」确认轮。
        # pending 席看的是新稿，不算 stale、不在此打扰。
        drift = dict(st.get("drift_pending") or {})
        stale = [m for m in (st_now.get("stale") or [])]
        if drift and stale:
            # [触发水位 2026-11-20 · 批次A第六轮返工] 确认轮的触发
            # 依据＝drift 账——同一席的第二次确认靠它分清。
            return ("seat", stale[0], "confirm",
                    _step_wm({"drift": drift, "stale": stale})), ""
        if promoted:
            return None, "promoted"
        if (str(st.get("type") or "requirements") == "requirements"
                and not has_draft):
            # [草稿已空判停 2026-11-07 · card_061←rv_3ee3fe5776 死后重放实案]
            # 草稿是 promote_draft 的唯一原料——空了还派「请调」，每一轮都
            # 只会吃「草稿为空」拒收；而正卡每次交回（submit_revision）又把
            # promote_asked 清零 ⇒ 下一扫描重派 ⇒ 永动重放十几轮。判停不藏
            # 故障：stop_reason 如实上报；草稿一旦被重新写入，has_draft 变真，
            # 派活自然恢复（下一修订批照常走）。
            return None, "draft_empty"
        if st.get("promote_asked"):
            # 先判更精确的诊断：ask 被回合消费掉但没转正 ⇒ consumed；
            # 都没消费过才看唤醒落没落（unread）。why 随行注入唤醒文。
            if _consumed(st.get("promote_asked_seq")):
                if wake_nudges < _WAKE_NUDGE_CAP:
                    return ("wake", "promote", "consumed"), ""
            elif 0 < dispatch_fails < _DISPATCH_FAIL_CAP:
                return ("wake", "promote", "unread"), ""
            return None, "awaiting_promote"
        if _dispatchable():
            return ("dispatch", "promote"), ""
        return None, "dispatch_capped"

    # ---- 迭代区：按成员序挑下一席 ----
    blocked = False
    awaiting_vote = False
    nudge_basis = _vote_nudge_basis(st, st_now)
    for m in members:
        verdict = str((votes.get(m) or {}).get("verdict") or "")
        if verdict == "approve":
            continue                    # 通过的席位不再被叫（drift 只在收口区）
        if not verdict:
            if m not in spoken:
                return ("seat", m, "speak"), ""
            if (seats.get(m) or {}).get("vote_nudge_basis") != nudge_basis:
                # [触发水位 2026-11-20 · 批次A第六轮返工] 补投的触发
                # 依据＝nudge_basis（票态/回应水位）——同席二次补投
                # 靠它分清，否则去重键相同第二笔被吞。
                return ("seat", m, "vote_nudge",
                        _step_wm(nudge_basis)), ""
            awaiting_vote = True
        if verdict == "reject":
            seen = int((seats.get(m) or {}).get("response_seen") or 0)
            if responded > seen:
                # [触发水位 2026-11-20 · 批次A第六轮返工] 重评的触发
                # 依据＝**回应水位**——responded=1 与 responded=2 的两次
                # 有效回应要各续评一次，水位进身份才分得开。
                return ("seat", m, "re_eval", responded), ""
            blocked = True              # 否过、正卡还没新回应 ⇒ 等/派活
    if blocked:
        if asked > responded:
            # [ask 过期 2026-11-02] 挂账 ask 覆盖到哪一版是**业务事实**：
            # 派时记的 asked_draft＝它携带的否决属于哪一版。有 draft_ver
            # 更新的否决 ⇒ 这张 ask 的业务已过期——该**重派**（发言原样重携
            # 带，新否决这次送到正卡），不是 nudge 一张旧 ask。
            # 缺失 asked_draft 的旧账按 0 计 ⇒ 有否决即重派一次落锚后自清。
            asked_draft = int(st.get("asked_draft") or 0)
            for m in members:
                v = votes.get(m) or {}
                if (str(v.get("verdict") or "") == "reject"
                        and int(v.get("draft_ver") or 0) > asked_draft):
                    if _dispatchable():
                        return ("dispatch", "revise", responded + 1), ""
                    return None, "dispatch_capped"
            # 已请过正卡回应还没交回：先判消费（读了没交回）再看唤醒落没落。
            if _consumed(st.get("asked_at_seq")):
                if wake_nudges < _WAKE_NUDGE_CAP:
                    return ("wake", "revise", "consumed"), ""
            elif 0 < dispatch_fails < _DISPATCH_FAIL_CAP:
                return ("wake", "revise", "unread"), ""
            return None, "awaiting_target"
        if _dispatchable():
            # 与 mark_asked 的应收回应水位一致，同稿仍可有多次修订请求。
            return ("dispatch", "revise", responded + 1), ""
        return None, "dispatch_capped"
    # 兜底：pending 里没人可做（理论上到不了——pending 必属上述三类之一）。
    return None, "awaiting_vote" if awaiting_vote else "stalled"


def _seat_step(seat: str, st: dict[str, Any], st_now: dict[str, Any],
               spoken: set, *,
               retry_missing_vote: bool = False) -> tuple[tuple | None, str]:
    """**点名席位**的实际步骤——``_classify`` 同组判据、只算这一席。

    与自动挑席的差别只有一个：点名席**不借成员序**——resume_review
    的 ``seat`` 参数走到哪一席，就只算哪一席的合法动作（用户任务书：
    「定点可以跳过默认的席位挑选顺序，仍须满足该席的阶段条件」）。

    返回 ``(action, "")``（与 _classify 同形，含触发水位）或
    ``(None, reason)``——reason 是该席当前没有合法步骤的事实：
    seat_not_member / seat_voted / awaiting_vote（补投已请未回）/
    awaiting_target_response（否决后待正卡新回应）/ seat_settled
    （收口区该席已无需确认）/ stalled。
    """
    members = [str(x) for x in (st_now.get("members") or [])]
    if seat not in members:
        return None, "seat_not_member"
    votes = dict(st_now.get("votes") or {})
    seats = dict(st.get("seats") or {})
    verdict = str((votes.get(seat) or {}).get("verdict") or "")
    if str(st.get("type") or "") == "requirements_align" and verdict:
        return None, "seat_voted"
    if st_now.get("all_passed"):
        # 收口区：只有 stale 席（批过旧稿）有合法动作＝增量确认；
        # 已按现稿批的席位不再有步骤——它的票就是答案。
        drift = dict(st.get("drift_pending") or {})
        stale = [m for m in (st_now.get("stale") or [])]
        if drift and seat in stale:
            return ("seat", seat, "confirm",
                    _step_wm({"drift": drift, "stale": stale})), ""
        return None, "seat_voted" if verdict else "seat_settled"
    if verdict == "approve":
        return None, "seat_voted"          # 已有当前有效票——票本身是答案
    if not verdict:
        if seat not in spoken:
            return ("seat", seat, "speak"), ""
        nudge_basis = _vote_nudge_basis(st, st_now)
        if (seats.get(seat) or {}).get("vote_nudge_basis") != nudge_basis:
            return ("seat", seat, "vote_nudge",
                    _step_wm(nudge_basis)), ""
        if retry_missing_vote:
            finished = int((seats.get(seat) or {}).get("vote_nudge_finished") or 0)
            return ("seat", seat, "vote_nudge",
                    f"{_step_wm(nudge_basis)}:retry:{finished}"), ""
        return None, "awaiting_vote"       # 补投已请、尚未表态
    if verdict == "reject":
        responded = int(st.get("responded_seq") or 0)
        seen = int((seats.get(seat) or {}).get("response_seen") or 0)
        if responded > seen:
            return ("seat", seat, "re_eval", responded), ""
        return None, "awaiting_target_response"   # 否过、正卡还没新回应
    return None, "stalled"


async def _target_facts(work_id: str, target: str) -> dict[str, Any]:
    """收口区要的两个正卡事实（一次读卡）：

    - ``promoted``：正稿写了没有（``requirements_state == "approved"``）；
    - ``has_draft``：草稿还在不在——它是 ``promote_draft`` 的唯一原料。
      读不出按「有草稿」处理（保持旧判据偏向：宁派勿停）。
    """
    try:
        card = await card_store.read_card(work_id, target)
        meta = card.get("meta") or {}
        raw = str(meta.get("requirements_draft") or "")
        return {
            "promoted": str(meta.get("requirements_state") or "") == "approved",
            "has_draft": bool(raw.strip()),
            "draft_fp": hashlib.sha1(raw.encode("utf-8")).hexdigest() if meta else "",
        }
    except Exception:  # noqa: BLE001
        return {"promoted": False, "has_draft": True}


def _review_block_reason(work_id: str, review_id: str) -> str:
    from . import control
    from ..storage import governance_store

    if control.is_halted(work_id, review_id) is not None:
        return "stopped"
    if governance_store.is_silenced(work_id, review_id):
        return "silenced"
    return ""


def _tail_seq(work_id: str, target: str) -> int | None:
    """正卡会话的末块 seq——「ask 被消费」的判据锚（指令之后还长新块
    ⇒ 它起过回合读到指令）。读不出（无会话/V1）返回 None＝宁等勿炸。"""
    if not target:
        return None
    try:
        seq = session_facade.scan_tail(work_id, target).get("seq")
        return int(seq) if seq is not None else None
    except Exception:  # noqa: BLE001
        return None


def _wake_refusal(work_id: str, target: str) -> str:
    """正卡此刻**唤不醒的策略原因**：``"halted"``（用户停）／``"silenced"``
    （治理缄默）／``"busy"``（在跑）。这些都不是派活失败——信已在正卡
    会话里（append 先于唤醒），它解停/解冻/收口自查自然读到。

    [停卡不算失败 2026-11-15 · work_073 实案] card_007 被用户停 16 秒
    （12:17:06–22）正好罩住首次派活：信落库成功但 deliver_now 被 halt
    闸拒 False ⇒ 当失败计；随后 5 次补唤醒全撞停 ⇒ 6 连败封顶
    dispatch_capped，此后三轮否决（草稿 v4/v6/v8）一条都没再派——
    一次瞬时停卡永久杀死了该批的派送通道。读不出按 ""（可唤醒方向）。
    """
    if not target:
        return ""
    try:
        from . import control as _ctl
        if _ctl.is_halted(work_id, target) is not None:
            return "halted"
    except Exception:  # noqa: BLE001 停表读不出按未停
        pass
    try:
        from ..storage import governance_store as _gov
        if _gov.is_silenced(work_id, target):
            return "silenced"
    except Exception:  # noqa: BLE001 治理表读不出不挡
        pass
    try:
        if session_facade.turn_holder(work_id, target):
            return "busy"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _wake_kickoff(kind: str, why: str, st: dict[str, Any]) -> str:
    """补唤醒的**起跑文**——按 classify 的诊断点破状况（不落会话、
    只做本轮激励文）：``consumed``＝「你上一轮读过了但没调」；
    ``unread``＝「有条指令还没被读到」。"""
    draft = int(st.get("draft") or 1)
    if why == "consumed":
        if kind == "promote":
            return ("〔评审场提醒〕你上一轮已经读过定稿指令，但回合结束时"
                    "没调 `promote_draft`——批次还挂在等你转正。本轮就这一件"
                    f"事：调 `promote_draft`（评审已全员通过，草稿第 {draft} 版）。")
        return ("〔评审场提醒〕你上一轮已经读过回应请求，但回合结束时没调"
                " `submit_revision`——评审还停在等你交回。本轮就这一件事："
                "把该改的改掉后调 `submit_revision` 交回本轮结论"
                "（决定不改也写理由）。")
    if kind == "promote":
        return ("〔评审场〕你有一条未处理的定稿指令（见会话里的批次指令）："
                f"评审已全员通过，本轮请调 `promote_draft`（草稿第 {draft} 版）。")
    return ("〔评审场〕你有一条未处理的回应指令（见会话里的批次指令）："
            "评审在等你的交回，请按它执行。")


# ------------------------------------------------------------ [批三] 材料快照 / 缩量装配
#: 评审按阶段缩量试点开关（成本总方案 §四·第三批）。缺省关闭＝现行完整装配逐字不变。
_ENV_PHASES = "REVIEW_CONTEXT_PHASES"

#: 试点只处理这两步：增量确认（confirm）与同版补票（vote_nudge）。
_PILOT_STEPS = frozenset({"confirm", "vote_nudge"})


def _phases_enabled() -> bool:
    return os.environ.get(_ENV_PHASES) == "1"


async def _draft_text_of(work_id: str, card_id: str) -> tuple[str, bool]:
    """被评卡草稿原文 → ``(原文, 可读)``：读失败与「真空稿」分开
    （指纹绑定只信真读到的原文）。"""
    try:
        meta = (await card_store.read_card(work_id, card_id)).get("meta") or {}
        return str(meta.get("requirements_draft") or ""), True
    except Exception:  # noqa: BLE001
        return "", False


async def _venue_blocks(work_id: str, review_id: str) -> list[dict]:
    """场内会话全部块——读失败按空表（缩量判据宁可回退完整装配）。"""
    try:
        blocks = await session_facade.read(work_id, review_id) or []
        return blocks if isinstance(blocks, list) else []
    except Exception:  # noqa: BLE001
        return []


def _speech_anchor(block: dict) -> dict[str, Any]:
    """发言块上的材料回读锚（finish 按**实际送给席位的版本**打下）。"""
    anch = block.get("review_anchor")
    return dict(anch) if isinstance(anch, dict) else {}


async def _material_snapshot(work_id: str, review_id: str, seat: str,
                             st: dict[str, Any], kind: str
                             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """本轮**材料快照**＋缩量判定（任务书 §二.1 / §三回退条件）。

    返回 ``(material, matctx)``：

    - ``material``＝**身份层**：评审卡/批次/席位/步骤/被评卡/稿次/原文
      指纹——随 source 进回合、随发言块落锚、随投票进写事务复核。
      ``restricted``＝本步判定可轻量装配；``reason``＝判定理由
      （缩量形态或回退原因）；``defer`` 非空＝原文指纹与已登记稿次
      对不上（``sync_draft`` 只在被评回合收口推版——版本推进在途），
      **这一步暂缓**，让现有稿次推进链先走。
    - ``matctx``＝**内容层**：现稿原文、票态、锚定发言等差事建造原料
      （体量大，不进 source）。
    """
    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")
    draft_ver = int(st.get("draft") or 1)
    material: dict[str, Any] = {
        "review_id": review_id, "batch": batch, "seat": seat, "step": kind,
        "target": target, "draft_ver": draft_ver,
        "restricted": False, "reason": "", "defer": "", "identical": False,
    }
    cur_draft, readable = await _draft_text_of(work_id, target)
    matctx: dict[str, Any] = {"draft": cur_draft, "readable": readable}
    # 原文指纹与 review_state.draft_hash 同口径（requirements_draft sha1
    # 逐字）；读不出 ⇒ 空指纹＝不可绑定（不拿 sha1("") 冒充真空稿）。
    material["draft_fp"] = (
        hashlib.sha1(cur_draft.encode("utf-8")).hexdigest() if readable else "")
    pilot = (_phases_enabled()
             and str(st.get("type") or "") == "requirements"
             and kind in _PILOT_STEPS)
    if not pilot:
        material["reason"] = ("switch_off" if not _phases_enabled()
                              else "non_pilot_step")
        return material, matctx
    # [§二.1] 原文指纹 ≠ 已登记稿次指纹 ⇒ 版本推进在途 ⇒ 暂缓（与「确认缺证」
    # 分账——这不是缺证，是稿还没推到它名下）。**草稿读不出**不算在途
    # （永久读不出会永久暂缓 ⇒ 评审卡死）：按缺证回退完整装配，mandate
    # 如实报「草稿为空」。
    if not readable:
        material["reason"] = "material_unreadable"
        return material, matctx
    if material["draft_fp"] != str(st.get("draft_hash") or ""):
        material["defer"] = "draft_sync_pending"
        material["reason"] = "draft_sync_pending"
        return material, matctx

    st_now = await review_store.status(work_id, review_id)
    blocks = await _venue_blocks(work_id, review_id)
    speeches = [b for b in blocks
                if str(b.get("actor") or "") == "assistant"
                and str(b.get("from") or "") == seat
                and str(b.get("content") or "").strip()]
    matctx.update({"status": st_now, "speeches": speeches})

    if kind == "confirm":
        vote = dict((st_now.get("votes") or {}).get(seat) or {})
        snap = str(vote.get("snapshot") or "")
        vote_ver = int(vote.get("draft_ver") or 0)
        if str(vote.get("verdict") or "") != "approve" or not snap:
            material["reason"] = "missing_snapshot"   # 旧票快照缺失/非通过票
            return material, matctx
        if vote_ver >= draft_ver:
            material["reason"] = "vote_not_stale"     # 无增量可确认
            return material, matctx
        # 原批准理由：锚必须同时绑到**被批准的那一次稿**（稿次号＋正文
        # 指纹双核对）——v1 与 v3 逐字相同时指纹对上不算数，锚的
        # draft_ver 必须等于被批准的 vote_ver；confirm 步自己的发言
        # 是「再次确认」不是当初批准理由，也排除。
        snap_fp = hashlib.sha1(snap.encode("utf-8")).hexdigest()
        appr = next(
            (b for b in reversed(speeches)
             if int(_speech_anchor(b).get("draft_ver") or 0) == vote_ver
             and str(_speech_anchor(b).get("draft_fp") or "") == snap_fp
             and str(_speech_anchor(b).get("batch") or "") == batch
             and str(_speech_anchor(b).get("step") or "") != "confirm"),
            None)
        matctx.update({"vote": vote, "vote_ver": vote_ver,
                       "snap": snap, "appr_speech": appr})
        # 「确认缺证」与「原文确实相同」分账——逐字相同才算正文一致。
        material["identical"] = bool(snap == cur_draft)
        material["restricted"] = True
        material["reason"] = ("confirm_identical" if material["identical"]
                              else "confirm_diff")
        return material, matctx

    # kind == "vote_nudge"：该席**同批同版**可核实意见＋当前票态＋原文坐标
    # [批三返修④] 同版＝**稿次号＋正文指纹**双核对——v1 意见在 v3 正文与
    # v1 逐字相同的场景下指纹能对上，但锚的 draft_ver 是 1 ≠ 3：不算同版。
    cur_fp = material["draft_fp"]
    opinion = next(
        (b for b in reversed(speeches)
         if str(_speech_anchor(b).get("batch") or "") == batch
         and int(_speech_anchor(b).get("draft_ver") or 0) == draft_ver
         and str(_speech_anchor(b).get("draft_fp") or "") == cur_fp
         and str(_speech_anchor(b).get("step") or "") != "vote_nudge"),
        None)
    if opinion is None:
        # 放宽到补票步的发言（上轮补票说的新立场也算意见）；仍无 ⇒ 缺证。
        opinion = next(
            (b for b in reversed(speeches)
             if str(_speech_anchor(b).get("batch") or "") == batch
             and int(_speech_anchor(b).get("draft_ver") or 0) == draft_ver
             and str(_speech_anchor(b).get("draft_fp") or "") == cur_fp),
            None)
    if opinion is None:
        # 只有「发过言」标志、锚缺失、或发言绑在别版/别批——都按缺证回退。
        material["reason"] = "no_anchored_speech"
        return material, matctx
    opinion_seq = int(opinion.get("seq") or 0)
    # 发言锚之后的场内用户裁定 ⇒ 意见可能过时 ⇒ 完整材料重理解
    if any(str(b.get("actor") or "") == "human"
           and int(b.get("seq") or 0) > opinion_seq for b in blocks):
        material["reason"] = "new_user_ruling"
        return material, matctx
    # 他席在**本稿**上的新否决（票时 ≥ 意见时）⇒ 新否决依据 ⇒ 完整
    op_ts = str(opinion.get("ts") or "")
    try:
        appr_rows = await card_db.run_read(work_id, lambda conn: [
            dict(r) for r in conn.execute(
                "SELECT role_id,draft_ver,revoked,ts,verdict FROM review_approvals"
                " WHERE work_id=? AND batch_id=?", (work_id, batch)).fetchall()],
            [])
    except Exception:  # noqa: BLE001
        appr_rows = []
    if any(str(r.get("role_id") or "") != seat
           and str(r.get("verdict") or "approve") == "reject"
           and not int(r.get("revoked") or 0)
           and int(r.get("draft_ver") or 0) == draft_ver
           and str(r.get("ts") or "") >= op_ts
           for r in appr_rows):
        material["reason"] = "new_reject_basis"
        return material, matctx
    # 意见 → 草稿片段机械定位：必要条款定不出 ⇒ 完整草稿（§三回退条件）
    from ..storage import predicates as _pred
    opinion_text = str(_pred.context_content(opinion) or "")
    if not opinion_text.strip():
        # 意见整条被隔离、无可供表态的残部——按缺证回退完整材料。
        material["reason"] = "opinion_quarantined"
        return material, matctx
    located = _locate_opinion_blocks(cur_draft, opinion_text)
    if not located:
        material["reason"] = "opinion_not_locatable"
        return material, matctx
    matctx.update({"opinion": opinion, "opinion_text": opinion_text,
                   "located": located})
    material["restricted"] = True
    material["reason"] = "nudge_same_ver"
    return material, matctx


def _locate_opinion_blocks(draft: str, opinion_text: str) -> list[tuple[str, int, int]]:
    """意见 → 草稿块坐标 `(块名, 首行, 末行)`（1 基行号，§三坐标口径）。

    机械判据（只有「发过言」不算证——要落到原文）：
    ① `### ` 块名在意见里逐字出现；② 意见里的引文逐条回验——
    「」/《》/“”（≥2 字）与 ASCII "..."（≥6 字）逐字落在某块行内。
    一条引文定不出 ⇒ 空表 ⇒ 上游回退完整草稿。
    """
    draft = str(draft or "")
    opinion_text = str(opinion_text or "")
    if not draft or not opinion_text:
        return []
    from . import card_tools as _ct          # 延迟 import：card_tools 反向引用
    blocks = _ct._req_block_ranges(draft)     # [(name, a, b)]
    if not blocks:
        return []
    found: list[tuple[str, int, int]] = []
    for name, a, b in blocks:
        if name and name != "（文首）" and name.strip() in opinion_text:
            found.append((name, a, b))
    lines = draft.split("\n")
    # [批三返修④] 完整性＝**每一条可定位依据都落得到原文**，不是「定到一条
    # 就算」：意见里的引文逐条回草稿找宿主行；定不齐 ⇒ 空表 ⇒ 回退完整草稿。
    # [二轮] 短引文一样是必要依据：「」/《》/“”是明确引用记号，≥2 字即逐条
    # 回验（「缺失项」这种三字引文查无原文 ⇒ 回退）；ASCII 双引号噪声面大，
    # 维持 ≥6 字门槛。
    _need = re.findall(r"[「“《]([^」”》\n]{2,})[」”》]", opinion_text)
    _need += re.findall(r"\"([^\"\n]{6,})\"", opinion_text)
    for quote in _need:
        hit = False
        for ln_no, ln in enumerate(lines, 1):
            if quote in ln:
                hit = True
                blk = next((bl for bl in blocks if bl[1] <= ln_no <= bl[2]), None)
                if blk is not None and blk not in found:
                    found.append(blk)
                break
        if not hit:
            return []
    # 点名条款（「X 一节/条/款/章」）同样须可定位——条款在草稿里查无此物
    # ⇒ 意见依据定不齐 ⇒ 回退。词干剥指示词（这/本/该/每），<2 字不算点名。
    for m in re.finditer(r"([一-鿿A-Za-z0-9_\-（）()]{2,20})\s*[节条款章部编]",
                         opinion_text):
        stem = m.group(1).lstrip("这本该上下前后各每全")
        if len(stem) < 2:
            continue
        blk = next((bl for bl in blocks
                    if stem and (stem in (bl[0] or "")
                                 or ((bl[0] or "") and bl[0] in stem))),
                   None)
        if blk is not None:
            if blk not in found:
                found.append(blk)
        elif stem not in draft:
            return []
    found.sort(key=lambda t: t[1])
    return found


async def _purge_turn_blocks(work_id: str, review_id: str, seat: str,
                             batch: str, pre_seq: int,
                             turn_pair: str = "", t0: str = "") -> None:
    """归档过期回合的块，再发布剩余的活跃会话。"""
    path = session_facade._session_path(work_id, review_id)
    async with session_store._session_lock(work_id, review_id):
        try:
            previous, fmt, blocks = await session_facade._read_existing_locked(
                work_id, review_id, path)
            if not blocks or fmt != 2:
                return
            header = (await asyncio.to_thread(session_facade._parse_v2, previous))["header"]

            def anchor_batch(block: dict[str, Any]) -> str:
                anchor = block.get("review_anchor")
                return str(anchor.get("batch") or "") if isinstance(anchor, dict) else ""

            file_replaced = max(int(b.get("seq") or 0) for b in blocks) <= pre_seq
            old_speech = [
                b for b in blocks
                if b.get("actor") == "assistant" and b.get("from") == seat
                and anchor_batch(b) == batch
                and (int(b.get("seq") or 0) > pre_seq or file_replaced
                     or (t0 and str(b.get("ts") or "") > t0))
            ]
            speech_seqs = {b.get("seq") for b in old_speech}
            turn_pairs = {str(b["pair_id"]) for b in old_speech if b.get("pair_id")}
            if turn_pair:
                turn_pairs.add(turn_pair)
            keep, dropped = [], []
            for b in blocks:
                paired = (anchor_batch(b) in ("", batch)
                          and str(b.get("pair_id") or "") in turn_pairs)
                belongs = b.get("actor") in ("assistant", "tool") and (
                    paired or b.get("seq") in speech_seqs)
                (dropped if belongs else keep).append(b)
            if not dropped:
                return

            def _update(conn: Any, archive_id: str) -> None:
                for b in dropped:
                    seq = b["seq"]
                    row = conn.execute(
                        "SELECT content, data FROM session_blocks WHERE card_id=? AND seq=?",
                        (review_id, seq)).fetchone()
                    if row:
                        data = json.loads(row["data"] or "{}")
                        if (str(row["content"] or "") != str(b.get("content") or "")
                                or str(data.get("pair_id") or "") != str(b.get("pair_id") or "")
                                or anchor_batch(data) != anchor_batch(b)):
                            raise RuntimeError(f"清场块与原文账不一致: {seq}")
                        conn.execute(
                            "INSERT INTO session_blocks (card_id,seq,type,time,content,data)"
                            " SELECT ?,seq,type,time,content,data FROM session_blocks"
                            " WHERE card_id=? AND seq=?", (archive_id, review_id, seq))
                        conn.execute("DELETE FROM session_blocks WHERE card_id=? AND seq=?",
                                     (review_id, seq))
                    conn.execute("DELETE FROM context_board WHERE card_id=? AND seq=?",
                                 (review_id, seq))

            archive = await session_facade.publish_review_session_locked(
                work_id, review_id, previous=previous,
                replacement=session_facade._render_v2_header(header) + "".join(
                    session_facade._render_v2_block(b) for b in keep),
                archive_id=f"{review_id}-purged-{batch}",
                archive_text=session_facade._render_v2_header(header) + "".join(
                    session_facade._render_v2_block(b) for b in dropped),
                update=_update)
        except Exception:
            logger.exception("评审旧回合清场失败 review=%s batch=%s", review_id, batch)
            return
        await session_facade.event_log.append(work_id, review_id, {
            "type": "review_purge",
            "data": {"batch": batch, "seat": seat,
                     "purged_seqs": [b["seq"] for b in dropped], "archive": archive}})


# ---------------------------------------------------------------- 席位回合
async def _seat_turn(work_id: str, review_id: str, seat: str,
                     st: dict[str, Any], *, kind: str,
                     hint: str = "") -> dict[str, Any]:
    """一席＝**一个普通卡回合**：act_as=席位、ctx_card=被评子卡、场授工具面。

    - `mark_spoken` 在回合**开始前**落：机械记"本轮被叫过"——给 `_run_step`
      的挑席判据用（说过还没投 → vote_nudge），投票本身不查它。
    - `mark_seen` 记它被叫时看到的回应水位——reject 席只在有新回应时才重叫。
    - mandate 不落会话（`append_user=False`）：它是差事，不是用户的话；
      席位的回复由 finish 带 `from=seat` 落进**场会话**（页面看得见谁在说）。
    """
    from . import routes  # 延迟 import：routes 体量大且反向引用

    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")
    # [409 标记序 2026-11-25 · 用户令「席位启动返回 409 时，发言标记和
    # 补票依据不推进」] mark_seen/mark_spoken 一律**回合真跑成才落**——
    # 被叫水位/补票依据/发言事实都是"这一席被叫到"的账，早退（锁冲突、
    # 材料过期、模型挂）没叫成，落账会把下一步错推到 vote_nudge 并吞掉
    # 重叫义务（实案 card_012：占锁 409 后席位被记"已发言"，场冻）。

    # [批三·材料绑定 2026-11-20] 本轮材料快照：席位实际将看到的稿次与
    # 原文指纹先定下来——随 source 进回合（finish 打发言锚、step 挂
    # 工具面投票复核、context_assembler 按 restricted 缩量装配）。
    # ``defer``＝原文指纹与已登记稿次对不上（sync_draft 只在被评回合
    # 收口推版）⇒ 这一步暂缓，等稿次推进链先走——不拿对不上账的
    # 材料开回合。
    material, matctx = await _material_snapshot(
        work_id, review_id, seat, st, kind)
    expected_fp = st.get("_expected_draft_fp")
    if expected_fp and material.get("draft_fp") != expected_fp:
        return {"seat": seat, "kind": kind, "error": "stale_material",
                "reason": "stale_material", "stale": True, "started": False}
    if material.get("defer"):
        logger.info("[review] 材料身份未定（%s）⇒ 暂缓席位回合 review=%s seat=%s kind=%s",
                    material["defer"], review_id, seat, kind)
        return {"seat": seat, "kind": kind, "voted": False, "vote": "",
                "error": "", "deferred": True,
                "reason": "awaiting_draft_sync",
                "_done": {"reason": "awaiting_draft_sync",
                          "seat": seat, "continued": False}}
    name = await _name_of(work_id, seat)
    try:
        event_stream.emit(work_id, review_id, "review_speaking", {
            "batch": batch, "role": seat, "kind": kind, "name": name})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass

    duty = _review_charter(seat)
    prof: dict[str, Any] = {}
    try:
        prof = function_store.get_profile(work_id, seat) or {}
    except Exception:  # noqa: BLE001
        prof = {}
    if not duty:
        # 职责书可能按**目录 code** 存（评审职责书按职能写，不按卡实例写）
        from . import function_room as _fr
        try:
            duty = _review_charter(_fr.catalog_code_of(prof))
        except Exception:  # noqa: BLE001
            duty = ""
    if not duty:
        ch = prof.get("charter") or {}
        duty = str(ch.get("text") if isinstance(ch, dict) else ch or "")
    # 145 区身份＝**评审职责书**（不是日常 charter——两个职责，用户令 2026-10-29）。
    act_meta = {"function_id": seat, "function_name": name,
                "charter": {"text": duty}}
    mandate = await _seat_mandate(work_id, review_id, seat, st, kind,
                                  material=material, matctx=matctx)
    if str(hint or "").strip():
        mandate = f"{mandate}\n\n【系统续接】{str(hint).strip()}"

    # [批三·一份材料只经一个入口] 受限步把场内过程**整段收窄到用户发言**：
    # 被引的原意见/原批准理由已带发言锚随差事下发，席位发言块再经
    # prior_extra 送一遍＝同一份材料双重计账；用户裁定不走差事、照旧全留。
    prior = await _prior_blocks(work_id, review_id)
    if material.get("restricted"):
        prior = [b for b in prior if str(b.get("role") or "") == "user"]

    # [批三返修①] 进场水位：本回合**之后**落在场会话上的块才可能属本回合
    # ——批次在回合中更换时按旧回合标识清场，新批落的记录一概不动。
    from . import card_tools as _ct
    try:
        _vb = await session_facade.read(work_id, review_id, project=("seq",))
        pre_seq = max((int(b.get("seq") or 0) for b in _vb), default=0)
    except Exception:  # noqa: BLE001
        pre_seq = 0
    t0 = session_store._now_iso()         # 本回合起步时刻（清场锚规则用）

    err = ""
    stale_why = ""
    turn_pair = ""
    try:
        result = await routes._run_message(
            work_id, review_id, mandate,
            append_user=False,
            source={"kind": "review_step", "seat": seat,
                    "batch": batch, "step": kind,
                    "material": material},
            act_as=seat, act_meta=act_meta, ctx_card=target,
            tool_allow=_seat_toolface(),
            prior_extra=prior)
        if isinstance(result, dict):
            turn_pair = str(result.get("pair_id") or "")
    except _ct.MaterialStale as exc:
        # [批三返修③] 材料在发送边界过期（首跳/续跳/重试任一边界）——
        # 旧材料零出站，按现有过期路径如实交回，下一步由调度决定。
        result = {}
        stale_why = str(exc) or "stale_material"
        turn_pair = str(getattr(exc, "pair_id", "") or "")
        err = "stale_material"
        logger.warning("[review] 材料在发送边界过期 review=%s seat=%s kind=%s why=%s",
                       review_id, seat, kind, stale_why)
    except Exception as exc:  # noqa: BLE001 模型挂/锁冲突也是一步的事实
        result = {}
        err = f"{type(exc).__name__}: {exc}"
        logger.warning("[review] 席位回合失败 seat=%s review=%s: %s",
                       seat, review_id, err)

    # [venue 批次守卫] 回合跑到一半批次换了（open_batch 在**同协程**里开的，
    # cancel_turn 停不掉自己）：这一轮回合写进场会话的每一块都是**旧批残块**
    # ——按**捕获的旧批次号**撤票（revoke 点名旧批，新批合法票不动）、按
    # **旧回合 pair_id＋水位**清残块（新批用户消息/记录全留，批三返修①）。
    st_now0 = await review_store.state_of(work_id, review_id)
    if stale_why or str(st_now0.get("batch") or "") != batch:
        if str(st_now0.get("batch") or "") != batch:
            logger.warning(
                "[review] 批次在回合中更换 review=%s seat=%s ⇒ 旧批残块清场",
                review_id, seat)
            try:
                await review_store.revoke(work_id, review_id, seat, batch=batch)
            except Exception:  # noqa: BLE001 撤不掉不阻断清场
                pass
            await _purge_turn_blocks(work_id, review_id, seat, batch,
                                     pre_seq, turn_pair, t0)
        return {"seat": seat, "kind": kind, "voted": False, "vote": "",
                "error": "stale_batch" if not stale_why else "stale_material",
                "reason": stale_why or "stale_batch",
                "started": False, "stale": True,
                "_done": {"reason": stale_why or "stale_batch",
                          "seat": seat, "continued": False}}

    # 回合结束读回事实：这一席表态了没有、投了什么（harness 续接的判据）。
    # **先判 err 再落标记**：非 dict（锁冲突 409 等早退）与 dict 内 error
    # 都是"没跑成"——mark_spoken/mark_seen 只在真跑成时落，否则发言事实、
    # 被叫水位、补票依据被假推进，下一步被错分类。
    st2 = await review_store.status(work_id, review_id)
    current_vote = (st2.get("votes") or {}).get(seat) or {}
    vote = str(current_vote.get("verdict") or "")
    vote_draft = current_vote.get("draft_ver")
    voted = False
    reason = ""
    if not err and not isinstance(result, dict):
        # 锁冲突等早退路径返回的是 JSONResponse——不是 dict ⇒ 回合根本没跑成。
        # 这是机械失败事实：不续跑（续了也是对着锁空烧），如实报。
        code = getattr(result, "status_code", None)
        err = f"turn_http_{code}" if code else "turn_non_dict_result"
        reason = "review_busy" if code == 409 else "seat_failed"
    if not err and isinstance(result, dict) and result.get("error"):
        # error 两种形态：{"code","message"} 字典（halted/转发错）或裸字符串
        # （旧 forward 返回）——只按 dict 取 .get("message") 会在字符串上崩。
        _e = result.get("error")
        reason = str(_e.get("code") or "seat_failed") if isinstance(_e, dict) else "seat_failed"
        err = str((_e.get("message") or reason) if isinstance(_e, dict) else _e)
    if isinstance(result, dict):
        voted = any(e.get("ok") and e.get("tool") in ("approve", "reject")
                    for e in (result.get("tool_events") or []))
        if not err and result.get("end_reason") in ("human_stop", "policy_stop"):
            reason = err = "stopped"
    if not err:
        # 发言与水位都按**起叫时刻**的快照（st）落——回合中稿次/回应涨了，
        # 这次发言仍属它被叫的那一轮，不冒充新轮发言。
        await review_store.mark_spoken(
            work_id, review_id, seat,
            draft=int(st.get("draft") or 1))
        await review_store.mark_seen(
            work_id, review_id, seat, batch=batch,
            vote_nudge_basis=((st.get("_vote_nudge_basis") or _vote_nudge_basis(st, st2))
                              if kind == "vote_nudge" else None),
            response_seen=int(st.get("responded_seq") or 0))
    try:
        event_stream.emit(work_id, review_id, "review_spoke", {
            "batch": batch, "role": seat, "name": name, "kind": kind,
            "voted": voted, "vote": vote,
            "draft": int(st2.get("draft") or 1), "error": err})
    except Exception:  # noqa: BLE001 事件失败不影响评审
        pass
    # round_done 不在此发：由 `run_once` 在 _RUNNING 释放后统一报——
    # 策略点火永远不会撞在这把锁上（撞了必丢唤醒，实跑踩过）。
    return {"seat": seat, "kind": kind, "voted": voted,
            "vote": vote, "vote_draft": vote_draft, "error": err,
            "reason": reason, "started": not err or voted,
            "_done": {"reason": f"seat:{kind}", "seat": seat,
                      "voted": voted, "continued": not err,
                      "error": err}}


def _seat_toolface() -> set[str]:
    """场授面＝**全部只读件** ∪ {approve, reject} − _SEAT_DENY。

    「席位自己的能力上限」已由 `build_tool_schemas(act_as)` 收窄——这个集合
    只是场的**许可上限**：在场内，读+表态；写产物/通信/建卡一律不在场。
    只读件按 `ToolDef.readonly` 现算，不写死名单（新注册的只读工具自动在场）。
    """
    from . import card_tools  # 延迟 import：card_tools 体量大且反向引用

    readonly = {n for n, d in card_tools.TOOL_REGISTRY.items()
                if getattr(d, "readonly", False)}
    return (set(readonly) - _SEAT_DENY) | set(VENUE_GRANT)


async def _seat_mandate(work_id: str, review_id: str, seat: str,
                        st: dict[str, Any], kind: str, *,
                        material: Optional[dict[str, Any]] = None,
                        matctx: Optional[dict[str, Any]] = None) -> str:
    """席位差事：**当轮任务口**（压在 user_prompt 位）。

    材料区只留**装配层给不了的**：brief（评审侧数据）＋草稿本体（按用户令
    不进任何上下文的评审对象）。子卡使命/现状/正稿由 ctx_card 的 bundle
    供给，不再复抄；confirm 另走快照 diff（更小）。

    [批三·按阶段缩量 2026-11-20] ``material``＝本轮材料身份；
    ``material["restricted"]`` 为真时走轻量差事——confirm 只给职责/
    裁定/身份/原票+稿次/现稿/真实 diff（＋可核实的原批准理由），
    vote_nudge 只给身份/依据/稿次/同版意见+发言锚/票态/原文片段坐标。
    """
    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")
    draft_ver = int(st.get("draft") or 1)
    name = await _name_of(work_id, seat)
    tgt_title = ""
    try:
        tgt_title = str((await card_store.read_card(
            work_id, target)).get("meta", {}).get("title") or "")
    except Exception:  # noqa: BLE001
        tgt_title = ""
    tgt = f"{target}" + (f"（{tgt_title}）" if tgt_title else "")
    brief = str(st.get("brief") or "").strip() or \
        f"（发起方未填写，仅有焦点：{st.get('focus') or '无'}）"
    material = material if isinstance(material, dict) else {}
    matctx = matctx if isinstance(matctx, dict) else {}
    _fp8 = str(material.get("draft_fp") or "")[:8]

    if str(st.get("type") or "") == "requirements_align":
        return await _align_mandate(work_id, review_id, seat, st,
                                    tgt=tgt, name=name, kind=kind)

    if kind == "confirm" and material.get("restricted"):
        return _confirm_mandate_lite(
            name=name, seat=seat, review_id=review_id, batch=batch,
            tgt=tgt, brief=brief, draft_ver=draft_ver, fp8=_fp8,
            material=material, matctx=matctx)

    if kind == "vote_nudge" and material.get("restricted"):
        return _nudge_mandate_lite(
            name=name, seat=seat, review_id=review_id, batch=batch,
            tgt=tgt, brief=brief, draft_ver=draft_ver, fp8=_fp8,
            material=material, matctx=matctx)

    if kind == "confirm":
        # [drift 收窄口径] 只评增量：你批的那份（快照）→ 现稿的条目级 diff。
        st_now = await review_store.status(work_id, review_id)
        snap = str(((st_now.get("votes") or {}).get(seat) or {})
                   .get("snapshot") or "")
        cur = await _draft_of(work_id, target)
        # [批三返修④] 缺旧快照＝**没有可信增量可算**：不能再发「只评增量」
        # 的空差事——回退完整重评：当前全文＋原评审条件如实给齐。
        if not snap:
            return "\n\n".join([
                "## 你是谁\n"
                f"你是本次评审的评审席：{name}（{seat}）。"
                f"评审卡 id = {review_id}。",
                "## 评审条件（发起方原文）\n" + brief,
                "## 这一轮的差事（重新评审）\n"
                f"你**批准过** {tgt} 的草稿，但批准时的原文快照缺失、"
                "无法对账——没有可信的增量可评。\n"
                f"请按**当前全文**（第 {draft_ver} 版）重新评审并表态："
                "通过调 `approve`，不通过调 `reject`"
                "（理由写在发言里、指向具体条目）。",
                "## 待评审的需求文档草稿（当前全文）\n"
                + (cur or "（草稿为空）"),
            ])
        diff = "\n".join(difflib.unified_diff(
            snap.splitlines(), cur.splitlines(),
            fromfile=f"你批准的 v{(st_now.get('votes') or {}).get(seat, {}).get('draft_ver')}",
            tofile=f"现稿 v{draft_ver}", lineterm=""))
        return "\n\n".join([
            "## 你是谁\n"
            f"你是本次评审的评审席：{name}（{seat}）。评审卡 id = {review_id}。",
            "## 这一轮的差事（只评增量）\n"
            f"你**批准过** {tgt} 的草稿，但批准之后草稿又改过——"
            f"现在收口的是第 {draft_ver} 版，不是你批的那版。\n"
            "**只评下面的增量 diff**：改动动摇你的通过就调 `reject`"
            "（理由写在发言里，指向具体条目）；不动摇就调 `approve`。\n\n"
            f"```diff\n{diff or '（两份草稿一致）'}\n```",
        ])

    materials = await _materials(work_id, review_id, st)
    if kind == "vote_nudge":
        task = ("## 这一轮的差事（补表态）\n"
                "你在本版草稿上发过言了，但**没有表态**——这一轮的差事只有一件："
                "把你的态度落成票，调 `approve` 或 `reject`。"
                "不通过的理由写在你的发言里（哪一条不满足、要改成什么）。")
    elif kind == "re_eval":
        task = ("## 这一轮的差事（重评）\n"
                "你否掉过上一版，正卡之后交回过回应——现在**重新评这份草稿**："
                "它解决了你的意见就调 `approve`；仍然不成立就调 `reject`，"
                "理由写在发言里（哪一条不满足、要改成什么）。")
    else:
        task = ("## 这一轮的差事\n"
                f"评审 **{tgt}** 的需求文档草稿（上面「待评审的草稿」一节，"
                f"现第 {draft_ver} 版）。它的卡面（使命/现状/正稿）、历史、"
                "记忆已在你的上下文里——那是背景；评审对象只有草稿。\n"
                "1. 先说出你的评审意见（发言就是正文，别只调工具）；\n"
                "2. 然后表态：通过调 `approve`，不通过调 `reject`——"
                "**理由写在你的发言里**（哪一条不满足、要改成什么），"
                "reject 只是投票不带理由；\n"
                "3. 评审中若得出**值得立成本项目规矩的判据**（这类东西都该"
                "这么量），写在你的发言里点出来——规则文件（rules.md 里"
                "你的 `## function.<你的code>` 区）只有人能改，你提、人裁定，"
                "不私记台账。")
    return "\n\n".join([
        "## 你是谁\n"
        f"你是本次评审的评审席：{name}（{seat}）。评审卡 id = {review_id}。\n"
        f"被评的是 **{tgt}**——你的上下文锚在它那张卡上（卡面/历史/记忆"
        "都已在场）；下面只有两样是它卡面之外的：发起方的评审要求、"
        "和它那份不进任何上下文的**草稿本体**。",
        materials,
        task,
    ])


def _confirm_mandate_lite(*, name: str, seat: str, review_id: str,
                          batch: str, tgt: str, brief: str,
                          draft_ver: int, fp8: str,
                          material: dict[str, Any],
                          matctx: dict[str, Any]) -> str:
    """[批三] confirm 轻量差事：只给完成**增量确认**所需的材料（§三表）。

    材料清单：席位职责(你是谁)＋原评审条件(brief)＋评审卡/被评卡/批次
    身份＋原批准票及其稿次＋现稿版本＋批准快照→现稿的**真实 diff**
    （逐字相同才显示「正文一致」＋并列两版）＋原批准理由（有可核实的
    发言锚才带）。
    """
    vote_ver = int(matctx.get("vote_ver") or 0)
    snap = str(matctx.get("snap") or "")
    cur = str(matctx.get("draft") or "")
    if material.get("identical"):
        diff_sec = (f"你批准的稿（v{vote_ver}）与现稿（v{draft_ver}）"
                    "**逐字相同——正文一致**。")
    else:
        diff = "\n".join(difflib.unified_diff(
            snap.splitlines(), cur.splitlines(),
            fromfile=f"你批准的 v{vote_ver}", tofile=f"现稿 v{draft_ver}",
            n=8, lineterm=""))
        diff_sec = f"```diff\n{diff}\n```"
    parts = [
        "## 你是谁\n"
        f"你是本次评审的评审席：{name}（{seat}）。评审卡 id = {review_id}。",
        "## 评审条件（发起方原文）\n" + brief,
        "## 这一轮的差事（增量确认）\n"
        f"你在批次 {batch} 投过 `approve`（第 {vote_ver} 版）。"
        f"现在收口的是 {tgt} 的第 {draft_ver} 版（指纹 {fp8}）。\n"
        "**只评下面的增量**（你批准的稿 → 现稿）：改动动摇你的通过就调 "
        "`reject`（理由写在发言里、指向具体条目）；不动摇就调 `approve`。\n\n"
        + diff_sec,
        "## 你的原票\n"
        f"verdict=approve · 稿次 v{vote_ver} · 批次 {batch}",
    ]
    appr = matctx.get("appr_speech")
    if isinstance(appr, dict):
        # [批三返修④] 原批准理由走**隔离投影**——块上的 quarantine/
        # ctx_content 是治理结果，直接取 content 会把已隔离的伪调用/
        # 假结果原文重新喂进轻量差事。锚元数据照常保留。
        from ..storage import predicates as _pred
        anch = _speech_anchor(appr)
        parts.append(
            "## 你的原批准理由（发言锚：批次 {b} · 稿次 v{v} · 指纹 {f}）\n{t}"
            .format(b=str(anch.get("batch") or batch),
                    v=int(anch.get("draft_ver") or vote_ver),
                    f=str(anch.get("draft_fp") or "")[:8],
                    t=str(_pred.context_content(appr) or "").strip()))
    return "\n\n".join(parts)


def _nudge_mandate_lite(*, name: str, seat: str, review_id: str,
                        batch: str, tgt: str, brief: str,
                        draft_ver: int, fp8: str,
                        material: dict[str, Any],
                        matctx: dict[str, Any]) -> str:
    """[批三] vote_nudge 轻量差事：只给完成**同版补票**所需的材料（§三表）。

    材料清单：共用身份＋评审依据(brief)＋当前稿次/指纹＋该席同批同版
    **可核实意见**（原文＋发言锚）＋当前票态＋意见涉及的草稿原文片段
    及坐标。意见原文不动——只截取片段坐标给草稿原文。
    """
    opinion = matctx.get("opinion") or {}
    opinion_text = str(matctx.get("opinion_text") or "").strip()
    anch = _speech_anchor(opinion)
    cur = str(matctx.get("draft") or "")
    lines = cur.split("\n")
    excerpt_parts = []
    for blk_name, a, b in (matctx.get("located") or []):
        seg = "\n".join(lines[a - 1:b])
        excerpt_parts.append(
            f"#### {blk_name or '（文首）'}（现稿 v{draft_ver} 第 {a}-{b} 行）\n{seg}")
    st_now = matctx.get("status") or {}
    votes = st_now.get("votes") or {}
    members = [str(m) for m in (st_now.get("members") or [])]
    vote_lines = [
        f"- {m}: {(votes.get(m) or {}).get('verdict') or '未表态'}"
        + (f"@v{(votes.get(m) or {}).get('draft_ver')}"
           if (votes.get(m) or {}).get("verdict") else "")
        for m in members] or ["（成员名单缺失）"]
    return "\n\n".join([
        "## 你是谁\n"
        f"你是本次评审的评审席：{name}（{seat}）。评审卡 id = {review_id}。",
        "## 评审条件（发起方原文）\n" + brief,
        "## 这一轮的差事（同版补票）\n"
        f"你在当前稿（{tgt} 第 {draft_ver} 版）上发过言、**没有落票**——"
        "本轮就这一件事：把态度落成票，调 `approve` 或 `reject`"
        "（理由要改就写进新发言；不改就照原意见落票）。",
        "## 你的原意见（发言锚：批次 {b} · 稿次 v{v} · 指纹 {f}）\n{t}".format(
            b=str(anch.get("batch") or batch),
            v=int(anch.get("draft_ver") or draft_ver),
            f=str(anch.get("draft_fp") or fp8)[:8],
            t=opinion_text),
        "## 意见涉及的草稿片段（现稿第 {v} 版坐标）\n{e}".format(
            v=draft_ver, e="\n\n".join(excerpt_parts) or "（定位失败）"),
        "## 当前票态（批次 {b}）\n{l}".format(b=batch, l="\n".join(vote_lines)),
    ])


async def _align_mandate(work_id: str, review_id: str, seat: str,
                         st: dict[str, Any], *, tgt: str, name: str,
                         kind: str) -> str:
    """[两轮评审 2026-11-14] 对齐批 mandate：总裁一席，判「偏离不偏离」。

    判据材料＝根卡需求正稿（最高）＋父卡需求正稿＋被评草稿——两份正稿
    不在被评卡 ctx 里（ctx 锚是子卡），由 mandate 直接注入；父卡＝根卡
    或父卡未定稿时如实标注，不让它脑补不存在的约束。
    """
    target = str(st.get("target") or "")
    draft_ver = int(st.get("draft") or 1)
    draft = await _draft_of(work_id, target)
    brief = str(st.get("brief") or "").strip()
    root_id = ""
    try:
        from ..storage import work_store as _ws
        root_id = str(_ws.work_card_id_of(work_id) or "")
    except Exception:  # noqa: BLE001
        root_id = ""
    parent_id = ""
    try:
        parent_id = str((await card_store.read_card(
            work_id, target)).get("meta", {}).get("parent") or "")
    except Exception:  # noqa: BLE001
        parent_id = ""
    root_req = await _requirements_of(work_id, root_id) if root_id else ""
    if parent_id and parent_id != root_id:
        parent_req = await _requirements_of(work_id, parent_id)
        parent_sec = (f"【父卡（{parent_id}）的需求文档正稿】\n"
                      f"{parent_req or '（父卡未定稿——判据以根卡为准）'}")
    else:
        parent_sec = "（本卡是根卡的直接子卡——父卡就是根卡，判据同上）"
    if kind == "vote_nudge":
        task = ("## 这一轮的差事（补表态）\n"
                "对齐闸还没落票——发言说不说过随你，票必须落："
                "不偏离调 `approve`，偏离调 `reject`（每条偏离写进发言、"
                "指向具体条目）。")
    else:
        task = ("## 这一轮的差事（对齐闸·一轮定生死）\n"
                f"你只对 **{tgt}** 的需求文档草稿（第 {draft_ver} 版）判一件事："
                "**偏不偏离**根卡/父卡的需求文档。\n"
                "- 范围越权、方向冲突、越级承诺、与上位条款打架＝偏离；"
                "措辞细节、格式、实现选择不算偏离。\n"
                "- **先发言再表态**：意见写正文（每条偏离指向具体条目——"
                "否决后这份清单会原样退回子卡）；然后调 `approve` 放行进入"
                "三席详评，或调 `reject` 终局（评审当场结束、问题退回子卡，"
                "由它向上汇报等裁定或自己改方向重提）。")
    return "\n\n".join([
        "## 你是谁\n"
        f"你是本批次唯一的评审席：{name}（{seat}）——**总裁**。评审卡 id = "
        f"{review_id}。\n这一批是**对齐轮**：你是唯一一票，你的否决就是终局。",
        "## 根卡的需求文档正稿（最高判据）\n" + (root_req or "（根卡未定稿）"),
        parent_sec,
        ("## 发起方填写：任务背景／要评审的内容（原文）\n" + brief
         ) if brief else "",
        "## 待评审的需求文档草稿（评审对象就是这一份）\n"
        + (draft or "（草稿为空）"),
        task,
    ])


async def _materials(work_id: str, review_id: str,
                     st: dict[str, Any]) -> str:
    """ mandate 材料＝**只有正常装配给不了的两样**：发起方 brief（评审侧
    数据）＋ 待评审草稿（用户令：草稿不进上下文/不进 read_card，差事是它
    进上下文的唯一通道）。

    [去重 2026-11] 子卡使命/现状/上一批正稿**不再嵌**——ctx_card 装配的
    bundle 已经把子卡卡面（使命/现状/正稿）摆进上下文，复抄一遍是送两次。
    """
    target = str(st.get("target") or "")
    draft = await _draft_of(work_id, target) if target else ""
    brief = str(st.get("brief") or "").strip() or \
        f"（发起方未填写，仅有焦点：{st.get('focus') or '无'}）"
    return "\n\n".join([
        "## 发起方填写：任务背景／当前状况／要评审的内容（原文）\n" + brief,
        "## 待评审的需求文档草稿（**评审对象就是这一份**）\n"
        + (draft or "（草稿为空）"),
    ])


async def _prior_blocks(work_id: str, review_id: str) -> list[dict[str, Any]]:
    """场内过程进席位历史的**收敛口径**（用户令：顶多上一轮三席观点）：

    - **席位发言**：actor=assistant 且 `from` 非空（同 `_batch_transcript`
      判据），每席只留**最近一次**——新一轮开场正好是上一轮三席观点；
      轮中则是「已发言席的本轮＋未发言席的上轮」，总量封顶席位数的段数。
    - **用户发言**：全留——「我说话它们看不见，那我为什么要说话」。
    - **工具块/工具调用信封**（assistant 无 from、actor=tool）：丢——
      场内务不进席位上下文，同派发侧口径。
    """
    try:
        blocks = await session_facade.read(work_id, review_id) or []
        if isinstance(blocks, dict):
            blocks = blocks.get("blocks") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 读评审卡会话失败 review=%s: %s", review_id, exc)
        return []
    last_speech: dict[str, int] = {}
    for i, b in enumerate(blocks):
        if (str(b.get("actor") or "") == "assistant"
                and str(b.get("from") or "").strip()
                and str(b.get("content") or "").strip()):
            last_speech[str(b.get("from"))] = i
    keep = set(last_speech.values())
    out: list[dict[str, Any]] = []
    for i, b in enumerate(blocks):
        content = str(b.get("content") or "")
        actor = str(b.get("actor") or "")
        if not content.strip():
            continue
        if actor == "assistant":
            seat = str(b.get("from") or "").strip()
            if not seat or i not in keep:
                continue
            out.append({"role": "assistant",
                        "content": f"—— {seat} ——\n{content}"})
        elif actor != "tool":
            out.append({"role": "user", "content": content})
    return out


# ---------------------------------------------------------------- 派活给正卡
async def _dispatch_child(work_id: str, review_id: str,
                          st: dict[str, Any], *, kind: str) -> Any:
    """派活＝**给正卡发一条正经消息**，它自己开普通回合干活。

    消息落**正卡自己的会话**（`actor=card:<评审卡>`，页面看得见来路），
    `extra.batch` 带批次号可审计；唤醒走既有 `deliver_now`——正卡在跑就
    不打断（它本轮收口的自查续轮会接上，信不丢）。

    ``kind``：``revise``（请它按意见改稿，交回 submit_revision）／
              ``promote``（请它调 promote_draft 定稿）。
    已派的活由 `mark_asked`/`promote_asked` 记成机械事实，不重复派。

    返回值：``True``＝信落＋写回都成；``False``＝信没落；``"stale"``
    ＝信落了但**写回被 CAS 拒**（append 期间批次/稿次已换——旧批的
    写回不落进新批，由调用方按过期销账，不谎报送达）。
    """
    from . import message_runner as _mr

    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")
    if not target:
        return False
    draft_ver = int(st.get("draft") or 1)
    # [写回核对 2026-11-20 · 批次A第五轮返工] append 是耗时外呼，
    # 期间批次可能换——写回必须带着快照身份过 CAS，写不进就报 stale。
    exp = {"batch": batch, "draft": draft_ver}
    if kind == "promote":
        text = (f"〔评审场 · {review_id} · 批次 {batch}〕\n"
                f"你的需求文档草稿（第 {draft_ver} 版）评审**已全员通过**。\n"
                "请调 `promote_draft` 把草稿转成正稿——这是本批次唯一剩下的动作。")
        rec = await session_facade.append_card_message(
            work_id, target, from_card=review_id, text=text,
            msg_type="msg_directive", event_type="review_dispatch",
            extra={"batch": batch, "kind": kind})
        if not rec:
            return False
        marked = await review_store.mark_promote_asked(
            work_id, review_id, at_seq=rec.get("seq"), expect=exp)
    else:
        said = await _batch_transcript(work_id, review_id, batch)
        text = (f"〔评审场 · {review_id} · 批次 {batch} · 草稿 v{draft_ver}〕\n"
                "你的需求文档草稿正在被评审，下面是评审现场成员的话（原样）。\n\n"
                f"{said}\n\n"
                "把该改的改掉（`edit_requirements`）；改完（或你决定不改）之后"
                "**调 `submit_revision` 交回本轮结论**——决定不改也要写明理由。"
                "不交回，评审就一直等你。")
        rec = await session_facade.append_card_message(
            work_id, target, from_card=review_id, text=text,
            msg_type="msg_directive", event_type="review_dispatch",
            extra={"batch": batch, "kind": kind})
        if not rec:
            return False
        marked = await review_store.mark_asked(
            work_id, review_id, at_seq=rec.get("seq"), expect=exp)
    if not marked:
        logger.info("[review] 派活写回被 CAS 拒（快照批次=%s v%s 已换）"
                    " review=%s kind=%s —— 按过期销账",
                    batch, draft_ver, review_id, kind)
        return "stale"
    # 落成 ⇒ 派活失败计数清零（一个批次的连续失败才累积封顶）。
    try:
        await review_store.clear_dispatch_fails(
            work_id, review_id, expect=exp)
    except Exception:  # noqa: BLE001 清计数失败不阻断派活
        pass
    try:
        event_stream.emit(work_id, review_id, "review_dispatch", {
            "batch": batch, "target": target, "kind": kind,
            "draft": draft_ver})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    return await _mr.deliver_now(
        work_id, target,
        text="你收到评审场的一条指令（见上一条消息），请按它执行。",
        steer_text=text,
        source={"kind": "review_dispatch", "from": review_id,
                "batch": batch, "draft": draft_ver, "step": kind})


# -------------------------------------------------- 两轮评审：对齐批收口动作
async def seat_deployments(work_id: str, review_id: str, batch: str,
                           batch_type: str, target: str,
                           members: list[str], delegator: str) -> None:
    """[职能底座 2026-10-31] 每席位一条差事：部署账与评审生命周期绑死。

    exit=verdict：本批次投出一张立着的票（revoked=0）即算办完；
    revoke 撤票 ⇒ 惰性核对自动让它回到在派。单席建账失败不卡开门。
    [两轮评审 2026-11-14] 从 _h_open_review 提成共用件：对齐批开时建
    总裁一席的账，晋级第二轮时本函数再为三席各建一条（batch_id 各自）。
    """
    from ..storage import function_store as _fs
    from . import function_room as _fr
    for _f in members:
        try:
            await _fs.create_deployment(
                work_id, _f, venue_kind="review", venue_ref=review_id,
                venue_label=f"{batch_type} 评审 · {target} 批次 {batch}",
                mandate=_fr.render_mandate(
                    "review", function=_f, review_card_id=review_id,
                    batch=str(batch), target=target)
                or f"本批次 {batch} 评审席（{target}）",
                delegator=delegator, batch_id=str(batch),
                target_card=target, exit_kind="verdict",
                tools=_fr.grant_for(work_id, _f, "review"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[差事] 评审席位建账失败 seat=%s: %s", _f, exc)


async def _requirements_of(work_id: str, card_id: str) -> str:
    """读卡的需求文档**正稿**（requirements 列）。读不到返回空串。"""
    try:
        card = await card_store.read_card(work_id, card_id)
        return str((card.get("body") or {}).get("requirements") or "")
    except Exception:  # noqa: BLE001
        return ""


async def _advance_from_align(work_id: str, review_id: str,
                              st: dict[str, Any]) -> dict[str, Any]:
    """[两轮评审 2026-11-14] 对齐批通过 ⇒ 关批＋同评审卡自动开第二轮三席批。

    第二轮成员＝开批时存进 ``next_members`` 的既定名单（open_review 时已按
    点名/目录默认/职责书/作者排除过滤过）；名单空着兜底用场默认再决议一次。
    """
    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")
    nxt = [str(m) for m in (st.get("next_members") or []) if str(m)]
    if not nxt:
        # 兜底（旧批态/异常路径）：场默认 code 过职能表再决议一次成 fn id——
        # 直接拿 code 进 members 会谁都投不了票（投票闸认 fn id）。
        try:
            from . import card_tools as _ct
            from . import function_room as _fr
            wanted = set(_ct._venue_default_codes("requirements"))
            for p in function_store.list_profiles(work_id):
                fid = str(p.get("function_id") or "")
                if not fid or str((p.get("duty") or {}).get("state")
                                  or "active") != "active":
                    continue
                if fid in wanted or _fr.catalog_code_of(p) in wanted:
                    nxt.append(fid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[review] 第二轮名单决议失败: %s", exc)
    if not nxt:
        # 没席可开：不开空场（谁也投不了票＝永久卡批）。批次要关掉——
        # 留着会让 classify 每步都返回 advance 空转。
        logger.warning("[review] 对齐通过但第二轮无席可用 review=%s", review_id)
        await review_store.close_batch(
            work_id, review_id,
            verdict="对齐通过但第二轮无席可用（批次已关，重新 open_review）")
        return {"continued": False, "reason": "advance_no_members",
                "_done": {"reason": "advance_no_members", "continued": False}}
    # 开新批同时归档旧批。发布失败时保留旧批，后续可重试晋级。
    st2 = await review_store.open_batch(
        work_id, review_id, str(st.get("focus") or ""),
        members=nxt, target=target, brief=str(st.get("brief") or ""),
        batch_type="requirements", expect_batch=batch)
    # 第二轮各席的差事账（batch_id 用新批次）——失败不卡开门。
    try:
        await seat_deployments(work_id, review_id, str(st2.get("batch") or ""),
                               "requirements", target, nxt, target)
    except Exception:  # noqa: BLE001
        pass
    try:
        event_stream.emit(work_id, review_id, "review_align_passed",
                          {"batch": batch, "target": target,
                           "next_batch": str(st2.get("batch") or ""),
                           "members": nxt})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    # 续跑不在此点火：run_once 收尾的 round_done 按新批态重算 has_next=True，
    # 由 harness 续点第二轮第一席（在步内 spawn 会撞 _RUNNING 锁白死）。
    return {"continued": False, "reason": "align_passed",
            "_done": {"reason": "align_passed", "continued": True,
                      "next_batch": str(st2.get("batch") or "")}}


async def _terminate_align(work_id: str, review_id: str,
                           st: dict[str, Any]) -> dict[str, Any]:
    """[两轮评审 2026-11-14] 对齐批否决 ⇒ 终局：关批＋否决通知投正卡会话。

    用户裁定「退回对话，让它自己向上汇报等裁定，或者自己改方向」——
    通知落正卡会话（含总裁发言原样）并唤醒一次，评审不自动重开、
    不派修订循环；``requirements_state`` 标 ``vetoed``（草稿不清）。
    """
    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")
    draft_ver = int(st.get("draft") or 1)
    said = await _batch_transcript(work_id, review_id, batch)
    await review_store.close_batch(
        work_id, review_id,
        verdict=f"对齐轮否决（批次 {batch}，意见已退回 {target}）")
    try:
        await card_store.update_card(
            work_id, target, {"requirements_state": "vetoed"},
            f"对齐评审否决（批次 {batch}）")
    except Exception as exc:  # noqa: BLE001 状态位落不上不阻断终局
        logger.warning("[review] vetoed 状态落不上 target=%s: %s", target, exc)
    text = (f"〔评审场 · {review_id} · 对齐轮否决 · 草稿 v{draft_ver}〕\n"
            "总裁否决了你的需求文档草稿——它与根卡/父卡的需求文档对不上。\n\n"
            f"{said}\n\n"
            "评审已经结束，不会自动重开。你的两条路：\n"
            "1. `report_up` 向上汇报，等上面的裁定；\n"
            "2. 自己改方向：`edit_requirements` 重写草稿后重新 `open_review`。")
    delivered = False
    try:
        rec = await session_facade.append_card_message(
            work_id, target, from_card=review_id, text=text,
            msg_type="msg_directive", event_type="review_align_vetoed",
            extra={"batch": batch, "kind": "veto"})
        if rec:
            from . import message_runner as _mr
            delivered = bool(await _mr.deliver_now(
                work_id, target,
                text="你收到评审场的一条终局通知（见上一条消息）。",
                steer_text=text,
                source={"kind": "review_dispatch", "from": review_id,
                        "batch": batch, "step": "veto"}))
    except Exception as exc:  # noqa: BLE001 通知没落＝如实报，终局不变
        logger.warning("[review] 否决通知投递失败 target=%s: %s", target, exc)
    # [两轮评审 2026-11-14 · 用户口径「评审问题和结果发回给总裁裁决」]
    # 否决是总裁席上的人格判的，但裁决活在总裁**本体会话**——问题清单
    # （它自己的发言原样）＋结果回投根卡，裁决手段是它的既有工具：
    # direct 令它改稿重提 / dispose 砍卡 / confirm_with_user 上报用户。
    try:
        from ..storage import work_store as _ws
        _root = str(_ws.work_card_id_of(work_id) or "")
    except Exception:  # noqa: BLE001
        _root = ""
    ceo_delivered = False
    if _root and _root != target:
        ceo_text = (f"〔评审场 · {review_id} · 对齐轮终局 · {target} 草稿 "
                    f"v{draft_ver}〕\n"
                    "你在对齐轮否决了它的需求文档草稿——评审已结束（不会"
                    "自动重开）。你在评审席上的意见原样如下：\n\n"
                    f"{said}\n\n"
                    f"{target} 已收到否决通知（state=vetoed），它可能向上"
                    "汇报等裁定、也可能自己改方向重提。要现在裁决，手段在"
                    "你手上：direct 给它指示 / delete_card 砍掉 / "
                    "confirm_with_user 上报用户。")
        try:
            crec = await session_facade.append_card_message(
                work_id, _root, from_card=review_id, text=ceo_text,
                msg_type="msg_directive", event_type="review_align_vetoed",
                extra={"batch": batch, "kind": "veto_ruling",
                       "target": target})
            if crec:
                from . import message_runner as _mr
                ceo_delivered = bool(await _mr.deliver_now(
                    work_id, _root,
                    text=f"评审场的一份对齐轮终局裁决请求（{target}，见上一条）。",
                    steer_text=ceo_text,
                    source={"kind": "review_dispatch", "from": review_id,
                            "batch": batch, "step": "veto_ruling"}))
        except Exception as exc:  # noqa: BLE001 回投失败不挡终局
            logger.warning("[review] 否决回投总裁失败 root=%s: %s", _root, exc)
    try:
        event_stream.emit(work_id, review_id, "review_align_vetoed",
                          {"batch": batch, "target": target,
                           "draft": draft_ver, "delivered": delivered,
                           "ceo_delivered": ceo_delivered})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    return {"continued": False, "reason": "align_vetoed",
            "_done": {"reason": "align_vetoed", "continued": False,
                      "delivered": delivered,
                      "ceo_delivered": ceo_delivered}}


async def _batch_transcript(work_id: str, review_id: str,
                            batch: str) -> str:
    """本批次评审现场的**席位发言**，按会话里的原始顺序排列（给正卡看的意见）。

    [场内外边界 2026-11-02 · 用户裁定「评审卡上下文不进子卡上下文」]
    只收**席位发言块**——机械判据 ``actor=="assistant" and from!=空``
    （``from``＝席位回合落块时按 act_as 打的身份，只有席位发言才有）。
    工具块一律不收：投票信封 ``{"verdict":..,"batch":..}``、席位
    ``read_card`` 读出的正卡卡面 JSON、工具调用信封都是**场内内务**，
    原样进指令就是把评审场上下文灌进子卡上下文（实案：旧实现整份会话
    非空块全送，内部件全漏）。
    """
    try:
        blocks = await session_facade.read(work_id, review_id) or []
        if isinstance(blocks, dict):
            blocks = blocks.get("blocks") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 读评审会话失败 review=%s: %s", review_id, exc)
        return "（评审会话读不到）"
    # [2026-11-02 转录范围收窄] 只收**每席最新一条**——派单在全员发完本轮言后
    # 触发，每席最新即本轮意见。此前全量收：同一批次迭代几十个草稿版本，
    # 每条派单把自开场以来的全部发言史重发一遍、单调胀爆（实案 card_146→
    # card_145 批次 rv_68d145b6d0 v119：63 条发言史 ~52 万字符/条派单）。
    rows: list[tuple[str, str]] = []       # (speaker, content) 按会话顺序
    last: dict[str, int] = {}              # speaker → rows 下标（覆写即最新）
    for b in blocks:
        if str(b.get("actor") or "") != "assistant":
            continue
        speaker = str(b.get("from") or "").strip()
        if not speaker:
            continue                       # 非席位块（工具调用信封等）不收
        content = str(b.get("content") or "")
        if content.strip():
            last[speaker] = len(rows)
            rows.append((speaker, content.strip()))
    keep = set(last.values())
    out = [f"—— {speaker} ——\n{content}"
           for j, (speaker, content) in enumerate(rows) if j in keep]
    return "\n\n".join(out) or "（本批次还没有发言）"


# ---------------------------------------------------------------- 事件
async def _report(work_id: str, review_id: str,
                  done: dict[str, Any]) -> None:
    """步收尾的事实上报——**只在 _RUNNING 释放之后**由 `run_once` 调。

    `has_next`＝"还有未处理项"（包括补投后仍在等票）；``runnable``＝
    "现在这一步真的能跑"——除有下一动作
    外还要这一步的结局不是「等外部条件」（见下文注释）。压制规则沿旧：
    这一步刚出错且下一个动作又是席位回合 ⇒ false——模型刚死就
    立刻重叫同一席只是空烧，等新事件（投票/回应/发言）自然再接；下一个动作
    是派活（机械件）则照常 true——seat 回合崩了但票已齐，派活不依赖模型。
    """
    try:
        st = await review_store.state_of(work_id, review_id, strict=True)
        st_now = await review_store.status(work_id, review_id)
        spoken = set(await review_store.spoken_round(work_id, review_id))
        batch = str(st.get("batch") or "")
        if not batch:
            return                       # 批次没了没什么可报的
        _facts = await _target_facts(work_id, str(st.get("target") or ""))
        promoted = bool(st_now.get("all_passed")) and _facts["promoted"]
        nxt, stop = _classify(st, st_now, spoken, promoted=promoted,
                               tail_seq=_tail_seq(
                                   work_id, str(st.get("target") or "")),
                               has_draft=_facts["has_draft"])
        err = str(done.get("error") or "")
        reason = str(done.get("reason") or "")
        # [待处理项与可运行动作分家 2026-11-20 · Harness 方案 §二.1]
        # 正卡还在等待解除停止、解除缄默或完成在途回合时，让后续状态变化再接力。
        _WAIT_END = ("delivered_quiet", "wake_delivered")
        runnable = bool(
            nxt is not None
            and reason != "step_error"          # 步内异常不自动续（防同类故障空转）
            and not (err and nxt[0] == "seat")  # 回合刚崩不立刻重叫同一席
            and not reason.startswith("awaiting_")
            and reason not in _WAIT_END)
        event_stream.emit(work_id, review_id, "review_round_done", {
            "batch": batch,
            "draft": int(st_now.get("draft") or 1),
            "draft_fp": _facts.get("draft_fp", ""),
            "all_passed": bool(st_now.get("all_passed")),
            "passed": list(st_now.get("passed") or []),
            "pending": list(st_now.get("pending") or []),
            "seat": str(done.get("seat") or ""),
            "voted": bool(done.get("voted")),
            "reason": str(done.get("reason") or ""),
            "continued": bool(done.get("continued")),
            "has_next": nxt is not None or stop == "awaiting_vote",
            "runnable": runnable,
            # [下一步身份 2026-11-20 · 批次A返工1] 由生产者上报"下一个
            # 动作是什么"——同一步的重复上报同串（去重锚），步变了串变
            # （dispatch:promote → wake:promote:consumed 是两个动作）。
            "next": _next_tag(nxt),
            "error": err,
        })
    except Exception:  # noqa: BLE001 事件失败不影响主流程
        pass


def _step_wm(basis: Any) -> str:
    """触发水位 → 步骤身份串的末段。

    [批次A第六轮返工] 同一席位动作可能被**同一批同一稿**触发两次
    （否决席对每次新回应各重评一次、票态一变就该再补投）——去重键
    里必须带「这次触发的水位」，否则第二次被当重放吞掉。
    标量直接用原值（可读）；复合依据取 8 位摘要（确定、紧凑）。"""
    if basis is None or basis == "":
        return ""
    if isinstance(basis, bool):
        return str(int(basis))
    if isinstance(basis, (int, float)):
        return str(int(basis))
    try:
        import hashlib
        raw = json.dumps(basis, ensure_ascii=False, sort_keys=True,
                         default=str)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        return "x"


def _next_tag(nxt: Any) -> str:
    """``_classify`` 下一动作 → 稳定身份串（评审事件 ``next`` 字段）。

    同一步的重复上报产同串 ⇒ harness 按它去重；动作换了（派活→补唤醒、
    换席、换 kind）串就变 ⇒ 各是各的投递义务。

    席动作第 4 元＝**触发水位**（重评＝回应水位、补投＝票态依据摘要、
    确认＝drift 账摘要）：同一席第二次触发靠它分清。"""
    if not nxt:
        return ""
    k = str(nxt[0] or "")
    if k == "seat":
        tag = f"seat:{nxt[1]}:{nxt[2]}"
        if len(nxt) > 3 and nxt[3] not in (None, ""):
            tag += f":{nxt[3]}"
        return tag
    if k == "dispatch":
        return ":".join(str(part) for part in nxt)
    if k == "wake":
        return f"wake:{nxt[1]}:{nxt[2]}"
    return k


async def _step_identity(work_id: str, review_id: str) -> dict[str, str]:
    """当前评审步骤身份 ``{batch, draft, next}``——与 ``_run_step``
    前段同一组机械读数＋``_classify``，**只读不执行**。

    ``run_once(expect=...)`` 用它做执行前复核：台账登记的步骤身份
    与此处现状不符 ⇒ stale_step 销账。"""
    st = await review_store.state_of(work_id, review_id)
    st_now = await review_store.status(work_id, review_id)
    batch = str(st.get("batch") or "")
    draft = str(int(st_now.get("draft") or 1))
    if not batch:
        return {"batch": "", "draft": draft, "next": ""}
    spoken = set(await review_store.spoken_round(work_id, review_id))
    target = str(st.get("target") or "")
    facts = await _target_facts(work_id, target)
    promoted = bool(st_now.get("all_passed")) and facts["promoted"]
    nxt, _stop = _classify(st, st_now, spoken, promoted=promoted,
                           tail_seq=_tail_seq(work_id, target),
                           has_draft=facts["has_draft"])
    return {"batch": batch, "draft": draft, "next": _next_tag(nxt),
            "draft_fp": str(facts.get("draft_fp") or "")}


# ---------------------------------------------------------------- 被评卡操作（任务书 2026-11-25）
#: 等待责任方映射——stop_reason/现状 → 谁在等、等什么条件。
#: 「等谁」是事实不是推断：席位缺票＝席位侧；已派活/已问未定稿＝正卡侧；
#: 派发/唤醒链自身卡住＝系统侧。stop_reason 全集与 _classify/_run_step 同源。
_WAITING_MAP: dict[str, tuple[str, str]] = {
    "awaiting_vote":        ("seats",    "等待席位表态"),
    "awaiting_target":      ("target",   "待正卡修订／交回回应"),
    "awaiting_promote":     ("target",   "待正卡定稿（promote_draft）"),
    "awaiting_draft_sync":  ("target",   "待草稿版本落定（材料与登记稿次未对齐）"),
    "draft_empty":          ("target",   "草稿为空——待正卡重新起草"),
    "dispatch_capped":      ("dispatch", "派发连续失败封顶——需续跑/人工介入"),
    "promoted":             ("none",     "已晋级（正稿已写）"),
    "veto":                 ("none",     "已被否决（终局）"),
    "stalled":              ("none",     "无待处理项"),
    "delivered_quiet":      ("target",   "指令已送达，正卡忙/停中——自查接上"),
    "wake_delivered":       ("target",   "补唤醒已发出，待正卡响应"),
    "awaiting_halted":      ("target",   "正卡已停——解停后自查接上"),
    "awaiting_silenced":    ("target",   "正卡缄默中"),
    "awaiting_busy":        ("target",   "正卡回合在跑"),
    "seat_no_step":         ("none",     "点名席当前无合法步骤"),
}


def _next_view(nxt: Any) -> dict[str, Any]:
    """动作元组 → 可读视图（标签＋席位＋动作类）。"""
    tag = _next_tag(nxt)
    if not tag:
        return {"tag": ""}
    out = {"tag": tag}
    if nxt and nxt[0] == "seat":
        out["seat"] = str(nxt[1] or "")
        out["kind"] = str(nxt[2] or "")
    elif nxt and nxt[0] in ("dispatch", "wake"):
        out["kind"] = str(nxt[1] or "")
    elif nxt:
        out["kind"] = str(nxt[0] or "")
    return out


async def review_snapshot(work_id: str, review_id: str, *,
                          caller: str = "") -> dict[str, Any]:
    """**只读**评审现状汇总——``read_review_status`` 与人工排障共用。

    回执五块：①批次/场型/稿次；②逐席票态（票值＋**票所属稿次**＋是否
    覆盖当前稿＝本轮新票）；③现有下一步（身份三元组同源 ``_classify``）；
    ④等待责任方与条件；⑤最近一次续跑动作的台账回执。
    读不出的事实一律标 ``unverified``——「待核实」不猜。

    [v7/v10 分账 2026-11-25 · card_982 实案] 票必须带 ``draft_ver`` 展示：
    旧稿通过票与当前稿待确认是**两个事实**——``covered``＝票覆盖当前稿
    （v10 有真票）与 ``stale``＝批的是旧稿（v7 旧票）可以同席并查；
    确认回合零投票时 ``covered`` 依旧假＝「v10 仍缺结果」如实显示。
    """
    out: dict[str, Any] = {"review_card": review_id, "caller": caller}
    unverified: list[str] = []
    owner_verified = False
    try:
        from ..infra import procs
        if _remote_review_owner(work_id, review_id):
            snapshot = await procs.forward_ctl(
                work_id, review_id, "/ctl/review_snapshot",
                {"work_id": work_id, "review_id": review_id, "caller": caller})
            if snapshot.get("review_card") == review_id:
                return snapshot
            unverified.append("review_owner")
        else:
            owner_verified = True
    except Exception:  # noqa: BLE001 保留本地可查的持久事实
        unverified.append("review_owner")
    st: dict[str, Any] = {}
    st_now: dict[str, Any] = {}
    try:
        st = await review_store.state_of(work_id, review_id, strict=True)
    except Exception as exc:  # noqa: BLE001
        unverified.append(f"state:{exc}")
    try:
        st_now = await review_store.status(work_id, review_id)
    except Exception as exc:  # noqa: BLE001
        unverified.append(f"status:{exc}")
    spoken = set(review_store.spoken_at_draft(st))
    batch = str(st.get("batch") or "")
    draft = int(st_now.get("draft") or st.get("draft") or 1)
    if st_now and (str(st_now.get("batch") or "") != batch
                   or int(st.get("draft") or 1) != draft):
        unverified.append("review_state_changed")
        st_now = {}
        draft = int(st.get("draft") or 1)
    votes = dict(st_now.get("votes") or {})
    stale = [str(m) for m in (st_now.get("stale") or [])]
    seats_state = dict(st.get("seats") or {})
    members = [str(m) for m in (st_now.get("members") or [])]
    seat_rows = []
    for m in members:
        v = votes.get(m) or {}
        verdict = str(v.get("verdict") or "")
        dver = int(v.get("draft_ver") or 0)
        srow = seats_state.get(m) or {}
        seat_rows.append({
            "seat": m,
            "verdict": verdict or "未表态",
            "draft_ver": dver or None,
            # 票覆盖当前稿＝本轮新票；approve 且 draft_ver<draft＝旧稿票
            "covered": bool(verdict) and dver == draft,
            "stale": m in stale,
            "spoken": m in spoken,
            "response_seen": srow.get("response_seen"),
            "nudge_issued": srow.get("vote_nudge_basis") is not None,
        })
    target = str(st.get("target") or "")
    facts = {"promoted": False, "has_draft": True}
    if batch and target:
        try:
            facts = await _target_facts(work_id, target)
        except Exception as exc:  # noqa: BLE001
            unverified.append(f"target_facts:{exc}")
    promoted = bool(st_now.get("all_passed")) and facts["promoted"]
    nxt: Any = None
    stop = ""
    if batch and st_now:
        try:
            nxt, stop = _classify(
                st, st_now, spoken, promoted=promoted,
                tail_seq=_tail_seq(work_id, target),
                has_draft=facts["has_draft"])
        except Exception as exc:  # noqa: BLE001
            unverified.append(f"classify:{exc}")
    running = None
    blocked = ""
    if owner_verified:
        running = (f"{work_id}/{review_id}" in _RUNNING
                   or bool(session_facade.turn_holder(work_id, review_id)))
        blocked = _review_block_reason(work_id, review_id)
    if not st_now:
        party, cond = "review", "评审状态待核实"
        stop = "unverified"
    elif not batch:
        party, cond = "none", "无进行中批次（已收口）"
    elif blocked:
        party, cond = "review", "评审卡已停止或处于沉默状态"
        stop = blocked
    elif running:
        party, cond = "review", "评审场正在跑这一步"
    elif facts.get("draft_fp") and st.get("draft_hash") and facts["draft_fp"] != st["draft_hash"]:
        party, cond = _WAITING_MAP["awaiting_draft_sync"]
        stop = "awaiting_draft_sync"
    elif nxt is not None:
        party, cond = "runner", "有下一步待执行"
    else:
        party, cond = _WAITING_MAP.get(stop, ("none", stop or "未知"))
    # 最近一次续跑动作回执（台账事实，不问模型）
    last_action: dict[str, Any] | None = None
    try:
        from ..infra import intervention_store as _ist
        row = _ist.store().latest_action(work_id, review_id,
                                         mode="continue")
        if row:
            receipt = row.get("receipt")
            last_action = {
                "action_id": str(row.get("action_id") or ""),
                "state": str(row.get("state") or ""),
                "result": (receipt or {}).get("result")
                          if isinstance(receipt, dict) else receipt,
                "receipt": receipt,
                "updated": str(row.get("updated_iso") or ""),
                "step": row.get("step_json") or {},
            }
    except Exception as exc:  # noqa: BLE001
        unverified.append(f"last_action:{exc}")
    if (batch and nxt and owner_verified and not (running or blocked or stop) and last_action
            and last_action["state"] == "failed"
            and last_action["step"].get("batch") == batch
            and last_action["step"].get("next") == _next_tag(nxt)):
        receipt = last_action["receipt"] or {}
        party, cond = "runner", f"上次派发失败，可续跑原动作（{receipt.get('reason') or '待核实'}）"
    out.update({
        "batch": batch,
        "closed_batch": str(st.get("closed_batch") or ""),
        "type": str(st.get("type") or ""),
        "target": target,
        "is_target": (not caller) or caller == target,
        "draft": draft,
        "draft_hash": str(st.get("draft_hash") or ""),
        "draft_changed": bool(st_now.get("draft_changed")),
        "stage": ("已收口" if not batch else
                  ("收口区" if st_now.get("all_passed") else "迭代区")),
        "members": seat_rows,
        "passed": list(st_now.get("passed") or []),
        "pending": list(st_now.get("pending") or []),
        "rejected": list(st_now.get("rejected") or []),
        "all_passed": bool(st_now.get("all_passed")),
        "promoted": promoted,
        "running": running,
        "next": {**_next_view(nxt), "batch": batch, "draft": draft,
                 "next": _next_tag(nxt), "draft_fp": facts.get("draft_fp", "")},
        "stop_reason": stop,
        "waiting": {"party": party, "condition": cond},
        "dispatch_fails": int(st.get("dispatch_fails") or 0),
        "wake_nudges": int(st.get("wake_nudges") or 0),
        "asked_seq": int(st.get("asked_seq") or 0),
        "responded_seq": int(st.get("responded_seq") or 0),
        "promote_asked": bool(st.get("promote_asked")),
        "verdict": str(st.get("verdict") or ""),
        "last_action": last_action,
        "unverified": unverified,
    })
    return out


def spawn_resume_delivery(work_id: str, action_id: str) -> Any:
    """后台兑现已登记的续跑动作（不阻塞调用方）。返回 Task，测试可 await。"""
    from . import message_runner as _mr
    from ..infra import intervention_runner, intervention_store
    store = intervention_store.store()
    action = store.get_action(work_id, action_id) or {}
    rid = str(action.get("target") or "")
    key = f"{work_id}/{rid}"

    async def deliver_when_idle() -> str:
        retried_after_release = False
        while True:
            if not _review_block_reason(work_id, rid):
                with _STEP_LOCK:
                    released = _STEP_RELEASED.setdefault(key, Future()) if key in _RUNNING else None
                if released is not None:
                    await asyncio.shield(asyncio.wrap_future(released))
                    continue
                if session_facade.turn_holder(work_id, rid):
                    await session_facade.wait_for_turn_release(work_id, rid)
                    continue
            result = await intervention_runner.deliver_registered_action(work_id, action_id)
            if result != "continue:0" or retried_after_release:
                return result
            row = store.get_action(work_id, action_id) or {}
            reason = (row.get("receipt") or {}).get("reason")
            if reason not in ("already_running", "review_busy"):
                return result
            # 锁可能在失败回执落账前已释放，此时用原动作号再试一次。
            retried_after_release = not (key in _RUNNING or
                                         session_facade.turn_holder(work_id, rid))

    return _mr._spawn_tracked(deliver_when_idle(), work_id=work_id, card_id=rid)


async def request_resume(work_id: str, target_id: str, *,
                         seat: str = "", spawn: bool = True,
                         source: str = "tool") -> dict[str, Any]:
    """被评正卡的续跑/点名请求——**持久登记 → 异步转属主 → 立即回受理**。

    与 harness 自动续跑**共用去重身份**：同一实际步骤（``batch|draft|next``
    业务键）手工/自动/点名命中同一个 action_id；忙（``already_running``）
    由台账 failed 留账、恢复链重投——同一安排只兑现一次。

    ``seat`` 非空＝点名：先核成员、再按 ``_seat_step`` 算**该席**的合法步
    （不借自动挑席结果）；该席无合法步骤如实回原因，不登记。

    ``spawn=False``（测试/同步驱动）只登记不派发，调用方自行
    ``await deliver_registered_action`` 兑现。
    """
    from ..infra import intervention_runner as _ir, intervention_store as _ist
    rid = await review_store.review_card_of(work_id, target_id)
    if not rid:
        return {"ok": False, "error": "no_review_card",
                "message": "本卡没有伴生评审卡"}
    try:
        from ..infra import procs
        if _remote_review_owner(work_id, rid):
            forwarded = await procs.forward_ctl(
                work_id, rid, "/ctl/review_resume",
                {"work_id": work_id, "target_id": target_id, "seat": seat,
                 "spawn": spawn, "source": source})
            if "ok" in forwarded:
                return forwarded
            error = forwarded.get("error") or {}
            return {"ok": False, "error": "runner_unreachable",
                    "message": error.get("message") if isinstance(error, dict) else str(error)}
    except Exception as exc:  # noqa: BLE001 属主未受理时由调用方重查重试
        return {"ok": False, "error": "runner_unreachable", "message": str(exc)}
    st = await review_store.state_of(work_id, rid)
    batch = str(st.get("batch") or "")
    if not batch:
        return {"ok": True, "outcome": "no_batch",
                "review_card": rid,
                "closed_batch": str(st.get("closed_batch") or ""),
                "verdict": str(st.get("verdict") or ""),
                "message": "当前没有进行中的批次"}
    if str(st.get("target") or "") != target_id:
        return {"ok": False, "error": "not_target",
                "message": "只有被评正卡能请求续跑（本批被评的是 "
                           f"{st.get('target') or '（空）'}）"}
    blocked = _review_block_reason(work_id, rid)
    if blocked:
        return {"ok": False, "error": blocked, "review_card": rid,
                "message": "评审卡已停止或处于沉默状态"}
    st_now = await review_store.status(work_id, rid)
    spoken = set(review_store.spoken_at_draft(st))
    draft = str(int(st.get("draft") or 1))
    facts = await _target_facts(work_id, target_id)
    if st_now.get("batch") != batch or str(st_now.get("draft") or 1) != draft:
        return {"ok": False, "error": "stale_step", "review_card": rid,
                "message": "评审批次或稿次已变化，请重新查询"}
    draft_fp = str(facts.get("draft_fp") or "")
    if not draft_fp:
        return {"ok": False, "error": "material_unverified", "review_card": rid,
                "message": "草稿材料待核实，请重新查询"}
    if st.get("draft_hash") and str(st["draft_hash"]) != draft_fp:
        return {"ok": True, "outcome": "waiting", "review_card": rid,
                "reason": "awaiting_draft_sync",
                "waiting": {"party": "target", "condition": "待草稿版本落定"}}
    pin: dict[str, Any] | None = None
    if seat:
        nxt, seat_reason = _seat_step(seat, st, st_now, spoken)
        retry_missing_vote = seat_reason == "awaiting_vote"
        if retry_missing_vote:
            nxt, seat_reason = _seat_step(seat, st, st_now, spoken,
                                          retry_missing_vote=True)
        if nxt is None:
            return {"ok": True, "outcome": "seat_no_step",
                    "review_card": rid, "seat": seat,
                    "reason": seat_reason,
                    "vote": (st_now.get("votes") or {}).get(seat) or {},
                    "message": f"席位 {seat} 当前没有可执行步骤：{seat_reason}"}
        pin = {"seat": seat, "kind": str(nxt[2] if nxt[0] == "seat"
                                        else nxt[0])}
        if retry_missing_vote:
            pin["retry_missing_vote"] = True
    else:
        promoted = bool(st_now.get("all_passed")) and facts["promoted"]
        nxt, stop = _classify(st, st_now, spoken, promoted=promoted,
                              tail_seq=_tail_seq(work_id, target_id),
                              has_draft=facts["has_draft"])
        if nxt is None:
            party, cond = _WAITING_MAP.get(stop, ("none", stop or "未知"))
            return {"ok": True, "outcome": "waiting",
                    "review_card": rid, "reason": stop,
                    "waiting": {"party": party, "condition": cond},
                    "message": f"当前没有可执行步骤：{cond}"}
    tag = _next_tag(nxt)
    expect: dict[str, Any] = {"batch": batch, "draft": draft,
                              "draft_fp": draft_fp, "next": tag}
    if pin:
        expect["pin"] = pin
    biz_key = f"{batch}|{draft}|{tag}"
    st_ = _ist.store()
    fire_key = f"resume_review|{target_id}|{tag}|{uuid.uuid4().hex[:8]}"
    seat_txt = f"点名 {pin['seat']}" if pin else "续跑"
    aid, created = st_.record_action(
        work_id, policy_id="review_step_not_finished",
        scope_key=f"card:{rid}", fire_key=fire_key,
        target=rid, mode="continue", wake=True,
        message=f"（续接）被评卡{seat_txt}——评审继续跑下一步。",
        biz_key=biz_key, step=expect)
    if not aid or (not created and not st_.prepare_review_action(work_id, aid, expect)):
        return {"ok": False, "error": "ledger_failed",
                "message": "动作登记失败（台账写不进），请重试或人工核查"}
    row = st_.get_action(work_id, aid) or {}
    state = str(row.get("state") or "")
    out: dict[str, Any] = {"ok": True, "review_card": rid,
                           "action_id": aid, "step": row.get("step_json") or expect,
                           "biz_key": biz_key, "dedup": not created,
                           "state": state}
    if state == "delivered":
        out["outcome"] = "already_done"
        out["message"] = "这一步的续跑已受理过（台账同一步身份），进展见回执"
        return out
    if state == "sending" and float(row["updated_ts"]) >= time.time() - _ir.CLAIM_STALE_S:
        out["outcome"] = "in_flight"
        out["message"] = "同一步已有动作在投递中（共享去重身份）"
        return out
    try:
        event_stream.emit(work_id, rid, "review_resume", {
            "batch": batch, "draft": int(draft), "next": tag,
            "seat": str((pin or {}).get("seat") or ""),
            "by": target_id, "source": source,
            "action_id": aid, "dedup": not created})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    if spawn:
        spawn_resume_delivery(work_id, aid)
    out["outcome"] = "queued" if created else "requeued"
    out["message"] = ("已登记并转交评审属主兑现（同一实际步骤共享去重身份）；"
                      "结果以动作回执为准" if created else
                      "同一步已有登记在账，重新排队兑现")
    return out


async def report_target_turn_end(work_id: str, child_id: str,
                                 outcome: str = "ok") -> bool:
    """**正卡回合结束** ⇒ 若它还是某批次的正卡，往**评审卡**如实发一条
    ``review_target_turn_end``——负载带**完整事实**：``outcome``（怎么结束的）
    ＋ ``pending_ask``（ask 还挂没挂）。本函数只报事实，**不做判断、不起轮**：
    要不要点火由 harness 策略的 where 挑（「失败≠done」若埋在这里，事件层
    就把 ok/error 归并成一种假账——细粒度事实归并正是 harness 收不全的
    根因，字段必须全量交给策略选）。
    """
    try:
        rid = await review_store.review_card_of(work_id, child_id)
        if not rid:
            return False
        st = await review_store.state_of(work_id, rid)
        if not st.get("batch"):
            return False
        if str(st.get("target") or "") != child_id:
            return False             # 场卡换过目标 ⇒ 这卡不再是本批正卡
        # [草稿版本轮粒度化 2026-11-14 · 用户令] 版本在回合收口这一刻才推：
        # 写钩不再随写 bump——轮内 N 次编辑＝一版；指纹变了才 +1。
        # 版本落定早于事件发射，harness 拿到的 draft 就是本轮定稿版。
        try:
            if await review_store.sync_draft(work_id, rid):
                st = await review_store.state_of(work_id, rid)
        except Exception as exc:  # noqa: BLE001 推版失败不阻断：水位比对照样报
            logger.warning("[review] 轮末推版失败 rid=%s: %s", rid, exc)
        pending = bool(st.get("promote_asked")) or \
            int(st.get("asked_seq") or 0) > int(st.get("responded_seq") or 0)
        # [turn_end 激活 2026-11-04 · 用户令「修改草稿的那一轮结束再激活评审卡」]
        # progress=刚结束这轮里正卡有没有实质推进：草稿版本涨了（改过稿）
        # 或 responded_seq 涨了（调过 submit_revision）。点火移到这个字段上——
        # 没推进的回合结束不该去叫评审场。
        cur_draft = int(st.get("draft") or 0)
        cur_resp = int(st.get("responded_seq") or 0)
        progress = cur_draft > int(st.get("turn_end_draft") or 0) or \
            cur_resp > int(st.get("turn_end_responded") or 0)
        event_stream.emit(work_id, rid, "review_target_turn_end", {
            "batch": str(st.get("batch") or ""),
            "target": child_id,
            "pending_ask": pending,
            "progress": progress,
            "outcome": outcome,
            # [轮末定版] 本轮落定的草稿版——点火后席位评的就是它。
            "draft": cur_draft,
        })
        # 回写水位——下一轮再比。写失败不阻断：无非是 progress 多记一轮旧账。
        try:
            st2 = dict(st)
            st2["turn_end_draft"] = cur_draft
            st2["turn_end_responded"] = cur_resp

            def _fn(conn: Any) -> None:
                conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                             (json.dumps(st2, ensure_ascii=False), rid))
            await card_db.run_write(work_id, _fn)
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as exc:  # noqa: BLE001 上报失败不影响正卡回合收口
        logger.warning("[review] 回合结束上报失败 child=%s: %s", child_id, exc)
        return False


# ---------------------------------------------------------------- 触发
def _pending_target_ask(st: dict[str, Any]) -> tuple[str, Any] | None:
    if st.get("promote_asked"):
        return "promote", st.get("promote_asked_seq")
    if int(st.get("asked_seq") or 0) > int(st.get("responded_seq") or 0):
        return "revise", st.get("asked_at_seq")
    return None


def is_batch_dispatch(source: dict[str, Any] | None) -> bool:
    if not source or source.get("kind") != "review_dispatch":
        return False
    step = str(source.get("step") or "")
    return step in ("promote", "revise") or step.startswith("wake_")


async def dispatch_block_reason(work_id: str, target: str,
                                source: dict[str, Any] | None, *,
                                phase: str) -> str:
    """起轮和插话消费共用：读不出则待核实，确证过期则只留历史。"""
    if not source or not is_batch_dispatch(source):
        return ""
    review_id = str(source.get("from") or "")
    batch = str(source.get("batch") or "")
    draft = source.get("draft")
    reason = "review_dispatch_unverified"
    if review_id and batch and draft not in (None, ""):
        for attempt in range(2):
            try:
                current = await review_store.state_of(work_id, review_id, strict=True)
                if (current.get("batch") == batch
                        and current.get("target") == target
                        and int(current.get("draft") or 1) == int(draft)):
                    return ""
                reason = "review_dispatch_stale"
                break
            except Exception as exc:  # noqa: BLE001 核验读失败不得起轮
                if attempt == 0:
                    await asyncio.sleep(0.05)
                else:
                    logger.warning("[review] 无法核验指令 review=%s target=%s: %s",
                                   review_id, target, exc)
    session_ops.ops_log(reason, work_id, target,
                        {"from": review_id, "batch": batch, "draft": draft,
                         "step": source.get("step"), "phase": phase})
    return reason


async def activation_wake_is_current(work_id: str, target: str,
                                     source: dict[str, Any]) -> bool:
    st = await review_store.state_of(work_id, str(source.get("from") or ""))
    ask = _pending_target_ask(st)
    return bool(st.get("batch") and st["batch"] == source.get("batch")
                and st.get("target") == target
                and source.get("ask_seq") is not None
                and ask == (source.get("step"), source["ask_seq"]))


async def resume_activated_target(work_id: str, target: str) -> bool:
    """解除缄默时接收当前批次已落账的请求。"""
    from . import message_runner

    if _wake_refusal(work_id, target):
        return False
    rid = await review_store.review_card_of(work_id, target)
    if not rid:
        return False
    st = await review_store.state_of(work_id, rid)
    ask = _pending_target_ask(st)
    if not st.get("batch") or st.get("target") != target or ask is None:
        return False
    kind, seq = ask
    if seq is None:
        return False
    return await message_runner.deliver_now(
        work_id, target, text=_wake_kickoff(kind, "unread", st),
        source={"kind": "review_activation", "from": rid, "batch": st["batch"],
                "step": kind, "ask_seq": seq})


async def resume_after_vote(work_id: str, review_id: str, batch: str) -> None:
    """场外新票落账后，唤醒曾因缺票等待的评审。场内投票由本步收口接力。"""
    if f"{work_id}/{review_id}" in _RUNNING:
        return
    st = await review_store.state_of(work_id, review_id)
    if not batch or st.get("batch") != batch:
        return
    if not any("vote_nudge_basis" in seat for seat in (st.get("seats") or {}).values()):
        return
    status = await review_store.status(work_id, review_id)
    if status.get("batch") != batch:
        return
    if not status.get("all_passed") or status.get("type") == "requirements_align":
        spawn_round(work_id, review_id)


async def rerun_if_pending(work_id: str, review_id: str) -> bool:
    """版本已被别处推进（撤销通过等）后，起一步重评。"""
    try:
        st = await review_store.status(work_id, review_id)
        if not st.get("batch") or st.get("all_passed"):
            return False
        spawn_round(work_id, review_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 重评起轮失败 review=%s: %s", review_id, exc)
        return False


def spawn_round(work_id: str, review_id: str) -> Any:
    """后台**跑一步**（不阻塞调用方）。返回 Task，测试可 await。

    [2026-10-29 用户裁定「用 harness 接管评审卡的流程存续」]
    只跑 `run_once` 一步：它发 `review_round_done`，由策略 continue 决定
    要不要再叫下一次。流程存续不再依赖某个活着的 task。
    """
    from . import message_runner as _mr
    return _mr._spawn_tracked(run_once(work_id, review_id))


# ---------------------------------------------------------------- 身份小件
def _review_charter(role_id: str) -> str:
    """读**评审职责书**（`config/review_charters.json`，按角色 id 存）。

    [2026-10-29 用户令]「它的日常职责和它的评审职责，是两个职责，评审职责是
    专门写的」⇒ 评审席上读的必须是这一份，不是 `function_profiles.charter`。
    没写到的角色返回空串，调用方回落到日常职责。
    """
    try:
        data = json.loads(REVIEW_CHARTERS.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 读不到＝没写，回落
        logger.warning("[review] 读评审职责书失败: %s", exc)
        return ""
    row = data.get(role_id) or {}
    return str(row.get("text") or "") if isinstance(row, dict) else ""


async def _name_of(work_id: str, role_id: str) -> str:
    try:
        prof = function_store.get_profile(work_id, role_id) or {}
        return str(prof.get("function_name") or role_id)
    except Exception:  # noqa: BLE001
        return role_id


async def _draft_of(work_id: str, card_id: str) -> str:
    """读子卡当前草稿（`requirements_draft`）。读不到返回空串。"""
    try:
        meta = (await card_store.read_card(work_id, card_id)).get("meta") or {}
        return str(meta.get("requirements_draft") or "")
    except Exception:  # noqa: BLE001
        return ""
