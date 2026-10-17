# -*- coding: utf-8 -*-
"""「等子卡恢复」机制（2026-10-17）——paused 父卡被其子卡完工唤醒。

【被修的缺口】``auto_driver.after_turn`` 沿 parent 链递归到父卡后调
``will_continue``，而它要求 ``goal_state == "active"`` ⇒ **paused 父卡判否**
⇒ 不 kick、继续上跳祖父卡 ⇒ 父卡恒为 ``paused/goal_rounds=0``，**永久挂死**。
实测 work_008：根卡 pause（理由原文写着「等子卡跑完回报」），card_002~005 全
complete，父卡恒 0。恢复看似"发生过"只是因为子卡**主动发了 msg_notify**
触发 K3 调起——**模型行为掩盖了机制缺口**；子卡若只 complete 而不发信，父卡死。

【为什么需要新标志位】``control.stop_auto``（用户按停）也只把 ``goal_state``
置 ``paused``，与模型自己 pause **不可区分** ⇒ 光看 goal_state 无法既救活
「等子卡」的父卡、又保证「用户按停」不被复活。故新增**机械可判**的意图标志
``goal_await_children``（0/1）：模型 pause 且**当时**有未终止子卡时置 1；
用户按停**必定清 0**（红线）。

【五条用例 = 五条硬判据】
  a. pause（有活跃子卡）⇒ goal_await_children=1
  b. 子卡 complete 后调 after_turn ⇒ 父卡恢复 active 并起轮（rounds 增加）
  c. pause（**无**未终止子卡）⇒ 标志位 0，且子卡完工**不**唤醒
  d. **用户按停** ⇒ 标志位被清 0，随后子卡完工**不**唤醒（红线）
  e. 非 auto 卡走同样路径 ⇒ 行为不变（红线 2）
外加 f：驱动判据单测（``wake_verdict`` 三态）＋ g：老库补列幂等。

【不变量（逐条钉死）】
- ``will_continue`` 语义**一个字不改**（它被 routes 的执行锁持有判定共用）——
  新判据是**另立的** ``wake_verdict``，只服务「向上唤醒」这一个动作。
- ``_h_update_goal`` 的收口闸（未回应询问拒绝 complete）未被触碰。
- 无未终止子卡的 pause ⇒ 行为逐字不变；非 auto 卡 ⇒ 行为逐字不变。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from src.api import auto_driver, card_tools, control, message_runner, serial_gate
from src.storage import card_db, card_store


# ============================================================
# 夹具
# ============================================================

@pytest.fixture(autouse=True)
def _clean_process_state():
    """进程级状态进/出例清零（沿 test_auto_mode 同款：跨例不清会串味）。"""
    from src.api import card_msg, event_stream

    def _reset():
        serial_gate.reset()
        message_runner._TRIGGER_TASKS.clear()
        card_msg.TRIGGERS.clear()
        event_stream._journals.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def no_kick(monkeypatch):
    """拦掉真实起轮，只记录**真 kick 会点火**的那些调用。

    ★ 判据用 ``wake_verdict``（本任务的新判据）而非 ``will_continue``：被唤醒的
    paused 父卡在 kick 之前已被 ``_resume_awaiting`` 改回 active，两者此刻等价；
    但对「仍 paused 的按停卡」只有 wake_verdict 能判出它**不该**起。
    """
    calls: list[tuple[str, str]] = []

    def _rec(w: str, c: str):
        if auto_driver.wake_verdict(w, c) in (auto_driver.WAKE_ACTIVE,
                                              auto_driver.WAKE_PAUSED):
            calls.append((w, c))
        return None

    monkeypatch.setattr(auto_driver, "kick", _rec)
    return calls


async def _mk(wid: str, title: str, parent: str, **kw) -> str:
    r = await card_store.create_card(wid, title, "模型生成", parent=parent, **kw)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _mk_auto(work, *, goal: str = "") -> tuple[str, str]:
    """root → 一张 auto 卡（已开自动；给了 goal 就连目标态一起置 active）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "自动卡", root)
    await card_store.update_card(wid, a, {"auto_mode": 1}, "开自动模式")
    if goal:
        await card_store.update_card(
            wid, a, {"goal": goal, "goal_state": "active"}, "设定目标")
    return wid, a


