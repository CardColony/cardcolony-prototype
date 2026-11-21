"""正卡恢复评审的锁、属主转发和版本回归。"""

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI

from src.api import card_tools, control, message_runner, model_service, review_runner, routes
from src.infra import fact_store, intervention_runner, intervention_store, procs
from src.storage import card_store, review_store, session_facade
from test_review_tools import _make_work_with_functions, _open_main_batch, _seated


@pytest.fixture
async def venue(sandbox):
    wid, cid, seats = await _make_work_with_functions(2)
    rid = await _open_main_batch(wid, cid, seats, card_store.iter_card_metas(wid))
    assert (await review_store.state_of(wid, rid))["type"] == "requirements"
    yield wid, cid, rid, seats
    control.unhalt(wid, rid)


async def test_busy_receipt_and_automatic_retry_keep_one_action(venue, monkeypatch):
    wid, cid, rid, seats = venue
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    request = await review_runner.request_resume(wid, cid, spawn=False)
    action_id = request["action_id"]
    turns = []

    assert session_facade.turn_try_acquire(wid, rid, "audit-facilitator")
    try:
        assert await intervention_runner.deliver_registered_action(wid, action_id) == "continue:0"
        status = await review_runner.review_snapshot(wid, rid)
        assert status["running"] is True
        assert status["last_action"]["state"] == "failed"
        assert status["last_action"]["receipt"]["reason"] == "review_busy"
        assert await review_store.spoken_round(wid, rid) == []

        async def answer(*args, act_as, **kwargs):
            turns.append(act_as)
            return {"reply": "意见", "tool_events": []}

        tasks = []
        spawn = review_runner.spawn_resume_delivery

        def capture(work_id, aid):
            tasks.append(spawn(work_id, aid))

        monkeypatch.setattr(routes, "_run_message", answer)
        monkeypatch.setattr(review_runner, "spawn_resume_delivery", capture)
        for _ in range(2):
            repeat = await review_runner.request_resume(wid, cid)
            assert repeat["action_id"] == action_id and repeat["dedup"]
        await asyncio.sleep(0)
        assert not turns
    finally:
        session_facade.turn_release(wid, rid, "audit-facilitator")
    await asyncio.gather(*(message_runner.await_spawned(task) for task in tasks))
    assert turns == [seats[0]]
    status = await review_runner.review_snapshot(wid, rid)
    assert status["last_action"]["state"] == "delivered"
    assert status["last_action"]["receipt"]["started"] is True


async def test_owner_http_failure_and_real_control_route(venue, monkeypatch):
    wid, cid, rid, seats = venue
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    runner = [0]
    monkeypatch.setattr(procs, "is_runner", lambda: True)
    monkeypatch.setattr(procs, "is_web", lambda: False)
    monkeypatch.setattr(procs, "runner_index", lambda: runner[0])
    monkeypatch.setattr(procs, "owner_index_of", lambda w, c: 1)

    app = FastAPI()
    app.include_router(routes.router)
    asgi = httpx.ASGITransport(app=app)
    owner_failed = True
    sent = []
    turns = []

    class OwnerTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            sent.append(request.url.path)
            if owner_failed and request.url.path == "/api/ctl/review_step":
                return httpx.Response(500, json={"detail": "owner unavailable"})
            runner[0] = 1
            try:
                return await asgi.handle_async_request(request)
            finally:
                runner[0] = 0

    async def answer(*args, act_as, **kwargs):
        turns.append(act_as)
        return {"reply": "意见", "tool_events": []}

    monkeypatch.setattr(routes, "_run_message", answer)
    async with httpx.AsyncClient(transport=OwnerTransport()) as client:
        monkeypatch.setattr(procs, "_loop_clients", lambda: {"ctl": client})
        request = await review_runner.request_resume(wid, cid, seat=seats[1], spawn=False)
        aid = request["action_id"]
        assert request["step"]["draft_fp"]
        assert await intervention_runner.deliver_registered_action(wid, aid) == "continue:0"
        snap = await review_runner.review_snapshot(wid, rid, caller=cid)
        assert snap["last_action"]["receipt"]["reason"] == "runner_http"
        assert snap["last_action"]["state"] == "failed"
        assert not turns

        owner_failed = False
        assert await intervention_runner.deliver_registered_action(wid, aid) == "continue:1"
        assert turns == [seats[1]]
        assert await intervention_runner.deliver_registered_action(wid, aid) == "delivered"
    assert sent[:3] == ["/api/ctl/review_resume", "/api/ctl/review_step",
                        "/api/ctl/review_snapshot"]


async def test_manual_action_stales_same_version_body_change(venue, monkeypatch):
    wid, cid, rid, _ = venue
    queued = await review_runner.request_resume(wid, cid, spawn=False)
    await card_store.update_card(wid, cid, {"requirements_draft": "新版正文"}, "audit")
    current = await review_store.state_of(wid, rid)
    assert str(current["draft"]) == queued["step"]["draft"]

    async def unexpected(*args, **kwargs):
        pytest.fail("旧材料动作进入了席位回合")

    monkeypatch.setattr(routes, "_run_message", unexpected)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    assert await intervention_runner.deliver_registered_action(
        wid, queued["action_id"]) == "continue:stale"
    row = intervention_store.store().get_action(wid, queued["action_id"])
    assert row["receipt"]["reason"] == "stale_step"
    assert "draft_fp" in row["receipt"]["diff"]


