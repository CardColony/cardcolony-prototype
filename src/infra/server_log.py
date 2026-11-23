# -*- coding: utf-8 -*-
"""服务端日志**落盘**（2026-10-26）

## 为什么

服务端日志原先只进**启动它的那个控制台** ⇒ 用户看不到 ⇒「我不知道有没有日志」
⇒ 排障只能靠猜（本轮先后猜了释放策略、事件循环堵死、快照机制…）。

⇒ web 和单进程落盘到 ``logs/server.log``，runner 与凝练各自轮转（16MB × 2）：

    type logs\\server.log -Wait        （PowerShell 实时跟）
    tail -f logs/server.log           （bash 实时跟）

覆盖：root logger（我们自己的 logger）＋ uvicorn 的 access / error / asgi。
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: 日志文件名（相对仓库根）。
LOG_NAME = "server.log"
#: 单文件上限 16MB，保留 2 份历史。
MAX_BYTES = 16 * 1024 * 1024
BACKUP_COUNT = 2
#: 要挂文件 handler 的 logger（"" ＝ root）。
_LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error", "uvicorn.asgi")


def install_file_logging(repo_root: Path | str,
                         level: int = logging.INFO) -> Path | None:
    """安装本进程的轮转日志。Windows 上多个进程不能同时轮转同一文件。"""
    root = Path(repo_root)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    role = (os.environ.get("ECO_ROLE") or "").strip().lower()
    if role == "runner":
        index = int(os.environ.get("ECO_RUNNER_INDEX") or "0")
        path = logs / f"server-runner{index}.log"
    elif role == "condense":
        path = logs / "server-condense.log"
    else:
        path = logs / LOG_NAME
    existing = getattr(install_file_logging, "_installed", None)
    if existing == str(path):
        return path
    h = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                           encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        lg.addHandler(h)
        if name == "":
            lg.setLevel(level)
    install_file_logging._installed = str(path)
    return path
