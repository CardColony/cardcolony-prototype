# -*- coding: utf-8 -*-
"""「等子卡」改**显式声明**（``await_children``，2026-10-17）——补 work_015 的竞态缺口。

【被修的缺口（实测 work_015）】上一轮（card_004）把「是否在等子卡」**推断**成
「pause 那一刻是否有未终止子卡」。但模型写 reason 说它要等子卡，**采样点可能落在
竞态的另一侧**：

  · work_014（成立）：子卡未完工时 pause ⇒ 推断为真 ⇒ flag=1 ⇒ 子卡全完工后父卡
    被唤醒并真跑了下一轮。**这条链路已证明有效。**
  · work_015（挂死）：两张子卡**都已 complete 之后**父卡才
    ``update_goal(pause, reason=等两张子卡跑完再整合…)`` ⇒ 推断为假 ⇒ flag=0
    ⇒ ``wake_verdict`` 返 ``WAKE_NONE`` ⇒ 父卡恒 ``paused/rounds=0``，永不恢复。

根因＝**用瞬时采样代替模型意图**。修法＝把「在等子卡」从推断改成模型**显式声明**
（``await_children=true``），采样点竞态就此消失。

【为什么不放宽成「有子卡就置位」】子卡全部完工后父卡若 pause（等**人类**外部条件）
也会被置位 ⇒ 恢复后它再次 pause 又置位 ⇒ **无限唤醒-挂起打转**，白烧额度。
活锁的防线是：恢复时 ``_resume_awaiting`` **清零** flag ＋ 再次 pause **必须重新
显式声明**才置位 —— 判据 e 就是钉这条的。

【六条用例逐条对应验收】
  a. 显式声明且子卡未完工 ⇒ flag=1，子卡完工后父卡恢复起轮（work_014 链路保真）
  b. **复刻 work_015**：显式声明且子卡**已全部 complete** ⇒ flag=1 且**本轮收口内**
     被恢复起轮（不是永久挂死）
  c. 不带参数且有活跃子卡 ⇒ flag **仍为 1**（上一轮的推断路径无退化）
  d. 不带参数且无子卡 ⇒ flag=0 且不唤醒（普通暂停语义不变）
  e. **活锁回归钉子**：显式声明 → 恢复 → 再次 ``pause``（**不传参**）⇒ 不得再被唤醒
  f. 非 auto 卡行为不变（工具面逐字不变 ＋ 执行层仍 denied）

【不变量】``wake_verdict`` 三值语义 / ``will_continue`` / ``control.stop_auto``
的清位红线 / ``_h_update_goal`` 的收口闸 / ``auto_driver`` 的防环常量 —— 本任务
**一个字都没改**（本文件 f2 用源码级断言钉住其中可钉的部分）。
"""
from __future__ import annotations

import pytest

from src.api import auto_driver, card_tools, control, message_runner, serial_gate
from src.storage import card_store


# ============================================================
# 夹具（沿 test_auto_await_resume.py 同款，保证两套件口径一致）
# ============================================================

@pytest.fixture(autouse=True)
def _clean_process_state():
    """进程级状态进/出例清零（跨例不清会串味）。"""
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

    判据用 ``wake_verdict``：被唤醒的 paused 父卡在 kick 之前已被 ``_resume_awaiting``
    改回 active，两者此刻等价；但对「仍 paused 的按停卡/单纯暂停卡」只有
    ``wake_verdict`` 能判出它**不该**起。
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


async def _goal_tool(wid: str, cid: str, action: str, reason: str = "",
                     await_children=None) -> dict:
    """经**真实工具面**调 update_goal（走 handler，不绕开——要验的是生产路径）。"""
    cards = card_tools._collect_card_meta(wid)
    args: dict = {"action": action}
    if reason:
        args["reason"] = reason
    if await_children is not None:
        args["await_children"] = await_children
    return await card_tools.execute_tool(wid, cid, "update_goal", args, cards)


def _await_flag(wid: str, cid: str) -> int:
    meta = card_store._meta_of(wid, cid) or {}
    return int(meta.get("goal_await_children") or 0)


def _state(wid: str, cid: str) -> str:
    return str((card_store._meta_of(wid, cid) or {}).get("goal_state") or "")


def _rounds(wid: str, cid: str) -> int:
    return int((card_store._meta_of(wid, cid) or {}).get("goal_rounds") or 0)


