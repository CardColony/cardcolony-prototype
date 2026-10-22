# -*- coding: utf-8 -*-
"""MiniMax Code（mavis）鉴权租约桥。

背景：本地安装的 MiniMax Code 是一个完整 agent 产品，它的模型调用走
``https://agent.minimax.cn/mavis/api/v1/llm/v1``（Anthropic Messages 协议），
鉴权**不认** open-platform 的 sk-key，只认它 App 内置 broker 铸出的短期令牌。

broker 形态（逆自 ``resources/mcode-tools/cli.mjs`` 内联的
``@mavis/oauth-lease-protocol@0.1.0-beta.0``，2026-10-22 实测复现）：
    - Windows 命名管道 ``\\\\.\\pipe\\mcode-auth-lease-<16 位小写 hex>``
      （后缀每次 App 启动会变，靠枚举发现）；
    - 能力文件 ``~/.minimax/run/mcode-auth-lease-v1.cap``（43 位 ``[A-Za-z0-9_-]``）；
    - 帧 ＝ **4 字节大端长度前缀** ＋ UTF-8 JSON；
    - 请求 ``{"version":1,"requestId":<UUID>,"capability":<cap 全文>,
      "method":"lease","minValidityMs":<60_000..300_000>}``；
    - 成功响应 ``result.accessToken``／``result.expiresAtMs``／``result.generation``。

铁律（与本仓拆掉模型 fallback 的同款哲学）：
    - App 没在跑／broker 不应允 ⇒ **响亮失败**（``LlmError`` AUTH），不静默、不兜底；
    - 令牌按 ``expiresAtMs`` 驱动续期，过期前 ``_MIN_VALIDITY_MS`` 内必换新。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .llm.errors import LlmError

__all__ = ["MavisLeaseClient", "find_broker_pipe", "mint_lease_token"]

#: 协议常量（与 @mavis/oauth-lease-protocol@0.1.0-beta.0 逐字对齐）
_PROTOCOL_VERSION = 1
_CAPABILITY_FILE = "mcode-auth-lease-v1.cap"
_PIPE_PREFIX = "mcode-auth-lease-"
_MIN_VALIDITY_MS = 240_000  # 请求令牌时要求的最小剩余有效期（协议上限 300_000）
_REFRESH_MARGIN_MS = 120_000  # 缓存令牌剩余有效期低于此值即提前续期

#: 能力文件候选位置（App 不同版本可能挪窝，逐个试）
_CAPABILITY_PATHS = (
    Path.home() / ".minimax" / "run" / _CAPABILITY_FILE,
    Path.home() / ".minimax" / "v2" / "run" / _CAPABILITY_FILE,
)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = -1


def find_broker_pipe() -> str:
    """枚举 ``\\\\.\\pipe\\`` 下的 broker 管道（后缀随 App 启动变化）。

    App 没在跑时抛 ``LlmError(AUTH)``——调用方应把「座位不可用」响亮报给用户，
    与模型 fallback 拆除后的失败哲学一致：不静默降级。
    """
    ps = (
        "Get-ChildItem //./pipe/ | "
        f"Where-Object {{ $_.Name -like '{_PIPE_PREFIX}*' }} | "
        "Select-Object -ExpandProperty Name"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        raise LlmError(f"枚举 mavis broker 管道失败: {exc}", "AUTH") from exc
    names = [n.strip() for n in out.splitlines() if n.strip()]
    if not names:
        raise LlmError(
            "未找到 MiniMax Code 的鉴权 broker（App 未运行？）——mavis 座位不可用",
            "AUTH",
        )
    suffix = names[0].replace("\\??\\pipe\\", "").replace("\\\\.\\pipe\\", "")
    if len(suffix) != len(_PIPE_PREFIX) + 16:
        raise LlmError(f"broker 管道名异常: {suffix}", "AUTH")
    return "\\\\.\\pipe\\" + suffix


def _load_capability() -> str:
    for path in _CAPABILITY_PATHS:
        try:
            secret = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if secret:
            return secret
    raise LlmError(
        f"未找到 mavis 能力文件（{' / '.join(str(p) for p in _CAPABILITY_PATHS)}）",
        "AUTH",
    )


def _pipe_roundtrip(pipe: str, payload: dict[str, Any]) -> dict[str, Any]:
    """一次命名管道往返：4 字节大端长度帧 ＋ JSON。"""
    handle = _kernel32.CreateFileW(
        pipe, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise LlmError(
            f"连接 mavis broker 管道失败: {pipe}（WinError {ctypes.get_last_error()}）", "AUTH"
        )
    try:
        body = json.dumps(payload).encode("utf-8")
        frame = struct.pack(">I", len(body)) + body  # 大端前缀（协议 writeUInt32BE）
        written = ctypes.c_ulong(0)
        if not _kernel32.WriteFile(handle, frame, len(frame), ctypes.byref(written), None):
            raise LlmError(
                f"写入 mavis broker 失败（WinError {ctypes.get_last_error()}）", "AUTH"
            )
        length_bytes = (ctypes.c_ubyte * 4)()
        read = ctypes.c_ulong(0)
        if not _kernel32.ReadFile(handle, length_bytes, 4, ctypes.byref(read), None):
            raise LlmError(
                f"读取 mavis broker 响应失败（WinError {ctypes.get_last_error()}）", "AUTH"
            )
        size = struct.unpack(">I", bytes(length_bytes))[0]
        if size <= 0 or size > 64 * 1024:
            raise LlmError(f"mavis broker 响应帧长异常: {size}", "AUTH")
        buffer = (ctypes.c_ubyte * size)()
        if not _kernel32.ReadFile(handle, buffer, size, ctypes.byref(read), None):
            raise LlmError(
                f"读取 mavis broker 响应体失败（WinError {ctypes.get_last_error()}）", "AUTH"
            )
        return json.loads(bytes(buffer).decode("utf-8"))
    finally:
        _kernel32.CloseHandle(handle)


def mint_lease_token(min_validity_ms: int = _MIN_VALIDITY_MS) -> dict[str, Any]:
    """向 broker 铸一个新令牌（同步，底层是本地管道，延迟毫秒级）。

    返回 ``{"accessToken","expiresAtMs","generation","audience","scopes"}``。
    """
    request = {
        "version": _PROTOCOL_VERSION,
        "requestId": str(uuid.uuid4()),
        "capability": _load_capability(),
        "method": "lease",
        "minValidityMs": min_validity_ms,
    }
    response = _pipe_roundtrip(find_broker_pipe(), request)
    if not response.get("ok"):
        error = response.get("error") or {}
        raise LlmError(
            f"mavis broker 拒发令牌: {error.get('code') or response}", "AUTH"
        )
    result = response.get("result") or {}
    token = result.get("accessToken")
    if not token:
        raise LlmError("mavis broker 响应缺少 accessToken", "AUTH")
    return result


class MavisLeaseClient:
    """带缓存与自动续期的令牌提供者（进程内单例）。

    续期策略：剩余有效期 < ``_REFRESH_MARGIN_MS`` 即提前换新（``minValidityMs``
    取协议允许的上限附近，保证续出的令牌尽量长寿）。并发调用共用一把 asyncio 锁，
    避免惊群。
    """

    def __init__(self, minter: Optional[Callable[[], dict[str, Any]]] = None) -> None:
        self._minter = minter or mint_lease_token
        self._token: Optional[str] = None
        self._expires_at_ms: float = 0.0
        self._generation: int = 0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """取一个必然在有效期内（≥ ``_REFRESH_MARGIN_MS``）的令牌。"""
        async with self._lock:
            now_ms = time.time() * 1000
            if self._token and now_ms + _REFRESH_MARGIN_MS < self._expires_at_ms:
                return self._token
            result = self._minter()
            self._token = str(result["accessToken"])
            self._expires_at_ms = float(result.get("expiresAtMs") or 0)
            self._generation = int(result.get("generation") or 0)
            if self._expires_at_ms <= now_ms:
                raise LlmError("mavis broker 铸出的令牌已过期", "AUTH")
            return self._token

    def status(self) -> dict[str, Any]:
        now_ms = time.time() * 1000
        return {
            "has_token": self._token is not None,
            "generation": self._generation,
            "expires_in_ms": max(0, int(self._expires_at_ms - now_ms)),
        }


#: 进程内共享实例（网关经它取令牌；测试可注入替身 minter）
_default_client: Optional[MavisLeaseClient] = None


def get_default_client() -> MavisLeaseClient:
    global _default_client
    if _default_client is None:
        _default_client = MavisLeaseClient()
    return _default_client
