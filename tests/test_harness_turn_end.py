# -*- coding: utf-8 -*-
"""[第一个系统消息事件 2026-10-26] 正常完成所有动作（有 done 事件）但没向上汇报 ⇒ 唤醒提醒。

**判据全部基于事件**（用户令：明明有事件机制，不许再拿卡的状态快照当判据）。
走真实链路：emit → event_log 落盘 → 接线 → harness.fire → 收件箱有信。

E1 本轮 done + 没汇报 ⇒ 收到提醒
E2 本轮 done + 已汇报 ⇒ 不发
E3 本轮出错（只有 error，没有 done）⇒ 不发
E4 总裁卡 / 职能卡 ⇒ 不发（不在 card_types）
E5 同一条 done 不会重复发
E6 上一轮汇报过、本轮没有 ⇒ 仍然要发（判据按"本轮"，不是"本卡历史"）
"""
from __future__ import annotations

import pytest

from src.api import event_stream
from src.infra import event_hub, harness, harness_rules
from src.storage import card_db, card_store, event_log, work_store


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # [2026-10-28 · 条件集上线] 本文件守的是**旧规则表那条路**
    # （event_log → harness.fire → 规则表）。默认接线已改走条件集
    # （policy_runner.feed → 值班员），这里显式打开 legacy 开关继续守旧路径，
    # 以便出问题时能一键回退。新路径的等价行为由 test_policy_set /
    # test_policy_runner 守着。**旧路径删掉时，本文件一并删。**
    monkeypatch.setenv("ECO_HARNESS_LEGACY", "1")
    event_hub.reset_state()
    harness_rules.reset_cache()
    yield
    event_hub.reset_state()
    harness_rules.reset_cache()


async def _seed():
    w = await work_store.create_work("完工汇报提醒")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(wid, "作业卡", "模型生成",
                                        card_type="sub_project",
                                        parent=root))["meta"]["card_id"]
    # 父卡用 direct 派活 —— 规则的前置条件（只对"父卡派发的任务"提醒）
    event_stream.emit(wid, sub, "msg_directive",
                      {"from": root, "content": "把这件事做完"})
    await _settle()
    return wid, root, sub


async def _settle():
    await event_log.drain()
    await harness.drain()
    await event_hub.flush_all()


async def _emit(wid, cid, ev_type, data=None):
    event_stream.emit(wid, cid, ev_type, data or {})
    await _settle()


async def _turn(wid, cid, *, report: bool = False, sync: bool = False,
                error: bool = False, card_type: str = "sub_project"):
    """模拟一个完整回合的事件序列（真实顺序：run_start … turn_end → done）。

    ``done`` 事件**自带 card_type**（生产链路已改），规则层的卡类型过滤只认事件字段。
    ``report`` → `msg_report`（汇报）；``sync`` → `msg_sync`（同步，不算汇报）。
    """
    await _emit(wid, cid, "run_start", {"pair_id": "p1"})
    await _emit(wid, cid, "tool_call", {"tool": "read_card"})
    if sync:
        await _emit(wid, cid, "msg_sync", {"to": "card_001"})
    if report:
        # 汇报就是一次成功的工具调用——事件记录里本来就有
        # tool_call/tool_result(tool=report_up, ok=true)，不需要额外补事件类型
        await _emit(wid, cid, "tool_call", {"tool": "report_up",
                                            "arguments": {"kind": "phase"}})
        await _emit(wid, cid, "tool_result", {"tool": "report_up", "ok": True})
    if error:
        await _emit(wid, cid, "error", {"msg": "模型调用失败"})
    await _emit(wid, cid, "turn_end", {"outcome": "error" if error else "ok"})
    if not error:
        await _emit(wid, cid, "done", {"delivered": "ok", "card_type": card_type})


async def _inbox(wid, cid):
    """读该卡会话里的 user 块。

    [2026-10-26] 消息现在落成**会话块**（永久在上下文里），不再进收件箱。
    """
    from src.storage import session_facade
    blocks = await session_facade.read(wid, cid) or []
    return [str(b.get("content") or "") for b in blocks
            if str(b.get("role") or "") == "user"]


# ---------------------------------------------------------------- E1
async def test_done_without_report_gets_reminder(sandbox):
    wid, root, sub = await _seed()
    await _turn(wid, sub, report=False)
    box = await _inbox(wid, sub)
    assert any("没有向上汇报" in c for c in box), box


# ---------------------------------------------------------------- E2
async def test_done_with_report_no_reminder(sandbox):
    wid, root, sub = await _seed()
    await _turn(wid, sub, report=True)
    box = await _inbox(wid, sub)
    assert not any("没有向上汇报" in c for c in box), box


# ---------------------------------------------------------------- E3
async def test_error_turn_no_reminder(sandbox):
    wid, root, sub = await _seed()
    await _turn(wid, sub, error=True)
    box = await _inbox(wid, sub)
    assert not any("没有向上汇报" in c for c in box), box


# ---------------------------------------------------------------- E4
async def test_ceo_card_no_reminder(sandbox):
    wid, root, sub = await _seed()
    await _turn(wid, root, report=False, card_type="project")   # 总裁卡不在 card_types
    box = await _inbox(wid, root)
    assert not any("没有向上汇报" in c for c in box), box


# ---------------------------------------------------------------- E5
async def test_same_done_not_duplicated(sandbox):
    wid, root, sub = await _seed()
    await _turn(wid, sub, report=False)
    first = [c for c in await _inbox(wid, sub) if "没有向上汇报" in c]
    assert len(first) == 1, first
    # 同一 seq 再评估（模拟重复投递/重放）
    recs = event_log.timeline(wid, sub, tail=50)
    seq = [r for r in recs if r.get("type") == "done"][-1]["seq"]
    await harness.fire(wid, "done", sub, {}, trigger_seq=int(seq))
    again = [c for c in await _inbox(wid, sub) if "没有向上汇报" in c]
    assert len(again) == 1, again


