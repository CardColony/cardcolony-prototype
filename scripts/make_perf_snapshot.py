# -*- coding: utf-8 -*-
"""把 work_073 拷进隔离数据根（_data5707），供 5707 开发实例做性能基线。

为什么要单独拷：5700 是生产实例，works/ 与 works_data/ 一个字节都不能写。
perf 基准里会有拖拽写位置、翻页写状态之类动作 ⇒ 必须落在副本上。

用法：python scripts/make_perf_snapshot.py <src_repo_root> <dst_data_root>
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

#: Windows 保留设备名（nul/con/prn/aux/com1..9/lpt1..9）。
#: 这些名字在 NTFS 上能建出目录项，但**打开它们指向设备** ⇒ copy 会报
#: SameFileError（源与「目标」其实是同一个设备）。拷贝时一律跳过。
_RESERVED = {"nul", "con", "prn", "aux"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def _skip_name(name: str) -> bool:
    stem = name.split(".")[0].lower()
    return stem in _RESERVED


def copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    """递归复制（目录已存在则合并）。返回 (文件数, 字节数)。

    断点续传：目标已存在且**大小相同** ⇒ 跳过（不重复拷 15GB）。
    """
    n = 0
    total = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if _skip_name(item.name):
            continue
        if item.is_dir():
            a, b = copy_tree(item, target)
            n += a
            total += b
            continue
        try:
            size = item.stat().st_size
            if target.exists() and target.stat().st_size == size:
                n += 1
                total += size
                continue
            shutil.copy2(item, target)
            n += 1
            total += size
        except (OSError, shutil.SameFileError) as exc:
            print(f"[skip] {item}: {exc}", flush=True)
    return n, total


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    data_root = Path(sys.argv[2]).resolve()
    wid = sys.argv[3] if len(sys.argv) > 3 else "work_073"

    jobs = [
        (root / "works" / wid, data_root / "works" / wid),
        (root / "works_data" / wid, data_root / "works_data" / wid),
    ]
    for src, dst in jobs:
        if not src.exists():
            print(f"[skip] 源不存在：{src}", flush=True)
            continue
        t0 = time.time()
        n, total = copy_tree(src, dst)
        print(
            f"[ok] {src} -> {dst}  files={n} size={total / 1024 / 1024:.1f}MB "
            f"took={time.time() - t0:.1f}s",
            flush=True,
        )
    print("[done] 隔离快照就绪", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
