# -*- coding: utf-8 -*-
"""一致性克隆数据根（选择性）：sqlite 走 backup()（在线一致快照，wal 由
backup 收进主库、不复制 wal/shm），其余文件逐字节复制但跳过：
- 目录名 evidence / _ops / _scratch_*（实验产物，非运行必需）
- >50MB 的非 db 文件（events/*.jsonl 等归档流，开卡路径不读）
用法：python scripts/clone_data_root.py <src> <dst>"""
import os
import shutil
import sqlite3
import sys

SRC, DST = sys.argv[1], sys.argv[2]
SKIP_DIRS = {"evidence", "_ops"}
MAX_FILE = 50 * 1024 * 1024
copied_db = copied_files = skipped = 0
for root, dirs, files in os.walk(SRC):
    dirs[:] = [d for d in dirs
               if d not in SKIP_DIRS and not d.startswith("_scratch")]
    rel = os.path.relpath(root, SRC)
    droot = os.path.join(DST, rel) if rel != "." else DST
    os.makedirs(droot, exist_ok=True)
    for f in files:
        s = os.path.join(root, f)
        d = os.path.join(droot, f)
        if f.endswith("-wal") or f.endswith("-shm"):
            skipped += 1
            continue
        if f.endswith(".db") or f.endswith(".sqlite") or f.endswith(".sqlite3"):
            src = sqlite3.connect(f"file:{s}?mode=ro", uri=True)
            dst = sqlite3.connect(d)
            src.backup(dst)
            src.close(); dst.close()
            copied_db += 1
        else:
            try:
                if os.path.getsize(s) > MAX_FILE:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            shutil.copyfile(s, d)
            copied_files += 1
print(f"cloned: {copied_db} db + {copied_files} files "
      f"(skipped {skipped}) -> {DST}")
