# -*- coding: utf-8 -*-
"""**快照产出范围**回归网（[2026-10-30 · 治「D 盘一周吃近 1T」]）。

## 线上症状（实测，不是推测）

- ``D:\\AI_station\\推演agent_data_snapshots`` = **705.61GB** / **4499 个批次**
- 其中 ``work_027`` 一个 work = 969 批 / **694.38GB**（占全库 98%）
- 单批 **8.7GB**（225 个文件）

## 四环因果链（每一环都实测过）

1. ``works/work_027/nul`` —— 0 字节、名叫 ``nul``（**Windows 保留设备名**），
   2026-10-29 20:55:50 由 PowerShell 的 ``> nul`` 重定向造成（PowerShell 不把
   ``nul`` 当设备名 ⇒ 真的造了个文件）。
2. 快照必拷它 ⇒ ``SameFileError`` ⇒ **每一批都「不完整」**（日志 553 条
   「快照部分文件失败」，work_027 占 64 条，样本 ``batch:works\\work_027\\nul``）。
3. 原实现「**整批完整**才调 ``_prune_batches``」⇒ 保留闸（200 份 / 2GB 两条）
   **从未执行** ⇒ 快照库无界增长。
4. 批次里还塞着**与卡片状态无关**的东西：``spike/`` 压测产物 **7.07GB**
   ＋ ``cards.db.bak_backfill`` 707MB ⇒ 每批 8.7GB。

⇒ 病根不是「只产出不清理」，而是**产出机制太粗暴** ＋ **保留闸被完整性绑架**。

## 本网钉住的不变量

| 编号 | 不变量 |
|---|---|
| S1 | 压测产物目录（``spike/``）不进快照（过程批次**与**日备都不进） |
| S2 | 备份副本（``*.bak*``）不进快照 |
| S3 | Windows 保留设备名（``nul``/``con``/``com1``…）不进快照，且不会把整批打成失败 |
| S4 | **保留闸无条件生效**：这批不完整也照修剪（这就是病根） |
| S5 | 份数闸默认 **10 份**（用户口径：「保险一点，最近 10 条也够」） |
| S6 | 日备**仍含留痕**（events/sessions）——收窄不许误砍恢复点 |
| S7 | manifest 记下本批字节数（全库体积闸的快路径） |
| S8 | 全库体积闸：超预算 ⇒ 跨 work 删最旧的非日备批次 |

铁律一：只用 conftest 的沙箱，不碰真实 works / 真实快照库。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.storage import data_snapshot

WORK = "work_001"


# ------------------------------------------------------------------ 脚手架

def _real_db(path: Path) -> None:
    """造一个**真的** SQLite 库（快照对 .db 走 VACUUM INTO，假文件会失败）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS t(x TEXT)")
        con.execute("INSERT INTO t VALUES ('x')")
        con.commit()
    finally:
        con.close()


def _stage(sandbox, extra: dict[str, str] | None = None) -> Path:
    """在沙箱 works/work_001/ 下铺状态文件 ＋ 产物/备份/野文件。"""
    w = sandbox.works / WORK
    w.mkdir(parents=True, exist_ok=True)
    _real_db(w / "cards.db")
    (w / "events").mkdir(exist_ok=True)
    (w / "sessions").mkdir(exist_ok=True)
    (w / "events" / "card_001.jsonl").write_text("事件\n", encoding="utf-8")
    (w / "sessions" / "session_001.md").write_text("会话\n", encoding="utf-8")
    # 产物：压测目录（与卡片状态无关）
    spike = w / "spike" / "s0_3" / "data"
    spike.mkdir(parents=True, exist_ok=True)
    (spike / "events_tier4.log").write_text("压测产物\n" * 100, encoding="utf-8")
    # 备份副本：上一份的库
    (w / "cards.db.bak_backfill").write_text("旧库\n", encoding="utf-8")
    for rel, content in (extra or {}).items():
        p = w / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return w


