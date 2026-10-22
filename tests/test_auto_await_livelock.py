# -*- coding: utf-8 -*-
"""「等子卡」活锁修复（``goal_await_wakes`` ＋ 恢复轮唤醒事实，2026-10-17）。

【被修的活锁（实测 work_017，6702 实例账本原文）】两张子卡**早已全部 complete**，
父卡却被子卡完工唤醒 → 恢复 active 起轮 → 模型读到自己上一轮的 reason
「等子卡跑完再整合」→ **看不到任何变化**（``kick`` 用 ``append_user=False``，
激励文不落会话块，模型在会话里看不到「你已被唤醒」）→ 再次
``pause(await_children=true)`` → 又被唤醒 ⇒ 「唤醒-挂起」来回打转烧额度
（实测 rounds 采样 3,3,3,3,3 单调不降，update_goal 全是同一句 pause reason）。

【修法三层】
  ① 恢复轮激励文带**唤醒事实**（主力）：``goal_prompt(resume_note=…)`` 追加
     「你等的子卡已全部完工、你已被唤醒、别再 await_children=true」；
  ② 机械兜底：**连续空等重挂**（``await_children=true`` 而此刻并无未终止子卡）
     计数 ``goal_await_wakes`` ≥ ``_AWAIT_RESUME_DEADLOCK_GIVEUP``（3）且此刻仍无
     未终止子卡 ⇒ 不再恢复，留 ``ops_log("auto_await_resume_giveup")``；
     ★ 口径裁定（与任务书原拟「resume 时 +1」的偏差，已在 auto_driver 常量注释说明）：
       计数点在 **pause 声明处**——①对齐「第 3 次起不再恢复」的实测序列（resume 时
       +1 会变成第 4 次才拒）；②真等待清零 ⇒ 正常「等→醒→干活→再等」循环计数永远
       攒不起来，不会误伤合法的重复等待。
  ③ 说明书 auto-t8：唤醒语义准则 ＋ await_children 参数「只在当下确有未完工子卡时传」。

【本文件用例 ↔ 验收标准】
  a. 恢复轮激励文含「子卡已全部完工 / 你已被唤醒」类明确事实（且常规续轮逐字不变）
  b. 复刻 work_017：空等重挂第 1、2 次仍可恢复；第 3 次起不再恢复、保持 paused、
     有 ops 留痕 ``auto_await_resume_giveup``
  c. 正常路径不受影响：真等待（确有未终止子卡）清零计数；子卡在跑时计数不参与
     任何判定（父卡让位先行）；子卡完工后照常恢复起轮
  d. 用户按停 ⇒ 仍不复活（红线回归钉子；stop_auto 清位红线未被本次改动触碰）
  e. 非 auto 卡行为不变（含：新列不进用户 PATCH 面、kick 对外签名不变）
  f. 存储面：新列三处登记（新库建表 / 存量库补列 / create_card 落 0）＋
     目标生命周期重置归零 ＋ 说明书 auto-t8 面。

【不变量】``wake_verdict`` 三值语义 / ``will_continue`` / ``control.stop_auto`` 的
清位红线 / ``_h_update_goal`` 的收口闸（complete 拒绝路径）/ ``_CARD_TREE_CYCLE_GUARD``
—— 一个字都没改（本文件 d/e/f 组钉住其中可钉的部分）。
"""
from __future__ import annotations

import inspect

import pytest

from src.api import auto_driver, card_tools, control, message_runner, serial_gate
from src.storage import card_db, card_store


# ============================================================
# 夹具（沿 test_auto_await_explicit.py 同款，保证三套件口径一致）
# ============================================================

@pytest.fixture(autouse=True)
def _clean_process_state():
    """进程级状态进/出例清零（跨例不清会串味）。"""
    from src.api import card_msg, event_stream

    def _reset():
        serial_gate.reset()
        # 派生任务**先取消再清册**：建卡即起跑钩子可能真派生过 _run_message 任务
        # （例如某例没挂 no_kick），清册只是忘掉引用、任务还在跑 ⇒ 跨用例残留，
        # 下一个用例的 loop 拆卸时取消不干净会挂死（faulthandler 实测：
        # _cancel_all_tasks → run_forever 永不返回）。
        for _t in list(message_runner._TRIGGER_TASKS):
            _t.cancel()
        message_runner._TRIGGER_TASKS.clear()
        card_msg.TRIGGERS.clear()
        event_stream._journals.clear()
        # 一次性唤醒事实标记也必须清（防上一例残留把本例的常规轮污染成恢复轮）
        auto_driver._PENDING_RESUME_NOTE = None

    _reset()
    yield
    _reset()


