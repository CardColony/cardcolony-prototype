# -*- coding: utf-8 -*-
"""干预生命周期（迭代五 · S1 落地）。policy_runner 的接线伙伴：

    feed():   命中 → ``record_fired`` 先把动作/事项**同步落账**，再 _spawn 投递
              → ``on_fact`` 让已开事项跟着新事实复核解除。
    resume(): 重启 → 复核全部 open 事项（已解决的带证据关账）→ 重投 pending/
              failed 动作（投前再核：已送达过、事项已关、目标自解的都跳过）。

守的三件事（docs/harness-智能干预迭代方案-v1-20261031.md）：

1. **动作先于投递落账**——命中即清小本子，动作行不先写库，进程一断就只剩
   「本子清了、事没人盯」。落账在 dispatch 同一调用栈内同步完成。
2. **解除要有结果依据**——事项的关闭条件由 ``_CHECKERS`` 按业务权威状态
   裁定（定稿＝批次收口+正稿 verdict），不靠"消息已送达"自欺。
3. **重投放前必复核**——重启不复活已解决的问题；已送达过的动作靠事实库
   里 ``harness_message.action_id`` 查重，不重复进会话。

零阻塞：台账读写失败只告警不拖垮回合；判定不了就保持现状（宁可多留）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any, Optional

from . import intervention_store

logger = logging.getLogger("infra.intervention")


# ================================================================ 检查器
def _review_state(work_id: str, review_id: str) -> dict[str, Any]:
    """同步读评审卡批次状态（review_state JSON 列）。"""
    from ..storage import card_db

    def _fn(conn: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT review_state FROM cards WHERE card_id=?",
            (review_id,)).fetchone()
        raw = (row["review_state"] if row else "") or ""
        try:
            d = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            d = {}
        return d if isinstance(d, dict) else {}

    try:
        return dict(card_db.read_sync(work_id, _fn, {}) or {})
    except Exception:  # noqa: BLE001 读不到 ⇒ 空（判不了 ⇒ 不解除）
        return {}


def _check_review_promote(work_id: str, matter: dict[str, Any]) -> tuple[str, dict]:
    """定稿事项裁定：**resolved / cancelled / open**。

    - 本批次的 ``batch`` 还在 ⇒ 仍未收口 ⇒ open；
    - 批次已不在且有 ``verdict`` ⇒ 收口完成（收口＝正稿已写，机械绑定）⇒ resolved；
    - 批次被换/清但没 verdict ⇒ 批没了事没成 ⇒ cancelled。
    """
    objects = matter.get("objects") or {}
    rid = str(objects.get("review_card") or "")
    if not rid:
        return "open", {"reason": "缺评审卡对象"}
    st = _review_state(work_id, rid)
    batch_now = str(st.get("batch") or "")
    verdict = str(st.get("verdict") or "")
    if batch_now and batch_now == str(matter.get("matter_key") or ""):
        return "open", {"batch": batch_now}
    if verdict:
        return "resolved", {"closed_batch": str(st.get("closed_batch") or ""),
                            "verdict": verdict}
    return "cancelled", {"reason": f"批次已不在（now={batch_now or '空'}）且无正稿"}


# [超限提醒合并 2026-11-20 · 成本优化批五·开关 COST_WAKE_COALESCE]
# 同一连续超限期的多次超限编辑合并到**同一事项/动作**：首次立案＋
# 起轮一次；后续编辑只刷新台账事实与待投递材料；回阈值内 _check
# 按**当前卡面**关事项（待受理动作随事项销账）；再超限起新事项。
_OVERSIZE_PID = "status_block_oversize_reminder"
_OVERSIZE_LIMIT = 10000


def _coalesce_on() -> bool:
    return str(os.environ.get("COST_WAKE_COALESCE") or "").strip() == "1"


def _status_len(work_id: str, card_id: str) -> Optional[int]:
    """同步读卡的当前 status_block 长度（读不到 ⇒ None，判不了保持 open）。"""
    if not card_id:
        return None
    from ..storage import card_db

    def _fn(conn: Any) -> Optional[int]:
        row = conn.execute(
            "SELECT status_block FROM cards WHERE card_id=?",
            (card_id,)).fetchone()
        return len(row["status_block"] or "") if row else None

    try:
        out = card_db.read_sync(work_id, _fn, None)
        return int(out) if out is not None else None
    except Exception:  # noqa: BLE001 判不了 ⇒ None（_check 保持 open）
        return None


def _oversize_card(matter: dict[str, Any]) -> str:
    """事项的目标卡：投递目标优先，回退 scope 的卡位。"""
    objects = matter.get("objects") or {}
    targets = [str(t) for t in (objects.get("deliver_targets") or []) if t]
    if targets:
        return targets[0]
    for cand in (objects.get("target"), objects.get("review_card")):
        if str(cand or ""):
            return str(cand)
    scope = str(matter.get("scope_key") or "")
    return scope.split(":", 1)[-1] if scope.startswith("card:") else ""


def _check_status_oversize(work_id: str, matter: dict[str, Any]) -> tuple[str, dict]:
    """连续超限事项裁定：卡面现状说了算——回阈值内 ⇒ resolved。

    用**当前 status_block 长度**而非事件携带的 new_len：晚到/回放
    的旧超限事实不能反转已恢复的现状（任务书批五「无法证明顺序时
    读当前卡面」）。读不到 ⇒ open（宁可多留）。
    """
    card = _oversize_card(matter)
    if not card:
        return "open", {"reason": "缺目标卡"}
    n = _status_len(work_id, card)
    if n is None:
        return "open", {"reason": "现状块读不到", "card": card}
    if n >= _OVERSIZE_LIMIT:
        return "open", {"card": card, "current_len": n}
    return "resolved", {"card": card, "current_len": n,
                        "recovered_below": _OVERSIZE_LIMIT}


_CHECKERS = {
    "review_promote": _check_review_promote,
    "status_oversize": _check_status_oversize,
}


def _check(work_id: str, matter: dict[str, Any]) -> tuple[str, dict]:
    fn = _CHECKERS.get(str(matter.get("kind") or ""))
    if fn is None:
        return "open", {"reason": f"未知 kind={matter.get('kind')}"}
    try:
        return fn(work_id, matter)
    except Exception as exc:  # noqa: BLE001 判不了 ⇒ 保持 open（宁可多留）
        logger.warning("[intervention] 解除判定异常 %s: %s",
                       matter.get("matter_id"), exc)
        intervention_store.store().record_error(
            work_id, stage="check", kind="check_exc",
            policy_id=str(matter.get("policy_id") or ""),
            matter_id=str(matter.get("matter_id") or ""),
            detail={"matter_kind": str(matter.get("kind") or ""),
                    "error": str(exc)})
        return "open", {"reason": f"check_exc:{exc}"}


# ================================================================ 记录（feed 同步段）
#: 实时投递/恢复共用的在途租约：``sending`` 超过此秒数视为发送者已
#: 退出/失联可重领（continue 投递含评审步，可能跑数分钟——给足）。
#: 接管判据是**租约过期**，不是进程 boot 时刻——新 runner 启动抢不走
#: 仍在宽限内的在途投递（批次A第三轮返工）。
CLAIM_STALE_S = 1800.0
#: [在途续约 2026-11-20 · 批次A第四轮返工] 活发送者的心跳间隔——
#: 投递等待期间周期性 touch_claim，让 ``sending`` 行保持新鲜；
#: resume 的租约过期判据只在「发送者真的退出（心跳停了）」后成立。
CLAIM_HEARTBEAT_S = 20.0


def _scope_str(scope: Any) -> str:
    parts = [str(x) for x in (scope or [])]
    if not parts:
        return "work:*"
    if len(parts) == 1:
        return f"card:{parts[0]}"
    return "rel:" + ">".join(parts)


def _resolve_ref(key: str, trigger: dict[str, Any]) -> str:
    """``event:<路径>`` 取触发事实字段，否则按字面值。"""
    key = str(key or "")
    if key.startswith("event:"):
        from . import harness_cond
        return str(harness_cond.get_path(trigger, key[6:]) or "")
    return key


def _matter_key(spec: dict[str, Any], trigger: dict[str, Any]) -> str:
    """matter.key 解析：``event:<路径>`` 取触发事实字段，否则按字面值。"""
    return _resolve_ref(str(spec.get("key") or ""), trigger)


def _resolve_function(work_id: str, fn_key: str, venue_kind: str) -> str:
    """职能 code/fn_NNN → 实例 ``function_id``（解不出 ⇒ ''，投递侧记失败）。

    ``identity_for`` 对查不到的职能会回显输入串，所以必须再用
    ``function_exists`` 做实存校验——否则不存在的职能会"解析成功"。
    """
    if not str(fn_key or "").strip():
        return ""
    try:
        from ..api import function_room
        from ..storage import function_store
        ident = function_room.identity_for(work_id, fn_key, venue_kind)
        fid = str((ident or {}).get("function_id") or "")
        if fid and function_store.function_exists(work_id, fid):
            return fid
        return ""
    except Exception:  # noqa: BLE001 供给侧坏了不拦记账——留 spec 投递侧报
        return ""


def _refresh_oversize_matter(work_id: str, st: Any, matter_id: str,
                             f: dict[str, Any], basis: dict[str, Any],
                             trigger: dict[str, Any], then: dict[str, Any],
                             ctx: dict[str, Any]) -> None:
    """同一连续超限期的后续超限编辑：刷新事项事实锚＋待投递材料。

    - 事项 basis 记录最新事实锚（card/seq/ts）＋**当前卡面实测字数**
      ——事件里的 new_len 可能是迟到旧值，材料以现状为准。
    - 仍超限 ⇒ 待受理动作（pending/failed）的 message 按当前字数重渲
      染；已受理（sending/sent/delivered）的只更新台账事实，历史提醒
      原样可查。恢复后 _check 会关事项——这里只负责刷新。
    - 刷新失败只记错不拦——重复事实的合并语义已成立。
    """
    try:
        m = st.get_matter(work_id, matter_id) or {}
        card = _oversize_card(m)
        cur = _status_len(work_id, card)
        st.update_matter_basis(
            work_id, matter_id,
            basis={**(basis or {}),
                   "current_len": cur,
                   "event_new_len": trigger.get("new_len")})
        if cur is None or cur < _OVERSIZE_LIMIT:
            return
        from . import harness
        c2 = dict(ctx)
        c2["trigger_data"] = {**(trigger or {}), "new_len": cur}
        text = harness.render_message(
            {"message": str(then.get("message") or "")}, c2)
        if not text:
            return
        for aid in (f.get("_actions") or {}).values():
            st.refresh_action_message(work_id, str(aid), text)
    except Exception as exc:  # noqa: BLE001 合并刷新失败不拦主链
        logger.warning("[intervention] 超限材料刷新失败 work=%s: %s",
                       work_id, exc)
        st.record_error(work_id, stage="record", kind="coalesce_refresh",
                        policy_id=_OVERSIZE_PID,
                        matter_id=str(matter_id or ""),
                        detail={"error": str(exc)})


def record_fired(work_id: str, fired: list[dict[str, Any]]) -> None:
    """命中即落账：解目标 → 立案（声明了 matter 的）→ 逐目标登记动作。

    在 dispatch 同一调用栈内同步完成——清账（内存）与记责（落盘）只差
    这一步，之后才允许异步投递。``f`` 被就地标注 ``_targets`` / ``_actions`` /
    ``_matter_id`` 供投递侧取用。
    """
    st = intervention_store.store()
    for f in fired or []:
        then = f.get("then") or {}
        ctx = f.get("ctx") or {}
        pid = str(f.get("policy") or "")
        scope_key = _scope_str(ctx.get("scope"))
        basis = f.get("basis") or {}
        trigger = ctx.get("trigger_data") or {}
        # [fire_key 带事实时刻 2026-11-15 · card_008 seq 复用实案] journal seq
        # 是卡内水位不是全局唯一——新批归档旧会话后计数器重走，跨天必然撞号：
        # 11-14 fn_002 的 round_done 拿到 seq 2428，fire_key 与 11-13 一条已
        # 投递动作撞键 ⇒ record_action 去重命中旧行 ⇒ action_skip 见
        # delivered 直接跳过 ⇒ 那次续跑义务无声蒸发，场冻 1.5h。触发事实的
        # ts 进键：同一事实重放（重启接责）⇒ 同 ts ⇒ 照旧去重；seq 复用的
        # 不同事实 ⇒ 不同 ts ⇒ 不再误去重。
        fire_key = "%s|%s|%s:%s|%s" % (
            pid, scope_key,
            str(basis.get("card") or ""), str(basis.get("seq") or 0),
            str(basis.get("ts") or ""))
        # [动作按业务状态去重 2026-11-20 · 批次A返工] then.dedup_fields
        # 声明的触发字段拼成 biz_key：同一可运行步骤（批次/稿次/席位/
        # 待投）换 seq/ts 再来 ⇒ 动作身份不变、零重投；真正进入下一步
        # ⇒ 新 biz_key ⇒ 恰好续一次。
        _dedup = then.get("dedup_fields")
        # [批五·开关] 伪调用同异常业务键只在合并试点开时启用——关态
        # 沿事实级 fire_key＋既有节流（行为同基线）。
        if pid == "pseudo_call_reinvoke" and not _coalesce_on():
            _dedup = None
        biz_key = ""
        step_map: Optional[dict] = None
        if isinstance(_dedup, list) and _dedup:
            # [步骤身份结构化 2026-11-20 · 批次A第三轮返工] dedup 字段
            # 的解析值同时落 step_json——投递/恢复把 {batch,draft,next}
            # 传给评审属主执行前复核，过期销账不执行。
            _vals = {str(x): _resolve_ref(f"event:{str(x)}", trigger)
                     for x in _dedup}
            _biz = "|".join(_vals[str(x)] for x in _dedup)
            # 字段全空 ⇒ 键退化成分隔符串——不启用 biz 去重，回落
            # fire_key 事实级（宁可多发不吞步骤）。
            biz_key = _biz if _biz.strip("|").strip() else ""
            if pid == "pseudo_call_reinvoke":
                missing = [key for key in ("pair_id", "round", "anomaly")
                           if not _vals.get(key)]
                if missing:
                    biz_key = ""
                    st.record_error(
                        work_id, stage="record", kind="missing_identity",
                        policy_id=pid, card_id=str(basis.get("card") or ""),
                        detail={"fields": missing})
                _vals["sid"] = str(trigger.get("sid") or "")
                _vals["task_id"] = str(trigger.get("task_id") or "")
            step_map = _vals
            if pid == "review_step_not_finished":
                draft_fp = _resolve_ref("event:draft_fp", trigger)
                if draft_fp:
                    step_map["draft_fp"] = draft_fp
        f["_ledger"] = True   # 本单已过台账记账——投递必须有可核对 action_id

        from . import harness
        dep = then.get("deploy") if isinstance(then.get("deploy"), dict) else None
        # deploy：动作落点＝**场**（venue），不走 then.target；差事文在
        # 场会话里跑，所以 target 列记场卡。
        where_expr = (str(dep.get("venue") or "self") if dep is not None
                      else str(then.get("target") or "self"))
        try:
            targets = harness.resolve_targets(
                where_expr, ctx, allow_self=(dep is not None))
        except Exception as exc:  # noqa: BLE001 解不出目标 ⇒ 无动作
            logger.warning("[intervention] 目标解析失败 %s: %s", pid, exc)
            st.record_error(work_id, stage="resolve",
                            kind=type(exc).__name__, policy_id=pid,
                            card_id=str(basis.get("card") or ""),
                            detail={"where": where_expr,
                                    "error": str(exc)})
            targets = []
        f["_targets"] = targets

        matter_id = ""
        mspec = then.get("matter")
        # [超限合并开关] 超限策略的 matter 合并只在试点开时生效——关态
        # 沿旧路：每次超限编辑各起一动作、各醒一次（行为逐字同基线）。
        if pid == _OVERSIZE_PID and not _coalesce_on():
            mspec = None
        if isinstance(mspec, dict) and mspec.get("kind"):
            mkey = _matter_key(mspec, trigger)
            # [超限立案前核验] 新立案（非复用）前按当前卡面复核——迟到/
            # 回放的旧超限事实不能在现状已恢复时重开事项；复用分支由
            # _check 每事实复核，不受此闸影响。
            if pid == _OVERSIZE_PID:
                _pre_card = (str(basis.get("card") or "")
                             or (str(targets[0]) if targets else ""))
                _pre_len = _status_len(work_id, _pre_card)
                if (_pre_len is not None
                        and _pre_len < _OVERSIZE_LIMIT):
                    logger.info(
                        "[intervention] %s 现状 %d 字已恢复，旧超限事实"
                        "不立案 card=%s", pid, _pre_len, _pre_card)
                    f["_matter_reused"] = True
                    f["_actions"] = {}
                    continue
            objects = {
                "review_card": (ctx.get("card_id")
                                or (ctx.get("scope") or [""])[-1]),
                "target": str(trigger.get("target") or ""),
                # [恢复目标持久化 2026-11-20 · 批次A返工2] 解析后的投递
                # 目标随事项落账——动作登记中断时 resume 沿**原解析目标**
                # 补账；不能回读事件语义字段（event.target 是被评对象，
                # 不是投递落点——review_all_passed 的 target 是正卡）。
                "deliver_targets": [str(t) for t in targets],
            }
            matter_id, _new = st.open_matter(
                work_id, policy_id=pid, scope_key=scope_key,
                kind=str(mspec["kind"]), matter_key=mkey,
                objects=objects,
                check_on=[str(x) for x in (mspec.get("check_on") or [])],
                basis=basis)
            f["_matter_id"] = matter_id
            if matter_id and not _new and st.matter_actions(work_id, matter_id):
                # [动作身份去重 2026-11-20 · 任务书批次A] 同一业务状态已有
                # open 事项且有动作行 ⇒ 这条新事实只是"同一状态又来一遍"：
                # 不再造新动作行，把原动作身份映射进 _actions 供报告/复核
                # 对齐；deliver_fired 见 _matter_reused 整单跳过（旧动作的
                # 送达与重投归台账/resume 盯，不由新触发代劳）。
                # 事项零动作（上次登记中断）⇒ 不走这里——落到下方正常
                # 登记自愈（批次A返工：恢复流程必须能补齐缺失动作）。
                f["_matter_reused"] = True
                f["_actions"] = {
                    str(a.get("target") or ""): str(a.get("action_id") or "")
                    for a in st.matter_actions(work_id, matter_id)
                }
                if pid == _OVERSIZE_PID:
                    _refresh_oversize_matter(
                        work_id, st, matter_id, f, basis, trigger, then, ctx)
                continue
            if not matter_id:
                logger.warning("[intervention] 立案落空 work=%s pid=%s",
                               work_id, pid)
                st.record_error(work_id, stage="record",
                                kind="matter_failed", policy_id=pid,
                                card_id=str(basis.get("card") or ""),
                                detail={"matter_kind": str(mspec["kind"]),
                                        "matter_key": mkey})

        mode = str(then.get("mode") or "message")
        wake = bool(then.get("wake", True))
        # 文案渲染在投递侧（render_text 要 ctx）——台账先存模板，便于排障。
        msg_tmpl = str(then.get("message") or "")
        actions: dict[str, str] = {}
        if dep is not None:
            # [deploy 2026-11-01] 第四手段：派差事。职能/场型/退出规格全落
            # spec（重启恢复不依赖当时 ctx 重算）；差事文按场渲染进 message；
            # fire_key 缀职能——同一触发往同一场派不同职能是两笔账。
            fn_key = _resolve_ref(str(dep.get("function") or ""), trigger)
            vk = str(dep.get("venue_kind") or "deploy").strip() or "deploy"
            spec0 = {
                "function": fn_key,
                "function_id": _resolve_function(work_id, fn_key, vk),
                "venue_kind": vk,
                "ctx": _resolve_ref(str(dep.get("ctx") or ""), trigger),
                "mandate_template": str(dep.get("mandate_template") or ""),
                "exit_kind": str(dep.get("exit_kind") or "explicit"),
                "exit_spec": _resolve_ref(str(dep.get("exit_spec") or ""),
                                        trigger),
                "venue_label": str(dep.get("venue_label") or ""),
                "batch_id": _resolve_ref(str(dep.get("batch_id") or ""),
                                         trigger),
                "target_card": _resolve_ref(str(dep.get("target_card") or ""),
                                            trigger),
                "wake": bool(dep.get("wake", True)),
            }
            dep_fire = f"{fire_key}|dep:{fn_key or '?'}"
            for t in targets:
                spec = dict(spec0)
                spec["ctx"] = spec["ctx"] or t
                spec["target_card"] = spec["target_card"] or t
                ctx2 = dict(ctx)
                ctx2["card_id"] = t
                text = harness.render_message(
                    {"message": dep.get("mandate") or ""}, ctx2)
                aid, _created = st.record_action(
                    work_id, policy_id=pid, scope_key=scope_key,
                    fire_key=dep_fire, target=t, mode="deploy",
                    wake=bool(dep.get("wake", True)), message=text,
                    matter_id=matter_id, biz_key=biz_key,
                    step=step_map,
                    spec=json.dumps(spec, ensure_ascii=False))
                if aid:
                    actions[t] = aid
                else:
                    st.record_error(work_id, stage="record",
                                    kind="action_failed", policy_id=pid,
                                    card_id=t, detail={"mode": "deploy"})
            f["_deploy"] = True
        else:
            for t in targets:
                # runner 与策略按原会话、原跳和异常共用一笔决定。
                if (pid == "pseudo_call_reinvoke" and _coalesce_on()
                        and step_map and biz_key):
                    aid, _created, _owner = st.claim_anomaly_decision(
                        work_id, policy_id=pid,
                        scope_key=scope_key, fire_key=fire_key,
                        target=t, mode=mode, wake=wake, message=msg_tmpl,
                        matter_id=matter_id, step=step_map)
                    if aid and not _created:
                        f["_matter_reused"] = True
                        f["_shared_decision"] = _owner
                else:
                    aid, _created = st.record_action(
                        work_id, policy_id=pid, scope_key=scope_key,
                        fire_key=fire_key,
                        target=t, mode=mode, wake=wake, message=msg_tmpl,
                        matter_id=matter_id, biz_key=biz_key, step=step_map,
                    # [批五·最新材料] 超限提醒把模板随动作落账——resume
                    # 领取时按当前卡面字数重渲染，不投陈旧读数。
                    spec=(json.dumps({"msg_tmpl": msg_tmpl},
                                     ensure_ascii=False)
                          if pid == _OVERSIZE_PID and _coalesce_on()
                          else json.dumps({"basis": basis, "signature": then.get("signature")})
                          if mode == "inspect" else ""))
                if aid:
                    actions[t] = aid
                else:
                    st.record_error(work_id, stage="record",
                                    kind="action_failed", policy_id=pid,
                                    card_id=t, detail={"mode": mode})
        f["_actions"] = actions


# ================================================================ 解除检查（feed 同步段）
def on_fact(work_id: str, fact: dict[str, Any]) -> None:
    """每条事实：开着的事项里，谁关注这类事件就复核谁。"""
    ev_type = str((fact or {}).get("type") or "")
    st = intervention_store.store()
    if not st.count_open(work_id):
        return
    for m in st.open_matters(work_id):
        watch = m.get("check_on") or []
        if watch and ev_type not in watch:
            continue
        verdict, evidence = _check(work_id, m)
        if verdict != "open":
            logger.info("[intervention] 事项%s %s kind=%s ev=%s",
                        "解除" if verdict == "resolved" else "作废",
                        m.get("matter_id"), m.get("kind"), ev_type)
            st.close_matter(work_id, str(m["matter_id"]),
                            state=verdict, evidence=evidence)


# ================================================================ 投递侧钩子
def action_skip(work_id: str, action_id: str) -> bool:
    """投前复核：动作已送过/作废，或其事项已关 ⇒ True（不发）。

    事项仍开 ⇒ 顺带跑一次解除检查——目标可能刚自己干完了。
    """
    st = intervention_store.store()
    a = st.get_action(work_id, action_id)
    if not a:
        return True
    if a.get("state") in ("delivered", "stale"):
        logger.info("[intervention] 动作跳过 aid=%s state=%s",
                    action_id, a.get("state"))
        return True
    mid = str(a.get("matter_id") or "")
    if not mid:
        return False
    m = st.get_matter(work_id, mid)
    if not m or m.get("state") != "open":
        if m is None or m.get("state") != "open":
            st.mark_action(work_id, action_id, "stale")
            return True
    verdict, evidence = _check(work_id, m)
    if verdict != "open":
        st.close_matter(work_id, mid, state=verdict, evidence=evidence)
        return True
    return False


def oversize_delivery_stale(work_id: str, action: dict[str, Any]) -> bool:
    """[批五·落信边界复核 2026-11-22] 超限动作**领取后、写会话前**
    再核一次：事项已关 或 当前卡面已回阈值内 ⇒ True（不落信）。

    领取与落信之间有缝——领取时超 10,000 的事实登记后，卡面可能
    已被编辑恢复到 9,999；此时落信＝用过期读数打扰用户还触发
    一次起轮（独立复跑实录）。只在合并开关开＋超限策略时生效。"""
    if not _coalesce_on():
        return False
    if str((action or {}).get("policy_id") or "") != _OVERSIZE_PID:
        return False
    mid = str(action.get("matter_id") or "")
    if mid:
        m = intervention_store.store().get_matter(work_id, mid)
        if m is None or str(m.get("state") or "") != "open":
            return True
    cur = _status_len(work_id, str(action.get("target") or ""))
    if cur is not None and cur < _OVERSIZE_LIMIT:
        return True
    return False


def claim_for_delivery(work_id: str, action_id: str) -> Optional[str]:
    """实时投递侧的原子领取（``policy_runner.deliver_fired`` 用）：
    ``sending`` 在 ``CLAIM_STALE_S`` 租约内视为仍有人在发 ⇒ 不抢；
    pending/failed/sent 或租约过期 ⇒ 领取成功转 sending。

    返回**领取凭据**（``claim_token``）——写回必须持本次凭据；
    ``None``＝没抢到。"""
    return intervention_store.store().claim_action(
        work_id, action_id,
        dead_sending_before=time.time() - CLAIM_STALE_S)


@contextlib.asynccontextmanager
async def claim_lease(work_id: str, action_id: str, claim_token: str,
                      *, interval: float = CLAIM_HEARTBEAT_S):
    """[在途续约 2026-11-20 · 批次A第四轮返工] 投递等待期间周期性
    ``touch_claim``——**活发送者让 ``sending`` 行保持新鲜**：resume
    的租约过期判据只在发送者真的退出（心跳停了）后成立，活在途
    投递不再被当成死账抢投。凭据已易主 ⇒ 心跳自停。"""
    if not claim_token:
        yield
        return

    stop = asyncio.Event()

    async def _beat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                ok = intervention_store.store().touch_claim(
                    work_id, action_id, claim_token)
                if ok is False:
                    return      # 凭据已易主 ⇒ 不再替别人续约
                # ok None＝可重试错误（锁冲突等瞬时 I/O）——本轮没续上，
                # 下轮再试，一次冲突不把心跳打死（批次A第五轮返工）。
            except Exception:  # noqa: BLE001 心跳失败下轮再试
                pass

    task = asyncio.ensure_future(_beat())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def step_expect(action: dict[str, Any]) -> Optional[dict[str, str]]:
    """动作行的步骤身份（``record_fired`` 按 ``dedup_fields`` 落的
    ``step_json``）——投递/恢复把它传给评审属主执行前复核。

    ``pin`` 这类结构化附加字段原样保留（点名席位的执行指定）——
    它不在 ``{batch,draft,next}`` 身份三元组里，复核只核三元组。"""
    raw = (action or {}).get("step_json")
    if isinstance(raw, dict) and raw:
        return {str(k): (v if isinstance(v, (dict, list)) else str(v))
                for k, v in raw.items()}
    return None


def step_contract_of(then: Any) -> bool:
    """策略 ``then`` 是否声明 ``dedup_fields``（＝continue 动作带
    步骤身份契约）。"""
    df = (then or {}).get("dedup_fields") if isinstance(then, dict) \
        else None
    return isinstance(df, list) and bool(df)


def _policy_then(action: dict[str, Any]) -> Optional[dict]:
    """动作所属策略的 ``then`` 配置；查不到策略 ⇒ ``None``。"""
    pid = str((action or {}).get("policy_id") or "")
    if not pid:
        return None
    try:
        from . import policy_set
        then = getattr(policy_set.dispatcher().policy(pid), "then", None)
        return then if isinstance(then, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _action_step_contract(action: dict[str, Any]) -> bool:
    """动作所属策略是否声明 ``dedup_fields``（＝continue 动作带步骤
    身份契约）。查不到策略 ⇒ False（按无契约处理，不误伤）。"""
    return step_contract_of(_policy_then(action))


def step_unverifiable(action: dict[str, Any], *,
                      contract: bool = True) -> bool:
    """[批次A第三/四轮返工] continue 动作在**步骤契约**下无法确认
    下一步身份：

    - ``step_json`` 缺失（契约策略的旧账，登记早于 step 列）或
      ``next`` 为空（旧格式事件只报了 runnable 没报身份）⇒ True；
    - 无契约策略（``dedup_fields`` 未声明）⇒ False——它们本就没有
      步骤身份概念，照常投递；
    - ``contract=False`` 显式声明无契约 ⇒ False。"""
    if str((action or {}).get("mode") or "") != "continue" \
            or not contract:
        return False
    step = step_expect(action)
    return step is None or not str(step.get("next") or "")


async def _refresh_step_expect(work_id: str,
                               action: dict[str, Any]
                               ) -> Optional[dict[str, str]]:
    """[批次A第四轮返工] 无法确认身份的旧 continue 账 ⇒ **重读当前
    状态**：评审属主现状有可跑下一步 ⇒ 返回现状身份作 ``expect``
    （属主执行前仍会再核，换批照样 stale）；无批次/无下一步 ⇒
    ``None``（调用方销账 stale——新事件会另行立案）。"""
    target = str((action or {}).get("target") or "")
    if not target:
        return None
    try:
        from ..api import review_runner
        ident = await review_runner._step_identity(work_id, target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[intervention] 重读步骤身份失败 %s: %s",
                       target, exc)
        return None
    if not isinstance(ident, dict) \
            or not str(ident.get("next") or ""):
        return None
    return {k: str(ident.get(k) or "")
            for k in ("batch", "draft", "next", "draft_fp") if k in ident}


def _merge_refreshed_action(work_id: str, action: dict[str, Any],
                            expect: dict[str, str]) -> str:
    """[旧账归并 2026-11-20 · 批次A第五轮返工] 刷新出的现状身份按
    **当前业务键**对账，再决定本行去留：

    - 已有同 ``(policy,scope,target,biz)`` 的另一动作 ⇒ 本行并入
      （``stale``＋``superseded_by`` 审计），由那一行负责投递；
    - 没有 ⇒ 本行 ``rekey`` 到当前键（登记现状身份，后续恢复/
      并发按现键查得到）后照常投；``rekey`` 撞键（并发先到）⇒
      同样并入占键者。

    返回 ``"superseded"``（并入，勿投）／``"ok"``（本行投）。"""
    st = intervention_store.store()
    aid = str(action.get("action_id") or "")
    then = _policy_then(action) or {}
    df = then.get("dedup_fields")
    if not isinstance(df, list) or not df:
        return "ok"      # 无契约字段集——无从拼业务键，照常投
    biz = "|".join(str(expect.get(str(f)) or "") for f in df)
    if not biz.strip("|").strip():
        return "ok"      # 退化键不启用（与 record_fired 同口径）

    pid = str(action.get("policy_id") or "")
    scope = str(action.get("scope_key") or "")
    target = str(action.get("target") or "")

    def _supersede(kept: str) -> str:
        st.mark_action(
            work_id, aid, "stale",
            receipt={"reason": "superseded_by", "kept": kept,
                     "biz_key": biz})
        logger.info("[intervention] 旧账并入既有动作 %s→%s"
                    " (biz=%s)", aid, kept, biz)
        return "superseded"

    other = st.find_action_by_biz(work_id, pid, scope, target, biz)
    if other and other != aid:
        return _supersede(other)
    rk = st.rekey_action(work_id, aid, biz_key=biz, step=expect)
    if rk == "conflict":   # 并发先到一步占键 ⇒ 并入占键者
        other = st.find_action_by_biz(work_id, pid, scope, target,
                                      biz)
        return _supersede(other or "(unknown)")
    return "ok"


def mark_receipt(work_id: str, action_id: str, result: Any, *,
                 claim_token: str = "") -> None:
    """投递结果落账：**受理判定**按 ``harness.deliver_accepted``——
    ``continue:0``（评审侧 already_running/stopped/review_busy…）不是
    已送达，是 failed（resume 下轮再试）；``continue:stale`` 是评审
    属主判定的**过期步骤**——销账 stale 不重试；其余 truthy ⇒ delivered。

    ``claim_token`` 非空 ⇒ 写回核对领取凭据（旧属主迟到回执拒写）。"""
    from . import harness
    st = intervention_store.store()
    s = str(result or "")
    details = result.details if isinstance(result, harness.ContinueReceipt) else {}
    if s.startswith("continue:"):
        previous = st.get_action(work_id, action_id) or {}
        if (previous.get("receipt") or {}).get("legacy_resume"):
            details = {**details, "legacy_resume": True}
    if s in ("continue:stale", "inspect:stale"):
        st.mark_action(work_id, action_id, "stale",
                       receipt={"result": s, "stale": True, **details},
                       claim_token=claim_token)
        return
    ok = harness.deliver_accepted(result)
    st.mark_action(work_id, action_id,
                   "delivered" if ok else "failed",
                   receipt={"result": s, "accepted": ok, **details},
                   claim_token=claim_token)
    if not ok:
        st.record_error(work_id, stage="deliver", kind="delivery_failed",
                        action_id=str(action_id or ""),
                        detail={"result": s, **details})


async def deliver_registered_action(work_id: str,
                                    action_id: str) -> str:
    """兑现**已登记**的一条动作：领取 → deliver → 回执——与
    ``deliver_fired``/``resume`` 同一组闸（action_skip/claim/claim_lease/
    mark_receipt），专给「人工/正卡请求」这条**非策略触发**的登记用。

    返回结局短码：``delivered`` / ``continue:N`` / ``failed`` /
    ``in_flight``（在途，没抢着）/ ``skipped``（已送过或销账）/
    ``missing`` / ``error``——调用方如实上报，不猜受理。"""
    st = intervention_store.store()
    a = st.get_action(work_id, action_id)
    if not a:
        return "missing"
    if a.get("state") in ("delivered", "stale"):
        return str(a.get("state"))
    try:
        if action_skip(work_id, action_id):
            return "skipped"
    except Exception as exc:  # noqa: BLE001 查不了不拦投递
        logger.warning("[intervention] 投前复核失败 %s: %s", action_id, exc)
    token = st.claim_action(
        work_id, action_id,
        dead_sending_before=time.time() - CLAIM_STALE_S)
    if not token:
        return "in_flight"
    try:
        from . import harness
        a = st.get_action(work_id, action_id) or a
        _mode = str(a.get("mode") or "continue")
        _expect = step_expect(a) if _mode == "continue" else None
        async with claim_lease(work_id, action_id, token):
            res = await harness.deliver(
                work_id, str(a.get("target") or ""),
                str(a.get("message") or ""),
                rule_id=str(a.get("policy_id") or ""),
                wake=bool(a.get("wake")), mode=_mode,
                action_id=action_id, expect=_expect)
        mark_receipt(work_id, action_id, res, claim_token=token)
        return str(res or "failed")
    except Exception as exc:  # noqa: BLE001 单动作失败记回执
        logger.warning("[intervention] 登记动作兑现失败 %s: %s",
                       action_id, exc)
        st.mark_action(work_id, action_id, "failed",
                       receipt={"result": f"deliver_exc:{exc}"},
                       claim_token=token)
        return "error"


def already_delivered(work_id: str, action_id: str) -> bool:
    """重启去重：事实库里已有该动作的 ``harness_message`` 回执 ⇒ 不再发。

    [批次A返工] 续跑被拒（continue:0 ⇒ 事件里 ``started=False``）不算已
    送达——那是"没走成"的回执，resume 必须还能重投。message/inject
    投递没有 started 字段，照常算已送达。"""
    from . import fact_store
    try:
        row = intervention_store.store().get_action(work_id, action_id) or {}
        legacy_resume = bool((row.get("receipt") or {}).get("legacy_resume"))
        rows = fact_store.store().query(
            work_id, types=["harness_message"],
            where={"action_id": action_id}, limit=5)
        for r in rows:
            d = r.get("data") or {}
            if legacy_resume and d.get("accepted") is not True:
                continue
            if str(d.get("mode") or "") == "continue" \
                    and d.get("started") is False:
                continue   # 续跑被评审侧拒绝（already_running/stopped…）
            return True
        return False
    except Exception as exc:  # noqa: BLE001 查不了 ⇒ 宁可发（不静默丢义务）
        logger.warning("[intervention] 查重失败 work=%s: %s", work_id, exc)
        intervention_store.store().record_error(
            work_id, stage="deliver", kind="dedup_exc",
            action_id=str(action_id or ""),
            detail={"error": str(exc)})
        return False


def _rebuild_missing_action(work_id: str, matter: dict[str, Any]) -> bool:
    """[批次A返工 · 恢复补齐] open 事项零动作 ⇒ 凭事项 basis＋现行策略
    重建动作行，让它进本次重投面——事项不许永久无主。

    动作身份：fire_key 用同一触发事实（basis 的 card/seq/ts）⇒ 与
    原登记同键，record_action 去重语义不变；目标取 matter.objects.target，
    缺席回落 scope 的 ``card:`` 键。拿不到策略/目标 ⇒ 记错误、留 open，
    人工核查。"""
    from . import policy_set
    st = intervention_store.store()
    mid = str(matter.get("matter_id") or "")
    pid = str(matter.get("policy_id") or "")
    scope_key = str(matter.get("scope_key") or "")
    basis = matter.get("basis") or {}
    p = policy_set.dispatcher().policy(pid)
    then = dict((p.then if p else None) or {})
    objects = matter.get("objects") or {}
    # [返工2] 优先沿**立案时持久化的解析目标**（deliver_targets）；
    # 旧账没有 ⇒ 回落 scope 的 card: 键（self 语义的保守近似）；
    # objects.target 是事件语义字段（被评对象），绝不当投递落点。
    targets = [str(t) for t in (objects.get("deliver_targets") or [])
               if str(t or "")]
    if not targets and scope_key.startswith("card:"):
        targets = [scope_key.split(":", 1)[1]]
    if not then or not targets:
        st.record_error(work_id, stage="resume", kind="action_rebuild_failed",
                        policy_id=pid, card_id=scope_key,
                        detail={"matter_id": mid,
                                "reason": "no_policy_or_target"})
        return False
    fire_key = "%s|%s|%s:%s|%s" % (
        pid, scope_key, str(basis.get("card") or ""),
        str(basis.get("seq") or 0), str(basis.get("ts") or ""))
    made = False
    for t in targets:
        aid, _created = st.record_action(
            work_id, policy_id=pid, scope_key=scope_key, fire_key=fire_key,
            target=t, mode=str(then.get("mode") or "message"),
            wake=bool(then.get("wake", True)),
            message=str(then.get("message") or ""), matter_id=mid)
        if aid:
            made = True
            logger.info("[intervention] 补齐缺失动作 matter=%s aid=%s"
                        " target=%s", mid, aid, t)
    if not made:
        st.record_error(work_id, stage="resume", kind="action_rebuild_failed",
                        policy_id=pid, card_id=scope_key,
                        detail={"matter_id": mid, "reason": "record_failed"})
        return False
    return True


# ================================================================ 重启恢复
def _reconcile_remediation(work_id: str, action: dict[str, Any]) -> str:
    """``mode="remediation"`` 残留占位的恢复对账——**不投递**。

    [2026-11-20 返修 · 用户复验] 占位→补跳是 runner 交付前的同一决定点，
    且「已发」由**实际请求边界**写回（pending→sent→delivered）。重启
    见到的残留行按请求/回包证据分三档——
      ① 台账有 ``remediation_jump``（回包证据）⇒ 核销 delivered；
      ② 台账有 ``remediation_request`` 或行态已 sent（请求已发、回包
        无凭）⇒ 核销 unverified 待核实；
      ③ 两者皆无 ⇒ 从未发出请求，标 stale＋待核实，回合收口时决定点
        按新 pair_id 重新裁定——**绝不把占位 message 当系统通知投回
        会话**（旧行为实锤）。
    返回核销状态供统计（"delivered" / "unverified" / "stale"）。
    """
    st = intervention_store.store()
    aid = str(action.get("action_id") or "")
    try:
        rec = json.loads(str(action.get("message") or "{}") or "{}")
    except (TypeError, ValueError):
        rec = {}
    bound = rec.get("bound") if isinstance(rec.get("bound"), dict) else {}
    pair_id = ""
    fire_key = str(action.get("fire_key") or "")
    parts = fire_key.split("|")
    if len(parts) >= 3:
        pair_id = parts[2]
    # ① 回包证据（台账查同一 action_id 的 remediation_jump）⇒ 核销 delivered
    fired = _ledger_has_remediation_jump(work_id, aid)
    if fired:
        st.mark_action(work_id, aid, "delivered",
                       receipt={"reconciled": "jump_found"})
        return "delivered"
    # ② 请求边界证据（台账查 remediation_request，或行态已是 sent）⇒
    #    请求确已发出、回包无凭 ⇒ 待核实核销（unverified），不重发不注入。
    sent = (str(action.get("state") or "") == "sent"
            or _ledger_has_remediation_request(work_id, aid))
    if sent:
        st.mark_action(
            work_id, aid, "unverified",
            receipt={"reconciled": "request_sent_no_response",
                     "pair_id": pair_id,
                     "bound_kind": str(bound.get("kind") or ""),
                     "note": "补救请求已发出、回包结果无凭——沿调用记录核查，"
                             "不当系统通知投递"})
        try:
            from ..api import tool_receipt
            tool_receipt.ledger({
                "kind": "remediation_reconciled",
                "work_id": work_id,
                "card_id": str(action.get("target") or ""),
                "pair_id": pair_id, "action_id": aid,
                "bound": bound, "outcome": "unverified_request_sent"})
        except Exception:  # noqa: BLE001
            pass
        return "unverified"
    # ③ 从未发出请求 ⇒ stale＋待核实（原任务义务留给回合决定点重裁定）
    st.mark_action(
        work_id, aid, "stale",
        receipt={"reconciled": "never_fired", "pair_id": pair_id,
                 "bound_kind": str(bound.get("kind") or ""),
                 "note": "原任务绑定仍在；回合收口时由补救决定点重新裁定"})
    try:
        from ..api import tool_receipt
        tool_receipt.ledger({
            "kind": "remediation_reconciled",
            "work_id": work_id,
            "card_id": str(action.get("target") or ""),
            "pair_id": pair_id, "action_id": aid,
            "bound": bound, "outcome": "stale_never_fired"})
    except Exception:  # noqa: BLE001
        pass
    return "stale"


def _ledger_scan_kind(work_id: str, action_id: str, kind: str) -> bool:
    """派发台账里查同一 action_id 的指定 kind 记录（限幅倒扫）。"""
    from pathlib import Path
    import json as _json
    path = Path("logs") / "tool_dispatch_ledger.jsonl"
    try:
        lines = path.read_text(encoding="utf-8",
                               errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-4000:]):
        try:
            row = _json.loads(line)
        except (TypeError, ValueError):
            continue
        if (row.get("kind") == kind
                and str(row.get("action_id") or "") == str(action_id)):
            return True
    return False


def _ledger_has_remediation_jump(work_id: str, action_id: str) -> bool:
    """派发台账里查同一 action_id 的 remediation_jump 记录（回包证据）。"""
    return _ledger_scan_kind(work_id, action_id, "remediation_jump")


def _ledger_has_remediation_request(work_id: str, action_id: str) -> bool:
    """派发台账里查同一 action_id 的 remediation_request 记录（请求证据）。"""
    return _ledger_scan_kind(work_id, action_id, "remediation_request")


async def resume(work_id: str) -> dict[str, int]:
    """重启接责：先给全部 open 事项复核，再把没送成的动作重新投递。

    顺序：事项复核在前——已解决的问题不唤醒；动作重投在后——投前
    ``already_delivered`` + ``action_skip`` 两道闸挡重发。返回统计供启动日志。
    [2026-11-20 返修] ``mode="remediation"`` 的动作走 ``_reconcile_remediation``
    对账核销——runner 占位不是待投消息，绝不进会话。
    """
    st = intervention_store.store()
    stats = {"matters": 0, "closed": 0, "actions": 0, "redelivered": 0,
             "skipped": 0}
    for m in st.open_matters(work_id):
        stats["matters"] += 1
        verdict, evidence = _check(work_id, m)
        if verdict != "open":
            st.close_matter(work_id, str(m["matter_id"]),
                            state=verdict, evidence=evidence)
            stats["closed"] += 1
    # [批次A返工 · 恢复补齐] 先补动作：open 事项零动作行 ⇒ 重建入册，
    # 随后 pending_actions 扫描才能把它带进本次重投面（不许永久无主）。
    for m in st.open_matters(work_id):
        if not st.matter_actions(work_id, str(m.get("matter_id") or "")):
            _rebuild_missing_action(work_id, m)
    from . import harness
    for a in st.pending_actions(work_id):
        stats["actions"] += 1
        aid = str(a.get("action_id") or "")
        if str(a.get("mode") or "") == "remediation":
            _reconcile_remediation(work_id, a)
            stats["skipped"] += 1
            continue
        if already_delivered(work_id, aid):
            st.mark_action(work_id, aid, "delivered")
            stats["skipped"] += 1
            continue
        if action_skip(work_id, aid):
            stats["skipped"] += 1
            continue
        _mode = str(a.get("mode") or "message")
        _expect: Optional[dict] = None
        if _mode == "continue":
            _expect = step_expect(a)
            contract = _action_step_contract(a)
            old_revise = bool(contract and _expect
                              and _expect.get("next") == "dispatch:revise")
            # [过期步骤销账 2026-11-20 · 批次A第三/四轮返工] 契约策略下
            # 无法确认身份的旧账（step_json 缺失/next 空）⇒ **恢复入口
            # 重读当前状态**：现状有可跑下一步 ⇒ 用现状身份重锚 expect
            # 投递（属主执行前仍会再核，换批照样 stale）；没有 ⇒ 销账。
            if old_revise or step_unverifiable(a, contract=contract):
                refreshed = await _refresh_step_expect(work_id, a)
                # 旧派修订缺应收回应水位。只允许归并到同批同稿的派修订。
                if old_revise and refreshed and _expect and (
                        refreshed["batch"] != _expect.get("batch")
                        or refreshed["draft"] != _expect.get("draft")
                        or not refreshed["next"].startswith("dispatch:revise:")):
                    refreshed = None
                if not refreshed:
                    st.mark_action(
                        work_id, aid, "stale",
                        receipt={"reason": "unverifiable_step",
                                 "policy_id": str(a.get("policy_id") or "")})
                    stats["skipped"] += 1
                    continue
                _expect = refreshed
                # [旧账归并] 刷新出的现状身份先按当前业务键对账——
                # 已有同键动作并入它，否则本行重锚到现键，不让旧账
                # 与已登记的当前动作各投一次（批次A第五轮返工）。
                if _merge_refreshed_action(work_id, a, _expect) \
                        == "superseded":
                    stats["skipped"] += 1
                    continue
        # [并发领取+凭据 2026-11-20 · 批次A第三轮返工] 发出前原子领取——
        # 与实时投递同一租约口径（now-CLAIM_STALE_S）：在途宽限内的
        # sending 属主仍活着不抢；租约过期＝确认退出才可接管，写回
        # 还须持本次 claim_token。
        # [在途续约 2026-11-20 · 批次A第四轮返工] claim_lease 让活
        # 发送者的行保鲜——超时≠退出，心跳停了才算。
        token = st.claim_action(
            work_id, aid,
            dead_sending_before=time.time() - CLAIM_STALE_S)
        if not token:
            stats["skipped"] += 1
            continue
        # [批五·落信边界复核] 领取到落信之间事项可能已关、卡面可能
        # 已回阈值——与即时投递同口径，复核不过销账不落信。
        if oversize_delivery_stale(work_id, a):
            st.mark_action(work_id, aid, "stale",
                           receipt={"reason": "resolved_before_deliver"},
                           claim_token=token)
            stats["skipped"] += 1
            continue
        if _mode == "continue":
            _expect = step_expect(st.get_action(work_id, aid) or a)
        try:
            if _mode == "deploy":
                # [deploy] 差事动作走部署执行器，不走送话管线；
                # create_deployment 幂等——补的是"唤起"，不重建账。
                from ..api import deploy_runner
                try:
                    spec = json.loads(str(a.get("spec") or "{}") or "{}")
                except (TypeError, ValueError):
                    spec = {}
                async with claim_lease(work_id, aid, token):
                    res = await deploy_runner.execute(
                        work_id, spec=spec, venue=str(a.get("target") or ""),
                        mandate=str(a.get("message") or ""), action_id=aid,
                        policy_id=str(a.get("policy_id") or ""))
                st.mark_action(work_id, aid,
                               "delivered" if res.get("ok") else "failed",
                               receipt=res, claim_token=token)
                if not res.get("ok"):
                    st.record_error(work_id, stage="resume",
                                    kind="deploy_failed", action_id=aid,
                                    policy_id=str(a.get("policy_id") or ""),
                                    detail=res)
                stats["redelivered"] += 1
                continue
            _msg = str(a.get("message") or "")
            # [批五·最新材料] 超限提醒领取后按**当前卡面**字数重渲染
            # （模板在 action.spec.msg_tmpl）——台账材料可能还停在更早
            # 一次编辑的读数；恢复路径与实时投递同口径。
            if (_coalesce_on()
                    and str(a.get("policy_id") or "") == _OVERSIZE_PID):
                _cur = _status_len(work_id, str(a.get("target") or ""))
                if _cur is not None:
                    try:
                        _tmpl = str(
                            json.loads(str(a.get("spec") or "{}") or "{}")
                            .get("msg_tmpl") or _msg)
                        _msg = (harness.render_message(
                            {"message": _tmpl},
                            {"trigger_data": {"new_len": _cur},
                             "card_id": str(a.get("target") or ""),
                             "work_id": work_id}) or _msg)
                    except Exception:  # noqa: BLE001 渲染失败沿存稿
                        pass
            async with claim_lease(work_id, aid, token):
                res = await harness.deliver(
                    work_id, str(a.get("target") or ""),
                    _msg,
                    rule_id=str(a.get("policy_id") or ""),
                    wake=bool(a.get("wake")), mode=_mode,
                    action_id=aid, expect=_expect)
            mark_receipt(work_id, aid, res, claim_token=token)
            stats["redelivered"] += 1
        except Exception as exc:  # noqa: BLE001 单动作失败不拖垮其余
            logger.warning("[intervention] 重投失败 %s: %s", aid, exc)
            st.mark_action(work_id, aid, "failed",
                           receipt={"result": f"resume_exc:{exc}"},
                           claim_token=token)
            st.record_error(work_id, stage="resume",
                            kind=type(exc).__name__, action_id=aid,
                            policy_id=str(a.get("policy_id") or ""),
                            detail={"error": str(exc)})
    return stats


# ================================================================ 查询面（最小）
def status(work_id: str) -> dict[str, Any]:
    """项目干预现状：开着的责任与待投动作（UI/排障直接读）。"""
    st = intervention_store.store()
    return {"open_matters": st.open_matters(work_id),
            "pending_actions": st.pending_actions(work_id),
            "errors": st.list_errors(work_id)}