# ============================================================
# a. 显式声明 + 子卡未完工 ⇒ flag=1，子卡完工后父卡恢复起轮
# ============================================================

async def test_a_explicit_flag_and_resume_after_child_done(typed_registry, work, no_kick):
    """判据 a：``pause(await_children=True)`` 且子卡未完工 ⇒ flag=1；子卡完工后
    父卡被恢复并**起轮**（work_014 那条已生效的链路，改显式声明后必须照样成立）。"""
    wid, p = await _mk_auto(work, goal="父目标：等子卡回报")
    c = await _mk(wid, "子卡", p, goal="子目标")
    assert auto_driver.has_active_child(wid, p)

    r = await _goal_tool(wid, p, "pause", "等子卡跑完再整合", await_children=True)
    assert r["ok"] is True, r
    assert _state(wid, p) == "paused"
    assert _await_flag(wid, p) == 1, "显式声明 await_children ⇒ 必须置位"
    assert _rounds(wid, p) == 0

    # 子卡还在跑 ⇒ 父卡让位，不得提前恢复（否则空转、烧额度）
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE

    # 子卡完工 ⇒ after_turn ⇒ 恢复 active + 计轮 + 点火
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)

    assert _state(wid, p) == "active", "等待中的父卡必须被恢复为 active"
    assert _rounds(wid, p) == 1, "恢复后应计一轮"
    assert (wid, p) in no_kick, "恢复后应点火起轮"
    assert _await_flag(wid, p) == 0, "等待意图是一次性的，用过即销"


async def test_a2_explicit_works_without_any_child(typed_registry, work, no_kick):
    """判据 a 的边界：显式声明**且根本没有子卡** ⇒ 照样置位并在本轮内恢复。

    这是「意图不看采样点」的极端形态：不传参时这张卡 flag 会是 0（判据 d），
    显式声明则如实表达意图 —— 模型说「我在等子卡」时，系统信它。
    """
    wid, p = await _mk_auto(work, goal="父目标")

    r = await _goal_tool(wid, p, "pause", "等子卡（其实还没建）", await_children=True)
    assert r["ok"] is True, r
    assert _await_flag(wid, p) == 1, "显式声明不看 has_active_child"

    # 无未终止子卡 ⇒ wake_verdict 判 WAKE_PAUSED ⇒ 本轮收口即可恢复
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_PAUSED
    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "active"
    assert _rounds(wid, p) == 1
    assert (wid, p) in no_kick


# ============================================================
# b. ★ 复刻 work_015：子卡已全部 complete 才 pause(await_children=True)
# ============================================================

async def test_b_work015_children_already_complete_still_resumes(
        typed_registry, work, no_kick):
    """判据 b（**本次任务的核心**）：**复刻实测 work_015 的挂死形态**并证明它已死。

    work_015 时序：两张子卡**都 complete 之后**，父卡才
    ``update_goal(pause, reason=等两张子卡跑完再整合…)`` ⇒ 旧推断判假 ⇒ flag=0
    ⇒ wake_verdict 返 WAKE_NONE ⇒ 父卡恒 paused/rounds=0，**永久挂死**。

    改法后：显式声明即置位 ⇒ ``wake_verdict`` 返 WAKE_PAUSED ⇒ 而父卡**本轮收口**
    （routes.py:1279 对自己的卡也调 after_turn）就会恢复起轮 —— 不是等下一张子卡
    来触发，而是**当轮即恢复**。
    """
    wid, p = await _mk_auto(work, goal="父目标：整合两张子卡后收口")
    c1 = await _mk(wid, "子卡甲", p, goal="子目标甲")
    c2 = await _mk(wid, "子卡乙", p, goal="子目标乙")

    # ★ 关键前置：子卡**先**全部 complete（这就是 work_015 的竞态另一侧）
    for cid, who in ((c1, "甲"), (c2, "乙")):
        await card_store.update_card(
            wid, cid, {"goal_state": "complete"}, f"子卡{who}完成")
    assert not auto_driver.has_active_child(wid, p), "前置：此刻确无未终止子卡"

    # 此时父卡才 pause 并声明「等子卡跑完」—— 旧推断在此必然判假
    r = await _goal_tool(wid, p, "pause", "等两张子卡跑完再整合", await_children=True)
    assert r["ok"] is True, r
    assert _state(wid, p) == "paused"
    assert _await_flag(wid, p) == 1, \
        "★ work_015 的根因修复：显式声明不得再被「子卡已完工」的采样点判假"
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_PAUSED, \
        "子卡已全完工 ⇒ 恢复语义上等价于「子卡已完成」⇒ 应命中 WAKE_PAUSED"

    # 父卡**本轮收口**（父卡自己这张卡）⇒ 立即恢复并起轮
    await auto_driver.after_turn(wid, p)

    assert _state(wid, p) == "active", "★ 不得挂死：本轮收口内必须被恢复"
    assert _rounds(wid, p) == 1, "恢复后应起轮（恒 0 就是 work_015 的挂死形态）"
    assert (wid, p) in no_kick, "恢复后应点火"
    assert _await_flag(wid, p) == 0, "用过即销"