def _batch_files(root: Path) -> set[str]:
    """一个批次（或日备）里的文件（相对 BASE_DIR 的 posix 路径）。"""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _batches(root: Path) -> list[Path]:
    wd = root / WORK
    if not wd.exists():
        return []
    return sorted([p for p in wd.iterdir()
                   if p.is_dir() and not p.name.startswith("daily_")],
                  key=lambda p: p.name)


def _daily(root: Path) -> Path | None:
    wd = root / WORK
    if not wd.exists():
        return None
    days = [p for p in wd.iterdir() if p.is_dir() and p.name.startswith("daily_")]
    return days[0] if days else None


# ------------------------------------------------------------------ S1 / S2

def test_s1_spike_artifacts_are_not_snapshotted(sandbox):
    """S1：压测产物目录（spike/）不进快照——它与卡片状态无关。"""
    _stage(sandbox)
    data_snapshot.snapshot_sync("scope-s1", work_id=WORK)

    batches = _batches(data_snapshot.SNAPSHOT_ROOT)
    assert len(batches) == 1, f"应产生一份过程批次：{batches}"
    got = _batch_files(batches[0])
    assert "works/work_001/cards.db" in got, f"真状态必须在批次里：{got}"
    assert not any("spike" in g for g in got), \
        f"压测产物不该进快照（实测它占每批 7.07GB）：{got}"


def test_s2_backup_copies_are_not_snapshotted(sandbox):
    """S2：备份副本（*.bak*）不进快照。"""
    _stage(sandbox)
    data_snapshot.snapshot_sync("scope-s2", work_id=WORK)

    got = _batch_files(_batches(data_snapshot.SNAPSHOT_ROOT)[0])
    assert "works/work_001/cards.db.bak_backfill" not in got, \
        f"上一份的库不该进快照（实测每批多 707MB）：{got}"


# ------------------------------------------------------------------ S3

def test_s3_reserved_device_names_are_recognised():
    """S3：Windows 保留设备名必须被识别（nul/con/com1…）。"""
    for name in ("nul", "NUL", "nul.txt", "con", "com1", "lpt9", "PRN"):
        assert data_snapshot._is_reserved_name(name), name
    for name in ("cards.db", "events_tier4.log", "canvas.json", "null"):
        assert not data_snapshot._is_reserved_name(name), name


def test_s3_reserved_name_copy_is_skipped_not_failed(tmp_path):
    """S3：拷保留设备名**静默跳过**，不抛异常（抛了就把整批打成「不完整」）。"""
    dst = tmp_path / "batch" / "works" / WORK / "nul"
    data_snapshot._copy_protected_file(Path("nul"), dst)   # 不该抛
    assert not dst.exists(), "保留设备名不该被拷进批次"


def test_s3_reserved_name_is_not_part_of_state_files(sandbox, monkeypatch):
    """S3：枚举层就把保留设备名挡掉（不进指纹 ⇒ 不触发新批次）。"""
    w = _stage(sandbox)
    names = {p.name for p in data_snapshot._iter_state_files(WORK)}
    assert "cards.db" in names, names
    assert not any(data_snapshot._is_reserved_name(n) for n in names), names


# ------------------------------------------------------------------ S4

def test_s4_prune_runs_even_when_batch_is_incomplete(sandbox, monkeypatch):
    """S4：**保留闸无条件生效**——这批缺文件也照修剪。

    这是本次事故的核心：原来「整批完整才修剪」⇒ 一个 0 字节的 ``nul``
    让**每批**都不完整 ⇒ 保留闸永久失效 ⇒ 快照库涨到 705.61GB。
    """
    root = data_snapshot.SNAPSHOT_ROOT / WORK
    root.mkdir(parents=True, exist_ok=True)
    for name in ("20260101_000000_000001", "20260102_000000_000002",
                 "20260103_000000_000003"):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "works" / WORK).mkdir(parents=True, exist_ok=True)
        (d / "works" / WORK / "cards.db").write_text("旧", encoding="utf-8")
    monkeypatch.setattr(data_snapshot, "KEEP_BATCHES", 2)

    # 让本轮这批**不完整**：cards.db 拷贝失败（模拟撞锁）
    real_copy = data_snapshot._copy_protected_file

    def flaky(p: Path, dst: Path) -> None:
        if p.name == "cards.db":
            raise OSError("boom: 模拟这批没拷全")
        return real_copy(p, dst)

    monkeypatch.setattr(data_snapshot, "_copy_protected_file", flaky)
    _stage(sandbox)
    r = data_snapshot.snapshot_sync("scope-s4", work_id=WORK)
    assert r["failed"], r

    left = {p.name for p in root.iterdir() if p.is_dir()}
    assert "20260101_000000_000001" not in left, \
        f"保留闸必须无条件生效（最旧的应被删）：{sorted(left)}"
    assert "20260103_000000_000003" in left, f"最新的必须留着：{sorted(left)}"


