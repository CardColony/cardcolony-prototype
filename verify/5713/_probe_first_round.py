#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探针：看验证跑的当前进度（状态文件 ＋ 实例端口 ＋ 最近 prompt 日志）。

用法：python verify/5713/_probe_first_round.py
（临时文件，收尾前删掉）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SB = Path(r"D:\AI_station\推演agent\.worktrees\5713_sandbox")
LOGS = SB / "logs"

print("now:", time.strftime("%Y-%m-%d %H:%M:%S"))

st = SB / "_first_round_status.json"
if st.exists():
    print("--- status ---")
    print(st.read_text(encoding="utf-8"))
else:
    print("status: (none)")

bg = SB / "_first_round_bg.log"
if bg.exists() and bg.stat().st_size:
    print("--- bg log (tail) ---")
    txt = bg.read_text(encoding="utf-8", errors="replace")
    print(txt[-4000:])
else:
    print("bg log: empty or missing")

il = SB / "_first_round_instance.log"
if il.exists() and il.stat().st_size:
    print("--- instance log (tail) ---")
    txt = il.read_text(encoding="utf-8", errors="replace")
    print(txt[-4000:])
else:
    print("instance log: empty or missing")

try:
    with urllib.request.urlopen("http://127.0.0.1:5713/__instance__", timeout=6) as r:
        print("--- instance alive ---")
        print(r.read().decode("utf-8", "replace"))
except Exception as e:
    print("instance probe:", type(e).__name__, e)

out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                     encoding="utf-8", errors="replace").stdout
ports = [l.strip() for l in out.splitlines() if ":5713" in l]
print("--- netstat 5713 ---")
print("\n".join(ports) or "(none)")

print("--- newest prompt logs ---")
if LOGS.is_dir():
    fs = sorted(LOGS.glob("prompt_*.json"), key=lambda p: p.stat().st_mtime,
                reverse=True)[:6]
    for p in fs:
        print(f"  {p.name}  mtime={time.strftime('%H:%M:%S', time.localtime(p.stat().st_mtime))}")
else:
    print("  (no logs dir)")
