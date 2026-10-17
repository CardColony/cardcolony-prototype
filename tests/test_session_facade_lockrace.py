# -*- coding: utf-8 -*-
"""[WinErr13 竞态 2026-10-17] Windows 文件锁竞态复现与回归用例。

对应任务：修 ``session_facade`` 在 Windows 上真实 502
（``[Errno 13] Permission denied: '<session>.md'``，整轮作废）。

**锁是真锁、不是 mock**：本机实测 Windows 上写者被拒时抛的正是
``PermissionError(errno=13, winerror=None)``，异常串是**单路径**
``[Errno 13] Permission denied: 'path'``——与线上 502 逐字同形；
而 ``os.replace`` 撞锁抛的是**双路径** ``[WinError 5] 拒绝访问。: 'a' -> 'b'``
（带 winerror）。故用 ``CreateFileW`` 造真句柄锁，不 monkeypatch 异常。

**锁的形状**＝``GENERIC_READ`` ＋ ``FILE_SHARE_READ``（实测）：

===================  ==================
操作                 结果
===================  ==================
``open(dest,'r')``   **OK**（读路径不受影响）
``open(dest,'a')``   PermissionError errno=13 winerror=None ← 真实 502 的形态
``os.replace``       PermissionError winerror=5
===================  ==================

这正是「并发 reader/append 撞上另一写方的 tmp→replace 窗口」的实况，
也是 L2 能先在旧码上复现 502 的原因。

用例清单：
- L1 ``_atomic_write_text`` 撞短暂锁 → 最终成功（内容逐字正确）；
- L1b 固定名 ``.tmp`` 被并发写方占住 → tmp 打开重试后成功；
- L2 **append 热路径撞锁 → 最终成功＝真实 502 的复现与修复证明**（旧码失败）；
- L2b ``append_many``（A＋N×B 同写）撞锁 → 整批落盘；
- L3 锁一直不放 ⇒ 仍抛 PermissionError（不静默吞），日志 ≤2 条；
- L4 真无权限（只读属性）⇒ **零重试**立即上抛（新增判据，不得掩盖真错）；
- L5 非锁竞态错误（ENOENT 等）⇒ 不重试；
- L6 非 Windows / 非 PermissionError ⇒ 行为不变；
- L7 日志含 attempt/winerror 且不刷屏。
"""
from __future__ import annotations

import ctypes
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from src.storage import card_store, session_facade

_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="Windows 文件锁语义专属（POSIX 允许 rename 覆盖被读文件）"
)

# CreateFileW 取值
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_SHARE_READ = 0x1
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _lock_read_side(path: Path) -> int:
    """对 path 造真句柄锁：允许别人读、拒绝一切写（append/replace 必被拒）。"""
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


# 修复前基线：本模块尚无锁竞态重试助手 ⇒ 下面三条纯新 API 单测跳过，
# 让「修复前」跑只剩下**复现类**用例的红（那才是 502 的证据）。
_HAS_FIX = hasattr(session_facade, "_open_with_lock_retry")
_needs_fix = pytest.mark.skipif(
    not _HAS_FIX, reason="修复前基线：锁竞态重试助手尚不存在")


class _OsShim:
    """只改 session_facade 眼中的 os.name，不碰真 os 模块。

    （直接 monkeypatch os.name 会污染全局，令 pytest 在 Windows 上构造出
    PosixPath 而内部崩溃。）"""

    def __init__(self, real, name):
        self._real, self.name = real, name

    def __getattr__(self, item):
        return getattr(self._real, item)


def _prime_v2(wid: str, cid: str) -> Path:
    """把会话铺成 V2 空会话（与 conftest.prime_card_session 同法），使后续走 append 分支。"""
    path = session_facade._session_path(wid, cid)
    session_facade._atomic_write_text(
        path, session_facade._render_v2_header(session_facade._default_header(cid)))
    return path