async def test_legacy_false_success_can_resume_same_action(venue):
    wid, cid, rid, _ = venue
    queued = await review_runner.request_resume(wid, cid, spawn=False)
    aid = queued["action_id"]
    intervention_runner.mark_receipt(wid, aid, "continue:1")
    fact_store.store().ingest(wid, {
        "seq": 1, "ts": time.time(), "card_id": rid, "type": "harness_message",
        "data": {"action_id": aid, "mode": "continue", "started": True,
                 "rule_id": "review_step_not_finished", "to": rid}})
    assert intervention_runner.already_delivered(wid, aid)
    retry = await review_runner.request_resume(wid, cid, spawn=False)
    assert retry["action_id"] == aid and retry["outcome"] == "requeued"
    assert not intervention_runner.already_delivered(wid, aid)


async def test_named_merge_survives_change_in_default_seat(venue, monkeypatch):
    wid, cid, rid, seats = venue
    await review_store.approve(wid, rid, seats[0])
    normal = await review_runner.request_resume(wid, cid, spawn=False)
    named = await review_runner.request_resume(wid, cid, seat=seats[1], spawn=False)
    assert normal["action_id"] == named["action_id"]
    assert (await review_runner._step_identity(wid, rid))["next"] == named["step"]["next"]
    await review_store.revoke(wid, rid, seats[0])
    assert (await review_runner._step_identity(wid, rid))["next"] != named["step"]["next"]
    turns = []

    async def answer(*args, act_as, **kwargs):
        turns.append(act_as)
        return {"reply": "意见", "tool_events": []}

    monkeypatch.setattr(routes, "_run_message", answer)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    assert await intervention_runner.deliver_registered_action(
        wid, named["action_id"]) == "continue:1"
    assert turns == [seats[1]]


async def test_named_seat_honors_reject_before_first_speech(venue):
    wid, cid, rid, seats = venue
    assert (await review_store.reject(wid, rid, seats[1]))["ok"]
    assert seats[1] not in await review_store.spoken_round(wid, rid)
    named = await review_runner.request_resume(wid, cid, seat=seats[1], spawn=False)
    assert named["reason"] == "awaiting_target_response"
    assert named["vote"]["verdict"] == "reject"


async def test_completed_nudge_without_vote_can_be_requested_again(venue, monkeypatch):
    wid, cid, rid, seats = venue
    await _seated(wid, rid, *seats)
    await review_store.approve(wid, rid, seats[0])
    turns = []

    async def answer(*args, act_as, **kwargs):
        turns.append(act_as)
        return {"reply": "尚未投票", "tool_events": []}

    run_message = routes._run_message
    monkeypatch.setattr(routes, "_run_message", answer)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    first = await review_runner.request_resume(wid, cid, seat=seats[1], spawn=False)
    assert await intervention_runner.deliver_registered_action(
        wid, first["action_id"]) == "continue:1"
    snap = await review_runner.review_snapshot(wid, rid)
    assert snap["stop_reason"] == "awaiting_vote"
    assert snap["last_action"]["receipt"]["voted"] is False

    second = await review_runner.request_resume(wid, cid, seat=seats[1], spawn=False)
    repeat = await review_runner.request_resume(wid, cid, seat=seats[1], spawn=False)
    assert second["action_id"] != first["action_id"]
    assert repeat["action_id"] == second["action_id"]
    monkeypatch.setattr(routes, "_run_message", run_message)
    calls = 0

    async def vote(**kwargs):
        nonlocal calls
        calls += 1
        tools = [{"name": "approve", "arguments": {"review_card_id": rid}}] if calls == 1 else []
        return {"content": "核对通过", "tool_calls": tools}, "audit-model"

    monkeypatch.setattr(model_service, "call_main", vote)
    assert await intervention_runner.deliver_registered_action(
        wid, second["action_id"]) == "continue:1"
    assert turns == [seats[1]] and calls >= 1
    snap = await review_runner.review_snapshot(wid, rid)
    assert snap["last_action"]["receipt"]["voted"] is True
    assert snap["last_action"]["receipt"]["vote_draft"] == snap["draft"]
    votes = (await review_store.status(wid, rid))["votes"]
    assert votes[seats[1]]["verdict"] == "approve"


async def test_stop_before_delivery_blocks_alignment_advance(sandbox, monkeypatch):
    wid, cid, _ = await _make_work_with_functions(2)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "brief": "评审"}, card_store.iter_card_metas(wid))
    rid = out["data"]["review_card_id"]
    initial = await review_store.state_of(wid, rid)
    seat = initial["members"][0]
    assert (await review_store.approve(wid, rid, seat))["ok"]
    named = await review_runner.request_resume(wid, cid, seat=seat, spawn=False)
    assert named["reason"] == "seat_voted"
    request = await review_runner.request_resume(wid, cid, spawn=False)
    assert request["step"]["next"] == "advance"
    control.halt(wid, rid, by="user")
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    try:
        assert await intervention_runner.deliver_registered_action(
            wid, request["action_id"]) == "continue:0"
        snap = await review_runner.review_snapshot(wid, rid)
        assert snap["batch"] == initial["batch"]
        assert snap["stop_reason"] == "stopped"
        assert snap["last_action"]["receipt"]["reason"] == "stopped"
    finally:
        control.unhalt(wid, rid)
