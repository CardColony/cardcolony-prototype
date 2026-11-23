# -*- coding: utf-8 -*-
"""db_watchdog 隔离治理测试（2026-11-22 · 磁盘治理第一批）。

判据来源：137GiB/28 份隔离副本事故——抽检假损坏反复 _preserve。
"""
import importlib
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def wd(tmp_path, monkeypatch):
    monkeypatch.setenv("ECO_DB_QUAR_ROOT", str(tmp_path / "quar"))
    import src.storage.db_watchdog as m
    importlib.reload(m)
    work = tmp_path / "w1"
    work.mkdir()
    db = work / "cards.db"
    con = sqlite3.connect(str(db))
    con.execute("create table t(x)")
    con.commit()
    con.close()
    return m, db, tmp_path / "quar"


def _dirs(q: Path):
    return [d for d in q.iterdir() if d.is_dir()] if q.is_dir() else []


def test_same_incident_merges_no_extra_copy(wd):
    """同库同判据连发 N 次 ⇒ 只 1 份物理副本，台账 count 累加。"""
    m, db, q = wd
    for i in range(10):
        m._preserve(db, "INTEGRITY", 100, f"fake #{i}", consistent=True)
    assert len(_dirs(q)) == 1
    inc = json.loads((q / "_incidents.json").read_text(encoding="utf-8"))
    assert inc[str(db)]["count"] == 10


def test_resolve_then_recur_new_case(wd):
    """恢复（resolved）后再出故障 ⇒ 立新案、新目录。"""
    m, db, q = wd
    m._preserve(db, "INTEGRITY", 100, "a", consistent=True)
    m._resolve_incident(db, ("INTEGRITY",))
    m._preserve(db, "INTEGRITY", 100, "b", consistent=True)
    assert len(_dirs(q)) == 2


def test_consistent_copy_is_openable(wd):
    """一致备份副本能开库且 quick_check 干净。"""
    m, db, q = wd
    m._preserve(db, "INTEGRITY", 100, "x", consistent=True)
    qdb = _dirs(q)[0] / "cards.db"
    con = sqlite3.connect(str(qdb))
    assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    con.close()


def test_budget_skips_nonforensic(wd, monkeypatch):
    """超预算：INTEGRITY 跳拷仍记账；ZEROED 应急豁免照拷。"""
    m, db, q = wd
    monkeypatch.setattr(m, "_QUAR_BUDGET", 0.0)
    m._preserve(db, "INTEGRITY", 100, "big", consistent=True)
    assert len(_dirs(q)) == 0
    skipped = m._load_incidents()[str(db)]
    assert skipped["result"] == "skipped"
    assert skipped["need_bytes"] == db.stat().st_size
    m._preserve(db, "ZEROED", 5000, "forensic")
    assert len(_dirs(q)) == 1


def test_budget_skipped_case_can_capture_after_budget_recovers(wd, monkeypatch):
    m, db, q = wd
    monkeypatch.setattr(m, "_QUAR_BUDGET", 0)
    m._preserve(db, "INTEGRITY", 100, "first", consistent=True)
    m._preserve(db, "INTEGRITY", 100, "still blocked", consistent=True)
    monkeypatch.setattr(m, "_QUAR_BUDGET", 1_000_000)
    m._preserve(db, "INTEGRITY", 100, "budget restored", consistent=True)
    assert len(_dirs(q)) == 1
    incident = m._load_incidents()[str(db)]
    assert incident["count"] == 3
    assert incident["result"] == "complete"


def test_probe_uses_backup_api(wd):
    """_probe_via_backup：活库被占着写也能拿一致副本（不再拼三件套）。"""
    m, db, q = wd
    writer = sqlite3.connect(str(db))
    writer.execute("insert into t values (1)")
    # 不 commit——活库 -wal 里有未落盘写：直拷三件套是脏的，backup() 不受影响
    assert m._probe_via_backup(db) == "ok"
    writer.rollback()
    writer.close()


# ---- 2026-11-22 返修四项 ----------------------------------------------------

def _rig_probe(m, snap_db, live_db, results, tmp_path):
    """probe 依赖全部钉死：只测记账逻辑。"""
    m._latest_daily_db_copies = lambda: [("w1", snap_db)] if snap_db else []
    m._find_live_db = lambda wid, name: live_db
    m._scan_roots = lambda: [live_db.parent]
    m._sweep_probe_temps = lambda *a, **k: 0
    m._check_db_file = lambda p: results.get(str(p), "ok")
    m._probe_via_backup = lambda p: results.get(str(p), "ok")


