# -*- coding: utf-8 -*-
"""**快照机制**回归网（[5715]）——治本，不是限量。

## 原来的机制为什么愚蠢

每次写操作（建卡/改卡/删卡/影子卡/建 work）都触发一次快照，而快照把
**整个会话状态全量拷一份**（cards.db ＋ events/*.jsonl ＋ sessions/*.md 全部）。
而其中 events/sessions 是**只追加**的——它们不会「变旧」，快照拷它们等于
**把同一份历史反复抄 N 遍**。

实测（2026-10-23 盘查）：一个 22 小时的会话 ⇒ **200 份 × 240MB = 48GB**；
其中 cards.db 只有 1MB，**其余 239MB 全是只追加的留痕**。

## 改成什么样

1. **分层**：过程批次**只拷可变状态**（cards.db 等）；留痕交给**日备**（每天一份全量）
   ⇒ 单份体积从 240MB 降到 ~1MB（**100 倍**），且**完整复盘点仍在**（日备）。
2. **只追加的留痕变化不再触发新批次**（指纹只按可变状态算）
   ⇒ 写操作再多，也不会把同一份历史反复抄。
3. **最小间隔**（默认 30 分钟）：过程快照是「兜底复盘点」，不是「每次写的回滚点」
   ⇒ 连续写不会产出几十份几乎相同的副本。**不可逆/突变时刻（删卡、恢复、收口）强制拍**。
4. 每批次写 ``_manifest.json``：这批是什么、留痕在哪（日备）——可恢复性的凭据。

跑法：pytest tests/test_snapshot_mechanism.py -q
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.storage import data_snapshot


STATE_DB = "works/work_001/cards.db"
EVENT_LOG = "works/work_001/events/card_001.jsonl"
SESSION_MD = "works/work_001/sessions/session_001.md"


def _setup(tmp_path, monkeypatch, files: dict[str, str]) -> Path:
    base = tmp_path / "base"
    (base / "works").mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".db"):
            # 真的是个 SQLite 库（快照对 .db 走 VACUUM INTO，假文件会失败）
            con = sqlite3.connect(str(p))
            con.execute("CREATE TABLE IF NOT EXISTS t(x TEXT)")
            con.commit()
            con.close()
        else:
            p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(data_snapshot, "BASE_DIR", base)
    monkeypatch.setattr(data_snapshot, "SNAPSHOT_ROOT", tmp_path / "snaps")
    monkeypatch.setattr(data_snapshot, "_FINGERPRINT_FILE", base / ".snapshot_fingerprints.json")
    monkeypatch.setattr(
        data_snapshot, "_iter_protected_files",
        lambda: [p for p in (base / "works").rglob("*") if p.is_file()],
    )
    # 间隔闸：测试里先放开，单独测间隔时再收紧
    monkeypatch.setattr(data_snapshot, "MIN_SNAPSHOT_INTERVAL", 0)
    monkeypatch.setattr(data_snapshot, "_last_snapshot_at", {})
    return base


def _batches(root: Path, wid: str = "work_001") -> list[Path]:
    wd = root / wid
    if not wd.exists():
        return []
    return sorted([p for p in wd.iterdir()
                   if p.is_dir() and not p.name.startswith("daily_")],
                  key=lambda p: p.name)


def _daily(root: Path, wid: str = "work_001") -> Path | None:
    wd = root / wid
    if not wd.exists():
        return None
    days = [p for p in wd.iterdir() if p.is_dir() and p.name.startswith("daily_")]
    return days[0] if days else None


# ================================================================ 分层

def test_process_batch_keeps_state_only_not_append_only_logs(tmp_path, monkeypatch):
    """过程批次**只含可变状态**；只追加的 events/sessions 不进批次。"""
    base = _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")

    batches = _batches(tmp_path / "snaps")
    assert len(batches) == 1, f"应产生一份过程批次：{batches}"
    got = {str(p.relative_to(batches[0])).replace("\\", "/") for p in batches[0].rglob("*") if p.is_file()}
    assert STATE_DB in got, f"可变状态必须在批次里：{got}"
    assert EVENT_LOG not in got, f"只追加留痕不该进过程批次：{got}"
    assert SESSION_MD not in got, f"只追加留痕不该进过程批次：{got}"


def test_daily_backup_stays_full(tmp_path, monkeypatch):
    """**日备仍是全量**（含留痕）⇒ 完整复盘点不丢。"""
    _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")

    daily = _daily(tmp_path / "snaps")
    assert daily is not None, "应产生日备"
    got = {str(p.relative_to(daily)).replace("\\", "/") for p in daily.rglob("*") if p.is_file()}
    assert EVENT_LOG in got, f"日备必须含留痕：{got}"
    assert SESSION_MD in got, f"日备必须含留痕：{got}"
    assert STATE_DB in got, f"日备必须含状态：{got}"


def test_batch_manifest_says_what_is_omitted(tmp_path, monkeypatch):
    """批次写 manifest：说清这批是 state-only、留痕在哪。"""
    _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")
    batch = _batches(tmp_path / "snaps")[0]
    mf = json.loads((batch / "_manifest.json").read_text(encoding="utf-8"))
    assert mf["mode"] == "state-only"
    assert EVENT_LOG in mf["omitted_log_files"]
    assert "daily_" in mf["log_files_note"], "manifest 要指明完整留痕在日备"


# ================================================================ 触发判据

def test_append_only_log_change_does_not_create_new_batch(tmp_path, monkeypatch):
    """**只追加留痕变了 ⇒ 不产生新批次**（指纹只按可变状态算）。

    这是治本的关键一条：否则每次写操作都因为事件流增长而拍一份全量副本。
    """
    base = _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 1

    (base / EVENT_LOG).write_text("line1\nline2\n", encoding="utf-8")
    data_snapshot.snapshot_sync("t2", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 1, \
        "只追加留痕变化不该刷出新批次（那正是 48GB 的来源）"


def test_state_change_creates_new_batch(tmp_path, monkeypatch):
    """可变状态变了 ⇒ 产生新批次。"""
    base = _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")
    (base / STATE_DB).write_text("db changed", encoding="utf-8")
    data_snapshot.snapshot_sync("t2", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 2, "状态变了必须出新批次"


# ================================================================ 最小间隔

def test_min_interval_skips_but_force_reason_overrides(tmp_path, monkeypatch):
    """间隔内不拍；**不可逆时刻（删卡/恢复）强制拍**。"""
    base = _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    monkeypatch.setattr(data_snapshot, "MIN_SNAPSHOT_INTERVAL", 3600)
    data_snapshot.snapshot_sync("t", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 1

    (base / STATE_DB).write_text("db2", encoding="utf-8")
    data_snapshot.snapshot_sync("t2", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 1, "间隔内不该再拍"

    (base / STATE_DB).write_text("db3", encoding="utf-8")
    data_snapshot.snapshot_sync("[snapshot] delete: card_x", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 2, "删卡这类不可逆时刻必须强制拍"
