# -*- coding: utf-8 -*-
"""消费方切换回归（迭代一·会话底座 · S6/A6：七处消费点全走门面，T069-T073）。

判据（机械可判定）：
1. 写切换（A6b）：POST /messages 用户条＋assistant 条均经门面落盘 →
   文件以 V2 头起、块含六事实字段、assistant 缺省 end=model_done；
2. 读切换（A6a）：GET /session（新版＋兼容层）返回门面块（五键契约
   ＋V2 六字段），旧前端只认的五键不受影响；
3. 活跃度切换：card_last_active 经门面 scan_tail（尾部反向扫描语义保留）；
4. 凝练读切换：condense 触发取到的未凝段与 facade.read 切片同尺
   （块数＋content 对齐——水位语义的承重前提）。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from src.api import model_service, routes
from src.storage import activity, session_facade, session_store


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


# ---------- 1/2：读写切换端到端 ----------

async def test_post_message_writes_v2_and_session_reads_facade(sandbox, work, client,
                                                               monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    wid, cid = work["work_id"], "card_001"
    r = await client.post(f"/api/works/{wid}/cards/{cid}/messages",
                          json={"content": "你好"})
    assert r.status_code == 200, r.text

    # 写切换实证：文件以 V2 头起（旧 append_message 不会写头）
    path = session_store._session_path(wid, cid)
    first_line = path.read_text(encoding="utf-8").split("\n", 1)[0]
    assert first_line == "---SESSION---", "会话文件必须已是 V2 格式"

    # 读切换实证：GET /session 返回门面块（五键＋V2 六字段）
    body_all = (await client.get(f"/api/works/{wid}/cards/{cid}/session")).json()
    # [迭代二 S4 配套] 环节留痕块（behavior 非空：triage 等）不计对话断言
    body = [m for m in body_all if not m.get("behavior")]
    assert len(body) == 2
    assert body[0]["role"] == "user" and body[0]["content"] == "你好"
    assert body[0]["actor"] == "human" and body[0]["seq"] == 0
    assert body[1]["role"] == "assistant" and body[1]["end"] == "model_done"
    assert body[1]["turn"] == 0 and body[1]["pair"] == "0", "pair=响应发起块 seq"
    # [迭代二 S4 配套] triage 环节块合法占用中间 seq（此处 seq=1），对话块
    # seq 门面分配、单调递增（0 < 2）
    assert [b["seq"] for b in body] == [0, 2], "seq 门面分配、单调"

    # 兼容层同走门面
    body2_all = (await client.get(f"/api/cards/{cid}/session")).json()
    body2 = [m for m in body2_all if not m.get("behavior")]
    assert [b["seq"] for b in body2] == [0, 2]
    assert body2[1]["end"] == "model_done"


# ---------- 3：活跃度切换 ----------

async def test_card_last_active_uses_facade_scan_tail(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    assert activity.card_last_active(wid, cid) == "", "空会话 ts 为空"
    await session_facade.append(wid, cid, {"role": "user", "content": "活跃"})
    st = session_facade.scan_tail(wid, cid)
    assert st["format"] == "v2" and st["ts"]
    assert activity.card_last_active(wid, cid) == st["ts"], "与门面扫描同源"


async def test_card_last_active_v1_file_still_works(sandbox, work):
    """V1 文件：scan_tail 委托 last_ts，活跃度语义不变。"""
    wid, cid = work["work_id"], "card_002"
    path = session_store._session_path(wid, cid)
    path.write_text("创建时间：x\n\n---MSG---\nrole: user\n"
                    "timestamp: 2026-10-10T00:00:00+08:00\n\n旧格式\n", encoding="utf-8")
    assert activity.card_last_active(wid, cid) != "", "V1 尾扫语义保留"
    from src.storage import session_store as ss
    assert activity.card_last_active(wid, cid) == ss.last_ts(wid, cid)


# ---------- 4：凝练读切换（切片同尺） ----------

async def test_condense_pending_slice_matches_facade_read(sandbox, work, monkeypatch):
    from src.storage import memory_store

    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "第一条"})
    await session_facade.append(wid, cid, {"role": "assistant", "content": "第二条"})
    await session_facade.append(wid, cid, {"role": "user", "content": "第三条"})
    mark = 1  # 水位在第 1 块后

    seen: dict = {}

    async def fake_ensure(w, c):
        return None

    async def fake_pipeline(w, c, pending, m):
        seen["pending"] = pending
        seen["mark"] = m

    monkeypatch.setattr(memory_store, "ensure_watermark", fake_ensure)
    monkeypatch.setattr(memory_store, "load_memory", lambda w, c: {"mark": mark})
    monkeypatch.setattr(memory_store, "trigger_k_tokens", lambda: 0)
    monkeypatch.setattr(memory_store, "_run_condense_pipeline", fake_pipeline)

    await memory_store.condense_turn_end(wid, cid)
    facade_blocks = await session_facade.read(wid, cid)
    assert seen["pending"] == facade_blocks[mark:], "未凝段与门面读切片逐块一致"
    assert [b["content"] for b in seen["pending"]] == ["第二条", "第三条"]
    assert seen["mark"] == mark
