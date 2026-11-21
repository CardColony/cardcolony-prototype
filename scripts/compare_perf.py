# -*- coding: utf-8 -*-
"""A/B 读数的**配对**统计（成对设计 ⇒ 该看逐轮差值，不是两堆样本各自的分布）。

用法：python scripts/compare_perf.py logs/perf_ab_result.json
"""
from __future__ import annotations

import json
import pathlib
import statistics
import sys


def pct(v: list[float], p: float):
    if not v:
        return None
    s = sorted(v)
    i = min(len(s) - 1, max(0, (-(-p * len(s) // 100)) - 1))
    return s[i]


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/perf_ab_result.json"
    d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    A, B = d.get("A", []), d.get("B", [])
    print(f"文件：{path}")
    print(f"说明：{d.get('note', '')}")
    fails = d.get("failures") or []
    print(f"作废轮次：{len(fails)}" + ("" if not fails else "（原因如下）"))
    for f in fails[:10]:
        print(f"   第 {f.get('round')} 轮 {f.get('arm')} 臂：{f.get('reason')}")
    if len(fails) > 10:
        print(f"   …（还有 {len(fails)-10} 条）")

    # 成对样本（同一轮的 A 与 B）
    byb = {r["round"]: r for r in B if r.get("firstRowMs") is not None}
    pairs = [(r["round"], r["firstRowMs"], byb[r["round"]]["firstRowMs"])
             for r in A if r.get("firstRowMs") is not None and r["round"] in byb]
    print(f"\n成对样本：{len(pairs)} 对")
    fa = [p[1] for p in pairs]
    fb = [p[2] for p in pairs]
    diffs = [a - b for _, a, b in pairs]
    rel = [(a - b) / b * 100 for _, a, b in pairs]

    print("\n=== 首批对话行（ms）===")
    print(f"  前(B)  中位 {statistics.median(fb):.0f}   P95 {pct(fb,95):.0f}   最小 {min(fb)}   最大 {max(fb)}")
    print(f"  后(A)  中位 {statistics.median(fa):.0f}   P95 {pct(fa,95):.0f}   最小 {min(fa)}   最大 {max(fa)}")
    print("\n=== 逐轮配对差值（A − B，负＝后侧更快）===")
    print(f"  差值中位 {statistics.median(diffs):+.0f} ms   （相对中位 {statistics.median(rel):+.1f}%）")
    print(f"  差值均值 {statistics.mean(diffs):+.0f} ms   （相对均值 {statistics.mean(rel):+.1f}%）")
    print(f"  A 更快的轮次 {sum(1 for x in diffs if x < 0)} / {len(diffs)}"
          f"   B 更快的轮次 {sum(1 for x in diffs if x > 0)}")
    sd = statistics.stdev(rel) if len(rel) > 1 else 0
    se = sd / (len(rel) ** 0.5) if len(rel) > 1 else 0
    if se:
        t = statistics.median(rel) / se
        print(f"  相对差值：中位 {statistics.median(rel):+.1f}%  标准误 {se:.1f}%  ⇒ t≈{t:.2f}")
        print(f"  ⇒ {'差异不显著（|t| < 2）' if abs(t) < 2 else '差异显著（|t| ≥ 2）'}")
    print("\n=== 载荷 ===")
    print(f"  API 请求数   前 {statistics.median([r['apiCount'] for r in B]):.0f}  后 {statistics.median([r['apiCount'] for r in A]):.0f}")
    print(f"  API 回包 KB  前 {statistics.median([r['apiKB'] for r in B]):.0f}  后 {statistics.median([r['apiKB'] for r in A]):.0f}")
    print(f"  长任务合计   前 {statistics.median([r['longTaskTotal'] for r in B]):.0f}ms  后 {statistics.median([r['longTaskTotal'] for r in A]):.0f}ms")
    print(f"  最长任务 max 前 {max(r['maxLongTask'] for r in B)}ms  后 {max(r['maxLongTask'] for r in A)}ms")
    fc = sum(1 for r in A if r.get("hasFullCard"))
    ft = sum(1 for r in A if r.get("hasFullModelTable"))
    print(f"  改后仍拉整张卡面的轮次 {fc}/{len(A)}；仍拉全项目模型表的轮次 {ft}/{len(A)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