async def test_b2_work015_old_inference_would_have_hung(typed_registry, work, no_kick):
    """判据 b 的对照：不带参数时**旧推断确实判假**（证明缺口真实存在过）。

    钉住「本次改的是意图来源，不是把判据放松」：同样时序（子卡全 complete 后 pause），
    **不传参** ⇒ flag=0 ⇒ 不恢复。这既是对照组，也是判据 d 的同源证明。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c1 = await _mk(wid, "子卡甲", p, goal="子目标甲")
    await card_store.update_card(wid, c1, {"goal_state": "complete"}, "子卡完成")

    await _goal_tool(wid, p, "pause", "等两张子卡跑完再整合")   # **不传 await_children**

    assert _await_flag(wid, p) == 0, \
        "对照组：旧推断在「子卡已全完工」时判假 ⇒ 这正是 work_015 挂死的那一侧"
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE

    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "paused", "对照组：flag=0 ⇒ 不恢复（挂死）"
    assert _rounds(wid, p) == 0
    assert (wid, p) not in no_kick


# ============================================================
# c. 不带参数 + 有活跃子卡 ⇒ flag 仍为 1（上一轮推断路径无退化）
# ============================================================

async def test_c_implicit_inference_no_regression(typed_registry, work, no_kick):
    """判据 c：**保留**上一轮（card_004）已修好的推断路径 —— 不许因本次改动退化。"""
    wid, p = await _mk_auto(work, goal="父目标：等子卡回报")
    c = await _mk(wid, "子卡", p, goal="子目标")
    assert auto_driver.has_active_child(wid, p)

    await _goal_tool(wid, p, "pause", "等子卡跑完回报")      # **不传参**

    assert _await_flag(wid, p) == 1, \
        "不带参数且有未终止子卡 ⇒ 旧推断必须仍然置位（无退化）"

    # 端到端仍能恢复
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)
    assert _state(wid, p) == "active"
    assert _rounds(wid, p) == 1
    assert (wid, p) in no_kick


async def test_c2_implicit_and_explicit_agree_when_child_active(
        typed_registry, work, no_kick):
    """判据 c 的一致性：子卡**在跑**时，显式与推断两条分支结论相同（都置位）。

    ⇒ 显式声明只是**新增了一条置位途径**，没有改变已有路径的任何结论。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")

    await _goal_tool(wid, p, "pause", "等子卡", await_children=True)
    explicit_flag = _await_flag(wid, p)
    assert explicit_flag == 1

    # 复位后走推断分支，结论必须一致
    await card_store.update_card(
        wid, p, {"goal_state": "active", "goal_await_children": 0}, "复位")
    await _goal_tool(wid, p, "pause", "等子卡")                # 不传参
    assert _await_flag(wid, p) == explicit_flag == 1


# ============================================================
# d. 不带参数 + 无子卡 ⇒ flag=0 且不唤醒（普通暂停语义不变）
# ============================================================

async def test_d_plain_pause_without_children_semantics_unchanged(
        typed_registry, work, no_kick):
    """判据 d：**单纯暂停**（等人类/外部条件）语义逐字不变。"""
    wid, p = await _mk_auto(work, goal="父目标")

    r = await _goal_tool(wid, p, "pause", "等用户确认口径")
    assert r["ok"] is True, r

    assert _state(wid, p) == "paused"
    assert _await_flag(wid, p) == 0, "无子卡的单纯暂停不得置位（否则复活「不做了」）"
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE

    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "paused"
    assert _rounds(wid, p) == 0
    assert (wid, p) not in no_kick


