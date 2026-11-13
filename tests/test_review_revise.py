# -*- coding: utf-8 -*-
"""评审场里「子卡回应」这一环（[venue 2026-10-31]）。

旧形态：评审侧手搓一个 `revise()` 回合叫子卡改稿。新形态（用户裁定
「对齐卡本体」）：评审场只做一件事——**给正卡发一条正经消息**
（`_dispatch_child`），子卡在**自己的普通回合**里改稿、调
`submit_revision` 交回。页面同步、工具执行、会话、锁、停止全部是
它自己的回合自带的。

钉四件事：
1. 派活＝往正卡会话落一条带批次号的消息（from=评审卡），然后唤醒它；
2. 子卡在自己回合里能改**自己的草稿**，改完调 `submit_revision` ⇒
   `responded_seq` 推进（否决席位据此知道有新回应可评）；
3. 子卡**不能 approve**（它是被评方，不是评审成员）；
4. 全员通过后派的活是「调 promote_draft」，不是「改草稿」。
"""
from __future__ import annotations

import json

from src.api import card_tools, model_service, review_runner, routes
from src.storage import (card_store, function_store, review_store, session_facade,
                         work_store)


async def _setup(n: int = 2):
    wdata = await work_store.create_work("评审改稿测试")
    wid = str(wdata["work_id"])
    fids = []
    # 评审默认席位集＝总裁／产品经理／技术总监三个目录 code
    # （`_meta.venue_defaults["requirements"]`；沙箱目录是空文件时回落旧默认）。
    review_codes = ("ceo", "pm", "cto")
    for i in range(n):
        code = review_codes[i] if i < len(review_codes) else f"f{i}"
        prof = await function_store.create_function(
            wid, f"职能{i}", {"jurisdiction": {"catalog_code": code}})
        fids.append(str(prof.get("function_id") or ""))
    r = await card_store.create_card(wid, "子项目甲", "用户直接创建",
                                     card_type="sub_project")
    cid = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid, {"requirements": "R0 正稿（不该被改）",
                   "requirements_draft": "R1 草稿（测试用）",
                   "requirements_state": "draft"}, "测试前置")
    cards = card_store.iter_card_metas(wid)
    out = await card_tools._h_open_review(
        wid, cid, {"card_id": cid, "focus": "R1 收尾",
                   "brief": "背景X\n现状Y\n评审Z"}, cards)
    # [两轮评审 2026-11-14] 过掉对齐批：总裁 approve＋一步 advance ⇒
    # 本文件用例测的都是第二轮三席批的机制。
    rid = str(out["data"]["review_card_id"])
    root = str(work_store.work_card_id_of(wid) or "")
    await card_tools._h_approve(wid, root, {"review_card_id": rid}, cards)
    await review_runner._run_step(wid, rid)
    return wid, cid, fids, rid


async def _child_turn(wid: str, cid: str) -> dict:
    """模拟 deliver_now 唤醒：子卡跑**它自己的普通回合**。"""
    return await routes._run_message(
        wid, cid, "你收到评审场的一条指令（见上一条消息），请按它执行。")


async def _rejecting_seat(wid: str, rid: str, seat: str) -> None:
    """机械落一席否决（不跑回合：投票闸要的是落座事实，不是回合）。"""
    await review_store.mark_spoken(wid, rid, seat)
    await review_store.mark_seen(wid, rid, seat)
    await review_store.reject(wid, rid, seat)


