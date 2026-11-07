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
import json
import logging
import os
from pathlib import Path
from typing import Any

from ..storage import card_db, card_store, function_store, review_store, session_facade
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

_RUNNING: set[str] = set()


# ---------------------------------------------------------------- 一步＝一次唤醒
async def run_once(work_id: str, review_id: str, *,
                   hint: str = "") -> dict[str, Any]:
    """**跑一步**：机械读评审状态，做下一个动作（叫一席／派活／停）。

    ``hint``＝harness ``continue`` 送来的"为什么叫你"（一次性、不落历史），
    拼进这一步的 mandate 尾部。

    与旧 `run_round` 的差别：这里**没有循环**。每步结束落
    `review_round_done`（带 seat/voted/draft_ver 等事实），要不要续由
    策略看事件决定——"还有活"这个事实代码只如实上报。
    """
    if os.environ.get(ENV_NO_AUTO) == "1":
        return {"continued": False, "reason": "no_auto"}
    key = f"{work_id}/{review_id}"
    if key in _RUNNING:
        return {"continued": False, "reason": "already_running"}
    _RUNNING.add(key)
    try:
        r = await _run_step(work_id, review_id, hint=hint)
    except Exception as exc:  # noqa: BLE001 步内异常＝一步的事实，如实报不续
        err = f"{type(exc).__name__}: {exc}"
        logger.warning("[review] 步内异常 review=%s: %s", review_id, err)
        r = {"continued": False, "reason": "step_error", "error": err,
             "_done": {"reason": "step_error", "continued": False,
                       "error": err}}
    finally:
        _RUNNING.discard(key)
    # **先放锁再报事实**——round_done 点火永远找不到占着的 _RUNNING。
    if isinstance(r, dict) and isinstance(r.get("_done"), dict):
        await _report(work_id, review_id, r.pop("_done"))
    return r