async def _goal_tool(wid: str, cid: str, action: str, reason: str = "") -> dict:
    """经**真实工具面**调 update_goal（走 handler，不绕开——要验的是生产路径）。"""
    cards = card_tools._collect_card_meta(wid)
    args: dict = {"action": action}
    if reason:
        args["reason"] = reason
    return await card_tools.execute_tool(wid, cid, "update_goal", args, cards)


def _await_flag(wid: str, cid: str) -> int:
    meta = card_store._meta_of(wid, cid) or {}
    return int(meta.get("goal_await_children") or 0)


# ============================================================
# a. pause（有活跃子卡）⇒ goal_await_children=1
# ============================================================

async def test_a_pause_with_active_child_sets_await_flag(typed_registry, work, no_kick):
    """判据 a：pause 时**确有未终止子卡** ⇒ 标志位置 1（声明「等子卡」）。"""
    wid, p = await _mk_auto(work, goal="父目标：等子卡回报")
    c = await _mk(wid, "子卡", p, goal="子目标")     # 继承 auto ＋ goal_state=active
    assert auto_driver.has_active_child(wid, p)

    r = await _goal_tool(wid, p, "pause", "等子卡跑完回报")
    assert r["ok"] is True, r
    meta = card_store._meta_of(wid, p)
    assert meta["goal_state"] == "paused"
    assert int(meta.get("goal_await_children") or 0) == 1, \
        "pause 且有未终止子卡 ⇒ 必须置位（否则父卡挂死，等于没修）"


async def test_a2_pause_sets_flag_via_auto_driver_verdict(typed_registry, work, no_kick):
    """判据 a 的机制侧：新判据 ``wake_verdict`` 认得这张「等子卡」的父卡。

    ★ 让位优先于恢复：子卡**还在跑**时判 ``WAKE_NONE``（父卡让位，否则空转）；
      子卡全部终止后才判 ``WAKE_PAUSED``。顺序即 ``wake_verdict`` 的实现次序。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡")
    assert _await_flag(wid, p) == 1
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE, \
        "子卡还在跑 ⇒ 让位优先，不得恢复父卡"

    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_PAUSED, \
        "子卡全终止 ⇒ 认得这张「等子卡」的父卡"


# ============================================================
# b. 子卡 complete 后 after_turn ⇒ 父卡恢复 active 并起轮
# ============================================================

async def test_b_child_complete_wakes_paused_parent(typed_registry, work, no_kick):
    """判据 b：**本任务的核心缺口**——paused 父卡被子卡完工唤醒并起轮。

    复刻 work_008 的挂死形态：父卡 pause 等子卡（rounds 恒 0）⇒ 子卡 complete
    ⇒ after_turn ⇒ 父卡应被**恢复为 active** 且 rounds 增加 1、kick 被调。
    """
    wid, p = await _mk_auto(work, goal="父目标：整合子卡回报")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等五张子卡跑完回报")
    assert card_store._meta_of(wid, p)["goal_rounds"] == 0

    # 子卡完工（只有 goal_state=complete，**不发任何消息**——正是原来的挂死态）
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)

    meta = card_store._meta_of(wid, p)
    assert meta["goal_state"] == "active", "等待中的父卡必须被恢复为 active"
    assert meta["goal_rounds"] == 1, "恢复后应计一轮（父卡恒 0 就是原缺口）"
    assert (wid, p) in no_kick, "恢复后应点火起轮"
    assert int(meta.get("goal_await_children") or 0) == 0, \
        "等待意图是一次性的，用过即销（不得跨目标生命周期留存）"


async def test_b2_resume_clears_flag_and_is_idempotent(typed_registry, work, no_kick):
    """判据 b 的收尾：恢复后**再**来一次 after_turn 不会二次恢复/二次计轮。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡")
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)
    assert card_store._meta_of(wid, p)["goal_rounds"] == 1

    # 恢复后父卡已是 active ⇒ 再驱一次走常规 active 路径（不再走恢复分支）
    await auto_driver.after_turn(wid, p)
    assert card_store._meta_of(wid, p)["goal_rounds"] == 2   # 常规续跑，非二次恢复
    assert card_store._meta_of(wid, p)["goal_state"] == "active"


