# -*- coding: utf-8 -*-
"""R10 v6：修法B（SQLite backup API 反向导入）在有活连接时的**落盘形态**与**验收判据**。

起因：card_014 独立复现后提醒——修法B 之后 `-wal` 文件**仍存在、只是被 checkpoint 截断为 0 字节**
（不是「旁挂被删掉了」），故验收不得用「旁挂不存在」当判据。
我在 v5 里看到的却是 `-wal: null`（文件没了）——两者不矛盾：v5 那一步所有连接都已关闭，
SQLite 在最后一个连接正常关闭时会删掉 -wal。本探针把「有活连接」这一生产形态显式摆进来，
量三件事：
 1. 修法B 之后 `-wal`/`-shm` 到底在不在、多大；
 2. 三条读路径（原活连接 / card_store 新连接 / 裸只读连接）各自读到哪一版；
 3. 「旁挂不存在」这条错误判据与「读回是快照态」这条正确判据在同一现场会给出相反结论。
"""
from __future__ import annotations

import asyncio
import gc
import json
import shutil
import sqlite3
import sys
from pathlib import Path

WT = Path(r"D:\AI_station\推演agent\.worktrees\5713")
OUT = Path(r"D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\b06")
V1, V2 = "第一版正文", "第二版正文"
EVID = {"stages": {}}


def rec(stage, **kw):
    EVID["stages"].setdefault(stage, {}).update(kw)
    try:
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:1100])
    except Exception:
        pass


def sidecars(db: Path) -> dict:
    out = {}
    for suf in ("", "-wal", "-shm"):
        p = Path(str(db) + suf)
        out[suf or "main"] = (None if not p.exists() else
                              {"exists": True, "bytes": p.stat().st_size,
                               "v1": p.read_bytes().count(V1.encode()),
                               "v2": p.read_bytes().count(V2.encode())})
        if out[suf or "main"] is None:
            out[suf or "main"] = {"exists": False}
    return out


def read_via(conn: sqlite3.Connection, cid: str):
    try:
        row = conn.execute("SELECT status_block FROM cards WHERE card_id=?", (cid,)).fetchone()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}: {exc}"


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.path.insert(0, str(WT))
    root = OUT / "r10_v6"
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

    wdata = await work_store.create_work("R10 v6")
    wid, cid = wdata["work_id"], wdata["work_card_id"]
    db = works / wid / "cards.db"

    await card_store.update_card(wid, cid, {"conclusion": V1}, "写入第一版")
    if data_snapshot._FINGERPRINT_FILE.exists():
        data_snapshot._FINGERPRINT_FILE.unlink()
    r = data_snapshot.snapshot_sync("v6")
    stamp = Path(r["dir"]).name
    batch_db = data_snapshot.SNAPSHOT_ROOT / wid / stamp / f"works/{wid}/cards.db"
    await card_store.update_card(wid, cid, {"conclusion": V2}, "写入第二版")

    # ===== 生产形态：留一条活连接（读一次、不关，模拟实例常驻连接）=====
    live = sqlite3.connect(str(db), timeout=5.0)
    live_read_before = read_via(live, cid)
    rec("S1_live_connection_held", read=live_read_before,
        journal_mode=live.execute("PRAGMA journal_mode").fetchone()[0],
        sidecars_before_fix=sidecars(db))

    # ===== 修法B：backup API 反向导入 + checkpoint(TRUNCATE)；全程不关 live =====
    err = None
    try:
        src = sqlite3.connect(f"file:{batch_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            dst = sqlite3.connect(str(db), timeout=5.0)
            try:
                src.backup(dst)
                ck = dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        ck = None
    live_read_after = read_via(live, cid)
    card_db.reset_caches()
    gc.collect()
    store_read = (await card_store.read_card(wid, cid))["body"]["status_block"]
    fresh = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        fresh_read = read_via(fresh, cid)
    finally:
        fresh.close()
    sc = sidecars(db)
    rec("S2_after_fixB_with_live_conn", error=err, checkpoint=ck,
        live_conn_read=live_read_after, card_store_read=store_read, fresh_ro_read=fresh_read,
        sidecars_after_fix=sc,
        criterion_sidecar_absent=(not sc.get("-wal", {}).get("exists")),
        criterion_read_is_snapshot=(store_read == V1),
        verdict=("「旁挂不存在」判据：%s ／「读回快照态」判据：%s"
                 % ("通过" if not sc.get("-wal", {}).get("exists") else "不通过",
                    "通过" if store_read == V1 else "不通过")))

    # ===== 收尾：关掉活连接后旁挂是否消失（解释 v5 为何看到 null）=====
    live.close()
    card_db.reset_caches()
    gc.collect()
    rec("S3_after_close_live", sidecars=sidecars(db),
        note="v5 看到 -wal:null 就是因为那一步没有活连接：SQLite 在最后一个连接正常关闭时删掉 -wal")

    EVID["meta"] = {"code_head": __import__("os").popen(f'git -C "{WT}" rev-parse --short HEAD').read().strip(),
                    "item": "修法B 的落盘形态与验收判据（R10 场景）"}
    (OUT / "evidence_r10_v6.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据已写入", OUT / "evidence_r10_v6.json")


if __name__ == "__main__":
    asyncio.run(main())