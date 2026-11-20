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

import json
import logging
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


_CHECKERS = {
    "review_promote": _check_review_promote,
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
            step_map = _vals
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
        if isinstance(mspec, dict) and mspec.get("kind"):
            mkey = _matter_key(mspec, trigger)
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
                aid, _created = st.record_action(
                    work_id, policy_id=pid, scope_key=scope_key, fire_key=fire_key,
                    target=t, mode=mode, wake=wake, message=msg_tmpl,
                    matter_id=matter_id, biz_key=biz_key, step=step_map)
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


def claim_for_delivery(work_id: str, action_id: str) -> Optional[str]:
    """实时投递侧的原子领取（``policy_runner.deliver_fired`` 用）：
    ``sending`` 在 ``CLAIM_STALE_S`` 租约内视为仍有人在发 ⇒ 不抢；
    pending/failed/sent 或租约过期 ⇒ 领取成功转 sending。

    返回**领取凭据**（``claim_token``）——写回必须持本次凭据；
    ``None``＝没抢到。"""
    return intervention_store.store().claim_action(
        work_id, action_id,
        dead_sending_before=time.time() - CLAIM_STALE_S)


def step_expect(action: dict[str, Any]) -> Optional[dict[str, str]]:
    """动作行的步骤身份（``record_fired`` 按 ``dedup_fields`` 落的
    ``step_json``）——投递/恢复把它传给评审属主执行前复核。"""
    raw = (action or {}).get("step_json")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    return None


def step_unverifiable(action: dict[str, Any]) -> bool:
    """[批次A第三轮返工] continue 动作带步骤身份但 ``next`` 为空 ⇒
    **旧格式事件只做诊断**：它证明不了"下一步要干什么"，不许触发
    投递——由恢复入口重读当前状态另行立案（新格式事件自带 next）。"""
    if str((action or {}).get("mode") or "") != "continue":
        return False
    step = step_expect(action)
    return step is not None and "next" in step \
        and not str(step.get("next") or "")


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
    if s == "continue:stale":
        st.mark_action(work_id, action_id, "stale",
                       receipt={"result": s, "stale": True},
                       claim_token=claim_token)
        return
    ok = harness.deliver_accepted(result)
    st.mark_action(work_id, action_id,
                   "delivered" if ok else "failed",
                   receipt={"result": s, "accepted": ok},
                   claim_token=claim_token)
    if not ok:
        st.record_error(work_id, stage="deliver", kind="delivery_failed",
                        action_id=str(action_id or ""),
                        detail={"result": s})


def already_delivered(work_id: str, action_id: str) -> bool:
    """重启去重：事实库里已有该动作的 ``harness_message`` 回执 ⇒ 不再发。

    [批次A返工] 续跑被拒（continue:0 ⇒ 事件里 ``started=False``）不算已
    送达——那是"没走成"的回执，resume 必须还能重投。message/inject
    投递没有 started 字段，照常算已送达。"""
    from . import fact_store
    try:
        rows = fact_store.store().query(
            work_id, types=["harness_message"],
            where={"action_id": action_id}, limit=5)
        for r in rows:
            d = r.get("data") or {}
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
        # [过期步骤销账 2026-11-20 · 批次A第三轮返工] 旧格式事件
        # （runnable 但缺 next）只做诊断——证明不了"下一步要干什么"，
        # 不触发投递、动作销账；当前状态由评审属主/新事件另行立案。
        if step_unverifiable(a):
            st.mark_action(work_id, aid, "stale",
                           receipt={"reason": "unverifiable_step",
                                    "policy_id": str(a.get("policy_id") or "")})
            stats["skipped"] += 1
            continue
        _mode = str(a.get("mode") or "message")
        _expect = step_expect(a) if _mode == "continue" else None
        # [并发领取+凭据 2026-11-20 · 批次A第三轮返工] 发出前原子领取——
        # 与实时投递同一租约口径（now-CLAIM_STALE_S）：在途宽限内的
        # sending 属主仍活着不抢；租约过期＝确认退出才可接管，写回
        # 还须持本次 claim_token。
        token = st.claim_action(
            work_id, aid,
            dead_sending_before=time.time() - CLAIM_STALE_S)
        if not token:
            stats["skipped"] += 1
            continue
        try:
            if _mode == "deploy":
                # [deploy] 差事动作走部署执行器，不走送话管线；
                # create_deployment 幂等——补的是"唤起"，不重建账。
                from ..api import deploy_runner
                try:
                    spec = json.loads(str(a.get("spec") or "{}") or "{}")
                except (TypeError, ValueError):
                    spec = {}
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
            res = await harness.deliver(
                work_id, str(a.get("target") or ""),
                str(a.get("message") or ""), rule_id=str(a.get("policy_id") or ""),
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
