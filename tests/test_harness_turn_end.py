# -*- coding: utf-8 -*-
"""[第一个系统消息事件 2026-10-26] 正常完成所有动作（有 done 事件）但没向上汇报 ⇒ 唤醒提醒。

**判据全部基于事件**（用户令：明明有事件机制，不许再拿卡的状态快照当判据）。
[2026-10-31 · 两代并轨] 本文件改走**条件集路径**：emit → event_log 落盘 →
policy_runner.feed → 值班员 → harness.deliver → 会话块。策略内容与
config/harness_policies.json 里迁移后的 ``turn_end_without_report`` 一致
（这里用副本加载，不受真配置里其余策略影响）。

E1 本轮 done + 没汇报 ⇒ 收到提醒
E2 本轮 done + 已汇报 ⇒ 不发
E3 本轮出错（只有 error，没有 done）⇒ 不发
E4 总裁卡 / 职能卡 ⇒ 不发（不在 card_types）
E5 每个没汇报的 done 都催（提醒自己的 harness_message 重新武装本子）
E6 上一轮汇报过、本轮没有 ⇒ 仍然要发（判据按"本轮"，不是"本卡历史"）
"""
from __future__ import annotations

import json

import pytest

from src.api import event_stream
from src.infra import event_hub, fact_store as fs
from src.infra import policy_runner, policy_set as ps
from src.storage import card_db, card_store, event_log, work_store

#: 与 config/harness_policies.json 里的迁移规则同形（副本，互不影响）
_TURN_END_POLICY = {
    "id": "turn_end_without_report", "active": True,
    "scope": {"kind": "card"},
    "watch": ["run_start", "done", "tool_result",
              "msg_directive", "harness_message"],
    "window": "none",
    "when": {"all": [
        {"event": {"types": ["done"], "within": "turn",
                   "where": {"card_type": {
                       "in": ["worker", "sub_project", "topic"]}}}},
        {"not": {"event": {"types": ["tool_result"], "within": "turn",
                           "where": {"tool": "report_up", "ok": True}}}},
        {"any": [
            {"event": {"types": ["msg_directive"],
                       "where": {"from": "$parent"}}},
            {"event": {"types": ["harness_message"],
                       "where": {"rule_id": "turn_end_without_report"}}}]},
    ]},
    "then": {"target": "self", "mode": "message", "wake": True,
             "message": "你这一轮正常完成了所有动作，但没有向上汇报。"
                        "请调 report_up(kind=done)。"},
}

#: 同步路径验证用的小策略（goal/change 走 append_sync 落盘）
_GOAL_POLICY = {
    "id": "goal_changed", "active": True,
    "scope": {"kind": "card"}, "watch": ["goal/change"], "window": "none",
    "when": {"event": {"types": ["goal/change"], "within": "turn"}},
    "then": {"target": "self", "mode": "message", "wake": False,
             "message": "目标变更:{card_id}"},
}


@pytest.fixture(autouse=True)
def _policies(tmp_path, monkeypatch):
    """把条件集两个单例指到临时数据集 + 测试策略表。"""
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    p = tmp_path / "policies.json"
    p.write_text(json.dumps(
        {"policies": [_TURN_END_POLICY, _GOAL_POLICY]},
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ECO_POLICIES_PATH", str(p))

    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    from src.infra import policy_config
    pset = ps.PolicySet()
    errs = pset.load(policy_config.load_raw())
    assert errs == []
    ps.reset_set(pset)
    event_hub.reset_state()
    yield
    event_hub.reset_state()
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


async def _seed():
    w = await work_store.create_work("完工汇报提醒")
    wid = w["work_id"]
    root = work_store.work_card_id_of(wid)
    sub = (await card_store.create_card(wid, "作业卡", "模型生成",
                                        card_type="sub_project",
                                        parent=root))["meta"]["card_id"]
    # 父卡用 direct 派活 —— 策略的前置条件（只对"父卡派发的任务"提醒）
    event_stream.emit(wid, sub, "msg_directive",
                      {"from": root, "content": "把这件事做完"})
    await _settle()
    return wid, root, sub


async def _settle():
    await event_log.drain()
    await policy_runner.drain()
    await event_hub.flush_all()


async def _emit(wid, cid, ev_type, data=None):
    event_stream.emit(wid, cid, ev_type, data or {})
    await _settle()


async def _turn(wid, cid, *, report: bool = False, sync: bool = False,
                error: bool = False, card_type: str = "sub_project"):
    """模拟一个完整回合的事件序列（真实顺序：run_start … turn_end → done）。

    ``done`` 事件**自带 card_type**（生产链路已改），策略的卡类型过滤只认事件字段。
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
async def test_every_unreported_done_nags(sandbox):
    """与旧规则同语义：**每个**没汇报的 done 都催，不是只催一次。

    条件集触发即撕本子，"曾被父卡派活"这个事实会跟着本子一起没——所以策略
    靠**本提醒自己的 harness_message**（投递留痕事件）重新武装下一个本子。
    """
    wid, root, sub = await _seed()
    await _turn(wid, sub, report=False)
    await _turn(wid, sub, report=False)
    box = await _inbox(wid, sub)
    nags = [c for c in box if "没有向上汇报" in c]
    assert len(nags) == 2, nags


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
    """本轮只做了 sync（横向同步事项）⇒ **仍算没汇报**，要提醒。

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
async def test_failed_report_still_counts_as_unreported(sandbox):
    """调了 report_up 但失败 ⇒ 仍算没汇报，要提醒。

    判据精确到「调了哪个工具、成没成功」——只看 tool_result 的 tool/ok 字段。
    """
    wid, root, sub = await _seed()
    await _emit(wid, sub, "run_start", {})
    await _emit(wid, sub, "tool_call", {"tool": "report_up"})
    await _emit(wid, sub, "tool_result", {"tool": "report_up", "ok": False})
    await _emit(wid, sub, "turn_end", {})
    await _emit(wid, sub, "done", {"card_type": "sub_project"})
    box = await _inbox(wid, sub)
    assert any("没有向上汇报" in c for c in box), box


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
async def test_append_sync_path_also_triggers(sandbox):
    """同步落盘路径（goal/change 走的就是它）也要能触发策略。"""
    wid, root, sub = await _seed()
    event_log.append_sync(wid, sub, {"type": "goal/change",
                                     "data": {"operation": "complete"}})
    await _settle()
    box = await _inbox(wid, sub)
    assert any("目标变更" in c for c in box), box