async def _run_step(work_id: str, review_id: str, *,
                    hint: str = "") -> dict[str, Any]:
    """`run_once` 的实体：**只算，不循环**。

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
    if not st.get("batch"):
        return {"continued": False, "reason": "no_batch"}
    batch = str(st.get("batch") or "")
    st_now = await review_store.status(work_id, review_id)
    spoken = set(await review_store.spoken_round(work_id, review_id))
    target = str(st.get("target") or "")
    facts = await _target_facts(work_id, target)
    promoted = bool(st_now.get("all_passed")) and facts["promoted"]
    tail_seq = _tail_seq(work_id, target)

    # [harness 驱动 · 2026-11-01] 挑谁/挑什么动作是评审卡内部的事（成员序）；
    # harness 只判"该不该点火"（has_next）。分类与 has_next 共用同一个
    # `_classify`——事件里上报的"还有活"和这里挑的"下一个活"同源。
    action, stop_reason = _classify(st, st_now, spoken, promoted=promoted,
                                    tail_seq=tail_seq,
                                    has_draft=facts["has_draft"])

    if action is None:
        return {"continued": False, "reason": stop_reason,
                "_done": {"reason": stop_reason, "continued": False}}

    if action[0] == "seat":
        _, seat, kind = action
        r = await _seat_turn(work_id, review_id, seat, st,
                             kind=kind, hint=hint)
        return _after_seat(work_id, review_id, batch, seat, r, st_now)

    if action[0] == "wake":
        # 纯唤醒：信已在正卡会话里，只再起一轮。正卡在跑＝信已"送达"
        # （它收口自查会接上），不算失败；空闲才叫 deliver_now。
        # ``why`` 是 classify 的诊断：唤醒文按状况点破（"信没读到" vs
        # "你上一轮读过了但没调"）——机械模板，不做内容判断。
        _, kind, why = action
        target = str(st.get("target") or "")
        ok = False
        busy = False
        try:
            busy = bool(session_facade.turn_holder(work_id, target))
        except Exception:  # noqa: BLE001 读不出按空闲处理
            pass
        if busy:
            ok = True
        else:
            try:
                from . import message_runner as _mr
                ok = await _mr.deliver_now(
                    work_id, target,
                    text=_wake_kickoff(kind, why, st),
                    source={"kind": "review_dispatch", "from": review_id,
                            "batch": batch, "step": f"wake_{why}"})
            except Exception as exc:  # noqa: BLE001 唤醒异常＝一次没落
                logger.warning("[review] 补唤醒异常 kind=%s review=%s: %s",
                               kind, review_id, exc)
                ok = False
        if ok:
            try:
                await review_store.clear_dispatch_fails(work_id, review_id)
            except Exception:  # noqa: BLE001
                pass
            if not busy:
                # 唤醒发出去＝记一次 nudge 并把 ask 锚重锚到此刻会话尾：
                # 下一次补唤醒只在「这之后又起过回合」时才发（一次唤醒对应
                # 一段新活动；anchor 不重锚会把同一段静默重复炸）。
                try:
                    await review_store.mark_wake_nudge(
                        work_id, review_id, kind,
                        at_seq=_tail_seq(work_id, target))
                except Exception:  # noqa: BLE001
                    pass
            return {"continued": False,
                    "reason": "awaiting_busy" if busy else "wake_delivered",
                    "_done": {"reason": "awaiting_busy" if busy
                              else "wake_delivered", "continued": False}}
        try:
            await review_store.mark_dispatch_failed(work_id, review_id)
        except Exception:  # noqa: BLE001
            pass
        return {"continued": False, "reason": "wake_failed",
                "error": "dispatch_failed",
                "_done": {"reason": "wake_failed", "continued": False,
                          "error": "dispatch_failed"}}

    # action[0] == "dispatch"
    _, kind = action
    try:
        ok = await _dispatch_child(work_id, review_id, st, kind=kind)
    except Exception as exc:  # noqa: BLE001 派活异常＝一次没落，照计数闸走
        logger.warning("[review] 派活异常 kind=%s review=%s: %s",
                       kind, review_id, exc)
        ok = False
    if ok:
        return {"continued": False, "reason": f"dispatch_{kind}",
                "_done": {"reason": f"dispatch_{kind}", "continued": False}}
    # 派活没落（消息没进正卡会话／正卡在跑没唤起）：落一次失败计数，
    # has_next 仍会让它再醒——`dispatch_fails` 封顶后 classify 收口到站。
    try:
        await review_store.mark_dispatch_failed(work_id, review_id)
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
    out = {"continued": not failed, "reason": "seat_done",
           "seat": seat, "kind": r.get("kind"), "voted": bool(r.get("voted")),
           "vote": r.get("vote"), "error": str(r.get("error") or "")}
    if r.get("_done") is not None:      # _seat_turn 的 round_done 载荷透传
        out["_done"] = r["_done"]
    return out


#: 派活连续失败封顶：超过就不再自动重试（持久 IO 故障不该烧循环），
#: 等新事件（发言/投票/回应/开批）自然再接。
_DISPATCH_FAIL_CAP = 6

#: 「ask 被消费但没成果」的补唤醒封顶：每次唤醒都烧正卡一个真回合，
#: 几次都叫不动就到站等外部事件（用户/开批/resume），不无限空烧。
_WAKE_NUDGE_CAP = 3


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
    会不会返回一个动作。
    """
    members = [str(x) for x in (st_now.get("members") or [])]
    votes = dict(st_now.get("votes") or {})
    seats = dict(st.get("seats") or {})
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
            return ("seat", stale[0], "confirm"), ""
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
    for m in members:
        verdict = str((votes.get(m) or {}).get("verdict") or "")
        if verdict == "approve":
            continue                    # 通过的席位不再被叫（drift 只在收口区）
        if m not in spoken:
            return ("seat", m, "speak"), ""
        if not verdict:
            return ("seat", m, "vote_nudge"), ""
        if verdict == "reject":
            _raw_seen = (seats.get(m) or {}).get("response_seen")
            # response_seen=0 是有效水位（被叫时还没有任何回应），不能用 `or`
            # ——0 会被当"没值"吞掉 ⇒ 否决席被误判"有新回应"无限重评。
            seen = -1 if _raw_seen is None else int(_raw_seen)
            if responded > seen:
                return ("seat", m, "re_eval"), ""
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
                        return ("dispatch", "revise"), ""
                    return None, "dispatch_capped"
            # 已请过正卡回应还没交回：先判消费（读了没交回）再看唤醒落没落。
            if _consumed(st.get("asked_at_seq")):
                if wake_nudges < _WAKE_NUDGE_CAP:
                    return ("wake", "revise", "consumed"), ""
            elif 0 < dispatch_fails < _DISPATCH_FAIL_CAP:
                return ("wake", "revise", "unread"), ""
            return None, "awaiting_target"
        if _dispatchable():
            return ("dispatch", "revise"), ""
        return None, "dispatch_capped"
    # 兜底：pending 里没人可做（理论上到不了——pending 必属上述三类之一）。
    return None, "stalled"


