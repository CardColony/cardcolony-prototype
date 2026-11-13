# -*- coding: utf-8 -*-
"""委办线（2026-11-14）：组织线的第二种——委办上级（created_by）。

组织线分化成两条：parent＝结构上级（定位置/树形/传递管辖），
created_by＝委办上级（谁派的活——平的、不传递，只管亲手建的那批）。
给的权就是组织权三件套：可删（管辖）、收汇报（指挥链上行）、可
direct（命令权下行）。

钉住的用户裁定：
- 职能卡能往树内任意卡下建子卡，建卡即记 ``created_by=职能``；
- 职能对它建的卡有**同步管理权**：可删（哪怕挂在别人树下）、
  它的 ``report_up`` 抄送职能、职能对它天然有通信线（可 direct）；
- 职能**删不掉树内不是它建的卡**（policy.can ⑤ 的全树可删被收窄到委办线内）；
- 树内卡的删域不变（子树照旧）；
- 存量卡由 ``work_events.card_created.data.by`` 回填列与创建线。
"""
from __future__ import annotations

import json

import pytest

from src.api import card_tools, message_runner
from src.permissions import policy
from src.storage import (card_db, card_store, session_facade, work_store)


async def _mk_work():
    wdata = await work_store.create_work("委办线测试")
    return str(wdata["work_id"])


def _cards(wid: str):
    return card_store.iter_card_metas(wid)


async def _mk_fn(wid: str, fid: str = "fn_001") -> str:
    """职能卡走正路：ensure_function_card（系统派生、离树、无创建者）。"""
    return await card_tools.ensure_function_card(wid, fid, "测试职能")


async def _mk_sub(wid: str, parent: str, title: str = "子卡",
                  created_by: str = "") -> str:
    r = await card_store.create_card(
        wid, title, "用户直接创建", card_type="sub_project",
        parent=parent, created_by=created_by)
    assert "error" not in r, r
    return str(r["meta"]["card_id"])


def _links_of(wid: str, cid: str) -> list[dict]:
    meta = _cards(wid).get(cid) or {}
    return [l for l in (meta.get("links") or []) if isinstance(l, dict)]


# ---------------------------------------------------------------- 建卡记权属
async def test_fn_creates_card_records_creator_and_link(sandbox):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    host = await _mk_sub(wid, root, "宿主卡")

    out = await card_tools._h_create_card(
        wid, fn, {"title": "职能建的卡", "parent": host,
                  "mission": "负责接口契约的核对与维护"}, _cards(wid))
    assert "error" not in out, out
    cid = str(out["card_id"])

    meta = _cards(wid).get(cid) or {}
    assert meta.get("created_by") == fn            # 委办线落列
    assert meta.get("parent") == host              # 结构线不变：挂在宿主下
    # 创建者默认通信线 ⇒ 职能 direct 它建的卡不需要另拉线
    assert policy.check_linked(fn, cid, _cards(wid)) is True
    # 持久化核验：重读库列（不经 meta 缓存）
    row = await card_db.run_read(
        wid, lambda c: c.execute(
            "SELECT created_by FROM cards WHERE card_id=?",
            (cid,)).fetchone(), None)
    assert row is not None and row["created_by"] == fn