# ------------------------------------------------------------------ S5

def test_s5_keep_batches_default_is_ten():
    """S5：份数闸默认 10 份（用户口径：「保险一点，最近 10 条也够」）。

    原来 200 份 × 单份 8.7GB ＝ 理论上限 1.7TB/work ⇒ 无界。
    """
    assert data_snapshot.KEEP_BATCHES == 10, data_snapshot.KEEP_BATCHES
    assert data_snapshot.KEEP_DAILY_DAYS == 3, data_snapshot.KEEP_DAILY_DAYS
    assert data_snapshot.KEEP_TOTAL_BYTES > 0, "全库闸不许默认关着"


def test_s5_count_gate_trims_to_ten(sandbox, monkeypatch):
    """S5：份数闸真的把旧批次削到 10 份。"""
    root = data_snapshot.SNAPSHOT_ROOT / WORK
    root.mkdir(parents=True, exist_ok=True)
    for i in range(14):
        d = root / f"2026010{i + 1}_000000_00000{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "works" / WORK).mkdir(parents=True, exist_ok=True)
        (d / "works" / WORK / "cards.db").write_text("旧", encoding="utf-8")
    monkeypatch.setattr(data_snapshot, "KEEP_BATCH_BYTES", 0)   # 只测份数闸
    removed = data_snapshot._prune_batches(WORK, force=True)
    assert removed == 4, removed
    assert len(_batches(data_snapshot.SNAPSHOT_ROOT)) == 10


# ------------------------------------------------------------------ S6

def test_s6_daily_backup_still_holds_log_files(sandbox):
    """S6：日备**仍含留痕**——收窄产出范围不许误砍恢复点。"""
    _stage(sandbox)
    data_snapshot.snapshot_sync("scope-s6", work_id=WORK)

    daily = _daily(data_snapshot.SNAPSHOT_ROOT)
    assert daily is not None, "应产生日备"
    got = _batch_files(daily)
    assert "works/work_001/events/card_001.jsonl" in got, f"日备必须含留痕：{got}"
    assert "works/work_001/sessions/session_001.md" in got, f"日备必须含留痕：{got}"
    assert "works/work_001/cards.db" in got, f"日备必须含状态：{got}"
    assert not any("spike" in g for g in got), \
        f"日备也不该含压测产物（实测 9.94GB/份）：{got}"


# ------------------------------------------------------------------ S7

def test_s7_manifest_records_batch_bytes(sandbox):
    """S7：manifest 记下本批字节数（全库体积闸的快路径 ⇒ 不必遍历文件）。"""
    _stage(sandbox)
    data_snapshot.snapshot_sync("scope-s7", work_id=WORK)

    batch = _batches(data_snapshot.SNAPSHOT_ROOT)[0]
    mf = json.loads(batch.with_name(batch.name + ".manifest.json")
                    .read_text(encoding="utf-8"))
    assert isinstance(mf.get("bytes"), int), mf
    assert mf["bytes"] > 0, mf
    # 对账：记的字节数必须等于批次里文件的实际字节数（不是自己跟自己比）
    real = sum(p.stat().st_size for p in batch.rglob("*") if p.is_file())
    assert mf["bytes"] == real, (mf["bytes"], real)


# ------------------------------------------------------------------ S8

