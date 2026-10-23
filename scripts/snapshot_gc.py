# -*- coding: utf-8 -*-
"""**快照库全局清扫**（[5715] · 磁盘治理）。

## 为什么有它

代码里那道闸是**每 work** 的体积上限（``data_snapshot.KEEP_BATCH_BYTES``）⇒
挡住了「单个 work 无限长」，但挡不住「**work 越开越多**」：30+ 个 work × 每份
2GB ⇒ 仍可到几十 GB（实测本机快照库 50.8GB：work_018 18.0GB / work_025 29.6GB）。

⇒ 本工具按**全局预算**清理：先报告，再（``--apply``）从**最旧**的过程批次开始删
（**日备不参与**——日备是「每天一份」的兜底，与过程批次不是一回事）。

## 用法

    # 只报告：每个 work 占了多少、总量多少、会删哪些
    python scripts/snapshot_gc.py

    # 真删（删的是留痕，不可恢复）
    python scripts/snapshot_gc.py --budget-gb 20 --apply

## 安全纪律

1. **默认只报告不删**（要删必须显式 ``--apply``）。
2. **日备永不删**（``daily_*`` 不参与删法；日备有自己的 30 天超期规则）。
3. **只删过程批次**（时间戳命名的目录），不碰 ``_backups``/``_trash`` 之类的旁支。
4. 删的是**快照留痕**，``works/`` 本体不动。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    """**按运行目录**定位仓库根（不是脚本所在目录）。

    理由：快照库是**按实例**分的（``<实例>_data_snapshots``）⇒ 要清哪个实例的库，
    就得在**那个实例的目录**里跑本脚本；若按脚本位置解析，跑到哪儿都是清
    worktree 的空库（还容易被误读成「没东西可清」）。
    """
    cwd = Path.cwd()
    if (cwd / "src" / "storage" / "data_snapshot.py").exists():
        return cwd
    return ROOT


sys.path.insert(0, str(_repo_root()))

from src.storage import data_snapshot  # noqa: E402


def batch_bytes(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def collect(root: Path, include_daily: bool = False) -> list[tuple[str, Path, int]]:
    """列出待清理的批次：默认**只含过程批次**（不含日备）；``include_daily`` 才把日备也算进来。"""
    out: list[tuple[str, Path, int]] = []
    if not root.exists():
        return out
    for work_dir in root.iterdir():
        if not work_dir.is_dir() or work_dir.name.startswith("_"):
            continue
        for b in work_dir.iterdir():
            if not b.is_dir():
                continue
            if b.name.startswith("daily_") and not include_daily:
                continue
            out.append((work_dir.name, b, batch_bytes(b)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="快照库全局清扫（按体积预算删最旧的批次）")
    ap.add_argument("--budget-gb", type=float, default=20.0, help="快照库总预算（GB）")
    ap.add_argument("--include-daily", action="store_true",
                    help="也删日备（默认不删：日备是「每天一份」的完整复盘点）")
    ap.add_argument("--apply", action="store_true", help="真删（默认只报告）")
    args = ap.parse_args(argv)

    root = data_snapshot.SNAPSHOT_ROOT
    batches = collect(root, include_daily=args.include_daily)
    total = sum(b[2] for b in batches)
    print(f"快照根: {root}")
    print(f"过程批次 {len(batches)} 份，合计 {total/1024**3:.2f} GB（预算 {args.budget_gb:.1f} GB）")

    per_work: dict[str, int] = {}
    for wid, _b, sz in batches:
        per_work[wid] = per_work.get(wid, 0) + sz
    for wid, sz in sorted(per_work.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {wid:<20} {sz/1024**3:8.2f} GB")

    budget = int(args.budget_gb * 1024 ** 3)
    if total <= budget:
        print("未超预算，无需清理")
        return 0

    # 从最旧的批次开始删（批���名带时间戳 ⇒ 名字序＝时间序）
    batches.sort(key=lambda t: t[1].name)
    victims = []
    running = total
    for wid, b, sz in batches:
        if running <= budget:
            break
        victims.append((wid, b, sz))
        running -= sz
    print(f"将删除 {len(victims)} 份最旧的批次（可回收 {sum(v[2] for v in victims)/1024**3:.2f} GB）")
    for wid, b, sz in victims[:20]:
        print(f"    {wid}/{b.name}  {sz/1024**2:.1f} MB")
    if len(victims) > 20:
        print(f"    …（其余 {len(victims)-20} 份略）")
    if not args.apply:
        print("（只报告；加 --apply 才真删）")
        return 0
    removed = 0
    for _wid, b, _sz in victims:
        shutil.rmtree(b, ignore_errors=True)
        removed += 1
    print(f"已删除 {removed} 份批次；剩余 {running/1024**3:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
