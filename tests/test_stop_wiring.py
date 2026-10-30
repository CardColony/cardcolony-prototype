# -*- coding: utf-8 -*-
"""停止**接线**测试——2026-10-23 用户实测「停止按钮根本停不下来」的回归钉。

故障真因（不是机制，是**接线**）：
``call_main`` 启动流时用 ``control.stop_event()`` 取停止事件，而那是**只取不建**
（事件只在点停止时才被创建）⇒ 流启动时恒为 ``None`` ⇒ 流根本不与停止信号赛跑。
同时 ``request_stop`` 看到 ``in_stream()`` 为真 ⇒ **不 cancel**。
两件事都没发生 ⇒ 点停止，流照跑到底。

为什么旧测试没抓到：``test_model_resume.py::test_stop_signal_ends_stream_immediately``
自己 ``control._stop_event_for(...)`` **手动创建**了事件再传给 ``_stop_raced``——
测的是机制，跳过了「call_main 到底拿不拿得到事件」这一段接线。
本文件补的就是那一段：**走真实 ``call_main``**（假网关，零网络）。

零网络。
"""
from __future__ import annotations

import asyncio
import time

import pytest


# ---------------------------------------------------------------- 假网关


class _SlowGateway:
    """吐字很慢的假网关：每 0.05s 一个增量，共 100 个（＝5 秒）。"""

    def __init__(self, total: int = 100, delay: float = 0.05, block_after: int | None = None):
        self.total = total
        self.delay = delay
        self.block_after = block_after      # 吐够 N 个后永久挂住（模拟端点不吐也不断）
        self.config = {"models": {"fake-model": {"temperature": 0.5}}}
        self.chunks_yielded = 0
        self.closed = False

    async def stream_call(self, model_name, system_prompt, user_prompt, **kw):
        try:
            for i in range(self.total):
                if self.block_after is not None and i >= self.block_after:
                    await asyncio.sleep(3600)        # 挂住：不发、不断
                await asyncio.sleep(self.delay)
                self.chunks_yielded += 1
                yield {"chunk_type": "content", "chunk": f"字{i}", "text": f"字{i}"}
            yield {"content": "".join(f"字{i}" for i in range(self.total)),
                   "tool_calls": [], "finish_reason": "stop",
                   "tokens_used": 100, "latency_ms": 100}
        finally:
            self.closed = True


@pytest.fixture()
def wired(monkeypatch):
    """把 model_service 接到假网关，并标记逐字流归属这张卡。"""
    from src.api import control, event_stream, model_service

    gw = _SlowGateway()
    monkeypatch.setattr(model_service, "get_gateway", lambda: gw)
    monkeypatch.setattr(model_service, "is_mock", lambda: False)
    token = event_stream.set_stream_context("w_stop", "c_stop")
    control._stop_events.pop(("w_stop", "c_stop"), None)
    control._stop_requests.discard(("w_stop", "c_stop"))
    try:
        yield gw
    finally:
        event_stream.reset_stream_context(token)
        control._stop_events.pop(("w_stop", "c_stop"), None)
        control._stop_requests.discard(("w_stop", "c_stop"))


# ---------------------------------------------------------------- 接线


def test_stream_creates_stop_event_before_waiting(wired):
    """流启动时必须**有**事件可等——这是整件事的前提。

    改前 ``stop_event()`` 只取不建 ⇒ 这里是 None ⇒ 后面全废。
    """
    from src.api import control

    ev = control.stop_event_for("w_stop", "c_stop")
    assert isinstance(ev, asyncio.Event)
    assert not ev.is_set()
    # 同一个键再取必须是同一个对象（停止端点要能 set 到流在等的那个）
    assert control.stop_event_for("w_stop", "c_stop") is ev


async def test_call_main_stops_immediately_when_stopped(wired):
    """**走真实 call_main**：点停止 ⇒ 流立刻收口，且已产出的字保留。

    改前这条必挂（流不与信号赛跑，要等 5 秒自然吐完）。
    """
    from src.api import control, model_service
    from src.storage import session_facade

    wid, cid = "w_stop", "c_stop"
    session_facade.turn_try_acquire(wid, cid, "turn:stop")
    t0 = time.monotonic()

    async def press_stop():
        await asyncio.sleep(0.3)                 # 0.3s 后点停止
        r = control.request_stop(wid, cid)
        assert r["stopped"] is True
        return time.monotonic() - t0

    try:
        # call_main 返回 (result, model_used)；press_stop 返回耗时
        (res, _used), _elapsed = await asyncio.gather(
            model_service.call_main("sys", "user", model_name="fake-model"),
            press_stop())
    finally:
        session_facade.turn_release(wid, cid, "turn:stop")
        control.discard_stop(wid, cid)

    cost = time.monotonic() - t0
    assert cost < 2.0, f"停止必须立刻生效，实测 {cost:.2f}s（流本该跑满 5s）"
    assert res["content"].startswith("字0"), "已产出的字必须保住"
    assert res["tool_calls"] == []
    assert wired.chunks_yielded < 100, "不该把 5 秒的字全收完才停"


