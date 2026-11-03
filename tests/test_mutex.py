# -*- coding: utf-8 -*-
"""逻辑互斥体回归（迭代一·会话底座 · S4/A4：同一登记处，T083-T086）。

判据（机械可判定，依据设计 4.4）：
1. 登记处单元语义：try_acquire 占用/拒绝、release 只释自己、holder 诊断；
2. 主线回合门禁：同卡在途（任何 owner）→ POST /messages 409 拒绝、不排队
   （T084，10-08 口径），拒绝留 ops 痕；释放后可入；
3. 凝练支线合并（T086）：凝练持有 → 主线 POST 409；主线持有 → 凝练触发
   跳过留痕（`_condensing_cards` 旧集合退役）；
4. 旧防护不删（T087）：重发去重等旧语义不受登记处影响（属消费面测试，
   此处以登记处独立存在为锚）。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from src.api import model_service, routes
from src.storage import session_facade, session_store


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(routes.router)
    return a


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------- 1：登记处单元 ----------

def test_turn_registry_basics(sandbox):
    assert session_facade.turn_try_acquire("w", "c", "turn:a") is True
    assert session_facade.turn_try_acquire("w", "c", "turn:b") is False, "同卡第二占拒绝"
    assert session_facade.turn_try_acquire("w", "c2", "turn:b") is True, "异卡不受影响"
    assert session_facade.turn_holder("w", "c") == "turn:a"
    session_facade.turn_release("w", "c", "turn:b")          # 非持有者释放＝无操作
    assert session_facade.turn_holder("w", "c") == "turn:a", "只释自己"
    session_facade.turn_release("w", "c", "turn:a")
    assert session_facade.turn_holder("w", "c") == ""


# ---------- 2：主线回合 409 门禁 ----------

async def test_post_message_409_when_turn_inflight(sandbox, work, client):
    wid, cid = work["work_id"], "card_001"
    assert session_facade.turn_try_acquire(wid, cid, "turn:manual") is True
    r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                          json={"content": "你好"})
    assert r.status_code == 409, r.text
    assert "回合进行中" in r.json()["error"]
    assert session_facade.turn_holder(wid, cid) == "turn:manual", "拒绝不得覆盖既有持有"
    # 拒绝留痕
    ops = session_store.WORKS_DIR / "_ops" / "session_ops.jsonl"
    actions = [json.loads(l)["action"] for l in ops.read_text(encoding="utf-8").strip().split("\n")]
    assert "mutex_reject" in actions
    session_facade.turn_release(wid, cid, "turn:manual")


async def test_post_message_ok_after_release(sandbox, work, client, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    wid, cid = work["work_id"], "card_001"
    r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                          json={"content": "你好"})
    assert r.status_code == 200, r.text
    assert session_facade.turn_holder(wid, cid) == "", "回合结束必须释放登记（finally）"


# ---------- 3：凝练支线合并（T086） ----------

async def test_post_message_409_when_condense_holds(sandbox, work, client):
    wid, cid = work["work_id"], "card_001"
    assert session_facade.turn_try_acquire(wid, cid, "condense") is True
    r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                          json={"content": "你好"})
    assert r.status_code == 409, "凝练持有期间主线回合同卡拒绝（同一登记处）"
    session_facade.turn_release(wid, cid, "condense")


async def test_condense_runs_when_turn_held(sandbox, work, monkeypatch):
    """[去锁裁定 2026-10-22] 凝练是后台任务：主线回合持锁**照常凝**——
    T086 旧语义「主线持有→凝练跳过」已被裁定反转（原实现永远抢不到锁，
    fn_002 六小时没凝过）。同卡互斥只剩 _CONDENSING（防两个凝练并发）。"""
    from src.storage import memory_store

    wid, cid = work["work_id"], "card_001"
    seen: dict = {}

    async def fake_ensure(w, c):
        return None

    async def fake_pipeline(w, c, pending, mark):
        seen["called"] = True

    monkeypatch.setattr(memory_store, "ensure_watermark", fake_ensure)
    monkeypatch.setattr(memory_store, "load_memory", lambda w, c: {"mark": 0})
    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 0)  # 恒到线
    monkeypatch.setattr(memory_store, "_run_condense_pipeline", fake_pipeline)
    assert not hasattr(memory_store, "_condensing_cards"), "旧集合必须退役（T086）"

    # 主线持有 → 凝练照常启动（不抢、不动主线登记）
    assert session_facade.turn_try_acquire(wid, cid, "turn:abc") is True
    await memory_store.condense_turn_end(wid, cid)
    assert seen.get("called") is True, "去锁裁定：凝练不碰回合锁，主线持锁照凝"
    assert session_facade.turn_holder(wid, cid) == "turn:abc", "凝练不得动主线登记"
    session_facade.turn_release(wid, cid, "turn:abc")

    # 同卡并发凝练互斥仍在（_CONDENSING）：占位时第二触发跳过
    memory_store._CONDENSING.add((wid, cid))
    seen.clear()
    await memory_store.condense_turn_end(wid, cid)
    assert seen.get("called") is None, "同卡已在凝 ⇒ 第二触发必须跳过"
    memory_store._CONDENSING.discard((wid, cid))