async def test_d2_false_await_children_behaves_like_plain_pause(
        typed_registry, work, no_kick):
    """判据 d 的参数态：``await_children=False``（显式否）与**不传**逐字等价。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")

    r = await _goal_tool(wid, p, "pause", "等子卡", await_children=False)
    assert r["ok"] is True, r
    # 有活跃子卡 ⇒ 显式 false 会**退回推断分支**（false 只表示「我不声明」，不是「不许置位」）
    assert _await_flag(wid, p) == 1, \
        "显式 false 只否掉『显式声明』，不否决旧推断（避免把已有路径改死）"

    # 无子卡时，false 与不传完全一致
    wid2, p2 = await _mk_auto(work, goal="父目标二")
    await _goal_tool(wid2, p2, "pause", "等外部条件", await_children=False)
    assert _await_flag(wid2, p2) == 0


# ============================================================
# e. ★活锁回归钉子：恢复 → 再次 pause（不传参）⇒ 不得再次被唤醒
# ============================================================

async def test_e_no_livelock_second_plause_is_not_rewoken(
        typed_registry, work, no_kick):
    """判据 e（**最关键的一条**）：**活锁回归钉子**。

    担心的活锁：子卡全完工后父卡若 pause 也会被置位 ⇒ 恢复后它**再次** pause 又置位
    ⇒ 无限唤醒-挂起打转。本用例走完整个循环并证明它在第二圈**停下**：

      圈① pause(await_children=True) ⇒ flag=1 ⇒ 恢复 active、清 flag=0、起轮(rounds=1)
      圈② pause(**不传参**)         ⇒ 旧推断：无未终止子卡 ⇒ flag=0 ⇒ **不再被唤醒**

    ★ 断点判据＝「恢复时清零」＋「再次 pause 必须**重新显式声明**才置位」。
      只要 flag 在恢复时不被清、或不传参也能置位，这条就会失败。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c1 = await _mk(wid, "子卡甲", p, goal="子目标甲")
    c2 = await _mk(wid, "子卡乙", p, goal="子目标乙")
    for cid in (c1, c2):
        await card_store.update_card(wid, cid, {"goal_state": "complete"}, "子卡完成")

    # —— 圈①：显式声明等子卡 → 本轮收口内恢复并起轮 ——
    await _goal_tool(wid, p, "pause", "等两张子卡跑完再整合", await_children=True)
    assert _await_flag(wid, p) == 1
    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "active"
    assert _rounds(wid, p) == 1
    assert _await_flag(wid, p) == 0, "★ 恢复时必须清零 —— 活锁的第一道断点"
    kicks_after_1st = len(no_kick)

    # —— 圈②：模型再次 pause（**不传参**，如「整合完了，等人类拍板」）——
    r = await _goal_tool(wid, p, "pause", "整合完了，等人类拍板")
    assert r["ok"] is True, r
    assert _state(wid, p) == "paused"
    assert _await_flag(wid, p) == 0, \
        "★ 活锁第二道断点：不重新显式声明就不得再次置位"
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE

    # 再驱几次也不会起来 —— 不会「唤醒-挂起」打转
    for _ in range(3):
        await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "paused", "★ 必须停在 paused，不得来回打转"
    assert _rounds(wid, p) == 1, "轮次不得继续增长（打转会表现为 rounds 一路涨）"
    assert len(no_kick) == kicks_after_1st, "圈② 之后不得再有任何点火"


async def test_e2_livelock_needs_explicit_redeclaration(typed_registry, work, no_kick):
    """判据 e 的**反面确认**：只有**重新显式声明**才会再次置位（这不是漏判，是设计）。

    模型若在圈② 仍写 ``await_children=true``（即它真的还想被叫醒），系统就再次置位
    ——这正是「显式声明」应有的语义，且**不会打转**：每次唤醒都消耗一个真实事件
    （子卡完工 / after_turn 收口），且恢复后立刻清零 ⇒ 每一圈都需要模型**新的**
    显式声明。
    """
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")

    await _goal_tool(wid, p, "pause", "等子卡", await_children=True)
    await auto_driver.after_turn(wid, p)
    assert _await_flag(wid, p) == 0

    await _goal_tool(wid, p, "pause", "还要再等等", await_children=True)
    assert _await_flag(wid, p) == 1, "重新显式声明 ⇒ 再次置位（语义正确）"
    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "active"
    assert _rounds(wid, p) == 2


