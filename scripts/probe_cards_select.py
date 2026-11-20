# -*- coding: utf-8 -*-
"""探针：cards 表的**全表读**在不同列集下的耗时/字节。

为什么问这个：/api/nav 的首次构建实测 **91 秒**（work_073，975 张卡）。
根因候选：① SELECT * 把正文大列（mission/status_block/requirements）整列拉进
Python（≈490MB 字符串分配）；② 即使窄选，若选到**排在大列之后**的列
（goal_state/goal_updated_at 在 requirements 之后），SQLite 仍要顺着
overflow 链跳过去。

跑法：python scripts/probe_cards_select.py [db_path] [work_id]
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import time

CASES = [
    ("A 最小集（只到大列之前）",
     "card_id, title, source, card_type, parent, state, updated_at"),
    ("B nav 需要的集（含 goal_*，在大列之后）",
     "card_id, title, source, card_type, parent, state, updated_at, goal_state, goal_updated_at"),
    ("C SELECT *（现状）", "*"),
]


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\AI_station\推演agent\_data5707\works_data\work_073\cards.db"
    path = pathlib.Path(db)
    if not path.exists():
        print(f"[skip] 库不存在：{db}")
        return 1
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for label, cols in CASES:
        t0 = time.perf_counter()
        n = 0
        total = 0
        for row in con.execute(f"SELECT {cols} FROM cards ORDER BY card_id"):
            n += 1
            for v in tuple(row):
                if isinstance(v, str):
                    total += len(v)
        dt = (time.perf_counter() - t0) * 1000
        print(f"{label:<42} {dt:9.1f} ms  rows={n:5d}  取回的字符串={total/1024/1024:8.1f} MB")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
