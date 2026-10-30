# -*- coding: utf-8 -*-
"""读**别的进程**的环境变量（Windows PEB 读法）。

**为什么需要它**：桌面端网关的 REST 端点（``/api/v1/status`` 等）要口令，
而口令只存在于**桌面端进程的环境**里（``CODEBUDDY_GATEWAY_PASSWORD``）——
磁盘上没有、也不在我们的 env 里。

**只读、只在本机、只取我们需要的名字**。非 Windows 直接抛
（⇒ 读不到网关状态 ⇒ 按"不知道"处理，不挡路）。

用法::

    from .winenv import read_proc_env
    env = read_proc_env(31628)
    pwd = env.get("CODEBUDDY_GATEWAY_PASSWORD", "")
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import byref, c_size_t, c_ubyte, c_void_p
from typing import Optional

#: 只取这些前缀的变量（避免把整个 env 拉进内存）。
ALLOW_PREFIXES = ("CODEBUDDY_", "WORKBUDDY_", "ACC_PRODUCT")


class NotSupportedError(RuntimeError):
    """当前平台不支持读别的进程的 env。"""


class _PBI(ctypes.Structure):
    _fields_ = [
        ("Reserved1", c_void_p),
        ("PebBaseAddress", c_void_p),
        ("Reserved2_0", c_void_p),
        ("Reserved2_1", c_void_p),
        ("UniqueProcessId", c_void_p),
        ("Reserved3", c_void_p),
    ]


def read_proc_env(pid: int) -> dict[str, str]:
    """读进程 ``pid`` 的环境变量（**只保留** :data:`ALLOW_PREFIXES` 开头的）。"""
    if sys.platform != "win32":
        raise NotSupportedError("只有 Windows 支持读别的进程的 env")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return {}
    try:
        pbi = _PBI()
        status = ntdll.NtQueryInformationProcess(
            h, 0, byref(pbi), ctypes.sizeof(pbi), byref(ctypes.c_ubyte()))
        if status != 0:
            return {}
        peb = pbi.PebBaseAddress

        def _read(addr: int, size: int) -> bytes:
            buf = (ctypes.c_ubyte * size)()
            n = c_size_t()
            if not kernel32.ReadProcessMemory(h, c_void_p(addr), buf, size,
                                             byref(n)):
                return b""
            return bytes(buf[:n.value])

        proc_params = int.from_bytes(_read(peb + 0x20, 8), "little")
        env_addr = int.from_bytes(_read(proc_params + 0x80, 8), "little")
        env_size = int.from_bytes(_read(proc_params + 0x3F0, 8), "little")
        if env_addr == 0 or not (0 < env_size <= 4 * 1024 * 1024):
            return {}
        blob = _read(env_addr, env_size)
        out: dict[str, str] = {}
        for part in blob.decode("utf-16-le", "ignore").split("\0"):
            i = part.find("=")
            if i <= 0:
                continue
            name = part[:i]
            if name.startswith(ALLOW_PREFIXES):
                out[name] = part[i + 1:]
        return out
    finally:
        kernel32.CloseHandle(h)


def gateway_password(pid: Optional[int]) -> str:
    """取某进程的 ``CODEBUDDY_GATEWAY_PASSWORD``（取不到 ⇒ 空串）。"""
    if not pid:
        return ""
    try:
        return read_proc_env(int(pid)).get("CODEBUDDY_GATEWAY_PASSWORD", "")
    except Exception:  # noqa: BLE001 读不到就当没有口令
        return ""
