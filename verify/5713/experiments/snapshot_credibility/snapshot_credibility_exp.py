# -*- coding: utf-8 -*-
"""5713 数据快照/恢复链路「真删真恢复」实验（card_009 · 第一轮交付）。

纪律：
- 只用真文件、真 sqlite 库、真删除、真恢复；无桩、无假实现。
- 全部写操作限制在 5713_sandbox/experiments/snapshot_credibility 内；
  data_snapshot.BASE_DIR / SNAPSHOT_ROOT / card_store.WORKS_DIR 指到实验根
  （与 scripts/run_test_instance_5701.py 的沙箱重定向同一手法），生产快照根零触碰。
- 恢复动作调用仓库已发布的两条通道：
  (a) 根目录 restore_from_snapshot.py 的 restore_batch()（只重定向模块级根常量）
  (b) src/storage/data_snapshot.py 的 restore_from_snapshot()
输出：experiments/snapshot_credibility/evidence.json
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sbx_candidates = [d for d in Path(r"D:\AI_station").iterdir()
                  if d.is_dir() and d.name.endswith("agent")]
WT = None
for c in sbx_candidates:
    p = c / ".worktrees" / "5713"
    if p.is_dir():
        WT = p
        break
assert WT is not None
SBX = WT.parent / "5713_sandbox"
EXP = SBX / "experiments" / "snapshot_credibility"
BASE = EXP / "base"
SNAP = EXP / "snapshots"
SRC_WORK = SBX / "works" / "work_007"
WID = "work_007"

EV: dict = {"run_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "wt": str(WT),
            "exp_root": str(EXP), "phases": {}}


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(root: Path) -> dict:
    out = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        n = p.name
        if n.endswith(".db") or n.endswith(".db-wal") or n.endswith(".db-shm"):
            continue
        out[str(p.relative_to(root)).replace("\\", "/")] = sha(p)
    return out


def db_state(db: Path) -> dict:
    if not db.exists():
        return {"exists": False}
    con = sqlite3.connect(str(db))
    try:
        st = {"exists": True, "size": db.stat().st_size}
        st["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        st["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
        tbls = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        st["rowcounts"] = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tbls}
        rows = sorted((r[0], r[1]) for r in con.execute("SELECT card_id, title FROM cards"))
        st["cards"] = rows
        st["cards_sha"] = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()
        return st
    finally:
        con.close()


def batches(work: str = WID) -> list[str]:
    d = SNAP / work
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def real_batches(work: str = WID) -> list[str]:
    return [b for b in batches(work) if not b.startswith("daily_")]


def batch_files(stamp: str, work: str = WID) -> list[str]:
    d = SNAP / work / stamp
    return sorted(str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*") if p.is_file())


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ---------- P0 隔离实验根 + 素材 ----------
    if EXP.exists():
        shutil.rmtree(EXP)
    (BASE / "works").mkdir(parents=True)
    SNAP.mkdir(parents=True)

    sys.path.insert(0, str(WT))
    os.chdir(str(WT))
    from src.storage import data_snapshot, card_store

    data_snapshot.BASE_DIR = BASE
    data_snapshot.SNAPSHOT_ROOT = SNAP
    data_snapshot._FINGERPRINT_FILE = BASE / ".snapshot_fingerprints.json"
    card_store.WORKS_DIR = BASE / "works"

    dst = BASE / "works" / WID
    shutil.copytree(SRC_WORK, dst)
    EV["phases"]["P0_setup"] = {
        "material": str(SRC_WORK),
        "files": batch_files.__name__ and sorted(str(p.relative_to(dst)).replace("\\", "/")
                                                 for p in dst.rglob("*") if p.is_file()),
        "db_state": db_state(dst / "cards.db"),
        "snapshot_root": str(data_snapshot.SNAPSHOT_ROOT),
        "base_dir": str(data_snapshot.BASE_DIR),
    }

    # ---------- P1 真拍快照（计时 + 批次内容） ----------
    t0 = time.perf_counter()
    r1 = data_snapshot.snapshot_sync("P1 baseline", WID)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    stamp = real_batches()[-1]
    EV["phases"]["P1_snapshot"] = {
        "result": r1, "elapsed_ms": ms, "batches": batches(), "stamp": stamp,
        "batch_files": batch_files(stamp),
        "pre_delete_tree_sha_files": len(tree_hash(dst)),
        "pre_delete_db": db_state(dst / "cards.db"),
    }
    pre_tree = tree_hash(dst)
    pre_db = db_state(dst / "cards.db")

    # ---------- P2 真删整目录 → 仓库脚本整批恢复 ----------
    shutil.rmtree(dst)
    gone = not dst.exists()
    spec = importlib.util.spec_from_file_location(
        "shipped_restore", str(WT / "restore_from_snapshot.py"))
    sr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sr)
    sr.SNAPSHOT_ROOT = SNAP
    sr.BASE_DIR = BASE
    sr.BACKUP_ROOT = EXP / "pre_restore_backups"
    rb = sr.restore_batch(WID, stamp)
    post_tree = tree_hash(dst)
    post_db = db_state(dst / "cards.db")
    EV["phases"]["P2_delete_restore"] = {
        "dir_deleted": gone, "restore_return": rb,
        "files_expected_non_db": len(pre_tree),
        "files_same": sum(1 for k in pre_tree if post_tree.get(k) == pre_tree[k]),
        "files_diff": [k for k in pre_tree if k in post_tree and post_tree[k] != pre_tree[k]],
        "files_missing": [k for k in pre_tree if k not in post_tree],
        "files_extra": [k for k in post_tree if k not in pre_tree],
        "pre_db": pre_db, "post_db": post_db,
        "db_logical_equal": pre_db.get("cards_sha") == post_db.get("cards_sha"),
        "integrity_after": post_db.get("integrity"),
        "wal_shm_after": sorted(p.name for p in dst.iterdir() if p.name.startswith("cards.db-")),
    }

    # ---------- P3 单文件恢复（库函数通道） ----------
    sess = dst / "sessions" / "session_001.md"
    sess_before = sha(sess)
    sess.write_text("已被破坏的内容\n", encoding="utf-8")
    corrupted = sha(sess)
    rf = data_snapshot.restore_from_snapshot(WID, stamp, "works/work_007/sessions/session_001.md")
    after = sha(sess)
    EV["phases"]["P3_single_file_restore"] = {
        "pre_hash": sess_before, "corrupted_hash": corrupted, "after_hash": after,
        "corrupted_differs_from_pre": corrupted != sess_before,
        "restore_return": rf, "restored_exact": after == sess_before,
    }

    # ---------- P4 快照之后新增/修改的文件会不会被回退 ----------
    (dst / "cards").mkdir(exist_ok=True)
    newf = dst / "cards" / "card_999.md"
    newf.write_text("快照之后才建的文件\n", encoding="utf-8")
    wj = dst / "work.json"
    wj_before = sha(wj)
    wj.write_text(wj.read_text(encoding="utf-8") + "\n# 快照后被改\n", encoding="utf-8")
    rb2 = sr.restore_batch(WID, stamp)
    EV["phases"]["P4_no_delete_semantics"] = {
        "restore_return": rb2,
        "post_snapshot_new_file_still_present": newf.exists(),
        "modified_file_reverted": sha(wj) == wj_before,
    }

    # ---------- P5 works/ 之外的写入有没有进快照 ----------
    tw = BASE / "trading-workspace" / "notes"
    tw.mkdir(parents=True, exist_ok=True)
    note = tw / "user_note.md"
    note.write_text("用户在文件工具里落的正文\n", encoding="utf-8")
    r5 = data_snapshot.snapshot_sync("P5 coverage", WID)
    latest5 = real_batches()[-1]
    note.unlink()
    rf5 = data_snapshot.restore_from_snapshot(WID, latest5, "trading-workspace/notes/user_note.md")
    EV["phases"]["P5_coverage_gap"] = {
        "snapshot_result": r5,
        "batch_files_after": batch_files(latest5),
        "trading_workspace_in_batch": any("trading-workspace" in f for f in batch_files(latest5)),
        "file_recoverable": note.exists(), "restore_return": rf5,
        "protected_dirs": list(getattr(data_snapshot, "PROTECTED_DIRS", ())),
    }

    # ---------- P6 指纹盲区：同尺寸 + 同秒 mtime 的修改 ----------
    tgt = dst / "sessions" / "session_001.md"
    b0 = tgt.read_bytes()
    st = tgt.stat()
    nb = bytearray(b0)
    flip = None
    for i, c in enumerate(nb):
        if c in b"0123456789abcdefghijklmnopqrstuvwxyz":
            nb[i] = ord("Z") if c != ord("Z") else ord("Y")
            flip = i
            break
    assert flip is not None
    tgt.write_bytes(bytes(nb))
    os.utime(tgt, (st.st_atime, st.st_mtime))
    n_before = len(real_batches())
    r6 = data_snapshot.snapshot_sync("P6 fingerprint", WID)
    n_after = len(real_batches())
    tgt.unlink()
    data_snapshot.restore_from_snapshot(WID, stamp, "works/work_007/sessions/session_001.md")
    got = tgt.read_bytes()
    EV["phases"]["P6_fingerprint_blind"] = {
        "flip_offset": flip, "size_before": len(b0), "size_after": len(nb),
        "mtime_forced_equal": True,
        "snapshot_result": r6,
        "batches_before": n_before, "batches_after": n_after,
        "new_batch_created": n_after > n_before,
        "after_delete_restore_equals_v1": got == b0,
        "after_delete_restore_equals_v2": got == bytes(nb),
    }

    # ---------- P7 日备是不是「当天最终状态」 ----------
    daily = [d for d in batches() if d.startswith("daily_")]
    tgt.write_text(tgt.read_text(encoding="utf-8") + "\n# 尺寸变化触发新批次\n", encoding="utf-8")
    r7 = data_snapshot.snapshot_sync("P7 daily", WID)
    last_batch = real_batches()[-1]
    dfile = SNAP / WID / daily[-1] / "works" / WID / "sessions" / "session_001.md"
    bfile = SNAP / WID / last_batch / "works" / WID / "sessions" / "session_001.md"
    live = tgt.read_bytes()
    EV["phases"]["P7_daily_backup"] = {
        "snapshot_result": r7, "daily_batches": daily, "latest_batch": last_batch,
        "daily_newest_file_equals_live": dfile.read_bytes() == live if dfile.exists() else None,
        "latest_batch_equals_live": bfile.read_bytes() == live,
        "daily_equals_latest_batch": dfile.read_bytes() == bfile.read_bytes() if dfile.exists() else None,
        "daily_file_age_seconds": round(time.time() - dfile.stat().st_mtime, 1) if dfile.exists() else None,
    }

    # ---------- P8 库文件被外部替换时，「活着的连接」还能不能拿到恢复后的数据 ----------
    dbp = dst / "cards.db"
    data_snapshot.snapshot_sync("P8 S1", WID)
    s1_stamp = real_batches()[-1]
    con = sqlite3.connect(str(dbp))
    cols = [d[1] for d in con.execute("PRAGMA table_info(cards)")]
    row = dict(zip(cols, con.execute("SELECT * FROM cards LIMIT 1").fetchone()))
    row["card_id"] = "card_777"
    row["title"] = "P8 追加卡"
    con.execute(f"INSERT INTO cards ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row[c] for c in cols])
    con.commit()
    con.close()
    data_snapshot.snapshot_sync("P8 S2", WID)
    s2_stamp = real_batches()[-1]
    s2_cards = db_state(dbp).get("cards")
    live_conn = sqlite3.connect(str(dbp))
    cnt_before = live_conn.execute("SELECT count(*) FROM cards").fetchone()[0]
    rf8 = data_snapshot.restore_from_snapshot(WID, s1_stamp, "works/work_007/cards.db")
    disk_state = db_state(dbp)
    try:
        cnt_live = live_conn.execute("SELECT count(*) FROM cards").fetchone()[0]
        live_read_err = None
    except Exception as exc:  # noqa: BLE001
        cnt_live, live_read_err = None, f"{type(exc).__name__}: {exc}"
    try:
        live_conn.execute("UPDATE cards SET title = title || ' [活连接写入]'")
        live_conn.commit()
        live_write_err = None
    except Exception as exc:  # noqa: BLE001
        live_write_err = f"{type(exc).__name__}: {exc}"
    live_conn.close()
    EV["phases"]["P8_live_connection"] = {
        "s1_batch": s1_stamp, "s2_batch": s2_stamp,
        "s2_batch_cards_at_snapshot_time": s2_cards,
        "live_conn_count_before_restore": cnt_before,
        "restore_return": rf8,
        "disk_state_after_restore_cards": disk_state.get("cards"),
        "disk_journal_mode_after_restore": disk_state.get("journal_mode"),
        "live_conn_count_after_restore": cnt_live,
        "live_read_err": live_read_err,
        "live_write_err": live_write_err,
        "final_state": db_state(dbp),
        "wal_shm_after": sorted(p.name for p in dst.iterdir() if p.name.startswith("cards.db-")),
    }

    # ---------- P9 仓库自带恢复 CLI 指向哪个根 ----------
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cp = subprocess.run([sys.executable, "restore_from_snapshot.py", "--list"],
                        cwd=str(WT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=env)
    EV["phases"]["P9_shipped_cli"] = {
        "cmd": "python restore_from_snapshot.py --list  (cwd=.worktrees/5713)",
        "returncode": cp.returncode,
        "stdout": cp.stdout.strip(), "stderr": cp.stderr.strip()[:500],
        "computed_root": str(WT.parent / "agent_data_snapshots"),
        "computed_root_exists": (WT.parent / (WT.parent.name and "agent_data_snapshots")).exists(),
    }

    # ---------- P10 恢复后经产品读路径能否读到；journal_mode 是否自愈 ----------
    from src.storage import card_db
    import asyncio
    got10, err10 = None, None
    try:
        got10 = asyncio.run(card_db.run_read(WID, lambda c: [tuple(r) for r in c.execute(
            "SELECT card_id, title FROM cards ORDER BY card_id")]))
    except Exception as exc:  # noqa: BLE001
        err10 = f"{type(exc).__name__}: {exc}"
    EV["phases"]["P10_read_after_restore"] = {
        "cards_via_product_path": got10, "error": err10,
        "journal_mode_after_product_connect": db_state(dbp).get("journal_mode"),
        "wal_shm_now": sorted(p.name for p in dst.iterdir() if p.name.startswith("cards.db-")),
    }

    # ---------- P11 真实实例里快照根盖住了多大一块数据 ----------
    def dir_size(p: Path) -> dict | None:
        if not p.exists():
            return None
        n = b = 0
        for q in p.rglob("*"):
            if q.is_file():
                n += 1
                b += q.stat().st_size
        return {"files": n, "bytes": b}
    EV["phases"]["P11_coverage_measure"] = {
        "instance_data_roots": {name: dir_size(SBX / name)
                                for name in ("works", "trading-workspace", "logs", "推演agent")},
        "snapshot_root_size": dir_size(SBX / "snapshots"),
        "protected_roots_in_code": list(getattr(data_snapshot, "PROTECTED_DIRS", ())),
    }

    # ---------- P12 真实实例「有数据 vs 有快照」对账 ----------
    def newest(root: Path) -> float:
        ts = [q.stat().st_mtime for q in root.rglob("*") if q.is_file()]
        return max(ts) if ts else 0.0

    audit = []
    INST_SNAP = SBX / "snapshots"
    def inst_batches(work):
        d = INST_SNAP / work
        if not d.exists():
            return []
        return sorted(x.name for x in d.iterdir() if x.is_dir())
    for w in sorted((SBX / "works").iterdir()):
        if not w.is_dir():
            continue
        bs = inst_batches(w.name)
        real = [b for b in bs if not b.startswith("daily_")]
        dn = [b for b in bs if b.startswith("daily_")]
        nw = newest(w)
        latest = real[-1] if real else None
        lag_min = None
        if latest:
            try:
                bts = dt.datetime.strptime(latest[:15], "%Y%m%d_%H%M%S").timestamp()
                lag_min = round((nw - bts) / 60.0, 1)
            except Exception:
                pass
        audit.append({
            "work": w.name,
            "live_files": sum(1 for q in w.rglob("*") if q.is_file()),
            "live_newest": dt.datetime.fromtimestamp(nw).strftime("%Y-%m-%d %H:%M:%S") if nw else None,
            "snapshot_batches": len(real), "daily_batches": len(dn),
            "latest_batch": latest,
            "latest_batch_files": len([q for q in (INST_SNAP / w.name / latest).rglob("*") if q.is_file()]) if latest else 0,
            "lag_minutes_live_vs_batch": lag_min,
        })
    EV["phases"]["P12_snapshot_audit"] = {
        "works_total": len(audit),
        "works_without_batch": [a["work"] for a in audit if a["snapshot_batches"] == 0],
        "works_without_daily": [a["work"] for a in audit if a["daily_batches"] == 0],
        "detail": audit,
    }

    (EXP / "evidence.json").write_text(json.dumps(EV, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("OK ->", EXP / "evidence.json")


if __name__ == "__main__":
    main()