"""建卡点火调度器：建卡不再「立即起跑」，进队列按容量点火。

[2026-10-19 · 5704 承载力迭代 1/N]

## 为什么要有它

`card_store.create_card` 里原本直接 ``auto_driver.kick()`` —— 建一张卡就立刻起一个
回合。实测 k3 在几分钟内建出 104 张卡，于是 104 个回合同时起跑：端点被打爆、
进程被 safe-delete 守卫杀掉、前端卡死。

**行为不变，只改「同时几个在跑」**：卡照样建、目标照样设、照样会跑，
只是从「瞬间全点火」变成「排队按容量点火」。

## 怎么工作

- 入队：``enqueue(work_id, card_id)``，**同一张卡在队列里只排一次**（去重）
- 出队：N 个 worker 各取一个，调 ``auto_driver.kick``，**await 整个回合**再取下一个
  ⇒ N 个 worker ＝ 同时最多 N 个回合在跑
- 容量：``config.models.yaml`` 的 ``concurrency.default``（缺省 3）
- 放弃：``kick`` 返回 None（卡已终止 / 已有在途 / 非 auto）⇒ 不占槽，直接取下一个

## 不做的事

- 不做优先级：先到先得，谁先建谁先跑（父卡建完自己的活，子卡按建卡顺序排队）
- 不做超时：回合本身的兜底在 gateway（流静默 600s）
- 不持久化队列：进程重启丢队列，但卡的 ``goal_state=active`` 还在，下一次
  唤醒/建卡会重新入队
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 队列（懒建：必须在事件循环内创建）
_queue: Optional[asyncio.Queue] = None
#: 在队/在跑去重集合，key = (work_id, card_id)
_pending: set[tuple[str, str]] = set()
#: worker 任务句柄（防 GC）
_workers: list[asyncio.Task] = []
#: 同时最多几个回合在跑
max_turns: int = 3


def _get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def enqueue(work_id: str, card_id: str) -> bool:
    """把一张卡排进点火队列。已在队/在跑 ⇒ 返回 False（不重复排）。

    **调度器未启动**（测试 / 脚本 / 未走 lifespan）⇒ 退回直点，保证旧路径逐字可用：
    那时没有 worker 来消费队列，入队等于把卡饿死。
    """
    if not _workers:
        try:
            from . import auto_driver
            return auto_driver.kick(work_id, card_id) is not None
        except Exception:  # noqa: BLE001 直点失败按"未起跑"回报
            logger.exception("[sched] 直点退回失败 work=%s card=%s", work_id, card_id)
            return False
    key = (str(work_id), str(card_id))
    if key in _pending:
        return False
    _pending.add(key)
    try:
        _get_queue().put_nowait(key)
    except Exception:  # noqa: BLE001 入队失败不阻断建卡本身
        _pending.discard(key)
        logger.exception("[sched] 入队失败 work=%s card=%s", work_id, card_id)
        return False
    return True


async def _worker(index: int) -> None:
    """一个点火槽位：取卡 → kick → **等这个回合跑完** → 再取下一张。"""
    from . import auto_driver  # 延迟 import 避循环

    q = _get_queue()
    while True:
        work_id, card_id = await q.get()
        try:
            _pending.discard((work_id, card_id))
            task = None
            try:
                task = auto_driver.kick(work_id, card_id)
            except Exception:  # noqa: BLE001 单卡点火失败不拖垮 worker
                logger.exception("[sched] 点火异常 work=%s card=%s", work_id, card_id)
            if task is not None:
                # 关键：**等回合结束**才释放槽位——这就是并发上限的实现方式
                try:
                    await task
                except Exception:  # noqa: BLE001 回合内异常已由 runner 记录
                    pass
        finally:
            q.task_done()


def start(limit: int = 3) -> None:
    """启动 worker（幂等）。``limit`` ＝ 同时最多几个回合在跑。"""
    global max_turns
    max_turns = max(1, int(limit or 3))
    if _workers:
        return
    loop = asyncio.get_event_loop()
    for i in range(max_turns):
        _workers.append(loop.create_task(_worker(i), name=f"auto-sched-{i}"))
    logger.info("[sched] 启动 %d 个点火槽位", max_turns)


async def stop() -> None:
    """停 worker（关停时调；队列里没跑完的卡靠 goal_state 下次重入队）。"""
    while _workers:
        t = _workers.pop()
        t.cancel()
    _pending.clear()


def stats() -> dict[str, Any]:
    """给日志/诊断用：队列长度、在跑数、容量。"""
    q = _get_queue()
    return {
        "queued": q.qsize(),
        "pending": len(_pending),
        "max_turns": max_turns,
        "workers": len(_workers),
    }
