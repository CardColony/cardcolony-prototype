# -*- coding: utf-8 -*-
"""性能批次三（模型请求装配）本地计时探针 · 2026-11-19

在隔离实例的**代码树内**跑（不调模型、不联网）：
    cd <隔离树> && PYTHONPATH=. python <主仓>/scripts/perf_batch3_probe.py --work work_001 --card card_006

测量：
  A. build_ctx 取数（装配链唯一 IO 段）耗时；
  B. compose_model_context 首跳（system 渲染+动态渲染）vs 续跳（缓存命中）耗时；
  C. build_context_bundle 带/不带 viewpoints 的总耗时（懒渲染收益）；
  D. system_cache_stats 命中账。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="work_001")
    ap.add_argument("--card", default="card_006")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    from src.api import context_assembler, context_regions

    print("=" * 72)
    print(f"装配链本地计时 · work={args.work} card={args.card}")
    # A. build_ctx
    ts = []
    for _ in range(args.n):
        t0 = time.perf_counter()
        ctx = context_assembler.build_ctx(args.work, args.card)
        ts.append((time.perf_counter() - t0) * 1000.0)
    print(f"[A] build_ctx 取数: P50={statistics.median(ts):.1f}ms "
          f"max={max(ts):.1f}ms cards={len(ctx.cards)}")

    # B. compose 首跳 vs 续跳
    first, hop = [], []
    tail = [{"role": "assistant", "content": "〔任务〕探针"}]
    for i in range(args.n):
        ctx = context_assembler.build_ctx(args.work, args.card)
        t0 = time.perf_counter()
        r1 = context_regions.compose_model_context(
            ctx, profile="main", prior_messages=[{"role": "user", "content": "q"}],
            user_content="本轮消息", turn_tail=tail)
        first.append((time.perf_counter() - t0) * 1000.0)
        prior2 = list(r1["prior_messages"]) + [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "read_card", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "回执"}]
        t0 = time.perf_counter()
        r2 = context_regions.compose_model_context(
            ctx, profile="main", prior_messages=prior2,
            prior_key="tool_turn", user_content="本轮消息", turn_tail=tail)
        hop.append((time.perf_counter() - t0) * 1000.0)
        assert r1["system_prompt"] == r2["system_prompt"], "system 区跨跳不一致!"
    print(f"[B] compose 首跳(全渲染): P50={statistics.median(first):.1f}ms "
          f"max={max(first):.1f}ms；续跳(缓存命中): P50={statistics.median(hop):.1f}ms "
          f"max={max(hop):.1f}ms；system 区逐字节一致 ✓")
    st = context_regions.system_cache_stats()
    print(f"[D] system 缓存账: {st}")

    # C. bundle 带/不带 viewpoints
    ts_v, ts_nv = [], []
    for _ in range(args.n):
        t0 = time.perf_counter()
        context_assembler.build_context_bundle(args.work, args.card)
        ts_v.append((time.perf_counter() - t0) * 1000.0)
        t0 = time.perf_counter()
        context_assembler.build_context_bundle(args.work, args.card,
                                               with_viewpoints=False)
        ts_nv.append((time.perf_counter() - t0) * 1000.0)
    print(f"[C] bundle 含 viewpoints: P50={statistics.median(ts_v):.1f}ms；"
          f"不含（生产主链路新路径）: P50={statistics.median(ts_nv):.1f}ms")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