# ---------------------------------------------------------------- E6
async def test_previous_turn_report_does_not_cover_current(sandbox):
    """上一轮汇报过，本轮没汇报 ⇒ 仍要提醒（判据是"本轮"，不是"本卡历史"）。"""
    wid, root, sub = await _seed()
    await _turn(wid, sub, report=True)        # 第 1 轮：汇报了
    await _turn(wid, sub, report=False)       # 第 2 轮：没汇报
    box = await _inbox(wid, sub)
    assert any("没有向上汇报" in c for c in box), box


# ---------------------------------------------------------------- E8
async def test_sync_is_not_report(sandbox):
    """本轮只做了 sync（横向同步事实）⇒ **仍算没汇报**，要提醒。

    这是「判断不能死板机械」的核心用例：改前 sync 与 report_up 都落 msg_notify，
    判据会把「同步过」误当成「汇报过」，该提醒的不提醒。
    """
    wid, root, sub = await _seed()
    await _turn(wid, sub, sync=True, report=False)
    box = await _inbox(wid, sub)
    assert any("没有向上汇报" in c for c in box), box


async def test_sync_plus_report_no_reminder(sandbox):
    """先 sync 再汇报 ⇒ 不提醒。"""
    wid, root, sub = await _seed()
    await _turn(wid, sub, sync=True, report=True)
    box = await _inbox(wid, sub)
    assert not any("没有向上汇报" in c for c in box), box


# ---------------------------------------------------------------- E10
async def test_guard_where_matches_tool_result(sandbox):
    """判据能精确到「调了哪个工具、成没成功」——用已有 tool_result 事件即可。

    线上实证（work_028 14:41）：一轮里调了 report_up(kind=phase) 且成功，
    但判据只认「有没有某类事件」，没看 tool 字段 ⇒ 误判为"没汇报"并唤醒提醒。
    """
    wid, root, sub = await _seed()
    await _turn(wid, sub, report=True)
    ctx = await harness.build_ctx(wid, sub, "done")
    guard = [{"kind": "no_event", "types": ["tool_result"],
              "where": {"tool": "report_up", "ok": True}, "scope": "turn"}]
    assert harness.evaluate_guard(guard, ctx) is False, "有成功汇报时 no_event 应为假"

    # 对照：调了但失败 ⇒ 仍算没汇报
    wid2, root2, sub2 = await _seed()
    await _emit(wid2, sub2, "run_start", {})
    await _emit(wid2, sub2, "tool_call", {"tool": "report_up"})
    await _emit(wid2, sub2, "tool_result", {"tool": "report_up", "ok": False})
    await _emit(wid2, sub2, "turn_end", {})
    await _emit(wid2, sub2, "done", {"card_type": "sub_project"})
    ctx2 = await harness.build_ctx(wid2, sub2, "done")
    assert harness.evaluate_guard(guard, ctx2) is True, "汇报失败应算没汇报"


# ---------------------------------------------------------------- E11
async def test_no_directive_no_reminder(sandbox):
    """没有父卡派发（事件流里没有来自父卡的 msg_directive）⇒ 不提醒。

    用户口径：只对**父卡派发给它的任务**做这个提醒；用户随手开的卡、
    goal 自驱的卡都不该被催汇报。
    """
    w = await work_store.create_work("无派发")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(wid, "卡", "模型生成",
                                        card_type="sub_project",
                                        parent=root))["meta"]["card_id"]
    await _turn(wid, sub, report=False)
    box = await _inbox(wid, sub)
    assert not any("没有向上汇报" in c for c in box), box


# ---------------------------------------------------------------- E12
async def test_deliver_now_text_mode_starts_turn(sandbox, monkeypatch):
    """消息不写 inbox 时，text 模式仍能起轮 —— direct / 系统提醒的唤醒路径。

    用户口径：**分场景**。sync 不唤醒（只告知）；direct（指令）与系统提醒
    必须唤醒。改消息落点后不能把"不唤醒"顺带套到这两类上。
    """
    import asyncio as _aio
    from src.api import message_runner, routes

    monkeypatch.delenv("ECO_NO_AUTO_DELIVER", raising=False)
    wid, root, sub = await _seed()
    started: dict = {}

    async def _fake_run(work_id, card_id, content, *a, **kw):
        started.update({"card": card_id, "content": content,
                        "source": kw.get("source")})

    monkeypatch.setattr(routes, "_run_message", _fake_run)
    ok = await message_runner.deliver_now(
        wid, sub, text="请执行上文指令",
        source={"kind": "card_msg", "from": root})
    assert ok is True, "text 模式应起轮（不能因 inbox 空而返回 False）"
    await _aio.sleep(0.1)
    assert started.get("card") == sub
    assert (started.get("source") or {}).get("from") == root


# ---------------------------------------------------------------- E7
async def test_append_sync_path_also_triggers(tmp_path, monkeypatch, sandbox):
    """同步落盘路径（goal/change 走的就是它）也要能触发规则。"""
    import json as _json
    p = tmp_path / "r_sync.json"
    p.write_text(_json.dumps({"rules": [{
        "id": "goal_changed", "enabled": True,
        "trigger": {"types": ["goal/change"]},
        "guard": [], "target": "self",
        "message": "目标变更:{card_id}"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(harness_rules.ENV_RULES_PATH, str(p))
    harness_rules.reset_cache()

    wid, root, sub = await _seed()
    event_log.append_sync(wid, sub, {"type": "goal/change",
                                     "data": {"operation": "complete"}})
    await _settle()
    box = await _inbox(wid, sub)
    assert any("目标变更" in c for c in box), box
