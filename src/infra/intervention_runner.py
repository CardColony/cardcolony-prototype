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
                            "verdict": verdict[:200]}
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
                    "error": str(exc)[:300]})
        return "open", {"reason": f"check_exc:{exc}"}


# ================================================================ 记录（feed 同步段）
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
        fire_key = "%s|%s|%s:%s" % (
            pid, scope_key,
            str(basis.get("card") or ""), str(basis.get("seq") or 0))

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
                                    "error": str(exc)[:300]})
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
            }
            matter_id, _new = st.open_matter(
                work_id, policy_id=pid, scope_key=scope_key,
                kind=str(mspec["kind"]), matter_key=mkey,
                objects=objects,
                check_on=[str(x) for x in (mspec.get("check_on") or [])],
                basis=basis)
            f["_matter_id"] = matter_id
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
                    matter_id=matter_id,
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
                    matter_id=matter_id)
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


def mark_receipt(work_id: str, action_id: str, result: Any) -> None:
    """投递结果落账：truthy ⇒ delivered；否则 failed（result 记回执＋错误账）。"""
    st = intervention_store.store()
    ok = bool(result)
    st.mark_action(work_id, action_id,
                   "delivered" if ok else "failed",
                   receipt={"result": str(result or "")[:300]})
    if not ok:
        st.record_error(work_id, stage="deliver", kind="delivery_failed",
                        action_id=str(action_id or ""),
                        detail={"result": str(result or "")[:300]})


def already_delivered(work_id: str, action_id: str) -> bool:
    """重启去重：事实库里已有该动作的 ``harness_message`` 回执 ⇒ 不再发。"""
    from . import fact_store
    try:
        rows = fact_store.store().query(
            work_id, types=["harness_message"],
            where={"action_id": action_id}, limit=1)
        return bool(rows)
    except Exception as exc:  # noqa: BLE001 查不了 ⇒ 宁可发（不静默丢义务）
        logger.warning("[intervention] 查重失败 work=%s: %s", work_id, exc)
        intervention_store.store().record_error(
            work_id, stage="deliver", kind="dedup_exc",
            action_id=str(action_id or ""),
            detail={"error": str(exc)[:300]})
        return False


# ================================================================ 重启恢复
async def resume(work_id: str) -> dict[str, int]:
    """重启接责：先给全部 open 事项复核，再把没送成的动作重新投递。

    顺序：事项复核在前——已解决的问题不唤醒；动作重投在后——投前
    ``already_delivered`` + ``action_skip`` 两道闸挡重发。返回统计供启动日志。
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
    from . import harness
    for a in st.pending_actions(work_id):
        stats["actions"] += 1
        aid = str(a.get("action_id") or "")
        if already_delivered(work_id, aid):
            st.mark_action(work_id, aid, "delivered")
            stats["skipped"] += 1
            continue
        if action_skip(work_id, aid):
            stats["skipped"] += 1
            continue
        try:
            if str(a.get("mode") or "") == "deploy":
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
                               receipt=res)
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
                wake=bool(a.get("wake")), mode=str(a.get("mode") or "message"),
                action_id=aid)
            mark_receipt(work_id, aid, res)
            stats["redelivered"] += 1
        except Exception as exc:  # noqa: BLE001 单动作失败不拖垮其余
            logger.warning("[intervention] 重投失败 %s: %s", aid, exc)
            st.mark_action(work_id, aid, "failed",
                           receipt={"result": f"resume_exc:{exc}"[:300]})
            st.record_error(work_id, stage="resume",
                            kind=type(exc).__name__, action_id=aid,
                            policy_id=str(a.get("policy_id") or ""),
                            detail={"error": str(exc)[:300]})
    return stats


# ================================================================ 查询面（最小）
def status(work_id: str) -> dict[str, Any]:
    """项目干预现状：开着的责任与待投动作（UI/排障直接读）。"""
    st = intervention_store.store()
    return {"open_matters": st.open_matters(work_id),
            "pending_actions": st.pending_actions(work_id),
            "errors": st.list_errors(work_id)}
