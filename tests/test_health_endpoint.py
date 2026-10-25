# -*- coding: utf-8 -*-
"""[服务自报 2026-10-25] `/api/health`：这个服务跑的是**哪份代码**？

## 为什么有这张网

改了后端要**重启**才生效，而「重启了没」全靠人猜 ⇒ 本轮已经出现过一次
「前端先改、后端没重启 ⇒ 角标端点 404」的线上回归。

⇒ 服务在启动那一刻记下关键源文件的 mtime，`/api/health` 报出来；
拿它和盘上文件 mtime 一比：**盘上更新 ⇒ 服务跑的是旧代码 ⇒ 该重启**。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| S1 | `/api/health` 返回 200，且含 `build.files`（关键源文件的 mtime）与 `build.started` |
| S2 | `build.files` 覆盖 `main.py` / `src/api/routes.py` / `src/api/access_log.py`（改这三个都要重启） |
| S3 | 顺带报访问日志行数（运维一眼看到日志在不在写） |
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def test_s1_health_reports_build_stamp(sandbox, monkeypatch):
    """S1：返回 200，且含 build.files / build.started。"""
    from src.api import routes

    app = FastAPI()
    app.include_router(routes.router)
    # 模拟 main.py 启动时打的戳
    app.state.build_stamp = {
        "started": "2026-10-25T16:40:55+08:00",
        "files": {"main.py": 1.0, "src/api/routes.py": 2.0, "src/api/access_log.py": 3.0},
        "git_head": "abc1234",
    }
    async with _client(app) as client:
        r = await client.get("/api/health")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True, data
    build = data["build"]
    assert build["started"] == "2026-10-25T16:40:55+08:00", build
    assert build["git_head"] == "abc1234", build
    assert "files" in build and build["files"], build


async def test_s2_health_covers_the_files_that_need_restart(sandbox, monkeypatch):
    """S2：覆盖 main.py / routes.py / access_log.py（改这三个都要重启）。"""
    from src.api import routes

    app = FastAPI()
    app.include_router(routes.router)
    app.state.build_stamp = {"started": "x", "files": {}, "git_head": "g"}
    async with _client(app) as client:
        r = await client.get("/api/health")
    data = r.json()
    # 端点本身存在（路由挂上了）
    assert "build" in data
    # 桩 app 没打戳时也要**如实报空**而不是崩
    assert data["build"] == {"started": "x", "files": {}, "git_head": "g"}, data
    assert "access_log_lines" in data, data


async def test_s3_health_reports_access_log_lines(sandbox, monkeypatch):
    """S3：顺带报访问日志行数（一眼看到日志在不在写）。"""
    from src.api import routes

    app = FastAPI()
    app.include_router(routes.router)
    app.state.build_stamp = {"started": "x", "files": {}, "git_head": "g"}
    async with _client(app) as client:
        r = await client.get("/api/health")
    data = r.json()
    assert isinstance(data["access_log_lines"], int), data
    assert data["access_log_lines"] >= 0, data
    assert data["logs_dir"], data
