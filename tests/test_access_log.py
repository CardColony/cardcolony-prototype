# -*- coding: utf-8 -*-
"""[2026-10-25 · 治「频繁网络错误，却一点日志都没有」] 访问日志 ＋ 客户端错误落盘网

## 为什么有这张网

用户报「整个页面频繁报网络错误」。排障时发现盘上**只有** `logs/server_errors.jsonl`，
而且它**只记未捕获异常（500）**，自 2026-10-24 起没有新条目 ⇒ 服务端没在抛异常。

但「网络错误」是**连接层**失败（Chrome 的 `TypeError: Failed to fetch`）：
请求**压根没进到应用** ⇒ 服务端日志天然看不见 ⇒ 排障只能靠猜
（这次先后怀疑了释放策略、事件循环堵死，最后才测到 uvicorn 5 秒空闲断 keep-alive）。

所以补两头：① 服务端**访问日志**；② 客户端**错误上报**。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| L1 | 每个 API 请求落一行：方法/路径/状态/耗时（**status 要对**、**ms 要有**） |
| L2 | `/static/*` 静态资源**不记**（量大、无信息量） |
| L3 | 客户端上报落到 `logs/client_errors.jsonl`（**与服务端日志分文件**） |
| L4 | 日志写失败**绝不影响请求**（零阻断） |
| L5 | 写日志**不拖慢请求**（同步写文件会堵事件循环 ⇒ 走线程池） |

铁律一：日志目录重定向到 tmp_path（**不碰真实 logs/**）。
"""
from __future__ import annotations

import json

import httpx
from fastapi import FastAPI


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


def _read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


async def test_l1_access_log_records_every_api_request(sandbox, monkeypatch, tmp_path):
    """L1：每个 API 请求落一行（方法/路径/状态/耗时）。"""
    from src.api import access_log
    from src.api.routes import router

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(access_log, "LOG_DIR", log_dir)

    app = FastAPI()
    app.include_router(router)
    access_log.install_access_log(app)

    async with _client(app) as client:
        r = await client.get("/api/nav/running")
        assert r.status_code == 200

    recs = _read_jsonl(log_dir / "access_log.jsonl")
    assert recs, "访问日志没落盘"
    hit = [x for x in recs if x.get("p") == "/api/nav/running"]
    assert hit, f"没记到 /api/nav/running：{recs}"
    one = hit[-1]
    assert one["m"] == "GET", one
    assert one["st"] == 200, one
    assert isinstance(one["ms"], int) and one["ms"] >= 0, one
    assert "ts" in one and "ua" in one and "client" in one, one


async def test_l2_static_assets_are_not_logged(sandbox, monkeypatch, tmp_path):
    """L2：静态资源不记（量大、无信息量）。"""
    from src.api import access_log
    from src.api.routes import router

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(access_log, "LOG_DIR", log_dir)

    app = FastAPI()
    app.include_router(router)
    access_log.install_access_log(app)

    @app.get("/static/probe.js")
    async def _probe():
        return {"ok": True}

    async with _client(app) as client:
        await client.get("/static/probe.js")

    recs = _read_jsonl(log_dir / "access_log.jsonl")
    assert not [x for x in recs if str(x.get("p", "")).startswith("/static/")], recs


async def test_l3_client_error_is_recorded(sandbox, monkeypatch, tmp_path):
    """L3：客户端上报落到 client_errors.jsonl（与服务端日志分文件）。"""
    from src.api import access_log
    from src.api.routes import router

    log_dir = tmp_path / "logs"
    monkeypatch.setattr(access_log, "LOG_DIR", log_dir)

    app = FastAPI()
    app.include_router(router)
    access_log.install_access_log(app)

    async with _client(app) as client:
        r = await client.post("/api/client-errors", json={
            "where": "call", "url": "/api/works/work_025/cards/card_059",
            "name": "TypeError", "message": "Failed to fetch",
            "href": "http://t/work/work_025/card/card_059", "ua": "test-ua",
        })
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}

    recs = _read_jsonl(log_dir / "client_errors.jsonl")
    assert recs, "客户端错误没落盘"
    one = recs[-1]
    assert one["where"] == "call", one
    assert one["name"] == "TypeError", one
    assert one["message"] == "Failed to fetch", one
    assert one["ua"] == "test-ua", one
    assert "ts" in one, one
    # 分文件：访问日志里只有「有人 POST 了 /api/client-errors」这一条**请求行**，
    # 客户端错误的**内容**只在 client_errors.jsonl 里（不该混进访问日志的字段里）。
    access = _read_jsonl(log_dir / "access_log.jsonl")
    for x in access:
        assert "name" not in x or x.get("p") != "/api/client-errors", \
            f"客户端错误内容不该出现在访问日志里：{x}"


async def test_l4_logging_failure_never_breaks_the_request(sandbox, monkeypatch, tmp_path):
    """L4：日志写失败绝不影响请求（零阻断）。"""
    from src.api import access_log
    from src.api.routes import router

    def _boom(*a, **kw):
        raise OSError("磁盘满了（模拟）")

    monkeypatch.setattr(access_log, "append_jsonl", _boom)
    monkeypatch.setattr(access_log, "LOG_DIR", tmp_path / "logs")

    app = FastAPI()
    app.include_router(router)
    access_log.install_access_log(app)

    async with _client(app) as client:
        r = await client.get("/api/nav/running")
        assert r.status_code == 200, "日志写失败不该让请求失败"
        r2 = await client.post("/api/client-errors", json={"where": "call"})
        assert r2.status_code == 200, "日志写失败不该让上报端点失败"


async def test_l5_logging_does_not_block_the_event_loop(sandbox, monkeypatch, tmp_path):
    """L5：写日志走线程池（同步写文件会堵事件循环 ⇒ 整个服务卡住）。"""
    import inspect

    from src.api import access_log

    src = inspect.getsource(access_log.install_access_log)
    assert "asyncio.to_thread" in src, \
        "写日志必须走 asyncio.to_thread（同步写文件会堵死事件循环——那正是本轮治的病）"
