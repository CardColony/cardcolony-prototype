# -*- coding: utf-8 -*-
"""R10 定性探针 v4：找出「谁还握着 cards.db/-wal」。

v3 已证：告警那一刻 `card_db._conns` 与 `_read_conns` **都是空的**（缓存连接全关了），
但 psutil 显示 cards.db / -wal / -shm 三者仍被本进程打开 ⇒ 持有者不是 card_db 的缓存连接。

本版把所有 `sqlite3.connect()` 都记下来（路径＋线程＋创建点栈），在告警瞬间逐条测活：
`execute("SELECT 1")` 能过＝还开着。活着的那条就是持有者，栈就是它的来路。
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

WT = Path(r"D:\AI_station\推演agent\.worktrees\5713")
OUT = Path(r"D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\b06")
V1, V2 = "第一版正文", "第二版正文"
EVID = {"stages": {}}

_orig_connect = sqlite3.connect
REC: list[dict] = []


def _connect(*a, **kw):
    conn = _orig_connect(*a, **kw)
    target = a[0] if a else kw.get("database", "?")
    stack = [f"{f.filename.split(chr(92))[-1]}:{f.lineno} {f.name}"
             for f in traceback.extract_stack()[:-1]
             if "5713" in f.filename and "storage" in f.filename or "api" in f.filename]
    REC.append({"target": str(target), "thread": threading.get_ident(),
                "stack": stack[-4:], "conn": conn})
    return conn


def rec(stage, **kw):
    EVID["stages"].setdefault(stage, {}).update(kw)
    try:
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:1200])
    except Exception:
        pass


def alive():
    out = []
    for r in REC:
        try:
            r["conn"].execute("SELECT 1")
            out.append({"target": r["target"].rsplit("\\", 1)[-1], "thread": r["thread"],
                        "stack": r["stack"]})
        except Exception:
            pass
    return out


class Alarm(logging.Handler):
    def __init__(self):
        super().__init__()
        self.hits = []

    def emit(self, record):
        if "旁挂文件失败" in record.getMessage():
            self.hits.append({"at": time.strftime("%H:%M:%S"),
                              "sqlite_conns_still_open": alive(),
                              "total_connects_recorded": len(REC)})


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.path.insert(0, str(WT))
    sqlite3.connect = _connect          # 只在本进程内挂钩，不改产品代码

    root = OUT / "r10_v4"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    base = root / "app_home"
    works = base / "works"
    works.mkdir(parents=True)
    for d in ("trading-workspace", "推演agent", "logs"):
        (base / d).mkdir()

    from src.api import file_tools, prompt_log
    from src.storage import (canvas_store, card_db, card_store, data_snapshot,
                             manuscript_store, session_store, work_store)
    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        mod.WORKS_DIR = works
    card_db.reset_caches()
    data_snapshot.BASE_DIR = base
    data_snapshot.SNAPSHOT_ROOT = root / "snapshots"
    data_snapshot._FINGERPRINT_FILE = base / ".snapshot_fingerprints.json"
    prompt_log.LOGS_DIR = base / "logs"
    file_tools.BASE_DIR = base
    file_tools.ALLOWED_ROOTS = ((base / "trading-workspace").resolve(), (base / "推演agent").resolve())
    file_tools.BACKUP_DIR = base / "推演agent" / "_backups"

    alarm = Alarm()
    logging.getLogger("storage.snapshot").addHandler(alarm)

    wdata = await work_store.create_work("R10 v4")
    wid, cid = wdata["work_id"], wdata["work_card_id"]
    db = works / wid / "cards.db"

    await card_store.update_card(wid, cid, {"conclusion": V1}, "写入第一版")
    if data_snapshot._FINGERPRINT_FILE.exists():
        data_snapshot._FINGERPRINT_FILE.unlink()
    r = data_snapshot.snapshot_sync("roundtrip-A")
    stamp = Path(r["dir"]).name
    rel = f"works/{wid}/cards.db"
    await card_store.update_card(wid, cid, {"conclusion": V2}, "写入第二版")
    await card_store.read_card(wid, cid)
    gc.collect()

    open_before = alive()
    res = data_snapshot.restore_from_snapshot(wid, stamp, rel)
    after = (await card_store.read_card(wid, cid))["body"]["status_block"]
    wal = Path(str(db) + "-wal")
    b = wal.read_bytes() if wal.exists() else b""

    rec("V4",
        ok=res.get("ok"), read_after=after, rolled_back=(after == V1),
        sqlite_conns_open_before_restore=open_before,
        unlink_failures=len(alarm.hits), alarm_hits=alarm.hits,
        wal_after={"bytes": len(b), "v1": b.count(V1.encode()), "v2": b.count(V2.encode())},
        main_after={"bytes": db.stat().st_size,
                    "v1": db.read_bytes().count(V1.encode()),
                    "v2": db.read_bytes().count(V2.encode())})
    (OUT / "evidence_r10_v4.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据已写入", OUT / "evidence_r10_v4.json")


if __name__ == "__main__":
    asyncio.run(main())