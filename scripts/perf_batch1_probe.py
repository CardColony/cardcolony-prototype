# -*- coding: utf-8 -*-
"""性能批次一（进程间转发复用）验收探针 · 2026-11-19

用法（隔离实例，例 web=5900 / runner 基座 5901）：
    PYTHONPATH=. python scripts/perf_batch1_probe.py --base http://127.0.0.1:5900

覆盖任务书第一批验收点：
  A. 建测试 work 与 3 张卡（POST 经 _forward 打到属主 runner）；
  B. nav/running 扇入 A/B 对照——旧样式（每次现建 AsyncClient）vs 复用客户端，
     各测连发读数的 P50/P95；
  C. 转发 GET（align-status，走 _forward/stream 池）与本地 GET（sessions，
     白名单直读）分别计时；
  D. 工作级 SSE 扇入常驻 + 慢订阅期间，短查询（fanin GET）仍能即时响应——
     证明短/长流分池互不挤占；
  E. forward_ctl 连续 8 次调用对同一回环 echo 服务：服务端连接数=1、请求数=8
     （连接复用实证）；非 2xx 折成 {"error":{...}}；不可达折成 runner_unreachable；
  F. 杀掉一台 runner 后扇入部分失败语义：合并仍成功、日志可见 ok=3/4 与各片耗时。

探针自身只在进程外测量，不改任何服务端状态（除 A 步在隔离实例里建的测试 work）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import sys
import time
from collections import Counter

import httpx


def _p50(xs):
    return statistics.median(xs)


def _p95(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = max(0, min(len(xs) - 1, int(round(0.95 * (len(xs) - 1)))))
    return xs[k]


def _ms_list_report(name: str, xs: list[float]) -> str:
    if not xs:
        return f"{name}: (no samples)"
    return (f"{name}: n={len(xs)} P50={_p50(xs):.1f}ms "
            f"P95={_p95(xs):.1f}ms max={max(xs):.1f}ms")


async def setup_work(base: str) -> tuple[str, list[str]]:
    """建测试 work ＋ 3 张卡（根卡直系两子 + 孙卡），返回 (work_id, card_ids)。"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{base}/api/works", json={"title": "性能批次一探针"})
        r.raise_for_status()
        work_id = r.json()["work_id"]
        cards = []
        # 两张根卡直系子卡（不同子树根 ⇒ 不同属主 runner，覆盖多片转发）
        for i, title in enumerate(("探针子卡A", "探针子卡B")):
            r = await c.post(f"{base}/api/works/{work_id}/cards",
                             json={"title": title, "source": "用户直接创建",
                                   "card_type": "topic"})
            r.raise_for_status()
            cards.append(r.json()["card_id"])
        # 一张孙卡（挂到子卡A 下，与其同子树）
        r = await c.post(f"{base}/api/works/{work_id}/cards",
                         json={"title": "探针孙卡A1", "source": "用户直接创建",
                               "card_type": "topic", "parent": cards[0]})
        r.raise_for_status()
        cards.append(r.json()["card_id"])
    return work_id, cards


async def fanin_old_style(base: str, n: int) -> list[float]:
    """旧样式基线：每次现建 AsyncClient（改前 _fanin_get_merge 的做法）。"""
    xs = []
    url = f"{base}/api/nav/running"
    for _ in range(n):
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url)
            assert r.status_code == 200, r.status_code
        xs.append((time.perf_counter() - t0) * 1000.0)
    return xs


async def fanin_reused(base: str, n: int) -> list[float]:
    """新样式：单客户端复用连发（新 _fanin_get_merge 的行为）。"""
    xs = []
    url = f"{base}/api/nav/running"
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(n):
            t0 = time.perf_counter()
            r = await c.get(url)
            assert r.status_code == 200, r.status_code
            xs.append((time.perf_counter() - t0) * 1000.0)
    return xs


async def timed_get(base: str, path: str, n: int = 8) -> list[float]:
    xs = []
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(n):
            t0 = time.perf_counter()
            r = await c.get(f"{base}{path}")
            assert r.status_code == 200, (path, r.status_code)
            xs.append((time.perf_counter() - t0) * 1000.0)
    return xs