def test_s8_global_cap_prunes_oldest_across_works(sandbox, monkeypatch):
    """S8：全库体积闸超预算 ⇒ 跨 work 删最旧的非日备批次。"""
    root = data_snapshot.SNAPSHOT_ROOT
    for wid, stamp, size in (("work_a", "20260101_000000_000001", 4 * 1024 * 1024),
                             ("work_b", "20260102_000000_000002", 4 * 1024 * 1024),
                             ("work_c", "20260103_000000_000003", 4 * 1024 * 1024)):
        d = root / wid / stamp / "works" / wid
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "cards.db", "wb") as fh:
            fh.truncate(size)
    monkeypatch.setattr(data_snapshot, "KEEP_TOTAL_BYTES", 5 * 1024 * 1024)

    removed = data_snapshot._prune_total(force=True)
    assert removed >= 1, removed
    left = sorted(p.name for p in (root / "work_a").iterdir() if p.is_dir())
    assert "20260101_000000_000001" not in left, f"最旧的应先被删：{left}"
    assert (root / "work_c" / "20260103_000000_000003").is_dir(), "最新的必须留着"


# ------------------------------------------------------------------ S9

def test_s9_readonly_files_do_not_block_prune(sandbox, monkeypatch):
    """S9：批次里的**只读文件**不许挡住删除。

    实测：旧批次里含 ``.git`` 对象（git 对象是只读 444）⇒ Windows 上
    ``unlink`` 直接 ``WinError 5 拒绝访问`` ⇒ 原来的
    ``shutil.rmtree(..., ignore_errors=True)`` **把错误吞掉** ⇒ 目录删不掉
    ⇒ 份数闸「删了等于没删」（实测 work_027 拍完一份仍是 11 份）。

    ⇒ 修剪必须能删掉含只读文件的批次（旧批次有 .git 副本，新批次不再拷 .git）。
    """
    import os
    import stat

    root = data_snapshot.SNAPSHOT_ROOT / WORK
    root.mkdir(parents=True, exist_ok=True)
    for name in ("20260101_000000_000001", "20260102_000000_000002"):
        d = root / name / "works" / WORK / "sub" / ".git" / "objects"
        d.mkdir(parents=True, exist_ok=True)
        obj = d / "e69b39f95ed45d2b669cac66b14fdeb8390c07"
        obj.write_text("blob", encoding="utf-8")
        os.chmod(obj, stat.S_IREAD)          # 只读（= git 对象那种）
    monkeypatch.setattr(data_snapshot, "KEEP_BATCHES", 1)

    removed = data_snapshot._prune_batches(WORK, force=True)
    assert removed == 1, removed
    left = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert left == ["20260102_000000_000002"], f"含只读文件的旧批次必须删得掉：{left}"


