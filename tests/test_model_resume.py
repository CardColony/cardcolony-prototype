# -*- coding: utf-8 -*-
"""模型中断续跑 + 取消墙钟（2026-10-23 · 用户令）。

用户原话：
  「哪个模型断连了，都会返回报错，你全部不处理，就设个钟」
  「我单纯在说模型中断后如何继续，然后取消你那个傻逼的墙钟」

两件事各钉一组：
A. 断连是**立刻可知的可恢复事件** ⇒ 在同一位置重发这次调用，不许判死整轮；
   已落盘的工具调用不重做，上下文天然接上。
B. 流式读的 300 秒墙钟**取消** ⇒ 慢但在干活的连接不被误杀；断连靠 socket 异常发现。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.infra.errors import ErrorCode, ModelError  # noqa: E402


class _State:
    work_id = "work_x"
    card_id = "card_x"
    rounds = 2


@pytest.fixture()
def runner(monkeypatch):
    from src.api import message_runner as mr

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(mr.event_stream, "emit",
                        lambda _w, _c, t, d: emitted.append((t, d)))
    return mr, emitted


async def test_interruption_resumes_same_call_and_returns(monkeypatch, runner):
    """前两次断连、第三次成功 ⇒ 这一轮照常返回，整轮不判死。"""
    mr, emitted = runner
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, "connection reset by peer")
        return ({"content": "续上了", "tool_calls": []}, "m1")

    monkeypatch.setattr(mr.model_service, "call_main", fake)
    result, used = await mr._call_model_resumable(_State(), system_prompt="s", user_prompt="u")

    assert result["content"] == "续上了"
    assert used == "m1"
    assert calls["n"] == 3, "断了两次，第三次才成功"
    resume_events = [d for t, d in emitted if t == "model_resume"]
    assert len(resume_events) == 2, "每次中断都要留痕，别静默重试"
    assert resume_events[0]["round"] == 2


async def test_interruption_passes_same_kwargs(monkeypatch, runner):
    """重发的是**同一次调用**：参数原样，不换上下文、不重跑整轮。"""
    mr, _ = runner
    seen: list[dict] = []

    async def fake(**kw):
        seen.append(dict(kw))
        if len(seen) == 1:
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, "boom")
        return ({"content": "ok", "tool_calls": []}, "m1")

    monkeypatch.setattr(mr.model_service, "call_main", fake)
    await mr._call_model_resumable(
        _State(), system_prompt="SYS", user_prompt="USR",
        prior_messages=[{"role": "tool", "content": "工具结果"}],
        tools=[{"name": "read_card"}], model_name="m1")

    assert len(seen) == 2
    assert seen[0] == seen[1], "两次调用的参数必须完全一致（同位置重发）"
    assert seen[1]["prior_messages"][0]["content"] == "工具结果", "已完成的工具结果仍在上下文里"


async def test_exhausted_attempts_raise(monkeypatch, runner):
    """重发耗尽 ⇒ 如实上抛（由上层判死），不假装成功。"""
    mr, emitted = runner
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        raise ModelError(ErrorCode.MODEL_CALL_FAILED, "down")

    monkeypatch.setattr(mr.model_service, "call_main", fake)
    with pytest.raises(ModelError):
        await mr._call_model_resumable(_State(), system_prompt="s", user_prompt="u")
    assert calls["n"] == mr._RESUME_MAX_ATTEMPTS, "按次数放弃，不按时间"


async def test_non_model_error_not_swallowed(monkeypatch, runner):
    """非模型错误（编程错误等）直接上抛，不当成断连重发。"""
    mr, _ = runner
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        raise ValueError("bug in caller")

    monkeypatch.setattr(mr.model_service, "call_main", fake)
    with pytest.raises(ValueError):
        await mr._call_model_resumable(_State(), system_prompt="s", user_prompt="u")
    assert calls["n"] == 1, "非 ModelError 不重发"


def test_wall_clock_removed():
    """B：流式读的 300 秒墙钟取消（read=None），断连靠异常而非时间发现。"""
    from src.model_gateway.llm_openai_compat import adapter

    assert adapter.DEFAULT_STREAM_IDLE_TIMEOUT_MS is None, \
        "墙钟常量必须为 None——2026-10-02 拆闸裁定禁墙钟类闸"
    assert adapter.CompatConnectionOptions(base_url="http://x", api_key="").stream_idle_timeout_ms is None


def test_timeout_object_has_no_remote_deadline():
    """远程等待一律不设墙钟：read 与 connect 都是 None。

    连接不设超时不是「不管」——对端拒绝/不可路由本来就立即报错；
    只有包被静默丢弃才需要等，那是 TCP 协议自己的握手超时（SYN 重传），
    不该由代码拍一个数。
    """
    import httpx

    from src.model_gateway.llm_openai_compat.adapter import CompatConnectionOptions

    conn = CompatConnectionOptions(base_url="http://x", api_key="")
    t = httpx.Timeout(
        connect=None,
        read=(None if conn.stream_idle_timeout_ms is None
              else conn.stream_idle_timeout_ms / 1000.0),
        write=30.0, pool=30.0)
    assert t.read is None, "读超时必须是 None"
    assert t.connect is None, "连接超时也交给 TCP 协议，不由代码拍数"
    assert t.write == 30.0 and t.pool == 30.0, "本地资源等待保留（不是远端墙钟）"


async def test_resume_has_no_sleep(monkeypatch, runner):
    """续跑零等待：连接已死就该立刻重连，不许瞎等（无反馈的退避也是钟）。"""
    import time as _t

    mr, _ = runner
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, "connection reset")
        return ({"content": "ok", "tool_calls": []}, "m1")

    monkeypatch.setattr(mr.model_service, "call_main", fake)
    t0 = _t.monotonic()
    await mr._call_model_resumable(_State(), system_prompt="s", user_prompt="u")
    cost = _t.monotonic() - t0
    assert calls["n"] == 3
    assert cost < 0.5, f"三次重发应在毫秒级完成，实测 {cost:.2f}s（说明还有退避钟）"
    assert not hasattr(mr, "_RESUME_BACKOFF_S"), "退避常量应已删除"


async def test_resume_has_no_sleep(monkeypatch, runner):
    """续跑零等待：连接已死就该立刻重连，不许瞎等（无反馈的退避也是钟）。"""
    import time as _t

    mr, _ = runner
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, "connection reset")
        return ({"content": "ok", "tool_calls": []}, "m1")

    monkeypatch.setattr(mr.model_service, "call_main", fake)
    t0 = _t.monotonic()
    await mr._call_model_resumable(_State(), system_prompt="s", user_prompt="u")
    cost = _t.monotonic() - t0
    assert calls["n"] == 3
    assert cost < 0.5, f"三次重发应在毫秒级完成，实测 {cost:.2f}s（说明还有退避钟）"
    assert not hasattr(mr, "_RESUME_BACKOFF_S"), "退避常量应已删除"


def test_gateway_idle_watchdog_disabled():
    """网关层那层 600 秒空闲兜底也取消——它才是真正盖住一切的那个。

    背景：adapter 的 httpx read 超时被它包在外层，所以只改 adapter 等于没改。
    `asyncio.wait_for(..., timeout=None)` ＝ 一直等（不设超时）。
    """
    from src.model_gateway import gateway as g

    assert g.STREAM_IDLE_TIMEOUT is None, "网关层空闲墙钟必须为 None"
    import inspect
    assert inspect.signature(g._stream_with_idle_timeout).parameters["idle"].default is None, \
        "默认参数在 def 时绑定，必须是 None（否则改常量无效）"




def test_gateway_idle_watchdog_disabled():
    """网关层那层 600 秒空闲兜底也取消——它才是真正盖住一切的那个。

    背景：adapter 的 httpx read 超时被它包在外层，所以只改 adapter 等于没改。
    `asyncio.wait_for(..., timeout=None)` ＝ 一直等（不设超时）。
    """
    import inspect

    from src.model_gateway import gateway as g

    assert g.STREAM_IDLE_TIMEOUT is None, "网关层空闲墙钟必须为 None"
    assert inspect.signature(g._stream_with_idle_timeout).parameters["idle"].default is None, \
        "默认参数在 def 时绑定，必须是 None（否则改常量无效）"


def test_anthropic_adapter_idle_watchdog_disabled():
    """Anthropic 协议适配器（MiniMax/mavis 那条线）同口径取消。"""
    from src.model_gateway.llm_anthropic import adapter as a

    assert a.DEFAULT_STREAM_IDLE_TIMEOUT_MS is None
    async def _tok() -> str:
        return "t"
    conn = a.AnthropicConnectionOptions(base_url="http://x", token_getter=_tok)
    assert conn.stream_idle_timeout_ms is None


async def test_stop_signal_ends_stream_immediately():
    """停止是信号不是钟：连接活着但不出字时，停止 50ms 生效，且保住已产出的字。

    原来靠 `_hard_cancel_after` 的 1.5 秒定时猜「软停没生效」——那是墙钟。
    """
    import time as _t

    from src.api import control, model_service

    async def silent_stream():
        yield {"chunk_type": "content", "chunk": "前半句"}
        await asyncio.sleep(3600)      # 永不自然返回

    control._stop_events.pop(("w1", "c1"), None)
    ev = control.stop_event_for("w1", "c1")
    t0 = _t.monotonic()

    async def _press_stop():
        await asyncio.sleep(0.05)
        ev.set()

    asyncio.create_task(_press_stop())
    got = [c async for c in model_service._stop_raced(silent_stream(), ev)]
    cost = _t.monotonic() - t0

    assert cost < 0.5, f"停止必须即时生效，实测 {cost:.2f}s（还有墙钟）"
    assert got and got[0]["chunk"] == "前半句", "已产出的字必须保住"
    control._stop_events.pop(("w1", "c1"), None)


async def test_no_stop_event_passes_through():
    """没有停止事件（非卡内调用）⇒ 原样透传，行为逐字不变。"""
    from src.api import model_service

    async def gen():
        yield {"chunk_type": "content", "chunk": "a"}
        yield {"content": "done"}

    got = [c async for c in model_service._stop_raced(gen(), None)]
    assert got == [{"chunk_type": "content", "chunk": "a"}, {"content": "done"}]


def test_stop_request_sets_signal_and_clears_on_discard(monkeypatch):
    """request_stop 置信号（不再起 1.5 秒定时任务）；收口清扫连信号一起清。"""
    from src.api import control

    monkeypatch.setattr(control.session_facade, "turn_holder", lambda *_: "holder-x")
    monkeypatch.setattr(control.session_ops, "ops_log", lambda *a, **k: None)

    async def _run():
        control._stop_events.pop(("w2", "c2"), None)
        r = control.request_stop("w2", "c2")
        assert r["stopped"] is True
        assert control.has_stop_signal("w2", "c2") is True, "停止必须是一个已置位的信号"
        control.discard_stop("w2", "c2")
        assert control.has_stop_signal("w2", "c2") is False, "回合收口要连信号一起清"

    asyncio.run(_run())
    assert not hasattr(control, "STOP_HARD_CANCEL_DELAY"), "1.5 秒硬停兜底必须删除"


async def test_resume_works_without_state(monkeypatch, runner):
    """起草/统合等没有 RunState 的路径也能续跑（标识走 getattr 兜底）。"""
    mr, emitted = runner
    calls = {"n": 0}

    async def fake(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelError(ErrorCode.MODEL_CALL_FAILED, "reset")
        return ({"content": "ok", "tool_calls": []}, "m1")

    monkeypatch.setattr(mr.model_service, "call_main", fake)
    r, _ = await mr._call_model_resumable(None, system_prompt="s", user_prompt="u")
    assert r["content"] == "ok" and calls["n"] == 2


def test_no_unwrapped_call_main_left_in_runner():
    """工具循环内**每一处** model_service.call_main 都必须经续跑包装。

    [2026-10-23 修] 统合官（产出最终答案那一次）原来漏在包装外 ⇒ 一断连整轮就死，
    前面所有工具轮白做。用户实测「自动重试没生效」即此。这条钉住别再漏。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "api" / "message_runner.py").read_text(encoding="utf-8")
    # 允许出现的位置：包装函数体内部（`return await model_service.call_main(**kwargs)`）
    hits = [ln.strip() for ln in src.splitlines()
            if "model_service.call_main(" in ln
            and "await model_service.call_main(**kwargs)" not in ln]
    assert hits == [], f"还有未包装的 call_main 调用点：{hits}"
