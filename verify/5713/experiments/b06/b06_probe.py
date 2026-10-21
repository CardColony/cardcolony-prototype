# -*- coding: utf-8 -*-
"""B-06「快照四病」真删真恢复实测（数据域·5713下一批修复 card_015）

原则：
- 只调**仓库已发布**的实现（src/storage/data_snapshot.py、restore_from_snapshot.py），
  一律 monkeypatch 模块级 BASE_DIR / SNAPSHOT_ROOT / _FINGERPRINT_FILE 到沙箱，
  生产快照根（D:\\AI_station\\推演agent_data_snapshots）与 5700 活数据零触碰。
- 每条结论都带真删/真改 -> 恢复的实测值；做不到的如实记 unable。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

WT = Path(r"D:\AI_station\推演agent\.worktrees\5713")
OUT = Path(r"D:\AI_station\推演agent\.worktrees\5713_sandbox\experiments\b06")
BASE = OUT / "inst5713"                     # 沙箱实例根（名字决定 SNAPSHOT_ROOT 派生结果）
SNAP = BASE.parent / f"{BASE.name}_data_snapshots"
PYEXE = r"D:\AI_station\推演agent\.venv\Scripts\python.exe"

EVID: dict = {"meta": {}, "stages": {}}


def rec(stage: str, **kw):
    EVID["stages"].setdefault(stage, {}).update(kw)
    try:
        print(f"[{stage}] " + json.dumps(kw, ensure_ascii=False)[:600])
    except Exception:
        pass


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_tree(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/")] = {
                "size": p.stat().st_size,
                "sha": sha(p) if p.suffix != ".db" else None,
            }
    return out


def db_facts(p: Path) -> dict:
    con = sqlite3.connect(str(p))
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {}
        for t in tables:
            counts[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        return {"integrity": integrity, "tables": tables, "rows": counts}
    finally:
        con.close()


def make_fixture():
    """沙箱实例：2 个 work，含 cards.db／canvas.json／work.json／events／memory／sessions。"""
    if BASE.exists():
        shutil.rmtree(BASE, ignore_errors=True)
    if SNAP.exists():
        shutil.rmtree(SNAP, ignore_errors=True)
    for wid in ("work_b06", "work_b06_other"):
        w = BASE / "works" / wid
        (w / "events").mkdir(parents=True)
        (w / "memory").mkdir(parents=True)
        (w / "sessions").mkdir(parents=True)
        con = sqlite3.connect(str(w / "cards.db"))
        con.executescript(
            "CREATE TABLE cards(id TEXT PRIMARY KEY, title TEXT, body TEXT);"
            "CREATE TABLE shadow(owner TEXT, body TEXT);"
            "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        for i in range(1, 6):
            con.execute("INSERT INTO cards VALUES(?,?,?)",
                        (f"card_{i:03d}", f"标题{i}", "正文" * 50))
            con.execute("INSERT INTO shadow VALUES(?,?)", (f"card_{i:03d}", "影子" * 30))
        con.execute("INSERT INTO meta VALUES('seq','5')")
        con.commit()
        con.close()
        (w / "canvas.json").write_text(json.dumps(
            {"work_id": wid, "cards": [{"id": f"card_{i:03d}"} for i in range(1, 6)]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (w / "work.json").write_text(json.dumps({"work_id": wid, "title": "沙箱"}, ensure_ascii=False),
                                     encoding="utf-8")
        for i in range(1, 4):
            (w / "events" / f"card_{i:03d}.jsonl").write_text(
                "".join(json.dumps({"seq": k, "type": "msg", "text": "x" * 40}, ensure_ascii=False) + "\n"
                        for k in range(1, 21)), encoding="utf-8")
            (w / "memory" / f"card_{i:03d}.json").write_text(
                json.dumps({"card": f"card_{i:03d}", "entries": ["结论"] * 5}, ensure_ascii=False),
                encoding="utf-8")
            (w / "sessions" / f"session_{i:03d}.md").write_text(
                f"# session {i}\n" + ("对话记录一行。\n" * 200), encoding="utf-8")


def load_shipped_cli(base: Path = BASE):
    """加载仓库已发布的 restore_from_snapshot.py，只把三处模块级常量指到沙箱。"""
    spec = importlib.util.spec_from_file_location("shipped_restore", str(WT / "restore_from_snapshot.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.BASE_DIR = base
    m.SNAPSHOT_ROOT = SNAP
    m.BACKUP_ROOT = OUT / "backups"
    return m


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    OUT.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(WT))
    from src.storage import data_snapshot as ds

    # ---- 只改模块级路径常量，不动任何源码 ----
    ds.BASE_DIR = BASE
    ds.SNAPSHOT_ROOT = SNAP
    ds._FINGERPRINT_FILE = BASE / ".snapshot_fingerprints.json"
    EVID["meta"] = {
        "code_head": subprocess.run(["git", "-C", str(WT), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True).stdout.strip(),
        "sandbox_base": str(BASE), "sandbox_snapshot_root": str(SNAP),
        "prod_snapshot_root_touched": False,
        "library_snapshot_root_rule": "BASE_DIR.parent / f'{BASE_DIR.name}_data_snapshots'",
        "keep_batches": ds.KEEP_BATCHES, "protected_dirs": list(ds.PROTECTED_DIRS),
    }
    make_fixture()
    rec("fixture", source=snapshot_tree(BASE / "works" / "work_b06"),
        db=db_facts(BASE / "works" / "work_b06" / "cards.db"))

    # ============ T1 能存：真拍一张 ============
    src_before = snapshot_tree(BASE / "works" / "work_b06")
    r1 = ds.snapshot_sync("T1 baseline", "work_b06")
    stamp = Path(r1["dir"]).name if r1.get("dir") else ""
    batch = SNAP / "work_b06" / stamp
    btree = snapshot_tree(batch) if batch.exists() else {}
    rec("T1_can_snapshot", result=r1, stamp=stamp,
        actual_batch_dir=str(batch), batch_dir_exists=batch.exists(),
        batch_files=len(btree),
        non_db_sha_match=all(
            btree[k]["sha"] == v["sha"] for k, v in src_before.items()
            if not k.endswith(".db") and k in btree),
        db_in_batch=db_facts(batch / "works" / "work_b06" / "cards.db") if (batch / "works" / "work_b06" / "cards.db").exists() else None,
        batch_total_bytes=sum(v["size"] for v in btree.values()),
        source_total_bytes=sum(v["size"] for v in src_before.values()))

    # ============ T2 病2：返回路径不一致 ============
    all_r = ds.snapshot_sync("T2 all works")     # 不给 work_id：拍全部
    rec("T2_returned_path", single_work={
        "returned_dir": r1["dir"], "returned_exists": Path(r1["dir"]).exists(),
        "actual_dir": str(batch), "actual_exists": batch.exists(),
        "same": str(Path(r1["dir"])) == str(batch)},
        all_works={"returned_dir": all_r["dir"], "returned_exists": Path(all_r["dir"]).exists(),
                   "work_ids": all_r["work_id"], "skipped": all_r["skipped"]},
        work_level_present_in_returned="work_b06" in r1["dir"])

    # ============ T3 能恢复：整目录真删 + 整批恢复 ============
    target = BASE / "works" / "work_b06"
    db_before = db_facts(target / "cards.db")
    shutil.rmtree(target)
    rec("T3a_deleted", dir_absent=not target.exists())
    cli = load_shipped_cli()
    rb = cli.restore_batch("work_b06", stamp)
    after = snapshot_tree(target)
    rec("T3b_restored_batch", restore_result=rb, files_back=len(after),
        all_files_back=set(after) == set(src_before),
        non_db_sha_match=all(after[k]["sha"] == v["sha"] for k, v in src_before.items() if not k.endswith(".db")),
        db_before=db_before, db_after=db_facts(target / "cards.db"),
        db_identical=db_before == db_facts(target / "cards.db"))

    # ============ T4 单文件恢复 ============
    vic = target / "sessions" / "session_001.md"
    vic.write_text("被损坏", encoding="utf-8")
    rf = ds.restore_from_snapshot("work_b06", stamp, "works/work_b06/sessions/session_001.md")
    rec("T4_single_file_restore", result=rf, sha_back=sha(vic),
        sha_expected=src_before["sessions/session_001.md"]["sha"],
        match=sha(vic) == src_before["sessions/session_001.md"]["sha"])

    # ============ T5 病3：指纹去重跳过显式快照 ============
    wfp = BASE / "works" / "work_fp"
    wfp.mkdir(parents=True, exist_ok=True)
    doc = wfp / "doc.md"
    A = "A" * 4096
    B = "B" * 4096          # 同尺寸
    doc.write_text(A, encoding="utf-8")
    st = doc.stat()
    ds.snapshot_sync("T5 first", "work_fp")
    n_batches_before = len([p for p in (SNAP / "work_fp").iterdir() if p.is_dir()])
    # 同尺寸改写 + 强设回同一整秒 mtime（产品里 write_text 就是原地重写）
    doc.write_text(B, encoding="utf-8")
    os.utime(doc, (st.st_atime, st.st_mtime))
    st2 = doc.stat()
    r5 = ds.snapshot_sync("T5 显式快照：改前保险", "work_fp")
    n_batches_after = len([p for p in (SNAP / "work_fp").iterdir() if p.is_dir()])
    same_size = st.st_size == st2.st_size
    same_mtime = int(st.st_mtime) == int(st2.st_mtime)
    batches_fp = sorted([p.name for p in (SNAP / "work_fp").iterdir() if p.is_dir()])
    # 真删 -> 恢复到最新批次
    latest = [b for b in batches_fp if not b.startswith("daily_")][-1]
    doc.unlink()
    rf5 = ds.restore_from_snapshot("work_fp", latest, "works/work_fp/doc.md")
    got = doc.read_text(encoding="utf-8")
    lost_everywhere = True
    for b in batches_fp:
        p = SNAP / "work_fp" / b / "works" / "work_fp" / "doc.md"
        if p.exists() and p.read_text(encoding="utf-8") == B:
            lost_everywhere = False
    rec("T5_fingerprint_skip", same_size=same_size, same_mtime=same_mtime,
        explicit_snapshot_result=r5, batches_before=n_batches_before,
        batches_after=n_batches_after, new_batch_created=n_batches_after > n_batches_before,
        restored_content_is_A=(got == A), restored_content_is_new_B=(got == B),
        new_content_in_any_batch=not lost_everywhere,
        restore_result=rf5)

    # ============ T6 病1-a：无跨文件一致时点（拷贝窗口内可写） ============
    wc = BASE / "works" / "work_conc"
    wc.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(wc / "cards.db"))
    con.execute("CREATE TABLE t(counter INTEGER)")
    con.execute("INSERT INTO t VALUES(0)")
    con.commit()
    (wc / "canvas.json").write_text(json.dumps({"counter": 0}), encoding="utf-8")
    for i in range(20):     # 填充文件拉长拷贝窗口
        (wc / f"blob_{i:02d}.bin").write_bytes(b"Z" * 200_000)
    stop = threading.Event()
    seen = {"k": 0, "errs": 0}

    def writer():
        k = 0
        while not stop.is_set():
            k += 1
            try:
                c = sqlite3.connect(str(wc / "cards.db"), timeout=5)
                c.execute("UPDATE t SET counter=?", (k,))
                c.commit()
                c.close()
                tmp = wc / "canvas.json.tmp"
                tmp.write_text(json.dumps({"counter": k, "pad": "y" * 2000}), encoding="utf-8")
                os.replace(tmp, wc / "canvas.json")
                seen["k"] = k
            except Exception:
                seen["errs"] += 1
            time.sleep(0.002)

    th = threading.Thread(target=writer, daemon=True)
    th.start()
    time.sleep(0.05)
    pairs = []
    for i in range(4):
        rr = ds.snapshot_sync(f"T6 concurrent {i}", "work_conc")
        s = Path(rr["dir"]).name
        b = SNAP / "work_conc" / s
        if not b.exists():
            pairs.append({"stamp": s, "batch": "missing", "result": rr})
            continue
        dbv = None
        try:
            c = sqlite3.connect(str(b / "works" / "work_conc" / "cards.db"))
            dbv = c.execute("SELECT counter FROM t").fetchone()[0]
            c.close()
        except Exception as e:
            dbv = f"ERR:{e}"
        try:
            cav = json.loads((b / "works" / "work_conc" / "canvas.json").read_text(encoding="utf-8"))["counter"]
        except Exception as e:
            cav = f"ERR:{e}"
        pairs.append({"stamp": s, "db": dbv, "canvas": cav,
                      "coherent_pair": (isinstance(dbv, int) and isinstance(cav, int)
                                        and (dbv == cav or dbv == cav + 1)),
                      "failed_files": rr.get("failed")})
    stop.set()
    time.sleep(0.05)
    rec("T6_no_point_in_time", writer_updates=seen["k"], writer_errors=seen["errs"],
        pairs=pairs, incoherent=[p for p in pairs if p.get("coherent_pair") is False],
        note="写入顺序固定 db 后 canvas ⇒ 可能的一致对只有 (db=k,canvas=k) 或 (db=k,canvas=k-1)")

    # ============ T7 病1-b：原地重写文件的撕裂窗口（产品同款 write_text 模式） ============
    wt_ = BASE / "works" / "work_torn"
    wt_.mkdir(parents=True, exist_ok=True)
    (wt_ / "cards.db").write_bytes(b"")
    for i in range(20):
        (wt_ / f"blob_{i:02d}.bin").write_bytes(b"Q" * 200_000)
    payload = {"pad": "p" * 1_500_000, "v": 0}
    stop2 = threading.Event()
    wstat = {"writes": 0}

    def writer2():
        v = 0
        while not stop2.is_set():
            v += 1
            payload["v"] = v
            # 与 canvas_store.py:598 同款：原地 truncate + write_text（非原子）
            (wt_ / "canvas.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            wstat["writes"] = v

    th2 = threading.Thread(target=writer2, daemon=True)
    th2.start()
    torn = []
    for i in range(6):
        rr = ds.snapshot_sync(f"T7 torn {i}", "work_torn")
        s = Path(rr["dir"]).name
        f = SNAP / "work_torn" / s / "works" / "work_torn" / "canvas.json"
        if not f.exists():
            torn.append({"stamp": s, "state": "missing-file", "failed": rr.get("failed")})
            continue
        try:
            json.loads(f.read_text(encoding="utf-8"))
            torn.append({"stamp": s, "state": "valid", "size": f.stat().st_size})
        except Exception as e:
            torn.append({"stamp": s, "state": "TORN", "size": f.stat().st_size, "err": str(e)[:80]})
    stop2.set()
    time.sleep(0.05)
    rec("T7_torn_copy", writes=wstat["writes"], attempts=len(torn),
        torn_count=sum(1 for t in torn if t.get("state") == "TORN"),
        detail=torn,
        note="canvas.json 在产品里由 canvas_store.py:598 原地 write_text 写（非原子）；memory/session 走 tmp+os.replace 原子写")

    # ---- T10 病1 的后果闭合：从撕裂批次真删真恢复，把 0 字节文件种回数据根 ----
    torn_rec = next((t for t in torn if t.get("state") == "TORN"), None)
    if torn_rec:
        victim = wt_ / "canvas.json"
        before = victim.stat().st_size if victim.exists() else None
        victim.unlink(missing_ok=True)
        rr10 = ds.restore_from_snapshot("work_torn", torn_rec["stamp"], "works/work_torn/canvas.json")
        planted = victim.stat().st_size if victim.exists() else None
        try:
            json.loads(victim.read_text(encoding="utf-8"))
            parses = True
        except Exception:
            parses = False
        rec("T10_torn_batch_restore", torn_stamp=torn_rec["stamp"],
            live_size_before_delete=before, restore_result=rr10,
            size_after_restore=planted, parses_after_restore=parses,
            note="恢复层不校验内容：撕裂批次里的 0 字节文件被原样种回数据根")
    else:
        rec("T10_torn_batch_restore", unable="本轮未抓到撕裂批次")

    # ============ T8 病1-c：全量拷贝代价（每次写都把整份数据再拷一遍） ============
    wbig = BASE / "works" / "work_big"
    (wbig / "events").mkdir(parents=True, exist_ok=True)
    for i in range(300):
        (wbig / "events" / f"c_{i:03d}.jsonl").write_bytes(b"J" * 100_000)
    for i in range(100):
        (wbig / f"blob_{i:03d}.bin").write_bytes(b"K" * 186_000)
    total_src = sum(p.stat().st_size for p in wbig.rglob("*") if p.is_file())
    times = []
    for i in range(3):
        if ds._FINGERPRINT_FILE.exists():
            fs = json.loads(ds._FINGERPRINT_FILE.read_text(encoding="utf-8"))
            fs.pop("work_big", None)
            ds._FINGERPRINT_FILE.write_text(json.dumps(fs), encoding="utf-8")
        t0 = time.perf_counter()
        r8 = ds.snapshot_sync(f"T8 cost {i}", "work_big")
        dt = time.perf_counter() - t0
        s = Path(r8["dir"]).name
        b = SNAP / "work_big" / s
        bsz = sum(p.stat().st_size for p in b.rglob("*") if p.is_file()) if b.exists() else 0
        times.append({"run": i, "seconds": round(dt, 3), "files": r8["files"],
                      "batch_bytes": bsz, "source_bytes": total_src})
    rec("T8_full_copy_cost", source_files=len([p for p in wbig.rglob('*') if p.is_file()]),
        source_bytes=total_src, runs=times)

    # ============ T9 病4：restore 落点（真跑仓库自带 CLI） ============
    prod_lib_root = WT.parent / "推演agent_data_snapshots"      # CLI 里硬编码名
    lib_rule_root = WT.parent / f"{WT.name}_data_snapshots"     # data_snapshot.py 的派生规则
    listrun = subprocess.run([PYEXE, "restore_from_snapshot.py", "--list"], cwd=str(WT),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    stamp9 = "20261020_120000_000000"
    fake = prod_lib_root / "work_b06probe" / stamp9 / "works" / "work_b06probe"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "session_001.md").write_text("RESTORED-FROM-HARDCODED-ROOT", encoding="utf-8")
    live_target = Path(r"D:\AI_station\推演agent\works\work_b06probe\session_001.md")
    rundir_dst = WT / "works" / "work_b06probe" / "session_001.md"
    run9 = subprocess.run([PYEXE, "restore_from_snapshot.py", "--work", "work_b06probe",
                           "--stamp", stamp9, "--file", "works/work_b06probe/session_001.md"],
                          cwd=str(WT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    rec("T9_restore_destination",
        cli_list_stdout=listrun.stdout.strip().splitlines()[:4],
        cli_computed_root=str(prod_lib_root), cli_root_exists_before=(prod_lib_root.exists()),
        library_rule_root=str(lib_rule_root), same_root=(prod_lib_root == lib_rule_root),
        restore_stdout=run9.stdout.strip(), restore_rc=run9.returncode,
        wrote_parallel_tree=str(rundir_dst), parallel_tree_exists=rundir_dst.exists(),
        live_data_untouched=not live_target.exists(),
        note="CLI 用 BASE_DIR=脚本所在目录 + 硬编码 '推演agent_data_snapshots'；库用 BASE_DIR 名派生")
    # 清理探针痕迹
    for p in (prod_lib_root, WT / "works" / "work_b06probe"):
        shutil.rmtree(p, ignore_errors=True)
    # worktrees 下的 works 若因此新建成空目录也清掉
    try:
        if (WT / "works").exists() and not any((WT / "works").iterdir()):
            (WT / "works").rmdir()
    except Exception:
        pass
    rec("T9_cleanup", hardcoded_root_gone=not prod_lib_root.exists(),
        parallel_gone=not (WT / "works" / "work_b06probe").exists(),
        prod_probe_gone=not live_target.exists())

    # ---- T9b 跨实例口径（纯派生，不写盘）：CLI 硬编码名 vs 库按实例名派生 ----
    sib = sorted(p.name for p in Path(r"D:\AI_station").glob("*_data_snapshots"))
    rec("T9b_cross_instance",
        sibling_snapshot_roots=sib,
        note="库按 BASE_DIR.name 派生 SNAPSHOT_ROOT；CLI 恒指 '推演agent_data_snapshots' ⇒ "
             "任何非正式实例（如 推演agent_test）跑 CLI 都会去读正式实例的快照库，"
             "并把内容恢复进自己的 BASE_DIR")

    EVID["meta"]["sandbox_snapshot_bytes"] = sum(p.stat().st_size for p in SNAP.rglob("*") if p.is_file())
    (OUT / "evidence_b06.json").write_text(json.dumps(EVID, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n证据已写入", OUT / "evidence_b06.json")


if __name__ == "__main__":
    main()