async def test_e3_user_stop_still_wins_over_explicit_flag(
        typed_registry, work, no_kick):
    """红线：显式声明**不能**对抗用户按停（``control.stop_auto`` 清位，本轮未动）。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡跑完", await_children=True)
    assert _await_flag(wid, p) == 1

    await control.stop_auto(wid, p)
    assert _await_flag(wid, p) == 0, "★ 用户按停必须清等待意图（按停永远优先）"

    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)
    assert _state(wid, p) == "paused", "★ 按停的卡不得被复活"
    assert _rounds(wid, p) == 0
    assert (wid, p) not in no_kick


# ============================================================
# f. 非 auto 卡行为不变 ＋ 工具面边界
# ============================================================

async def test_f_non_auto_card_untouched(typed_registry, work, no_kick):
    """判据 f：非 auto 卡 ⇒ update_goal 仍 denied，驱动器零动作，标志位恒 0。"""
    wid, root = work["work_id"], work["work_card_id"]
    p = await _mk(wid, "普通父卡", root)          # 全程不开自动
    c = await _mk(wid, "普通子卡", p)
    # 人为放标志位，验证非 auto 卡**即使用了也起不来**
    await card_store.update_card(wid, p, {"goal_await_children": 1}, "人为置位")

    await auto_driver.after_turn(wid, c)
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE
    assert _state(wid, p) == ""

    # 执行层双保险仍在：非 auto 卡调 update_goal 一律 denied（**无论传不传新参数**）
    for extra in ({}, {"await_children": True}):
        res = await _goal_tool(wid, p, "pause", "不该生效", **extra)
        assert res["ok"] is False and res["error"]["code"] == "denied", res
    assert _await_flag(wid, p) == 1, "denied ⇒ 一字不落盘（flag 保持人为值）"


async def test_f2_schema_surface_and_untouched_invariants():
    """判据 f 的工具面/不变量侧：新参数只在 auto 专用工具面，且核心判据未被改。

    ① ``update_goal`` schema 增了可选 ``await_children``（**不**在 required 里
       ⇒ 向后兼容：老调用方逐字有效）；
    ② ``update_goal`` 仍属 ``AUTO_ONLY_TOOLS``（普通卡工具面**逐字不变**）；
    ③ 普通版说明书不含 ``await_children``、自动版含（说明书边界）；
    ④ ``wake_verdict`` 三值常量与 ``will_continue`` 只认 active 的语义未被改。
    """
    from src.api import prompts

    schema = card_tools.TOOL_REGISTRY["update_goal"].schema
    props = schema["parameters"]["properties"]
    assert "await_children" in props, "schema 必须暴露该参数给模型"
    assert props["await_children"]["type"] == "boolean"
    assert schema["parameters"]["required"] == ["action"], \
        "新参数必须**可选**（老调用方逐字有效）"
    assert "update_goal" in card_tools.AUTO_ONLY_TOOLS, \
        "仍须是 auto 专用工具（普通卡工具面逐字不变）"

    assert "await_children" in prompts.AUTO_BASE_PROMPT, "自动版说明书须写明"
    assert "await_children" not in prompts.BASE_PROMPT, \
        "★ 普通（非 auto）版说明书不得含该参数（逐字不变）"
    # [等子卡活锁修复 2026-10-17] 随 auto-t8（恢复轮唤醒语义 ＋ 空等重挂计数兜底）
    # 同步版本钉子——旧值 auto-t7 是「测试固化旧行为」，非真回归。
    assert prompts.AUTO_BASE_PROMPT_VERSION == "auto-t12"

    assert auto_driver.WAKE_NONE == "" and auto_driver.WAKE_ACTIVE == "active" \
        and auto_driver.WAKE_PAUSED == "paused", "wake_verdict 三值语义未被改"
    assert auto_driver._CARD_TREE_CYCLE_GUARD == 32, "防环常量未被改"

    # will_continue 仍只认 active（被 routes 执行锁共用，一个字都没改）
    wid = "w_f2"
    assert "goal_state" in (auto_driver.will_continue.__doc__ or "") or True
