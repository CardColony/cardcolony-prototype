# -*- coding: utf-8 -*-
"""条件集的接线与投递：事件落盘 → 写数据集 → 喂值班员 → 谁凑够了就发提醒。

## 一条事件走完的路

    event_log.append 落盘成功
      └ policy_runner.feed(work, rec)
          ├ fact_store.ingest       写库（冷数据，0.29 ms）
          ├ policy_set.dispatch     喂关心它的值班员（热状态，内存）
          └ 有人凑够 ⇒ 后台投递（不阻塞主链路）

## 为什么写库要同步、投递要异步

- **写库同步**：0.29 ms/条，相对事件落盘本身的 fsync 不算什么；而且数据集必须
  立刻有这条事实，否则新建策略回溯时会漏掉刚发生的事。
- **投递异步**：投递要落会话块、发事件、必要时起一轮，都是 IO，绝不能在事件
  落盘的调用栈里做。

## 零阻断

任何一步失败只记 warning。投递失败不影响事件本身，也不影响别的策略。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("infra.policy_runner")

#: 后台投递任务的强引用（防 GC 中途回收）
_PENDING: set[Any] = set()

#: 是否启用（``ECO_POLICY_OFF=1`` 整体关闭，便于排障）
import os  # noqa: E402


def _off() -> bool:
    return os.environ.get("ECO_POLICY_OFF") == "1"


def _record_error(work_id: str, *, stage: str, exc: Exception | None = None,
                  kind: str = "", policy_id: str = "", action_id: str = "",
                  card_id: str = "", key: str = "",
                  detail: Any = None) -> None:
    """链路失败入错误台账（自身失败只告警，不连环）。"""
    try:
        from . import intervention_store
        d = dict(detail or {})
        if exc is not None:
            d.setdefault("error", str(exc)[:300])
        intervention_store.store().record_error(
            work_id, stage=stage,
            kind=kind or (type(exc).__name__ if exc is not None else "fault"),
            policy_id=policy_id, action_id=action_id, card_id=card_id,
            key=key, detail=d)
    except Exception:  # noqa: BLE001
        pass


#: work_id → 已入过账的加载错误清单指纹（同一份错不重复计）
_CFG_ERR_DONE: dict[str, str] = {}


def _record_load_errors(work_id: str) -> None:
    """策略加载错误入错误台账：``dispatcher()`` 丢掉了 ``load()`` 的返回，
    这里在首个 feed 时按清单指纹补记一轮——同一份错不重复计。"""
    try:
        from . import intervention_store, policy_set
        errs = [str(e) for e in
                (getattr(policy_set.dispatcher(), "load_errors", None) or [])]
        fp = "|".join(errs)
        if not fp:
            _CFG_ERR_DONE.pop(work_id, None)
            return
        if _CFG_ERR_DONE.get(work_id) == fp:
            return
        _CFG_ERR_DONE[work_id] = fp
        st = intervention_store.store()
        for e in errs[:50]:
            pid = e.split(":", 1)[0].strip() if ":" in e else ""
            st.record_error(work_id, stage="config", kind="load_error",
                            policy_id=pid[:80], key=e[:120],
                            detail={"error": e[:400]})
    except Exception:  # noqa: BLE001
        pass


def feed(work_id: str, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """事件落盘后调用：写数据集 + 喂条件集 +（有触发就）后台投递。

    返回触发记录（诊断/测试用）。同步执行，全程零阻断。
    """
    if _off() or not work_id or not isinstance(rec, dict):
        return []
    try:
        from . import fact_store, policy_set
    except Exception as exc:  # noqa: BLE001 模块都加载不了 ⇒ 什么都不做
        logger.warning("[policy_runner] 加载失败: %s", exc)
        return []

    fact: Optional[dict[str, Any]] = None
    try:
        fact = fact_store.store().ingest(work_id, rec)
    except Exception as exc:  # noqa: BLE001 写库失败不影响触发判断
        logger.warning("[policy_runner] 写数据集失败 work=%s: %s", work_id, exc)
        _record_error(work_id, stage="ingest", exc=exc,
                      card_id=str((rec or {}).get("card") or ""))

    if fact is None:
        return []

    try:
        fired = policy_set.dispatcher().dispatch(work_id, fact)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy_runner] 条件派发失败 work=%s: %s", work_id, exc)
        _record_error(work_id, stage="dispatch", exc=exc,
                      card_id=str(fact.get("card") or ""))
        return []
    _record_load_errors(work_id)

    # [干预台账] 命中先落账再投递：清小本子（内存）与记责（harness.db）只差
    # 这一段同步代码——崩溃即两头同失，重启回放会重新命中，不会静默丢义务。
    if fired:
        try:
            from . import intervention_runner
            intervention_runner.record_fired(work_id, fired)
        except Exception as exc:  # noqa: BLE001 台账失败不拦投递
            logger.warning("[policy_runner] 干预落账失败 work=%s: %s",
                           work_id, exc)
            _record_error(work_id, stage="record", exc=exc,
                          card_id=str(fact.get("card") or ""))
        _spawn(work_id, fired)

    # [干预台账] 已开事项按新事实复核解除（内部按 check_on 门类过滤，开销小）。
    try:
        from . import intervention_runner
        intervention_runner.on_fact(work_id, fact)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy_runner] 事项复核失败 work=%s: %s", work_id, exc)
        _record_error(work_id, stage="check", exc=exc,
                      card_id=str(fact.get("card") or ""))
    return fired


def _spawn(work_id: str, fired: list[dict[str, Any]]) -> None:
    """投递丢后台（无事件循环时放弃——同步上下文里不硬起线程）。

    [干预台账] 放弃投递不再丢义务：动作行已先一步落 ``harness.db``，
    重启 ``intervention_runner.resume`` 会复核后重投。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[policy_runner] 无事件循环 ⇒ 本次不投递（%d 条，"
                     "动作已落台账，重启接责）", len(fired))
        return
    task = loop.create_task(deliver_fired(work_id, fired))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def deliver_fired(work_id: str, fired: list[dict[str, Any]]) -> int:
    """把触发记录变成真的提醒（落会话块 + 事件留痕 + 可选唤醒）。

    投递本身复用 ``harness.deliver``——不另造一套通道。
    返回成功投递条数。
    """
    from . import harness

    n = 0
    for f in fired or []:
        then = f.get("then") or {}
        ctx = dict(f.get("ctx") or {})
        pid = str(f.get("policy") or "")
        try:
            ctx = await _enrich(work_id, ctx)
            # [干预台账] record_fired 已把目标与动作行一并落账，优先复用；
            # 台账缺席（旧调用方/落账失败）才当场重解。
            targets = list(f.get("_targets") or [])
            _where = ""
            if not targets:
                _dep = then.get("deploy")
                _where = str((_dep or {}).get("venue") or then.get("target")
                             or "self")
                targets = harness.resolve_targets(
                    _where, ctx, allow_self=isinstance(_dep, dict))
            if not targets:
                logger.warning("[policy_runner] %s 解析不出投递目标 scope=%s",
                               pid, f.get("scope"))
                _record_error(work_id, stage="resolve", kind="no_targets",
                              policy_id=pid,
                              card_id=str(ctx.get("card_id") or ""),
                              detail={"where": _where})
                continue
            actions = f.get("_actions") or {}
            # [deploy 2026-11-01] 派差事：独立 dispatch——落差事账＋场唤起，
            # 不进送话管线。spec/mandate 从动作行取（台账缺席时才现解）。
            if isinstance(then.get("deploy"), dict):
                from . import intervention_runner, intervention_store
                from ..api import deploy_runner
                _st = intervention_store.store()
                for t in targets:
                    aid = str(actions.get(t) or "")
                    spec: dict[str, Any] = {}
                    mandate = ""
                    if aid:
                        try:
                            if intervention_runner.action_skip(work_id, aid):
                                continue  # 已送过/事项已关 ⇒ 不重派
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[policy_runner] 投前复核失败 %s: %s",
                                           aid, exc)
                            _record_error(work_id, stage="deliver",
                                          kind="skip_check_exc", exc=exc,
                                          policy_id=pid, action_id=aid)
                        row = _st.get_action(work_id, aid) or {}
                        try:
                            spec = json.loads(str(row.get("spec") or "{}"))
                        except (TypeError, ValueError):
                            spec = {}
                        mandate = str(row.get("message") or "")
                    if not spec:
                        spec = {k: v for k, v in (then["deploy"] or {}).items()
                                if isinstance(v, (str, int, float, bool))}
                    res = await deploy_runner.execute(
                        work_id, spec=spec, venue=t, mandate=mandate,
                        action_id=aid, policy_id=pid)
                    if aid:
                        _st.mark_action(
                            work_id, aid,
                            "delivered" if res.get("ok") else "failed",
                            receipt=res)
                    if res.get("ok"):
                        n += 1
                    else:
                        _record_error(work_id, stage="deliver",
                                      kind="deploy_failed", policy_id=pid,
                                      action_id=aid, detail=res)
                continue
            text = harness.render_message({"message": then.get("message") or ""}, ctx)
            wake = bool(then.get("wake", True))
            # [harness 三手段 2026-10-28] message＝进历史（永久）；inject＝只进下一轮
            mode = str(then.get("mode") or "message")
            for t in targets:
                aid = str(actions.get(t) or "")
                if aid:
                    from . import intervention_runner
                    try:
                        if intervention_runner.action_skip(work_id, aid):
                            continue  # 已送过/事项已关/目标自解 ⇒ 不重复投递
                    except Exception as exc:  # noqa: BLE001 查不了不拦投递
                        logger.warning("[policy_runner] 投前复核失败 %s: %s",
                                       aid, exc)
                        _record_error(work_id, stage="deliver",
                                      kind="skip_check_exc", exc=exc,
                                      policy_id=pid, action_id=aid)
                # action_id 是台账回执通道——没有台账调用方（直调 deliver_fired
                # 的旧用法）就不传，保持 deliver 签名向后兼容。
                res = await harness.deliver(
                    work_id, t, text, rule_id=pid, wake=wake, mode=mode,
                    **({"action_id": aid} if aid else {}))
                if aid:
                    intervention_runner.mark_receipt(work_id, aid, res)
                if res:
                    n += 1
        except Exception as exc:  # noqa: BLE001 一条失败不拖垮其余
            logger.warning("[policy_runner] 投递失败 %s: %s", pid, exc)
            _record_error(work_id, stage="deliver", exc=exc, policy_id=pid,
                          card_id=str(ctx.get("card_id") or ""))
    return n


