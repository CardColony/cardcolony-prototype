# -*- coding: utf-8 -*-
"""[角色同步 2026-10-20 · card_004] 正文块被改 ⇒ 关注角色必须收到同步。

回归的是真机实验（verify/5713/dynamic_cabinet_v2.py，work_010）暴露的缺口：

- 现状块/定论块被 `update_card` 直接改时，角色一行同步也收不到（同步只挂在
  「工作卡回合收口」上）；
- 没有会话块的卡（只有现状/定论的卡）连回合都没有，`manual_check` 里那段
  「last 归 0」的兜底与 `from_seq = max(baseline, consumed)+1 ≥ 1` 相矛盾，
  0 >= 1 恒假 ⇒ 兜底从未生效。

判据全部机械：直接读 `role_sync` 表的行，不看模型自述。
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
        parent=root, charter=f"## 使命\n{title}",
    )
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _mk_role(wid: str, root: str, watch: list[str], *,
                   signals: list[str], domain: str = "sync_case") -> str:
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_role(wid, root, {
        "title": f"同步用例角色-{domain}",
        "charter": "盯关注卡的现状与定论变化；无变化则沉默。",
        "jurisdiction": {"domain": domain, "statement": "同步用例域"},
        "watch_card_ids": watch,
        "signals": signals,
    }, cards)
    assert "error" not in r, r
    return r["role_card_id"]


async def _pending(wid: str, rid: str) -> list[dict]:
    """等异步通知任务落地（create_task 是排在下一次事件循环），再读队。"""
    for _ in range(50):
        rows = role_store.pending_syncs(wid, rid)
        if rows:
            return rows
        await asyncio.sleep(0.02)
    return role_store.pending_syncs(wid, rid)


async def test_status_block_edit_queues_once_per_change(work):
    """无会话块的卡：改现状 ⇒ 入队 1 行；同内容重写 ⇒ 不再入队；再改 ⇒ 再入 1 行。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "现状卡")
    rid = await _mk_role(wid, root, [c], signals=["status_changed"])

    assert role_store.pending_syncs(wid, rid) == []

    await card_store.update_card(wid, c, {"status_block": "口径 A：UTC 带 Z"}, "用例：改现状")
    rows = await _pending(wid, rid)
    assert len(rows) == 1, rows
    assert "status_changed" in rows[0]["signals"]
    # 无会话块 ⇒ 快照口径
    assert rows[0]["from_seq"] == 0 and rows[0]["to_seq"] == 0

    # 同一内容再写一次：内容哈希没变 ⇒ 不再入队（避免重复灌同一份材料）
    await card_store.update_card(wid, c, {"status_block": "口径 A：UTC 带 Z"}, "用例：重写同内容")
    await asyncio.sleep(0.1)
    assert len(role_store.pending_syncs(wid, rid)) == 1

    # 内容真变了、但队里已有一条 pending ⇒ 合并（不重复灌），仍只有 1 行
    await card_store.update_card(wid, c, {"status_block": "口径 B：本地 naive"}, "用例：改现状2")
    await asyncio.sleep(0.1)
    assert len(role_store.pending_syncs(wid, rid)) == 1

    # 消费掉以后，下一次改动要能重新入队（不是永久沉默）
    await role_store.consume_syncs(wid, [r["seq"] for r in role_store.pending_syncs(wid, rid)])
    await card_store.update_card(wid, c, {"status_block": "口径 C：毫秒精度"}, "用例：改现状3")
    for _ in range(50):
        rows = role_store.pending_syncs(wid, rid)
        if rows:
            break
        await asyncio.sleep(0.02)
    assert len(rows) == 1, rows


