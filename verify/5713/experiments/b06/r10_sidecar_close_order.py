# -*- coding: utf-8 -*-
"""R10 v9：旁挂能否消失，由「最后关闭的那条连接有没有写权限」决定？

v8 已看到关键反差（两场景唯一差别是"有没有一条 mode=ro 只读连接碰过"）：
  RW_only ：全关后 → `-wal`/`-shm` **双双消失**
  RO涉及  ：全关后 → **仍在**（0B / 32768B）；再开一条读写连接读一次并关掉后 → 才消失

v7 之所以看到"全关后仍在"，复盘其代码：最后一次关闭的是 **mode=ro 探针/池连接**。
⇒ 假设：**删 -wal/-shm 需要写权限；只读连接（`file:...?mode=ro`，正是 `card_db._read_conn` 的形态）
关闭时删不掉，文件就留着；直到下一次"读写连接干净关闭"才被清掉。**

本版只改「关闭顺序」这一个变量，四种顺序各跑一遍：
  O1 只有读写连接            → 关它
  O2 只读先开、读写后开      → 先关只读、再关读写
  O3 同上                   → 先关读写（此时只读还在）、再关只读
  O4 O3 之后               → 再开一条读写连接读一次并关闭
预测（若假设成立）：O1 消失／O2 消失／O3 留着／O4 消失。
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
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:1000])
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
    c = sqlite3.connect(str(db))
    c.executescript("CREATE TABLE cards(card_id TEXT PRIMARY KEY, status_block TEXT);")
    c.execute("INSERT INTO cards VALUES('card_001', ?)", (V1,))
    c.commit()
    c.execute("PRAGMA journal_mode=WAL")
    c.close()
    b = root / "snapshots" / "work_001" / "S1" / "works" / "work_001"
    b.mkdir(parents=True)
    shutil.copy2(db, b / "cards.db")
    c2 = sqlite3.connect(str(db))
    c2.execute("UPDATE cards SET status_block=? WHERE card_id='card_001'", (V2,))
    c2.commit()
    c2.close()
    return db, b / "cards.db"


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


def rw(db: Path):
    return sqlite3.connect(str(db), timeout=5.0)


def ro(db: Path):
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)


def run(tag, order):
    root = OUT / f"r10_v9_{tag}"
    db, bdb = build(root)
    conns = {}
    if order in ("O2_ro_first_then_rw", "O3_rw_first_then_ro", "O4_then_rw_reopen"):
        conns["ro"] = ro(db)
        read(conns["ro"])
    conns["rw"] = rw(db)
    read(conns["rw"])
    fixB(bdb, db)
    after_fix = {"sidecars": sc(db), "rw_read": read(conns["rw"],
                                                     "card_001")}
    if order in ("O1_rw_only",):
        conns["rw"].close()
    elif order == "O2_ro_first_then_rw":
        conns["ro"].close()
        conns["rw"].close()
    elif order in ("O3_rw_first_then_ro", "O4_then_rw_reopen"):
        conns["rw"].close()
        conns["ro"].close()
    gc.collect()
    after_close = sc(db)
    extra = None
    final = after_close
    if order == "O4_then_rw_reopen":
        p = rw(db)
        extra = {"read": read(p)}
        p.close()
        gc.collect()
        extra["sidecars"] = sc(db)
        final = extra["sidecars"]          # O4 的"最终现场"在 reopen 之后，不能取 after_close
    rec(tag, order=order, after_fix=after_fix, sidecars_after_all_closed=after_close,
        reopen_rw=extra, sidecars_final=final)
    return final


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    o1 = run("O1_rw_only", "O1_rw_only")
    o2 = run("O2_ro_first_then_rw", "O2_ro_first_then_rw")
    o3 = run("O3_rw_first_then_ro", "O3_rw_first_then_ro")
    o4 = run("O4_then_rw_reopen", "O4_then_rw_reopen")

    def gone(s):
        return s["-wal"]["exists"] is False and s["-shm"]["exists"] is False

    rec("VERDICT",
        O1_gone=gone(o1), O2_gone=gone(o2), O3_gone=gone(o3), O4_gone=gone(o4),
        rule_holds=(gone(o1) and gone(o2) and not gone(o3) and gone(o4)),
        rule="删 -wal/-shm 需写权限：最后关闭的是 mode=ro 只读连接时删不掉（文件留着），"
             "直到下一次有读写连接干净关闭才被清掉",
        implication="凡涉及只读连接池（card_db._read_conns 就是 mode=ro）的实例，"
                    "旁挂存灭取决于关闭顺序 ⇒ 任何「断言旁挂存在/不存在」的用例都会跟顺序漂")
    EVID["meta"] = {"code_head": os.popen(f'git -C "{WT}" rev-parse --short HEAD').read().strip(),
                    "item": "旁挂存灭的真正变量＝最后关闭者的写权限（关闭顺序）"}
    (OUT / "evidence_r10_v9.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("证据已写入", OUT / "evidence_r10_v9.json")


if __name__ == "__main__":
    main()