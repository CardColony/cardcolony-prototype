# -*- coding: utf-8 -*-
"""只拷 works_data/{wid}（卡库）到隔离数据根——与 make_perf_snapshot.py 分开跑，
两份并行（works/ 与 works_data/ 是两棵树，不冲突）。

用法：python scripts/copy_db_snapshot.py <repo_root> <dst_data_root> [work_id]
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from make_perf_snapshot import copy_tree  # noqa: E402


def main() -> int:
    root = pathlib.Path(sys.argv[1]).resolve()
    data_root = pathlib.Path(sys.argv[2]).resolve()
    wid = sys.argv[3] if len(sys.argv) > 3 else "work_073"
    src = root / "works_data" / wid
    dst = data_root / "works_data" / wid
    if not src.exists():
        print(f"[skip] 源不存在：{src}")
        return 1
    t0 = time.time()
    n, total = copy_tree(src, dst)
    print(f"[ok] {src} -> {dst}  files={n} size={total / 1024 / 1024:.1f}MB "
          f"took={time.time() - t0:.1f}s", flush=True)
    print("[done] 卡库快照就绪", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
