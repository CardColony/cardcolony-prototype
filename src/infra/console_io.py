# -*- coding: utf-8 -*-
"""控制台 IO 编码护栏（Windows 的 GBK 坑）。

**实测（2026-10-25）**：Windows 上 ``sys.stdout`` 默认是 **GBK 且 strict**，
``sys.stderr`` 是 GBK 且 **backslashreplace**。

⇒ 任何含「⇒」「→」这类字符的 ``print()``（走 stdout）会**直接抛**
``UnicodeEncodeError: 'gbk' codec can't encode character '\\u21d2'``；
``logger`` 走 stderr ⇒ 不炸，但会被 backslashreplace 写成 ``\\u21d2``（日志变乱码）。

**现场**：验收实例把 stdout 接到管道上时，某个异常处理分支自己 print 了一条含
「⇒」的中文 ⇒ print 自己又炸 ⇒ 报出来的错是「编码错误」而不是真正的错
⇒ **真实原因被编码问题吃掉了**。

**修法**：把 stdout/stderr 统一切到 UTF-8（``replace`` 兜底）。控制台本来就该是
UTF-8（PEP 528），重定向到文件/管道时也不再炸。切不动（流被换成别的对象）⇒
跳过，**不阻断启动**。

放在 ``src/infra`` 而不是各自入口里，是因为有三个入口都要它：``main.py``（生产）
和两个测试实例脚本——后者**故意不 import main**（见脚本头部说明）⇒ 只改 main.py
覆盖不到沙箱。
"""
from __future__ import annotations

import sys


def ensure_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8（``replace`` 兜底）。幂等，可重复调用。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 不是 TextIO（被换成别的对象）⇒ 不处理
            pass
