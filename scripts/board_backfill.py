# -*- coding: utf-8 -*-
"""context_board 一次性回填：全库每卡重建缓存板（幂等，可重跑）。

用法：python scripts/board_backfill.py [work_id ...]
  不带参数 = 全部 work_*；带参数 = 只跑指定 work。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)  # works_data 相对根

for _l in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k, _v)

from src.storage import card_db, context_board  # noqa: E402


def _card_ids(work_id: str) -> list[str]:
    def _fn(conn):
        return [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT card_id FROM session_blocks ORDER BY card_id")]
    try:
        return card_db.read_sync(work_id, _fn, []) or []
    except Exception:
        return []


def main() -> None:
    works = sys.argv[1:] or sorted(
        d for d in os.listdir("works_data")
        if d.startswith("work_") and os.path.isdir(f"works_data/{d}"))
    total = 0
    t0 = time.time()
    for wid in works:
        for cid in _card_ids(wid):
            if cid.startswith("rv_"):  # 归档死卡不建板
                continue
            n = context_board.rebuild_board(wid, cid)
            total += n
            print(f"{wid}/{cid}: {n} 行")
    print(f"完成：{len(works)} 个 work，共 {total} 板行，{time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
