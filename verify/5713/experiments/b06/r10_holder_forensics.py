# -*- coding: utf-8 -*-
"""R10 定性探针 v3：把「未回滚」钉到具体机制，并区分两种现场。

v2 的教训：我先手工调了 `card_db.close_work()` 再 restore，等于替产品把活干了
（WAL 在最后一次连接关闭时就被 SQLite 自己删掉）⇒ 那一次绿是**假绿**。
本版不预调 close_work，完全照 pytest 顺序走，且在**告警发生的那一瞬间**抓现场：

S1 原始顺序（＝pytest 用例）：update v1 → 快照 → update v2 → read → restore → read
S2 对照：同 S1，但在 restore 之前 `gc.collect()` 并把 asyncio 线程池等干净（判断是不是线程残留）
两段都在「恢复后清理旁挂文件失败」这条 warning 触发的时刻记录：本进程句柄 + card_db 缓存表。
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

WT = Path(r"D:\AI_station\推演agent\.worktrees\5713")
OUT = Path(r"D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\b06")
V1, V2 = "第一版正文", "第二版正文"
EVID: dict = {"stages": {}}


def rec(stage, **kw):
    EVID["stages"].setdefault(stage, {}).update(kw)
    try:
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:900])
    except Exception:
        pass


def holders(suffix=""):
    try:
        import psutil
    except Exception:
        return "psutil-unavailable"
    return [f.path.rsplit("\\", 1)[-1] for f in psutil.Process().open_files()
            if "cards.db" in f.path]


def tables():
    from src.storage import card_db
    return {"_conns": [k.rsplit("\\", 1)[-1] for k in getattr(card_db, "_conns", {})],
            "_read_conns": [f"{k[0].rsplit(chr(92), 1)[-1]}@{k[1]}"
                            for k in getattr(card_db, "_read_conns", {})]}


class Alarm(logging.Handler):
    """抓住 data_snapshot 的旁挂清理告警，在**同一瞬间**取现场。"""

    def __init__(self):
        super().__init__()
        self.hits = []

    def emit(self, record):
        if "旁挂文件失败" in record.getMessage():
            self.hits.append({"at": time.strftime("%H:%M:%S"),
                              "holders": holders(), "card_db": tables()})


async def scenario(tag: str, pre_gc: bool):
    root = OUT / f"r10_{tag}"
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
    lg = logging.getLogger("storage.snapshot")
    lg.addHandler(alarm)

    wdata = await work_store.create_work(f"R10 {tag}")
    wid, cid = wdata["work_id"], wdata["work_card_id"]
    db = works / wid / "cards.db"

    await card_store.update_card(wid, cid, {"conclusion": V1}, "写入第一版")
    if data_snapshot._FINGERPRINT_FILE.exists():
        data_snapshot._FINGERPRINT_FILE.unlink()
    r = data_snapshot.snapshot_sync("roundtrip-A")
    stamp = Path(r["dir"]).name
    rel = f"works/{wid}/cards.db"
    batch_db = data_snapshot.SNAPSHOT_ROOT / wid / stamp / rel

    await card_store.update_card(wid, cid, {"conclusion": V2}, "写入第二版")
    before = (await card_store.read_card(wid, cid))["body"]["status_block"]

    if pre_gc:
        gc.collect()
        await asyncio.sleep(0.05)
        state_before_restore = {"holders": holders(), "card_db": tables()}
    else:
        state_before_restore = {"holders": holders(), "card_db": tables()}

    res = data_snapshot.restore_from_snapshot(wid, stamp, rel)
    after = (await card_store.read_card(wid, cid))["body"]["status_block"]
    wal = Path(str(db) + "-wal")
    lg.removeHandler(alarm)

    def scan(p: Path):
        if not p.exists():
            return None
        b = p.read_bytes()
        return {"bytes": len(b), "v1": b.count(V1.encode()), "v2": b.count(V2.encode())}

    rec(f"{tag}", read_before=before, read_after=after, ok=res.get("ok"),
        rolled_back=(after == V1),
        unlink_failures=len(alarm.hits), alarm_hits=alarm.hits,
        state_before_restore=state_before_restore,
        main_after=scan(db), wal_after=scan(wal),
        batch_db_status=scan(batch_db))
    return root


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.path.insert(0, str(WT))
    logging.basicConfig(level=logging.INFO)
    await scenario("S1_original_order", pre_gc=False)
    await scenario("S2_pre_gc", pre_gc=True)
    EVID["meta"] = {"code_head": __import__("os").popen(f'git -C "{WT}" rev-parse --short HEAD').read().strip(),
                    "item": "R10 test_snapshot_restore_roundtrip"}
    (OUT / "evidence_r10.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据已写入", OUT / "evidence_r10.json")


if __name__ == "__main__":
    asyncio.run(main())