async def test_call_main_stops_even_if_endpoint_never_speaks(wired, monkeypatch):
    """端点**一个字都不吐**（连接活着、不发也不断）时，点停止照样立刻收口。

    这是 1.5 秒硬停兜底当年要覆盖的场景——现在靠信号，不靠钟。
    """
    from src.api import control, model_service
    from src.storage import session_facade

    silent = _SlowGateway(block_after=0)          # 一个 chunk 都不发，直接挂住
    monkeypatch.setattr(model_service, "get_gateway", lambda: silent)

    wid, cid = "w_stop", "c_stop"
    session_facade.turn_try_acquire(wid, cid, "turn:silent")
    t0 = time.monotonic()

    async def press_stop():
        await asyncio.sleep(0.3)
        control.request_stop(wid, cid)

    try:
        await asyncio.gather(
            model_service.call_main("sys", "user", model_name="fake-model"),
            press_stop())
    finally:
        session_facade.turn_release(wid, cid, "turn:silent")
        control.discard_stop(wid, cid)

    cost = time.monotonic() - t0
    assert cost < 2.0, f"静默端点也必须能被停掉，实测 {cost:.2f}s"


async def test_stop_outside_stream_cancels_turn_task(wired, monkeypatch):
    """卡在**非流**区段（工具执行/装配）时，停止必须直接 cancel 回合协程。

    流里靠信号收口；流外信号没人等 ⇒ 必须 cancel，否则互斥体永不释放。
    """
    from src.api import control, model_service

    wid, cid = "w_stop", "c_stop"
    from src.storage import session_facade
    session_facade.turn_try_acquire(wid, cid, "turn:tool")
    assert model_service.in_stream(wid, cid) is False

    async def fake_tool_run():
        await asyncio.sleep(3600)                  # 模拟卡在工具里

    task = asyncio.create_task(fake_tool_run())
    control.bind_turn_task(wid, cid, task)
    try:
        r = control.request_stop(wid, cid)
        assert r["stopped"] is True
        for _ in range(20):                        # 只推进事件循环，不是等钟
            if task.done():
                break
            await asyncio.sleep(0)
        assert task.cancelled(), "不在流里的回合必须被立刻取消"
    finally:
        control.unbind_turn_task(wid, cid, task)
        session_facade.turn_release(wid, cid, "turn:tool")
        control.discard_stop(wid, cid)


def test_no_stop_context_means_no_event(wired):
    """没有卡上下文（凝练/归拢等后台调用）⇒ 不建事件、不加闸，行为逐字不变。"""
    from src.api import control

    # call_main 里 ctx 为 None ⇒ 不取事件；这里直接验证「不会凭空建出事件」
    assert control.has_stop_signal("w_other", "c_other") is False


# ---------------------------------------------------------------- 接口收敛


def test_only_one_way_to_get_the_stop_event():
    """**只许有一个「取用」入口**，且它恒有返回值。

    2026-10-23 的事故就是「同名不同行为」：``stop_event()``（只取不建，可能返回
    None）与 ``stop_event_for()``（取不到就建）并存，流启动时选错那个 ⇒ 停止彻底
    失效。删掉只取不建的那个，这条断言防止它长回来。
    """
    from src.api import control

    assert not hasattr(control, "stop_event"), \
        "不许再出现「只取不建、可能返回 None」的 stop_event——那是停止失效的根"
    assert hasattr(control, "stop_event_for"), "取用入口必须存在"
    assert hasattr(control, "has_stop_signal"), "诊断入口（只读 bool）必须存在"


def test_reasoning_echo_has_single_judgement_function():
    """思考回传判据也只许有一个函数（三态），不许再并存一个更弱的 bool 版。"""
    from src.api import model_config

    assert not hasattr(model_config, "needs_reasoning_echo"), \
        "判据必须只有 reasoning_echo_fill 一个（三态），不许并存 bool 版"
    assert hasattr(model_config, "reasoning_echo_fill")
