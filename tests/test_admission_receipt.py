import json

import pytest

from src.api import card_tools, coordination_runner, message_runner, model_service
from src.storage import card_store, session_facade
from test_coordination_runner import _appointed, _live_case, _mk_card, _seeded_work

pytestmark = pytest.mark.asyncio


async def test_original_receipt_link_can_read_persisted_report(sandbox, monkeypatch, client):
    wid, plan, dev, aid, key, _ = await _live_case(monkeypatch)
    sent = await coordination_runner.coordination_deliver(wid, plan, assignment_id=aid, rev=0, action_key=key)
    async def no_model(*args, **kwargs):
        return False
    monkeypatch.setattr(message_runner, "deliver_now", no_model)
    mid = sent["msg_id"]
    report = await card_tools._h_report_up(
        wid, dev, {"content":"隔离原件回读","task":mid,"_coordination_msg_id":mid},
        card_store.iter_card_metas(wid))
    assert report["pending"]
    receiver = report["pending"][0]["to"]
    [row] = coordination_runner.read_execution_evidence(wid, plan, key)
    assert row["reply_session_card"] == receiver, f"台账保存 {row['reply_session_card']}，实际收件卡是 {receiver}"
    r = await client.get(f"/api/works/{wid}/cards/{row['reply_session_card']}/timeline",
                         params={"session": f"{row['reply_session_card']}@{wid}"})
    assert r.status_code == 200 and row["reply_msg_id"] in r.text



async def test_receipt_link_precise_msg_id(client, sandbox, monkeypatch):
    """[第六轮③] 两条行动回执各自带 msg_id 精确定位（不是同一整页）。"""
    wid, plan, dev, aid, key, _ = await _live_case(monkeypatch)
    sent = await coordination_runner.coordination_deliver(
        wid, plan, assignment_id=aid, rev=0, action_key=key)
    async def no_model(*args, **kwargs):
        return False
    monkeypatch.setattr(message_runner, "deliver_now", no_model)
    mid = sent["msg_id"]
    await card_tools._h_report_up(
        wid, dev, {"content": "精确定位回执", "task": mid,
                   "_coordination_msg_id": mid},
        card_store.iter_card_metas(wid))
    [row] = coordination_runner.read_execution_evidence(wid, plan, key)
    r = await client.get(
        f"/api/works/{wid}/cards/{row['reply_session_card']}/timeline",
        params={"session": row["reply_session_card"],
                "msg_id": row["reply_msg_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body.get("focus_msg_id") == row["reply_msg_id"]
    assert body.get("focus_seq", 0) > 0


async def test_focus_ignores_mention_in_body(client, sandbox, monkeypatch):
    """[第七轮②] 引用消息正文提到回执号不抢焦点；只按消息身份定位。"""
    wid, plan, dev, aid, key, _ = await _live_case(monkeypatch)
    sent = await coordination_runner.coordination_deliver(
        wid, plan, assignment_id=aid, rev=0, action_key=key)
    async def no_model(*args, **kwargs):
        return False
    monkeypatch.setattr(message_runner, "deliver_now", no_model)
    mid = sent["msg_id"]
    await card_tools._h_report_up(
        wid, dev, {"content": "原件", "task": mid,
                   "_coordination_msg_id": mid},
        card_store.iter_card_metas(wid))
    [row] = coordination_runner.read_execution_evidence(wid, plan, key)
    rmid = row["reply_msg_id"]
    # 在收件卡再落一条正文引用同一 msg_id 的消息（身份字段是别的号）
    recv = row["reply_session_card"]
    await session_facade.append_card_message(
        wid, recv, from_card=dev,
        text=f"引用：上一条回执 {rmid}",
        msg_type="msg_notify")
    r = await client.get(
        f"/api/works/{wid}/cards/{recv}/timeline",
        params={"session": recv, "msg_id": rmid})
    assert r.status_code == 200
    body = r.json()
    focus_seq = body["focus_seq"]
    # 焦点事件的 data.msg_id 必须就是回执号，不是引用消息
    hit = [x for x in body["items"] if int(x.get("seq") or 0) == focus_seq]
    assert hit and str((hit[0].get("data") or {}).get("msg_id") or "") == rmid


async def test_keyed_wake_never_steals_running_turn(sandbox, monkeypatch):
    """[第八轮①] 带行动键的唤醒不插进目标正在跑的普通回合。"""
    wid = "work_001"
    target = "card_010"
    calls = []
    from src.api import steer as _steer
    async def _fake_steer(*a, **kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(_steer, "request_steer", _fake_steer)
    monkeypatch.setattr(
        session_facade, "turn_holder", lambda w, c: "some-owner")
    async def _admit():
        return True
    got = await message_runner.deliver_now(
        wid, target, text="普通通知", source_kind="directive",
        source={"kind": "card_msg"}, admission=_admit, action_key="ak1")
    assert got is False and calls == [], (
        f"带行动键的唤醒插话了在跑普通回合：{got}, {calls}")


async def test_interrupted_fragment_stays_in_session(sandbox, monkeypatch):
    """[第八轮②] 附加会话的中断残段写回该会话，不污染主会话。"""
    from types import SimpleNamespace
    wid, sid = "work_001", "card_006@coordination"
    state = SimpleNamespace(work_id=wid, card_id="card_006",
                            session_id=sid)
    written = []
    async def _fake_append(w, c, blk, **kw):
        written.append((w, c, blk))
        return {}
    monkeypatch.setattr(session_facade, "append", _fake_append)
    await message_runner.save_interrupted_reply(state, "中断残段正文")
    assert written and written[0][1] == sid, written


@pytest.mark.parametrize("action", ["create_card", "create_worker_card", "create_ui_card"])
async def test_strong_turn_cannot_create_development_cards(sandbox, monkeypatch, action):
    """实发模式的统筹轮也不能直接新增开发卡。"""
    from test_coordination_runner import _sequence_call_main
    wid, _, fids = await _seeded_work(monkeypatch)
    plan = await _mk_card(wid, "域责任卡派工计划")
    aid = await _appointed(monkeypatch, wid, fids, plan, mode="live")
    before = set(card_store.iter_card_metas(wid))
    proposal = json.dumps({"action": action, "title": "执行卡", "task": "开发任务"})
    fake, _ = _sequence_call_main([proposal])
    monkeypatch.setattr(model_service, "call_main", fake)

    out = await coordination_runner.run_strong_first_turn(
        wid, plan, assignment_id=aid)

    assert out["status"] == "unparsed", out
    assert f"指令无效: {action!r}" in out["detail"]["error"], out
    assert set(card_store.iter_card_metas(wid)) == before