# ---------------------------------------------------------------- 复现：写最终成功
@_WINDOWS_ONLY
def test_l1_atomic_write_survives_brief_lock(tmp_path):
    """L1（任务书验收 1）：目标文件被短暂占用 → _atomic_write_text 最终成功。"""
    target = tmp_path / "session_001.md"
    target.write_text("OLD\n", encoding="utf-8")

    with _BriefLock(target, hold=0.30):
        # 锁确实生效（否则本用例假绿）：写者此刻必被拒
        with pytest.raises(PermissionError):
            open(target, "a").close()
        session_facade._atomic_write_text(target, "NEW\n")

    assert target.read_text(encoding="utf-8") == "NEW\n"
    assert not (tmp_path / "session_001.md.tmp").exists(), "tmp 应已被 replace 消耗"


@_WINDOWS_ONLY
def test_l1b_atomic_write_survives_tmp_collision(tmp_path):
    """L1b：固定名 ``.tmp`` 被并发写方占住 → 打开 tmp 也走重试后成功。

    ``tmp = path.name + '.tmp'`` 是**固定名**，同一文件的两个并发原子写方会撞同
    一个 tmp；这是改动前**完全没有重试**的一环。
    """
    target = tmp_path / "session_001.md"
    target.write_text("OLD\n", encoding="utf-8")
    tmp = tmp_path / "session_001.md.tmp"
    tmp.write_text("STALE", encoding="utf-8")

    with _BriefLock(tmp, hold=0.30):
        with pytest.raises(PermissionError):
            open(tmp, "w").close()
        session_facade._atomic_write_text(target, "NEW\n")

    assert target.read_text(encoding="utf-8") == "NEW\n"


@_WINDOWS_ONLY
async def test_l2_session_append_survives_brief_lock(work):
    """L2：**真实 502 的复现与修复证明**——append 热路径撞锁后最终成功。

    线上 502 形态是单路径 ``[Errno 13]``，只可能来自 ``open()``；本模块原先
    唯独给 ``os.replace``（双路径 WinError 5）配了重试，append 零重试。
    读路径不受本锁影响，故唯一被拒的正是 ``open(path,'a')``。
    """
    r = await card_store.create_card(work["work_id"], "锁竞态卡", "用户直接创建")
    assert "error" not in r, r
    wid, cid = work["work_id"], r["meta"]["card_id"]
    path = _prime_v2(wid, cid)

    with _BriefLock(path, hold=0.30):
        blk = await session_facade.append(
            wid, cid, {"role": "user", "content": "锁住了也要落盘"})
    assert blk["seq"] == 0
    assert "锁住了也要落盘" in path.read_text(encoding="utf-8")


@_WINDOWS_ONLY
async def test_l2b_append_many_survives_brief_lock(work):
    """L2b：批量追加（A＋N×B 同写）撞锁 → 整批落盘，成对性不受影响。"""
    r = await card_store.create_card(work["work_id"], "批量卡", "用户直接创建")
    wid, cid = work["work_id"], r["meta"]["card_id"]
    path = _prime_v2(wid, cid)

    with _BriefLock(path, hold=0.30):
        out = await session_facade.append_many(wid, cid, [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ])
    assert [b["seq"] for b in out] == [0, 1]
    assert path.read_text(encoding="utf-8").count(session_facade.BLOCK_MARKER) == 2


# ---------------------------------------------------------------- 失败路径不静默吞
@_WINDOWS_ONLY
def test_l3_lock_never_released_still_raises(tmp_path, caplog):
    """L3：锁一直不放 ⇒ 重试耗尽后**照旧抛 PermissionError**，且日志不刷屏。"""
    target = tmp_path / "session_001.md"
    target.write_text("OLD\n", encoding="utf-8")
    h = _lock_read_side(target)
    try:
        with caplog.at_level("WARNING", logger="storage.session_facade"):
            with pytest.raises(PermissionError):
                session_facade._atomic_write_text(target, "NEW\n")
    finally:
        _close_handle(h)

    races = [r for r in caplog.records if "文件锁竞态" in r.getMessage()]
    assert races, "锁竞态应有 warning 便于对账"
    assert len(races) <= 2, f"日志刷屏：{len(races)} 条"
    assert "重试耗尽" in races[-1].getMessage()
    assert "winerror=" in races[-1].getMessage()
    assert target.read_text(encoding="utf-8") == "OLD\n", "失败不得改动原文件"


