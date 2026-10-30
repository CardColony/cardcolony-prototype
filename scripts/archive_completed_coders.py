# -*- coding: utf-8 -*-
"""存量迁移:把全部 works 里已完工的 coder 卡归档成 coder_archive 记录。

[2026-10-25 · 用户令「完工静默的 coder 卡折叠——视觉到数据,仅剩记录」]

幂等:只处理 state=completed 且 card_type=coder 的活卡;已归档的天然跳过
(不在 cards 表)。会话/事件文件不动(它们就是剩下的记录)。

用法::
    python scripts/archive_completed_coders.py            # 干跑,只报告
    python scripts/archive_completed_coders.py --apply    # 真归档
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正归档(缺省干跑)")
    args = ap.parse_args()

    import glob
    from src.api import coder_fold
    from src.storage import card_store, work_store

    total, done = 0, 0
    for w in work_store.list_works():
        wid = str(w.get("work_id") or "")
        if not wid:
            continue
        con = sqlite3.connect(ROOT / "works" / wid / "cards.db")
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT card_id, title FROM cards "
            "WHERE card_type='coder' AND state='completed'").fetchall()
        con.close()
        if not rows:
            continue
        print(f"{wid}: {len(rows)} 张完工 coder")
        for r in rows:
            total += 1
            if not args.apply:
                print(f"  [干跑] {r['card_id']} {r['title']}")
                continue
            res = await coder_fold.archive_coder(wid, r["card_id"])
            ok = res.get("ok")
            done += 1 if ok else 0
            mark = "✓" if ok else "✗"
            print(f"  {mark} {r['card_id']} {r['title']} "
                  f"{'' if ok else res.get('message', '')}")
    print(f"\n{'归档' if args.apply else '干跑'}完成: "
          f"{done if args.apply else 0}/{total} 张"
          + ("" if args.apply else "(加 --apply 执行)"))


if __name__ == "__main__":
    asyncio.run(main())
