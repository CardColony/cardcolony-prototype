# -*- coding: utf-8 -*-
"""
导出生产库里 role 相关存量数据 —— 只读任务，绝不对数据库做任何写操作。

作用：为后续删除旧 role 路径（role_store.py / role_room.py / role_* 五张表 / /roles 端点）
做数据保全。每个有数据的 work 导出一个 JSON。

只读保证：所有连接一律 sqlite3.connect(f"file:{path}?mode=ro", uri=True)。
"""
import sqlite3
import json
import os
import datetime

WORKS = ["work_018", "work_022"]
HERE = os.path.dirname(os.path.abspath(__file__))


def find_base():
    """从脚本所在目录向上找，定位含 works/ 的推演agent 根目录。"""
    d = HERE
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "works")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    raise RuntimeError("cannot locate 推演agent root (with works/) from " + HERE)


BASE = find_base()
WORKS_DIR = os.path.join(BASE, "works")

ROLE_TABLES = [
    "role_profiles",
    "role_verdicts",
    "role_alarms",
    "role_sync",
    "role_charter_history",
    "role_directives",
]


def ro_connect(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def dump_rows(con, table, order_by=None):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if order_by:
        q = f'SELECT * FROM {table} ORDER BY {order_by}'
    else:
        q = f'SELECT * FROM {table}'
    rows = con.execute(q).fetchall()
    out = []
    for row in rows:
        out.append({c: v for c, v in zip(cols, row)})
    return out


def export_work(work_id):
    db_path = os.path.join(WORKS_DIR, work_id, "cards.db")
    con = ro_connect(db_path)

    # 1) role 卡（cards 表）
    role_cards = []
    try:
        rows = con.execute(
            "SELECT card_id, title, card_type, parent, created_at FROM cards WHERE card_type='role' ORDER BY card_id"
        ).fetchall()
        role_cards = [
            {"card_id": r[0], "title": r[1], "card_type": r[2], "parent": r[3], "created_at": r[4]}
            for r in rows
        ]
    except Exception as e:
        role_cards = {"error": str(e)}

    # 2) 各 role_* 表全量（role_verdicts 按 id 排序，一条不漏）
    tables = {}
    for t in ROLE_TABLES:
        try:
            # 确认表存在
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if not exists:
                tables[t] = {"exists": False, "rows": []}
                continue
            order = "id" if t in ("role_verdicts", "role_alarms", "role_charter_history", "role_directives") else None
            rows = dump_rows(con, t, order_by=order)
            tables[t] = {"exists": True, "rows": rows}
        except Exception as e:
            tables[t] = {"error": str(e), "rows": []}

    # 3) 与 role 卡相关的收件箱消息（发件人或收件人是 role 卡）
    role_msg = []
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbox'"
        ).fetchone()
        if exists:
            role_ids = [c["card_id"] for c in role_cards if isinstance(c, dict) and "card_id" in c]
            if role_ids:
                marks = ",".join("?" * len(role_ids))
                rows = con.execute(
                    f"SELECT * FROM inbox WHERE target_card_id IN ({marks}) OR from_card IN ({marks}) ORDER BY seq",
                    role_ids + role_ids,
                ).fetchall()
                cols = [r[1] for r in con.execute("PRAGMA table_info(inbox)")]
                role_msg = [{c: v for c, v in zip(cols, row)} for row in rows]
            else:
                role_msg = []
        else:
            role_msg = {"error": "no inbox table"}
    except Exception as e:
        role_msg = {"error": str(e)}

    con.close()

    row_counts = {}
    for t, v in tables.items():
        if isinstance(v, dict) and "rows" in v and isinstance(v["rows"], list):
            row_counts[t] = len(v["rows"])
        else:
            row_counts[t] = 0
    row_counts["role_cards"] = len(role_cards) if isinstance(role_cards, list) else -1
    row_counts["role_inbox_messages"] = len(role_msg) if isinstance(role_msg, list) else -1

    doc = {
        "work_id": work_id,
        "source_db": db_path,
        "exported_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "row_counts": row_counts,
        "role_cards": role_cards,
        "role_tables": tables,
        "role_related_inbox_messages": role_msg,
    }
    return doc


def main():
    os.makedirs(HERE, exist_ok=True)
    all_counts = {}
    for work in WORKS:
        doc = export_work(work)
        out_path = os.path.join(HERE, f"role_archive_{work}.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        print(f"WROTE {out_path}  ({os.path.getsize(out_path)} bytes)")
        all_counts[work] = doc["row_counts"]
    # 统计摘要
    summary_path = os.path.join(HERE, "row_counts_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(all_counts, fh, ensure_ascii=False, indent=2)
    print("SUMMARY:", json.dumps(all_counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
