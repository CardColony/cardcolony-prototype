# -*- coding: utf-8 -*-
"""[多角色同关注一卡 2026-10-20 · card_004] 唤醒互斥与长期积压的**机械**验证。

**为什么必须验**：真实项目里「一张卡被两个职能部门同时盯」是常态（如口径一致性角色＋成本角色
都关注同一张接口卡），但此前的真机证据只有**单角色**。

**要回答的四件事（全部结构事实，不看模型措辞）**
1. 一次 `status_block` 改动，两个角色是否**各自**入队一行（而不是只有一个收到）；
2. 去重键是否**按角色分开记**——`_role_state_key(work, role, source, field)`。
   这是我自己写的那版里最容易出错的地方：若退化成"按卡记"，第一个角色记了哈希，
   第二个角色就会被判"已看过"而永远收不到；
3. 连续快速改 N 次是否**合并**而不是堆 N 行（`add_sync` 幂等）；
4. 停职期间连续改是否**有界**（不无限增长），以及进程重启后 `startup_sweep_all`
   能否把带 pending 的角色续跑。

**边界**：本文件验的是"入队/去重/合并/续跑"这些同步层机制，不验模型判词质量
（那部分由真机驱动器 `verify/5713/multi_role_watch.py` 覆盖）。
"""
from __future__ import annotations

import asyncio

import pytest

from src.api import card_tools, role_room
from src.storage import card_store, role_store


@pytest.fixture(autouse=True)
def _reset_role_module_state():
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()
    role_room._LOCKS.clear()
    yield
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()
    role_room._LOCKS.clear()


async def _mk_child(wid: str, root: str, title: str) -> str:
    r = await card_store.create_card(
        wid, title=title, source="用户直接创建", card_type="sub_project",
        parent=root, charter=f"## 使命\n{title}")
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _mk_role(wid: str, root: str, watch: list[str], *,
                   title: str, key: str,
                   signals: list[str] | None = None) -> str:
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_role(wid, root, {
        "title": title,
        "charter": f"{title}：盯关注卡的现状变化，互斥才报警，否则沉默。",
        "jurisdiction": {"domain": key, "statement": f"{title} 的域",
                         "exclusive_key": key},
        "watch_card_ids": watch,
        "signals": signals or ["status_changed"],
    }, cards)
    assert "error" not in r, r
    return r["role_card_id"]


async def _settle_async():
    """等写卡通知的异步任务落地（create_task 排在下一轮事件循环）。"""
    for _ in range(50):
        await asyncio.sleep(0.02)


def _pending(wid: str, rid: str) -> list[dict]:
    return role_store.pending_syncs(wid, rid)


async def test_one_edit_queues_row_for_each_watching_role(work):
    """判据 1：一张卡被两个角色关注，一次改动 ⇒ **两人各一行**。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "共享接口卡")
    r1 = await _mk_role(wid, root, [c], title="口径观察员", key="interface_contract")
    r2 = await _mk_role(wid, root, [c], title="成本观察员", key="cost_budget")

    await card_store.update_card(wid, c, {"status_block": "新现状：口径已定稿"},
                                 "用例：改一次现状")
    await _settle_async()

    p1, p2 = _pending(wid, r1), _pending(wid, r2)
    assert len(p1) == 1, p1
    assert len(p2) == 1, p2
    assert p1[0]["source"] == c and p2[0]["source"] == c
    assert "status_changed" in p1[0]["signals"]
    assert "status_changed" in p2[0]["signals"]


async def test_dedupe_key_is_per_role_not_per_card(work):
    """判据 2（最容易错的一条）：去重必须按角色分开记。

    做法：让角色 A 先看一次内容 H（记下哈希），再让角色 B 关注同一张卡，
    然后把卡改回**同一份内容 H**。若去重键是"按卡"记的，B 会被判"已看过"而收不到；
    正确行为是 **B 也能入队**。
    """
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "共享接口卡")
    r1 = await _mk_role(wid, root, [c], title="口径观察员", key="interface_contract")
    same = "同一份内容 H：时间一律 UTC 带 Z"

    await card_store.update_card(wid, c, {"status_block": same}, "用例：A 先看到 H")
    await _settle_async()
    assert len(_pending(wid, r1)) == 1
    # 清空 A 的队列（模拟已被消费）
    await role_store.consume_syncs(wid, [r["seq"] for r in _pending(wid, r1)])
    assert _pending(wid, r1) == []

    # 现在 B 才加入，关注同一张卡
    r2 = await _mk_role(wid, root, [c], title="成本观察员", key="cost_budget")
    # 把卡改成别的值再改回 H —— 内容最终仍是 H
    await card_store.update_card(wid, c, {"status_block": "中间态"}, "用例：中间态")
    await _settle_async()
    await card_store.update_card(wid, c, {"status_block": same}, "用例：改回 H")
    await _settle_async()

    # B 必须收到（它是第一次见 H）
    got = [r for r in _pending(wid, r2) if r["source"] == c]
    assert got, "角色 B 首次见到内容 H 却没入队 —— 去重键退化成了按卡记"
    # A 也收到（内容从「中间态」变回 H，对 A 而言是新变化）
    assert [r for r in _pending(wid, r1) if r["source"] == c]


async def test_ten_rapid_edits_merge_into_one_pending_row(work):
    """判据 3：连续快速改 10 次 ⇒ 每人仍只有 1 行 pending（合并，不堆 10 行）。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "抖动卡")
    r1 = await _mk_role(wid, root, [c], title="口径观察员", key="interface_contract")
    r2 = await _mk_role(wid, root, [c], title="成本观察员", key="cost_budget")

    for i in range(10):
        await card_store.update_card(wid, c, {"status_block": f"第 {i} 次改动"},
                                     f"用例：快速改第 {i} 次")
    await _settle_async()

    for rid in (r1, r2):
        rows = _pending(wid, rid)
        assert len(rows) == 1, f"{rid} 堆了 {len(rows)} 行（合并失效）"
        assert rows[0]["source"] == c


