# -*- coding: utf-8 -*-
"""★ **清理"我们卡片跑出来的"会话残留**（2026-11-04 用户令）。

用户令：
1. 「各自做完的任务，在各自客户端内做一个清理机制」
2. 「**千万别删除对话**」「**千万别删错对话**」
3. 「删对话也作为你定时任务的一环」

⇒ 这个脚本是**定时任务里的那一环**（扫掉崩溃/中断留下的残留）。

★★★ **安全边界（红线，写死在代码里）** ★★★
- **只删工作目录在白名单根下面的会话**（默认 = 仓库的 ``works/``）
  ⇒ **每张 coder 卡的工作目录都在这儿**；
  ⇒ 用户自己在 Devin Desktop / opencode 里的会话（目录是仓库根或别处）
    **结构上就落选** ⇒ **不可能被误删**。
- **只按精确 id 删**（不按标题 / 名字前缀 / 时间范围）。
- **删前验身份**（Devin 侧：``devin list`` 在该目录下复核 id 与工作目录）。
- **WorkBuddy 桌面端一律不碰**（``~/.workbuddy/app/session`` 是用户的对话）。
- 默认 **dry-run**（只报告不删）；要真删加 ``--delete``。

用法::

    python scripts/cleanup_exec_sessions.py              # 干跑（只报告）
    python scripts/cleanup_exec_sessions.py --delete     # 真删
    python scripts/cleanup_exec_sessions.py --delete --root D:\\some\\works
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def now_iso() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def load_key() -> str:
    try:
        for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENCODE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return os.environ.get("OPENCODE_API_KEY", "")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true", help="真删（默认只报告）")
    ap.add_argument("--root", action="append", default=[],
                    help="额外的白名单根（可重复）；默认用仓库的 works/")
    args = ap.parse_args()

    from src.api.wb_exec import cleanup as cl
    from src.api.wb_exec.config import load as load_cfg

    cfg = load_cfg()
    roots = [str(p) for p in (cl.DEFAULT_WORK_ROOTS if not args.root else [])]
    roots += [str(Path(r)) for r in args.root]
    dry = not args.delete

    print(f"=== 清理我们自己的会话残留（{now_iso()}）===")
    print(f"  模式：{'★ 干跑（只报告，不删）' if dry else '真删'}")
    print(f"  白名单根：{roots}")

    devin_exe = ""
    oc_exe = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules",
                           "opencode-ai", "bin", "opencode.exe")
    for ex in cfg.executors:
        if ex.devin_cli_exe:
            devin_exe = ex.devin_cli_exe
        if ex.oc_serve_exe and os.path.exists(ex.oc_serve_exe):
            oc_exe = ex.oc_serve_exe

    report: dict = {}

    # ---- Devin
    print("\n--- Devin ---")
    dv = await cl.sweep_devin_sessions(devin_exe, roots, dry_run=dry)
    report["wb-devin"] = dv
    print(f"  全库会话 {dv.get('scanned')} 条；**我们的** {len(dv.get('ours') or [])} 条")
    for r in (dv.get("ours") or [])[:20]:
        print(f"    - {r['id']:<22} {r.get('working_directory','')}  {r.get('title','')[:40]}")
    if dv.get("deleted"):
        print(f"  已删 {len(dv['deleted'])} 条")
    if dv.get("skipped"):
        print(f"  跳过 {len(dv['skipped'])} 条：{dv['skipped'][:3]}")
    if dv.get("error"):
        print(f"  ⚠ {dv['error']}")

    # ---- opencode
    print("\n--- opencode ---")
    oc = await cl.sweep_opencode_sessions(oc_exe, load_key(), roots, dry_run=dry)
    report["wb-oc"] = oc
    print(f"  全库会话 {oc.get('scanned')} 条；**我们的** {len(oc.get('ours') or [])} 条")
    for r in (oc.get("ours") or [])[:20]:
        print(f"    - {r['id']:<26} {r.get('directory','')}  {r.get('title','')[:30]}")
    if oc.get("deleted"):
        print(f"  已删 {len(oc['deleted'])} 条")
    if oc.get("skipped"):
        print(f"  跳过 {len(oc['skipped'])} 条：{oc['skipped'][:3]}")
    if oc.get("error"):
        print(f"  ⚠ {oc['error']}")

    # ---- WorkBuddy：★ 只报告，绝不删（用户的对话）
    print("\n--- WorkBuddy（桌面端）---")
    print("  ★ **不删**：桌面端会话库（~/.workbuddy/app/session）是用户自己的对话。")
    print("    我们跑的是 CLI 池，会话落在 CLI 自己的存储里，体积很小，暂不处理。")

    print("\n=== 汇总 ===")
    for tag, rep in report.items():
        print(f"  {tag}: 扫 {rep.get('scanned')} / 我们的 {len(rep.get('ours') or [])}"
              f" / 已删 {len(rep.get('deleted') or [])}"
              f" / 跳过 {len(rep.get('skipped') or [])}")
    out = REPO / "logs" / "wb_exec" / "cleanup_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"at": now_iso(), "dry_run": dry,
                                "roots": roots, "report": report},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
