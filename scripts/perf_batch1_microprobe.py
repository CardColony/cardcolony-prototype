# -*- coding: utf-8 -*-
"""定点微探针：分辨 fanin 慢读数卡在 web 侧还是 runner 侧。

三路同测：
  direct  —— 探针进程直连 4 个 runner 的 /api/nav/running（绕过 web）；
  viaweb  —— 走 web 扇入 /api/nav/running；
  viaweb_pulse —— 走 web 扇入 /api/works/{w}/pulse。
每路 N 次连发，逐次打印时间戳与耗时；尾部给出 P50/P95。
若 direct 恒快而 viaweb 偶发秒级 ⇒ 卡点在 web 侧（连接池/循环）；
若两路同步出现秒级 ⇒ 卡点在 runner 侧。
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


def _p95(xs):
    xs = sorted(xs)
    return xs[int(round(0.95 * (len(xs) - 1)))] if xs else 0.0


async def _series(tag: str, urls: list[str], n: int,
                  shared: httpx.AsyncClient | None = None) -> list[float]:
    xs = []
    own = shared is None
    c = shared or httpx.AsyncClient(timeout=30)
    for i in range(n):
        t0 = time.perf_counter()
        results = await asyncio.gather(*(c.get(u) for u in urls),
                                       return_exceptions=True)
        dt = (time.perf_counter() - t0) * 1000.0
        ok = sum(1 for r in results if not isinstance(r, Exception)
                 and r.status_code == 200)
        xs.append(dt)
        print(f"  [{tag}] #{i + 1}: {dt:8.1f}ms ok={ok}/{len(urls)} "
              f"@{time.strftime('%H:%M:%S')}")
    if own:
        await c.aclose()
    print(f"  [{tag}] P50={statistics.median(xs):.1f}ms "
          f"P95={_p95(xs):.1f}ms max={max(xs):.1f}ms")
    return xs


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default="http://127.0.0.1:5900")
    ap.add_argument("--runners", default="5901,5902,5903,5904")
    ap.add_argument("--work", default="work_001")
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()
    web = args.web.rstrip("/")
    rurls = [f"http://127.0.0.1:{p}/api/nav/running"
             for p in args.runners.split(",")]

    print("== direct → runners（绕过 web）==")
    await _series("direct", rurls, args.n)
    print("== via web fanin nav/running（复用客户端）==")
    async with httpx.AsyncClient(timeout=30) as c:
        await _series("viaweb", [f"{web}/api/nav/running"], args.n, c)
    print("== via web fanin pulse（复用客户端）==")
    async with httpx.AsyncClient(timeout=30) as c:
        await _series("viaweb_pulse",
                      [f"{web}/api/works/{args.work}/pulse"], args.n, c)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