def test_s9_prune_reports_failure_instead_of_faking_success(sandbox, monkeypatch):
    """S9'：真删不掉时必须**如实返回 False**（别让调用方以为删了）。"""
    root = data_snapshot.SNAPSHOT_ROOT / WORK
    d = root / "20260103_000000_000003"
    d.mkdir(parents=True, exist_ok=True)
    (d / "keep.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(data_snapshot, "KEEP_BATCHES", 0)
    monkeypatch.setattr(data_snapshot, "_safe_rmtree", lambda p: False)
    removed = data_snapshot._prune_batches(WORK, force=True)
    assert removed == 0, "删不掉就不许计数为「已删」"
    assert d.exists(), "删不掉时目录应还在（没丢东西）"


# ================================================================ S10 备份粒度

def _rich_db(path: Path) -> None:
    """造一张「有状态、有历史、有垃圾」的库（照线上 cards.db 的形状）。"""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE cards (card_id TEXT PRIMARY KEY, body TEXT)")
        con.execute("CREATE TABLE session_blocks (card_id TEXT, seq INT, content TEXT)")
        con.execute("CREATE TABLE work_events (seq INT, data TEXT)")
        con.execute("CREATE TABLE trash (card_id TEXT, payload TEXT)")
        con.execute("INSERT INTO cards VALUES ('card_001', '第一版')")
        con.execute("INSERT INTO session_blocks VALUES ('card_001', 1, '历史块')")
        con.execute("INSERT INTO work_events VALUES (1, '事件')")
        con.execute("INSERT INTO trash VALUES ('card_009', '删掉的东西')")
        con.commit()
    finally:
        con.close()


def _tables_of(db: Path) -> set[str]:
    import sqlite3

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        con.close()


def test_s10_process_batch_excludes_append_only_history(sandbox):
    """S10：过程批次**只含会变的表**——只追加的历史与垃圾不进。

    〔2026-10-30 · 治「每改一次就全项目备份」〕线上 cards.db 777MB 里：
    session_blocks 311MB ＋ work_events 131MB ＋ trash 26MB 都是**只追加/垃圾**
    ⇒ 每写一次就整库拷 855MB（实测 10.5s），而真正会变的表只有 12MB。
    """
    w = sandbox.works / WORK
    w.mkdir(parents=True, exist_ok=True)
    _rich_db(w / "cards.db")

    data_snapshot.snapshot_sync("scope-s10", work_id=WORK)
    batch = _batches(data_snapshot.SNAPSHOT_ROOT)[0]
    got = _tables_of(batch / "works" / WORK / "cards.db")
    assert "cards" in got, f"状态表必须在批次里：{got}"
    for excluded in ("session_blocks", "work_events", "trash"):
        assert excluded not in got, f"只追加/垃圾表不该进过程批次：{got}"


def test_s10_daily_backup_still_keeps_every_table(sandbox):
    """S10'：**日备仍是整库**（完整恢复点）——历史表在里面。"""
    w = sandbox.works / WORK
    w.mkdir(parents=True, exist_ok=True)
    _rich_db(w / "cards.db")

    data_snapshot.snapshot_sync("scope-s10b", work_id=WORK)
    daily = _daily(data_snapshot.SNAPSHOT_ROOT)
    assert daily is not None, "应产生日备"
    got = _tables_of(daily / "works" / WORK / "cards.db")
    for t in ("cards", "session_blocks", "work_events", "trash"):
        assert t in got, f"日备必须是整库（{t} 不在里面）：{got}"


def test_s10_restore_rolls_back_state_and_keeps_history(sandbox):
    """S10''：恢复＝**状态回滚、历史不动**（关键不变量）。

    过程批次的库只含状态表 ⇒ 若恢复走整文件替换，live 库的会话原文块/
    事件流会被一起抹掉 ⇒ 必须走逐表合并。
    """
    import sqlite3

    # [库搬出施工目录 2026-11-07] live 库的真实位置是 works_data/{wid}/——
    # 恢复落点也是它（restore_from_snapshot 把 *.db 一律解到当前库位）。
    w = sandbox.base / "works_data" / WORK
    w.mkdir(parents=True, exist_ok=True)
    _rich_db(w / "cards.db")

    data_snapshot.snapshot_sync("scope-s10c", work_id=WORK)
    batch = _batches(data_snapshot.SNAPSHOT_ROOT)[0]
    stamp = batch.name

    # 快照之后：改了状态、又追加了一条历史
    con = sqlite3.connect(str(w / "cards.db"))
    try:
        con.execute("UPDATE cards SET body='第二版' WHERE card_id='card_001'")
        con.execute("INSERT INTO session_blocks VALUES ('card_001', 2, '快照之后的新块')")
        con.commit()
    finally:
        con.close()

    res = data_snapshot.restore_from_snapshot(WORK, stamp, f"works/{WORK}/cards.db")
    assert res["ok"] is True, res

    con = sqlite3.connect(str(w / "cards.db"))
    try:
        body = con.execute("SELECT body FROM cards WHERE card_id='card_001'").fetchone()[0]
        blocks = con.execute(
            "SELECT count(*) FROM session_blocks WHERE card_id='card_001'").fetchone()[0]
    finally:
        con.close()
    assert body == "第一版", f"状态必须回到快照态：{body}"
    assert blocks == 2, f"历史表**不许**被回滚（它只追加）：{blocks}"