async def test_fn_create_requires_explicit_tree_parent(sandbox):
    """委办上级≠结构上级：职能不给 parent 不许挂自己下面（防脱树小枝）。"""
    wid = await _mk_work()
    fn = await _mk_fn(wid)
    out = await card_tools._h_create_card(
        wid, fn, {"title": "没给宿主的卡",
                  "mission": "负责接口契约的核对与维护"}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "validation"


async def test_offtree_card_cannot_be_parent(sandbox):
    """显式点名离树卡当宿主同样拒——工作卡只挂树内卡下。"""
    wid = await _mk_work()
    fn = await _mk_fn(wid)
    out = await card_tools._h_create_card(
        wid, fn, {"title": "挂职能下的卡", "parent": fn,
                  "mission": "负责接口契约的核对与维护"}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "validation"


# ---------------------------------------------------------------- 删域收窄
async def test_fn_deletes_own_created_card(sandbox):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    host = await _mk_sub(wid, root)
    cid = await _mk_sub(wid, host, "职能建的卡", created_by=fn)

    out = await card_tools._h_delete_card(
        wid, fn, {"card_id": cid}, _cards(wid))
    assert "error" not in out, out
    assert not card_store.card_exists(wid, cid)


async def test_fn_cannot_delete_card_created_by_other(sandbox):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    other = await _mk_sub(wid, root, "别人的卡")
    cid = await _mk_sub(wid, other, "别人建的卡", created_by=other)

    out = await card_tools._h_delete_card(
        wid, fn, {"card_id": cid}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "denied"
    assert card_store.card_exists(wid, cid)      # 没删掉


async def test_fn_cannot_delete_uncreated_card(sandbox):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    cid = await _mk_sub(wid, root)               # created_by 空＝无权属记录

    out = await card_tools._h_delete_card(
        wid, fn, {"card_id": cid}, _cards(wid))
    assert (out.get("error") or {}).get("code") == "denied"
    assert card_store.card_exists(wid, cid)


async def test_tree_actor_delete_unchanged(sandbox):
    """收窄只管离树卡：树内父卡删自己子树内的卡（含职能建的）照旧。"""
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    host = await _mk_sub(wid, root, "宿主卡")
    cid = await _mk_sub(wid, host, "职能建的卡", created_by=fn)

    out = await card_tools._h_delete_card(
        wid, host, {"card_id": cid}, _cards(wid))
    assert "error" not in out, out              # host 子树管辖不动
    assert not card_store.card_exists(wid, cid)


# ---------------------------------------------------------------- 双向汇报
async def test_report_up_reaches_parent_and_creator(sandbox, monkeypatch):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    host = await _mk_sub(wid, root, "宿主卡")
    cid = await _mk_sub(wid, host, "职能建的卡", created_by=fn)

    woke = []

    async def _fake_deliver(w, target, **kw):
        woke.append(target)
        return True

    monkeypatch.setattr(message_runner, "deliver_now", _fake_deliver)
    out = await card_tools._h_report_up(
        wid, cid, {"content": "接口契约核对完毕，v2 为准"}, _cards(wid))
    assert "error" not in out, out
    assert set(out["recipients"]) == {host, fn}   # 父卡＋创建者双送达
    assert set(woke) == {host, fn}

    host_blocks = await session_facade.read(wid, host) or []
    fn_blocks = await session_facade.read(wid, fn) or []
    assert any("接口契约核对完毕" in str(b.get("content") or "")
               for b in host_blocks), "父卡没收到汇报块"
    assert any("接口契约核对完毕" in str(b.get("content") or "")
               for b in fn_blocks), "创建者没收到抄送块"


async def test_report_up_parent_only_when_creator_is_parent(sandbox,
                                                            monkeypatch):
    """树内父卡自己建的卡：created_by==parent ⇒ 不重复投（单收件人）。"""
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    host = await _mk_sub(wid, root, "宿主卡")
    cid = await _mk_sub(wid, host, "宿主自建的卡", created_by=host)

    woke = []

    async def _fake_deliver(w, target, **kw):
        woke.append(target)
        return True

    monkeypatch.setattr(message_runner, "deliver_now", _fake_deliver)
    out = await card_tools._h_report_up(
        wid, cid, {"content": "完工"}, _cards(wid))
    assert "error" not in out, out
    assert out["recipients"] == [host]
    assert woke == [host]


# ---------------------------------------------------------------- 可 direct
async def test_fn_directs_created_card(sandbox, monkeypatch):
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    host = await _mk_sub(wid, root)
    cid = await _mk_sub(wid, host, "职能建的卡", created_by=fn)

    monkeypatch.setattr(message_runner, "deliver_now",
                        lambda *a, **kw: _fake_true())
    out = await card_tools._h_scene_direct(
        wid, fn, {"card_id": cid, "instruction": "把契约文档更新到 v2",
                  "acceptance": "文档块里能看到 v2 条款"}, _cards(wid))
    assert "error" not in out, out


async def _fake_true():
    return True


# ---------------------------------------------------------------- 存量回填
async def test_backfill_created_by_from_events(sandbox):
    """老卡（created_by 空）按 card_created 事件的 by 回填列＋创建线。"""
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    fn = await _mk_fn(wid)
    host = await _mk_sub(wid, root)
    cid = await _mk_sub(wid, host)               # 无 created_by＝模拟老卡

    # 真实建卡事件可能已落 work_events（seq 从 1 起、PK 冲突 OR IGNORE 会
    # 吃掉种子行）——有则改其 data.by（模拟「历史事件记录了创建者」），
    # 没有则插一条。
    from src.infra import event_hub

    def _seed_and_migrate(conn):
        conn.execute(event_hub._DDL)
        n = conn.execute(
            "UPDATE work_events SET data=? "
            "WHERE card_id=? AND type='card_created'",
            (json.dumps({"by": fn}, ensure_ascii=False), cid)).rowcount
        if not n:
            conn.execute(
                "INSERT INTO work_events"
                " (work_id, card_id, seq, type, run_id, round, ts, data)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (wid, cid, 900, "card_created", "", 0,
                 "2026-11-14T00:00:00",
                 json.dumps({"by": fn}, ensure_ascii=False)))
        card_db._backfill_created_by(conn)

    await card_db.run_write(wid, _seed_and_migrate)

    meta = _cards(wid).get(cid) or {}
    assert meta.get("created_by") == fn
    assert policy.check_linked(fn, cid, _cards(wid)) is True


async def test_backfill_skips_when_creator_gone(sandbox):
    """by 指向已删卡 ⇒ 整行跳过：不落权属列、不造假线（保守方向）。"""
    wid = await _mk_work()
    root = work_store.work_card_id_of(wid)
    host = await _mk_sub(wid, root)
    cid = await _mk_sub(wid, host)

    from src.infra import event_hub

    def _seed_and_migrate(conn):
        conn.execute(event_hub._DDL)
        n = conn.execute(
            "UPDATE work_events SET data=? "
            "WHERE card_id=? AND type='card_created'",
            (json.dumps({"by": "card_999"}, ensure_ascii=False), cid)).rowcount
        if not n:
            conn.execute(
                "INSERT INTO work_events"
                " (work_id, card_id, seq, type, run_id, round, ts, data)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (wid, cid, 900, "card_created", "", 0,
                 "2026-11-14T00:00:00",
                 json.dumps({"by": "card_999"}, ensure_ascii=False)))
        card_db._backfill_created_by(conn)

    await card_db.run_write(wid, _seed_and_migrate)

    meta = _cards(wid).get(cid) or {}
    assert not meta.get("created_by")
    links = _links_of(wid, cid)
    assert not any(l.get("target") == "card_999" for l in links)
