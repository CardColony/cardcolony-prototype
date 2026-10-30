# -*- coding: utf-8 -*-
"""[快照锁竞态 2026-10-10] data_snapshot 逐文件容错 ＋ 瞬时锁韧性回归。

对应任务：修数据快照「瞬时文件锁撞一次就放弃整批、且缺哪些文件不可见」。

**锁是真锁、不是 mock**：照抄 ``tests/test_session_facade_lockrace.py`` 里现成的
``_lock_read_side`` / ``_BriefLock`` / ``_OsShim`` 写法（``CreateFileW``
``GENERIC_READ + FILE_SHARE_READ``＝允许读、拒绝一切写），不另发明锁形状。

**锁点在哪（实测，非推断）**：``shutil.copy2`` 撞**只读侧**的源根本不失败
（读被放行）；被拒的是 CreateFileW 的**写**请求，即落**目标**那一步，
异常形态 ``PermissionError(errno=13, winerror=None)``、串是单路径——与线上
观测到的「路径在 ``snapshots\\...`` 下」逐字同形。源被 share=0 独占时失败点
才回到源。故本模块一律**预置并锁住目标文件**来复现，形状与线上一致。

用例清单：
- a 短暂锁 ⇒ 重试后成功，整批完整（且确有重试日志）；
- b 永不释放的锁 ⇒ **该文件失败、其余照拷**，返回值与日志都能点名缺谁；
- c 只读属性（真无权限）⇒ **零退避**立即判失败，其余照拷；
- d 非 Windows / 非 PermissionError ⇒ 行为与改动前一致（单次尝试、零退避）；
- e 有失败的批次 ⇒ **指纹不更新**（下次照常重试）；**修剪照跑**（保留闸必须无条件生效，
  见 test_e1_failed_batch_keeps_fingerprint_but_prune_still_runs 的契约变更说明）；
- f 日备部分失败 ⇒ 整份日备作废（缺文件的日备不许被当成「今日已备」）。
"""
from __future__ import annotations

import ctypes
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from src.storage import data_snapshot, session_facade

_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="Windows 文件锁语义专属（POSIX 允许覆盖被读文件）"
)

# CreateFileW 取值（与 test_session_facade_lockrace.py 同款）
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_SHARE_READ = 0x1
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _lock_read_side(path: Path) -> int:
    """对 path 造真句柄锁：允许别人读、拒绝一切写（copy2 落目标必被拒）。"""
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    handle = k32.CreateFileW(
        str(path), _GENERIC_READ, _FILE_SHARE_READ, None, _OPEN_EXISTING, 0x80, None)
    assert handle not in (None, _INVALID_HANDLE), "CreateFileW 建锁失败"
    return handle


def _close_handle(handle: int) -> None:
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


class _BriefLock:
    """后台线程持锁 ``hold`` 秒后自动释放——复刻真实竞态的时间窗。"""

    def __init__(self, path: Path, hold: float = 0.30):
        self.path, self.hold, self._h = path, hold, None
        self._th = threading.Thread(target=self._run, daemon=True)
        self.ready = threading.Event()

    def _run(self) -> None:
        self._h = _lock_read_side(self.path)
        self.ready.set()
        time.sleep(self.hold)
        _close_handle(self._h)

    def __enter__(self):
        self._th.start()
        assert self.ready.wait(5), "锁未能在 5s 内建立"
        return self

    def __exit__(self, *exc):
        self._th.join(timeout=10)
        return False


class _OsShim:
    """只改被测模块眼中的 os.name，不碰真 os 模块。"""

    def __init__(self, real, name):
        self._real, self.name = real, name

    def __getattr__(self, item):
        return getattr(self._real, item)



# ---------------------------------------------------------------- 用例脚手架
_STAMP = "20261010_120000_000000"


def _stage(sandbox, names) -> tuple[str, Path]:
    """在沙箱 works/work_001/ 下铺若干普通文件（不经 card_store，快且确定）。"""
    wid = "work_001"
    d = sandbox.base / "works" / wid
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text(f"内容-{n}", encoding="utf-8")
    return wid, d


def _batch_dst(wid: str, name: str, stamp: str = _STAMP) -> Path:
    return data_snapshot.SNAPSHOT_ROOT / wid / stamp / "works" / wid / name


def _rel(wid: str, name: str) -> str:
    """批次内相对路径的**平台正确**写法（Windows 上是反斜杠，别写死 "/"）。"""
    return str(Path("works") / wid / name)