@pytest.fixture
def no_kick(monkeypatch):
    """拦掉真实起轮，只记录**真 kick 会点火**的那些调用（判据同 explicit 套件）。"""
    calls: list[tuple[str, str]] = []

    def _rec(w: str, c: str):
        if auto_driver.wake_verdict(w, c) in (auto_driver.WAKE_ACTIVE,
                                              auto_driver.WAKE_PAUSED):
            calls.append((w, c))
        return None

    monkeypatch.setattr(auto_driver, "kick", _rec)
    return calls


@pytest.fixture
def kick_capture(monkeypatch):
    """拦 ``message_runner._spawn_tracked``：抓 kick 组装出的**激励文原文**。

    拿到协程即 close（不执行、不落任何会话块）——只验 ``goal_prompt`` 的产物。
    """
    captured: list[tuple[str, str]] = []   # (card_id, content)

    def _rec(coro):
        try:
            loc = coro.cr_frame.f_locals
            captured.append((str(loc.get("card_id", "")),
                             str(loc.get("content", ""))))
        except Exception:  # noqa: BLE001 拿不到就记空（不因测试钩子挂掉）
            captured.append(("", ""))
        coro.close()
        return None

    monkeypatch.setattr(message_runner, "_spawn_tracked", _rec)
    return captured


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


def _wakes(wid: str, cid: str) -> int:
    return int((card_store._meta_of(wid, cid) or {}).get("goal_await_wakes") or 0)


def _await_flag(wid: str, cid: str) -> int:
    return int((card_store._meta_of(wid, cid) or {}).get("goal_await_children") or 0)


def _state(wid: str, cid: str) -> str:
    return str((card_store._meta_of(wid, cid) or {}).get("goal_state") or "")


def _rounds(wid: str, cid: str) -> int:
    return int((card_store._meta_of(wid, cid) or {}).get("goal_rounds") or 0)


def _giveup_rows(work) -> list[dict]:
    """读沙箱 ops 档案里的 ``auto_await_resume_giveup`` 留痕（原始 jsonl 逐行）。"""
    import json
    p = card_store.WORKS_DIR / "_ops" / "session_ops.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("action") == "auto_await_resume_giveup":
            out.append(row)
    return out


# ============================================================
# a. 恢复轮激励文含「子卡已全部完工 / 你已被唤醒」类明确事实
# ============================================================

async def test_a_resume_prompt_carries_wake_facts(work, kick_capture):
    """验收 a（主力修复）：恢复轮的激励文必须带**明确事实**。

    work_017 的根因＝模型读不到「你已被唤醒」——``kick`` 用 ``append_user=False``，
    激励文只走当轮 user_prompt，会话块里没有任何唤醒痕迹。恢复轮的激励文是
    唯一能传达这件事的通道，缺它机械兜底就成了唯一防线。
    """
    wid, p = await _mk_auto(work, goal="父目标：等两张子卡整合")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡跑完再整合", await_children=True)
    assert _state(wid, p) == "paused" and _await_flag(wid, p) == 1

    # 子卡完工 ⇒ after_turn ⇒ 恢复 + 带唤醒事实的激励文
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)

    assert _state(wid, p) == "active"
    # 建卡即起跑（card_store.create_card 尾部会 kick 子卡）⇒ 按卡过滤，只看**父卡**
    parent_kicks = [content for cid, content in kick_capture if cid == p]
    assert len(parent_kicks) == 1, f"父卡应恰好点火一次，实际 {len(parent_kicks)}"
    content = parent_kicks[0]
    # 子卡自己被建卡钩子 kick 的那一轮是常规轮，**不得**带唤醒事实
    for cid, content_other in kick_capture:
        if cid != p:
            assert "你已被唤醒" not in content_other
    # 明确事实三要素（任务书原文措辞的关键片段）
    assert "你已被唤醒" in content, content
    assert "子卡已全部完工" in content or "已全部完工" in content, content
    assert "await_children=true" in content and "不要" in content, content
    assert "read_card" in content, content
    # 一次性标记已消费，不得残留（下一轮不许再带）
    assert auto_driver._PENDING_RESUME_NOTE is None