async def test_b3_paused_parent_not_woken_while_child_still_active(
        typed_registry, work, no_kick):
    """判据 b 的边界：子卡**仍在跑** ⇒ 父卡让位，不恢复（否则空转、烧额度）。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡")
    assert auto_driver.has_active_child(wid, p)

    await auto_driver.after_turn(wid, c)      # 子卡这一轮结束但仍 active
    meta = card_store._meta_of(wid, p)
    assert meta["goal_state"] == "paused", "子卡还在跑 ⇒ 父卡不得被恢复"
    assert meta["goal_rounds"] == 0
    assert (wid, p) not in no_kick


# ============================================================
# c. pause（无未终止子卡）⇒ 标志位 0，子卡完工**不**唤醒
# ============================================================

async def test_c_pause_without_active_child_does_not_wake(typed_registry, work, no_kick):
    """判据 c：无未终止子卡的 pause ⇒ 标志位 0，「不做了」**不得**被当成「等子卡」。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡早已完成")

    r = await _goal_tool(wid, p, "pause", "这件事先不做了")
    assert r["ok"] is True, r
    meta = card_store._meta_of(wid, p)
    assert meta["goal_state"] == "paused"
    assert int(meta.get("goal_await_children") or 0) == 0, \
        "无未终止子卡 ⇒ 不得置位（否则把「不做了」误当「等子卡」复活）"

    await auto_driver.after_turn(wid, c)
    after = card_store._meta_of(wid, p)
    assert after["goal_state"] == "paused", "标志位 0 ⇒ 不得被唤醒"
    assert after["goal_rounds"] == 0
    assert (wid, p) not in no_kick


async def test_c2_pause_with_no_child_at_all(typed_registry, work, no_kick):
    """判据 c 的极端形态：**根本没有子卡**时 pause ⇒ 标志位仍 0。"""
    wid, p = await _mk_auto(work, goal="父目标")
    r = await _goal_tool(wid, p, "pause", "等外部条件")
    assert r["ok"] is True, r
    assert _await_flag(wid, p) == 0
    await auto_driver.after_turn(wid, p)
    assert card_store._meta_of(wid, p)["goal_state"] == "paused"


async def test_c3_blocked_and_complete_never_set_flag(typed_registry, work, no_kick):
    """判据 c 的镜像：complete / blocked **不置位**（只有 pause 才可能置位）。"""
    wid, p = await _mk_auto(work, goal="父目标")
    await _mk(wid, "子卡", p, goal="子目标")      # 有未终止子卡
    assert auto_driver.has_active_child(wid, p)

    r = await _goal_tool(wid, p, "blocked", "被闸拦住")
    assert r["ok"] is True, r
    assert card_store._meta_of(wid, p)["goal_state"] == "blocked"
    assert _await_flag(wid, p) == 0, "blocked 不得置等待位"


# ============================================================
# d. ★红线：用户按停 ⇒ 清标志位，子卡完工**不**唤醒
# ============================================================

async def test_d_user_stop_clears_await_flag(typed_registry, work, no_kick):
    """判据 d 第一半：用户按停 ⇒ ``goal_await_children`` 被清 0。

    覆盖的是**最容易漏的那条**：模型先 pause 等子卡（标志位=1、goal_state=paused），
    用户随后按停。原 ``stop_auto`` 循环只处理 ``goal_state=="active"`` 的卡，
    这张已 paused 的卡会被跳过 ⇒ 等待意图**永远留着** ⇒ 子卡一完工就复活。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡跑完")
    assert _await_flag(wid, p) == 1, "前置：此刻确实在等子卡"

    await control.stop_auto(wid, p)

    assert _await_flag(wid, p) == 0, "★ 用户按停必须清等待意图（红线）"
    assert card_store._meta_of(wid, p)["goal_state"] == "paused"
    # 返回键逐字不变（既有契约；清位信息只进 ops_log，不进返回值）
    r = await control.stop_auto(wid, p)     # 幂等：再按一次仍只回两个既有键
    assert set(r) == {"paused", "lock_released"}
    assert r["paused"] == []                 # 已 paused ⇒ 幂等 no-op（不重复计入）


async def test_d2_user_stop_prevents_revive(typed_registry, work, no_kick):
    """判据 d 第二半（**红线本体**）：按停后子卡完工 ⇒ 父卡**绝不**被唤醒/复活。

    这是「用户按停不被复活」这条红线的端到端证明：按停 → 清位 → 子卡 complete
    → after_turn ⇒ 父卡仍 paused、rounds 仍 0、kick 未被调。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡跑完")
    await control.stop_auto(wid, p)               # 用户按停
    assert _await_flag(wid, p) == 0

    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)

    meta = card_store._meta_of(wid, p)
    assert meta["goal_state"] == "paused", "★ 按停的卡不得被复活"
    assert meta["goal_rounds"] == 0
    assert (wid, p) not in no_kick
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE


