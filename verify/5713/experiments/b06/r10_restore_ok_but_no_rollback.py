# -*- coding: utf-8 -*-
"""R10 定性探针 v5：验证「不靠删文件」的修法能不能真回滚。

v3/v4 已证：告警那一刻 card_db 缓存连接皆空、记录在案的 sqlite 连接也全关，
但仍报 WinError 32 —— 说明「先关连接、再删 -wal/-shm、再覆盖主库」这条路不可靠。
本版在**同一个已经变红的现场**上试两种修法，看哪种真能把内容拨回快照态：

修法A（现写法补强）：删旁挂（带重试）→ 再 copy 主库
修法B（SQLite 原生）：sqlite3 backup API 把快照库**写进**活库 → wal_checkpoint(TRUNCATE)
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
EVID = {"stages": {}}


def rec(stage, **kw):
    EVID["stages"].setdefault(stage, {}).update(kw)
    try:
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:1000])
    except Exception:
        pass


def status(db: Path, cid: str):
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            row = con.execute("SELECT status_block FROM cards WHERE card_id=?", (cid,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}: {exc}"


def files(db: Path):
    out = {}
    for suf in ("", "-wal", "-shm"):
        p = Path(str(db) + suf)
        out[suf or "main"] = (None if not p.exists() else
                              {"b": p.stat().st_size,
                               "v1": p.read_bytes().count(V1.encode()),
                               "v2": p.read_bytes().count(V2.encode())})
    return out


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.path.insert(0, str(WT))
    root = OUT / "r10_v5"
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

    wdata = await work_store.create_work("R10 v5")
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

    # ===== 先复现红（产品现写法）=====
    res = data_snapshot.restore_from_snapshot(wid, stamp, rel)
    red = status(db, cid)
    rec("S0_reproduce_red", ok=res.get("ok"), read=red, rolled_back=(red == V1),
        files=files(db))

    # ===== 修法A：删旁挂（带重试）+ 覆盖主库 =====
    card_db.reset_caches()
    gc.collect()
    deleted, errs = [], {}
    for suf in ("-wal", "-shm"):
        p = Path(str(db) + suf)
        for attempt in range(5):
            try:
                if p.exists():
                    p.unlink()
                deleted.append(suf)
                break
            except OSError as exc:
                errs[suf] = f"{type(exc).__name__}: {exc}"
                time.sleep(0.05 * (2 ** attempt))
    if not errs:
        card_db.reset_caches()
        shutil.copy2(batch_db, db)
        gc.collect()
    a_read = status(db, cid)
    rec("FIX_A_delete_sidecars_then_copy", deleted=deleted, errors=errs,
        read=a_read, rolled_back=(a_read == V1), files=files(db))

    # ===== 回到第二版，再试修法B：sqlite backup API 写回 =====
    card_db.reset_caches()
    await card_store.update_card(wid, cid, {"conclusion": V2}, "再次写入第二版")
    b_before = status(db, cid)
    card_db.reset_caches()
    b_err = None
    try:
        src = sqlite3.connect(f"file:{batch_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            dst = sqlite3.connect(str(db), timeout=5.0)
            try:
                src.backup(dst)                      # 快照库 → 活库（覆盖式）
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as exc:  # noqa: BLE001
        b_err = f"{type(exc).__name__}: {exc}"
    card_db.reset_caches()
    gc.collect()
    b_read = status(db, cid)
    rec("FIX_B_sqlite_backup_api", before=b_before, error=b_err,
        read=b_read, rolled_back=(b_read == V1), files=files(db))

    EVID["meta"] = {"code_head": __import__("os").popen(f'git -C "{WT}" rev-parse --short HEAD').read().strip(),
                    "item": "R10 test_snapshot_restore_roundtrip / 修法候选"}
    (OUT / "evidence_r10_v5.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据已写入", OUT / "evidence_r10_v5.json")


if __name__ == "__main__":
    asyncio.run(main())