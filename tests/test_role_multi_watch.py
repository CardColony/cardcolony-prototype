# -*- coding: utf-8 -*-
"""[角色机制 5713 · card_017] 多角色同关注一卡的唤醒互斥 ＋ 长期积压与冷却。

补两项未验的机制空白，判据全部机械（读 role_sync 表行数/字段/ID、读
`_role_state_key` 的 meta 哈希、spawn 的 _ACTIVE 键），不探模型自述：

空白一：两个角色（不同 exclusive_key）同时关注同一张工作卡
  1. 一次 status_block 改动 ⇒ 各自入队一行（不是只有一个角色收到）；
  2. `_role_state_key(work, role, source, field)` 按角色分开记哈希——
     第一个角色记了哈希，第二个角色不能被判「已看过」而收不到；
  3. spawn 的 `_ACTIVE` / `_LAST_SPAWN` 是 per-(work,role) 键 ⇒ 各自起回合、
     互不吞掉对方的唤醒；
  4. duty 各自 profile、consume_syncs 按 role_id 隔离 ⇒ 无交错污染。

空白二：长期运行的积压与冷却
  1. 连续快速改同一张卡 status_block 10 次 ⇒ role_sync 合并为 1 行
     （add_sync 幂等：快照口径 from=to=0 靠「已有 pending 即跳过」），
     pending_sync 计数值 = 1；
  2. 停职状态改 20 次 ⇒ 队列不无限增长（仍 1 行）；
  3. startup_sweep_all(limit=N) 有上限 ⇒ 超过 N 个带 pending 的角色会被漏跑
     （进程重启续跑只补前 N 个）。
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
                   signals: list[str], domain: str = "multi_watch") -> str:
    cards = card_tools._collect_card_meta(wid)
    r = await card_tools._h_create_role(wid, root, {
        "title": f"多角色用例-{domain}",
        "charter": "盯关注卡现状变化；无新增风险则沉默。",
        "jurisdiction": {"domain": domain, "statement": f"{domain} 域"},
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


async def _wait_verdicts(wid: str, rid: str, n: int = 1) -> list[dict]:
    for _ in range(300):
        await asyncio.sleep(0.02)
        recs = await role_room.list_verdicts(wid, rid)
        if len(recs) >= n:
            return recs
    return await role_room.list_verdicts(wid, rid)


async def _fake_verdict_then_deliver(calls: dict):
    """第一轮落判词（工具轮），后续轮空手收口（deliver）——回合正常终止。"""
    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": "", "tool_calls": [{
                "name": "record_verdict",
                "arguments": {"decision": "silent", "reason": "用例判词：无新增风险"},
            }]}, "fake")
        return ({"content": "巡检完毕。", "tool_calls": []}, "fake")
    return fake_call_main


# ============================================================
# 空白一：多角色同关注一卡的唤醒互斥
# ============================================================

async def test_two_roles_same_watch_each_gets_own_sync_row(work):
    """一次 status_block 改动 ⇒ 两个角色**各自入队一行**（不是只有一个收到）。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "被盯卡")
    rid_a = await _mk_role(wid, root, [c], signals=["status_changed"], domain="role_a")
    rid_b = await _mk_role(wid, root, [c], signals=["status_changed"], domain="role_b")

    # 不同 exclusive_key 可并存；watch 重叠 ⇒ roles_watching_any 返回两个
    pa = await role_room.get_profile(wid, rid_a)
    pb = await role_room.get_profile(wid, rid_b)
    assert pa["jurisdiction"]["exclusive_key"] != pb["jurisdiction"]["exclusive_key"]
    watchers = await role_room.roles_watching_any(wid, c)
    assert len(watchers) == 2

    await card_store.update_card(wid, c, {"status_block": "口径 V1"}, "用例：改现状")
    rows_a = await _pending(wid, rid_a)
    rows_b = await _pending(wid, rid_b)
    assert len(rows_a) == 1, rows_a
    assert len(rows_b) == 1, rows_b
    assert rows_a[0]["signals"] == ["status_changed"]
    assert rows_b[0]["signals"] == ["status_changed"]