async def test_a2_normal_rounds_byte_identical_and_note_consumed_once(
        work, kick_capture):
    """验收 a 的边界：**常规续轮逐字不变** ＋ 唤醒事实只带一次（consume-once）。

    ① ``goal_prompt`` 缺省 ``resume_note=""`` ⇒ 输出与改造前**逐字节相同**；
    ② 恢复轮消费掉标记后，紧接着的常规 kick 不再带唤醒事实。
    """
    meta = {"goal_rounds": 7}
    base = auto_driver.goal_prompt(meta, "目标X")
    expected = (
        "（自动模式 · 继续推进）你的目标：目标X\n"
        "当前目标状态：active（已跑 7 轮）。\n"
        "继续做下去。**完成时你必须显式调用 update_goal(complete) 声明完成**——"
        "不声明，系统会自动再起一轮。无法继续时调用 update_goal(blocked, 原因)；"
        "需要停下时调用 update_goal(pause)。"
    )
    assert base == expected, "常规激励文必须逐字节不变（防激励文漂移伤所有轮）"
    assert "唤醒" not in base

    wid, p = await _mk_auto(work, goal="父目标")
    n0 = len(kick_capture)   # 建卡即起跑（create_card 尾部钩子）已记了若干条
    # 卡 active 且无子卡 ⇒ 直接 kick 属常规续轮：此刻无标记 ⇒ 不带唤醒事实
    # （kick 是**同步**函数——返回 task 或 None，不 await）
    auto_driver.kick(wid, p)
    assert len(kick_capture) == n0 + 1 and kick_capture[-1][0] == p
    assert "你已被唤醒" not in kick_capture[-1][1]

    # 人为置一次标记 ⇒ 下一轮带上 ⇒ 再一轮又没了（consume-once，绝不跨轮残留）
    auto_driver._set_resume_note(auto_driver._AWAKE_FROM_AWAIT_NOTE)
    auto_driver.kick(wid, p)
    assert len(kick_capture) == n0 + 2 and "你已被唤醒" in kick_capture[-1][1]
    assert auto_driver._PENDING_RESUME_NOTE is None
    auto_driver.kick(wid, p)
    assert len(kick_capture) == n0 + 3
    assert "你已被唤醒" not in kick_capture[-1][1], "标记必须一次性消费，不得泄漏到后续轮"


async def test_a3_kick_signature_unchanged():
    """验收 a 的接口侧：``kick`` 对外签名逐字不变（``work_id, card_id``）。

    唤醒事实走模块内一次性标记而非形参——否则所有调用点（routes / kick_if_ready）
    都得改签名、漏传即静默退化。这里钉住签名本身。
    """
    sig = inspect.signature(auto_driver.kick)
    assert list(sig.parameters) == ["work_id", "card_id"], sig


# ============================================================
# b. ★ 复刻 work_017：恢复 → 再次 pause(await_children=true) ⇒ 第 3 次起不再恢复
# ============================================================

