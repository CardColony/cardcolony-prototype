# -*- coding: utf-8 -*-
"""离线回放驱动（任务书批次A · 2026-11-20）。

把已固化的事件片段逐条喂进**真实** ``feed → record_fired → deliver_fired``
链，投递面换成记录型投递器：``harness.deliver`` / ``deploy_runner.execute`` /
``card_store.archive_card`` 全部替成记账函数——不起轮、不写会话、不产真实
副作用；干预台账（matters/actions/errors）与事实库按真实路径落账，供验收
对账。

判定口径（盲回放）：

- 逐事件行 ``verdict``：
  ``fired`` 命中（actions/deliveries 分列）；``no_fire`` 未命中；
  ``insufficient_info`` 注视该类型的策略声明了 ``when.event.where`` 字段
  而事实里缺席（旧事件缺 ``runnable`` 这类——如实标信息不足，不按
  新旧语义猜它该不该发）。
- 重放同一事件 ⇒ 同 ``fire_key`` ⇒ 同动作行；同一业务状态的新事实 ⇒
  事项复用、动作沿用原身份（``_matter_reused``，投递计 0）。
- 历史事件不得走真实投递/启动流程——本模块只接受显式事件清单，
  不读生产 works 目录（夹具是逐字段固化的快照）。

    res = await replay_events(wid, events)
    res = await replay_events(wid, events, restart=True)   # 中途模拟重启
    stats = await resume_after_restart(wid)                # 恢复路径
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger("infra.replay")

#: 记录型投递器收到的投递条目形态
Delivery = dict[str, Any]


def _watch_where_fields(work_id: str, ev_type: str) -> dict[str, list[str]]:
    """注视 ev_type 的策略里，``when.event.where`` 声明的字段清单。

    返回 {policy_id: [字段…]}——只取 event.where 平铺形态（其它 cond
    形态不靠字段缺席判"信息不足"）。"""
    try:
        from . import policy_set
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[str]] = {}
    for p in policy_set.dispatcher().policies():
        if not p.active or (p.work and p.work != work_id):
            continue
        if not p.watches_all and ev_type not in (p.watch or []):
            continue
        ev = (p.when or {}).get("event") if isinstance(p.when, dict) else None
        where = (ev or {}).get("where") if isinstance(ev, dict) else None
        if isinstance(where, dict) and where:
            out[p.id] = [str(k) for k in where.keys()]
    return out


async def replay_events(work_id: str, events: Iterable[dict[str, Any]],
                        *, restart_at: Optional[int] = None,
                        deliver_fn: Optional[Any] = None) -> dict[str, Any]:
    """逐条重放事件；``restart_at``＝在第几条索引处先模拟进程重启。

    ``deliver_fn(entry)``：可选投递回执替身——返回投递结果串（如
    ``"continue:0"`` 拒收），缺省 ``replay-msg-N`` 恒受理。同步或
    async 均可。

    返回::

        {"rows": [逐事件行], "deliveries": [记录投递],
         "deploys": [差事调用], "archives": [归档调用]}

    行内 ``fired`` 是策略命中清单（policy/matter/action 身份如实列出），
    ``deliveries_new`` 是本事件引起的投递条数。
    """
    from . import policy_runner, intervention_runner, harness, policy_set
    from ..api import deploy_runner
    from ..storage import card_store as _cs

    deliveries: list[Delivery] = []
    deploys: list[Delivery] = []
    archives: list[Delivery] = []
    queued: list[tuple[str, list[dict[str, Any]]]] = []

    orig_spawn = policy_runner._spawn            # noqa: SLF001 回放同步化
    orig_deliver = harness.deliver
    orig_execute = deploy_runner.execute
    orig_archive = _cs.archive_card

    async def _rec_deliver(wid: str, target: str, text: str, *,
                           rule_id: str = "", wake: bool = True,
                           mode: str = "message",
                           action_id: str = "",
                           expect: Any = None) -> str:
        entry = {"work": wid, "target": target, "text": text,
                 "rule_id": rule_id, "wake": bool(wake),
                 "mode": mode, "action_id": str(action_id or ""),
                 "expect": dict(expect) if isinstance(expect, dict) else expect}
        deliveries.append(entry)
        if deliver_fn is None:
            return f"replay-msg-{len(deliveries)}"
        res = deliver_fn(entry)
        if hasattr(res, "__await__"):
            res = await res
        return str(res)

    async def _rec_execute(wid: str, *, spec: dict, venue: str, mandate: str,
                           action_id: str = "",
                           policy_id: str = "") -> dict[str, Any]:
        deploys.append({"work": wid, "venue": venue, "spec": dict(spec or {}),
                        "mandate": mandate, "action_id": str(action_id or ""),
                        "policy_id": str(policy_id or "")})
        return {"ok": True, "receipt": "replay"}

    async def _rec_archive(wid: str, card_id: str, *, reason: str = "",
                           by: str = "") -> dict[str, Any]:
        archives.append({"work": wid, "card": card_id, "reason": reason,
                         "by": by})
        return {"ok": True}

    def _queue(wid: str, fired: list[dict[str, Any]]) -> None:
        queued.append((wid, list(fired)))

    policy_runner._spawn = _queue                 # noqa: SLF001
    harness.deliver = _rec_deliver
    deploy_runner.execute = _rec_execute
    _cs.archive_card = _rec_archive
    rows: list[dict[str, Any]] = []
    try:
        for i, rec in enumerate(events or []):
            if restart_at is not None and i == restart_at:
                policy_set.dispatcher().reset()   # 清内存本子；台账/事实落盘不动
            n0 = len(deliveries) + len(deploys) + len(archives)
            fired = policy_runner.feed(work_id, rec)
            for _wid, batch in queued:
                await policy_runner.deliver_fired(_wid, batch)
            queued.clear()
            ev_type = str((rec or {}).get("type") or "")
            want = _watch_where_fields(work_id, ev_type)
            data = (rec or {}).get("data") or {}
            insufficient = {
                pid: [k for k in fields if k not in data]
                for pid, fields in want.items()
            }
            insufficient = {p: f for p, f in insufficient.items() if f}
            fired_ids = [str(f.get("policy") or "") for f in fired or []]
            insufficient = {p: f for p, f in insufficient.items()
                            if p not in fired_ids}
            verdict = "fired" if fired else (
                "insufficient_info" if insufficient else "no_fire")
            rows.append({
                "idx": i, "seq": (rec or {}).get("seq"),
                "type": ev_type, "card": (rec or {}).get("card_id"),
                "reason": str(data.get("reason") or ""),
                "batch": str(data.get("batch") or ""),
                "fired": [
                    {"policy": str(f.get("policy") or ""),
                     "matter": str(f.get("_matter_id") or ""),
                     "reused": bool(f.get("_matter_reused")),
                     "actions": dict(f.get("_actions") or {})}
                    for f in fired or []],
                "insufficient": insufficient,
                "deliveries_new": len(deliveries) + len(deploys)
                + len(archives) - n0,
                "verdict": verdict,
            })
    finally:
        policy_runner._spawn = orig_spawn         # noqa: SLF001
        harness.deliver = orig_deliver
        deploy_runner.execute = orig_execute
        _cs.archive_card = orig_archive
    return {"rows": rows, "deliveries": deliveries,
            "deploys": deploys, "archives": archives}


async def resume_after_restart(work_id: str,
                               deliver_fn: Optional[Any] = None,
                               ) -> dict[str, Any]:
    """重启接责回放：先清内存本子（模拟重启），再走真实 ``resume``——
    投递仍落记录型投递器。返回 ``{"stats":…,"deliveries":…}``。"""
    from . import intervention_runner, policy_set, harness
    from ..api import deploy_runner
    from ..storage import card_store as _cs

    policy_set.dispatcher().reset()
    deliveries: list[Delivery] = []
    orig_deliver = harness.deliver
    orig_execute = deploy_runner.execute

    async def _rec_deliver(wid: str, target: str, text: str, *,
                           rule_id: str = "", wake: bool = True,
                           mode: str = "message",
                           action_id: str = "",
                           expect: Any = None) -> str:
        entry = {"work": wid, "target": target, "text": text,
                 "rule_id": rule_id, "wake": bool(wake),
                 "mode": mode, "action_id": str(action_id or ""),
                 "expect": dict(expect) if isinstance(expect, dict) else expect}
        deliveries.append(entry)
        if deliver_fn is None:
            return f"replay-msg-{len(deliveries)}"
        res = deliver_fn(entry)
        if hasattr(res, "__await__"):
            res = await res
        return str(res)

    async def _rec_execute(wid: str, *, spec: dict, venue: str, mandate: str,
                           action_id: str = "",
                           policy_id: str = "") -> dict[str, Any]:
        deliveries.append({"work": wid, "target": venue, "mode": "deploy",
                           "spec": dict(spec or {}),
                           "action_id": str(action_id or "")})
        return {"ok": True, "receipt": "replay"}

    harness.deliver = _rec_deliver
    deploy_runner.execute = _rec_execute
    try:
        stats = await intervention_runner.resume(work_id)
    finally:
        harness.deliver = orig_deliver
        deploy_runner.execute = orig_execute
        _ = _cs
    return {"stats": stats, "deliveries": deliveries}
