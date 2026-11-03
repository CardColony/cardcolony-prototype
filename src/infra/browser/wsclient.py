# -*- coding: utf-8 -*-
"""极简 WebSocket 客户端（RFC 6455，纯标准库）。

只覆盖 CDP 需要的子集：ws://（本机回环，无 TLS）、文本帧收发、
ping/pong、close。客户端发送帧按规范必须 mask。大消息（Chrome
序列化结果可能到几百 KB）走 TCP 流收满为止；分片帧会拼起来。
"""

from __future__ import annotations

import base64
import os
import socket
import struct
from urllib.parse import urlsplit


class WSError(Exception):
    pass


class WSClosed(WSError):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WSClosed("connection closed while reading")
        buf += chunk
    return buf


class WSConnection:
    """一条已握手的 WebSocket 连接（阻塞式，文本帧进出）。"""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 15.0) -> "WSConnection":
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise WSError(f"只支持 ws://，拿到: {url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: chat\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        # 读响应头（到 \r\n\r\n）。CDP 的响应很小，一段就够。
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSError("握手响应中断")
            resp += chunk
            if len(resp) > 65536:
                raise WSError("握手响应过大")
        head = resp.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
        status_line = head.split("\r\n", 1)[0]
        if "101" not in status_line.split(" ")[:3]:
            raise WSError(f"握手失败: {status_line.strip()}")
        conn = cls(sock)
        # 握手段之外可能粘了帧数据——本实现 recv 按帧读，粘包字节
        # 已在 sock 缓冲区里，无妨（socket 层自己缓存）。
        return conn

    def send_text(self, payload: str) -> None:
        if self._closed:
            raise WSClosed("send on closed connection")
        data = payload.encode("utf-8")
        header = bytearray()
        header.append(0x81)  # FIN + text
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        try:
            self._sock.sendall(bytes(header) + masked)
        except OSError as exc:
            self._closed = True
            raise WSClosed(str(exc)) from exc

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_message(self, timeout: float | None = None) -> str:
        """收一条完整消息（拼接分片帧；自动回 ping；遇 close 抛 WSClosed）。"""
        if self._closed:
            raise WSClosed("recv on closed connection")
        if timeout is not None:
            self._sock.settimeout(timeout)
        fragments: list[bytes] = []
        while True:
            try:
                b1, b2 = _recv_exact(self._sock, 2)
            except socket.timeout:
                raise  # 探测性超时不是断连（pump 靠它判空），别杀连接
            except (OSError, WSClosed) as exc:
                self._closed = True
                if isinstance(exc, WSClosed):
                    raise
                raise WSClosed(str(exc)) from exc
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", _recv_exact(self._sock, 2))
            elif length == 127:
                (length,) = struct.unpack(">Q", _recv_exact(self._sock, 8))
            mask = _recv_exact(self._sock, 4) if masked else b""
            payload = _recv_exact(self._sock, length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:  # ping
                try:
                    self._send_frame(0xA, payload)
                except OSError:
                    pass
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                self._closed = True
                raise WSClosed("received close frame")
            # text(1) / binary(2) / continuation(0)
            fragments.append(payload)
            if fin:
                data = b"".join(fragments)
                return data.decode("utf-8", "replace")

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        finally:
            self._closed = True
