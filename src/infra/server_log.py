# -*- coding: utf-8 -*-
"""服务端日志**落盘**（2026-10-26）

## 为什么

服务端日志原先只进**启动它的那个控制台** ⇒ 用户看不到 ⇒「我不知道有没有日志」
⇒ 排障只能靠猜（本轮先后猜了释放策略、事件循环堵死、快照机制…）。

⇒ 落盘到 ``logs/server.log``（轮转 16MB × 2），随时可看：

    type logs\\server.log -Wait        （PowerShell 实时跟）
    tail -f logs/server.log           （bash 实时跟）

覆盖：root logger（我们自己的 logger）＋ uvicorn 的 access / error / asgi。
"""
from __future__ import annotations

import logging
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
    """把服务端日志挂到 ``<repo_root>/logs/server.log``。返回文件路径（失败 ⇒ None）。

    幂等：重复调用只会多挂一个 handler（用标记避免）。
    """
    root = Path(repo_root)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
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
