# -*- coding: utf-8 -*-
"""模型调用流式观察器（只读）：每轮打印 在途/新增完成/错误事件/告警。
用法：python scripts/watch_calls.py [轮数] [间隔秒]
"""
import glob
import json
import sys
import time
from datetime import datetime

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
GAP = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def load():
    lines = []
    for f in glob.glob("logs/observe-*.jsonl"):
        for l in open(f, encoding="utf-8"):
            try:
                lines.append(json.loads(l))
            except Exception:
                pass
    lines.sort(key=lambda x: x.get("ts") or "")
    return lines


def show(seen_begin, seen_end):
    lines = load()
    now = time.time()
    begins = [l for l in lines if l.get("kind") == "call_begin"]
    ends = {l["trace_id"]: l for l in lines
            if l.get("kind") == "model_call" or l.get("kind") == "call_end"}
    infl = [b for b in begins if b["trace_id"] not in ends]
    fresh_b = [b for b in begins if b["ts"] not in seen_begin]
    fresh_e = [l for l in lines if l.get("ts") not in seen_end
               and (l.get("kind") == "model_call" or l.get("kind") == "call_end")]
    print(f"── {datetime.now():%H:%M:%S} 在途 {len(infl)} ─────────────────────────")
    for b in infl[-6:]:
        age = now - time.mktime(time.strptime(b["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
        flag = " ⚠长静默" if age > 900 else ""
        print(f"  在途 {b.get('caller') or '(无caller)'} {b.get('work_id')}/{b.get('card_id')} "
              f"seat={b.get('seat')} 已 {age:.0f}s{flag}")
    for b in fresh_b[-4:]:
        print(f"  ▶ 开跑 {b.get('caller') or '(无caller)'} {b.get('work_id')}/{b.get('card_id')} "
              f"seat={b.get('seat')}")
    for l in fresh_e[-6:]:
        if l.get("kind") == "call_end":
            continue
        err = l.get("error") or ""
        tag = f" ✗{err[:80]}" if err else f" fin={l.get('finish_reason')}"
        print(f"  ✓ 完成 {l.get('caller') or '(无caller)'} {l.get('work_id')}/{l.get('card_id')} "
              f"tok={l.get('tokens_used')} {l.get('latency_ms')}ms{tag}")
    return {b.get("ts") for b in begins}, {l.get("ts") for l in lines}


lines = load()
seen_begin = {b.get("ts") for b in lines if b.get("kind") == "call_begin"}
seen_end = {l.get("ts") for l in lines}
for i in range(ROUNDS):
    seen_begin, seen_end = show(seen_begin, seen_end)
    if i < ROUNDS - 1:
        time.sleep(GAP)
