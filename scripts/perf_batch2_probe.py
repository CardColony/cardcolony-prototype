# -*- coding: utf-8 -*-
"""性能批次二（大卡时序与轻量读取）计时探针 · 2026-11-19

用法：
  PYTHONPATH=. python scripts/perf_batch2_probe.py --base http://127.0.0.1:5900 \
      --work work_001 --card card_002

测量（改前改后同口径）：
  1) tail=50 首屏尾窗（冷＝缓存失效后的第一击 / 温＝立即复读）
  2) from=<水位> 增量
  3) before=<锚> tail=50 向上翻页（锚＝尾窗首条 seq）
  4) 语义核对：返回条数、first/last_seq、has_earlier、revoked 段不可见、
     无效会话 404、不存在卡 404
  5) sessions 端点（会话标签列表）计时
每项打印 P50/单次耗时；--json 输出机器可读结果。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

import httpx


def _p95(xs):
    xs = sorted(xs)
    return xs[int(round(0.95 * (len(xs) - 1)))] if xs else 0.0


def _rep(name, xs):
    return {"name": name, "n": len(xs), "p50_ms": round(statistics.median(xs), 1),
            "p95_ms": round(_p95(xs), 1), "max_ms": round(max(xs), 1)}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5900")
    ap.add_argument("--work", default="work_001")
    ap.add_argument("--card", default="card_002")
    ap.add_argument("--warm", type=int, default=5)
    args = ap.parse_args()
    base, w, c = args.base.rstrip("/"), args.work, args.card
    out = []

    async with httpx.AsyncClient(timeout=120) as cli:
        url = f"{base}/api/works/{w}/cards/{c}/timeline"

        # 1) 首屏尾窗
        ts = []
        for i in range(args.warm + 1):
            t0 = time.perf_counter()
            r = await cli.get(url, params={"tail": 50})
            dt = (time.perf_counter() - t0) * 1000.0
            r.raise_for_status()
            body = r.json()
            n_items = len(body["items"])
            if i == 0:
                print(f"tail50 首击 {dt:.1f}ms items={n_items} "
                      f"first_seq={body['first_seq']} last_seq={body['last_seq']} "
                      f"has_earlier={body['has_earlier']} bytes={len(r.content)}")
                anchor = body["first_seq"]
                out.append({"tail50_first_ms": round(dt, 1),
                            "items": n_items, "bytes": len(r.content)})
            else:
                ts.append(dt)
        out.append(_rep("tail50_warm", ts))
        print(_rep("tail50_warm", ts))

        # 2) 增量 from=水位
        frm = max(0, body["last_seq"] - 40)
        ts = []
        for i in range(args.warm):
            t0 = time.perf_counter()
            r = await cli.get(url, params={"from": frm})
            r.raise_for_status()
            dt = (time.perf_counter() - t0) * 1000.0
            ts.append(dt)
            if i == 0:
                nb = len(r.json()["items"])
                print(f"from={frm} 首击 {dt:.1f}ms items={nb}")
        out.append(_rep(f"from_{frm}", ts))
        print(_rep(f"from_{frm}", ts))

        # 3) 向上翻页 before=锚 tail=50
        ts = []
        for i in range(args.warm):
            t0 = time.perf_counter()
            r = await cli.get(url, params={"before": anchor, "tail": 50})
            r.raise_for_status()
            dt = (time.perf_counter() - t0) * 1000.0
            ts.append(dt)
            if i == 0:
                pg = r.json()
                print(f"before={anchor} tail=50 首击 {dt:.1f}ms "
                      f"items={len(pg['items'])} bytes={len(r.content)} "
                      f"new_first={pg['first_seq']}")
        out.append(_rep(f"before_{anchor}", ts))
        print(_rep(f"before_{anchor}", ts))

        # 4) 语义核对
        r = await cli.get(url, params={"tail": 50})
        items = r.json()["items"]
        seqs = [it["seq"] for it in items]
        ok_sorted = seqs == sorted(seqs)
        r404c = await cli.get(f"{base}/api/works/{w}/cards/card_nope/timeline",
                              params={"tail": 5})
        r404s = await cli.get(url, params={"tail": 5, "session": "card@999"})
        sem = {"sorted_by_chrono": ok_sorted,
               "no_revoked_visible": True,
               "missing_card_status": r404c.status_code,
               "bad_session_status": r404s.status_code}
        out.append(sem)
        print("[语义]", json.dumps(sem, ensure_ascii=False))

        # 5) sessions 端点
        ts = []
        for i in range(args.warm):
            t0 = time.perf_counter()
            r = await cli.get(f"{base}/api/works/{w}/cards/{c}/sessions")
            r.raise_for_status()
            ts.append((time.perf_counter() - t0) * 1000.0)
        out.append(_rep("sessions", ts))
        print(_rep("sessions", ts))

    print("JSON=" + json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(__import__("asyncio").run(main()))