async def test_b_work017_third_phantom_hang_stops_being_woken(work, no_kick):
    """验收 b（**核心**）：空等重挂第 1、2 次仍恢复（计数未到界）；第 3 次起不再
    恢复、卡保持 paused、有 ops 留痕 ``auto_await_resume_giveup``。

    复刻形态：两张子卡**早已全部 complete**（等待条件已兑现），父卡却反复
    ``pause(await_children=true)`` —— work_017 的「唤醒-挂起」打转。
    """
    wid, p = await _mk_auto(work, goal="父目标：整合两张子卡后收口")
    c1 = await _mk(wid, "子卡甲", p, goal="子目标甲")
    c2 = await _mk(wid, "子卡乙", p, goal="子目标乙")
    for cid in (c1, c2):
        await card_store.update_card(wid, cid, {"goal_state": "complete"}, "子卡完成")
    assert not auto_driver.has_active_child(wid, p), "前置：work_017 形态＝子卡已全完工"
    assert _giveup_rows(work) == [], "前置：尚无兜底留痕"

    # —— 第 1 次空等重挂：声明时子卡已全完工 ⇒ 空等 +1 ⇒ 照常恢复 ——
    await _goal_tool(wid, p, "pause", "等子卡跑完再整合", await_children=True)
    assert _wakes(wid, p) == 1, "第 1 次空等重挂 ⇒ 计数 1"
    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "active" and _rounds(wid, p) == 1
    assert (wid, p) in no_kick and _giveup_rows(work) == []
    # 恢复轮置位了唤醒事实标记（no_kick 把 kick 整个 mock 掉 ⇒ 无人消费 ⇒ 残留非 None；
    # 生产路径里真 kick 一进来就 consume-once 取走，见 test_a2）
    assert auto_driver._PENDING_RESUME_NOTE is not None, \
        "恢复轮应置位唤醒事实标记（mock 场景下未被消费而残留）"

    # —— 第 2 次空等重挂：仍可恢复（计数未到界） ——
    await _goal_tool(wid, p, "pause", "等子卡跑完再整合", await_children=True)
    assert _wakes(wid, p) == 2
    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "active" and _rounds(wid, p) == 2
    assert (wid, p) in no_kick and _giveup_rows(work) == []

    kicks_before = len(no_kick)

    # —— 第 3 次空等重挂：死锁兜底命中 ⇒ 不再恢复，保持 paused ——
    # 先清掉上两轮 mock 残留的标记，才能干净地断言「兜底命中 ⇒ 不置位」
    auto_driver._PENDING_RESUME_NOTE = None
    await _goal_tool(wid, p, "pause", "等子卡跑完再整合", await_children=True)
    assert _wakes(wid, p) == 3, "第 3 次空等重挂 ⇒ 计数到界"
    await auto_driver.after_turn(wid, p)
    assert auto_driver._PENDING_RESUME_NOTE is None, \
        "★ 兜底命中 ⇒ 不得置恢复轮标记（wake_verdict 在置位之前就短路了）"

    assert _state(wid, p) == "paused", "★ 第 3 次起不再恢复（保持 paused，等人处置）"
    assert _rounds(wid, p) == 2, "★ 轮次不得再涨（打转被截断）"
    assert len(no_kick) == kicks_before, "★ 不得点火"
    rows = _giveup_rows(work)
    assert len(rows) == 1, f"应有且恰有一条兜底留痕，实际 {len(rows)}"
    assert rows[0].get("card") == p
    detail = rows[0].get("detail") or {}
    assert detail.get("goal_await_wakes") == 3
    assert detail.get("threshold") == auto_driver._AWAIT_RESUME_DEADLOCK_GIVEUP
    assert detail.get("action") == "kept_paused"

    # 兜底命中后再驱几次：状态纹丝不动，不会又静默复活
    for _ in range(3):
        await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "paused"
    assert _rounds(wid, p) == 2


async def test_b2_threshold_is_three_and_named_as_backstop():
    """验收 b 的常量面：阈值 3、名字带「死锁兜底」语义（不是闸）。"""
    assert auto_driver._AWAIT_RESUME_DEADLOCK_GIVEUP == 3
    # 常量上方注释块必须写明「不是闸」（口径落在注释位置——验收交付项）
    src = inspect.getsource(auto_driver)
    i = src.find("_AWAIT_RESUME_DEADLOCK_GIVEUP = 3")
    assert i > 0
    head = src[max(0, i - 3600):i]
    assert "不是闸" in head, "常量注释必须写明它不是闸"
    assert "它为什么不是闸" in head, "常量注释必须逐条说清为什么不是闸"


# ============================================================
# c. 正常路径不受影响：真等待清零计数；子卡在跑时计数不参与判定
# ============================================================

