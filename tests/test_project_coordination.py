"""The project agent carries a single inspection transcript across model handoff."""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from src.api import coordination_runner, event_stream, message_runner, model_service
from src.infra import coordination_policy, coordination_watcher, policy_runner, policy_set
from src.storage import card_db, card_store, coordination_store, event_log, memory_store, session_facade
from test_coordination import _appoint_kwargs, _mk_card, _seeded_work


AGENT = coordination_store.PROJECT_AGENT_ID


@pytest.fixture(autouse=True)
def isolate_harness_policies(tmp_path, monkeypatch):
    monkeypatch.setenv("ECO_POLICIES_PATH", str(tmp_path / "harness_policies.json"))
    monkeypatch.setattr(policy_set, "_SET", None)
    monkeypatch.setattr(policy_set, "_CONFIG_STAMP", ())


def _install_inspection(wid, aid, target, *, check="查接口", wake_when="缺证据", **kwargs):
    return coordination_policy.handle(wid, aid, {"op": "save", "id": "verify",
        "trigger": "card_block_edited", "card_ids": [target], "check": check,
        "wake_when": wake_when, **kwargs}, {target})


async def test_sse_latest_cursor_waits_for_next_frame():
    work, session = "coordination-cursor-test", coordination_store.PROJECT_CHAT_SESSION
    event_stream.emit(work, session, "run_start", {"pair_id": "cursor-test"}, journal_only=True)
    cursors = [event_stream.journal_last_seq(work, session) + 1,
               event_stream.token_journal_last_seq(work, session) + 1]
    samples = [event_stream.subscribe(work, session, cursors[0]),
               event_stream.subscribe_tokens(work, session, cursors[1])]
    futures = [asyncio.create_task(sample.__anext__()) for sample in samples]
    try:
        await asyncio.sleep(0)
        assert all(not future.done() for future in futures)
        event_stream.emit(work, session, "round_start", {"round": 1}, journal_only=True)
        frames = await asyncio.wait_for(asyncio.gather(*futures), 2)
        assert [frame["type"] for frame in frames] == ["round_start", "round_start"]
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        for sample in samples:
            await sample.aclose()


async def test_project_chat_serializes_native_tools_at_provider_boundary(sandbox, monkeypatch, client):
    from src.model_gateway.gateway import build_gateway

    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口回读验收")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    route = appt["models"]["strong"]
    gateway = build_gateway({"models": {route: {
        "vendor": "kimi", "provider": "openai", "model": "k3.1-pro",
        "base_url": "https://example.test/v1", "api_key_env": "COORD_TEST_API_KEY",
        "retry": {"max_attempts": 1, "backoff_seconds": 0},
    }}, "retry_grading": {"enabled": False}})
    monkeypatch.setenv("COORD_TEST_API_KEY", "fixture-key")
    monkeypatch.setattr(model_service, "_gateway", gateway)
    sent = []
    first_chunk = asyncio.Event()
    release = asyncio.Event()

    class DelayedTool(httpx.AsyncByteStream):
        async def __aiter__(self):
            frame = json.dumps({"choices": [{"delta": {"content": "正在读取"}, "finish_reason": None}]})
            yield f"data: {frame}\n\n".encode("utf-8")
            first_chunk.set()
            await release.wait()
            delta = {"tool_calls": [{"index": 0, "id": "call_read", "type": "function",
                                      "function": {"name": "coord_read_card", "arguments": json.dumps({"card_id": domain})}}]}
            frame = json.dumps({"choices": [{"delta": delta, "finish_reason": "tool_calls"}]})
            yield f"data: {frame}\n\ndata: [DONE]\n\n".encode("utf-8")

    def provider(request):
        body = json.loads(request.content)
        sent.append(body)
        expected = {"coord_read_card", "coord_read_events", "coord_read_dialogue", "coord_plan_board",
                    "coord_submit_instruction", "design_policy", "read_memory", "memory_expand"}
        if ({tool["function"]["name"] for tool in body["tools"]} != expected
                or any(tool["function"]["parameters"].get("type") != "object" for tool in body["tools"])):
            return httpx.Response(400, json={"error": {"message": "function name is invalid"}})
        if len(sent) == 1:
            return httpx.Response(200, stream=DelayedTool())
        assert any(msg["role"] == "tool" and "接口回读验收" in msg["content"] for msg in body["messages"])
        frame = json.dumps({"choices": [{"delta": {"content": "已查到接口回读验收卡。"}, "finish_reason": "stop"}]})
        return httpx.Response(200, content=f"data: {frame}\n\ndata: [DONE]\n\n".encode("utf-8"))

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(
        transport=httpx.MockTransport(provider), **kw))
    endpoint = f"/api/works/{wid}/coordination/agent/chat"
    send = asyncio.create_task(client.post(endpoint, json={"assignment_id": appt["assignment_id"],
                                                          "expected_rev": 0, "content": "查一下接口回读验收。"}))
    flow = f"/api/works/{wid}/cards/{AGENT}"
    sid = coordination_store.PROJECT_CHAT_SESSION
    try:
        await asyncio.wait_for(first_chunk.wait(), 5)
        items = (await client.get(flow + "/timeline", params={"session": sid})).json()["items"]
        assert [(x["type"], x["data"]["content"]) for x in items] == [("user_message", "查一下接口回读验收。")]
        assert (await client.get(flow + "/progress", params={"session": sid})).json()["active"]
        assert not send.done()
    finally:
        release.set()
        response = await asyncio.wait_for(send, 5)
    assert response.status_code == 200, response.text
    assert len(sent) == 2 and all(body["model"] == "k3.1-pro" for body in sent)
    assert [x["type"] for x in event_stream.journal_of(wid, sid)] == [
        "user_message", "run_start", "round_start", "assistant_text", "tool_call", "tool_result",
        "round_start", "assistant_text", "done", "turn_end",
    ]
    assert [x["data"]["text"] for x in event_stream.token_journal_of(wid, sid)
            if x["type"] == "content_chunk"] == ["正在读取", "已查到接口回读验收卡。"]
    messages = (await client.get(endpoint)).json()["messages"]
    assert [msg["content"] for msg in messages] == ["查一下接口回读验收。", "已查到接口回读验收卡。"]
    event_stream._journals.pop(event_stream._key(wid, sid), None)
    items = (await client.get(flow + "/timeline", params={"session": sid, "view": "brief"})).json()["items"]
    assert [x["type"] for x in items] == ["user_message", "assistant_text", "tool_call", "tool_result", "assistant_text"]
    assert items[1]["data"]["content"] == "正在读取"
    assert items[2]["data"]["tool_call_id"] == items[3]["data"]["tool_call_id"]
    assert len({item["run_id"] for item in items}) == 1
    detail = await client.get(flow + "/timeline/event", params={"session": sid, "seqs": items[3]["seq"]})
    assert detail.status_code == 200
    assert "接口回读验收" in detail.json()["items"][0]["data"]["result"]["content"]


