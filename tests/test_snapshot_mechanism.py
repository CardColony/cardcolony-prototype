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
   ⇒ 单份体积从 173MB 降到 59MB（work_025 实测）、105MB 降到 12MB（work_018）
   ⇒ 全库 49.8GB → ≈15GB（份数上限 200 自然封顶，D 盘余量 949GB）。
2. **只追加的留痕变化不再触发新批次**（指纹只按可变状态算）
   ⇒ 写操作再多，也不会把同一份历史反复抄。
3. **没有最小间隔**（这一条是自我更正）：原先加过「30 分钟间隔」，实测它砍掉了
   产品里写明的既有契约（「清指纹必须真拍」「上批失败下次必须重试」）⇒ 全量跑出
   7 条新红 ⇒ 已撤。撤掉后体积仍由「只拷可变状态」治住，不需要靠限量。
4. 每批次写 ``_manifest.json``：这批是什么、留痕在哪（日备）——可恢复性的凭据。

跑法：pytest tests/test_snapshot_mechanism.py -q
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from src.storage import data_snapshot


STATE_DB = "works/work_001/cards.db"
STATE_JSON = "works/work_001/canvas.json"
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
    return base


def _write_db(path: Path, marker: str) -> None:
    """写一个**真的** SQLite 库（快照对 .db 走 VACUUM INTO，假文件会失败）。"""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS t(x TEXT)")
        con.execute("DELETE FROM t")
        con.execute("INSERT INTO t(x) VALUES (?)", (marker,))
        con.commit()
    finally:
        con.close()


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
    """批次写 manifest：说清这批是 state-only、留痕在哪。

    manifest 写在批次目录**旁边**（不写进批次里）：批次目录的既有契约是
    「只装运行数据（顶层只有 works/）」——manifest 塞进去会破坏它。
    """
    _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")
    batch = _batches(tmp_path / "snaps")[0]
    mf_path = batch.with_name(batch.name + ".manifest.json")
    assert mf_path.exists(), "manifest 应写在批次旁边"
    mf = json.loads(mf_path.read_text(encoding="utf-8"))
    assert mf["mode"] == "state-only"
    assert EVENT_LOG in mf["omitted_log_files"]
    assert "daily_" in mf["log_files_note"], "manifest 要指明完整留痕在日备"
    # 批次目录本身**只装运行数据**（既有契约：顶层只有 works/）
    tops = {p.relative_to(batch).parts[0] for p in batch.rglob("*") if p.is_file()}
    assert tops == {"works"}, tops


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
    _write_db(base / STATE_DB, "db changed")
    data_snapshot.snapshot_sync("t2", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 2, "状态变了必须出新批次"


# ================================================================ 判据：只按内容去重，**没有间隔**

def test_unchanged_state_does_not_create_new_batch(tmp_path, monkeypatch):
    """可变状态没变 ⇒ 不拍（内容指纹去重仍在，且这是唯一的跳过理由）。"""
    _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 1

    r = data_snapshot.snapshot_sync("t2", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 1, "状态没变就不该再拍"
    assert r["skipped"] is True and r["files"] == 0, r


def test_cleared_fingerprint_forces_real_snapshot(tmp_path, monkeypatch):
    """**产品里写明的既有契约**：清掉指纹 ⇒ 本次必须真拍（不是放宽断言）。

    〔5715 · 自我更正〕这条契约曾被「30 分钟最小间隔」砍掉——间隔闸把「清了指纹
    还要再拍」判成 skipped ⇒ 全量跑出 7 条新红 ⇒ 间隔已撤，这里把契约钉死。
    """
    base = _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")
    assert len(_batches(tmp_path / "snaps")) == 1

    data_snapshot._FINGERPRINT_FILE.unlink()          # 清指纹（既有 net 的做法）
    _write_db(base / STATE_DB, "v2")
    r = data_snapshot.snapshot_sync("t2", work_id="work_001")
    assert r["skipped"] is False, "清了指纹就必须真拍"
    assert r["files"] > 0, r
    assert len(_batches(tmp_path / "snaps")) == 2


def test_failed_batch_is_not_fingerprinted_and_retries_next_call(tmp_path, monkeypatch):
    """缺文件的批次**不许**被当成完整批次而永久跳过（既有契约）。

    这条契约曾被间隔闸一并砍掉（上批失败 ⇒ 指纹没置位 ⇒ 下次本该重试，
    却被间隔判成 skipped）⇒ 这里钉死：上批失败 ⇒ 下次必真拍。
    """
    _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", STATE_JSON: '{"x":1}',
        EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    real_copy = data_snapshot._copy_protected_file

    def flaky(p, dst):
        if p.name == "cards.db":
            raise OSError("boom: 模拟这批没拷全")
        return real_copy(p, dst)

    monkeypatch.setattr(data_snapshot, "_copy_protected_file", flaky)
    r = data_snapshot.snapshot_sync("t", work_id="work_001")
    assert r["failed"], f"这批应当记账为失败：{r}"
    fps = json.loads(data_snapshot._FINGERPRINT_FILE.read_text(encoding="utf-8"))
    assert "work_001" not in fps, "缺文件的批次不许被当成完整批次而永久跳过"

    monkeypatch.setattr(data_snapshot, "_copy_protected_file", real_copy)
    r2 = data_snapshot.snapshot_sync("t2", work_id="work_001")
    assert r2["skipped"] is False, "上批失败 ⇒ 这次必须真拍而不是跳过"
    assert not r2["failed"], r2["failed"]
    assert len(_batches(tmp_path / "snaps")) == 2, "补拍应多出一份批次"


def test_no_min_interval_blind_window(tmp_path, monkeypatch):
    """**没有间隔盲窗**：状态一变就出新批次（不是等 N 分钟）。

    钉死机制：过程批次是「兜底复盘点」，但**不留 30 分钟盲窗**——盲窗期内
    进程挂掉，那段时间的改卡就找不回来了。
    """
    base = _setup(tmp_path, monkeypatch, {
        STATE_DB: "db", EVENT_LOG: "line1\n", SESSION_MD: "session\n",
    })
    data_snapshot.snapshot_sync("t", work_id="work_001")
    for i, marker in enumerate(("v2", "v3", "v4")):
        _write_db(base / STATE_DB, marker)
        data_snapshot.snapshot_sync(f"t{i + 2}", work_id="work_001")
        assert len(_batches(tmp_path / "snaps")) == i + 2, \
            f"状态变了必须立刻出新批次（第 {i + 2} 次）"


# ================================================================ 调度层：合并（不丢恢复点）

def _reset_sched_state() -> None:
    data_snapshot._snap_tasks.clear()
    data_snapshot._snap_dirty.clear()
    data_snapshot._snap_last_at.clear()


async def test_background_schedule_coalesces_writes(tmp_path, monkeypatch):
    """后台路径：**合并窗口**内的连续写不立刻拍（写放大被压住）。

    〔5715 · 自我更正 · 第二次〕合并窗口必须放**调度层**（schedule），
    不能放 API 层（snapshot_sync）——放错层会把「显式调用即执行」的契约砍掉。
    """
    calls: list[str] = []

    async def fake_snapshot(reason="", work_id=None):
        calls.append(reason)
        return True

    monkeypatch.setattr(data_snapshot, "snapshot", fake_snapshot)
    monkeypatch.setattr(data_snapshot, "SNAPSHOT_COALESCE_SECONDS", 30)
    _reset_sched_state()
    try:
        await data_snapshot.schedule("w1", work_id="work_001")
        await asyncio.sleep(0.02)
        assert len(calls) == 1, f"第一次应立即拍：{calls}"

        await data_snapshot.schedule("w2", work_id="work_001")
        await data_snapshot.schedule("w3", work_id="work_001")
        await asyncio.sleep(0.05)
        assert len(calls) == 1, f"窗口内的连续写不该立刻再拍：{calls}"
    finally:
        for t in list(data_snapshot._snap_tasks.values()):
            t.cancel()
        _reset_sched_state()


async def test_background_schedule_force_reason_snapshots_now(tmp_path, monkeypatch):
    """**不可逆时刻（删卡/恢复/收口）当场拍**，不等合并窗口。

    错过就再也回不去了 ⇒ 合并窗口不能吞掉它。
    """
    calls: list[str] = []

    async def fake_snapshot(reason="", work_id=None):
        calls.append(reason)
        return True

    monkeypatch.setattr(data_snapshot, "snapshot", fake_snapshot)
    monkeypatch.setattr(data_snapshot, "SNAPSHOT_COALESCE_SECONDS", 300)
    _reset_sched_state()
    try:
        await data_snapshot.schedule("w1", work_id="work_001")
        await asyncio.sleep(0.02)
        assert len(calls) == 1

        await data_snapshot.schedule(
            "[snapshot][work_001] delete: card_x", work_id="work_001")
        await asyncio.sleep(0.05)
        assert len(calls) == 2, f"删卡必须当场拍，不等窗口：{calls}"
    finally:
        for t in list(data_snapshot._snap_tasks.values()):
            t.cancel()
        _reset_sched_state()


# ================================================================ 恢复：**真生效**

async def test_restore_really_rolls_back_content(work):
    """恢复必须**真的**把内容回滚（旧 WAL 不得把旧数据复活）。

    〔5715 · 根因修复〕WAL 模式下 live 库旁的 ``cards.db-wal`` 常常删不掉
    （长活连接还握着它 ⇒ WinError 32）。若恢复只做「复制文件 + 删旁挂」，
    删不掉的旧 WAL 会**把旧数据复活**：文件已是快照内容，读出来却是旧值，
    而 ``restore_from_snapshot`` 还返回 ok=True ⇒ **静默失败**（实测曾如此）。

    现在恢复 .db 走 SQLite **backup API** 回写内容 ⇒ 内容真回滚。
    """
    from src.storage import card_store

    wid = work["work_id"]
    root = work["work_card_id"]

    await card_store.update_card(wid, root, {"conclusion": "第一版正文"}, "写第一版")
    r = data_snapshot.snapshot_sync("restore-A", work_id=wid)
    assert r["ok"] is True and r["files"] > 0, r
    stamp = Path(r["dir"]).name

    await card_store.update_card(wid, root, {"conclusion": "第二版正文"}, "写第二版")
    assert (await card_store.read_card(wid, root))["body"]["status_block"] == "第二版正文"

    res = data_snapshot.restore_from_snapshot(wid, stamp, f"works/{wid}/cards.db")
    assert res["ok"] is True, res
    data = await card_store.read_card(wid, root)
    assert data["body"]["status_block"] == "第一版正文", \
        "恢复没生效：旧 WAL 把旧数据复活了（这就是那次静默失败）"