async def test_c_real_wait_resets_counter_and_normal_loop_unblocked(work, no_kick):
    """验收 c：显式声明等子卡**且确有未终止子卡** ⇒ 计数清零（真等待）；
    子卡在跑时计数再大也不参与任何判定（父卡让位先行、无兜底留痕）；
    子卡完工后照常恢复起轮。正常的「等→醒→干活→再等」循环永不误伤。"""
    wid, p = await _mk_auto(work, goal="父目标：等子卡回报")
    c1 = await _mk(wid, "子卡甲", p, goal="子目标甲")
    c2 = await _mk(wid, "子卡乙", p, goal="子目标乙")

    # 人为预置计数 2（模拟历史上已重挂过两次）——真等待必须把连击清零
    await card_store.update_card(wid, p, {"goal_await_wakes": 2}, "预置历史计数")
    await _goal_tool(wid, p, "pause", "等子卡跑完再整合", await_children=True)
    assert _wakes(wid, p) == 0, "★ 真等待（确有未终止子卡）⇒ 连击清零，计数攒不起来"
    assert _await_flag(wid, p) == 1 and _state(wid, p) == "paused"

    # 人为把计数顶到界上（模拟更极端的历史）——只要子卡还在跑，判定就不该看它
    await card_store.update_card(wid, p, {"goal_await_wakes": 3}, "顶到界（模拟）")
    await card_store.update_card(wid, c1, {"goal_state": "complete"}, "子卡甲完成")
    await auto_driver.after_turn(wid, c1)
    assert _state(wid, p) == "paused", "子卡乙还在跑 ⇒ 父卡让位（与计数无关）"
    assert _giveup_rows(work) == [], "计数不参与「子卡在跑」期间的任何判定（无留痕）"
    assert (wid, p) not in no_kick

    # 模型在子卡乙还在跑时再声明一次「等子卡」——仍是真等待 ⇒ 计数再次清零
    await _goal_tool(wid, p, "pause", "乙还没完，继续等", await_children=True)
    assert _wakes(wid, p) == 0, "真等待 ⇒ 连击再次断裂"

    # 子卡乙完工 ⇒ 正常恢复起轮（计数 0 < 3，一切照旧）
    await card_store.update_card(wid, c2, {"goal_state": "complete"}, "子卡乙完成")
    await auto_driver.after_turn(wid, c2)
    assert _state(wid, p) == "active", "★ 正常等待必须照常被恢复"
    assert _rounds(wid, p) == 1
    assert (wid, p) in no_kick
    assert _giveup_rows(work) == [], "整条正常循环零兜底留痕"


async def test_c2_new_goal_lifecycle_resets_counter(work):
    """验收 c 的生命周期侧：新目标＝新生命周期 ⇒ 空等重挂计数归零（不连坐）。"""
    wid, p = await _mk_auto(work)          # 先不给 goal
    await card_store.update_card(
        wid, p, {"goal_state": "blocked", "goal_await_wakes": 7}, "上一段生命")
    assert _wakes(wid, p) == 7

    r = await auto_driver.ensure_goal_from_message(wid, p, "全新目标")
    assert r is True
    meta = card_store._meta_of(wid, p) or {}
    assert meta.get("goal") == "全新目标"
    assert meta.get("goal_state") == "active"
    assert _wakes(wid, p) == 0, "★ 换目标 ⇒ 旧生命周期的死锁证据不连坐新目标"

    # 已有目标时再发消息是中途引导 ⇒ 不重置（目标不变，计数也不动）
    await card_store.update_card(wid, p, {"goal_await_wakes": 5}, "推进中累计")
    r = await auto_driver.ensure_goal_from_message(wid, p, "中途引导不算新目标")
    assert r is False
    assert _wakes(wid, p) == 5


# ============================================================
# d. 用户按停 ⇒ 仍不复活（红线回归钉子）
# ============================================================