def _fix_stamp(monkeypatch, stamp: str = _STAMP) -> None:
    monkeypatch.setattr(data_snapshot, "_now_stamp", lambda: stamp)


def _prepare_locked(dst: Path) -> None:
    """预置目标文件（批次目录本该是空的；预置＝复现「目标已存在且被占」）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("STALE", encoding="utf-8")


def _fps() -> dict:
    f = data_snapshot._FINGERPRINT_FILE
    return json.loads(f.read_text("utf-8")) if f.exists() else {}


# ---------------------------------------------------------------- a 短暂锁
@_WINDOWS_ONLY
def test_a1_brief_lock_retried_then_batch_complete(sandbox, monkeypatch, caplog):
    """a：单文件撞**短暂**锁 ⇒ 退避重试后成功，整批完整（一个都不少）。"""
    wid, d = _stage(sandbox, ["a.json", "b.json", "c.json"])
    _fix_stamp(monkeypatch)
    locked = _batch_dst(wid, "b.json")
    _prepare_locked(locked)

    with _BriefLock(locked, hold=0.30), \
            caplog.at_level("WARNING", logger="storage.snapshot"):
        r = data_snapshot.snapshot_sync("lockres-a", work_id=wid)

    assert r["ok"] is True, r
    assert r["failed"] == [], r["failed"]
    assert r["files"] == 3, r
    # 内容逐字正确（含被锁后重试成功的那个）
    for n in ("a.json", "b.json", "c.json"):
        out = _batch_dst(wid, n)
        assert out.exists(), n
        assert out.read_text(encoding="utf-8") == (d / n).read_text(encoding="utf-8"), n
    # 证明确实走过重试（而不是恰好没失败）
    races = [x.getMessage() for x in caplog.records if "文件锁竞态" in x.getMessage()]
    assert races, "短暂锁应留下一条重试 warning"
    assert "attempt=1/5" in races[0]
    # 整批完整 ⇒ 指纹照常置位
    assert wid in _fps()


# ---------------------------------------------------------------- b 永不释放
@_WINDOWS_ONLY
def test_b1_permanent_lock_fails_only_that_file(sandbox, monkeypatch, caplog):
    """b：锁永不释放 ⇒ **该文件失败但其余照拷**；缺谁在返回值与日志里都查得到。"""
    wid, d = _stage(sandbox, ["a.json", "b.json", "c.json"])
    _fix_stamp(monkeypatch)
    locked = _batch_dst(wid, "b.json")
    _prepare_locked(locked)
    h = _lock_read_side(locked)
    try:
        with caplog.at_level("WARNING", logger="storage.snapshot"):
            r = data_snapshot.snapshot_sync("lockres-b", work_id=wid)
    finally:
        _close_handle(h)

    # 整批没有作废：流程跑完了，两个好文件都进了批次
    assert r["ok"] is True, r
    assert r["files"] == 2, r
    for n in ("a.json", "c.json"):
        out = _batch_dst(wid, n)
        assert out.exists(), n
        assert out.read_text(encoding="utf-8") == (d / n).read_text(encoding="utf-8"), n

    # 缺失文件在返回值里逐字可查
    assert [f["file"] for f in r["failed"]] == [_rel(wid, "b.json")], r["failed"]
    assert r["failed"][0]["work"] == wid
    assert r["failed"][0]["stage"] == "batch"
    assert "Permission denied" in r["failed"][0]["error"]
    # 半截文件不留残骸（恢复时宁可缺、不可静默损坏）
    assert not locked.exists() or locked.read_text(encoding="utf-8") == "STALE"

    # 缺失文件在日志里也可查，且一条说清（不刷屏）
    msgs = [x.getMessage() for x in caplog.records
            if "快照部分文件失败" in x.getMessage()]
    assert len(msgs) == 1, msgs
    assert "b.json" in msgs[0] and "(1/3)" in msgs[0], msgs[0]
    # 改动前这里会是一条笼统的「快照异常」，现在点名到文件
    assert not [x for x in caplog.records
                if x.getMessage().startswith("快照异常")]


# ---------------------------------------------------------------- c 真无权限
@_WINDOWS_ONLY
def test_c1_readonly_attr_fails_immediately_with_zero_backoff(sandbox, monkeypatch):
    """c：只读属性（**真无权限**）⇒ 零退避、立即判失败，不得被重试掩盖真错。"""
    wid, d = _stage(sandbox, ["a.json", "b.json"])
    _fix_stamp(monkeypatch)
    ro = _batch_dst(wid, "b.json")
    _prepare_locked(ro)
    os.chmod(ro, stat.S_IREAD)
    assert os.stat(ro).st_file_attributes & session_facade._FILE_ATTRIBUTE_READONLY

    slept = []
    monkeypatch.setattr(data_snapshot.time, "sleep", lambda s: slept.append(s))
    try:
        r = data_snapshot.snapshot_sync("lockres-c", work_id=wid)
    finally:
        os.chmod(ro, stat.S_IWRITE)

    assert slept == [], f"只读属真无权限，不该退避重试：{slept}"
    assert [f["file"] for f in r["failed"]] == [_rel(wid, "b.json")], r["failed"]
    assert "Permission denied" in r["failed"][0]["error"]
    # 其余文件照拷
    ok = _batch_dst(wid, "a.json")
    assert ok.exists() and ok.read_text(encoding="utf-8") == (d / "a.json").read_text(
        encoding="utf-8")


# ------------------------------------------------- d 非 Windows / 非权限错误
@_WINDOWS_ONLY
def test_d1_non_permission_error_single_attempt_no_backoff(sandbox, monkeypatch):
    """d-1：非 PermissionError（ENOENT）⇒ 单次尝试、零退避，且**不外抛**。

    线上 8 条「快照异常」里有 1 条正是 ``FileNotFoundError: cards.db-journal``：
    文件在列目录后、拷贝前消失。改动前它会废掉整批，现在只废那一个文件。
    """
    d = sandbox.base / "works" / "work_001"
    d.mkdir(parents=True, exist_ok=True)
    (d / "keep.json").write_text("内容-keep", encoding="utf-8")
    _fix_stamp(monkeypatch)   # 定住批次名，好对 _batch_dst 取路径
    # 「列目录时还在、拷贝时没了」——用**指向不存在路径的清单**复现，
    # 不用 unlink 真删（本沙箱的 safe-delete 守卫会把删拦成 SystemExit，
    # 那样测到的就不是 FileNotFoundError 了）。
    ghost = d / "cards.db-journal"
    real_iter = data_snapshot._iter_protected_files
    monkeypatch.setattr(
        data_snapshot, "_iter_protected_files",
        lambda: [d / "keep.json", ghost])

    slept = []
    monkeypatch.setattr(data_snapshot.time, "sleep", lambda s: slept.append(s))
    calls = []
    real_copy = data_snapshot._copy_protected_file
    def counting(p, dst):
        calls.append(p.name)
        return real_copy(p, dst)
    monkeypatch.setattr(data_snapshot, "_copy_protected_file", counting)

    r = data_snapshot.snapshot_sync("lockres-d1", work_id="work_001")

    assert slept == [], slept
    # ghost 走两趟（批次 + 日备），每趟**只试一遍**——非锁竞态不重试。
    # 改动前它是第一个异常就跳出整批循环，连 keep.json 都不拷。
    assert calls.count("cards.db-journal") == 2, calls
    assert calls.count("keep.json") == 2, f"后面的文件照拷：{calls}"
    assert r["ok"] is True
    assert {f["file"] for f in r["failed"]} == {_rel("work_001", "cards.db-journal")}
    assert {f["error"].split(":")[0] for f in r["failed"]} == {"FileNotFoundError"}
    assert {f["stage"] for f in r["failed"]} == {"batch", "daily"}
    # 好文件确实进了批次
    assert _batch_dst("work_001", "keep.json").exists()
    assert real_iter  # 保持引用，避免未用告警


@_WINDOWS_ONLY
def test_d2_non_windows_judgement_unchanged(sandbox, monkeypatch):
    """d-2：非 Windows ⇒ 判据恒 False（POSIX 无此竞态），不引入退避。"""
    real = os.name
    assert real == "nt"
    monkeypatch.setattr(session_facade, "os", _OsShim(os, "posix"))
    try:
        slept = []
        monkeypatch.setattr(data_snapshot.time, "sleep", lambda s: slept.append(s))
        src = sandbox.base / "works" / "work_001" / "a.json"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("v", encoding="utf-8")
        dst = _batch_dst("work_001", "a.json")
        _prepare_locked(dst)
        h = _lock_read_side(dst)
        try:
            err = data_snapshot._copy_with_lock_retry(src, dst, "work_001")
        finally:
            _close_handle(h)
        assert err and err.startswith("PermissionError"), err
        assert slept == [], "非 Windows 不该退避"
    finally:
        monkeypatch.setattr(session_facade, "os", os)
    # 非 PermissionError 一律不重试（判据本身对非 Windows 恒 False）
    assert not session_facade._is_windows_lock_race(
        FileNotFoundError(2, "No such file"), src)


# ---------------------------------------------------------------- e 指纹不更新
@_WINDOWS_ONLY
def test_e1_failed_batch_keeps_fingerprint_but_prune_still_runs(sandbox, monkeypatch):
    """e：有文件失败的批次 ⇒ **指纹不更新**（下次照常重试这批）。

    〔2026-10-30 · 契约变更〕**修剪照跑**（原来这条是「不完整的批次不触发修剪」）：
    那条契约正是「保留闸永久失效」的病根——线上一个 0 字节的 ``nul`` 野文件
    让**每一批**都不完整 ⇒ 200 份/2GB 两条保留闸全废 ⇒ 快照库实测涨到
    **705.61GB**（work_027 一个 work 969 批 / 694.38GB）。
    ⇒ 修剪只看**保留策略**，与这批完整与否无关；**指纹语义不变**（失败⇒重试）。
    """
    wid, _ = _stage(sandbox, ["a.json", "b.json"])
    _fix_stamp(monkeypatch)
    pruned = []
    monkeypatch.setattr(data_snapshot, "_prune_batches",
                        lambda w: pruned.append(w) or 0)

    locked = _batch_dst(wid, "b.json")
    _prepare_locked(locked)
    h = _lock_read_side(locked)
    try:
        r = data_snapshot.snapshot_sync("lockres-e1", work_id=wid)
    finally:
        _close_handle(h)

    assert r["failed"], r
    assert wid not in _fps(), "缺文件的批次不许被当成完整批次而永久跳过"
    assert pruned == [wid], "保留闸必须无条件生效（与这批完整与否无关）"
    assert r["skipped"] is False, "未置指纹 ⇒ 本轮不算「内容未变」"

    # 解锁后重跑：这次补齐、置指纹
    r2 = data_snapshot.snapshot_sync("lockres-e1-retry", work_id=wid)
    assert r2["skipped"] is False, "上一次失败 ⇒ 这次必须真拍而不是跳过"
    assert r2["failed"] == [], r2["failed"]
    assert _batch_dst(wid, "b.json").read_text(encoding="utf-8") == "内容-b.json"
    assert wid in _fps()
    assert pruned == [wid, wid]

    # 再拍一次：内容未变 ⇒ 走既有跳过语义（指纹语义未被改动）
    r3 = data_snapshot.snapshot_sync("lockres-e1-again", work_id=wid)
    assert r3["skipped"] is True and r3["files"] == 0, r3


# ---------------------------------------------------------------- f 日备作废
@_WINDOWS_ONLY
def test_f1_partial_daily_dir_is_discarded(sandbox, monkeypatch):
    """f：日备部分文件失败 ⇒ 整份日备作废（否则明日当成「今日已备」永不补齐）。

    日备目录的 ``not daily.exists()`` 闸使真实撞锁难以在测试里复现（预置即
    被闸挡掉），故此处只在日备分支注入一个 FileNotFoundError——被验证的是
    「整份作废」这条新纪律本身，拷贝与记账都走真实代码。
    """
    wid, _ = _stage(sandbox, ["a.json", "b.json"])
    _fix_stamp(monkeypatch)   # 定住批次名，好对 _batch_dst 取路径
    real_copy = data_snapshot._copy_protected_file

    def flaky(p, dst):
        if "daily_" in str(dst) and p.name == "b.json":
            raise FileNotFoundError(2, "No such file or directory", str(p))
        return real_copy(p, dst)
    monkeypatch.setattr(data_snapshot, "_copy_protected_file", flaky)

    # 本沙箱的 safe-delete 守卫会把真删拦成 SystemExit（_safe_rmtree 接住、不外抛），
    # 故不断言「目录已消失」，而断言**清理动作确实被发起**——那才是新纪律本身。
    pruned = []
    real_rmtree = data_snapshot._safe_rmtree
    monkeypatch.setattr(
        data_snapshot, "_safe_rmtree",
        lambda q: (pruned.append(q), real_rmtree(q))[1])

    r = data_snapshot.snapshot_sync("lockres-f", work_id=wid)

    assert r["daily"] is False, "日备不完整不得标成已完成"
    assert any(getattr(q, "name", "").startswith("daily_") for q in pruned), \
        f"残缺日备须整份作废（须调 _safe_rmtree）：pruned={pruned}"
    assert any(f["stage"] == "daily" for f in r["failed"]), r["failed"]
    # 批次本体仍是「好文件进了、坏的记账」，且指纹不更新
    assert _batch_dst(wid, "a.json").exists()
    assert wid not in _fps()