async def test_project_chat_can_read_its_previous_tool_results(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口回读")
    marker = "上一轮工具原件的状态标记"
    await card_store.update_card(wid, domain, {"status_block": marker}, "测试")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    previous_checks = []

    async def model(system, user, **kwargs):
        if user == "先查证。":
            return {"tool_calls": [{"name": "coord_read_card", "arguments": {"card_id": domain}}]}, kwargs["model_name"]
        history = kwargs["prior_messages"]
        if user == "回查上一轮用了什么依据。":
            assert all(marker not in m["content"] for m in history)
            return {"tool_calls": [{"name": "coord_read_dialogue", "arguments": {"card_id": AGENT}}]}, kwargs["model_name"]
        assert history[-1]["role"] == "tool" and marker in history[-1]["content"]
        previous_checks.append(history[-1]["content"])
        return {"content": "完成查证。"}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    endpoint = f"/api/works/{wid}/coordination/agent/chat"
    for content in ("先查证。", "回查上一轮用了什么依据。"):
        result = await client.post(endpoint, json={"assignment_id": appt["assignment_id"],
                                                   "expected_rev": 0, "content": content})
        assert result.status_code == 200, result.text
    assert len(previous_checks) == 2
    assert [msg["type"] for msg in json.loads(previous_checks[1])["messages"]] == [
        "user_message", "tool_call", "tool_result", "assistant_text",
    ]


async def test_project_chat_paged_evidence_keeps_wire_prefix_until_compaction(sandbox, monkeypatch):
    from outbound_probe import OutboundRecorder, frame_finish, frame_text, frame_tool, sse
    from src.model_gateway.gateway import build_gateway

    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "对照需求")
    pages = [marker * 3200 + "续页原文" for marker in "甲乙丙丁戊己"]
    document = "# 约束与验收\n" + "\n".join(pages) + "\n末尾标记"
    await card_store.update_card(wid, domain, {"requirements": document}, "测试")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    route = appt["models"]["strong"]
    gateway = build_gateway({"models": {route: {
        "vendor": "kimi", "provider": "openai", "model": "k3.1-pro",
        "base_url": "https://example.test/v1", "api_key_env": "COORD_TEST_API_KEY",
        "retry": {"max_attempts": 1, "backoff_seconds": 0},
    }}})
    monkeypatch.setenv("COORD_TEST_API_KEY", "fixture-key")
    monkeypatch.setattr(model_service, "_gateway", gateway)
    calls = [{"card_id": domain}] + [{"card_id": domain, "block": "requirements",
                                      "para_start": line, "para_end": line}
                                     for line in (2, 2, 3, 4, 5, 6)]

    def provider(index, _):
        if index < len(calls):
            return sse(frame_tool(f"read-{index}", "coord_read_card", calls[index]), frame_finish())
        return sse(frame_text("已核对指定条款。"), frame_finish("stop"))

    wire = OutboundRecorder(provider).install(monkeypatch)
    await coordination_runner.run_user_chat(wid, AGENT, assignment_id=appt["assignment_id"],
                                            expected_rev=0, content="核对这些条款。")
    assert len(wire.requests) == 8
    assert max(request["bytes"] for request in wire.requests) < 64 * 1024
    assert "末尾标记" not in wire.joined(1) and "约束与验收" in wire.joined(1)
    first_page = next(m for m in wire.requests[2]["body"]["messages"] if pages[0][:3200] in (m.get("content") or ""))
    first_messages = wire.requests[2]["body"]["messages"]
    prefix = first_messages[:first_messages.index(first_page) + 1]
    assert wire.requests[3]["body"]["messages"][:len(prefix)] == prefix
    assert wire.joined(3).count(pages[0][:3200]) == 1 and "重复回执" in wire.joined(3)
    assert "较早的回执已留档" in wire.joined(7) and "戊" * 100 in wire.joined(7)
    saved = await session_facade.read(wid, coordination_store.PROJECT_CHAT_SESSION)
    assert sum(pages[0][:3200] in block["content"] for block in saved if block["actor"] == "tool") == 2
    next_page = json.loads(first_page["content"])["next"]
    continuation = json.loads(await coordination_runner._exec_coord_read(
        wid, "coord_read_card", next_page, {domain}, project=True))
    assert continuation["content"] == "续页原文" and continuation["next"] is None
    await card_store.update_card(wid, domain, {"requirements": document + "改版"}, "测试")
    assert "需求版本已变化" in await coordination_runner._exec_coord_read(
        wid, "coord_read_card", next_page, {domain}, project=True)
    old_result = next(block for block in saved if block["actor"] == "tool" and pages[0][:3200] in block["content"])
    args = {
        "card_id": AGENT, "tool_call_id": old_result["tool_call_id"], "offset": 3200,
    }
    allowed = coordination_runner._allowed_card_ids(wid, appt["observe_scope"])
    archived = json.loads(await coordination_runner._read_dialogue(
        wid, args, allowed, project_chat=True, exclude_pair=old_result["pair_id"]))
    assert archived["content"] == old_result["content"][3200:]
    assert "没有这条可查" in await coordination_runner._read_dialogue(
        wid, args, set(), project_chat=True, exclude_pair=old_result["pair_id"])


