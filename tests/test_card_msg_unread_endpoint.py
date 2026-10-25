# -*- coding: utf-8 -*-
"""[P21 · 治「切页卡死」] 未读角标轻端点 `/card-messages/unread` 的验收网。

## 为什么有这张网

左栏未读角标**每 60 秒**轮询一次（`esm/nav_system.js` 的 `refreshBadge`）。
原来它拉的是**全量**端点 `/card-messages`——而那个端点里有一份**旧档案扫描**
（每张卡读一份整会话档案）。work_025 有 44 张卡 ⇒ 实测 **1.7~2.1s**，
等于**每分钟一次**的后台负载。

只算角标其实**不需要**旧档案：`unread_by_card` 只看收件箱（inbox）。
所以新开了 `/card-messages/unread`：不扫旧档案 ⇒ 毫秒级。

## 网里钉的不变量

| 编号 | 不变量 |
|---|---|
| U1 | 口径逐字相同：新端点的 `unread_by_card` 与全量端点**完全一致**（同一份实现，不另写一套） |
| U2 | **不许扫旧档案**：只算未读时不得调用 `session_facade.read`（旧档案＝整会话档案读） |
| U3 | 响应形状：只有 `unread_by_card` 一个键（**不含** items） |
| U4 | 全量端点**仍保留** `items`（通知面板与画布通信线要用）——不许把老端点改成轻量版 |
| U5 | 水位语义没被改：seq ≤ 水位的算已读、> 水位的算未读 |

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI


def _client():
    from src.api.routes import router
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def _mkcard(wid: str, title: str, parent: str) -> str:
    from src.storage import card_store
    r = await card_store.create_card(wid, title, "用户直接创建", parent=parent)
    assert "error" not in r, r
    for cid, meta in card_store.iter_card_metas(wid).items():
        if meta.get("title") == title:
            return cid
    raise AssertionError(f"找不到卡: {title}")


async def _send(wid: str, cid: str, msg_id: str, from_card: str) -> int:
    from src.storage import card_db
    return await card_db.append_inbox(
        wid, msg_id=msg_id, target_card_id=cid, from_card=from_card,
        type="msg_notify", content="[[card-msg from=" + from_card + "]]\n正文",
    )


async def _set_seen(wid: str, cid: str, seq: int) -> None:
    from src.storage import card_db
    await card_db.run_write(
        wid, lambda conn: card_db.set_msg_seen_seq(conn, wid, cid, seq))


async def _fixture(sandbox, work, monkeypatch):
    """造两张卡 + 三条信：A 收两条（第一条已读）、B 收一条（未读）。"""
    from src.storage import activity
    monkeypatch.setattr(activity, "WORKS_DIR", sandbox.works)
    wid, root = work["work_id"], work["work_card_id"]
    cid_a = await _mkcard(wid, "未读验收卡A", root)
    cid_b = await _mkcard(wid, "未读验收卡B", root)
    seq_a1 = await _send(wid, cid_a, "m-a1", root)
    seq_a2 = await _send(wid, cid_a, "m-a2", root)
    seq_b1 = await _send(wid, cid_b, "m-b1", root)
    # A 的读取水位推过第一条 ⇒ 第一条算已读、第二条算未读
    await _set_seen(wid, cid_a, seq_a1)
    return wid, cid_a, cid_b, (seq_a1, seq_a2, seq_b1)


async def test_u1_unread_by_card_matches_full_endpoint(sandbox, work, monkeypatch):
    """U1：新旧两个端点的 unread_by_card **完全一致**。"""
    wid, cid_a, cid_b, _ = await _fixture(sandbox, work, monkeypatch)
    async with _client() as client:
        full = (await client.get(f"/api/works/{wid}/card-messages")).json()
        light = (await client.get(f"/api/works/{wid}/card-messages/unread")).json()
    assert "unread_by_card" in full and "unread_by_card" in light
    assert light["unread_by_card"] == full["unread_by_card"], (
        f"未读口径必须逐字相同：full={full['unread_by_card']} light={light['unread_by_card']}")
    # U5 水位语义：A、B 都该有未读
    assert cid_a in light["unread_by_card"], light
    assert cid_b in light["unread_by_card"], light


async def test_u2_unread_endpoint_never_scans_old_archive(sandbox, work, monkeypatch):
    """U2：只算未读时**不许**读旧档案（session_facade.read）。"""
    from src.storage import session_facade
    wid, cid_a, cid_b, _ = await _fixture(sandbox, work, monkeypatch)

    called = []

    async def _boom(*a, **kw):
        called.append(a)
        raise AssertionError("只算未读不该读旧档案（那是每卡一份整会话档案读）")

    monkeypatch.setattr(session_facade, "read", _boom)
    async with _client() as client:
        r = await client.get(f"/api/works/{wid}/card-messages/unread")
    assert r.status_code == 200, r.text
    assert called == [], "未读端点扫了旧档案"
    assert cid_a in r.json()["unread_by_card"]


async def test_u3_shape_and_u4_full_endpoint_keeps_items(sandbox, work, monkeypatch):
    """U3：轻端点只有 unread_by_card；U4：全量端点仍给 items。"""
    wid, cid_a, cid_b, _ = await _fixture(sandbox, work, monkeypatch)
    async with _client() as client:
        light = (await client.get(f"/api/works/{wid}/card-messages/unread")).json()
        full = (await client.get(f"/api/works/{wid}/card-messages")).json()
    assert set(light.keys()) == {"unread_by_card"}, light
    assert "items" in full, "全量端点必须保留 items（通知面板/画布通信线要用）"
    assert isinstance(full["items"], list)


async def test_u5_seen_seq_semantics_unchanged(sandbox, work, monkeypatch):
    """U5：水位之后的算未读、水位之前的算已读。"""
    from src.storage import card_db
    wid, cid_a, cid_b, (seq_a1, seq_a2, seq_b1) = await _fixture(sandbox, work, monkeypatch)
    # 把 A 的水位推到第二条 ⇒ A 变已读；B 保持未读
    await _set_seen(wid, cid_a, seq_a2)
    async with _client() as client:
        light = (await client.get(f"/api/works/{wid}/card-messages/unread")).json()
    assert cid_a not in light["unread_by_card"], light
    assert cid_b in light["unread_by_card"], light
