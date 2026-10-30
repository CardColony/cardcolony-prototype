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
import logging
from typing import Any, Optional

logger = logging.getLogger("infra.policy_runner")

#: 后台投递任务的强引用（防 GC 中途回收）
_PENDING: set[Any] = set()

#: 是否启用（``ECO_POLICY_OFF=1`` 整体关闭，便于排障）
import os  # noqa: E402


def _off() -> bool:
    return os.environ.get("ECO_POLICY_OFF") == "1"


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

    if fact is None:
        return []

    try:
        fired = policy_set.dispatcher().dispatch(work_id, fact)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy_runner] 条件派发失败 work=%s: %s", work_id, exc)
        return []

    if fired:
        _spawn(work_id, fired)
    return fired


def _spawn(work_id: str, fired: list[dict[str, Any]]) -> None:
    """投递丢后台（无事件循环时放弃——同步上下文里不硬起线程）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[policy_runner] 无事件循环 ⇒ 本次不投递（%d 条）", len(fired))
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
            targets = harness.resolve_targets(
                str(then.get("target") or "self"), ctx)
            if not targets:
                logger.warning("[policy_runner] %s 解析不出投递目标 scope=%s",
                               pid, f.get("scope"))
                continue
            text = harness.render_message({"message": then.get("message") or ""}, ctx)
            wake = bool(then.get("wake", True))
            # [harness 三手段 2026-10-28] message＝进历史（永久）；inject＝只进下一轮
            mode = str(then.get("mode") or "message")
            # [harness 三手段 2026-10-28] message＝进历史（永久）；inject＝只进下一轮
            mode = str(then.get("mode") or "message")
            for t in targets:
                if await harness.deliver(work_id, t, text, rule_id=pid,
                                         wake=wake, mode=mode):
                    n += 1
        except Exception as exc:  # noqa: BLE001 一条失败不拖垮其余
            logger.warning("[policy_runner] 投递失败 %s: %s", pid, exc)
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

    pset = policy_set.dispatcher()
    longest = max([p.window_sec or 0 for p in pset.policies() if p.active] or [0])
    win = longest if longest > 0 else (time_now() - since)
    try:
        facts = store.query(work_id, since=time_now() - win)
        out["seeded"] = pset.seed(work_id, facts)
        fired = pset.check_all(work_id)
        out["fired"] = len(fired)
        if fired:
            _spawn(work_id, fired)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy_runner] 回溯建本子失败 work=%s: %s", work_id, exc)
    return out


def time_now() -> float:
    import time
    return time.time()