async def _target_facts(work_id: str, target: str) -> dict[str, bool]:
    """收口区要的两个正卡事实（一次读卡）：

    - ``promoted``：正稿写了没有（``requirements_state == "approved"``）；
    - ``has_draft``：草稿还在不在——它是 ``promote_draft`` 的唯一原料。
      读不出按「有草稿」处理（保持旧判据偏向：宁派勿停）。
    """
    try:
        card = await card_store.read_card(work_id, target)
        meta = card.get("meta") or {}
        return {
            "promoted": str(meta.get("requirements_state") or "") == "approved",
            "has_draft": bool(str(meta.get("requirements_draft") or "").strip()),
        }
    except Exception:  # noqa: BLE001
        return {"promoted": False, "has_draft": True}


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
    await review_store.mark_seen(work_id, review_id, seat)
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
    mandate = await _seat_mandate(work_id, review_id, seat, st, kind)
    if str(hint or "").strip():
        mandate = f"{mandate}\n\n【系统续接】{str(hint).strip()}"

    err = ""
    try:
        result = await routes._run_message(
            work_id, review_id, mandate,
            append_user=False,
            source={"kind": "review_step", "seat": seat,
                    "batch": batch, "step": kind},
            act_as=seat, act_meta=act_meta, ctx_card=target,
            tool_allow=_seat_toolface(),
            prior_extra=await _prior_blocks(work_id, review_id))
    except Exception as exc:  # noqa: BLE001 模型挂/锁冲突也是一步的事实
        result = {}
        err = f"{type(exc).__name__}: {exc}"
        logger.warning("[review] 席位回合失败 seat=%s review=%s: %s",
                       seat, review_id, err)

    # [venue 批次守卫] 回合跑到一半批次换了（open_batch 在**同协程**里开的，
    # cancel_turn 停不掉自己）：这一轮回合写进场会话的每一块都是**旧批残块**
    # ——场是新的，再清一次；它在新批次里误落的票也要撤掉（它压根没见过
    # 新稿）；死批次的表决/发言事实同样不发给 harness。
    st_now0 = await review_store.state_of(work_id, review_id)
    if str(st_now0.get("batch") or "") != batch:
        logger.warning("[review] 批次在回合中更换 review=%s seat=%s ⇒ 旧批残块清场",
                       review_id, seat)
        try:
            await review_store.revoke(work_id, review_id, seat)
        except Exception:  # noqa: BLE001 撤不掉不阻断清场
            pass
        await review_store._clear_review_session(work_id, review_id)
        return {"seat": seat, "kind": kind, "voted": False, "vote": "",
                "error": "stale_batch"}

    # 回合跑成才记"说过话"（挑席判据：说过还没投 → vote_nudge）；
    # 回合没跑成（锁冲突/模型挂）不标，下一步照样按 speak 叫。
    if not err:
        await review_store.mark_spoken(work_id, review_id, seat)
    # 回合结束读回事实：这一席表态了没有、投了什么（harness 续接的判据）。
    st2 = await review_store.status(work_id, review_id)
    vote = str((st2.get("votes") or {}).get(seat, {}).get("verdict") or "")
    if not err and not isinstance(result, dict):
        # 锁冲突等早退路径返回的是 JSONResponse——不是 dict ⇒ 回合根本没跑成。
        # 这是机械失败事实：不续跑（续了也是对着锁空烧），如实报。
        err = getattr(result, "status_code", None) and f"turn_http_{result.status_code}" \
              or "turn_non_dict_result"
    if not err and isinstance(result, dict) and result.get("error"):
        err = str((result.get("error") or {}).get("message")
                  or result.get("error") or "")
    try:
        event_stream.emit(work_id, review_id, "review_spoke", {
            "batch": batch, "role": seat, "name": name, "kind": kind,
            "voted": bool(vote), "vote": vote,
            "draft": int(st2.get("draft") or 1), "error": err})
    except Exception:  # noqa: BLE001 事件失败不影响评审
        pass
    # round_done 不在此发：由 `run_once` 在 _RUNNING 释放后统一报——
    # 策略点火永远不会撞在这把锁上（撞了必丢唤醒，实跑踩过）。
    return {"seat": seat, "kind": kind, "voted": bool(vote),
            "vote": vote, "error": err,
            "_done": {"reason": f"seat:{kind}", "seat": seat,
                      "voted": bool(vote), "continued": not err,
                      "error": err}}


def _seat_toolface() -> set[str]:
    """场授面＝**全部只读件** ∪ {approve, reject}。

    「席位自己的能力上限」已由 `build_tool_schemas(act_as)` 收窄——这个集合
    只是场的**许可上限**：在场内，读+表态；写产物/通信/建卡一律不在场。
    只读件按 `ToolDef.readonly` 现算，不写死名单（新注册的只读工具自动在场）。
    """
    from . import card_tools  # 延迟 import：card_tools 体量大且反向引用

    readonly = {n for n, d in card_tools.TOOL_REGISTRY.items()
                if getattr(d, "readonly", False)}
    return set(readonly) | set(VENUE_GRANT)


