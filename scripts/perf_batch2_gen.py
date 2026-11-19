# -*- coding: utf-8 -*-
"""合成大卡生成器（隔离实例用）· 性能批次二基线/复测

在隔离实例的 works/<w>/events/<card>.jsonl 上直接追加合成事件：
  - normal 行 ~1.5KB × N_NORMAL（assistant_text / tool_call / tool_result 混合）
  - giant 行 ~N_GIANT × 20MB（tool_result 整卡回灌形态，card_007 实况的缩影）
  - revoke 记录 N_REVOKE 条（打在normal 行区间上，制造可见集合裁剪）
  - 追加阶段（--append N）：再追加 M 条 normal + 1 条 revoke（模拟持续追加）
直接写文件（读侧只见追加后的文件，与生产异步落盘后的实况等价）。

用法：
  python scripts/perf_batch2_gen.py --iso D:/AI_station/agent_perf_iso \
      --work work_001 --card card_002 [--append 200]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

ISO_TS = "2026-11-19T21:00:00.000+08:00"


def _line(seq: int, card: str, typ: str, data: dict) -> str:
    rec = {"seq": seq, "card_id": card, "run_id": f"run{seq}", "round": 1,
           "type": typ, "data": data, "model": "", "ts": ISO_TS}
    return json.dumps(rec, ensure_ascii=False)


def gen(iso: str, work: str, card: str, n_normal: int, n_giant: int,
        n_revoke: int, start_seq: int = 0) -> Path:
    d = Path(iso) / "works" / work / "events"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{card}.jsonl"
    t0 = time.perf_counter()
    seq = start_seq
    giant_every = max(1, n_normal // max(1, n_giant))
    revokes = []
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        for i in range(n_normal):
            seq += 1
            if i % 7 == 0:
                typ, data = "tool_call", {"tool": "read_linked", "args": {"x": i}}
            elif i % 7 == 1:
                typ, data = "tool_result", {"content": "ok " + "x" * 800}
            else:
                typ, data = "assistant_text", {"text": f"合成事件 {i}。" + "词" * 500}
            f.write(_line(seq, card, typ, data) + "\n")
            if n_giant and i % giant_every == 0 and len(revokes) < 10 ** 9:
                # 巨型行：~20MB 的 tool_result（data 内大文本）
                seq += 1
                blob = "B" * (20 * 1024 * 1024)
                f.write(_line(seq, card, "tool_result",
                              {"content": blob, "note": "giant"}) + "\n")
            if n_revoke and i % max(1, n_normal // n_revoke) == 0 and i > 50:
                revokes.append(seq)
        for fr in revokes[:n_revoke]:
            seq += 1
            f.write(_line(seq, card, "revoke", {"from_seq": fr - 40}) + "\n")
    dt = time.perf_counter() - t0
    print(f"gen ok file={p} size={p.stat().st_size/1e6:.1f}MB "
          f"last_seq={seq} took={dt:.1f}s")
    return p


def append_more(iso: str, work: str, card: str, n: int,
                start_seq: int) -> Path:
    p = Path(iso) / "works" / work / "events" / f"{card}.jsonl"
    size0 = p.stat().st_size
    t0 = time.perf_counter()
    seq = start_seq
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        for i in range(n):
            seq += 1
            f.write(_line(seq, card, "assistant_text",
                          {"text": f"追加事件 {i}。" + "字" * 400}) + "\n")
        seq += 1
        f.write(_line(seq, card, "revoke", {"from_seq": seq - 30}) + "\n")
    dt = time.perf_counter() - t0
    grew = p.stat().st_size - size0
    print(f"append ok +{n+1} lines +{grew/1e3:.0f}KB took={dt:.2f}s last_seq={seq}")
    return p


def last_seq_of(p: Path) -> int:
    import re
    m = re.compile(r'"seq":\s*(\d+)')   # json.dumps 默认分隔符带空格
    s = p.stat().st_size
    with open(p, "rb") as f:
        f.seek(max(0, s - 65536))
        tail = f.read().decode("utf-8", errors="replace")
    seqs = [int(x) for x in m.findall(tail)]
    return max(seqs) if seqs else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iso", required=True)
    ap.add_argument("--work", default="work_001")
    ap.add_argument("--card", default="card_002")
    ap.add_argument("--normal", type=int, default=3000)
    ap.add_argument("--giant", type=int, default=5)
    ap.add_argument("--revoke", type=int, default=8)
    ap.add_argument("--append", type=int, default=0)
    args = ap.parse_args()
    p = Path(args.iso) / "works" / args.work / "events" / f"{args.card}.jsonl"
    start = last_seq_of(p) if p.exists() else 0
    if args.append:
        append_more(args.iso, args.work, args.card, args.append, start)
    else:
        gen(args.iso, args.work, args.card, args.normal, args.giant,
            args.revoke, start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