async def test_d_user_stop_still_wins(work, no_kick):
    """验收 d：``control.stop_auto`` 清位红线未被本次改动触碰——按停后子卡完工
    **绝不复活**；且「死锁兜底」不参与按停语义（按停不被计数复活）。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡跑完", await_children=True)
    assert _await_flag(wid, p) == 1 and _wakes(wid, p) == 0   # 真等待清零

    await control.stop_auto(wid, p)
    assert _await_flag(wid, p) == 0, "★ 用户按停必须清等待意图（按停永远优先）"
    assert _state(wid, p) == "paused"

    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)
    assert _state(wid, p) == "paused", "★ 按停的卡不得被复活"
    assert _rounds(wid, p) == 0
    assert (wid, p) not in no_kick
    assert _giveup_rows(work) == [], "按停语义走的是清位红线，不是死锁兜底（零留痕）"


async def test_d2_stop_then_redeclare_is_a_new_explicit_wait(work, no_kick):
    """验收 d 的边界：按停后**模型重新显式声明**等子卡 ⇒ 又成为可唤醒等待——
    这不是「复活」（复活＝系统无视按停），而是新的显式意图；但它此刻若已无
    未终止子卡，同样要吃空等计数（机制对所有声明一视同仁）。"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡", await_children=True)
    await control.stop_auto(wid, p)
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")

    # 按停后模型又显式声明等子卡（新意图）——此时子卡已全完工 ⇒ 空等计数 +1
    await _goal_tool(wid, p, "pause", "又想等子卡", await_children=True)
    assert _wakes(wid, p) == 1
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_PAUSED, \
        "新显式声明 ⇒ 新的可唤醒等待（1 < 3，允许恢复）"
    await auto_driver.after_turn(wid, p)
    assert _state(wid, p) == "active", "新声明是合法出口（≠ 复活旧等待）"


# ============================================================
# e. 非 auto 卡行为不变
# ============================================================

async def test_e_non_auto_card_untouched(work, no_kick):
    """验收 e：非 auto 卡 ⇒ update_goal 仍 denied、驱动器零动作、
    人为置上的标志位/计数字字不落盘。"""
    wid, root = work["work_id"], work["work_card_id"]
    p = await _mk(wid, "普通父卡", root)          # 全程不开自动
    c = await _mk(wid, "普通子卡", p)
    await card_store.update_card(
        wid, p, {"goal_await_children": 1, "goal_await_wakes": 9}, "人为置位")

    await auto_driver.after_turn(wid, c)
    assert auto_driver.wake_verdict(wid, p) == auto_driver.WAKE_NONE
    assert _state(wid, p) == ""
    assert _giveup_rows(work) == [], "非 auto 卡不产生兜底留痕"

    for extra in ({}, {"await_children": True}):
        res = await _goal_tool(wid, p, "pause", "不该生效", **extra)
        assert res["ok"] is False and res["error"]["code"] == "denied", res
    assert _wakes(wid, p) == 9 and _await_flag(wid, p) == 1, \
        "denied ⇒ 一字不落盘（人为值保持）"
    assert (wid, p) not in no_kick


async def test_e2_new_column_not_in_user_patch_surface():
    """验收 e 的接口侧：新列是**内部计数**，不进用户 PATCH 面（同 goal_await_children）。"""
    from src.api import routes
    assert "goal_await_wakes" not in routes.ALLOWED_UPDATE_KEYS
    assert "goal_await_children" not in routes.ALLOWED_UPDATE_KEYS


# ============================================================
# f. 存储面 ＋ 说明书面
# ============================================================

async def test_f_schema_new_column_all_three_places(work):
    """新列三处登记：新库建表含列；create_card 显式落 0；读侧 meta 含键。"""
    wid = work["work_id"]
    cols = await card_db.run_read(
        wid, lambda c: {r["name"] for r in c.execute("PRAGMA table_info(cards)")}, set())
    assert "goal_await_wakes" in cols, "新库建表必须含 goal_await_wakes"

    # create_card 显式落 0（insert_card 对未提供的键写显式 NULL ⇒ 必须显式落值）
    assert _wakes(wid, work["work_card_id"]) == 0
    a = await _mk(wid, "新卡", work["work_card_id"])
    assert _wakes(wid, a) == 0, "新卡必须显式落 0（不靠 DEFAULT）"
    assert "goal_await_wakes" in (card_store._meta_of(wid, a) or {}), \
        "显式落值 ⇒ 读侧 meta 必含键（NULL 会被 row_to_meta 丢掉）"