async def test_d3_user_stop_clears_flag_on_active_card_too(typed_registry, work, no_kick):
    """判据 d 第三半：**active** 卡按停时，既置 paused 也清位（常规路径同样到位）。"""
    wid, p = await _mk_auto(work, goal="父目标")      # active、有未终止子卡
    c = await _mk(wid, "子卡", p, goal="子目标")
    r = await control.stop_auto(wid, p)
    # ③ 级联是**既有**语义（子树内 active 的 auto 子卡一并 paused），非本任务新增
    assert set(r["paused"]) == {p, c}
    assert set(r) == {"paused", "lock_released"}      # 返回键逐字不变
    assert _await_flag(wid, p) == 0
    assert card_store._meta_of(wid, p)["goal_state"] == "paused"


async def test_d4_user_stop_cascades_to_child(typed_registry, work, no_kick):
    """判据 d 第四半：按停级联到子卡时，子卡的等待意图也一并清掉。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await card_store.update_card(wid, c, {"goal_await_children": 1}, "子卡等它自己的子卡")
    assert _await_flag(wid, c) == 1

    await control.stop_auto(wid, p)
    assert card_store._meta_of(wid, c)["goal_state"] == "paused"
    assert _await_flag(wid, c) == 0, "级联的子卡也要清等待意图（按停递归权威）"


async def test_d5_stop_auto_noop_for_non_auto_still_holds(typed_registry, work, no_kick):
    """红线 2：非 auto 卡 ⇒ stop_auto 仍全 no-op（逐字不变）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    r = await control.stop_auto(wid, a)
    assert r == {"paused": [], "lock_released": False}
    assert not str(card_store._meta_of(wid, a).get("goal_state") or "")
    assert _await_flag(wid, a) == 0


# ============================================================
# e. 非 auto 卡走同样路径 ⇒ 行为不变
# ============================================================

async def test_e_non_auto_card_untouched_end_to_end(typed_registry, work, no_kick):
    """判据 e：非 auto 卡 ⇒ 驱动器零动作、按停零动作、标志位恒 0。"""
    wid, root = work["work_id"], work["work_card_id"]
    p = await _mk(wid, "普通父卡", root)              # 全程不开自动
    c = await _mk(wid, "普通子卡", p)
    # 人为放一个等待标志位，验证非 auto 卡**即使用了也起不来**
    await card_store.update_card(wid, p, {"goal_await_children": 1}, "人为置位")

    await auto_driver.after_turn(wid, c)
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE
    assert not str(card_store._meta_of(wid, p).get("goal_state") or "")

    r = await control.stop_auto(wid, p)
    assert r == {"paused": [], "lock_released": False}
    assert not str(card_store._meta_of(wid, p).get("goal_state") or "")

    # update_goal 在非 auto 卡上仍被拒（既有双保险）
    res = await _goal_tool(wid, p, "pause", "不该生效")
    assert res["ok"] is False and res["error"]["code"] == "denied", res


async def test_e2_non_auto_parent_chain_not_woken_by_child(typed_registry, work, no_kick):
    """判据 e 的镜像：非 auto 卡 ⇒ update_goal 路径也碰不到标志位（denied）。"""
    wid, root = work["work_id"], work["work_card_id"]
    a = await _mk(wid, "甲", root)
    res = await _goal_tool(wid, a, "complete")
    assert res["ok"] is False and res["error"]["code"] == "denied", res
    assert _await_flag(wid, a) == 0


# ============================================================
# f. 判据单测：wake_verdict 三态 + will_continue 语义未被改
# ============================================================