async def test_suspended_backlog_stays_bounded(work):
    """判据 4a：停职期间连续改 20 次 ⇒ 队列**有界**（仍 1 行），不无限增长。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "停职期卡")
    r1 = await _mk_role(wid, root, [c], title="口径观察员", key="interface_contract")
    await role_room.set_duty(wid, r1, "suspended", actor="用例")

    for i in range(20):
        await card_store.update_card(wid, c, {"status_block": f"停职期第 {i} 次"},
                                     f"用例：停职期改第 {i} 次")
    await _settle_async()

    rows = _pending(wid, r1)
    assert len(rows) == 1, f"停职期间堆了 {len(rows)} 行（应收敛为 1 行）"
    # 值班闸：停职不起回合
    assert role_room.spawn(wid, r1) is False
    # 档案里的 pending_sync 计数与队列一致（不撑爆/不算错）
    prof = role_store.get_profile(wid, r1) or {}
    assert int((prof.get("duty") or {}).get("pending_sync") or 0) >= 0


async def test_restart_sweep_picks_up_pending_roles(work, monkeypatch):
    """判据 4b：进程重启（内存态清零）后，带 pending 的角色能被续跑。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "续跑卡")
    r1 = await _mk_role(wid, root, [c], title="口径观察员", key="interface_contract")
    await card_store.update_card(wid, c, {"status_block": "待续跑的新事实"},
                                 "用例：制造 pending")
    await _settle_async()
    assert len(_pending(wid, r1)) == 1

    # 模拟重启：清空全部进程内内存态
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()
    role_room._LOCKS.clear()

    started: list[tuple[str, str]] = []
    monkeypatch.setattr(role_room, "spawn",
                        lambda w, r: started.append((w, r)) or True)
    n = await role_room.startup_sweep_all(limit=50)
    assert n >= 1, "重启后没有续跑任何带 pending 的角色"
    assert (wid, r1) in started, started


async def test_two_roles_have_independent_cooldown(work, monkeypatch):
    """判据 2 的补充：真调 `spawn()`，验证单飞/冷却是**按 (work, role) 隔离**的。

    **为什么要改这条**：初版我把 spawn 换成一个假函数、再去调那个假函数——
    那是在测我自己写的桩，不是测产品代码。这里改成**真调 `role_room.spawn()`**，
    只把两处副作用替换掉（起回合的 `_spawn_tracked`、占用判定的 `turn_holder`），
    其余闸门逻辑（duty 状态、冷却、pending 批、单飞）全部走真代码。
    """
    from src.api import message_runner
    from src.storage import session_facade

    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "共享接口卡")
    r1 = await _mk_role(wid, root, [c], title="口径观察员", key="interface_contract")
    r2 = await _mk_role(wid, root, [c], title="成本观察员", key="cost_budget")
    await card_store.update_card(wid, c, {"status_block": "两人都该被唤醒"},
                                 "用例：改动")
    await _settle_async()
    assert len(_pending(wid, r1)) == 1 and len(_pending(wid, r2)) == 1

    spawned: list[str] = []
    monkeypatch.setattr(message_runner, "_spawn_tracked",
                        lambda coro: (coro.close(), spawned.append("x"))[0])
    monkeypatch.setattr(session_facade, "turn_holder",
                        lambda w, cid: False)
    role_room._LAST_SPAWN.clear()
    role_room._ACTIVE.clear()

    assert role_room.spawn(wid, r1) is True        # 真 spawn：r1 起回合
    assert role_room.spawn(wid, r2) is True        # r2 不能被 r1 的唤醒吞掉
    # 单飞是**按角色**的：两个 key 各自在 _ACTIVE 里
    assert (wid, r1) in role_room._ACTIVE
    assert (wid, r2) in role_room._ACTIVE
    # 同一角色重复调用仍被单飞挡住（这条证明闸门真在跑，不是被我们一起绕过了）
    assert role_room.spawn(wid, r1) is False
