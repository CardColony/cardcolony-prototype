#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探针：找 GBK 编码报错（'gbk' codec can't encode character '⇒'）的出处。

用法：python verify/5713/_probe_gbk.py
（临时文件，收尾前删掉）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(r"D:\AI_station\推演agent\.worktrees\5713")
ARROW = "\u21d2"          # ⇒
PAT = re.compile(r"print\(|sys\.stdout|sys\.stderr|logging\.|logger\.")

files = sorted((ROOT / "src").rglob("*.py"))
print("scanning", len(files), "files under", ROOT / "src")
hits = []
for f in files:
    try:
        text = f.read_text(encoding="utf-8")
    except Exception:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        if ARROW in line or ("print(" in line and ("⇒" in line or "▸" in line)):
            hits.append((f, i, line.strip()))
        elif "print(" in line and ("{card" in line or "card=" in line):
            hits.append((f, i, line.strip()))
print("=== arrow/print hits:", len(hits))
for f, i, line in hits[:60]:
    print(f"  {f.relative_to(ROOT)}:{i}: {line[:180]}")

# 重点：模型调用链路上谁把内容打到了 stdout
print()
print("=== model_service.py prints ===")
for name in ("src/api/model_service.py", "src/api/simple_service.py",
             "src/api/message_runner.py", "src/api/prompts.py"):
    p = ROOT / name
    if not p.exists():
        continue
    text = p.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if "print(" in line or "sys.stdout" in line:
            print(f"  {name}:{i}: {line.strip()[:180]}")