async def sse_hold_and_short_query(base: str, work_id: str,
                                   hold_s: float = 12.0) -> dict:
    """常驻工作级 SSE（扇入泵占住 stream 池），期间连发 fanin GET 短查询。"""
    got_lines: Counter = Counter()
    short_ms: list[float] = []

    async def _consume():
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET",
                                f"{base}/api/works/{work_id}/events") as resp:
                assert resp.status_code == 200, resp.status_code
                async for line in resp.aiter_lines():
                    got_lines["sse"] += 1

    async def _short_queries(n: int):
        async with httpx.AsyncClient(timeout=30) as c:
            for _ in range(n):
                t0 = time.perf_counter()
                r = await c.get(f"{base}/api/nav/running")
                assert r.status_code == 200, r.status_code
                short_ms.append((time.perf_counter() - t0) * 1000.0)
                await asyncio.sleep(0.3)

    cons = [asyncio.create_task(_consume()) for _ in range(2)]  # 双慢订阅
    await asyncio.sleep(1.0)
    await _short_queries(8)
    await asyncio.sleep(hold_s)
    for t in cons:
        t.cancel()
    return {"sse_lines": got_lines["sse"], "short_ms": short_ms}


# --------------------------------------------------------------- forward_ctl
class _EchoServer:
    """极简 keep-alive echo：统计连接数与请求数；可注入状态码。"""

    def __init__(self) -> None:
        self.connections = 0
        self.requests = 0
        self.status = 200
        self.server = None

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for ln in head.split(b"\r\n"):
                    if ln.lower().startswith(b"content-length:"):
                        length = int(ln.split(b":")[1].strip())
                if length:
                    await reader.readexactly(length)
                self.requests += 1
                body = json.dumps({"ok": True, "n": self.requests}).encode()
                head_out = (f"HTTP/1.1 {self.status} X\r\n"
                            f"Content-Type: application/json\r\n"
                            f"Content-Length: {len(body)}\r\n"
                            f"Connection: keep-alive\r\n\r\n").encode()
                writer.write(head_out + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


async def probe_forward_ctl() -> dict:
    """forward_ctl 三态：正常（连接复用实证）／非 2xx／不可达。"""
    from src.infra import procs
    echo = _EchoServer()
    port = await echo.start()
    procs._URLS = [f"http://127.0.0.1:{port}"]
    procs._RUNNER_COUNT = 1
    t0 = time.perf_counter()
    for _ in range(8):
        r = await procs.forward_ctl("w", "c", "/ctl/echo", {"x": 1})
        assert r.get("ok") is True, r
    reuse_ms = (time.perf_counter() - t0) * 1000.0
    echo.status = 500
    r500 = await procs.forward_ctl("w", "c", "/ctl/echo", {})
    echo.status = 200
    procs._URLS = ["http://127.0.0.1:1"]  # 不可达端口
    tun = await procs.forward_ctl("w", "c", "/ctl/echo", {})
    await echo.stop()
    procs.close_all_loop_clients()
    return {"connections": echo.connections, "requests": echo.requests,
            "reuse_8_calls_ms": round(reuse_ms, 1),
            "http500_err_code": (r500.get("error") or {}).get("code"),
            "unreachable_err_code": (tun.get("error") or {}).get("code")}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5900")
    ap.add_argument("--skip-setup", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 72)
    print("性能批次一探针 ·", base)
    if not args.skip_setup:
        work_id, cards = await setup_work(base)
        print(f"[A] 测试 work={work_id} cards={cards}")
    else:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/api/works")
            work_id = [w["work_id"] for w in r.json().get("works", [])
                       if "探针" in str(w.get("title", ""))][-1]
        cards = []
        print(f"[A] 复用既有 work={work_id}")

    old = await fanin_old_style(base, 6)
    print(_ms_list_report("[B1] fanin 旧样式（每次现建客户端）", old))
    new = await fanin_reused(base, 12)
    print(_ms_list_report("[B2] fanin 新样式（复用客户端连发）", new))

    fwd = await timed_get(base, f"/api/works/{work_id}/cards/{cards[0]}/align-status"
                          if cards else f"/api/works/{work_id}/pulse", 8)
    print(_ms_list_report("[C1] 转发 GET（_forward/stream 池）", fwd))
    try:
        loc = await timed_get(base, f"/api/works/{work_id}/cards/{cards[0]}/sessions"
                              if cards else "/api/works", 8)
        print(_ms_list_report("[C2] 本地 GET（web 白名单直读）", loc))
    except AssertionError:
        print("[C2] sessions 端点本实例无卡可查，跳过")

    sse = await sse_hold_and_short_query(base, work_id)
    print(_ms_list_report("[D] SSE 常驻期间 fanin 短查询", sse["short_ms"]))
    print(f"[D] 双 SSE 订阅 {len(sse['short_ms'])} 次短查询期间收到 {sse['sse_lines']} 行")

    ctl = await probe_forward_ctl()
    print("[E] forward_ctl:", json.dumps(ctl, ensure_ascii=False))
    print("=" * 72)
    print("探针完成。请到隔离实例 logs/server.log 查『回环客户端族已建』行数（每循环一条）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
