# -*- coding: utf-8 -*-
"""[双循环隔离 2026-11-05] HTTP 服务面与跑卡域各挂一条事件循环。

病：runner 回合里的同步 CPU 段（拼上下文/序列化/扇出/会话处理）与
HTTP 全挤**同一条协作式循环**——一段卡住＝所有请求在后面排队
（实测满载时 /api/nav/version 9.34s，正常 27ms；插座活着、进程活着，
纯粹是事件循环被饿死）。

药：
  - **主循环 A**（主线程）：runner/scheduler/harness + 一切状态写与 emit；
  - **服务循环 B**（独立线程）：uvicorn + 静态 + 白名单只读 API + SSE 订阅者。

线程间由 OS 抢占调度 ⇒ B 上的请求永远拿得到时间片，不再被 A 的同步段
饿死（GIL 只影响并行加速，不影响隔离卡顿——秒级饿死变毫秒级抢占）。

边界铁律（施工与验收都按这查）：
  ① 白名单 GET（nav/timeline/stream/tokens/events/…）——全是**无锁读**
     （读路径经核实不碰任何 asyncio.Lock）⇒ B 原生跑；
  ② 其余一切端点（写/emit/孵化/取锁）⇒ ``on_main`` 委托回 A——
     **单写者语义与改前逐字一致**，asyncio.Lock 永远不跨 loop 绑定；
  ③ A→B 只有一条路：event_stream._fanout 按队列属主
     ``call_soon_threadsafe`` 投递（asyncio.Queue 跨 loop put 唤不醒
     消费者，必须回到属主 loop 上 put）；
  ④ ``_MAIN_LOOP`` 未注册时（单测/TestClient/旧启动路径）一切委托
     退化为原地执行 ⇒ 行为与单循环逐字一致。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import re
from typing import Any, Callable

from starlette.requests import Request

logger = logging.getLogger("loops")

_MAIN_LOOP: asyncio.AbstractEventLoop | None = None
_SERVING_LOOP: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def set_serving_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _SERVING_LOOP
    _SERVING_LOOP = loop


def main_loop() -> asyncio.AbstractEventLoop | None:
    return _MAIN_LOOP


def serving_loop() -> asyncio.AbstractEventLoop | None:
    return _SERVING_LOOP


# ---------------------------------------------------------------- 迟滞仪表
# 「循环被饿死」的直接证据＝回调实际触发时刻 − 计划时刻。
# 改造前实测满载时这个值是秒级（请求 9.34s）；改造后 A 上仍可能有毫秒~百毫秒
# 级抖动（GIL 抢占），但 B 的请求面不再受它影响。/api/health 报告它 ⇒ 验收有数。
_LAG_MS: float = 0.0


def loop_lag_ms() -> float:
    return _LAG_MS


def start_lag_monitor(loop: asyncio.AbstractEventLoop, interval: float = 2.0) -> None:
    """周期性测「sleep(interval) 实际睡了多久」，超出的就是循环迟滞。"""
    async def _beat() -> None:
        global _LAG_MS
        t = loop.time()
        while True:
            await asyncio.sleep(interval)
            now = loop.time()
            _LAG_MS = max(0.0, (now - t - interval) * 1000.0)
            t = now
    try:
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_beat()))
    except RuntimeError:  # noqa: BLE001 loop 已关
        pass


async def _prefetch_request_body(a: tuple, kw: dict) -> None:
    """端点即将挪到 A 上跑——ASGI ``receive`` 绑在 B loop 的传输层上。

    先在 B（调用方 loop）把请求体消费进 ``Request._body`` 缓存，
    否则端点在 A 里 ``await request.json()/body()/form()/stream()``
    会等 B 的挂起原语 → RuntimeError（实测 PUT 全被吞成
    400 "请求体不是合法 JSON"，保存类端点全灭）。
    预读失败静默：让端点按原有语义自行报错，不在包装层发明新行为。
    """
    for v in (*a, *kw.values()):
        if isinstance(v, Request):
            try:
                await v.body()
            except Exception:  # noqa: BLE001
                pass
            return


def on_main(fn: Callable[..., Any]) -> Callable[..., Any]:
    """装饰器：async 函数——当前不在主循环时，整段协程提交回主循环执行。

    主循环未注册（测试/旧路径）或已在主循环上 ⇒ 原地 await，零开销零差异。
    """
    @functools.wraps(fn)
    async def _w(*a: Any, **kw: Any) -> Any:
        loop = _MAIN_LOOP
        if loop is None:
            return await fn(*a, **kw)
        try:
            if asyncio.get_running_loop() is loop:
                return await fn(*a, **kw)
        except RuntimeError:  # 无线程本地运行环（极端兜底）
            pass
        await _prefetch_request_body(a, kw)
        fut = asyncio.run_coroutine_threadsafe(fn(*a, **kw), loop)
        return await asyncio.wrap_future(fut)
    return _w


def run_sync_on_main(fn: Callable[..., Any], *a: Any, **kw: Any) -> "concurrent.futures.Future[Any]":
    """同步函数提交到主循环线程执行（call_soon_threadsafe 回调），
    返回 concurrent.Future。主循环未注册 ⇒ 原地执行。"""
    cf: "concurrent.futures.Future[Any]" = concurrent.futures.Future()
    loop = _MAIN_LOOP
    if loop is None:
        try:
            cf.set_result(fn(*a, **kw))
        except BaseException as exc:  # noqa: BLE001
            cf.set_exception(exc)
        return cf

    def _run() -> None:
        if not cf.set_running_or_notify_cancel():
            return
        try:
            cf.set_result(fn(*a, **kw))
        except BaseException as exc:  # noqa: BLE001
            cf.set_exception(exc)

    try:
        loop.call_soon_threadsafe(_run)
    except RuntimeError as exc:  # noqa: BLE001 主循环已关
        cf.set_exception(exc)
    return cf


def _on_main_sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    """on_main 的同步端点版：FastAPI 见包装是协程函数 ⇒ 直接 await；
    内部把同步体经 run_sync_on_main 放到主循环线程上跑。"""
    @functools.wraps(fn)
    async def _w(*a: Any, **kw: Any) -> Any:
        loop = _MAIN_LOOP
        if loop is None:
            return fn(*a, **kw)
        try:
            if asyncio.get_running_loop() is loop:
                return fn(*a, **kw)
        except RuntimeError:
            return fn(*a, **kw)
        await _prefetch_request_body(a, kw)
        return await asyncio.wrap_future(run_sync_on_main(fn, *a, **kw))
    return _w


# ---------------------------------------------------------------- 读白名单
# 「在 B 上原生跑」的端点：全部经核实为**无锁读或 SSE 流**——不 emit、
# 不写库、不取 asyncio.Lock。模板里 {param} 归一成 {} 比对。
# 规则：宁可少放（落在 A 上只是不快），不可错放（写路径上 B 会跨 loop 取锁）。
_NATIVE_GET_TPLS: frozenset[str] = frozenset({
    # HTML 页（模板读取，与卡域零交互）
    "/",
    "/settings",
    "/work/{}",
    "/work/{}/card/{}",
    # 导航/健康/总览——UI 高频轮询面
    "/api/nav",
    "/api/nav/children",
    "/api/nav/running",
    "/api/nav/version",
    "/api/health",
    "/api/observe/calls",
    "/api/works",
    "/api/works/{}/pulse",
    # 卡域只读（SQLite 连接 check_same_thread=False + 无锁读）
    "/api/works/{}/canvas",
    "/api/works/{}/cards",
    "/api/works/{}/cards/{}",
    "/api/works/{}/cards/{}/timeline",
    "/api/card/{}/timeline",
    "/api/works/{}/cards/{}/session",
    "/api/works/{}/cards/{}/progress",
    "/api/works/{}/card-messages/unread",
    # SSE 流——订阅队列的**消费方**，必须在 B（生产方经 _fanout 桥过来）
    "/api/works/{}/cards/{}/stream",
    "/api/works/{}/events",
    "/api/works/{}/cards/{}/tokens",
})

_PARAM_RE = re.compile(r"\{[^}]*\}")


def _native_route(route: Any) -> bool:
    """该路由是否允许在 B 上原生跑：仅当**纯 GET 系**且模板在白名单内。"""
    methods = getattr(route, "methods", None) or set()
    if not methods or not methods <= {"GET", "HEAD", "OPTIONS"}:
        return False
    tpl = _PARAM_RE.sub("{}", getattr(route, "path", "") or "")
    return tpl in _NATIVE_GET_TPLS


def _iter_api_routes(routes: Any, _seen: set[int]) -> Any:
    """递归剥出所有真 ``APIRoute``（有 dependant 的那种）。

    新版 FastAPI 的 ``include_router`` 不再把路由平铺拷进 ``app.routes``，
    而是放一个 ``_IncludedRouter`` 委托壳（无 dependant），真端点在
    ``original_router.routes``（含嵌套壳与 ``_low_priority_routes``）。
    只扫 app.routes 表层会漏掉整棵子树——[2026-11-04 事故实证]：
    post_work_message 整场 runner 跑在 B 上，模型限流 Semaphore 绑到 B，
    A 侧全部 "bound to a different event loop"。
    """
    for route in routes or ():
        dep = getattr(route, "dependant", None)
        if dep is not None:
            yield route
            continue
        sub = getattr(route, "original_router", None)
        if sub is not None and id(sub) not in _seen:
            _seen.add(id(sub))
            yield from _iter_api_routes(getattr(sub, "routes", ()), _seen)
            yield from _iter_api_routes(
                getattr(sub, "_low_priority_routes", ()), _seen)


def install_main_dispatch(app: Any) -> int:
    """把非白名单端点的执行包装为「委托回主循环 A」。返回包装条数。

    实现：改 ``route.dependant.call``——FastAPI 请求处理器每次请求对
    ``dependant.call`` 做 iscoroutinefunction 判定并调用 ⇒ 换成 async
    包装后（async 走 run_coroutine_threadsafe，sync 走 run_sync_on_main），
    端点体就回到 A 上跑，签名/依赖注入/返回契约全不变。

    ``_MAIN_LOOP`` 未注册时包装原地执行 ⇒ 单测/TestClient 零差异。
    """
    n = 0
    for route in _iter_api_routes(getattr(app, "routes", ()), set()):
        dep = getattr(route, "dependant", None)
        if dep is None:
            continue  # 防御：迭代器只吃有 dependant 的，此处冗余保险
        if not getattr(route, "methods", None):
            continue  # WebSocket 等无 HTTP methods 路由：其 receive/send 全程绑 B，委托会拆散会话
        if _native_route(route):
            continue
        orig = dep.call
        if getattr(orig, "_on_main_wrapped", False):
            continue  # 幂等：重复安装不叠加
        dep.call = on_main(orig) if asyncio.iscoroutinefunction(orig) else _on_main_sync(orig)
        dep.call._on_main_wrapped = True  # type: ignore[attr-defined]
        n += 1
    if n:
        logger.info("[loops] 主循环委托已安装：%s 个非白名单端点 → A", n)
    return n