def test_snap_and_live_incidents_are_separate(wd, tmp_path):
    """返修：坏日备案挂 snap: 键——活库过检不替它销案，复查计同案。"""
    m, db, q = wd
    snap = tmp_path / "c.db"
    snap.write_bytes(b"x")
    res = {str(snap): "page bad"}
    _rig_probe(m, snap, db, res, tmp_path)
    m._integrity_probe()
    inc = json.loads((q / "_incidents.json").read_text(encoding="utf-8"))
    skey = f"snap:{snap}"
    assert inc[skey]["reason"] == "SNAP_INTEGRITY"
    assert not inc[skey]["resolved"]
    assert str(db) not in inc          # 活库没落案
    m._integrity_probe()               # 复查：同案计数，不立新案
    inc = json.loads((q / "_incidents.json").read_text(encoding="utf-8"))
    assert inc[skey]["count"] == 2
    res[str(snap)] = "ok"              # 那份副本自己被替换修好才结案
    m._integrity_probe()
    inc = json.loads((q / "_incidents.json").read_text(encoding="utf-8"))
    assert inc[skey]["resolved"]


def test_snap_ok_does_not_resolve_live_incident(wd, tmp_path):
    """返修：活库有未结 INTEGRITY 案，快照过检**不**替它销案。"""
    m, db, q = wd
    m._preserve(db, "INTEGRITY", 1, "live-corrupt", consistent=True)
    snap = tmp_path / "c.db"
    snap.write_bytes(b"x")
    _rig_probe(m, snap, db, {}, tmp_path)   # 全过检
    m._integrity_probe()
    inc = json.loads((q / "_incidents.json").read_text(encoding="utf-8"))
    assert not inc[str(db)]["resolved"]     # 活库案还在


def test_partial_marked_when_main_copy_fails(wd, monkeypatch):
    """返修：只拷出 WAL、主库拷贝失败 ⇒ result=partial + failed=.db。"""
    import shutil as _sh
    m, db, q = wd
    (db.parent / "cards.db-wal").write_bytes(b"wal-bytes")
    real = _sh.copy2

    def flaky(src, dst):
        if str(src).endswith(".db"):
            raise OSError("denied")
        return real(src, dst)

    monkeypatch.setattr(m.shutil, "copy2", flaky)
    m._preserve(db, "ZEROED", 9, "forensic")
    note = (_dirs(q)[0] / "_NOTE.txt").read_text(encoding="utf-8")
    assert "result=partial" in note
    assert "failed=.db" in note


def test_budget_ignores_legacy_dirs(wd, monkeypatch):
    """返修：台账外的旧隔离存量不吃新取证预算（137GiB 历史副本豁免）。"""
    m, db, q = wd
    legacy = q / "20250101_legacy_big"
    legacy.mkdir(parents=True)
    (legacy / "x.db").write_bytes(b"0" * 10_000_000)
    monkeypatch.setattr(m, "_QUAR_BUDGET", 100_000.0)   # 100KB：存量早爆
    m._preserve(db, "INTEGRITY", 1, "small", consistent=True)
    names = {d.name for d in _dirs(q)}
    assert "20250101_legacy_big" in names               # 旧件原样未动
    assert len(names) == 2                              # 新案仍建出来了


def test_rotator_meters_actual_size_when_rename_blocked(tmp_path, monkeypatch):
    """改名暂不可用时仍在字节预算内，解锁后下一写即轮转。"""
    from src.infra.stdout_rotate import _RotatingStream
    p = tmp_path / "x.log"
    s = _RotatingStream(p, max_bytes=16, backups=2)
    blocked = {"on": True}
    real = type(p).replace

    def deny(self, target):
        if blocked["on"]:
            raise OSError("WinError 32")
        return real(self, target)

    monkeypatch.setattr(type(p), "replace", deny)
    try:
        for _ in range(3):
            s.write("x" * 20)
        assert s._size == p.stat().st_size == 16
        blocked["on"] = False
        s.write("y")
        assert (tmp_path / "x.log.1").stat().st_size == 16
        assert p.stat().st_size == 1
    finally:
        s._fh.close()


def test_resolved_incidents_still_count_retained_managed_copies(wd, monkeypatch):
    m, db, q = wd
    m._preserve(db, "INTEGRITY", 1, "first", consistent=True)
    size = m._dir_bytes(_dirs(q)[0])
    m._resolve_incident(db, ("INTEGRITY",))
    m._preserve(db, "INTEGRITY", 1, "second", consistent=True)
    monkeypatch.setattr(m, "_QUAR_BUDGET", m._dir_bytes(q) - size / 2)
    assert m._quarantine_free_bytes() < 0


def test_probe_budget_defers_check_without_creating_copy(wd, monkeypatch):
    m, db, q = wd
    monkeypatch.setattr(m, "_QUAR_BUDGET", 1)
    assert m._probe_via_backup(db) is None
    assert _dirs(q) == []
    assert not (q / "_incidents.json").exists()