async def _seat_mandate(work_id: str, review_id: str, seat: str,
                        st: dict[str, Any], kind: str) -> str:
    """席位差事：**当轮任务口**（压在 user_prompt 位）。

    材料区只留**装配层给不了的**：brief（评审侧数据）＋草稿本体（按用户令
    不进任何上下文的评审对象）。子卡使命/现状/正稿由 ctx_card 的 bundle
    供给，不再复抄；confirm 另走快照 diff（更小）。
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

    if kind == "confirm":
        # [drift 收窄口径] 只评增量：你批的那份（快照）→ 现稿的条目级 diff。
        st_now = await review_store.status(work_id, review_id)
        snap = str(((st_now.get("votes") or {}).get(seat) or {})
                   .get("snapshot") or "")
        cur = await _draft_of(work_id, target)
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
                          st: dict[str, Any], *, kind: str) -> bool:
    """派活＝**给正卡发一条正经消息**，它自己开普通回合干活。

    消息落**正卡自己的会话**（`actor=card:<评审卡>`，页面看得见来路），
    `extra.batch` 带批次号可审计；唤醒走既有 `deliver_now`——正卡在跑就
    不打断（它本轮收口的自查续轮会接上，信不丢）。

    ``kind``：``revise``（请它按意见改稿，交回 submit_revision）／
              ``promote``（请它调 promote_draft 定稿）。
    已派的活由 `mark_asked`/`promote_asked` 记成机械事实，不重复派。
    """
    from . import message_runner as _mr

    batch = str(st.get("batch") or "")
    target = str(st.get("target") or "")
    if not target:
        return False
    draft_ver = int(st.get("draft") or 1)
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
        await review_store.mark_promote_asked(work_id, review_id,
                                              at_seq=rec.get("seq"))
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
        await review_store.mark_asked(work_id, review_id,
                                      at_seq=rec.get("seq"))
    # 落成 ⇒ 派活失败计数清零（一个批次的连续失败才累积封顶）。
    try:
        await review_store.clear_dispatch_fails(work_id, review_id)
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
        source={"kind": "review_dispatch", "from": review_id,
                "batch": batch, "step": kind})


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

    `has_next`＝"再叫一步有没有事做"：拿**刚落盘的**状态再过一遍 `_classify`，
    它会不会返回动作就是事实本身（与 `_run_step` 同源，不是推断）。
    压制规则：这一步刚出错且下一个动作又是席位回合 ⇒ false——模型刚死就
    立刻重叫同一席只是空烧，等新事件（投票/回应/发言）自然再接；下一个动作
    是派活（机械件）则照常 true——seat 回合崩了但票已齐，派活不依赖模型。
    """
    try:
        st = await review_store.state_of(work_id, review_id)
        st_now = await review_store.status(work_id, review_id)
        spoken = set(await review_store.spoken_round(work_id, review_id))
        batch = str(st.get("batch") or "")
        if not batch:
            return                       # 批次没了没什么可报的
        _facts = await _target_facts(work_id, str(st.get("target") or ""))
        promoted = bool(st_now.get("all_passed")) and _facts["promoted"]
        nxt, _stop = _classify(st, st_now, spoken, promoted=promoted,
                               tail_seq=_tail_seq(
                                   work_id, str(st.get("target") or "")),
                               has_draft=_facts["has_draft"])
        err = str(done.get("error") or "")
        reason = str(done.get("reason") or "")
        has_next = (
            nxt is not None
            and reason != "step_error"          # 步内异常不自动续（防同类故障空转）
            and not (err and nxt[0] == "seat")  # 回合刚崩不立刻重叫同一席
        )
        event_stream.emit(work_id, review_id, "review_round_done", {
            "batch": batch,
            "draft": int(st_now.get("draft") or 1),
            "all_passed": bool(st_now.get("all_passed")),
            "passed": list(st_now.get("passed") or []),
            "pending": list(st_now.get("pending") or []),
            "seat": str(done.get("seat") or ""),
            "voted": bool(done.get("voted")),
            "reason": str(done.get("reason") or ""),
            "continued": bool(done.get("continued")),
            "has_next": bool(has_next),
            "error": err,
        })
    except Exception:  # noqa: BLE001 事件失败不影响主流程
        pass


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
async def maybe_rerun(work_id: str, child_id: str) -> bool:
    """**子卡卡面变了** ⇒ 只推进草稿版本，**不起轮**。

    [2026-10-30 用户令「谁让你做这么多独立的起轮口子」]
    写卡不是起轮的时机：正在跑的那一步自己会收尾并发事件接力。
    这里只做一件事：把草稿版本推进（下一步据此知道"第几版"）。
    """
    try:
        rid = await review_store.review_card_of(work_id, child_id)
        if not rid:
            return False
        return await review_store.sync_draft(work_id, rid)
    except Exception as exc:  # noqa: BLE001 触发失败不影响写卡
        logger.warning("[review] 重评触发失败 child=%s: %s", child_id, exc)
        return False


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
