# -*- coding: utf-8 -*-
"""[function 实体化第一步 2026-10-21 · card_028] 职能＝独立号段 fn_NNN + 独立五表。

判据对照（任务书验收判据）：
- 判据①：建职能后 function_profiles 有一行、cards 表**没有**这一行；
- 判据②：canvas 卡片清单 / 卡树遍历结果**不含**职能；
- 判据③：连建三个 fn_001/002/003 不重号；并发建不重号（可复现）；
- 判据⑤：存量 role_* 路径与旧代码路径仍可用（新旧并存、互不干扰）。
（判据④走 verify/5713/c28_function_migration_check.py 对存量 work 拷贝实测；
 判据⑥由整套 pytest 跑分。）
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from src.storage import (
    canvas_store, card_db, card_store, function_store, role_store, work_store,
)


def _tables_of(work_id: str) -> set[str]:
    def _fn(conn: sqlite3.Connection) -> set[str]:
        return {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    return card_db.read_sync(work_id, _fn, set()) or set()


def _cards_rows(work_id: str) -> int:
    def _fn(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])

    return int(card_db.read_sync(work_id, _fn, 0) or 0)


def _tree_ids(work_id: str) -> set[str]:
    """从根卡沿 parent 关系遍历整棵卡树（卡世界的遍历口径）。"""
    metas = card_store.iter_card_metas(work_id)
    children: dict[str, list[str]] = {}
    for cid, m in metas.items():
        children.setdefault(str(m.get("parent") or ""), []).append(cid)
    root = work_store.work_card_id_of(work_id)
    seen: set[str] = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    return seen


# ============================================================
# 判据①：职能在 function_profiles，不在 cards
# ============================================================

async def test_create_function_gets_fn_identity_and_stays_out_of_cards(work):
    wid = work["work_id"]
    prof = await function_store.create_function(wid, "质量管理", {
        "jurisdiction": {"domain": "quality", "statement": "交付质量"},
        "charter": {"version": 1, "text": "对交付质量负责",
                    "ratified_by": "root:card_001"},
    })
    fid = prof["function_id"]
    assert fid == "fn_001"
    assert prof["function_name"] == "质量管理"
    assert prof["charter"]["text"] == "对交付质量负责"
    assert prof["duty"]["state"] == "active"

    def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
        n_prof = int(conn.execute(
            "SELECT COUNT(*) FROM function_profiles WHERE work_id=?",
            (wid,)).fetchone()[0])
        n_cards = int(conn.execute(
            "SELECT COUNT(*) FROM cards WHERE card_id=?", (fid,)).fetchone()[0])
        return n_prof, n_cards

    n_prof, n_cards = card_db.read_sync(wid, _counts, (0, 0))
    assert n_prof == 1, "function_profiles 应有一行"
    assert n_cards == 0, "cards 表不得有职能行（不进卡片世界）"

    # 卡世界的一切寻址口径都看不到它
    assert card_store._meta_of(wid, fid) is None
    with pytest.raises(card_store.CardNotFoundError):
        await card_store.read_card(wid, fid)
    # 不派生影子、不建会话文件
    n_shadow = card_db.read_sync(wid, lambda c: int(
        c.execute("SELECT COUNT(*) FROM shadows WHERE owner_card_id=?",
                  (fid,)).fetchone()[0]), -1)
    assert n_shadow == 0
    sessions = Path(card_store.WORKS_DIR) / wid / "sessions"
    assert not list(sessions.glob("fn_*")), "职能不得有会话文件"
    # 五张表都建出来了
    tables = _tables_of(wid)
    assert {"function_profiles", "function_verdicts", "function_alarms",
            "function_sync", "function_charter_history"} <= tables


# ============================================================
# 判据②：canvas 卡片清单 / 卡树遍历不含职能
# ============================================================

async def test_function_absent_from_canvas_and_card_tree(work):
    wid, root = work["work_id"], work["work_card_id"]
    # 先放一张普通卡进卡世界，证明画布/卡树机制本身在工作
    child = await card_store.create_card(
        wid, title="子卡", source="用户直接创建", parent=root,
        charter="## 使命\n子")
    assert "error" not in child
    cid = child["meta"]["card_id"]

    prof = await function_store.create_function(wid, "安全审计")
    fid = prof["function_id"]

    data = canvas_store.load(wid)
    canvas_ids = [c["card_id"] for c in (data.get("cards") or [])]
    assert cid in canvas_ids, "画布应含普通卡（机制自证）"
    assert fid not in canvas_ids, "画布不得含职能"
    assert canvas_store.card_projection(wid, fid) is None

    metas = card_store.iter_card_metas(wid)
    assert fid not in metas, "卡片清单不得含职能"
    assert cid in metas, "卡片清单应含普通卡（机制自证）"

    tree = _tree_ids(wid)
    assert cid in tree, "卡树应含普通卡（机制自证）"
    assert fid not in tree, "卡树遍历不得含职能"
    assert fid not in card_store._subtree_ids(wid, root)


# ============================================================
# 判据③：连建不重号 + 并发不重号
# ============================================================

async def test_sequential_ids_fn_001_002_003(work):
    wid = work["work_id"]
    ids = []
    for name in ("甲", "乙", "丙"):
        prof = await function_store.create_function(wid, name)
        ids.append(prof["function_id"])
    assert ids == ["fn_001", "fn_002", "fn_003"]


async def test_concurrent_creates_no_duplicate_ids(work):
    """并发建职能不重号（可复现）：发号与建行同一 BEGIN IMMEDIATE 事务。"""
    wid = work["work_id"]
    n = 12
    results = await asyncio.gather(*[
        function_store.create_function(wid, f"并发职能{i}") for i in range(n)])
    ids = [r["function_id"] for r in results]
    assert len(ids) == n and len(set(ids)) == n, f"并发重号: {ids}"
    assert set(ids) == {f"fn_{i:03d}" for i in range(1, n + 1)}

    # 跨事件循环的真线程并发（run_write 内部 to_thread，锁按库路径串行化）
    def _one(i: int) -> str:
        return asyncio.run(function_store.create_function(wid, f"线程{i}")
                           )["function_id"]

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        tids = list(ex.map(_one, range(4)))
    assert len(set(tids)) == 4, f"线程并发重号: {tids}"
    all_ids = set(ids) | set(tids)
    assert not (all_ids & set()), "占位"
    assert len(all_ids) == n + 4

    def _n(conn: sqlite3.Connection) -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM function_profiles WHERE work_id=?",
            (wid,)).fetchone()[0])

    assert card_db.read_sync(wid, _n, 0) == n + 4


# ============================================================
# 号段独立：fn 号与 card 号互不侵占
# ============================================================

async def test_fn_seq_independent_from_card_seq(work):
    wid, root = work["work_id"], work["work_card_id"]
    assert await function_store.create_function(wid, "甲") is not None
    child = await card_store.create_card(
        wid, title="子", source="用户直接创建", parent=root, charter="## 使命\n子")
    cid = child["meta"]["card_id"]
    assert cid == "card_002", "职能不占卡号：建职能后下一张卡仍是 card_002"
    prof = await function_store.create_function(wid, "乙")
    assert prof["function_id"] == "fn_002", "建卡不占职能号"

    def _meta(conn: sqlite3.Connection) -> tuple[str, str]:
        seq = str(card_db.get_meta_value(conn, "seq", ""))
        fn_seq = str(card_db.get_meta_value(conn, "fn_seq", ""))
        return seq, fn_seq

    seq, fn_seq = card_db.read_sync(wid, _meta, ("", ""))
    assert seq == "2" and fn_seq == "2"


# ============================================================
# 台账 roundtrip（判断簿/警报簿/同步队列/职责书史/值班态）
# ============================================================

async def test_profile_ledgers_roundtrip(work):
    wid = work["work_id"]
    prof = await function_store.create_function(wid, "进度协调", {
        "charter": {"version": 1, "text": "盯全局进度", "ratified_by": "human"},
    })
    fid = prof["function_id"]

    hist = await function_store.charter_history(wid, fid)
    assert [h["version"] for h in hist] == [1]
    assert hist[0]["text"] == "盯全局进度"
    ch2 = await function_store.append_charter(wid, fid, "盯全局进度与风险", "human")
    assert ch2["version"] == 2
    assert [h["version"] for h in
            await function_store.charter_history(wid, fid)] == [2, 1]

    rec = await function_store.add_verdict(
        wid, fid, "speak", "发现进度缺口", ["card_002"], ["card_002"], ["e1"])
    assert rec["decision"] == "speak"
    assert [v["id"] for v in function_store.list_verdicts(wid, fid)] == [rec["id"]]
    await function_store.supersede_verdict(wid, rec["id"], "newid")
    vs = function_store.list_verdicts(wid, fid)
    assert vs[0]["superseded_by"] == "newid"

    alarm = await function_store.add_alarm(
        wid, fid, "warn", "里程碑超期", "详情", question="是否调整？",
        options=["顺延", "加人"], recommendation="加人", default_action="顺延")
    assert alarm["status"] == "open"
    alarm2 = await function_store.add_alarm(wid, fid, "info", "更正", "更正详情",
                                            supersedes=alarm["id"])
    got = function_store.list_alarms(wid, status="", function_id=fid)
    by_id = {a["id"]: a for a in got}
    assert by_id[alarm["id"]]["status"] == "corrected"
    assert by_id[alarm["id"]]["corrected_by"] == alarm2["id"]
    assert await function_store.ack_alarm(wid, alarm2["id"]) is True

    assert await function_store.add_sync(wid, fid, "card_002", 1, 5,
                                         ["turn_completed"]) is True
    assert await function_store.add_sync(wid, fid, "card_002", 1, 4,
                                         ["turn_completed"]) is False  # 幂等跳过
    pend = function_store.pending_syncs(wid, fid)
    assert len(pend) == 1 and pend[0]["to_seq"] == 5
    assert function_store.last_consumed_seq(wid, fid, "card_002") == -1
    await function_store.consume_syncs(wid, [pend[0]["seq"]])
    assert function_store.pending_syncs(wid, fid) == []
    assert function_store.last_consumed_seq(wid, fid, "card_002") == 5

    duty = await function_store.set_duty_state(wid, fid, "suspended", "人令")
    assert duty["state"] == "suspended" and duty["previous"] == "active"
    duty = await function_store.touch_duty(wid, fid, last_wake_at="2026-10-21T21:00:00+08:00")
    assert duty["state"] == "suspended", "touch 不得碰 state"
    assert duty["last_wake_at"] == "2026-10-21T21:00:00+08:00"

    await function_store.update_profile(wid, fid, {"function_name": "进度协调（扩）"})
    prof2 = function_store.get_profile(wid, fid)
    assert prof2["function_name"] == "进度协调（扩）"
    assert prof2["charter"]["version"] == 2
    assert [p["function_id"] for p in function_store.list_profiles(wid)] == [fid]


# ============================================================
# 判据⑤：存量 role_* 路径仍可用，新旧并存互不干扰
# ============================================================

async def test_role_paths_still_work_and_coexist_with_functions(work):
    wid, root = work["work_id"], work["work_card_id"]
    # 旧路径：角色卡仍是 card_NNN + role_profiles（role_room 同一建法）
    role = await card_store.create_card(
        wid, title="旧角色", source="系统派生", card_type="role",
        parent="", charter="## 使命\n旧角色", skip_kick=True)
    assert "error" not in role
    rid = role["meta"]["card_id"]
    assert rid.startswith("card_"), "角色身份本步不变（仍是 card_NNN）"
    prof = await role_store.ensure_profile(wid, rid, {
        "jurisdiction": {"domain": "legacy", "statement": "旧路径"},
        "charter": {"version": 1, "text": "旧职责", "ratified_by": "t"},
    })
    assert prof["jurisdiction"]["domain"] == "legacy"
    assert (await role_store.charter_history(wid, rid))[0]["text"] == "旧职责"
    rv = await role_store.add_verdict(wid, rid, "silent", "无事", [], [], [])
    assert rv["decision"] == "silent"

    # 新路径：职能 fn_NNN + function_profiles
    fprof = await function_store.create_function(wid, "新职能")
    fid = fprof["function_id"]
    assert fid == "fn_001"

    def _counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
        n_role = int(conn.execute(
            "SELECT COUNT(*) FROM role_profiles WHERE work_id=? AND card_id=?",
            (wid, rid)).fetchone()[0])
        n_fn = int(conn.execute(
            "SELECT COUNT(*) FROM function_profiles WHERE work_id=? AND function_id=?",
            (wid, fid)).fetchone()[0])
        n_fn_in_cards = int(conn.execute(
            "SELECT COUNT(*) FROM cards WHERE card_id=?", (fid,)).fetchone()[0])
        return n_role, n_fn, n_fn_in_cards

    n_role, n_fn, n_fn_in_cards = card_db.read_sync(wid, _counts, (0, 0, 0))
    assert (n_role, n_fn, n_fn_in_cards) == (1, 1, 0), \
        "角色行在、职能行在、职能不在 cards——新旧并存且职能不进卡片世界"
    # 旧角色仍在卡世界（本步不改角色）；职能不在
    assert card_store._meta_of(wid, rid) is not None
    assert card_store._meta_of(wid, fid) is None