async def test_dispatch_revise_leaves_directive_in_child_session(sandbox):
    """派活＝正卡会话里多一条带批次号的指令消息（可审计对号）。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)

    ok = await review_runner._dispatch_child(wid, rid, st, kind="revise")
    # 测试环境 ECO_NO_AUTO_DELIVER=1 ⇒ 只落消息不真唤醒（真实链路才投递）
    assert ok is False

    blocks = await session_facade.read(wid, cid) or []
    text = "\n".join(str(b.get("content") or "") for b in blocks)
    assert rid in text and st.get("batch") in text, "消息带评审卡 id 与批次号"
    assert "submit_revision" in text, "指令要指明交回工具"
    assert "edit_requirements" in text
    # 机械事实：已请过一次回应，下一步不重复派
    st2 = await review_store.state_of(wid, rid)
    assert int(st2.get("asked_seq") or 0) == 1


async def test_child_edits_own_draft_in_its_own_turn(sandbox, monkeypatch):
    """子卡按意见改草稿（它自己的普通回合）⇒ 正稿不动；调 submit_revision 交回。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="revise")

    calls = {"n": 0}

    async def fake_call(**kw):
        # call_main 是 kwargs 调用；工具循环无轮次封顶——工具调用只发一次，
        # 之后回纯文本收尾（每轮都发同一件＝死循环）。
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": "", "tool_calls": [{
                "name": "edit_requirements",
                "arguments": {"card_id": cid,
                              "old_string": "R1 草稿（测试用）",
                              "new_string": "R1 改过（按意见补了证据）"}}]},
                    "fake-model")
        if calls["n"] == 2:
            return ({"content": "已改：补了证据", "tool_calls": [{
                "name": "submit_revision",
                "arguments": {"summary": "补了证据", "review_card_id": rid}}]},
                    "fake-model")
        return ({"content": "改完了", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    await _child_turn(wid, cid)

    card = await card_store.read_card(wid, cid)
    meta, body = card.get("meta") or {}, card.get("body") or {}
    assert "R1 改过" in str(meta.get("requirements_draft") or "")
    assert str(body.get("requirements") or "") == "R0 正稿（不该被改）", \
        "子卡只能改草稿，正稿由评审通过产生"

    # 改稿推进草稿版本；交回推进 responded_seq（否决席位的「有新回应」判据）。
    st2 = await review_store.state_of(wid, rid)
    assert int(st2.get("draft") or 1) == 2
    assert int(st2.get("responded_seq") or 0) == 1


async def test_child_cannot_approve_itself(sandbox, monkeypatch):
    """子卡不是评审成员：approve 必须被拒，票不能变。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="revise")

    calls = {"n": 0}

    async def fake_call(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": "", "tool_calls": [
                {"name": "approve", "arguments": {"review_card_id": rid}}]},
                    "fake-model")
        return ({"content": "（不改）", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    await _child_turn(wid, cid)

    st2 = await review_store.status(wid, rid)
    assert cid not in st2["passed"], "被评卡不能给自己投票"
    # 没调 submit_revision ⇒ 回应水位不动
    st3 = await review_store.state_of(wid, rid)
    assert int(st3.get("responded_seq") or 0) == 0


async def test_dispatch_carries_seat_speeches_verbatim(sandbox, monkeypatch):
    """派给正卡的指令带**评审现场的原话**（含未通过成员的发言，不截断）。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)

    # 一席先发言＋否掉（真实席位回合：发言落评审卡会话，from=席位）
    n = {"c": 0}

    async def say(**kw):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "第三条不满足：没有证据",
                     "tool_calls": [{"name": "reject",
                                     "arguments": {"review_card_id": rid}}]},
                    "fake-model")
        return ({"content": "说完了", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", say)
    await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")

    st2 = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st2, kind="revise")

    # 指令落在子卡自己的会话里——发言原文在其中
    blocks = await session_facade.read(wid, cid) or []
    text = "\n".join(str(b.get("content") or "") for b in blocks)
    assert "第三条不满足" in text, "否决席位的原话必须随指令带到子卡"


async def test_child_toolface_has_no_approve(sandbox, monkeypatch):
    """子卡回合的工具面：没有 approve（它不是成员），有 edit_requirements
    与 submit_revision（回应评审批次的出口能力）。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="revise")

    captured: dict = {}

    async def cap(**kw):
        if "tools" not in captured:
            captured["tools"] = [
                str((t.get("function") or t).get("name") or "")
                for t in (kw.get("tools") or [])]
        return ({"content": "（不改）", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", cap)
    await _child_turn(wid, cid)

    # approve 是 function-only（不进基础池）；reject 对非成员由投票闸拒——
    # 闸在 handler，不靠工具面（席位本来就允许是任意卡）。
    assert "approve" not in captured["tools"], captured["tools"]
    assert "edit_requirements" in captured["tools"]
    assert "submit_revision" in captured["tools"], captured["tools"]


async def test_no_submit_revision_means_still_waiting(sandbox, monkeypatch):
    """子卡回了文字但没调 submit_revision ⇒ 机械上回应没交回。ask 已被
    回合**消费**（指令落点之后会话长了新块）⇒ 补**纯唤醒**而不是再派活
    （信不重复落）或空等死。沙箱里 ECO_NO_AUTO_DELIVER=1 ⇒ 唤醒发不出，
    落 wake_failed＋dispatch_fails 计数。"""
    wid, cid, fids, rid = await _setup()
    for f in fids:
        await _rejecting_seat(wid, rid, f)
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="revise")

    async def fake_call(**kw):
        return ({"content": "我认为不用改，理由如下……", "tool_calls": []},
                "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    await _child_turn(wid, cid)

    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    async def never_dispatch(*a, **k):
        raise AssertionError("等回应期间不该再派活")

    monkeypatch.setattr(review_runner, "_dispatch_child", never_dispatch)
    r = await review_runner.run_once(wid, rid)
    assert r["reason"] == "wake_failed" and r["continued"] is False, r
    st2 = await review_store.state_of(wid, rid)
    assert int(st2.get("asked_seq") or 0) == 1, "没再派活 ⇒ asked_seq 不涨"


async def test_dispatch_does_not_interrupt_running_child(sandbox, monkeypatch):
    """正卡自己的回合在跑 ⇒ 不另起新回合——deliver_now 忙时走 steer 插话
    （[2026-11-08] 语义：在跑≠不送达，插话进在跑回合返回 True；模型
    绝不被二次调用，不并发跑两个回合改同一张卡）。"""
    wid, cid, fids, rid = await _setup()
    for f in fids:
        await _rejecting_seat(wid, rid, f)
    st = await review_store.state_of(wid, rid)

    called = {"n": 0}

    async def fake_call(*a, **k):
        called["n"] += 1
        return ({"content": "不该被调到", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", fake_call)
    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)

    session_facade.turn_try_acquire(wid, cid, "user-turn")
    try:
        ok = await review_runner._dispatch_child(wid, rid, st, kind="revise")
    finally:
        session_facade.turn_release(wid, cid, "user-turn")

    assert ok is True, "忙时走 steer 插话——送达＝True（旧语义返回 False 已废）"
    assert called["n"] == 0, "不另起回合就不该调模型"
    # 消息与 mark_asked 仍落了：它本轮收口的自查续轮会接上
    blocks = await session_facade.read(wid, cid) or []
    assert any("submit_revision" in str(b.get("content") or "") for b in blocks)
    st2 = await review_store.state_of(wid, rid)
    assert int(st2.get("asked_seq") or 0) == 1


async def test_full_loop_via_steps_then_promote(sandbox, monkeypatch):
    """端到端：开批 → 席位否 → 派活改稿 → 子卡自己回合改＋交回 →
    席位重评过 → 派活定稿 → promote_draft 转正稿。"""
    wid, cid, fids, rid = await _setup()

    calls: list = []

    async def fake_seat(work_id, review_id, seat, st, *, kind, hint=""):
        calls.append((seat, kind))
        await review_store.mark_spoken(work_id, review_id, seat)
        await review_store.mark_seen(work_id, review_id, seat)
        if int(st.get("draft") or 1) == 1:
            await review_store.reject(work_id, review_id, seat)
        else:
            await review_store.approve(work_id, review_id, seat)
        return {"seat": seat, "kind": kind, "voted": True}

    async def fake_dispatch(work_id, review_id, st, *, kind):
        if kind == "revise":
            await review_store.mark_asked(work_id, review_id)
            # 模拟正卡自己回合的产物：改稿＋交回＋**轮末定版**
            # （[轮粒度 2026-11-14] 版本只在回合收口推进——真回合靠
            # report_target_turn_end，替身也一样）。
            await card_store.update_card(
                wid, cid, {"requirements_draft": "R2 最终稿（按意见改完）"},
                "子卡改草稿")
            await review_store.mark_responded(work_id, review_id)
            await review_runner.report_target_turn_end(
                work_id, cid, "ok")
        else:
            await review_store.mark_promote_asked(work_id, review_id)
        return True

    monkeypatch.setattr(review_runner, "_seat_turn", fake_seat)
    monkeypatch.setattr(review_runner, "_dispatch_child", fake_dispatch)
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)

    # [harness 驱动 2026-11-01] round_done{has_next} 让策略真续跑——
    # 撞 _RUNNING 的迭代让出轮转给派生步跑完。
    import asyncio as _aio
    for _ in range(120):
        await review_runner.run_once(wid, rid)
        if (await review_store.status(wid, rid)).get("all_passed"):
            break
        await _aio.sleep(0)

    # 收口＝子卡自己调 promote_draft（系统不代劳）
    pr = await card_tools._h_promote_draft(
        wid, cid, {"card_id": cid}, card_store.iter_card_metas(wid))
    assert pr.get("ok"), pr

    card = await card_store.read_card(wid, cid)
    body, meta = card.get("body") or {}, card.get("meta") or {}
    assert str(body.get("requirements") or "") == "R2 最终稿（按意见改完）"
    assert str(meta.get("requirements_draft") or "") == ""
    assert str(meta.get("requirements_state") or "") == "approved"
    st = await review_store.status(wid, rid)
    assert st["all_passed"] is True and st["batch"] == "", "批次应已收口"


async def test_member_sees_the_draft_not_the_final(sandbox, monkeypatch):
    """评审成员看的必须是**草稿**，不是正稿（[2026-10-29 用户抓到]）。

    mandate 的「待评审的草稿」一节装 `requirements_draft`；
    正稿只在「上一批次正稿」一节出现（那是上一批的结论，不是评审对象）。
    """
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)

    captured: dict[str, str] = {}
    n = {"c": 0}

    async def cap(**kw):
        n["c"] += 1
        if "user" not in captured:
            # 只抓首轮——工具续跳轮 user_prompt 故意清空（去假 user）。
            captured["user"] = str(kw.get("user_prompt") or "")
        if n["c"] == 1:
            return ({"content": "看完了",
                     "tool_calls": [{"name": "reject",
                                     "arguments": {"review_card_id": rid}}]},
                    "fake-model")
        return ({"content": "说完了", "tool_calls": []}, "fake-model")

    monkeypatch.setattr(model_service, "call_main", cap)
    await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")

    assert "R1 草稿（测试用）" in captured["user"], "评审对象必须是草稿"
    assert "R0 正稿" not in captured["user"], "正稿不能混进评审对象"


async def test_seat_identity_reads_review_charter(sandbox, monkeypatch, tmp_path):
    """席位身份区读**评审职责书**，不是日常职责（[2026-10-29 用户令]）。

    职责书按**席位 id**查、查不到再按**目录 code**查（评审职责按职能写）；
    都没有 ⇒ 回落日常 charter。走 `act_meta` 注入普通回合的 145 区。
    """
    from src.api import review_runner as rr

    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)

    # ① 按席位 id 查中
    charter_file = tmp_path / "review_charters.json"
    charter_file.write_text(json.dumps(
        {fids[0]: {"text": "评审职责书·按席位"}, "ceo": {"text": "评审职责书·按code"}},
         ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rr, "REVIEW_CHARTERS", charter_file)

    captured: list[dict] = []

    async def fake_run(work_id, card_id, content, **kw):
        captured.append(kw)
        return {"reply": "ok"}

    monkeypatch.setattr(routes, "_run_message", fake_run)
    await rr._seat_turn(wid, rid, fids[0], st, kind="speak")
    meta0 = (captured[-1].get("act_meta") or {}).get("charter") or {}
    assert meta0.get("text") == "评审职责书·按席位", meta0

    # ② 席位 id 查不到 ⇒ 按目录 code（fn_001 的 catalog_code=ceo）
    charter_file.write_text(json.dumps(
        {"ceo": {"text": "评审职责书·按code"}}, ensure_ascii=False),
        encoding="utf-8")
    await rr._seat_turn(wid, rid, fids[0], st, kind="speak")
    meta1 = (captured[-1].get("act_meta") or {}).get("charter") or {}
    assert meta1.get("text") == "评审职责书·按code", meta1


def test_tool_results_are_not_truncated():
    """工具结果不许硬截断（单一投影 `render_tool_result`）。

    [2026-10-29 实弹] 旧评审循环把 `json.dumps(r)[:12000]` 砍掉了需求块。
    现在工具结果走普通回合的 `render_tool_result`——**唯一来源**，
    不许再出现 `[:N]` 截断。
    """
    big = {"content": "X" * 20000 + "需求块在这里"}
    rendered = card_tools.render_tool_result({"ok": True, "data": big})
    assert "需求块在这里" in rendered, "工具结果不许砍"
    assert len(rendered) > 20000
    # 错误结果也走同一投影：信封化，不当数据喂给模型
    err = card_tools.render_tool_result(
        {"ok": False, "error": {"code": "denied", "message": "没权限"}})
    assert "denied" in err and "没权限" in err


async def test_spoke_without_vote_gets_nudge_not_loop(sandbox, monkeypatch):
    """只说话不表态 ⇒ 这一步结束；**下一步**机械派「补表态」回合。

    [2026-10-31 用户令] 不做内部重试、不收敛工具面：每个席位回合都是
    普通回合，重投是 harness 按事件接力的**下一次唤醒**，不是进程内循环。
    """
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)
    calls: list[dict] = []

    async def model(**kw):
        calls.append({"user": str(kw.get("user_prompt") or "")})
        if len(calls) == 1:
            return ({"content": "结论：通过。", "tool_calls": []}, "fake")
        if len(calls) == 2:
            return ({"content": "通过",
                     "tool_calls": [{"name": "approve",
                                     "arguments": {"review_card_id": rid}}]},
                    "fake")
        return ({"content": "说完了", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", model)
    await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")

    assert len(calls) == 1, "只说话不表态 ⇒ 结束，由 harness 决定下一步"
    st1 = await review_store.status(wid, rid)
    assert fids[0] in st1["pending"], "没投票就还是 pending"

    # 下一步：同一席位被叫回来补投（说了没投 → vote_nudge）
    monkeypatch.delenv(review_runner.ENV_NO_AUTO, raising=False)
    r = await review_runner.run_once(wid, rid)
    assert r["reason"] == "seat_done" and r.get("kind") == "vote_nudge", r
    # 补投轮的首轮调用带差事文本（续跳轮 user_prompt 故意为空）。
    assert "补表态" in calls[1]["user"], "差事文本要说清是补表态"
    st2 = await review_store.status(wid, rid)
    assert fids[0] in st2["passed"], "补投的 approve 要落成真票"


async def test_dispatch_promote_tells_child_to_write_final(sandbox):
    """全过后派的活是「调 promote_draft 写正稿」，不是「改草稿」。"""
    wid, cid, fids, rid = await _setup()
    for f in fids:
        await review_store.mark_spoken(wid, rid, f)
        await review_store.approve(wid, rid, f)
    st = await review_store.state_of(wid, rid)

    await review_runner._dispatch_child(wid, rid, st, kind="promote")

    blocks = await session_facade.read(wid, cid) or []
    text = "\n".join(str(b.get("content") or "") for b in blocks)
    assert "promote_draft" in text, "话里要指明调哪个工具"
    assert "已全员通过" in text
    st2 = await review_store.state_of(wid, rid)
    assert st2.get("promote_asked"), "已请定稿要落成机械事实"


async def test_revision_after_promote_asked_re_asks(sandbox):
    """[实跑回放 2026-11-01 · work_027 card_128] 请定稿后正卡答的是
    `submit_revision`（不是 promote_draft）⇒ promote_asked 被这次回应
    **消费**——清位，下一次唤醒自然重派，批次不停尸在 awaiting_promote。
    """
    wid, cid, fids, rid = await _setup()
    for f in fids:
        await review_store.mark_spoken(wid, rid, f)
        await review_store.approve(wid, rid, f)
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="promote")
    assert (await review_store.state_of(wid, rid)).get("promote_asked")

    # 正卡交回（没转正）⇒ 那次请定稿被消费：promote_asked 清位
    await review_store.mark_responded(wid, rid)
    st2 = await review_store.state_of(wid, rid)
    assert int(st2.get("responded_seq") or 0) == 1
    assert not st2.get("promote_asked"), \
        "交回不是转正 ⇒ 请定稿作消费论，下一次唤醒要重派"

    # classify 口径复核：all_passed ∧ !promote_asked ⇒ 再派 promote
    nxt, _why = review_runner._classify(
        st2, await review_store.status(wid, rid),
        set((st2.get("spoken") or {}).get("roles") or []),
        promoted=False)
    assert nxt == ("dispatch", "promote"), nxt


async def test_promote_ask_consumed_by_turn_wakes_again(sandbox):
    """[实跑回放 2026-11-01 · work_027 card_124→card_074] 请定稿后正卡回合
    **正常结束但没调 promote_draft**（也没交回——mark_responded 清位碰不到）
    ⇒ ask 被回合**消费**但没成果。

    机械判据：指令落点（promote_asked_seq）之后正卡会话长了新块 ⇒ 它起过
    回合读到指令 ⇒ classify 补**纯唤醒**（不重复落信）；nudge 封顶后到站。
    点火入口是正卡 turn_end ⇒ report_target_turn_end 往评审卡报事实。
    """
    wid, cid, fids, rid = await _setup()
    for f in fids:
        await review_store.mark_spoken(wid, rid, f)
        await review_store.approve(wid, rid, f)
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="promote")

    st1 = await review_store.state_of(wid, rid)
    assert st1.get("promote_asked") and st1.get("promote_asked_seq") is not None

    # 正卡起了一个回合（指令落点之后会话长新块）但没调 promote_draft。
    await session_facade.append(
        wid, cid, {"actor": "assistant",
                   "content": "（自述：先看下批次现状……回合收尾）"})
    spoken = set((st1.get("spoken") or {}).get("roles") or [])
    nxt, _why = review_runner._classify(
        st1, await review_store.status(wid, rid), spoken,
        promoted=False, tail_seq=review_runner._tail_seq(wid, cid))
    assert nxt == ("wake", "promote", "consumed"), nxt

    # 唤醒文按状况点破：consumed ⇒ 「你上一轮读过了但没调」。
    kickoff = review_runner._wake_kickoff("promote", "consumed", st1)
    assert "promote_draft" in kickoff and "上一轮" in kickoff

    # 正卡 turn_end ⇒ 事实报给评审卡（harness 的点火入口），不发=没人再叫。
    assert await review_runner.report_target_turn_end(wid, cid) is True

    # 消费口径不误伤：指令落地后正卡**一直没起回合**（会话没长块）
    # ⇒ 不算消费 ⇒ 到站等它（deliver_now 的唤醒可能还在路上）。
    await review_store.mark_wake_nudge(wid, rid, "promote",
                                       at_seq=review_runner._tail_seq(wid, cid))
    st_quiet = await review_store.state_of(wid, rid)
    nxt_q, why_q = review_runner._classify(
        st_quiet, await review_store.status(wid, rid), spoken,
        promoted=False, tail_seq=review_runner._tail_seq(wid, cid))
    assert nxt_q is None and why_q == "awaiting_promote", (nxt_q, why_q)

    # 又有新回合但仍没转正 ⇒ 再消费一次；nudge 封顶 ⇒ 到站不空烧。
    await session_facade.append(
        wid, cid, {"actor": "assistant", "content": "（还是没转正）"})
    for _ in range(review_runner._WAKE_NUDGE_CAP):
        await review_store.mark_wake_nudge(
            wid, rid, "promote", at_seq=review_runner._tail_seq(wid, cid))
    st2 = await review_store.state_of(wid, rid)
    nxt2, why2 = review_runner._classify(
        st2, await review_store.status(wid, rid), spoken,
        promoted=False, tail_seq=review_runner._tail_seq(wid, cid))
    assert nxt2 is None and why2 == "awaiting_promote", (nxt2, why2)


async def test_revise_ask_consumed_by_turn_wakes_again(sandbox):
    """同款消费检测在 revise 侧：请回应后正卡回合结束但没调 submit_revision
    ⇒ asked_at_seq 之后有新块 ⇒ 补纯唤醒。"""
    wid, cid, fids, rid = await _setup()
    for f in fids:
        await _rejecting_seat(wid, rid, f)
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="revise")

    st1 = await review_store.state_of(wid, rid)
    assert int(st1.get("asked_seq") or 0) > int(st1.get("responded_seq") or 0)
    assert st1.get("asked_at_seq") is not None

    # 正卡回合跑过了指令（没交回）⇒ 消费 ⇒ 补唤醒而不是永久 awaiting_target。
    await session_facade.append(
        wid, cid, {"actor": "assistant", "content": "（干了别的，没交回）"})
    spoken = set((st1.get("spoken") or {}).get("roles") or [])
    nxt, _why = review_runner._classify(
        st1, await review_store.status(wid, rid), spoken,
        promoted=False, tail_seq=review_runner._tail_seq(wid, cid))
    assert nxt == ("wake", "revise", "consumed"), nxt


async def test_dispatch_never_leaks_venue_internals(sandbox, monkeypatch):
    """[场内外边界 2026-11-02 · 用户裁定「评审卡上下文不进子卡上下文」]
    派活指令只带**席位发言**（actor=assistant 且 from=席位）：投票信封
    JSON、席位 read_card 读出的正卡卡面等工具块是场内内务，不得进
    子卡会话。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)

    # 真席位回合落发言（from=席位）＋手工塞两条内务块进评审会话。
    async def say(**kw):
        return ({"content": "接口约束缺验收口径", "tool_calls": []}, "fake-model")
    monkeypatch.setattr(model_service, "call_main", say)
    await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")
    await session_facade.append(wid, rid, {
        "actor": "tool", "tool_call_id": "call_x",
        "content": '{"review_card_id": "card_117", "verdict": "reject"}'})
    await session_facade.append(wid, rid, {
        "actor": "tool", "tool_call_id": "call_y",
        "content": '{"card_id": "card_021", "title": "被评卡", "requirements": "内部读出的卡面"}'})
    await session_facade.append(wid, rid, {
        "actor": "assistant", "content": "（无 from 的信封块，不该去）"})

    st2 = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st2, kind="revise")

    blocks = await session_facade.read(wid, cid) or []
    text = "\n".join(str(b.get("content") or "") for b in blocks)
    assert "接口约束缺验收口径" in text, "席位发言必须随指令带到"
    assert "verdict" not in text and "card_117\", \"verdict" not in text
    assert "内部读出的卡面" not in text, "工具块内容不得漏进子卡会话"
    assert "不该去" not in text