async def test_role_state_key_is_per_role_no_swallow(work):
    """去重键按 (work, role, source, field) 分开记：角色 A 记了哈希，
    角色 B 不会被判「已看过」而收不到——消费后再改，两边都能重新收到。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "被盯卡")
    rid_a = await _mk_role(wid, root, [c], signals=["status_changed"], domain="key_a")
    rid_b = await _mk_role(wid, root, [c], signals=["status_changed"], domain="key_b")

    # 键本身按角色分开
    key_a = role_room._role_state_key(wid, rid_a, c, "status")
    key_b = role_room._role_state_key(wid, rid_b, c, "status")
    assert key_a != key_b

    await card_store.update_card(wid, c, {"status_block": "口径 V1"}, "用例：改现状")
    rows_a = await _pending(wid, rid_a)
    rows_b = await _pending(wid, rid_b)
    # 两边的哈希都写了（不是只写第一个角色）
    assert role_room._get_meta(wid, key_a, "") != ""
    assert role_room._get_meta(wid, key_b, "") != ""
    # 消费掉之后，下一次改动两个角色都能重新入队
    await role_store.consume_syncs(wid, [r["seq"] for r in rows_a])
    await role_store.consume_syncs(wid, [r["seq"] for r in rows_b])
    await card_store.update_card(wid, c, {"status_block": "口径 V2"}, "用例：改现状2")
    rows_a2 = await _pending(wid, rid_a)
    rows_b2 = await _pending(wid, rid_b)
    assert len(rows_a2) == 1, rows_a2
    assert len(rows_b2) == 1, rows_b2


async def test_two_roles_spawn_independently_no_mutual_swallow(work, monkeypatch):
    """spawn 的 _ACTIVE/_LAST_SPAWN 是 per-(work,role)：A 在跑不挡 B，
    两边都落判词、判词 id 各自独立——互不吞掉唤醒。

    挡掉写卡自动通知（它自己就会 spawn，会把 _ACTIVE 先占上，显式 spawn
    会被单飞闸挡掉——测的不是「手动 vs 自动」，是「两个角色并行不互吞」），
    手动入队后各自 spawn 一次。
    """
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "被盯卡")
    rid_a = await _mk_role(wid, root, [c], signals=["status_changed"], domain="sp_a")
    rid_b = await _mk_role(wid, root, [c], signals=["status_changed"], domain="sp_b")

    monkeypatch.setattr(role_room, "schedule_card_changed", lambda *a, **k: None)
    await card_store.update_card(wid, c, {"status_block": "一次改动"}, "用例：改现状")
    await asyncio.sleep(0.1)
    assert role_store.pending_syncs(wid, rid_a) == []
    assert role_store.pending_syncs(wid, rid_b) == []

    await role_store.add_sync(wid, rid_a, c, 0, 0, ["status_changed"])
    await role_store.add_sync(wid, rid_b, c, 0, 0, ["status_changed"])

    calls = {"n": 0}
    monkeypatch.setattr("src.api.model_service.call_main",
                        await _fake_verdict_then_deliver(calls))

    assert role_room.spawn(wid, rid_a) is True
    # spawn 同步设置 _ACTIVE（任务要等下一个 await 才跑），此刻仍在
    assert (wid, rid_a) in role_room._ACTIVE
    assert (wid, rid_b) not in role_room._ACTIVE
    # A 在跑 ⇒ B 的 spawn 不被 A 挡掉（不同 key）
    assert role_room.spawn(wid, rid_b) is True
    assert (wid, rid_b) in role_room._ACTIVE

    va = await _wait_verdicts(wid, rid_a)
    vb = await _wait_verdicts(wid, rid_b)
    assert va and va[0]["decision"] == "silent", va
    assert vb and vb[0]["decision"] == "silent", vb
    assert va[0]["id"] != vb[0]["id"]          # 各自独立判词
    assert va[0]["sources"] and vb[0]["sources"]


async def test_duty_and_sync_isolated_per_role(work):
    """duty 各自 profile、consume_syncs 按 role_id 隔离 ⇒ 无交错污染。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "被盯卡")
    rid_a = await _mk_role(wid, root, [c], signals=["status_changed"], domain="iso_a")
    rid_b = await _mk_role(wid, root, [c], signals=["status_changed"], domain="iso_b")

    # A 停职只改 A 自己的 duty；B 保持 active
    await role_room.set_duty(wid, rid_a, "suspended", actor="用例")
    pa = await role_room.get_profile(wid, rid_a)
    pb = await role_room.get_profile(wid, rid_b)
    assert pa["duty"]["state"] == "suspended"
    assert pb["duty"]["state"] == "active"

    # 停职 A 仍入队，B 也入队（各自一行）
    await card_store.update_card(wid, c, {"status_block": "新事实"}, "用例：改现状")
    rows_a = await _pending(wid, rid_a)
    rows_b = await _pending(wid, rid_b)
    assert len(rows_a) == 1 and len(rows_b) == 1

    # 消费按 role 隔离：只消费 A 的，B 的还在
    await role_store.consume_syncs(wid, [r["seq"] for r in rows_a])
    assert role_store.pending_syncs(wid, rid_a) == []
    assert len(role_store.pending_syncs(wid, rid_b)) == 1


