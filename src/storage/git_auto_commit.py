# -*- coding: utf-8 -*-
"""git 自动提交。失败只记日志，绝不抛异常中断主流程。"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("storage.git")

BASE_DIR = Path(__file__).resolve().parents[2]

# 模块加载时探测一次 git 可用性，结果缓存
_GIT_AVAILABLE: bool


def _probe_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=15)
        return True
    except Exception:
        return False


_GIT_AVAILABLE = _probe_git()
if not _GIT_AVAILABLE:
    logger.warning("git不可用，自动提交暂停")


async def commit(message: str) -> bool:
    """git add -A && git commit -m {message}。失败记日志返回 False。"""
    if not _GIT_AVAILABLE:
        logger.warning("git不可用，跳过提交: %s", message)
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "add", "-A",
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.warning("git add 失败 (rc=%s): %s", proc.returncode, message)
            return False

        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", message,
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.warning(
                "git commit 失败 (rc=%s): %s | %s",
                proc.returncode, message,
                (stdout or b"").decode("utf-8", "replace").strip() or (stderr or b"").decode("utf-8", "replace").strip(),
            )
            return False
        return True
    except Exception as exc:
        logger.warning("git 提交异常: %s (%s)", message, exc)
        return False
