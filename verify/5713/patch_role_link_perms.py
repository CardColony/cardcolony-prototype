# -*- coding: utf-8 -*-
"""只给存量 role 卡补 link/unlink（精确修补，不动其它卡的 perms）。

为什么不用 backfill 全量重算：那会把整个 work 所有卡刷成 v10，
包括我自己的管理卡（会被摘掉 tools_file/tools_web，我就没法继续干活了）。
本脚本只动 card_type='role' 的卡，且只在缺 link/unlink 时补，幂等。
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

WORKS = r"D:\AI_station\推演agent\works"
DRY = "--apply" not in sys.argv
NEED = ("link", "unlink")

print("模式:", "DRY-RUN（不写库）" if DRY else "APPLY（会写库，先自动备份）")
print()

total_fixed = 0
for wn in sorted(os.listdir(WORKS)):
    db = os.path.join(WORKS, wn, "cards.db")
    if not os.path.exists(db):
        continue
    try:
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT card_id, title, perms, perms_version FROM cards "
            "WHERE card_type='role'"
        ).fetchall()
        if not rows:
            c.close()
            continue
    except sqlite3.Error as e:
        print(f"  {wn}: 读失败 {e}")
        continue

    todo = []
    for r in rows:
        try:
            cur = set(json.loads(r["perms"] or "[]"))
        except (TypeError, ValueError):
            cur = set()
        missing = [s for s in NEED if s not in cur]
        if missing:
            todo.append((r["card_id"], r["title"], sorted(cur), missing,
                         r["perms_version"]))
    if not todo:
        c.close()
        continue

    print(f"=== {wn} ===")
    for cid, title, cur, missing, ver in todo:
        print(f"  {cid} 「{str(title)[:26]}」 v{ver}")
        print(f"     现状 {cur}")
        print(f"     缺   {missing}")

    if not DRY:
        bak_dir = os.path.join(WORKS, "_backup_role_link")
        os.makedirs(bak_dir, exist_ok=True)
        bak = os.path.join(bak_dir, f"{wn}.cards.db.{time.strftime('%Y%m%d_%H%M%S')}")
        with sqlite3.connect(db) as src, sqlite3.connect(bak) as dst:
            src.backup(dst)
        print(f"     已备份 → {bak}")

        for cid, title, cur, missing, ver in todo:
            new_perms = sorted(set(cur) | set(NEED))
            c.execute(
                "UPDATE cards SET perms=?, perms_version=? WHERE card_id=?",
                (json.dumps(new_perms, ensure_ascii=False), 10, cid),
            )
            print(f"     写入 {cid}: {new_perms}")
        c.commit()
        total_fixed += len(todo)
    c.close()
    print()

print("=== 汇总 ===")
if DRY:
    print(f"  待修 {sum(1 for _ in [])} —— 见上方各 work 明细")
    print("  加 --apply 才会真正写库")
else:
    print(f"  已修 {total_fixed} 张 role 卡")