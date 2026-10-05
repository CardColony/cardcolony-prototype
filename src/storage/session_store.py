# -*- coding: utf-8 -*-
"""会话存储。追加式消息记录，永不改写已有内容。

Phase 7：会话归属于 work，路径 = works/{work_id}/sessions/session_NNN.md。

消息格式：
---MSG---
role: user            # 或 assistant
timestamp: ISO8601
model: 模型名          # 仅 assistant 消息有
---
消息正文（可多行）

转义规则：正文中某行恰好是 "---MSG---" 时，行首加反斜杠写成 "\\---MSG---"；
解析时还原。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiofiles

logger = logging.getLogger("storage.session")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKS_DIR = BASE_DIR / "works"
SESSIONS_DIR = BASE_DIR / "sessions"  # 旧路径，兼容用

MARKER = "---MSG---"
ESCAPED_MARKER = "\\---MSG---"


def _session_path(work_id: str, card_id: str) -> Path:
    seq = card_id.split("_", 1)[1]
    return WORKS_DIR / work_id / "sessions" / f"session_{seq}.md"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _escape(content: str) -> str:
    return "\n".join(
        ESCAPED_MARKER if line == MARKER else line for line in content.split("\n")
    )


def _unescape(lines: list[str]) -> list[str]:
    return [MARKER if line == ESCAPED_MARKER else line for line in lines]


# [T10-2-1] 落盘互斥锁：键＝(work_id, card_id)，不同卡不互堵。
# 单进程 uvicorn 内 asyncio.Lock 足够；锁生命周期覆盖打开—写—关整段（2-2）。
_session_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _session_lock(work_id: str, card_id: str) -> asyncio.Lock:
    key = (work_id, card_id)
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock


async def append_message(work_id: str, card_id: str, role: str, content: str,
                         model: Optional[str] = None,
                         pair_id: Optional[str] = None) -> dict[str, Any]:
    """追加一条消息，返回消息字典。

    [T10-1-2/1-3] pair_id 为可选参数：传入时在 timestamp 行之后新增一行
    `pair_id: {标识}`；缺省不写该行——旧调用方不受影响。
    [T10-2-3/2-4] 加锁等待超过 1 秒记一行 warning（含 work_id/card_id/等待毫秒数）。
    该阈值只是日志触发条件，不构成任何终止判据：超时仍照常写入、不放弃、不重试。
    """
    path = _session_path(work_id, card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    header = [MARKER, f"role: {role}", f"timestamp: {ts}"]
    if pair_id:
        header.append(f"pair_id: {pair_id}")
    if model is not None:
        header.append(f"model: {model}")
    header.append("---")
    block = "\n".join(header) + "\n" + _escape(content) + "\n"
    lock = _session_lock(work_id, card_id)
    wait_started = time.monotonic()
    async with lock:
        waited_ms = (time.monotonic() - wait_started) * 1000
        if waited_ms > 1000:
            logger.warning(
                "会话写入锁等待超过 1 秒 work=%s card=%s 等待=%.0fms（照常写入）",
                work_id, card_id, waited_ms,
            )
        async with aiofiles.open(path, mode="a", encoding="utf-8") as f:
            await f.write(block)
    msg = {"role": role, "content": content, "timestamp": ts,
           "model": model, "pair_id": pair_id or ""}
    return msg


async def read_session(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """解析会话文件为消息列表。文件不存在返回空列表。"""
    path = _session_path(work_id, card_id)
    if not path.exists():
        return []
    async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
        text = await f.read()

    lines = text.split("\n")
    messages: list[dict[str, Any]] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i] != MARKER:
            i += 1
            continue
        i += 1
        header: dict[str, str] = {}
        while i < n and lines[i] != "---":
            if ": " in lines[i]:
                k, v = lines[i].split(": ", 1)
                header[k] = v
            i += 1
        i += 1
        content_lines: list[str] = []
        while i < n and lines[i] != MARKER:
            content_lines.append(lines[i])
            i += 1
        if content_lines and content_lines[-1] == "":
            content_lines = content_lines[:-1]
        messages.append({
            "role": header.get("role", ""),
            "content": "\n".join(_unescape(content_lines)),
            "timestamp": header.get("timestamp", ""),
            "model": header.get("model"),
            # [T10-1-5] 读取侧按行前缀识别（上方 header 通用解析已捕获 pair_id: 行）；
            # 缺该行时置空——旧会话文件原样读出、原内容完整（向后兼容，V7）。
            "pair_id": header.get("pair_id", ""),
        })
    return messages
