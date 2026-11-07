# -*- coding: utf-8 -*-
"""SSE 连接预算网（[P28 · 2026-10-26]）

## 病根（实测，不是推测）

浏览器同源 HTTP/1.1 只有 **6 条**并发连接（**不是我们设的**，是 HTTP/1.1＋浏览器的
硬上限）。而改前的前端连接数**随并行度线性增长**：

| 页面 | 改前占几条 | 10 张卡并行时 |
|---|---|---|
| 画布「监控态」 | **每张被盯的卡 1 条**（`/cards/{cid}/stream`） | 10 条 ⇒ 必然打满 |
| 对话页 | **每张卡 2 条**（`/stream` ＋ `/tokens`） | 切 3 张卡就 6 条占满 |

⇒ 普通请求（timeline / card / nav）**排队拿不到连接** ⇒ fetch 抛 TypeError
⇒ 弹「网络错误」、切卡卡死。

关键事实（canvas.js 自己写着）：画布那条每卡 SSE **不承载内容**——内容是 3 秒
轮询拉的，SSE 只当「有变化了，去重拉」的触发器 ⇒ 白占连接。

## 解法：连接数与并行度解耦

1. **工作级流带卡级帧**（`?cards=1` opt-in）⇒ 一条连接带全部卡的帧；
2. **画布不再每卡开流**（改吃工作级流）⇒ 盯 10 张卡也只占 **1 条**；
3. **每卡 2 条合并成 1 条**（`/stream?tokens=1`，帧带 `ch` 标记）⇒ 对话页 3 条→2 条。

## 本网钉住的不变量

| 编号 | 不变量 |
|---|---|
| C1 | 合并流：**一条连接**同时带过程帧(ch="e")与逐字帧(ch="t") |
| C2 | 不带 `tokens=1` 时帧形态**一字不改**（无 `ch`）⇒ 老消费方零影响 |
| C3 | 工作级 `?cards=1` 带卡级帧（顶层 `card`）；**不带则不带**（opt-in 隔离） |
| C4 | **逐字帧不进工作级**（每 token 一帧会把工作级队列打爆，画布也不需要） |
| C5 | 工作级原生帧（card_changed/card_message/card_running）**顶层没有** `card` |
| C6 | 画布**没有**「每卡一条 EventSource」⇒ 连接数与卡数解耦 |
| C7 | 对话页**只开一条** EventSource，且带 `tokens=1` ⇒ 每卡占 1 条而不是 2 条 |

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from src.api import event_stream, routes

ROOT = Path(__file__).resolve().parents[1]
STREAM_JS = (ROOT / "static" / "stream.js").read_text(encoding="utf-8")
CANVAS_JS = (ROOT / "static" / "canvas.js").read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去掉注释（块注释 + 行注释）——源码级断言不能被注释里的旧代码骗过。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    return src


async def _pump(resp, frames: list, stop: asyncio.Event) -> None:
    """把 SSE 响应体里的 data 帧收进 frames。"""
    async for chunk in resp.body_iterator:
        print("[DBG] chunk:", repr(chunk)[:140])
        for line in chunk.splitlines():
            if line.startswith("data: "):
                try:
                    frames.append(json.loads(line[6:]))
                except Exception:  # noqa: BLE001 单帧坏不致命
                    continue
                if stop.is_set():
                    return


async def _wait_frames(frames: list, n: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while len(frames) < n and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def _collect_stream(resp, want: int, action, timeout: float = 5.0) -> list:
    """起一条 SSE → 触发 action → 收回 want 帧（或超时返回已收到的）。"""
    frames: list = []
    stop = asyncio.Event()
    task = asyncio.create_task(_pump(resp, frames, stop))
    await asyncio.sleep(0.15)          # 让订阅者先就位
    print("[DBG] before action, frames:", frames)
    r = action()
    print("[DBG] after action, frames:", frames)
    if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
        await r
    await _wait_frames(frames, want, timeout=timeout)
    stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 关流失败不阻断断言
        pass
    if task.done() and not task.cancelled() and task.exception() is not None:
        raise AssertionError(f"SSE 消费协程炸了：{task.exception()!r}")
    try:
        await resp.body_iterator.aclose()
    except Exception:  # noqa: BLE001
        pass
    return frames


# ── C1 合并流：一条连接带两路 ──────────────────────────────────────────────

async def test_c1_merged_stream_carries_both_channels(work):
    """C1：`?tokens=1` ⇒ **一条**连接同时送过程帧(ch=e)与逐字帧(ch=t)。"""
    wid, cid = work["work_id"], work["work_card_id"]

    def _emit():
        event_stream.emit(wid, cid, "tool_call", {"tool": "x"})
        event_stream.emit_token(wid, cid, "content_chunk", "你好")

    # 直接调路由函数：Query(...) 默认值是 Query 对象而非 0 ⇒ 必须显式传全部参数
    resp = await routes.stream_card_events(wid, cid, 0, 0, 1, 0)
    assert resp.media_type == "text/event-stream"
    frames = await _collect_stream(resp, 2, _emit)

    chs = {f.get("ch") for f in frames}
    assert "e" in chs, f"合并流里没有过程帧：{frames}"
    assert "t" in chs, f"合并流里没有逐字帧：{frames}"
    assert len(chs) == 2, f"只应有 e/t 两种通道标记，实得 {chs}"
    # 逐字帧的正文不能丢
    texts = [f.get("data", {}).get("text") for f in frames if f.get("ch") == "t"]
    assert "你好" in texts, f"逐字帧内容丢了：{frames}"


async def test_c1b_merged_stream_two_cursors_are_independent(work):
    """C1b：两路**各自的游标**独立（from＝过程、fromT＝逐字）⇒ 续传语义与改前一致。"""
    wid, cid = work["work_id"], work["work_card_id"]
    event_stream.emit(wid, cid, "tool_call", {"tool": "before"})
    event_stream.emit_token(wid, cid, "content_chunk", "旧字")

    # 过程游标落在末帧之后 ⇒ 收不到旧**过程**帧；
    # 逐字游标显式给 1 ⇒ **仍然**收得到旧逐字帧（两路游标各管各的）。
    #
    # ⚠ 口径修正（2026-10-26）：原断言写的是 `frames == []`（"一帧都不许有"），
    #   与本用例的 docstring、以及下面 B 段（fromT=1 ⇒ 该收到逐字帧）**三方矛盾**：
    #   A、B 两调用的 fromT 都是 1，实测两者都收到同一枚 ch='t' 的逐字帧
    #   （探针见 docs 里 §P28-c1b 归因）。真正该守的是"**过程**帧不回来"。
    resp = await routes.stream_card_events(wid, cid, 10 ** 9, 1, 1, 0)
    frames = await _collect_stream(resp, 1, lambda: None, timeout=1.0)
    # [死游标 v2 2026-11-09] 死游标不再归零整本重放，而是发**恰好一条**
    # resync 控制帧（前端据此走落盘重建＋游标重锚）——断言修正为：
    # 零**重放**过程帧（旧帧一条都不回来）＋至多一条 type=resync 控制帧。
    replays = [f for f in frames if f.get("ch") == "e" and f.get("type") != "resync"]
    assert not replays, f"过程游标在末帧之后却收到了旧过程帧：{frames}"
    resyncs = [f for f in frames if f.get("type") == "resync"]
    assert len(resyncs) <= 1, f"resync 控制帧至多一条：{frames}"

    resp2 = await routes.stream_card_events(wid, cid, 0, 1, 1, 0)
    frames2 = await _collect_stream(resp2, 1, lambda: None, timeout=1.0)
    assert any(f.get("ch") == "t" for f in frames2), \
        f"逐字游标 fromT=1 却没收不到存量逐字帧：{frames2}"


# ── C2 老形态不变 ─────────────────────────────────────────────────────────

async def test_c2_legacy_stream_shape_unchanged(work):
    """C2：不带 tokens=1 时帧**一字不改**（没有 ch 字段）⇒ 老消费方零影响。"""
    wid, cid = work["work_id"], work["work_card_id"]

    def _emit():
        event_stream.emit(wid, cid, "tool_call", {"tool": "x"})

    resp = await routes.stream_card_events(wid, cid, 0, 0, 0, 0)
    frames = await _collect_stream(resp, 1, _emit)
    assert frames, "老形态流没收到帧"
    for f in frames:
        assert "ch" not in f, f"老形态流不许带 ch 标记：{f}"
        assert set(f.keys()) >= {"seq", "ts", "type", "data"}, f


# ── C3/C4/C5 工作级带卡级帧 ───────────────────────────────────────────────

async def test_c3_work_events_cards_optin(work):
    """C3：`?cards=1` 才带卡级帧；不带则**一条都不带**（opt-in，对话页零浪费）。"""
    wid, cid = work["work_id"], work["work_card_id"]

    # 不带 cards：订阅表里**没有**卡级帧订阅者
    resp = await routes.stream_work_events(wid, 0)
    frames = await _collect_stream(resp, 1, lambda: None, timeout=0.6)
    assert event_stream.work_card_subscriber_count(wid) == 0, \
        "不带 cards=1 时不许挂卡级帧订阅者"
    assert not any("card" in f for f in frames), \
        f"不带 cards=1 时不该收到卡级帧：{frames}"

    # 带 cards=1：卡级事件（走 emit）⇒ 帧顶层多一个 card 字段
    resp2 = await routes.stream_work_events(wid, 1)

    def _emit():
        event_stream.emit(wid, cid, "tool_call", {"tool": "x"})

    frames2 = await _collect_stream(resp2, 1, _emit)
    assert frames2, "带 cards=1 却没收不到卡级帧"
    card_frames = [f for f in frames2 if f.get("card")]
    assert card_frames, f"卡级帧没有 card 字段：{frames2}"
    assert card_frames[0]["card"] == cid, card_frames[0]


async def test_c4_token_frames_not_in_work_cards(work):
    """C4：逐字帧**不进**工作级卡级帧（每 token 一帧会把队列打爆，画布也不需要）。"""
    wid, cid = work["work_id"], work["work_card_id"]
    out: list = []
    stop = asyncio.Event()
    sub = event_stream.subscribe_work_cards(wid)
    task = asyncio.create_task(_pump_gen(sub, out, stop))
    await asyncio.sleep(0.1)

    event_stream.emit_token(wid, cid, "content_chunk", "字")
    await asyncio.sleep(0.25)
    assert out == [], f"逐字帧不该进工作级卡级帧：{out}"

    event_stream.emit(wid, cid, "tool_call", {"tool": "x"})
    await _wait_frames(out, 1)
    assert len(out) == 1, f"过程帧该进工作级卡级帧：{out}"
    assert out[0]["card"] == cid

    stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    try:
        await sub.aclose()
    except Exception:  # noqa: BLE001
        pass


async def _pump_gen(sub, frames: list, stop: asyncio.Event) -> None:
    async for evt in sub:
        frames.append(evt)
        if stop.is_set():
            return


async def test_c5_work_native_frames_have_no_card_field(work):
    """C5：工作级原生帧（card_changed）**顶层没有** card ⇒ 前端可区分两类帧。"""
    wid = work["work_id"]
    resp = await routes.stream_work_events(wid, 1)
    frames: list = []
    stop = asyncio.Event()
    task = asyncio.create_task(_pump(resp, frames, stop))
    await asyncio.sleep(0.15)
    print("[DBG] subs work=", event_stream.work_subscriber_count(wid),
          "cards=", event_stream.work_card_subscriber_count(wid))
    print("[DBG] emit_work ->", event_stream.emit_work(wid, "card_changed", {"card_id": "card_001"}))
    for _q in event_stream._work_subscribers.get(wid, ()):
        print("[DBG] work queue", id(_q), "size", _q.qsize(), "getters", len(_q._getters))
    for _q in event_stream._work_card_subscribers.get(wid, ()):
        print("[DBG] card queue", id(_q), "size", _q.qsize(), "getters", len(_q._getters))
    await asyncio.sleep(0.5)
    print("[DBG] frames:", frames)
    stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    assert frames, "没收到工作级原生帧"
    assert "card" not in frames[0], \
        f"工作级原生帧顶层不该有 card 字段：{frames[0]}"
    assert frames[0]["type"] == "card_changed"


# ── C6/C7 前端连接预算（源码级） ─────────────────────────────────────────

def test_c6_canvas_has_no_per_card_eventstream():
    """C6：画布**没有**「每卡一条 EventSource」⇒ 连接数与卡数解耦。"""
    code = _code_only(CANVAS_JS)
    per_card = re.findall(r"new EventSource\([^)]*?/cards/", code)
    assert not per_card, \
        f"画布不该再给每张卡开一条 SSE（10 张卡 ⇒ 10 条 ⇒ 打满同源 6 条上限）：{per_card}"
    assert "watchStream" not in code, "watchStream（每卡一条流）该退役了"
    assert "watch.es" not in code, "watch.es（每卡一条流的句柄表）该退役了"


def test_c6b_canvas_work_stream_opts_in_cards():
    """C6b：画布的工作级流带 ?cards=1（否则拿不到卡级帧）。"""
    code = _code_only(CANVAS_JS)
    m = re.search(r"new EventSource\(`([^`]*?/events[^`]*?)`\)", code)
    assert m, "画布没找到工作级 EventSource"
    assert "cards=1" in m.group(1), \
        f"画布的工作级流必须带 cards=1（否则卡级帧收不到）：{m.group(1)}"


def test_c7_chat_uses_single_merged_stream():
    """C7：对话页**只开一条** EventSource 且带 tokens=1 ⇒ 每卡占 1 条而不是 2 条。"""
    code = _code_only(STREAM_JS)
    urls = re.findall(r"new EventSource\(([^)]*?)\)", code, flags=re.S)
    assert len(urls) == 1, \
        f"对话页只该开一条 SSE（改前每卡 2 条 ⇒ 切 3 张卡就打满 6 条上限）：{urls}"
    assert "tokens=1" in urls[0], f"合并流必须带 tokens=1：{urls[0]}"
    assert "/tokens" not in urls[0], \
        f"对话页不该再单独开逐字流：{urls[0]}"
    assert "esT" not in code.replace("es ＝ /stream、esT ＝ /tokens", ""), \
        "esT（第二条流的句柄）该退役了"
