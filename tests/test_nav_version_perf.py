# -*- coding: utf-8 -*-
"""[P22 · 治「页面卡顿」] `/api/nav/version` 的指纹必须**并发**算

## 为什么有这张网

访问日志（`logs/access_log.jsonl`，本轮新加的）第一眼就抓到：
`/api/nav/version` 最慢跑到 **4387ms**、2842ms、1253ms，而左栏**每 60 秒**探一次
⇒ 一次就能顶掉好几秒（页面看着像死了）。

拆开量过（`_probe_nav_version_cost.py`）：
- 35 个 work；
- `sessions/` 目录扫描：**2ms**（296 个 .md）——不是瓶颈；
- 大头是 `card_db.read_rev` 的**逐个开 SQLite 库**：串行 **358ms** ⇒ 并发 **19ms**。
- 原来对每个 work **串行** `await asyncio.to_thread(...)` ⇒ 磁盘一忙就线性叠加
  （空载 68ms、忙时 4387ms）。

⇒ 改法：并发算（`asyncio.gather`）。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| V1 | 指纹**并发**算（实测并发度 > 1）——不许退回逐个串行 await |
| V2 | 响应结构不变：`{"version", "by_work"}`，`by_work` 覆盖全部 work |
| V3 | 指纹内容不变（并发不改变结果：同样的输入 ⇒ 同样的哈希） |
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import time

import httpx
from fastapi import FastAPI


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def _mk_works(n: int) -> list[str]:
    """在沙箱里造 n 个 work（端点只认「含 work.json 的目录」）。"""
    from src.storage import work_store
    wids = []
    for i in range(n):
        w = await work_store.create_work(f"并发指纹验收{i}")
        assert "error" not in w, w
        wids.append(w["work_id"])
    return wids


async def test_v1_fingerprints_are_computed_concurrently(sandbox, monkeypatch):
    """V1：指纹并发算（并发度 > 1）——不许退回逐个串行 await。"""
    from src.api import routes

    wids = await _mk_works(3)
    lock = threading.Lock()
    state = {"cur": 0, "max": 0}

    def _slow_fp(wid: str) -> str:
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.05)          # 放大串行/并发的差别
        with lock:
            state["cur"] -= 1
        return "fp-" + wid

    monkeypatch.setattr(routes, "_work_fingerprint_sync", _slow_fp)

    app = FastAPI()
    app.include_router(routes.router)
    async with _client(app) as client:
        r = await client.get("/api/nav/version")
    assert r.status_code == 200, r.text
    assert state["max"] > 1, \
        (f"指纹应当并发算（实测最大并发度 {state['max']}）"
         f"——串行 await 会把每个 work 的耗时线性叠加")
    assert set(r.json()["by_work"]) >= set(wids), r.json()


async def test_v2_response_shape_unchanged(sandbox, monkeypatch):
    """V2：响应结构不变（{"version","by_work"}）。"""
    from src.api import routes

    wids = await _mk_works(2)
    app = FastAPI()
    app.include_router(routes.router)
    async with _client(app) as client:
        r = await client.get("/api/nav/version")
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data.keys()) == {"version", "by_work"}, data
    assert isinstance(data["by_work"], dict) and data["by_work"], data
    assert set(wids) <= set(data["by_work"]), data
    # version 必须是各 work 指纹的复合哈希（确定性）
    h = hashlib.sha256()
    for wid in sorted(data["by_work"]):
        h.update(f"{wid}|{data['by_work'][wid]}\n".encode("utf-8"))
    assert data["version"] == h.hexdigest(), "version 必须仍是 by_work 的复合哈希"


async def test_v3_concurrency_does_not_change_the_result(sandbox, monkeypatch):
    """V3：并发不改变结果（同样的输入 ⇒ 同样的哈希）。"""
    from src.api import routes

    await _mk_works(2)
    app = FastAPI()
    app.include_router(routes.router)
    async with _client(app) as client:
        a = (await client.get("/api/nav/version")).json()
        b = (await client.get("/api/nav/version")).json()
    assert a == b, "同一时刻两次探测结果必须一致（并发没引入不确定性）"


def test_v4_source_must_not_regress_to_serial_await(sandbox):
    """V1 的源码级护栏：源码级断言（改回来就红）。"""
    from src.api import routes

    src = inspect.getsource(routes.get_nav_version)
    assert "asyncio.gather" in src, \
        "指纹必须并发算（asyncio.gather）——串行 await 会把 35 个 work 线性叠加（实测 4387ms）"
