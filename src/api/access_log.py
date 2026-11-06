# -*- coding: utf-8 -*-
"""访问日志 ＋ 客户端错误落盘（2026-10-25 · 「页面频繁报网络错误，却一点日志都没有」）

## 为什么要有这个

排障时发现盘上**只有** `logs/server_errors.jsonl`，而且它**只记未捕获异常（500）**，
自 2026-10-24 09:31 起**没有新条目** ⇒ 服务端没在抛异常。

但用户看到的「网络错误」是 `static/esm/api.js` 的 `call()` 弹的
「网络错误：Failed to fetch」——**连接层**失败：请求**压根没进到应用**
⇒ 服务端日志天然看不见 ⇒ 排障只能靠猜（这次就是这么猜的：先怀疑释放策略、
再怀疑事件循环堵、最后才测到 keep-alive 断连）。

所以这里补两头：

1. **访问日志**（`logs/access_log.jsonl`）：谁在什么时候调了哪个端点、
   返回什么状态、耗时多少、UA 是谁。慢请求（实测 `/api/nav/version` 有跑到
   **2.4 秒**的）与 4xx/5xx 一眼可见。
2. **客户端错误上报**（`logs/client_errors.jsonl`）：连接层失败由前端
   `api.js` 主动报上来（POST `/api/client-errors`），落盘留证。

## 设计约束

- **不拖慢请求**：同步写文件会阻塞事件循环 ⇒ 走线程池（`asyncio.to_thread`）。
- **不无限长盘**：单文件超过 `MAX_BYTES` 就轮转（保留 1 份历史）。
- **不刷屏**：`/static/*` 静态资源不记（量大且无信息量）；SSE 长连接只记「开/关」。
- **零阻断**：写日志失败绝不影响请求（全部 try/except 包住）。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI

#: 日志根目录＝**仓库根**的 logs/（本文件在 src/api/ ⇒ 往上两级）。
#: （写成 parent.parent 会落到 src/logs —— 目录会被顺带创建出来，日志却不在该在的地方。）
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"

#: 单个日志文件上限（超过就轮转；留 1 份历史）。
MAX_BYTES = 8 * 1024 * 1024

#: 不记访问日志的路径前缀（静态资源量大、无信息量）。
_SKIP_PREFIX = ("/static/", "/favicon")

#: SSE 长连接端点（耗时是「开着多久」而不是「处理多久」⇒ 单独标记）。
_SSE_MARKERS = ("/stream", "/tokens", "/events")

#: 写日志的**专用**单线程执行器（2026-10-25）。
#: 为什么不共用默认线程池：默认池会被业务占满（实测 `/api/nav/version` 一次并发
#: 35 个指纹任务，SQLite 开库又可能等锁 ⇒ 整池被占）⇒ 日志写入排在后面，
#: 一次客户端错误上报被拖到 **6333ms**（访问日志实测）。
#: ⇒ 日志自己占一个线程：① 永远不等业务；② 单写者 ⇒ 天然不交错。
_LOG_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="access-log")

#: 进程内写锁（跨线程 append 交错会写坏行——实测 596 行里坏过 1 行）。
_WRITE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _rotate_if_needed(path: Path) -> None:
    """超过上限就轮转（保留 1 份历史：path → path.1）。"""
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            old = path.with_suffix(path.suffix + ".1")
            try:
                if old.exists():
                    old.unlink()
            except OSError:
                pass
            os.replace(path, old)
    except OSError:
        pass


def append_jsonl(name: str, rec: dict[str, Any]) -> None:
    """追加一条 JSONL 记录（**同步**；调用方负责放到线程里跑）。

    整个「开文件→写一行→关文件」在**写锁内**完成：并发 append 会交错写坏行
    （实测 596 行里坏过 1 行，机器读不了）。
    """
    try:
        with _WRITE_LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / name
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 日志失败绝不影响主流程
        pass


async def write_log(name: str, rec: dict[str, Any]) -> None:
    """把一条日志写出去（**走专用线程**，不排在业务的默认线程池后面）。"""
    try:
        await asyncio.wrap_future(_LOG_EXECUTOR.submit(append_jsonl, name, rec))
    except Exception:  # noqa: BLE001 日志失败绝不影响主流程
        pass


def log_client_error(rec: dict[str, Any]) -> None:
    """客户端上报的连接层错误 → `logs/client_errors.jsonl`。

    **同步**函数（调用方负责放到线程里跑：见 routes.py 的 `/api/client-errors`
    ——async 端点里同步写文件会堵事件循环，那正是本轮在治的病）。
    内部再兜一层 try/except ⇒ 写日志失败绝不影响请求。
    """
    try:
        out = {"ts": _now_iso()}
        out.update(rec)
        append_jsonl("client_errors.jsonl", out)
    except Exception:  # noqa: BLE001 日志失败绝不影响主流程
        pass


async def write_client_error(rec: dict[str, Any]) -> None:
    """客户端错误落盘（**走专用线程**：不等业务线程池——实测被拖到 6333ms）。"""
    out = {"ts": _now_iso()}
    out.update(rec)
    await write_log("client_errors.jsonl", out)


def install_access_log(app: FastAPI) -> None:
    """给 app 挂访问日志中间件（重复调用无害）。"""

    @app.middleware("http")
    async def _access_log(request, call_next):  # noqa: ANN001
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIX):
            return await call_next(request)
        t0 = time.perf_counter()
        status = 0
        err = ""
        try:
            resp = await call_next(request)
            status = int(getattr(resp, "status_code", 0) or 0)
            return resp
        except Exception as exc:  # noqa: BLE001 记下来再抛（不吞异常）
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ms = int((time.perf_counter() - t0) * 1000)
            sse = any(m in path for m in _SSE_MARKERS)
            rec = {
                "ts": _now_iso(),
                "m": request.method,
                "p": path,
                "q": str(request.url.query or ""),
                "st": status,
                "ms": ms,
                "sse": sse,
                "ua": str(request.headers.get("user-agent") or ""),
                "client": (request.client.host if request.client else ""),
                "ref": str(request.headers.get("referer") or ""),
            }
            if err:
                rec["err"] = err
            # 走**专用**写线程（不用默认池：默认池会被业务占满 ⇒ 日志排队 ⇒
            # 实测一次客户端错误上报被拖到 6333ms）
            await write_log("access_log.jsonl", rec)
