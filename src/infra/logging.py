"""structlog 结构化日志与 trace_id 工具。"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import structlog

_LOGGER_CONFIGURED = False


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def setup_logging(config: dict | None = None) -> None:
    """初始化 structlog，日志写入 logs/<date>.log（写入失败则回退到控制台）。"""
    global _LOGGER_CONFIGURED
    config = config or {}
    log_dir = Path(config.get("log_dir", "./logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{_today()}.log"
        file_factory = structlog.WriteLoggerFactory(
            file=log_file.open("a", encoding="utf-8")
        )
    except Exception:
        file_factory = structlog.PrintLoggerFactory()

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=file_factory,
        cache_logger_on_first_use=False,
    )
    _LOGGER_CONFIGURED = True


def get_logger(name: str):
    """返回 structlog 日志器；在 setup_logging 之前也可安全调用。"""
    return structlog.get_logger(name)


def new_trace_id() -> str:
    """生成全链路唯一标识（8 位）。"""
    return str(uuid.uuid4())[:8]