async def test_project_chat_can_stop_and_send_again(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def model(system, user, **kwargs):
        if not entered.is_set():
            entered.set()
            await hold.wait()
        return {"content": "继续回复。"}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    endpoint = f"/api/works/{wid}/coordination/agent/chat"
    payload = {"assignment_id": appt["assignment_id"], "expected_rev": 0, "content": "暂停本轮。"}
    task = asyncio.create_task(client.post(endpoint, json=payload))
    stop = f"/api/works/{wid}/cards/{AGENT}/stop"
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await client.post(stop, params={"session": coordination_store.PROJECT_CHAT_SESSION})
        assert response.json()["stopped"] is True
        assert (await asyncio.wait_for(task, 5)).status_code == 200
    finally:
        hold.set()
    assert (await client.post(endpoint, json={**payload, "content": "继续。"})).status_code == 200
    assert [msg["content"] for msg in (await client.get(endpoint)).json()["messages"]] == [
        "暂停本轮。", "继续。", "继续回复。",
    ]


async def test_project_chat_updates_only_the_plan_objective(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    before = (await coordination_store.save_plan(wid, AGENT, assignment_id=aid, expected_rev=0, plan={
        **appt["plan"], "requirements": [{"ref": "card_001", "quote": "各域并行交付"}],
        "items": [{"item_id": "it_existing", "title": "核对联验"}],
    }))["appointment"]
    objective = "各域并行推进，以需求正稿为验收依据。"
    request = {"name": "coord_plan_board", "arguments": {"op": "apply", "objective": objective}}

    async def model(system, user, **kwargs):
        board = next(tool for tool in kwargs["tools"] if tool["name"] == "coord_plan_board")
        assert "objective" in board["parameters"]["properties"]
        if user:
            assert before["plan"]["objective"] in kwargs["turn_tail"][1]["content"]
            return {"tool_calls": [request, request, {**request, "arguments": {
                "op": "apply", "objective": " "}}]}, kwargs["model_name"]
        saved, replayed, invalid = [message["content"] for message in kwargs["prior_messages"][-3:]]
        assert json.loads(saved) == {"ok": True, "rev": 2, "changed": [],
                                     "allocated": {}, "objective": objective}
        assert saved == replayed
        assert "总目标 objective 须为非空文字" in invalid
        assert objective in kwargs["turn_tail"][1]["content"]
        return {"content": "已更新统筹总目标。"}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    response = await client.post(f"/api/works/{wid}/coordination/agent/chat", json={
        "assignment_id": aid, "expected_rev": before["rev"], "content": f"把统筹总目标改成：{objective}",
    })
    assert response.status_code == 200, response.text
    panel = coordination_store.panel(wid, AGENT, history=True)
    assert panel["rev"] == 2 and panel["plan"] == {**before["plan"], "objective": objective}
    saved_revision = next(entry for entry in panel["history"] if entry["rev"] == 2)
    assert saved_revision["detail"]["objective"] == objective
    with pytest.raises(coordination_store.CoordinationError) as stale:
        await coordination_store.board_apply(wid, AGENT, assignment_id=aid, expected_rev=before["rev"],
                                             changes=None, objective="过期写入")
    assert stale.value.code == "conflict"


async def test_project_chat_can_plan_and_direct_with_a_formal_receipt(sandbox, monkeypatch, client):
    from src.infra import loops

    wid, _, _ = await _seeded_work(monkeypatch)
    target = await _mk_card(wid, "接口责任卡")
    reviewer = await _mk_card(wid, "复验责任卡")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    assert appt["mode"] == "live"
    aid = appt["assignment_id"]
    await coordination_store.consume_wake(wid, aid, "appoint", aid)
    await card_db.run_write(wid, lambda conn: conn.execute(
        "UPDATE coordination_appointments SET mode='observe' WHERE assignment_id=?", (aid,)))
    wake = []
    item = {}
    recipient_loop = asyncio.new_event_loop()

    async def deliver(work_id, card_id, *args, **kwargs):
        assert work_id == wid and card_id in (target, reviewer)
        assert asyncio.get_running_loop() is recipient_loop
        assert kwargs["admission"]()
        wake.append(card_id)
        return True

    async def model(system, user, **kwargs):
        assert kwargs["caller"] == "coordination_chat" and kwargs["model_name"] == "strong-test-route"
        tools = {tool["name"] for tool in kwargs["tools"]}
        assert "coord_submit_instruction" in tools and "create_worker_card" not in tools
        await kwargs["before_send"]()
        if user:
            return {"tool_calls": [{"name": "coord_plan_board", "arguments": {
                "op": "apply", "changes": [{"op": "add", "owner_card": target,
                                            "title": "接口回读验证", "next_result": "提交复跑结果"}]
            }}]}, kwargs["model_name"]
        if not item:
            result = json.loads(kwargs["prior_messages"][-1]["content"])
            item["id"] = result["changed"][0]
            assert result["rev"] == 1
            first = {"name": "coord_submit_instruction", "arguments": {
                "target_card": target, "item_id": item["id"], "instruction": "回读旧数据并提交复跑结果。",
                "acceptance": "报告包含真实命令、退出码和证据坐标。"}}
            second = {"name": "coord_submit_instruction", "arguments": {
                **first["arguments"], "target_card": reviewer, "instruction": "独立核对回读证据。"}}
            return {"tool_calls": [first, first, second]}, kwargs["model_name"]
        first, repeated, second = [m["content"] for m in kwargs["prior_messages"][-3:]]
        assert first == repeated and first != second
        return {"content": first + "\n" + second}, kwargs["model_name"]

    monkeypatch.setattr(message_runner, "deliver_now", deliver)
    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.setattr(loops, "runner_lane_for",
                        lambda work, card: recipient_loop if work == wid and card in (target, reviewer) else None)
    path = f"/api/works/{wid}/coordination/agent/chat"
    recipient_thread = threading.Thread(target=recipient_loop.run_forever, daemon=True)
    recipient_thread.start()
    try:
        response = await asyncio.wait_for(client.post(path, json={
            "assignment_id": aid, "expected_rev": 0, "content": f"请介入，让 {target} 复核旧数据回读。",
        }), timeout=15)
    finally:
        recipient_loop.call_soon_threadsafe(recipient_loop.stop)
        await asyncio.to_thread(recipient_thread.join)
        recipient_loop.close()
    assert response.status_code == 200, response.text
    path_panel = path.removesuffix("/chat")
    panel = (await client.get(path_panel)).json()
    actions = {action["target_card"]: action for action in panel["actions"]}
    assert set(actions) == {target, reviewer}
    action = actions[target]
    assert action["item_id"] == item["id"] and action["rev"] == 1
    assert all(entry["woke"] for entry in actions.values()) and wake == [target, reviewer]
    directive, = [m for m in await session_facade.read(wid, target) if m["actor"] == f"card:{AGENT}"]
    assert action["msg_id"] in directive["content"] and "真实命令、退出码和证据坐标" in directive["content"]
    reply = (await client.get(path)).json()["messages"][-1]["content"]
    assert action["receipt_id"] in reply and action["msg_id"] in reply and "已接纳" in reply
    assert actions[reviewer]["receipt_id"] in reply

    response = await client.post(path_panel, json={"assignment_id": aid, "expected_rev": 1,
                                                   "op": "set_mode", "mode": "observe"})
    assert response.status_code == 400, response.text
    assert len((await client.get(path_panel)).json()["actions"]) == 2 and wake == [target, reviewer]
    assert [m for m in await session_facade.read(wid, target) if m["actor"] == f"card:{AGENT}"] == [directive]


async def test_project_chat_continues_after_eighth_tool_round(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    targets = [await _mk_card(wid, f"接口查证 {index}") for index in range(1, 10)]
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    calls = 0

    async def model(system, user, **kwargs):
        nonlocal calls
        await kwargs["before_send"]()
        if calls:
            last_result = kwargs["prior_messages"][-1]
            assert last_result["role"] == "tool"
            assert json.loads(last_result["content"])["card_id"] == targets[calls - 1]
        calls += 1
        if calls <= len(targets):
            return {"tool_calls": [{"name": "coord_read_card", "arguments": {
                "card_id": targets[calls - 1],
            }}]}, kwargs["model_name"]
        return {"content": "九张卡的接口查证完毕。"}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    chat = f"/api/works/{wid}/coordination/agent/chat"
    response = await client.post(chat, json={"assignment_id": appt["assignment_id"],
                                            "expected_rev": appt["rev"], "content": "查证九张接口卡。"})
    assert response.status_code == 200, response.text
    assert calls == 10
    messages = (await client.get(chat)).json()["messages"]
    assert messages[-1]["content"] == "九张卡的接口查证完毕。"


@pytest.mark.parametrize("entry", ["chat", "inspection"])
async def test_plan_edit_refreshes_project_round_without_applying_a_stale_write(sandbox, monkeypatch, client, entry):
    wid, _, _ = await _seeded_work(monkeypatch)
    target = await _mk_card(wid, "接口责任卡")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    await coordination_store.save_plan(wid, AGENT, assignment_id=aid, expected_rev=0, plan={
        "items": [{"item_id": "check", "title": "接口查证", "owner_card": target}],
    })
    sent = []

    async def model(system, user, **kwargs):
        await kwargs["before_send"]()
        sent.append(kwargs["turn_tail"][-1]["content"])
        if len(sent) == 1:
            await coordination_store.board_apply(wid, AGENT, assignment_id=aid, expected_rev=1,
                changes=[{"op": "update", "item_id": "check", "next_result": "新的验收结果"}],
                by="user")
            await kwargs["before_send"]()
            return {"tool_calls": [{"name": "coord_plan_board", "arguments": {"op": "apply",
                "changes": [{"op": "update", "item_id": "check", "next_result": "盖回旧结果"}]}}]}, kwargs["model_name"]
        assert "新的验收结果" in sent[-1]
        assert "已过期" in kwargs["prior_messages"][-1]["content"]
        return {"content": "已核对更新后的计划。"}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    if entry == "chat":
        endpoint = f"/api/works/{wid}/coordination/agent/chat"
        response = await client.post(endpoint, json={"assignment_id": aid, "expected_rev": 0,
                                                     "content": "核对计划。"})
        assert response.status_code == 200, response.text
    else:
        result = await coordination_runner.run_strong_first_turn(wid, AGENT, assignment_id=aid)
        assert result["status"] == "parsed" and result["note"] == "已核对更新后的计划。"
    panel = coordination_store.panel(wid, AGENT)
    assert len(sent) == 2 and panel["rev"] == 2 and panel["control_seq"] == 0
    assert panel["board"]["items"][0]["next_result"] == "新的验收结果"


def test_legacy_action_table_migration_preserves_existing_receipt_and_accepts_two_actions():
    import sqlite3

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(coordination_runner._ACTIONS_DDL.removesuffix(")") + ", UNIQUE(assignment_id, turn_id))")
        insert = ("INSERT INTO coordination_actions"
                  " (work_id,plan_card_id,assignment_id,action_key,turn_id,rev,target_card,instruction,msg_id,receipt_id)"
                  " VALUES ('work','coordination','appointment',?,'turn',0,'card_001','检查',?,?)")
        conn.execute(insert, ("first", "cmsg-first", "receipt-first"))
        original = dict(conn.execute("SELECT * FROM coordination_actions").fetchone())
        coordination_runner._ensure_actions(conn)
        coordination_runner._ensure_actions(conn)
        assert dict(conn.execute("SELECT * FROM coordination_actions").fetchone()) == original
        conn.execute(insert, ("second", "cmsg-second", "receipt-second"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("first", "cmsg-third", "receipt-third"))


async def test_project_control_change_still_invalidates_a_queued_request(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    guard = coordination_runner._make_send_guard(wid, AGENT, aid, appt["rev"], control_seq=appt["control_seq"])
    await guard()
    paused = await coordination_store.control_op(wid, AGENT, assignment_id=aid, op="pause",
                                                   expected_rev=appt["rev"])
    await coordination_store.control_op(wid, AGENT, assignment_id=aid, op="resume",
                                         expected_rev=paused["appointment"]["rev"])
    with pytest.raises(coordination_runner._StaleCoordinationTurn, match="运行设置或授权已经变更"):
        await guard()


async def test_project_chat_restores_accepted_messages_without_journal(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    chat = f"/api/works/{wid}/coordination/agent/chat"
    timeline = f"/api/works/{wid}/cards/{AGENT}/timeline"
    sid = coordination_store.PROJECT_CHAT_SESSION
    payload = {"assignment_id": appt["assignment_id"], "expected_rev": 0}

    async def model(system, user, **kwargs):
        if user.startswith("失败"):
            raise RuntimeError("供应商拒绝")
        return {"content": f"回复{user}"}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    for content in ("失败一", "失败二"):
        response = await client.post(chat, json={**payload, "content": content})
        assert response.status_code == 502 and response.json()["accepted"] is True
    for content in ("发送一", "发送二"):
        assert (await client.post(chat, json={**payload, "content": content})).status_code == 200

    key = event_stream._key(wid, sid)
    event_stream._journals.pop(key, None)
    assert not event_stream.journal_of(wid, sid)
    expected = ["失败一", "失败二", "发送一", "回复发送一", "发送二", "回复发送二"]
    assert [m["content"] for m in (await client.get(chat)).json()["messages"]] == expected
    page = (await client.get(timeline, params={"session": sid, "tail": 4})).json()
    assert page["has_earlier"] and [m["data"]["content"] for m in page["items"]] == expected[2:]
    earlier = (await client.get(timeline, params={"session": sid, "before": page["first_seq"]})).json()
    assert [m["data"]["content"] for m in earlier["items"]] == expected[:2]


async def test_project_chat_reports_an_accepted_instruction_with_unconfirmed_delivery(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    target = await _mk_card(wid, "接口责任卡")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    await coordination_store.save_plan(wid, AGENT, assignment_id=aid, expected_rev=0, plan={
        "items": [{"item_id": "intake", "title": "验证接口", "owner_card": target}],
    })

    async def model(system, user, **kwargs):
        if not user:
            return {"content": kwargs["prior_messages"][-1]["content"]}, kwargs["model_name"]
        return {"tool_calls": [{"name": "coord_submit_instruction", "arguments": {
            "target_card": target, "item_id": "intake", "instruction": "复跑接口验证。",
            "acceptance": "回报实际退出码。",
        }}]}, kwargs["model_name"]

    async def broken_handoff(*args, **kwargs):
        raise RuntimeError("投递阶段错误")

    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.setattr(coordination_runner, "coordination_deliver", broken_handoff)
    endpoint = f"/api/works/{wid}/coordination/agent"
    response = await client.post(endpoint + "/chat", json={"assignment_id": aid, "expected_rev": 1,
                                                           "content": f"请 {target} 复跑接口验证。"})
    assert response.status_code == 200, response.text
    messages = (await client.get(endpoint + "/chat")).json()["messages"]
    assert "已受理，仍在等待补投或确认收件" in messages[-1]["content"]
    action, = (await client.get(endpoint)).json()["actions"]
    assert action["status"] == "待投" and not action["msg_id"]


async def test_inspection_handoff_then_archive_and_sync_final_decision(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口实施")
    root = next(cid for cid, info in card_store.iter_card_metas(wid).items()
                if info.get("card_type") == "project")
    await session_facade.append(wid, domain, {
        "actor": "human", "content": "接口负责人确认，旧数据回读失败，需要复验。",
    }, emit_event=False)
    configured = await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"])
    aid = configured["appointment"]["assignment_id"]
    configured = await coordination_store.save_plan(
        wid, AGENT, assignment_id=aid, expected_rev=0,
        plan={"objective": "按需求实施", "items": [
            {"item_id": "intake", "title": "完成接口兼容", "owner_card": domain}]}, by="strong")
    rev = configured["appointment"]["rev"]
    live = await coordination_store.control_op(
        wid, AGENT, assignment_id=aid, expected_rev=rev, op="set_mode", mode="live")
    assert live["appointment"]["mode"] == "live"
    _install_inspection(wid, aid, domain, check="关注旧数据回读", wake_when="复验失败时唤醒")

    weak_checks = []
    first_read = asyncio.Event()
    finish_read = asyncio.Event()
    strong_started = asyncio.Event()
    finish_strong = asyncio.Event()
    coord = f"event:{wid}:{domain}:900"
    marker = "接口负责人确认，旧数据回读失败，需要复验。"
    parent_marker = "用户裁定旧数据不可清理。"
    memory_marker = "项目历史决策：数据迁移必须可回滚。"
    main_session = coordination_store.PROJECT_CHAT_SESSION
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    await session_facade.append_many(wid, main_session, [
        {"actor": "human", "content": "旧的已凝练对话", "coordination_scope": {
            "kind": "user_chat", "assignment_id": aid}},
        {"actor": "assistant", "content": parent_marker, "coordination_scope": {
            "kind": "user_chat", "assignment_id": aid}},
    ], emit_event=False)
    blocks = await session_facade.read(wid, main_session)
    await memory_store.save_memory(wid, main_session, {
        "mark": blocks[1]["seq"], "entries": [{"gist": memory_marker, "level": 1,
                                               "seq": blocks[0]["seq"],
                                               "coveredSeqs": [blocks[0]["seq"]]}],
    })
    expansion = await coordination_runner._project_memory_read(
        wid, "memory_expand", {"seq": blocks[0]["seq"]})
    assert "旧的已凝练对话" in expansion
    assert "该记忆工具只读取统筹官主对话" in await coordination_runner._project_memory_read(
        wid, "memory_expand", {"seq": blocks[0]["seq"], "card_id": domain})
    received = []

    async def no_target_model(work_id, card_id, *args, **kwargs):
        received.append(card_id)
        return False

    async def model(system, user, **kw):
        await kw["before_send"]()
        assert memory_marker in system
        assert any(parent_marker in message["content"] for message in kw["prior_messages"])
        assert all("旧的已凝练对话" not in message["content"] for message in kw["prior_messages"])
        if kw["caller"] == "coordination_weak":
            assert kw["model_name"] == "weak-test-route"
            assert "关注旧数据回读" in system and "复验失败时唤醒" in system
            assert {definition["name"] for definition in kw["tools"]} == {
                "coord_read_card", "coord_read_events", "coord_read_dialogue", "coord_wake_strong",
                "read_memory", "memory_expand",
            }
            weak_checks.append(system)
            if len(weak_checks) == 1:
                first_read.set()
                await finish_read.wait()
                result = {"name": "coord_read_dialogue", "arguments": {"card_id": domain}}
            else:
                tool_reply = kw["prior_messages"][-1]
                assert tool_reply["role"] == "tool" and marker in tool_reply["content"]
                result = {"name": "coord_wake_strong", "arguments": {
                    "item_id": "intake", "reason": "旧数据回读不符合要求",
                    "evidence_refs": [f"{wid}:{domain}:1234", "inspect:1:seq1234"],
                    "question": "是否需要安排回读复验？"}}
            return {"tool_calls": [result]}, kw["model_name"]
        assert kw["caller"] == "coordination_strong" and kw["model_name"] == "strong-test-route"
        assert all(name not in system for name in ("coord_ask_function", "coord_qa_request", "direct_nodes"))
        if user:
            assert marker not in user
            assert any(message["role"] == "tool" and marker in message["content"]
                       for message in kw["prior_messages"])
            assert "旧数据回读不符合要求" in kw["prior_messages"][-1]["content"]
            strong_started.set()
            await finish_strong.wait()
            return {"tool_calls": [
                {"name": "coord_read_card", "arguments": {"card_id": domain}},
                {"name": "coord_read_dialogue", "arguments": {"card_id": domain}},
            ]}, kw["model_name"]
        assert not user and kw["prior_messages"][-1]["role"] == "tool"
        if "已向" in kw["prior_messages"][-1]["content"]:
            return {"content": "安排接口回读复验。"}, kw["model_name"]
        assert kw["prior_messages"][-2]["role"] == "tool"
        assert marker in kw["prior_messages"][-1]["content"]
        assert "coord_submit_instruction" in {tool["name"] for tool in kw["tools"]}
        return {"tool_calls": [{
            "name": "coord_submit_instruction",
            "arguments": {"item_id": "intake", "target_card": root, "instruction": "安排验证旧数据回读。",
                          "acceptance": "提交一次实际回读的结果", "evidence_refs": [coord],
                          "note": "安排接口回读复验。"},
        }]}, kw["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.setattr(message_runner, "deliver_now", no_target_model)
    monkeypatch.setattr(coordination_watcher, "_maybe_dispatch", lambda *args: None)
    fired = policy_runner.feed(wid, {"type": "card_block_edited", "seq": 900, "card_id": domain,
        "data": {"block": "status"}, "ts": datetime.now(timezone.utc).isoformat()})
    assert len(fired) == 1
    await policy_runner.drain()
    batch_id = card_db.read_sync(wid, lambda conn: conn.execute(
        "SELECT batch_id FROM coordination_batches WHERE assignment_id=?", (aid,)).fetchone()[0], None)
    inspect = asyncio.create_task(coordination_watcher.run_owned_batch(wid, AGENT, aid, batch_id))
    endpoint = f"/api/works/{wid}/coordination/agent"
    try:
        await asyncio.wait_for(first_read.wait(), 5)
        active = (await client.get(f"{endpoint}/runs?active=1")).json()["runs"]
        assert len(active) == 1 and active[0]["phase"] == "checking"
        run_id = active[0]["run_id"]
        assert session_facade.turn_holder(wid, f"{AGENT}@{run_id}") == "inspection"
        finish_read.set()
        outcome = await asyncio.wait_for(inspect, 5)
        assert outcome["status"] == "wake" and outcome["run_id"] == run_id, outcome
        assert outcome["evidence_refs"] == [coord, "inspect:1"]
        await asyncio.wait_for(strong_started.wait(), 5)
        active = (await client.get(f"{endpoint}/runs?active=1")).json()["runs"]
        assert [(run["run_id"], run["phase"]) for run in active] == [(run_id, "working")]
        assert session_facade.turn_holder(wid, f"{AGENT}@{run_id}") == "weak_wake"
        assert [msg["content"] for msg in (await client.get(f"{endpoint}/chat")).json()["messages"]] == [
            "旧的已凝练对话", parent_marker]
    finally:
        finish_read.set()
        finish_strong.set()
        await inspect
        await asyncio.gather(*tuple(event_log._pending), return_exceptions=True)

    archive = (await client.get(f"{endpoint}/runs/{run_id}")).json()
    assert archive["phase"] == "archived"
    assert not session_facade.turn_holder(wid, f"{AGENT}@{run_id}")
    assert (await client.get(f"{endpoint}/runs?active=1")).json()["runs"] == []
    actors = [entry["actor"] for entry in archive["messages"]]
    assert actors == ["inspection_call", "inspection_result", "inspection_call",
                      "inspection_conclusion", "inspection_result", "strong_update",
                      "strong_tool_call", "strong_tool_result", "strong_tool_call", "strong_tool_result",
                      "strong_tool_call", "strong_tool_result", "strong_step", "strong_conclusion"]
    assert marker in archive["messages"][1]["content"]
    assert (archive["messages"][0]["tool_calls"][0]["id"]
            == archive["messages"][1]["tool_call_id"])
    assert (archive["messages"][2]["tool_calls"][0]["id"]
            == archive["messages"][4]["tool_call_id"])
    assert coord in archive["messages"][5]["content"]
    for index in (6, 8, 10):
        assert (archive["messages"][index]["tool_calls"][0]["id"]
                == archive["messages"][index + 1]["tool_call_id"])
    parent = (await client.get(f"{endpoint}/chat")).json()["messages"]
    assert len(parent) == 3 and parent[-1]["actor"] == "decision" and parent[-1]["run_id"] == run_id
    assert marker not in parent[-1]["content"] and "安排接口回读复验" in parent[-1]["content"]
    blocks = await session_facade.read(wid, root)
    directives = [block for block in blocks if block.get("from") == AGENT]
    assert len(directives) == 1 and "安排验证旧数据回读" in directives[0]["content"]
    assert received == [root]


async def test_inspection_pass_archives_without_contacting_strong_model(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口")
    configured = await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"])
    aid = configured["appointment"]["assignment_id"]
    calls = []

    async def model(system, user, **kwargs):
        calls.append(kwargs["caller"])
        if calls == ["coordination_strong"]:
            assert "design_policy" in {tool["name"] for tool in kwargs["tools"]}
            return {"tool_calls": [{"name": "design_policy", "arguments": {
                "op": "save", "id": "verify", "check": "检查接口回读", "card_ids": [domain],
                "trigger": "card_block_edited", "wake_when": "接口回读失败时唤醒",
            }}]}, kwargs["model_name"]
        if kwargs["caller"] == "coordination_strong":
            return {"content": '{"decision":"已配置接口回读巡检。"}'}, kwargs["model_name"]
        assert kwargs["caller"] == "coordination_weak"
        assert "检查接口回读" in system and "接口回读失败时唤醒" in system
        if user:
            return {"tool_calls": [{"name": "coord_read_card",
                                    "arguments": {"card_id": domain}}]}, kwargs["model_name"]
        return {"content": '{"decision":"pass","reason":"无异常"}'}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    start = await coordination_runner.run_strong_first_turn(wid, AGENT, assignment_id=aid)
    assert start["status"] == "parsed"
    assert await coordination_runner.run_weak_observation(
        wid, AGENT, {"changes": [], "policy_rev": 1}) == {"status": "skipped"}
    endpoint = f"/api/works/{wid}/coordination/agent"
    chat = (await client.get(f"{endpoint}/chat")).json()["messages"]
    result = await coordination_runner.run_weak_observation(wid, AGENT, {"changes": []})
    assert result["status"] == "passed"
    archived = (await client.get(f"{endpoint}/runs/{result['run_id']}")).json()
    assert archived["phase"] == "archived"
    assert [msg["actor"] for msg in archived["messages"]] == [
        "inspection_call", "inspection_result", "inspection_conclusion"]
    assert (await client.get(f"{endpoint}/chat")).json()["messages"] == chat
    assert calls == ["coordination_strong", "coordination_strong", "coordination_weak", "coordination_weak"]


async def test_pending_inspection_cannot_take_over_after_policy_changes(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口验证")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    rule = {"inspection_id": "verify", "watch": ["card_block_edited"], "check": "查接口"}
    plan = {"inspection_plan": [rule], "wake_standards": [{"standard_id": "verify", "escalate": "缺证据"}]}
    await coordination_store.save_plan(wid, AGENT, assignment_id=aid, expected_rev=0, plan=plan)
    _install_inspection(wid, aid, domain)
    policy, = coordination_policy.policies(wid, aid)
    batch_id = "handoff-before-replacement"
    calls = []

    async def model(system, user, **kwargs):
        calls.append(kwargs["caller"])
        if len(calls) == 1:
            return {"tool_calls": [{"name": "coord_read_card", "arguments": {"card_id": domain}}]}, kwargs["model_name"]
        if len(calls) == 2:
            return {"tool_calls": [{"name": "coord_wake_strong", "arguments": {
                "reason": "需核对接口", "evidence_refs": ["inspect:1"],
            }}]}, kwargs["model_name"]
        return {"content": '{"decision":"旧提请被执行"}'}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    result = await coordination_runner.run_weak_observation(
        wid, AGENT, {"batch_id": batch_id, "changes": [], "policy_rev": 1,
                     "policy_bindings": {policy["id"]: coordination_policy.binding(policy["then"])}})
    assert result["status"] == "wake"
    _install_inspection(wid, aid, domain, wake_when="只看正式失败")
    result = await coordination_watcher.run_owned_turn(wid, AGENT, aid, "weak_wake", batch_id)
    assert result["status"] == "skipped"
    assert calls == ["coordination_weak", "coordination_weak"]
    assert not coordination_store.list_runs(wid, active_only=True)
    assert not any(wake.get("batch_id") == batch_id for wake in coordination_store.panel(wid, AGENT)["pending"])


async def test_old_project_review_deadline_cannot_start_a_strong_turn(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    due = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old_wakes = [{"kind": "review_due", "due_at": due}, {"kind": "appoint", "request_id": aid}]
    await card_db.run_write(wid, lambda conn: conn.execute(
        "UPDATE coordination_appointments SET next_review_at=?, pending=? WHERE assignment_id=?",
        (due, json.dumps(old_wakes), aid)))
    run_id = "run_" + "c" * 32
    await coordination_store.set_run_phase(wid, aid, run_id, trigger="review_due", phase="working")
    spawned = []
    monkeypatch.setattr(coordination_watcher, "_spawn_strong_turn", lambda *args: spawned.append(args))
    for _ in range(2):
        assert coordination_watcher.tick(wid, project_only=True)["review_spawned"] == 0
    assert not spawned
    assert (await coordination_watcher.run_owned_turn(wid, AGENT, aid, "review_due", due))["status"] == "skipped"
    appt = coordination_store.panel(wid, AGENT)
    assert not appt["pending"] and not appt["overdue"] and not appt["next_review_at"]
    assert coordination_store.list_runs(wid, active_only=True) == []
    assert coordination_store.get_run(wid, run_id)["phase"] == "archived"


async def test_idle_sweep_preserves_report_interval_after_process_restart(sandbox, monkeypatch):
    from src.infra import fact_store, idle_sweeper, procs

    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "长期静默卡")
    base = datetime.now(timezone.utc)
    old = (base - timedelta(hours=12)).isoformat()
    recent = (base - timedelta(minutes=25)).isoformat()
    facts = fact_store.store()
    facts.ingest(wid, {"type": "card_block_edited", "seq": 1, "card_id": domain, "ts": old})
    facts.ingest(wid, {"type": "card_idle", "seq": 2, "card_id": domain, "ts": recent})
    clock = [base.timestamp()]
    emitted = []
    monkeypatch.setattr(idle_sweeper, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(idle_sweeper, "_last_emit", {})
    monkeypatch.setattr(card_store, "iter_card_metas", lambda work: {domain: {"created_at": old}} if work == wid else {})
    monkeypatch.setattr(procs, "owner_index_of", lambda *args: procs.runner_index())
    monkeypatch.setattr(event_stream, "emit", lambda *args: emitted.append(args))
    assert await idle_sweeper._sweep_once() == 0 and emitted == []
    clock[0] += 36 * 60
    assert await idle_sweeper._sweep_once() == 1
    assert [(work, card, kind) for work, card, kind, _ in emitted] == [(wid, domain, "card_idle")]


async def test_native_harness_ignores_events_before_installation(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口验证")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    _install_inspection(wid, appt["assignment_id"], domain)
    fired = policy_runner.feed(wid, {"type": "card_block_edited", "card_id": domain, "seq": 1,
        "ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "data": {"block": "status"}})
    assert not fired
    assert coordination_store.list_runs(wid) == []


async def test_invalid_inspection_handoff_keeps_the_batch_for_review(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口验证")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    _install_inspection(wid, aid, domain)

    async def model(system, user, **kwargs):
        if user:
            call = {"name": "coord_read_card", "arguments": {"card_id": domain}}
        elif "为什么需要强模型接手" in kwargs["prior_messages"][-1]["content"]:
            return {"content": ""}, kwargs["model_name"]
        else:
            call = {"name": "coord_wake_strong", "arguments": {"evidence_refs": ["inspect:1"]}}
        return {"tool_calls": [call]}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    result = await coordination_runner.run_weak_observation(wid, AGENT, {"batch_id": "missing-reason", "changes": []})
    assert result["status"] == "unjudged"
    assert any(p.get("batch_id") == "missing-reason" and p["kind"] == "unjudged_batch"
               for p in coordination_store.panel(wid, AGENT)["pending"])
    assert any(entry.get("batch_id") == "missing-reason" and entry["status"] == "unjudged"
               for entry in coordination_store.list_observations(wid, AGENT))


async def test_project_inspection_preserves_prose_without_treating_it_as_a_wake(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    domain = await _mk_card(wid, "接口验证")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    _install_inspection(wid, aid, domain)

    cases = [
        ("clear", "**巡检结论：无需介入。**\n接口责任卡仍在复跑。", "passed", 9),
        ("ambiguous", "接口验证还缺一份签收。", "passed", 0),
        ("prose-wake", "应该叫强模型接手，接口验证仍未签收。", "passed", 1),
        ("json-wake", '{"decision":"wake","reason":"接口验证仍未签收"}', "passed", 1),
    ]
    for batch, answer, expected, reads in cases:
        calls = []

        async def model(system, user, **kwargs):
            assert "巡检员" in system and "无需介入" in system
            calls.append(kwargs["caller"])
            if user.startswith("正文没有发起接手请求"):
                return {"content": "接口验证仍未签收，暂由责任卡继续跟进。"}, kwargs["model_name"]
            response = ({"tool_calls": [{"name": "coord_read_events", "arguments": {
                "card_id": domain, "tail": len(calls),
            }}]} if len(calls) <= reads else {"content": answer})
            return response, kwargs["model_name"]

        monkeypatch.setattr(model_service, "call_main", model)
        result = await coordination_runner.run_weak_observation(wid, AGENT, {"batch_id": batch, "changes": []})
        assert result["status"] == expected
        assert calls == ["coordination_weak"] * (reads + 1 + (batch == "json-wake"))
        blocks = await coordination_runner.read_coordination_run(wid, result["run_id"])
        conclusion = "接口验证仍未签收，暂由责任卡继续跟进。" if batch == "json-wake" else answer
        assert conclusion in blocks[-1]["content"] and "非法 JSON" not in blocks[-1]["content"]
    assert not any(p["kind"] == "weak_wake" for p in coordination_store.panel(wid, AGENT)["pending"])


async def test_project_does_not_dispatch_an_action_written_as_text(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    appointment = (await coordination_store.configure_project_agent(
        wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appointment["assignment_id"]
    await coordination_store.control_op(
        wid, AGENT, assignment_id=aid, expected_rev=0, op="set_mode", mode="live")

    async def model(system, user, **kwargs):
        if "正文里的操作没有执行" in user:
            return {"content": "本轮没有实际发令。"}, kwargs["model_name"]
        return {"content": '{"action":"coord_submit_instruction","instruction":"派工"}'}, kwargs["model_name"]

    async def unexpectedly_dispatched(*args, **kwargs):
        raise AssertionError("模型正文被当成工具执行")

    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.setattr(coordination_runner, "coordination_intake", unexpectedly_dispatched)
    result = await coordination_runner.run_strong_first_turn(wid, AGENT, assignment_id=aid)
    assert result["status"] == "parsed" and result["note"] == "本轮没有实际发令。"
    assert not coordination_runner.board_summary(wid, AGENT)["actions"]


async def test_project_inspection_can_direct_after_twelve_model_calls(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    target = await _mk_card(wid, "接口责任卡")
    configured = await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"])
    aid = configured["appointment"]["assignment_id"]
    saved = await coordination_store.save_plan(wid, AGENT, assignment_id=aid, expected_rev=0,
        plan={"items": [{"item_id": "check", "title": "核查接口", "owner_card": target}]})
    run_id = "run_" + "d" * 32
    await coordination_store.set_run_phase(wid, aid, run_id, phase="waiting", trigger="inspection")
    model_calls = []
    received = []

    async def model(system, user, **kwargs):
        await kwargs["before_send"]()
        assert "提交 plan" not in user
        model_calls.append(kwargs["caller"])
        number = len(model_calls)
        if number <= 13:
            name, args = "coord_read_events", {"card_id": target, "tail": number}
        elif number <= 15:
            if number == 15:
                assert "证据引用不属于本任命" in kwargs["prior_messages"][-1]["content"]
            name, args = "coord_submit_instruction", {
                "item_id": "check", "target_card": target, "instruction": "核对接口并回报。",
                "acceptance": "提供复跑结果。",
                "evidence_refs": [f"event:{wid}:{target}:999999"] if number == 14 else [],
            }
        else:
            assert "已向" in kwargs["prior_messages"][-1]["content"]
            return {"content": "已安排接口复验。"}, kwargs["model_name"]
        calls = [{"name": name, "arguments": args}]
        if number == 15:
            calls.insert(0, {"name": "coord_plan_board", "arguments": {"op": "apply",
                "changes": [{"op": "update", "item_id": "check", "priority": 25}]}})
        return {"tool_calls": calls}, kwargs["model_name"]

    async def deliver(work_id, card_id, *args, **kwargs):
        assert work_id == wid and kwargs["admission"]()
        received.append(card_id)
        return True

    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.setattr(message_runner, "deliver_now", deliver)
    result = await coordination_runner.run_strong_first_turn(wid, AGENT, assignment_id=aid,
        trigger="weak_wake", reason={"run_id": run_id, "policy_rev": saved["appointment"]["rev"]})
    assert result["status"] == "parsed" and result["receipt_id"] and result["run_id"] == run_id
    assert model_calls == ["coordination_strong"] * 16 and received == [target]
    action, = coordination_runner.board_summary(wid, AGENT)["actions"]
    assert action["rev"] == coordination_store.panel(wid, AGENT)["rev"] == 2
    directives = [b for b in await session_facade.read(wid, target) if b["actor"] == f"card:{AGENT}"]
    assert len(directives) == 1 and result["instruction"]["msg_id"] in directives[0]["content"]
    assert result["receipt_id"] in (await coordination_runner.read_user_chat(wid, AGENT))[-1]["content"]
    assert coordination_store.get_run(wid, run_id)["phase"] == "archived"


async def test_strong_chat_authors_cross_card_harness_policy(sandbox, monkeypatch, client):
    from src.api import model_config
    from src.infra import policy_runner

    wid, _, _ = await _seeded_work(monkeypatch)
    implementation = await _mk_card(wid, "负责接口实施")
    dependency = await _mk_card(wid, "负责兼容验证")
    unrelated = await _mk_card(wid, "无关卡")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    assert not appt["next_review_at"]
    await coordination_store.consume_wake(wid, aid, "appoint", aid)
    strategy = {"id": "handoff-risk", "trigger": "card_stopped_self", "card_ids": [dependency],
                "observe": ["worker_done"], "check": "接口完工后，验证卡是否仍停滞",
                "wake_when": "停滞阻塞验收时", "window": "2h", "when": {
                    "event": {"types": ["worker_done"], "where": {"from": implementation}}}}
    issued = []
    insights = []

    async def model(system, user, **kwargs):
        await kwargs["before_send"]()
        assert kwargs["model_name"] == "strong-test-route"
        assert "你是项目" in system and wid in system
        if user:
            return {"tool_calls": [
                {"name": "coord_read_card", "arguments": {"card_id": dependency}},
                {"name": "design_policy", "arguments": {"op": "save", **strategy}},
            ]}, kwargs["model_name"]
        assert dependency in kwargs["prior_messages"][-2]["content"]
        assert json.loads(kwargs["prior_messages"][-1]["content"])["saved"]
        return {"content": "已保存巡检策略。"}, kwargs["model_name"]

    async def inspect(work_id, plan_id, batch):
        insights.extend(batch["changes"])
        return {"status": "passed"}

    monkeypatch.setattr(model_service, "call_main", model)
    monkeypatch.setattr(coordination_runner, "run_weak_observation", inspect)
    now = datetime.now(timezone.utc)

    async def event(seq, card, kind, **data):
        issued.append(policy_runner.feed(wid, {
            "seq": seq, "card_id": card, "type": kind, "data": data,
            "ts": (now + timedelta(seconds=seq)).isoformat(),
        }))
        await policy_runner.drain()
        for _ in range(40):
            if not coordination_watcher._weak_dispatching:
                break
            await asyncio.sleep(0.01)

    await event(1, dependency, "card_idle", silent_sec=90000)
    assert not insights
    endpoint = f"/api/works/{wid}/coordination/agent"
    response = await client.post(endpoint + "/chat", json={
        "assignment_id": aid, "expected_rev": 0, "content": "配置接口和验证卡的联合巡检。"})
    assert response.status_code == 200, response.text
    assert not (await client.get(endpoint)).json()["next_review_at"]
    await event(3, implementation, "worker_done", **{"from": implementation})
    await event(4, unrelated, "card_idle", silent_sec=90000)
    assert not insights
    with monkeypatch.context() as removed_model:
        removed_model.setattr(model_config, "load", lambda: {"models": {"weak-test-route": {}}})
        await event(5, dependency, "card_stopped_self", mode="stop", by=dependency)
        assert not insights
        assert (await client.get(endpoint)).json()["inspection_status"] == "missing_models"
    await event(6, implementation, "worker_done", **{"from": implementation})
    await event(7, dependency, "card_stopped_self", mode="stop", by=dependency)
    assert [(change["seq"], change["inspection_ids"]) for change in insights] == [
        (7, [f"inspection/{wid}/handoff-risk"])]
    messages = (await client.get(endpoint + "/chat")).json()["messages"]
    assert [message["actor"] for message in messages] == ["human", "assistant"]


async def test_inspection_tool_exposes_supported_events_and_allows_retry(sandbox, monkeypatch, client):
    wid, _, _ = await _seeded_work(monkeypatch)
    watched = await _mk_card(wid, "接口实施")
    receiving = await _mk_card(wid, "验收")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    await coordination_store.consume_wake(wid, aid, "appoint", aid)
    rule = {"id": "finish", "card_ids": [watched], "trigger": "worker_done", "check": "核对接口复跑证据"}
    wake_when = "完工或停手后仍缺正式复跑证据时介入"

    async def model(system, user, **kwargs):
        await kwargs["before_send"]()
        tool = next(tool for tool in kwargs["tools"] if tool["name"] == "design_policy")
        events = tool["parameters"]["properties"]["trigger"]["enum"]
        assert "worker_done" in events and "card_idle" in events
        if user:
            args = {"op": "preview", **rule, "trigger": "card_idle"}
        else:
            result = kwargs["prior_messages"][-1]["content"]
            if result.startswith("{") and json.loads(result).get("saved"):
                return {"content": "已保存接口巡检。"}, kwargs["model_name"]
            assert coordination_policy.policies(wid, aid) == []
            if result.startswith("{"):
                trial = json.loads(result)["trial"]
                assert trial["would_fire"] == 1
                hit, = trial["recent_matches"]
                assert (hit["recorded_on_card"], hit["matched_card"], hit["matched_on"]) == (receiving, watched, "from")
                args = {"op": "save", **rule}
            else:
                assert "idle_for" in result
                args = {"op": "preview", **rule}
        return {"tool_calls": [{"name": tool["name"], "arguments": {
            **args, "wake_when": wake_when,
        }}]}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    assert not policy_runner.feed(wid, {"type": "worker_done", "card_id": receiving, "seq": 1,
        "ts": datetime.now(timezone.utc).isoformat(), "data": {"from": watched}})
    response = await client.post(f"/api/works/{wid}/coordination/agent/chat", json={
        "assignment_id": aid, "expected_rev": 0, "content": "安排接口巡检。"})
    assert response.status_code == 200, response.text
    saved, = coordination_policy.policies(wid, aid)
    assert saved["watch"] == ["worker_done"] and saved["then"]["wake_when"] == wake_when
    assert policy_set.dispatcher().policy(saved["id"]).then["mode"] == "inspect"
    assert coordination_store.panel(wid, AGENT)["rev"] == 0
    results = [frame["data"]["ok"] for frame in event_stream.journal_of(
        wid, coordination_store.PROJECT_CHAT_SESSION) if frame["type"] == "tool_result"]
    assert results == [False, True, True]


async def test_harness_deduplicates_idle_episode_and_discards_disabled_action(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    target = await _mk_card(wid, "待核查卡")
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    monkeypatch.setattr(coordination_watcher, "_maybe_dispatch", lambda *args: None)
    _install_inspection(wid, aid, target, id="other")
    idle = {"id": "idle", "trigger": "card_idle", "idle_for": "12h"}
    _install_inspection(wid, aid, target, **idle)
    now = datetime.now(timezone.utc)

    async def sample(seq, silent, delta=0):
        fired = policy_runner.feed(wid, {"card_id": target, "seq": seq, "type": "card_idle",
            "ts": (now + timedelta(seconds=seq)).isoformat(), "data": {
                "silent_sec": silent, "silent_since": now.timestamp() - 50000 + delta, "card_type": "worker"}})
        await policy_runner.drain()
        return fired

    assert await sample(100, 40000) == []
    first, = await sample(101, 50000)
    repeat, = await sample(102, 51000)
    assert first["_actions"] == repeat["_actions"]
    action_id = first["_actions"][AGENT]
    coordination_policy.handle(wid, aid, {"op": "save", "id": "idle", "active": False}, {target})
    assert (await coordination_watcher.run_owned_batch(wid, AGENT, aid, f"harness_{action_id}"))["status"] == "filtered"
    assert await sample(103, 52000) == []
    assert policy_set.dispatcher().policy(f"inspection/{wid}/other").active
    _install_inspection(wid, aid, target, **idle)
    new, = await sample(104, 49000, delta=1000)
    assert new["_actions"] != first["_actions"]


async def test_five_idle_matches_share_one_live_inspection_and_one_takeover(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    cards = [await _mk_card(wid, f"责任卡 {i}") for i in range(5)]
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    coordination_policy.handle(wid, aid, {"op": "save", "id": "idle", "trigger": "card_idle",
        "card_ids": cards, "idle_for": "12h", "check": "核对停滞", "wake_when": "确认停滞才接手"}, set(cards))
    monkeypatch.setattr(coordination_watcher, "_maybe_dispatch", lambda *args: None)
    spawned = []
    monkeypatch.setattr(coordination_watcher, "_spawn_strong_turn", lambda *args: spawned.append(args[-1]))
    first_weak, release_weak = asyncio.Event(), asyncio.Event()
    first_strong, release_strong = asyncio.Event(), asyncio.Event()
    weak_inputs, strong_inputs = [], []
    actions = []
    now = datetime.now(timezone.utc)

    async def fire(i):
        fired, = policy_runner.feed(wid, {"card_id": cards[i], "seq": 101, "type": "card_idle",
            "ts": (now + timedelta(seconds=i)).isoformat(), "data": {
                "silent_sec": 50000, "silent_since": now.timestamp() - 50000, "card_type": "worker"}})
        actions.append(fired["_actions"][AGENT])
        await policy_runner.drain()

    def evidence(i):
        return f"event:{wid}:{cards[i]}:101"

    async def model(system, user, **kwargs):
        if kwargs["caller"] == "coordination_weak":
            weak_inputs.append(user)
            if len(weak_inputs) == 1:
                first_weak.set()
                await release_weak.wait()
                return {"content": "首条事件暂时无需接手。"}, kwargs["model_name"]
            assert evidence(1) in user and evidence(2) in user
            return {"tool_calls": [{"name": "coord_wake_strong", "arguments": {
                "reason": "三张责任卡需要统筹查证"}}]}, kwargs["model_name"]
        strong_inputs.append(user)
        if len(strong_inputs) == 1:
            first_strong.set()
            await release_strong.wait()
            material = user.split("同一巡检收到的新证据：", 1)[1]
            assert all(evidence(i) in material for i in range(4))
            return {"content": "此前四条已核对。"}, kwargs["model_name"]
        assert evidence(4) in user and evidence(0) not in user
        return {"content": "五条静默事件已核对。"}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    await fire(0)
    batch_id = f"harness_{actions[0]}"
    weak = asyncio.create_task(coordination_watcher.run_owned_batch(wid, AGENT, aid, batch_id))
    try:
        await asyncio.wait_for(first_weak.wait(), 30)
        await fire(1)
        await fire(2)
    finally:
        release_weak.set()
    judged = await asyncio.wait_for(weak, 30)
    assert judged["status"] == "wake" and spawned == [batch_id]
    await fire(3)
    strong = asyncio.create_task(coordination_watcher.run_owned_turn(wid, AGENT, aid, "weak_wake", batch_id))
    try:
        await asyncio.wait_for(first_strong.wait(), 30)
        await fire(4)
    finally:
        release_strong.set()
    decided = await asyncio.wait_for(strong, 30)
    assert decided["status"] == "parsed" and decided["note"] == "五条静默事件已核对。"
    assert decided["run_id"] == judged["run_id"]
    assert await coordination_watcher.enqueue_harness_inspection(
        wid, AGENT, f"inspection/{wid}/idle", actions[1]) == batch_id
    rows = card_db.read_sync(wid, lambda conn: conn.execute(
        "SELECT batch_id,changes,status,accepting FROM coordination_batches ORDER BY id").fetchall(), [])
    row, = rows
    assert [change["coord"] for change in json.loads(row["changes"])] == [evidence(i) for i in range(5)]
    assert row["status"] == "sent" and row["accepting"] == 0 and len(set(actions)) == 5
    assert len(weak_inputs) == len(strong_inputs) == 2
    assert len(coordination_store.list_runs(wid)) == 1


async def test_native_reports_group_by_explicit_item_across_policies(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    owner, worker = [await _mk_card(wid, title) for title in ("收口卡", "执行卡")]
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    await coordination_store.save_plan(wid, AGENT, assignment_id=aid, expected_rev=0,
        plan={"items": [{"item_id": "it_same", "title": "联验", "owner_card": owner},
                        {"item_id": "it_new", "title": "其他事项", "owner_card": owner}]})
    monkeypatch.setattr(coordination_watcher, "_maybe_dispatch", lambda *args: None)
    spawned = []
    monkeypatch.setattr(coordination_watcher, "_spawn_strong_turn", lambda *args: spawned.append(args[-1]))
    _install_inspection(wid, aid, worker, id="report", trigger="msg_report")
    _install_inspection(wid, aid, worker, id="proof", trigger="worker_done")
    for kind, item in (("worker_done", "it_same"), ("msg_report", "it_same"), ("msg_report", "it_new")):
        event_log.append_sync(wid, owner, {"type": kind, "data": {
            "from": worker, "content": f"计划板事项 {item} 的回执"}})
        await policy_runner.drain()
    groups = card_db.read_sync(wid, lambda conn: [json.loads(row[0]) for row in conn.execute(
        "SELECT changes FROM coordination_batches ORDER BY id")], [])
    assert [[change["item_id"] for change in group] for group in groups] == [["it_same", "it_same"], ["it_new"]]
    assert {change["inspection_ids"][0].split("/")[-1] for change in groups[0]} == {"proof", "report"}

    first_id = card_db.read_sync(wid, lambda c: c.execute(
        "SELECT batch_id FROM coordination_batches ORDER BY id LIMIT 1").fetchone()[0], "")
    inputs = []

    async def model(system, user, **kwargs):
        inputs.append(user)
        if kwargs["caller"] == "coordination_strong":
            assert "巡检补充结论" in user and "新回执确认范围" in user
            return {"content": "联验事项收口。"}, kwargs["model_name"]
        if len(inputs) == 2:
            assert "已在排队或处理的统筹事项" in user and "it_same" in user
        return {"tool_calls": [{"name": "coord_wake_strong", "arguments": {
            "item_id": "it_same", "reason": "新回执确认范围", "question": "核对正式验收"}}]}, kwargs["model_name"]

    monkeypatch.setattr(model_service, "call_main", model)
    assert (await coordination_watcher.run_owned_batch(wid, AGENT, aid, first_id))["status"] == "wake"
    event_log.append_sync(wid, owner, {"type": "msg_report", "data": {
        "from": worker, "content": "补充联验回执，原汇报没有写事项编号。"}})
    await policy_runner.drain()
    latest_id = card_db.read_sync(wid, lambda c: c.execute(
        "SELECT batch_id FROM coordination_batches ORDER BY id DESC LIMIT 1").fetchone()[0], "")
    joined = await coordination_watcher.run_owned_batch(wid, AGENT, aid, latest_id)
    assert joined["status"] == "joined" and joined["joined_batch_id"] == first_id
    assert spawned == [first_id]
    result = await coordination_watcher.run_owned_turn(wid, AGENT, aid, "weak_wake", first_id)
    assert result["status"] == "parsed" and result["note"] == "联验事项收口。"


async def test_failed_inspection_retains_only_the_events_it_had_not_seen(sandbox, monkeypatch):
    wid, _, _ = await _seeded_work(monkeypatch)
    appt = (await coordination_store.configure_project_agent(wid, _appoint_kwargs()["models"]))["appointment"]
    aid = appt["assignment_id"]
    changes = [{"coord": f"event:{wid}:card_001:{seq}"} for seq in (71, 72, 73)]

    def seed(conn):
        coordination_watcher._ensure(conn)
        conn.execute("INSERT INTO coordination_batches"
                     " (work_id,plan_card_id,assignment_id,batch_id,changes,status) VALUES (?,?,?,?,?,'running')",
                     (wid, AGENT, aid, "failure", json.dumps(changes)))

    await card_db.run_write(wid, seed)
    for _ in range(2):
        await coordination_watcher.keep_unseen_inspection_events(wid, aid, "failure", 1)
    old, new = card_db.read_sync(wid, lambda c: c.execute(
        "SELECT changes,accepting,status FROM coordination_batches ORDER BY id").fetchall(), [])
    assert not old["accepting"] and json.loads(old["changes"]) == changes
    assert new["status"] == "open" and json.loads(new["changes"]) == changes[1:]
