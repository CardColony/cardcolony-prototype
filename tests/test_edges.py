"""三线边表：建卡双线、连线/解线、换父、删卡清边、迁移幂等。

用户裁定（2026-10-27）：
- 「所有的通信必须基于底层，而不是基于卡本身」⇒ 线在表里，不在卡字段里
- 「建卡的同时同时建两条线：父子关系线 + link 通信线」
- 「三张表」（parent / links / relations 各自一张，约束不同）
"""

from __future__ import annotations

import sqlite3

import pytest

from src.storage import card_db, card_store, work_store


async def _edges(wid: str, cid: str) -> dict:
    return await card_db.run_read(
        wid, lambda conn: card_db.edges_of(conn, cid), {}) or {}


async def _make_tree():
    """建一个 work ＋ 根卡 ＋ 一张子卡，返回 (wid, root, child)。"""
    w = await work_store.create_work("边表测试")
    wid = str(w["work_id"])
    root = str(w["work_card_id"])
    r = await card_store.create_card(wid, "子项目·边表", "用户直接创建",
                                     card_type="sub_project")
    return wid, root, str(r["meta"]["card_id"])


# ---------------------------------------------------------------- 建卡双线
async def test_create_card_makes_two_lines(sandbox):
    """建卡同时落两条线：父子关系线 ＋ 默认通信线。"""
    wid, root, cid = await _make_tree()
    e = await _edges(wid, cid)
    assert e.get("parent") == root, f"父子关系线没落：{e}"
    links = e.get("links") or []
    assert any(str(x.get("target")) == root for x in links), (
        f"默认通信线没落：{links}")
    assert any("父子默认通信线" in str(x.get("note") or "") for x in links)


async def test_root_has_no_parent_line(sandbox):
    """根卡没有父 ⇒ 不落父子线，也不落默认通信线。"""
    wid, root, _ = await _make_tree()
    e = await _edges(wid, root)
    assert not e.get("parent"), e
    assert not [x for x in (e.get("links") or [])
                if "父子默认通信线" in str(x.get("note") or "")]


# ---------------------------------------------------------------- 连线
async def test_link_cards_writes_table_and_keeps_default(sandbox):
    """link_cards 写进表，**且不冲掉父子默认线**。"""
    wid, root, cid = await _make_tree()
    r = await card_store.create_card(wid, "另一张", "用户直接创建",
                                     card_type="sub_project")
    other = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid,
        {"links": [{"target": other, "type": "关联", "note": "手工线"}]},
        "建线")
    e = await _edges(wid, cid)
    targets = {str(x.get("target")) for x in (e.get("links") or [])}
    assert other in targets, f"手工线没落表：{e}"
    assert root in targets, "父子默认线被一次 link 冲掉了"


async def test_unlink_removes_only_that_line(sandbox):
    wid, root, cid = await _make_tree()
    r = await card_store.create_card(wid, "第三张", "用户直接创建",
                                     card_type="sub_project")
    other = str(r["meta"]["card_id"])
    await card_store.update_card(
        wid, cid, {"links": [{"target": other, "type": "关联"}]}, "建线")
    # 解掉手工线：整块覆盖成空
    await card_store.update_card(wid, cid, {"links": []}, "解线")
    e = await _edges(wid, cid)
    targets = {str(x.get("target")) for x in (e.get("links") or [])}
    assert other not in targets, "手工线没解掉"
    assert root in targets, "默认线被顺手清掉了"


# ---------------------------------------------------------------- 换父
async def test_reparent_moves_default_line(sandbox):
    """换父：parent 表更新，默认通信线迁到新父，旧父那条删掉。"""
    wid, root, cid = await _make_tree()
    r = await card_store.create_card(wid, "新父", "用户直接创建",
                                     card_type="sub_project")
    newp = str(r["meta"]["card_id"])
    await card_store.update_card(wid, cid, {"parent": newp}, "换父")
    e = await _edges(wid, cid)
    assert e.get("parent") == newp, e
    notes = {str(x.get("target")): str(x.get("note") or "")
             for x in (e.get("links") or [])}
    assert "父子默认通信线" in notes.get(newp, ""), f"新父没默认线：{notes}"
    assert "父子默认通信线" not in notes.get(root, ""), f"旧父默认线没删：{notes}"


# ---------------------------------------------------------------- 删卡
async def test_delete_card_clears_all_edges(sandbox):
    """删卡：三个方向的边一起清（作父、作子、作两端）。"""
    wid, root, cid = await _make_tree()
    await card_store.delete_card(wid, cid, cascade=False)
    def _count(conn):
        return (conn.execute("SELECT COUNT(*) FROM card_parent WHERE card_id=? OR parent_id=?",
                             (cid, cid)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM card_links WHERE from_card=? OR to_card=?",
                             (cid, cid)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM card_relations WHERE from_card=? OR to_card=?",
                             (cid, cid)).fetchone()[0])
    got = await card_db.run_read(wid, _count, (0, 0, 0))
    assert got == (0, 0, 0), f"删卡后仍有残留边：{got}"


# ---------------------------------------------------------------- 反向查询
async def test_reverse_queries(sandbox):
    """谁连了我 / 谁依赖我 —— 建表换来的能力。"""
    wid, root, cid = await _make_tree()
    def _q(conn):
        return (card_db.linkers_of(conn, root),
                card_db.children_of(conn, root))
    linkers, children = await card_db.run_read(wid, _q, ([], []))
    assert cid in linkers, f"谁连了我 查不到：{linkers}"
    assert cid in children, f"直接子卡 查不到：{children}"


# ---------------------------------------------------------------- 迁移幂等
def test_migration_idempotent_on_raw_db(tmp_path):
    """裸 sqlite 上跑两遍迁移，行数不变（存量 work 首次连接即走这条路）。"""
    db = tmp_path / "cards.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE cards (card_id TEXT PRIMARY KEY, parent TEXT, links TEXT, "
        "relations TEXT, created_at TEXT)")
    conn.execute("INSERT INTO cards VALUES('c1','', '[]', '[]', 't')")
    conn.execute("INSERT INTO cards VALUES('c2','c1', ?, ?, 't')", (
        '[{"target":"c1","type":"关联","note":"x"}]',
        '[{"target":"c1","kind":"依赖","detail":"y"}]'))
    conn.commit()

    for ddl in card_db._EDGES_DDL:
        conn.execute(ddl)
    card_db._migrate_edges_from_cards(conn)
    conn.commit()
    first = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("card_parent", "card_links", "card_relations")]
    card_db._migrate_edges_from_cards(conn)
    conn.commit()
    second = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("card_parent", "card_links", "card_relations")]
    assert first == second == [1, 1, 1], (first, second)
    conn.close()


def test_migration_survives_broken_json(tmp_path):
    """卡上 JSON 坏了不许炸迁移（存量数据可能有脏的）。"""
    db = tmp_path / "cards.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE cards (card_id TEXT PRIMARY KEY, parent TEXT, links TEXT, "
        "relations TEXT, created_at TEXT)")
    conn.execute("INSERT INTO cards VALUES('c1','', 'not-json', NULL, 't')")
    conn.commit()
    for ddl in card_db._EDGES_DDL:
        conn.execute(ddl)
    card_db._migrate_edges_from_cards(conn)     # 不许抛
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM card_links").fetchone()[0] == 0
    conn.close()