# ---------------------------------------------------------------- 其它 PermissionError 逐字不变
@_WINDOWS_ONLY
@_needs_fix
def test_l4_real_no_permission_raises_without_retry(tmp_path, monkeypatch):
    """L4：**真无权限**（只读属性）⇒ 零重试、立即上抛，不得被重试掩盖。

    只读文件的 ``open()`` 与锁竞态异常形态完全相同（都是单路径 errno 13），
    故靠只读位区分——本用例钉死这条新判据不误伤真错。
    """
    target = tmp_path / "session_001.md"
    target.write_text("OLD\n", encoding="utf-8")
    os.chmod(target, stat.S_IREAD)
    try:
        assert os.stat(target).st_file_attributes & session_facade._FILE_ATTRIBUTE_READONLY
        slept = []
        monkeypatch.setattr(session_facade.time, "sleep", lambda s: slept.append(s))
        with pytest.raises(PermissionError):
            session_facade._open_with_lock_retry(target, "a", "unit")
        assert slept == [], f"只读属真无权限，不该退避重试：{slept}"
    finally:
        os.chmod(target, stat.S_IWRITE)


@_WINDOWS_ONLY
@_needs_fix
def test_l5_non_lock_errors_not_retried(tmp_path, monkeypatch):
    """L5：非 PermissionError（ENOENT 等）⇒ 不重试、立即上抛。"""
    missing = tmp_path / "no_such_dir" / "x.md"   # 父目录不存在 ⇒ FileNotFoundError
    slept = []
    monkeypatch.setattr(session_facade.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(FileNotFoundError):
        session_facade._open_with_lock_retry(missing, "w", "unit")
    assert slept == []

    # winerror 非 5/32 的 PermissionError（3＝路径不存在）⇒ 不重试
    exc = PermissionError(13, "x", str(tmp_path / "a.md"))
    exc.winerror = 3
    assert not session_facade._is_windows_lock_race(exc, tmp_path / "a.md")


@_needs_fix
def test_l6_non_windows_and_non_permission_unchanged(tmp_path, monkeypatch):
    """L6：非 Windows 平台 ⇒ 判据恒 False（POSIX 无此竞态，不引入 sleep）。"""
    real = os.name
    monkeypatch.setattr(session_facade, "os", _OsShim(os, "posix"))
    try:
        assert not session_facade._is_windows_lock_race(
            PermissionError(13, "Permission denied"), tmp_path)
    finally:
        monkeypatch.setattr(session_facade, "os", os)
    assert real == "nt"
    # 非 PermissionError 一律不重试
    assert not session_facade._is_windows_lock_race(
        FileNotFoundError(2, "No such file"), tmp_path)


@_WINDOWS_ONLY
def test_l7_log_carries_attempt_and_winerror(tmp_path, caplog):
    """L7：首撞即记一条 warning，含 attempt/winerror/errno（便于对账）。"""
    target = tmp_path / "session_001.md"
    target.write_text("OLD\n", encoding="utf-8")
    with _BriefLock(target, hold=0.25), \
            caplog.at_level("WARNING", logger="storage.session_facade"):
        session_facade._atomic_write_text(target, "NEW\n")
    races = [r for r in caplog.records if "文件锁竞态" in r.getMessage()]
    assert races, "首撞必须留一条 warning"
    msg = races[0].getMessage()
    assert "attempt=1/5" in msg and "winerror=" in msg and "errno=" in msg