# ============================================================
# 空白二：长期运行的积压与冷却
# ============================================================

async def test_ten_rapid_status_changes_merge_to_one_sync(work):
    """连续快速改同一张卡 status_block 10 次 ⇒ role_sync 合并为 1 行，
    pending_sync 计数值 = 1（快照口径靠「已有 pending 即跳过」）。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "积压卡")
    rid = await _mk_role(wid, root, [c], signals=["status_changed"], domain="backlog")
    for i in range(10):
        await card_store.update_card(wid, c, {"status_block": f"第 {i} 版现状"},
                                     "用例：连续改")
    await asyncio.sleep(0.4)  # 等 10 个异步通知任务全部落地
    rows = role_store.pending_syncs(wid, rid)
    assert len(rows) == 1, rows          # 合并成一行，不堆 10 行
    s = await role_room.role_summary(wid, rid)
    assert s["duty"]["pending_sync"] == 1


async def test_suspended_twenty_changes_queue_stays_bounded(work):
    """停职状态连续改 20 次 ⇒ 队列不无限增长（仍 1 行）；停职挡起回合。"""
    wid, root = work["work_id"], work["work_card_id"]
    c = await _mk_child(wid, root, "停职积压卡")
    rid = await _mk_role(wid, root, [c], signals=["status_changed"], domain="sus_bk")
    await role_room.set_duty(wid, rid, "suspended", actor="用例")
    for i in range(20):
        await card_store.update_card(wid, c, {"status_block": f"停职期第 {i} 版"},
                                     "用例：停职期改")
    await asyncio.sleep(0.5)
    rows = role_store.pending_syncs(wid, rid)
    assert len(rows) == 1, rows          # 有界：不随改动次数线性增长
    # 停职 ⇒ 值班闸直接 False（机械判据，不走到模型）
    assert role_room.spawn(wid, rid) is False


async def test_startup_sweep_all_limit_caps_started(work, monkeypatch):
    """startup_sweep_all(limit=N) 有上限：超过 N 个带 pending 的 active 角色，
    第 N+1 个起不到回合（pending 留在队列等下一次重启/新改动）——进程重启续跑
    存在 limit=20 漏跑窗口，记入观察（不改实现，报主卡裁定）。"""
    wid, root = work["work_id"], work["work_card_id"]
    rids = []
    for i in range(3):
        c = await _mk_child(wid, root, f"续跑卡{i}")
        rid = await _mk_role(wid, root, [c], signals=["status_changed"],
                             domain=f"sweep_{i}")
        await role_store.add_sync(wid, rid, c, 0, 0, ["status_changed"])
        rids.append(rid)
    assert [p["source"] for p in role_room.pending_batch(wid, rids[0])] != []

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(role_room, "spawn",
                        lambda w, r: calls.append((w, r)) or True)
    started = await role_room.startup_sweep_all(limit=2)
    assert started == 2
    assert len(calls) == 2               # 第 3 个角色被 limit 挡住
    assert (wid, rids[2]) not in calls
    # 被挡的角色的 pending 仍在队列（下次重启/新改动才会续跑）
    assert len(role_store.pending_syncs(wid, rids[2])) == 1


def test_role_state_key_format_separates_role_field():
    """键格式：同卡同 field 不同角色 ⇒ 键不同（防「第一个角色记了哈希，
    第二个角色被挡」）；同角色不同 field ⇒ 键不同（status/third 分开记）。"""
    a_s = role_room._role_state_key("w1", "rA", "c1", "status")
    b_s = role_room._role_state_key("w1", "rB", "c1", "status")
    a_t = role_room._role_state_key("w1", "rA", "c1", "third")
    assert a_s != b_s
    assert a_s != a_t
    assert a_s == role_room._role_state_key("w1", "rA", "c1", "status")
