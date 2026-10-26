# -*- coding: utf-8 -*-
"""服务端日志**落盘**的网（[P27 · 2026-10-26]）

## 为什么

服务端日志原先只进**启动它的那个控制台** ⇒ 用户看不到 ⇒「我不知道有没有日志」
⇒ 排障只能靠猜。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| G1 | 装完之后，`logs/server.log` **真的被写**（root logger 的 INFO 进得去） |
| G2 | uvicorn 的 logger 也被挂上（access/error）⇒ 访问日志也在文件里 |
| G3 | 幂等：重复调用**不重复挂** handler（否则一行日志写 N 遍） |

铁律一：只用 tmp_path，不碰真实 logs/。
"""
from __future__ import annotations

import logging

from src.infra import server_log


def test_g1_root_logger_writes_to_file(tmp_path):
    """G1：root logger 的日志进得去文件。"""
    path = server_log.install_file_logging(tmp_path)
    assert path is not None and path.exists()
    try:
        logging.getLogger("writing-agent").info("落盘探针-这条应该出现在 server.log")
        for h in logging.getLogger("").handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        assert "落盘探针" in text, text[-500:]
    finally:
        # 摘掉本用例挂的 handler（别污染别的用例）
        for name in server_log._LOGGERS:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                if getattr(h, "baseFilename", "").endswith("server.log"):
                    lg.removeHandler(h)


def test_g2_uvicorn_loggers_are_covered():
    """G2：uvicorn 的 logger 也被挂上。"""
    assert "uvicorn.access" in server_log._LOGGERS
    assert "uvicorn.error" in server_log._LOGGERS


def test_g3_install_is_idempotent(tmp_path):
    """G3：幂等（重复调用不重复挂 handler）。"""
    server_log.install_file_logging(tmp_path)
    before = len(logging.getLogger("").handlers)
    server_log.install_file_logging(tmp_path)
    after = len(logging.getLogger("").handlers)
    assert after == before, "重复安装会多挂 handler（一行日志写 N 遍）"
    for name in server_log._LOGGERS:
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            if getattr(h, "baseFilename", "").endswith("server.log"):
                lg.removeHandler(h)
