# -*- coding: utf-8 -*-
"""R10 v7：旁挂存灭到底由什么决定——连接计数受控实测 + 复盘 v6 的 S3 为何"没消失"。

背景：card_014 报告「他和 card_016 观察相反」，判断差别是"还剩几个连接"。
我不能采信任何一方，先自己量三件事：

 A 受控表：修法B 之后，按「写连接 / 只读连接 / 探针连接」逐个关，看 -wal/-shm 何时消失；
 B 真开一个只读连接（file:...?mode=ro）会不会改变消失行为（v6 的池里正是这种连接）；
 C 复盘 v6：完全照 v6 的顺序走，用 sqlite3.connect 挂钩枚举「那一刻还开着谁」，
    解释我 v6 记录到的「全关后仍在」到底是"普遍规律"还是"我漏了一条连接"。
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import shutil
import sqlite3
import sys
import threading
import traceback
from pathlib import Path

WT = Path(r"D:\AI_station\推演agent\.worktrees\5713")
OUT = Path(r"D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\b06")
V1, V2 = "第一版正文", "第二版正文"
EVID = {"stages": {}}

_orig_connect = sqlite3.connect
REC: list[dict] = []
HOOK = {"on": False}


def _connect(*a, **kw):
    conn = _orig_connect(*a, **kw)
    if HOOK["on"]:
        stack = [f"{f.filename.split(chr(92))[-1]}:{f.lineno}"
                 for f in traceback.extract_stack()[:-1]
                 if "5713" in f.filename]
        REC.append({"target": str(a[0] if a else kw.get("database", "?")).rsplit("\\", 1)[-1],
                    "thread": threading.get_ident(), "stack": stack[-3:], "conn": conn})
    return conn


def alive() -> list[dict]:
    out = []
    for r in REC:
        try:
            r["conn"].execute("SELECT 1")
            out.append({"target": r["target"], "thread": r["thread"], "stack": r["stack"]})
        except Exception:
            pass
    return out


def rec(stage, **kw):
    EVID["stages"].setdefault(stage, {}).update(kw)
    try:
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:1100])
    except Exception:
        pass


def sc(db: Path) -> dict:
    w, s = Path(str(db) + "-wal"), Path(str(db) + "-shm")
    return {"wal": ({"exists": False} if not w.exists() else
                    {"exists": True, "bytes": w.stat().st_size,
                     "v1": w.read_bytes().count(V1.encode()), "v2": w.read_bytes().count(V2.encode())}),
            "shm": ({"exists": False} if not s.exists() else
                    {"exists": True, "bytes": s.stat().st_size})}


def fixB(batch_db: Path, db: Path):
    src = _orig_connect(f"file:{batch_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        dst = _orig_connect(str(db), timeout=5.0)
        try:
            src.backup(dst)
            ck = dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return ck


def read(conn, cid="card_001"):
    try:
        r = conn.execute("SELECT status_block FROM cards WHERE card_id=?", (cid,)).fetchone()
        return r[0] if r else None
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}"


def build(root: Path):
    """极简夹具：一个 work，v1 快照 → 改成 v2。返回 (db, batch_db, cid)。"""
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    base = root / "app_home"
    w = base / "works" / "work_001"
    w.mkdir(parents=True)
    db = w / "cards.db"
    con = sqlite3.connect(str(db))
    con.executescript("CREATE TABLE cards(card_id TEXT PRIMARY KEY, status_block TEXT);"
                      "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
    con.execute("INSERT INTO cards VALUES('card_001', ?)", (V1,))
    con.execute("INSERT INTO meta VALUES('seq','1')")
    con.commit()
    con.execute("PRAGMA journal_mode=WAL")
    con.close()
    # 快照副本（模拟 VACUUM INTO 的一致副本）
    batch = root / "snapshots" / "work_001" / "S1" / "works" / "work_001"
    batch.mkdir(parents=True)
    shutil.copy2(db, batch / "cards.db")
    # 现改成 v2（走 WAL）
    c2 = sqlite3.connect(str(db))
    c2.execute("UPDATE cards SET status_block=? WHERE card_id='card_001'", (V2,))
    c2.commit()
    c2.close()
    return db, batch / "cards.db", "card_001"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sqlite3.connect = _connect

    # ---------- A 受控表：逐个关连接 ----------
    root = OUT / "r10_v7_A"
    db, bdb, cid = build(root)
    w_con = sqlite3.connect(str(db))                       # 写连接
    r_con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)   # 只读连接（同 _read_conn 形态）
    before = {"wal": sc(db)["wal"], "write_read": read(w_con, cid), "ro_read": read(r_con, cid)}
    ck = fixB(bdb, db)
    step = {"fixB": {"checkpoint": ck, "sidecars": sc(db),
                     "write_read": read(w_con, cid), "ro_read": read(r_con, cid)}}
    w_con.close()
    step["close_write_only"] = {"sidecars": sc(db), "ro_still_open_read": read(r_con, cid)}
    r_con.close()
    gc.collect()
    step["close_all"] = {"sidecars": sc(db)}
    p = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    step["reopen_probe"] = {"sidecars": sc(db), "probe_read": read(p, cid)}
    p.close()
    gc.collect()
    step["close_probe"] = {"sidecars": sc(db)}
    rec("A_controlled_conn_table", before_fix=before, steps=step)

    # ---------- C 复盘 v6：完全照 v6 顺序，挂钩枚举活连接 ----------
    root2 = OUT / "r10_v7_C"
    db2, bdb2, cid2 = build(root2)
    HOOK["on"] = True
    REC.clear()
    live = sqlite3.connect(str(db2))
    _ = read(live, cid2)
    fixB(bdb2, db2)
    live_read_after = read(live, cid2)
    store_read = None   # v6 里这一步走 card_store（它开只读池连接）；本版改直连只读模拟
    ro_pool = sqlite3.connect(f"file:{db2.as_posix()}?mode=ro", uri=True)   # 模拟 _read_conns 里那条
    _ = read(ro_pool, cid2)
    side_before_close = sc(db2)
    live.close()
    gc.collect()
    side_after_write_close = sc(db2)
    alive_at_that_moment = alive()
    ro_pool.close()
    gc.collect()
    side_after_all_close = sc(db2)
    HOOK["on"] = False
    rec("C_replay_v6_order", live_read_after=live_read_after,
        sidecars_before_close=side_before_close,
        sidecars_after_write_close_only=side_after_write_close,
        connections_still_open_then=alive_at_that_moment,
        sidecars_after_all_close=side_after_all_close,
        note="v6 的 S3 只关了我自己的 live 连，池里那条只读连接没关 ⇒ 旁挂留着；"
             "这说明「关掉活连接后仍不消失」不是普遍规律，而是「还剩连接」时的形态")

    EVID["meta"] = {"code_head": os.popen(f'git -C "{WT}" rev-parse --short HEAD').read().strip(),
                    "item": "旁挂存灭与连接数的关系（纠正 §7.7 的普遍化表述）"}
    (OUT / "evidence_r10_v7.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据已写入", OUT / "evidence_r10_v7.json")


if __name__ == "__main__":
    main()