# -*- coding: utf-8 -*-
"""裸 stdout/stderr 的进程内轮转（2026-11-22 · 磁盘治理第一批）。

**为什么**：``start.bat``/``restart_daily.bat`` 用 ``>>`` 把各进程 stdout
追加到 ``logs/runnerN_stdout.log`` 等文件——**无界增长**（实测 runner2
单文件 11.21GiB，四份共 17.56GiB），且 Windows 上文件被句柄占着，
应用层想改名轮转必 ``WinError 32``。

⇒ 由**进程自己**接管：装了它就写到有上限的轮转文件（默认 32MB × 2 份），
``>>`` 的兜底输出改落到小 ``*_boot.log``（安装前的早期崩溃也有留痕）。

启用方式（环境变量，谁设谁转，不设不动）：
    ``ECO_STDOUT_LOG=logs\runner0_stdout.log``
    ``ECO_STDOUT_LOG_MAX``（字节，默认 32MB）、``_BACKUPS``（默认 2）。

**绝不炸宿主**：写入/轮转的任何异常全部吞掉——打印通道坏了不能把服务带死。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Optional

_DEFAULT_MAX = 32 * 1024 * 1024
_DEFAULT_BACKUPS = 2


class _RotatingStream:
    """最小 TextIO 替身：write → 追加 + 超限轮转（doRollover 内联）。"""

    def __init__(self, path: Path, max_bytes: int, backups: int) -> None:
        self._path = path
        self._max = max_bytes
        self._backups = backups
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "ab", buffering=0)
            self._size = path.stat().st_size if path.exists() else 0
        except OSError:
            self._fh = None

    # -- 轮转 ----------------------------------------------------------
    def _rollover(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except OSError:
            pass
        self._fh = None
        try:
            for i in range(self._backups - 1, 0, -1):
                src = self._path.with_name(f"{self._path.name}.{i}")
                dst = self._path.with_name(f"{self._path.name}.{i + 1}")
                if src.exists():
                    try:
                        src.replace(dst)
                    except OSError:
                        pass
            first = self._path.with_name(f"{self._path.name}.1")
            if self._path.exists():
                try:
                    self._path.replace(first)
                except OSError:
                    pass
            oldest = self._path.with_name(
                f"{self._path.name}.{self._backups + 1}")
            if oldest.exists():
                try:
                    oldest.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        try:
            self._fh = open(self._path, "ab", buffering=0)
            # 〔2026-11-22 返修〕改名可能失败（外部句柄占着主文件）——
            # 按**文件实际大小**继续计量，下一次 write 还会再试改名；
            # 归零会放纵文件在被占期间无界再涨一轮。
            self._size = (self._path.stat().st_size
                          if self._path.exists() else 0)
        except OSError:
            self._fh = None
            self._size = 0

    # -- file-like ------------------------------------------------------
    def write(self, s) -> int:
        try:
            data = s if isinstance(s, bytes) else str(s).encode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return 0
        received = len(data)
        if received > self._max:
            data = data[:self._max].decode("utf-8", "ignore").encode("utf-8")
        with self._lock:
            try:
                if self._fh is None:
                    try:
                        self._fh = open(self._path, "ab", buffering=0)
                        self._size = (self._path.stat().st_size
                                      if self._path.exists() else 0)
                    except OSError:
                        return 0
                if self._size + len(data) > self._max:
                    self._rollover()
                if self._fh is not None and self._size + len(data) <= self._max:
                    self._fh.write(data)
                    self._size += len(data)
            except Exception:  # noqa: BLE001
                pass
        return received

    def writelines(self, lines) -> None:
        for ln in lines:
            self.write(ln)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("rotating stream has no fd")

    @property
    def encoding(self) -> str:
        return "utf-8"


def install_from_env() -> Optional[Path]:
    """``ECO_STDOUT_LOG`` 有值 ⇒ stdout/stderr 重指到轮转文件。返回路径或 None。"""
    raw = (os.environ.get("ECO_STDOUT_LOG") or "").strip()
    if not raw:
        return None
    try:
        max_b = int(os.environ.get("ECO_STDOUT_LOG_MAX") or _DEFAULT_MAX)
        bks = int(os.environ.get("ECO_STDOUT_LOG_BACKUPS") or _DEFAULT_BACKUPS)
        stream = _RotatingStream(Path(raw), max(1, max_b), max(1, bks))
        sys.stdout = stream        # type: ignore[assignment]
        sys.stderr = stream        # type: ignore[assignment]
        return Path(raw)
    except Exception:  # noqa: BLE001
        return None
