# -*- coding: utf-8 -*-
"""旧档案扫描的**按 work 合并**（[P27 · 2026-10-26]）

## 为什么

实测：一个**旧版前端**标签页每 60 秒拉一次**全量** `card-messages` ⇒ 每次都重扫
该 work 的全部会话文件 ⇒ **3~8 秒/次** ⇒ 把磁盘/线程池占住 ⇒ **整个服务被拖慢**
（实测 `nav/running` 最慢 2978ms、`nav/version` 3162ms 都是被它拖的；
另一次实测**页面 DCL 106683ms —— 106 秒**）。

前端已改（角标走轻端点 `scope=recent`），但**旧 bundle 还在跑** ⇒ 服务端也必须扛得住。

⇒ 同一 work 的重复扫描**合并**（TTL 内复用 ＋ 串行闸）⇒ 一个旧页面**拖不死**服务器。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| A1 | 同一 work 的第二次全量请求**不重扫**（TTL 内复用 ⇒ 扫描次数 1） |
| A2 | 两次响应内容**一致**（缓存不许改内容） |
| A3 | 与 inbox 的**去重**仍在（同一 msg_id 不双出） |
| A4 | `scope=recent` **不扫**旧档案（轻端点） |

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def test_a1_a2_second_full_request_does_not_rescan(sandbox, work, monkeypatch):
    """A1/A2：第二次全量请求不重扫（扫描次数 1），且内容一致。"""
    from src.api import routes
    from src.storage import card_store, session_facade

    wid = work["work_id"]
    root = work["work_card_id"]
    await card_store.create_card(wid, "合并网子卡", "用户直接创建", parent=root)

    calls = []
    real_read = session_facade.read

    async def counting_read(work_id, card_id, **kw):
        calls.append(card_id)
        return await real_read(work_id, card_id, **kw)

    monkeypatch.setattr(routes.session_facade, "read", counting_read)
    routes._ARCHIVE_CACHE.clear()

    app = FastAPI()
    app.include_router(routes.router)
    async with _client(app) as client:
        r1 = (await client.get(f"/api/works/{wid}/card-messages")).json()
        n1 = len(calls)
        r2 = (await client.get(f"/api/works/{wid}/card-messages")).json()
        n2 = len(calls)

    assert n1 > 0, "第一次应扫旧档案"
    assert n2 == n1, f"第二次不该重扫（扫描次数 {n1} → {n2}）——一个旧页面每 60 秒拉一次全量会拖死服务器"
    assert r1["items"] == r2["items"], "缓存不许改内容"
    assert r1["unread_by_card"] == r2["unread_by_card"]


async def test_a3_dedup_with_inbox_kept(sandbox, work, monkeypatch):
    """A3：与 inbox 的去重仍在（同一 msg_id 不双出）。"""
    from src.api import routes

    wid = work["work_id"]
    app = FastAPI()
    app.include_router(routes.router)
    routes._ARCHIVE_CACHE.clear()
    async with _client(app) as client:
        data = (await client.get(f"/api/works/{wid}/card-messages")).json()
    ids = [i["id"] for i in data["items"]]
    assert len(ids) == len(set(ids)), f"出现重复 msg_id：{ids[:10]}"


async def test_a4_scope_recent_does_not_scan_old_archive(sandbox, work, monkeypatch):
    """A4：scope=recent 不扫旧档案（轻端点）。"""
    from src.api import routes
    from src.storage import card_store, session_facade

    wid = work["work_id"]
    root = work["work_card_id"]
    await card_store.create_card(wid, "轻端点网子卡", "用户直接创建", parent=root)

    calls = []
    real_read = session_facade.read

    async def counting_read(work_id, card_id, **kw):
        calls.append(card_id)
        return await real_read(work_id, card_id, **kw)

    monkeypatch.setattr(routes.session_facade, "read", counting_read)
    app = FastAPI()
    app.include_router(routes.router)
    async with _client(app) as client:
        await client.get(f"/api/works/{wid}/card-messages?scope=recent")
    assert not calls, f"scope=recent 不该扫旧档案（却扫了 {len(calls)} 次）"