async def test_failed_target_turn_reports_fact_policy_gates(sandbox, monkeypatch):
    """[实跑回放 2026-11-02 · work_027 card_117→card_021] 正卡回合**模型
    失败**（上下文超限重试耗尽）⇒ 事件层必须**照发完整事实**（outcome
    进负载——细粒度事实不归并）；「失败≠done」由真实策略的 where 拒掉，
    不是 reporter 代码悄悄不报。
    """
    from src.infra import harness_cond
    emitted = []
    monkeypatch.setattr(review_runner.event_stream, "emit",
                        lambda wid_, rid_, t, d: emitted.append((t, d)) or {})

    wid, cid, fids, rid = await _setup()
    for f in fids:
        await _rejecting_seat(wid, rid, f)
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="revise")

    # 真实策略的 where（生产同一份，不是抄进测试的副本）。
    import json
    from pathlib import Path
    pols = json.loads((Path(__file__).resolve().parents[1]
                       / "config" / "harness_policies.json")
                      .read_text(encoding="utf-8"))
    pol = next(p for p in pols.get("policies", pols)
               if p.get("id") == "review_target_turn_end_continue")

    # 每种结局都照发事实；无推进时只有正常收口 + 挂账过策略闸
    # （progress=false 的回合走 stall 兜底那半支）。
    for oc, fires in (("error", False), ("aborted", False),
                      ("max-tokens", False), ("ok", True)):
        emitted.clear()
        assert await review_runner.report_target_turn_end(wid, cid, oc) is True
        ev = next(d for t, d in emitted if t == "review_target_turn_end")
        assert ev["outcome"] == oc and ev["pending_ask"] is True \
            and ev["progress"] is False
        ctx = {"trigger_data": ev, "events": [ev]}
        assert harness_cond.eval_cond(pol["when"], ctx) is fires, (oc, ev)