async def test_rulings_edit_queues_decision_signal_only(work):
    """定论块被改 ⇒ decision_appended 入队；只订了 status_changed 的角色不收。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "定论卡")
    rid_third = await _mk_role(wid, root, [c], signals=["decision_appended"],
                              domain="third_sig")
    rid_status = await _mk_role(wid, root, [c], signals=["status_changed"],
                               domain="status_only")

    await card_store.update_card(wid, c, {"rulings": "D01【裁定】一律带时区。"}, "用例：追加定论")

    rows = await _pending(wid, rid_third)
    assert len(rows) == 1, rows
    assert rows[0]["signals"] == ["decision_appended"], rows

    # 没订 decision_appended 的角色：一行都不进
    await asyncio.sleep(0.1)
    assert [r for r in role_store.pending_syncs(wid, rid_status)
            if r["source"] == c] == []


async def test_suspended_still_queues_but_does_not_spawn(work, monkeypatch):
    """停职期间事实必须进队（不丢）；但值班闸口挡住起回合；复职后要求起回合。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "停职卡")
    rid = await _mk_role(wid, root, [c], signals=["status_changed"])

    await role_room.set_duty(wid, rid, "suspended", actor="用例")
    await card_store.update_card(wid, c, {"status_block": "停职期间的新事实"}, "用例：停职期改现状")
    rows = await _pending(wid, rid)
    assert len(rows) == 1, rows            # 事实进队（停职只挡回合，不挡入队）
    assert rows[0]["signals"] == ["status_changed"], rows

    # 真 spawn：值班态闸口直接 False（不走到模型），这是机械判据不是替身
    assert role_room.spawn(wid, rid) is False

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(role_room, "spawn",
                        lambda w, r: calls.append((w, r)) or True)
    await role_room.set_duty(wid, rid, "active", actor="用例")
    # 复职 ⇒ 要求补巡检。允许出现不止一次（写卡通知任务是异步排队的，可能晚一步
    # 才跑到），spawn 自身有单飞/冷却闸，重复调用是空操作。
    assert (wid, rid) in calls, calls


async def test_manual_check_queues_snapshot_for_session_less_card(work, monkeypatch):
    """回归现象 2：无会话块的卡，manual_check 也要把当前现状进队（旧写法 0>=1 恒假）。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "巡检卡")
    rid = await _mk_role(wid, root, [c], signals=["status_changed"])

    # 先把「写卡即通知」这条自动路挡掉，单独验 manual_check 自己会不会入队
    monkeypatch.setattr(role_room, "schedule_card_changed", lambda *a, **k: None)
    await card_store.update_card(wid, c, {"status_block": "巡检前先改一次现状"}, "用例：改现状")
    await asyncio.sleep(0.1)
    assert role_store.pending_syncs(wid, rid) == []

    res = await role_room.manual_check(wid, rid)
    assert res.get("error") is None, res
    rows = role_store.pending_syncs(wid, rid)
    assert len(rows) == 1, rows
    assert rows[0]["from_seq"] == 0 and rows[0]["to_seq"] == 0
    # 已在队里 ⇒ 再点一次巡检不重复灌
    await role_room.manual_check(wid, rid)
    assert len(role_store.pending_syncs(wid, rid)) == 1

async def test_duty_bookkeeping_never_clobbers_state(work):
    """回归 dynamic_cabinet D 段实测的丢更新：收尾记账只合并字段，
    不得把 set_duty 刚写入的 state 冲回旧值（旧实现整字典写回，
    复职 active 被上轮收尾的 suspended 快照覆盖，spawn 从此静默拒起）。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "值班记账卡")
    rid = await _mk_role(wid, root, [c], signals=["status_changed"])

    await role_room.set_duty(wid, rid, "suspended", actor="用例")
    # 模拟上轮巡检收尾与停职/复职并发：记账必须只动 last_wake_at/pending_sync
    await role_store.touch_duty(wid, rid, last_wake_at="T1", pending_sync=0)
    duty = (role_store.get_profile(wid, rid) or {}).get("duty") or {}
    assert duty.get("state") == "suspended", duty   # state 不被记账冲掉
    assert duty.get("last_wake_at") == "T1", duty

    # 复职后再记账：state 保持 active（丢更新回归点）
    await role_room.set_duty(wid, rid, "active", actor="用例")
    await role_store.touch_duty(wid, rid, last_wake_at="T2", pending_sync=1)
    duty = (role_store.get_profile(wid, rid) or {}).get("duty") or {}
    assert duty.get("state") == "active", duty
    assert duty.get("last_wake_at") == "T2" and duty.get("pending_sync") == 1, duty

    # set_duty_state 原子合并：不丢 touch 写下的记账字段
    await role_room.set_duty(wid, rid, "suspended", actor="用例", note="再停")
    duty = (role_store.get_profile(wid, rid) or {}).get("duty") or {}
    assert duty.get("state") == "suspended" and duty.get("last_wake_at") == "T2", duty