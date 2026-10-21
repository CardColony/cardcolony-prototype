# -*- coding: utf-8 -*-
"""R10 v8：旁挂消失与否，到底由「连接数」还是「有没有只读连接（mode=ro）碰过」决定？

v7 已看到：写连接 + 只读连接都关掉、gc 之后，`-wal`(0B) 与 `-shm`(32768B) **仍在**。
这与 card_014 的「全关即消失」不符 ⇒ 他们的「区别是还剩几个连接」这个归因不足以解释。

本版做严格 A/B，唯一变量＝**是否有一个 mode=ro 的只读连接参与过**：
 场景 RW：全程只用读写连接（fixB 的 dst 也是读写连接）
 场景 RO：多开一条 file:...?mode=ro 只读连接读一次
两场景其余完全一致（同夹具、同修法B、同样全关到底）。
"""
from __future__ import annotations

import gc
import json
import os
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
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:1200])
    except Exception:
        pass


def sc(db: Path) -> dict:
    out = {}
    for suf in ("-wal", "-shm"):
        p = Path(str(db) + suf)
        out[suf] = ({"exists": False} if not p.exists()
                    else {"exists": True, "bytes": p.stat().st_size})
    return out


def build(root: Path):
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    w = root / "app_home" / "works" / "work_001"
    w.mkdir(parents=True)
    db = w / "cards.db"
    con = sqlite3.connect(str(db))
    con.executescript("CREATE TABLE cards(card_id TEXT PRIMARY KEY, status_block TEXT);")
    con.execute("INSERT INTO cards VALUES('card_001', ?)", (V1,))
    con.commit()
    con.execute("PRAGMA journal_mode=WAL")
    con.close()
    batch = root / "snapshots" / "work_001" / "S1" / "works" / "work_001"
    batch.mkdir(parents=True)
    shutil.copy2(db, batch / "cards.db")
    c2 = sqlite3.connect(str(db))
    c2.execute("UPDATE cards SET status_block=? WHERE card_id='card_001'", (V2,))
    c2.commit()
    c2.close()
    return db, batch / "cards.db", "card_001"


def read(c, cid="card_001"):
    try:
        r = c.execute("SELECT status_block FROM cards WHERE card_id=?", (cid,)).fetchone()
        return r[0] if r else None
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}"


def fixB(bdb: Path, db: Path):
    s = sqlite3.connect(f"file:{bdb.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        d = sqlite3.connect(str(db), timeout=5.0)
        try:
            s.backup(d)
            d.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            d.commit()
        finally:
            d.close()
    finally:
        s.close()


def scenario(tag: str, use_ro: bool):
    root = OUT / f"r10_v8_{tag}"
    db, bdb, cid = build(root)
    live = sqlite3.connect(str(db))                  # 唯一常驻读写连接（模拟实例的写连接）
    ro = None
    if use_ro:
        ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)   # 同 card_db._read_conn 形态
        read(ro, cid)
    _ = read(live, cid)
    fixB(bdb, db)
    mid = {"sidecars": sc(db), "live_read": read(live, cid),
           "ro_read": read(ro, cid) if ro else None}
    live.close()
    if ro:
        ro.close()
    gc.collect()
    end = {"sidecars": sc(db)}
    # 关完之后再开一个读写连接读一次，看内容与旁挂
    p = sqlite3.connect(str(db))
    end["reopen_rw_read"] = read(p, cid)
    end["sidecars_after_reopen"] = sc(db)
    p.close()
    gc.collect()
    end["sidecars_final"] = sc(db)
    rec(f"SCENARIO_{tag}", use_readonly_conn=use_ro, after_fix=mid, after_all_closed=end)
    return end["sidecars_final"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    a = scenario("RW_only", use_ro=False)
    b = scenario("RO_involved", use_ro=True)
    rec("VERDICT",
        rw_only_final=a, ro_involved_final=b,
        same=(a == b),
        conclusion=("旁挂存灭由「是否有只读连接(mode=ro)参与过」决定，而不是「还剩几个连接」"
                    if a != b else "两场景一致——归因需另找"))

    EVID["meta"] = {"code_head": os.popen(f'git -C "{WT}" rev-parse --short HEAD').read().strip(),
                    "item": "旁挂存灭的真正变量：只读连接 vs 连接数"}
    (OUT / "evidence_r10_v8.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据已写入", OUT / "evidence_r10_v8.json")


if __name__ == "__main__":
    main()