# -*- coding: utf-8 -*-
"""5713 快照批次完整性对账（只读审计，不写任何快照/数据）。

对 5713_sandbox/snapshots 下每个 work 的每个批次，检查：
- 批次是否包含 cards.db（恢复卡片库的必要文件）
- 批次里的文件集 vs 该 work 当前 live 文件集（缺哪些）
输出 experiments/snapshot_credibility/completeness.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SBX = None
for d in Path(r"D:\AI_station").iterdir():
    p = d / ".worktrees" / "5713_sandbox"
    if p.is_dir() and d.name.endswith("agent"):
        SBX = p
        break
assert SBX
SNAP = SBX / "snapshots"
WORKS = SBX / "works"
OUT = SBX / "experiments" / "snapshot_credibility" / "completeness.json"

report = {"snapshot_root": str(SNAP), "works": []}
for wd in sorted(SNAP.iterdir()):
    if not wd.is_dir():
        continue
    work = wd.name
    live = WORKS / work
    live_files = set()
    if live.exists():
        live_files = {str(p.relative_to(live)).replace("\\", "/")
                      for p in live.rglob("*") if p.is_file()}
    batches = sorted([b for b in wd.iterdir() if b.is_dir()], key=lambda p: p.name)
    real = [b for b in batches if not b.name.startswith("daily_")]
    rows = []
    missing_db = 0
    for b in batches:
        files = {str(p.relative_to(b)).replace("\\", "/") for p in b.rglob("*") if p.is_file()}
        has_db = f"works/{work}/cards.db" in files
        if not has_db:
            missing_db += 1
        rows.append({"stamp": b.name, "files": len(files), "has_cards_db": has_db})
    latest = rows[-1] if rows else None
    latest_real = [r for r in rows if not r["stamp"].startswith("daily_")]
    latest_real = latest_real[-1] if latest_real else None
    miss_in_latest = []
    if latest_real:
        bp = wd / latest_real["stamp"]
        bfiles = {str(p.relative_to(bp)).replace("\\", "/") for p in bp.rglob("*") if p.is_file()}
        expect = {f"works/{work}/{x}" for x in live_files}
        miss_in_latest = sorted(expect - bfiles)
    report["works"].append({
        "work": work,
        "live_files": len(live_files),
        "batches": len(batches),
        "real_batches": len(real),
        "daily_batches": len(batches) - len(real),
        "batches_missing_cards_db": missing_db,
        "missing_cards_db_pct": round(100.0 * missing_db / len(batches), 1) if batches else None,
        "latest_batch": latest["stamp"] if latest else None,
        "latest_batch_files": latest["files"] if latest else 0,
        "latest_real_batch": latest_real["stamp"] if latest_real else None,
        "latest_real_has_cards_db": latest_real["has_cards_db"] if latest_real else None,
        "live_files_absent_from_latest_real_batch": miss_in_latest,
        "sample_last5": rows[-5:],
    })

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

tot = sum(w["batches"] for w in report["works"])
miss = sum(w["batches_missing_cards_db"] for w in report["works"])
print(f"works={len(report['works'])} batches={tot} missing_cards_db={miss} ({round(100.0*miss/tot,1)}%)")
bad = [w for w in report["works"] if w["live_files_absent_from_latest_real_batch"]]
print(f"latest_batch 缺文件的 work 数={len(bad)}")
for w in bad:
    print(f"  {w['work']}: live={w['live_files']} latest={w['latest_real_batch']} 缺文件={len(w['live_files_absent_from_latest_real_batch'])} "
          f"{w['live_files_absent_from_latest_real_batch'][:4]}")
print("->", OUT)