#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探针：定位 GBK 崩溃点（模型调用链路上谁 print 了含 ⇒ 的短串）。

用法：python verify/5713/_probe_gbk2.py
（临时文件，收尾前删掉）
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(r"D:\AI_station\推演agent\.worktrees\5713")
ARROW = "\u21d2"

print("=== 含 print( 且同行含 ⇒ 的行（源码级） ===")
for f in sorted((ROOT / "src").rglob("*.py")):
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except Exception:
        continue
    for i, line in enumerate(lines, 1):
        if "print(" in line and ARROW in line:
            print(f"  {f.relative_to(ROOT)}:{i}: {line.strip()[:200]}")

print()
print("=== 所有 print( 出现的文件与行数 ===")
counts = {}
for f in sorted((ROOT / "src").rglob("*.py")):
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except Exception:
        continue
    n = sum(1 for l in lines if "print(" in l)
    if n:
        counts[str(f.relative_to(ROOT))] = n
for k, v in sorted(counts.items(), key=lambda x: -x[1])[:20]:
    print(f"  {v:4d}  {k}")

print()
print("=== gateway / model 调用链上的 print（逐行） ===")
for name in ("src/api/model_service.py", "src/api/simple_service.py",
             "src/api/message_runner.py", "src/api/prompts.py",
             "src/api/current_turn.py"):
    p = ROOT / name
    if not p.exists():
        continue
    lines = p.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, 1):
        if "print(" in line:
            print(f"  {name}:{i}: {line.strip()[:200]}")

print()
print("=== 网关模块位置 ===")
for p in sorted(ROOT.rglob("*.py")):
    s = str(p).lower()
    if "gateway" in s and "verify" not in s and "tests" not in s:
        print("  ", p.relative_to(ROOT))
