# -*- coding: utf-8 -*-
"""Windows 事件循环工厂（给 uvicorn 的 ``loop=`` 用）。

为什么单独一件：uvicorn 0.36+ 用 ``loop_factory`` 起环，而它的
``asyncio_loop_factory`` 在 Windows 上**写死返回 ProactorEventLoop**
（``uvicorn/loops/asyncio.py``）⇒ ``asyncio.set_event_loop_policy()`` 会被
绕过去。Proactor 的 accept 协程在**连接 churn 很猛**时（性能基准连着起 20 个
浏览器）偶发 ``Accept failed on a socket: [WinError 64]`` ⇒ accept 协程一挂，
**进程还活着但不再监听**（后续请求全是 connection refused）。

⇒ 显式给一个 Selector 环：accept 走普通 select，不经 IOCP，没这个坑。
★ 只给**隔离开发实例**用；生产 5700 的事件循环策略不动（那是重启项，
  按项目规则请示）。
"""
from __future__ import annotations

import asyncio


def selector_loop_factory(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """返回 Selector 事件循环（Windows 上避开 Proactor accept 的 WinError 64）。"""
    return asyncio.SelectorEventLoop()
