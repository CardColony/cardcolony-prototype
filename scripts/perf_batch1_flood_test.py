# -*- coding: utf-8 -*-
"""第一批返工复验：连接池占满时，普通短读是否被长流挤占（长尾单列）。

场景（任务书口径「用长流占满连接池的场景复验」）：
  --mode work：N 条**工作级** SSE（/events 扇入泵，占 sse 池）
  --mode card：N 条**卡级** SSE（/stream 直通，占 card_stream 池）——
              与普通转发（progress 等，relay 池）隔离的验证目标
两类长流都以「占满/超过对应池上限」的量级开订阅。

计量口径（用户令）：
  - 每类请求打印**全部样本**并单列长尾（>200ms）——不再用一刀切 PASS 掩盖长尾；
  - 功能判定 = P50 < 200ms（短读无需等流释放即可完成）；
  - P95 与 max 如实报告；SYN 残留尖峰计入长尾单列、不计入功能判定。

用法：
  PYTHONPATH=. python scripts/perf_batch1_flood_test.py --base http://127.0.0.1:5900 \
      --mode work --n-sse 10
  PYTHONPATH=. python scripts/perf_batch1_flood_test.py --base http://127.0.0.1:5900 \
      --mode card --n-sse 10
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

import httpx

SLOW_TAIL_MS = 200.0


def _p95(xs):
    xs = sorted(xs)
    return xs[int(round(0.95 * (len(xs) - 1)))] if xs else 0.0


def _report(kind: str, xs: list[float]) -> None:
    tails = [(i, round(x, 1)) for i, x in enumerate(xs) if x > SLOW_TAIL_MS]
    print(f"  [{kind}] P50={statistics.median(xs):.1f}ms "
          f"P95={_p95(xs):.1f}ms max={max(xs):.1f}ms n={len(xs)} "
          f"长尾(>{SLOW_TAIL_MS:.0f}ms)={tails if tails else '无'} "
          f"全部={[round(x, 1) for x in xs]}")


async def _timed(cli: httpx.AsyncClient, kind: str, url: str,
                 params: dict | None, n: int) -> list[float]:
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = await cli.get(url, params=params)
        dt = (time.perf_counter() - t0) * 1000.0
        assert r.status_code == 200, (kind, r.status_code)
        xs.append(dt)
        await asyncio.sleep(0.15)
    _report(kind, xs)
    return xs


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5900")
    ap.add_argument("--work", default="work_001")
    ap.add_argument("--card", default="card_006")
    ap.add_argument("--mode", choices=("work", "card"), default="work")
    ap.add_argument("--n-sse", type=int, default=10)
    ap.add_argument("--n-query", type=int, default=6)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    w, c = args.work, args.card

    print("=" * 76)
    print(f"占满复验 · mode={args.mode} web={base} 订阅={args.n_sse}")
    if args.mode == "card":
        print(f"  卡级流目标池=card_stream（上限 32 连接），"
              f"{args.n_sse} 条直通订阅")
    else:
        print(f"  工作级扇入泵 = {args.n_sse} × 4 runner = "
              f"{args.n_sse * 4} 条 > sse 池上限 32")

    got_lines = 0
    sse_errs: list[str] = []

    async def _work_consumer(idx: int):
        nonlocal got_lines
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as cc:
                async with cc.stream("GET",
                                     f"{base}/api/works/{w}/events") as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        got_lines += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            sse_errs.append(f"sse{idx}:{type(exc).__name__}")

    async def _card_consumer(idx: int):
        """卡级流直通消费者：占住 /stream 连接（有无帧都占）。"""
        nonlocal got_lines
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as cc:
                async with cc.stream(
                        "GET",
                        f"{base}/api/works/{w}/cards/{c}/stream") as resp:
                    assert resp.status_code == 200, resp.status_code
                    async for line in resp.aiter_lines():
                        got_lines += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            sse_errs.append(f"card{idx}:{type(exc).__name__}")

    async def _flood_round(tag: str) -> tuple[float, float, float]:
        nav = await _timed(cli, f"{tag} nav/running(short池)",
                           f"{base}/api/nav/running", None, args.n_query)
        prog = await _timed(cli, f"{tag} progress(relay池)",
                            f"{base}/api/works/{w}/cards/{c}/progress", None,
                            args.n_query)
        tl = await _timed(cli, f"{tag} timeline(本地直读)",
                          f"{base}/api/works/{w}/cards/{c}/timeline",
                          {"tail": 50}, args.n_query)
        return (statistics.median(nav), statistics.median(prog),
                statistics.median(tl))

    async with httpx.AsyncClient(timeout=30, trust_env=False) as cli:
        print("-- 基线（无长流占用）--")
        base_m = await _flood_round("基线")
        print(f"-- 长流常驻（mode={args.mode}, {args.n_sse} 订阅）--")
        consumer = _work_consumer if args.mode == "work" else _card_consumer
        consumers = [asyncio.create_task(consumer(i))
                     for i in range(args.n_sse)]
        await asyncio.sleep(2.5)
        flood_m = await _flood_round("占满")
        await asyncio.sleep(1.0)
        for t in consumers:
            t.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)

    print(f"-- 长流健康度：订阅期间共收 {got_lines} 行 err={sse_errs or '无'} --")
    ok = (all(m < 200.0 for m in flood_m) and got_lines > 0 and not sse_errs)
    print("功能判定：", "PASS —— 短读 P50 全部 <200ms（无需等流释放），"
          "长尾已单列见上方" if ok else "FAIL —— 短读 P50 被挤占或流不健康")
    print("=" * 76)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