async def test_turn_end_progress_marks_revision_and_draft(sandbox, monkeypatch):
    """[turn_end 激活 2026-11-04] ``progress``＝刚结束这轮正卡有实质推进：
    草稿版本涨（改稿）或 responded_seq 涨（交回）。交回后回合崩了也照点——
    回应是既成事实。没推进的回合 progress=false，点火交给 pending_ask 半支。"""
    from src.infra import harness_cond
    emitted = []
    monkeypatch.setattr(review_runner.event_stream, "emit",
                        lambda wid_, rid_, t, d: emitted.append((t, d)) or {})
    import json
    from pathlib import Path
    pols = json.loads((Path(__file__).resolve().parents[1]
                       / "config" / "harness_policies.json")
                      .read_text(encoding="utf-8"))
    pol = next(p for p in pols.get("policies", pols)
               if p.get("id") == "review_target_turn_end_continue")

    wid, cid, fids, rid = await _setup()
    for f in fids:
        await _rejecting_seat(wid, rid, f)
    st = await review_store.state_of(wid, rid)
    await review_runner._dispatch_child(wid, rid, st, kind="revise")

    # 改稿一轮：草稿指纹真变 ⇒ sync_draft 推版本 ⇒ turn_end 报 progress。
    meta = (await card_store.read_card(wid, cid)).get("meta") or {}
    draft = str(meta.get("requirements_draft") or "")
    await card_store.update_card(
        wid, cid, {"requirements_draft": draft + "\n新增一条验收标准"}, "测试改稿")
    assert await review_runner.report_target_turn_end(wid, cid, "ok") is True
    ev = next(d for t, d in emitted if t == "review_target_turn_end")
    assert ev["progress"] is True
    assert harness_cond.eval_cond(pol["when"],
                                  {"trigger_data": ev, "events": [ev]}) is True

    # 第二轮没动草稿 ⇒ progress 回落 false（水位已回写，只吃增量）。
    emitted.clear()
    assert await review_runner.report_target_turn_end(wid, cid, "ok") is True
    ev2 = next(d for t, d in emitted if t == "review_target_turn_end")
    assert ev2["progress"] is False

    # 第三轮没改稿但交回了（submit_revision ⇒ responded_seq 涨）⇒ progress。
    emitted.clear()
    await review_store.mark_responded(wid, rid)
    assert await review_runner.report_target_turn_end(wid, cid, "ok") is True
    ev3 = next(d for t, d in emitted if t == "review_target_turn_end")
    assert ev3["progress"] is True and ev3["pending_ask"] is False

    # 交回后回合崩了（error）⇒ progress 仍是既成事实，策略照点。
    emitted.clear()
    await review_store.mark_responded(wid, rid)
    assert await review_runner.report_target_turn_end(wid, cid, "error") is True
    ev4 = next(d for t, d in emitted if t == "review_target_turn_end")
    assert ev4["progress"] is True and ev4["outcome"] == "error"
    assert harness_cond.eval_cond(pol["when"],
                                  {"trigger_data": ev4, "events": [ev4]}) is True


async def test_review_target_responded_policy_retired(sandbox):
    """submit_revision 的中途点火并入 turn_end.progress——策略停用但
    事件/事实照发（水位判定靠它）。真实配置里它必须是 inactive。"""
    import json
    from pathlib import Path
    pols = json.loads((Path(__file__).resolve().parents[1]
                       / "config" / "harness_policies.json")
                      .read_text(encoding="utf-8"))
    pol = next(p for p in pols.get("policies", pols)
               if p.get("id") == "review_target_responded")
    assert pol.get("active") is False