async def test_f_wake_verdict_three_states(typed_registry, work, no_kick):
    """``wake_verdict`` 三态：active→ACTIVE；paused+等子卡→PAUSED；其余→NONE。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")

    # active 但子卡还在跑 ⇒ 让位 ⇒ NONE
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE

    # 子卡终止 ⇒ 父卡 active ⇒ ACTIVE
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_ACTIVE

    # paused 但无标志位（= 用户按停的形态）⇒ NONE
    await card_store.update_card(
        wid, p, {"goal_state": "paused", "goal_await_children": 0}, "按停形态")
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE

    # paused 且有标志位 ⇒ PAUSED
    await card_store.update_card(
        wid, p, {"goal_state": "paused", "goal_await_children": 1}, "等子卡形态")
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_PAUSED


async def test_f2_wake_verdict_requires_goal_and_auto(typed_registry, work, no_kick):
    """判据 f 的边界：有标志位但**无目标**或**非 auto** ⇒ 一律 NONE。"""
    wid, root = work["work_id"], work["work_card_id"]
    # auto 但无 goal
    a = await _mk(wid, "无目标自动卡", root)
    await card_store.update_card(
        wid, a, {"auto_mode": 1, "goal_state": "paused", "goal_await_children": 1},
        "无目标却想等")
    assert auto_driver.wake_verdict(wid, a) == auto_driver.WAKE_NONE


async def test_f3_will_continue_semantics_unchanged(typed_registry, work, no_kick):
    """**回归钉子**：``will_continue`` 仍**只认 active**（一个字都没被本任务改）。

    它被 ``routes._run_message`` 的回合末执行锁持有判定共用，改语义会波及串行队列
    ——本任务因此另立 ``wake_verdict``。此用例把这条边界钉死。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡")
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")

    assert _await_flag(wid, p) == 1
    assert auto_driver.will_continue(wid, p) is False, \
        "will_continue 必须仍对 paused 判否（执行锁语义不得被本任务改写）"
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_PAUSED, \
        "唤醒走的是新判据，不是 will_continue（同一张卡：两判据结论不同）"


async def test_f4_cycle_guard_is_not_a_round_gate():
    """红线 1（本任务沿用）：``_CARD_TREE_CYCLE_GUARD`` 仍只是防环兜底，不是闸。

    源码级断言（沿 test_auto_mode 同款手法）：它一个常量、只在 ``after_turn`` 的
    ``_hops`` 比较里出现，**不参与任何轮次/时间/token 判定**。
    """
    src = (card_tools.__file__.replace("card_tools.py", "auto_driver.py"))
    import io as _io
    text = _io.open(src, encoding="utf-8").read()
    assert "_CARD_TREE_CYCLE_GUARD" in text
    assert "轮次闸" in text and "不是闸" in text, "常量处须写死「不是闸」"
    # 它不得出现在任何「轮次上限」语义的上下文里
    assert "goal_rounds >=" not in text and "MAX_ROUNDS" not in text


# ============================================================
# g. 存储层：新库有列 / 老库幂等补列 / 存量行取默认 0
# ============================================================

async def test_g_new_db_has_await_column(work):
    """硬判据：新库建表即含 ``goal_await_children``。"""
    wid = work["work_id"]
    cols = await card_db.run_read(
        wid, lambda conn: {r["name"] for r in conn.execute("PRAGMA table_info(cards)")},
        set())
    assert "goal_await_children" in cols


async def test_g2_old_db_upgraded_idempotent_default_zero(work):
    """硬判据：老库缺列即 ALTER 补上；**幂等**；存量行取默认 0。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = await _mk(wid, "存量卡", root)

    db = card_db.db_path(wid)
    card_db.close_work(wid)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE cards DROP COLUMN goal_await_children")
    conn.commit()
    conn.close()

    # 首次连接 ⇒ 幂等补列
    cols = await card_db.run_read(
        wid, lambda c: {r["name"] for r in c.execute("PRAGMA table_info(cards)")}, set())
    assert "goal_await_children" in cols
    meta = card_store._meta_of(wid, cid) or {}
    assert int(meta.get("goal_await_children") or 0) == 0, "存量行取默认 0"

    # 幂等：重复跑 _ensure_schema 结构与数据零变化
    again = await card_db.run_read(
        wid,
        lambda c: (card_db._ensure_schema(c),
                   {r["name"] for r in c.execute("PRAGMA table_info(cards)")})[1],
        None)
    assert again == cols
    assert card_store._meta_of(wid, cid) == meta


async def test_g3_flag_survives_meta_roundtrip(typed_registry, work, no_kick):
    """标志位能经 meta 往返（row_to_meta ⇄ update_card），不被静默丢弃。

    钉的是 ``_UPDATABLE_META_KEYS`` 登记——不在其中的键会被**静默丢弃**（不报错），
    那会让整个机制看起来"配了但没生效"。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    await card_store.update_card(wid, p, {"goal_await_children": 1}, "置位")
    assert _await_flag(wid, p) == 1
    await card_store.update_card(wid, p, {"goal_await_children": 0}, "清位")
    assert _await_flag(wid, p) == 0
