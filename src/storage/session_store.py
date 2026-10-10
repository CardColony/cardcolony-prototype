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
            # [T12-1-1] 会话块序号读时现算（解析循环天然可得，0 起）：凝练水位
            # mark 的锚点。存储格式零改动（header 不落盘 seq 字段）、不新增
            # 存储字段——仅本内存字典暴露（既有消费方均按键取用，不受增键影响）。
            "seq": len(messages),
        })
    return messages


# ============================================================
# [T17-B2] 会话末条时间戳（导航活跃度专用）
# ============================================================

def _last_ts_from_chunk(chunk: str) -> tuple[bool, str]:
    """取 chunk 内最后一个完整会话块 header 里的 timestamp 行。

    块头定位走行锚定 "\\n---MSG---\\n"（含文件头恰为 MARKER 的特例）：
    正文里被转义的 "\\---MSG---" 与行内含连字符的普通文本都不会误命中。
    返回 (是否找到块头, timestamp 或 "")——找到块头即视为终局：
    窗口延伸到文件尾，比它更晚的块头必然也在窗口内。
    """
    idx = chunk.rfind("\n" + MARKER + "\n")
    if idx >= 0:
        body_start = idx + 1 + len(MARKER) + 1
    elif chunk.startswith(MARKER + "\n"):
        body_start = len(MARKER) + 1
    else:
        return False, ""
    for line in chunk[body_start:].split("\n"):
        if line == "---":
            break
        if line.startswith("timestamp: "):
            return True, line[len("timestamp: "):].strip()
    return True, ""


def last_ts(work_id: str, card_id: str) -> str:
    """取该卡会话文件末条消息的 timestamp（同步、尾部反向扫描）。

    [T17-B2] 供导航活跃度现算用，禁止改走 read_session() 全量解析
    （R-2：86KB×N 条会话全量解析会拖垮导航）。
    [统一读入口 2026-10-10] R-2 禁令随 session_facade 统一读入口作废
    （方案 4.2/T072）：本函数保留为门面 V1 路径的快路径实现（语义不变，
    调用面收进门面；消费方切换在 S6）。从文件尾按窗口反向扫描
    （起点 64KB，找不到块头则 ×4 扩窗直至全文件），取最后一个
    "---MSG---" 块 header 的 "timestamp: " 行。文件不存在/读失败返回 ""。
    """
    path = _session_path(work_id, card_id)
    if not path.exists():
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            window = 65536
            while True:
                read_len = min(size, window)
                f.seek(size - read_len)
                chunk = f.read(read_len).decode("utf-8", errors="replace")
                # 行尾归一化：存量会话有整文件 CRLF 的（文本模式 read_session
                # 靠通用换行翻译无感），二进制扫描必须先归一，行锚才咬得住
                chunk = chunk.replace("\r\n", "\n")
                # 行对齐：窗口不从文件头起时，丢弃首个不完整行
                if read_len < size:
                    nl = chunk.find("\n")
                    if nl < 0:
                        if window >= size:
                            return ""
                        window *= 4
                        continue
                    chunk = chunk[nl + 1:]
                found, ts = _last_ts_from_chunk(chunk)
                if found:
                    return ts
                if window >= size:
                    return ""
                window *= 4
    except OSError as exc:
        logger.warning("last_ts 尾部扫描失败 work=%s card=%s: %s", work_id, card_id, exc)
        return ""