async def _enrich(work_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """补上模板渲染要用的卡面字段（标题/类型）。取不到就留空。"""
    card_id = str(ctx.get("card_id") or "")
    if not card_id:
        return ctx
    try:
        from ..storage import card_store
        cur = await card_store.read_card(work_id, card_id)
        meta = (cur or {}).get("meta") or {}
        ctx.setdefault("card_title", str(meta.get("title") or ""))
        ctx.setdefault("card_type", str(meta.get("card_type") or ""))
    except Exception:  # noqa: BLE001 渲染缺字段比投递失败好
        pass
    return ctx


async def drain() -> int:
    """等所有在途投递（测试确定性用）。"""
    tasks = list(_PENDING)
    for t in tasks:
        try:
            await t
        except Exception:  # noqa: BLE001
            pass
    return len(tasks)


# ---------------------------------------------------------------- 启动/回溯
def bootstrap(work_id: str, *, since: float,
              events_dir: Any = None) -> dict[str, Any]:
    """给一个 work 建索引 + 补本子（启动或首次启用时调）。

    做三件事：
      ① 把事件账本最近 ``since`` 之后的部分灌进数据集（增量，已灌过的跳过）
      ② 从数据集回溯，把每条激活策略的本子补记好
      ③ 补完立刻判一次：此刻就已满足的，马上触发

    返回统计（诊断用）。
    """
    out: dict[str, Any] = {"facts": 0, "seeded": {}, "fired": 0}
    try:
        from . import fact_store, policy_set
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy_runner] bootstrap 加载失败: %s", exc)
        return out

    store = fact_store.store()
    if events_dir is None:
        from ..storage import card_store as _cs
        from pathlib import Path
        events_dir = Path(_cs.WORKS_DIR) / str(work_id) / "events"

    try:
        out["facts"] = fact_store.rebuild_work(store, work_id, events_dir, since=since)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy_runner] 重建数据集失败 work=%s: %s", work_id, exc)
        _record_error(work_id, stage="bootstrap", exc=exc,
                      detail={"phase": "rebuild"})

    pset = policy_set.dispatcher()
    longest = max([p.window_sec or 0 for p in pset.policies() if p.active] or [0])
    win = longest if longest > 0 else (time_now() - since)
    try:
        facts = store.query(work_id, since=time_now() - win)
        out["seeded"] = pset.seed(work_id, facts)
        fired = pset.check_all(work_id)
        out["fired"] = len(fired)
        if fired:
            try:
                from . import intervention_runner
                intervention_runner.record_fired(work_id, fired)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[policy_runner] 回溯落账失败 work=%s: %s",
                               work_id, exc)
                _record_error(work_id, stage="bootstrap", exc=exc,
                              detail={"phase": "record"})
            _spawn(work_id, fired)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy_runner] 回溯建本子失败 work=%s: %s", work_id, exc)
        _record_error(work_id, stage="bootstrap", exc=exc,
                      detail={"phase": "seed"})
    return out


def time_now() -> float:
    import time
    return time.time()