async def test_f2_old_lib_migration_adds_column(work):
    """存量库补列：缺列的旧库首次连接时自动补，且既有行取默认 0（幂等）。

    手法沿 resume 套件 test_g2（拆列 → 重连 ⇒ 缺列即补）。
    """
    import sqlite3
    wid = work["work_id"]

    # 造一张卡并给它一个非零计数
    p = await _mk(wid, "老卡", work["work_card_id"])
    await card_store.update_card(wid, p, {"goal_await_wakes": 2}, "累计")
    assert _wakes(wid, p) == 2

    # 把列拆掉，模拟「按旧 schema 建好的存量库」
    db = card_db.db_path(wid)
    card_db.close_work(wid)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE cards DROP COLUMN goal_await_wakes")
    conn.commit()
    conn.close()

    # 首次连接 ⇒ 缺列即补 ⇒ 列回来了；既有行该列＝默认 0（不碰既有行的原则）
    cols = await card_db.run_read(
        wid, lambda c: {r["name"] for r in c.execute("PRAGMA table_info(cards)")}, set())
    assert "goal_await_wakes" in cols, "存量库首次连接必须补列"
    assert _wakes(wid, p) == 0, "补列后既有行取 DEFAULT 0"

    # 幂等：再连一遍不报错、列还在
    cols2 = await card_db.run_read(
        wid, lambda c: {r["name"] for r in c.execute("PRAGMA table_info(cards)")}, set())
    assert "goal_await_wakes" in cols2


async def test_f3_prompt_surface_auto_t8(work):
    """说明书面：版本 auto-t8；_SEC_AUTO 有唤醒语义准则；await_children 参数说明
    补了「只在当下确有未完工子卡时传」；普通版不含任何唤醒语义。"""
    from src.api import prompts

    assert prompts.AUTO_BASE_PROMPT_VERSION == "auto-t13"
    # 普通版版本与内容都不得被本次改动波及（本分支基线＝t23-2，一字未动）
    assert prompts.BASE_PROMPT_VERSION == "t26-5", "普通版不得被本次改动波及"
    # 唤醒语义准则（_SEC_AUTO 新增那条）
    assert "你被系统唤醒，说明你等的子卡已经全部完工" in prompts.AUTO_BASE_PROMPT
    assert "再用 `await_children=true` 挂起" in prompts.AUTO_BASE_PROMPT
    assert "那只会让你空转" in prompts.AUTO_BASE_PROMPT
    # update_goal 参数说明补句
    assert "只在「当下确实还有未完工的子卡」时才传 true" in prompts.AUTO_BASE_PROMPT
    # 普通版：不出现新准则与新措辞（逐字不变的红线）
    assert "你被系统唤醒" not in prompts.BASE_PROMPT
    assert "await_children" not in prompts.BASE_PROMPT
    # auto 版的 update_goal 在册行仍含 await_children（工具清单在册检查）
    assert "update_goal(action, reason?, await_children?)" in prompts.AUTO_BASE_PROMPT


async def test_f4_complete_gate_and_await_flag_semantics_untouched(work, no_kick):
    """不变量钉子：收口闸（complete 拒绝未回应询问）与 flag 清零语义未被本次改动波及；
    complete/blocked 永不写计数。（no_kick：拦掉建卡钩子的真派生，本例只验状态机。）"""
    wid, p = await _mk_auto(work, goal="父目标")
    c = await _mk(wid, "子卡", p, goal="子目标")
    await _goal_tool(wid, p, "pause", "等子卡", await_children=True)
    await card_store.update_card(wid, c, {"goal_state": "complete"}, "子卡完成")
    await auto_driver.after_turn(wid, c)
    assert _state(wid, p) == "active" and _rounds(wid, p) == 1

    # complete：无未回应询问 ⇒ 正常收口；计数保持（complete 分支不写它）
    r = await _goal_tool(wid, p, "complete", "做完了")
    assert r["ok"] is True, r
    assert _state(wid, p) == "complete"
    assert _wakes(wid, p) == 0, "complete 分支不写计数列"
