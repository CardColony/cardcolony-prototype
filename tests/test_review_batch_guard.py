"""批次守卫：旧批次在途席位回合不许把内容写进新批次会话。

[2026-10-31 用户令修正] 开新批次＝新建会话**并把旧会话归档**——旧块迁到
`{rid}-{旧批次}` 保留，不是删了。

在途旧轮可能正拿着写光标跑——守卫做两件事：
1. `open_batch` 在场卡属主归档旧会话，并与新批次一起发布空白活跃会话。
2. 新批发布后取消在途旧回合。同协程留下的旧残块按回合身份归档。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.anyio]

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.storage import card_db, session_facade, review_store, card_store
from src.api import card_tools, model_service
from tests.test_review_user_intervention import _setup


def _sid(rid: str, batch: str) -> str:
    """归档会话 id：`{rid}-{批次}`。"""
    return f"{rid}-{batch}"


async def test_inflight_old_batch_speech_never_touches_new_batch_session(sandbox,
                                                                       monkeypatch):
    """席位回合跑到一半批次换了（同协程开批）：它随后写的残块被清，
    误落在新批次的票被撤，旧批已发言的块归档保留。"""
    from src.api import review_runner

    wid, cid, fids, rid = await _setup(2)
    # 旧批次已有一块发言（开批时该归档走的内容）
    await session_facade.append(wid, rid, {
        "actor": "assistant", "from": fids[0],
        "content": "旧批次的发言不该被冲"})
    old_batch = str((await review_store.state_of(wid, rid)).get("batch"))
    mid_flight = asyncio.Event()
    calls = {"n": 0}

    async def model_a(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # 同一协程里开新批次（正卡 turn 里 open_review 的真实拓扑）——
            # 归档旧会话、开全新场；停不掉的在途轮由收尾守卫兜底。
            await review_store.open_batch(wid, rid, "二轮",
                                          members=fids, target=cid, brief="新")
            mid_flight.set()
            return ({"content": "在途旧轮说了一半的话",
                     "tool_calls": [{"name": "reject",
                                     "arguments": {"review_card_id": rid}}]},
                    "fake")
        return ({"content": "残块", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", model_a)
    st0 = await review_store.state_of(wid, rid)
    await review_runner._seat_turn(wid, rid, fids[0], st0, kind="speak")

    assert mid_flight.is_set(), "批次确实在回合中途换了"
    # 旧块在归档里，一块不丢
    old = await session_facade.read(wid, _sid(rid, old_batch))
    assert len(old) == 1 and "旧批次的发言不该被冲" in str(old[0])
    # 场会话（新批次的）一片残块都没有
    new = await session_facade.read(wid, rid)
    assert new == [], f"在途旧轮的残块必须被清掉，实际读到 {len(new)} 块"
    # 它在新批次里误投的否决票也必须被撤（它压根没见过新稿）
    st1 = await review_store.status(wid, rid)
    assert fids[0] not in st1.get("rejected", []), st1


async def test_new_batch_archives_old_session_and_restarts_seq(sandbox):
    """开新批次：旧会话归档到 `{rid}-{旧批次}`、场会话空白从 seq=1 重起。"""
    wid, cid, fids, rid = await _setup(2)
    await session_facade.append(wid, rid, {
        "actor": "assistant", "content": "旧批次痕迹"})
    old_batch = str((await review_store.state_of(wid, rid)).get("batch"))

    await review_store.open_batch(wid, rid, "二轮",
                                  members=fids, target=cid, brief="新")

    assert await session_facade.read(wid, rid) == []
    blocks = await session_facade.read(wid, _sid(rid, old_batch))
    assert len(blocks) == 1
    assert "旧批次痕迹" in str(blocks[0])

    await session_facade.append(
        wid, rid, {"type": "say", "role": "assistant", "content": "新批次第一句"})
    blocks2 = await session_facade.read(wid, rid)
    assert len(blocks2) == 1
    assert blocks2[0]["seq"] == 0, "新会话从头编号"
    assert "新批次第一句" in blocks2[0]["content"]


async def test_new_batch_is_published_on_review_owner(sandbox, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from src.api import routes
    from src.infra import procs

    wid, cid, fids, rid = await _setup(2)
    old_batch = (await review_store.state_of(wid, rid))["batch"]
    await session_facade.append(wid, rid, {"role": "user", "content": "old owner session"})
    runner = [0]
    monkeypatch.setattr(procs, "is_web", lambda: False)
    monkeypatch.setattr(procs, "is_runner", lambda: True)
    monkeypatch.setattr(procs, "runner_index", lambda: runner[0])
    monkeypatch.setattr(procs, "owner_index_of", lambda w, c: 1)
    app = FastAPI()
    app.include_router(routes.router)
    asgi = httpx.ASGITransport(app=app)

    class Owner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.path == "/api/ctl/review_open_batch"
            runner[0] = 1
            try:
                return await asgi.handle_async_request(request)
            finally:
                runner[0] = 0

    async with httpx.AsyncClient(transport=Owner()) as client:
        monkeypatch.setattr(procs, "_loop_clients", lambda: {"ctl": client})
        new = await review_store.open_batch(
            wid, rid, "owner", members=fids, target=cid, expect_batch=old_batch)
    assert new["batch"] != old_batch
    assert await session_facade.read(wid, rid) == []
    assert "old owner session" in str(await session_facade.read(wid, _sid(rid, old_batch)))
