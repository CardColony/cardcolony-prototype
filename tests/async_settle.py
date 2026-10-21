# -*- coding: utf-8 -*-
"""测试侧等待原语：等**异步链路自己收口**，不在等待期间反复读目标文件。

────────────────────────────────────────────────────────────────────
为什么需要（5713 · M2「4 条自不稳」）

这批用例的断言目标几乎都落在「某个异步任务把结果写完」上（置脏合并落盘、
回合收口落 assistant 块、S4 自动回发落收件箱）。旧写法是**固定窗口**：

    for _ in range(80):
        await asyncio.sleep(0.05)          # 赌 4 秒内必然发生
        blocks = await session_facade.read(wid, nid)   # 每 50ms 读一次文件
        if any(...): break

两处错叠在一起：
  1. 窗口是拍的不是等的——机器一慢（Defender 扫 tmp、pytest tmp 目录在 D:）
     就可能跑不完，「这次比上次红多了几条」于是永不可用；
  2. **等待本身制造了故障**——Windows 的 ``open()`` 不带 FILE_SHARE_DELETE，
     读句柄在，``os.replace(tmp, 目标)`` 就回 ``WinError 5``。产品侧的重试是
     有界的（``session_facade``：5 次／约 1.2 秒），压不住一个 50ms 周期的读
     循环。同一机制在 ``session_facade._atomic_write_text`` 的注释里有本仓
     自己的实测记录：「同一用例连跑 6 次挂 3 次（rename 一次，而测试每 50ms
     读一次会话文件）」。``board._atomic_write_json`` 的 ``os.replace`` 更是
     裸的、无重试——被读一次就死，flush 任务当场异常退出，盘上永远不达预期。

故改用两种等待：

  • :func:`settle`      —— 等「本用例启的这批异步任务全部跑完」
                          （循环条件＝**没有未完成的任务**，即「收口」本身）；
                          等待期间**不读任何文件**，收口后再读一次。
  • :func:`wait_until`  —— 等一个**纯查询谓词**成立，且该谓词必须**就是**
                          后面要断言的那条不变量（用于 SQLite 收件箱状态：
                          查库不碰会话文件，不产生上面那种争用）。

判据一个字没放宽：原来断言什么，现在仍断言什么，只是把「赌时间」换成「等事实」。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Iterable, Optional, Set


def snapshot() -> Set["asyncio.Task"]:
    """记下当下的任务集合——当作「本用例接下来新起的任务」的基线。

    用法：动作**之前** snapshot()，动作之后 settle(since=那个集合)。
    """
    return set(asyncio.all_tasks())


def _pending(since: Optional[Iterable["asyncio.Task"]] = None) -> list:
    me = asyncio.current_task()
    baseline = set(since) if since is not None else set()
    out = []
    for t in asyncio.all_tasks():
        if t is me or t.done() or t in baseline:
            continue
        out.append(t)
    return out


async def settle(since: Optional[Iterable["asyncio.Task"]] = None,
                 rounds: int = 400,
                 per_round_timeout: float = 30.0) -> int:
    """等本批异步任务跑完；返回等过的轮数（>1 表示中途又起了新任务）。

    ``since``：只等这批任务（``snapshot()`` 的返回）；缺省＝等除自身外全部。
    ``per_round_timeout`` 只是**防挂死**的上界（不是等多久的判据）：正常路径
    由「没有未完成的任务」这一条件返回，超时说明真有任务永不收口，应当报错
    而不是静默放行。

    任务抛异常时**原样上抛**——异步链路的真实错因（例：WinError 5）不该被
    后面的断言失败掩盖成一个难查的「块没出现」。
    """
    for i in range(rounds):
        pend = _pending(since)
        if not pend:
            return i
        done, _still = await asyncio.wait(pend, timeout=per_round_timeout)
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc is not None:
                raise exc
    raise AssertionError(
        "异步链路未收口：仍有 %d 个任务未完成（等了 %d 轮）" % (len(_pending(since)), rounds))


async def wait_until(pred: Callable[[], Any],
                     what: str = "",
                     rounds: int = 500,
                     interval: float = 0.02) -> Any:
    """等一个**纯查询**谓词成立并返回其值；超时抛 AssertionError。

    ``pred`` 可以是普通函数或协程函数；**必须是纯查询**（不落盘、不读会话
    文件），且必须**就是**后面要断言的那条不变量——否则又回到「谓词≠断言」。
    """
    last: Any = None
    for _ in range(rounds):
        last = pred()
        if asyncio.iscoroutine(last) or isinstance(last, Awaitable):
            last = await last
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError("%s：等待超时（最后取值 %r）" % (what or "谓词未成立", last))