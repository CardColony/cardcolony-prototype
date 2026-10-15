# -*- coding: utf-8 -*-
"""[画布重构 2026-10-15 · 阶段 1.5] 卡片位置存储验收（原 layout.json 存储测试改写）。

★ **文件名是历史遗留**：本文件原为「画布第二步 D1 · layout.json 读写能力」
验收，那套存储（``works/{work_id}/layout.json``）已整体退役。文件名按任务书
「改 2 + 新 1 + 扩展 1」的清单保持原样（便于对账），内容已换成 cards 表
坐标列（``pos_x``/``pos_y``/``pinned``）的验收。

判据：
1. ``load_positions``：库不存在／无坐标 → 返回 ``{}`` 不抛（fail-open，L8）；
2. **稀疏**（L6）：用户没表态过的卡不出现在返回里；
3. **往返等价**：写 → 读 → **断连重连**（模拟刷新页面）再读，三次一致；
4. **pinned 置位/取消**：只发 ``{pinned: false}`` 时坐标原样保留（E6 深合并，
   RD-04 不许回退）；
5. **脏行过滤**：载荷指向已不存在的卡 → 不写、不过滤成新行（方案书风险 2）；
6. **随行删除**：卡删了坐标记录自然消失（不留孤儿）；
7. **迁移脚本幂等**：同一批库跑两次，第二次零改动（结构与值都不变）。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from src.storage import card_db, card_store, canvas_store

ROOT = Path(__file__).resolve().parents[1]


async def _mkcard(wid: str, title: str, parent: str) -> str:
    r = await card_store.create_card(wid, title, "用户直接创建", parent=parent)
    assert "error" not in r, r
    return r["meta"]["card_id"]


async def _seed(wid: str, root: str, n: int = 3) -> list[str]:
    return [await _mkcard(wid, f"位置测试卡{i}", root) for i in range(n)]


# ---------------------------------------------------------------- 1：fail-open


async def test_load_positions_missing_db_returns_empty(sandbox):
    """库都不存在 → {}，不抛（不变量 L8：任何缺失都不许阻断画布打开）。"""
    assert await canvas_store.load_positions("work_pos_missing") == {}


async def test_load_positions_no_cards_returns_empty(sandbox, work):
    """库在、但没卡 → {}。"""
    assert await canvas_store.load_positions(work["work_id"]) == {}


# ---------------------------------------------------------------- 2：稀疏（L6）


async def test_untouched_cards_absent_from_positions(sandbox, work):
    """没被用户摆过的卡**不在** positions 里（不建空记录）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cids = await _seed(wid, root, n=2)
    assert await canvas_store.load_positions(wid) == {}, "谁都没碰过就不该有任何坐标"

    await canvas_store.merge_positions(wid, {cids[0]: {"x": 10.0, "y": 20.0, "pinned": True}})
    pos = await canvas_store.load_positions(wid)
    assert set(pos) == {cids[0]}, "只出被摆过的那张卡，其余不占位"
    assert pos[cids[0]] == {"x": 10.0, "y": 20.0, "pinned": True}


# ---------------------------------------------------------------- 3：往返等价


async def test_write_read_reopen_roundtrip(sandbox, work):
    """写 → 读 → **断连重连**（模拟刷新）→ 读，三次一致（证明落盘、非缓存）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = (await _seed(wid, root, n=1))[0]

    await canvas_store.merge_positions(wid, {cid: {"x": 341.091, "y": -14.5, "pinned": True}})
    first = await canvas_store.load_positions(wid)

    card_db.reset_caches()          # 关连接、丢内存态＝刷新页面的等价物
    second = await canvas_store.load_positions(wid)

    assert first == second == {cid: {"x": 341.091, "y": -14.5, "pinned": True}}


# ---------------------------------------------------------------- 4：pinned


async def test_pinned_only_patch_keeps_coordinates(sandbox, work):
    """只发 ``{pinned: false}``（前端「整理画布」的真实载荷）→ 坐标必须原样保留。

    这是 E6 深合并语义的防线：拿「全字段覆盖」去实现会把用户摆好的坐标抹成 NULL。
    """
    wid, root = work["work_id"], work["work_card_id"]
    cid = (await _seed(wid, root, n=1))[0]
    await canvas_store.merge_positions(wid, {cid: {"x": 100.0, "y": 200.0, "pinned": True}})

    await canvas_store.merge_positions(wid, {cid: {"pinned": False}})
    rec = (await canvas_store.load_positions(wid))[cid]
    assert rec["x"] == 100.0 and rec["y"] == 200.0, "深合并必须保留缺失的坐标字段"
    assert rec["pinned"] is False

    # 再钉回去：只带 pinned 不带坐标，坐标仍在
    await canvas_store.merge_positions(wid, {cid: {"pinned": True}})
    rec = (await canvas_store.load_positions(wid))[cid]
    assert (rec["x"], rec["y"], rec["pinned"]) == (100.0, 200.0, True)


async def test_pinned_without_coordinates_is_reported(sandbox, work):
    """「钉住但没坐标」的卡要**出现在** positions 里、坐标给 None。

    前端判的是 ``Number.isFinite(pos.x)``（canvas.js L190）——``None`` 与缺
    字段同样落 false，于是该卡走自动排；同时 ``pinned`` 仍为真，图钉标识与
    ``pinnedIdSet`` 都不丢。这条不许被「顺手过滤成没有这条记录」破坏。
    """
    wid, root = work["work_id"], work["work_card_id"]
    cid = (await _seed(wid, root, n=1))[0]
    await canvas_store.merge_positions(wid, {cid: {"pinned": True}})
    rec = (await canvas_store.load_positions(wid))[cid]
    assert rec == {"x": None, "y": None, "pinned": True}


# ---------------------------------------------------------------- 5：脏行过滤


async def test_position_for_missing_card_is_filtered(sandbox, work):
    """载荷指向**不存在的卡** → 不写脏行、不凭空造卡（方案书风险 2）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = (await _seed(wid, root, n=1))[0]
    await canvas_store.merge_positions(wid, {cid: {"x": 5.0, "y": 6.0, "pinned": True}})

    await canvas_store.merge_positions(wid, {"card_999": {"x": 1.0, "y": 2.0, "pinned": True}})
    pos = await canvas_store.load_positions(wid)
    assert "card_999" not in pos, "不存在的卡不许凭空长出坐标记录"

    def _rows(conn):
        return {r["card_id"] for r in conn.execute("SELECT card_id FROM cards")}

    rows = await card_db.run_read(wid, _rows, set())
    assert "card_999" not in rows, "merge_positions 只 UPDATE，绝不 INSERT 新卡"


async def test_position_follows_card_deletion(sandbox, work):
    """卡删了 → 坐标记录随之消失（坐标在卡身上，不留孤儿）。"""
    wid, root = work["work_id"], work["work_card_id"]
    cid = (await _seed(wid, root, n=1))[0]
    await canvas_store.merge_positions(wid, {cid: {"x": 7.0, "y": 8.0, "pinned": True}})
    assert cid in await canvas_store.load_positions(wid)

    r = await card_store.delete_card(wid, cid)
    assert "error" not in r, r
    assert await canvas_store.load_positions(wid) == {}


# ---------------------------------------------------------------- 6：迁移幂等


def _make_legacy_db(path: Path, rows: int = 2) -> None:
    """造一个「加列之前」的老库：cards 表只有 card_id/title，无任何坐标列。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE cards (card_id TEXT PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO cards (card_id, title) VALUES (?, ?)",
            [(f"card_{i:03d}", f"老卡{i}") for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _load_migrate_script():
    """加载 scripts/migrate_cards_add_position_columns.py（进程内调用，不起子进程）。"""
    spec = importlib.util.spec_from_file_location(
        "migrate_cards_add_position_columns",
        ROOT / "scripts" / "migrate_cards_add_position_columns.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cols(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(cards)")]
    finally:
        conn.close()


def _snapshot(path: Path) -> tuple[list[str], list[tuple]]:
    """列名清单 ＋ 全部行的坐标三元组（幂等判据用）。**要求三列已在**。"""
    conn = sqlite3.connect(str(path))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cards)")]
        rows = list(conn.execute(
            "SELECT card_id, pos_x, pos_y, pinned FROM cards ORDER BY card_id"
        ))
        return cols, rows
    finally:
        conn.close()


def test_migration_script_is_idempotent(sandbox, tmp_path, monkeypatch, capsys):
    """同一批老库跑两次：第二次**零改动**（结构与值都不变）。

    同时钉住任务书的完成标准：加完 13/全部库都含三列、存量行 pos_x/pos_y=NULL
    且 pinned=0。
    """
    works = tmp_path / "legacy_works"
    db = works / "work_001" / "cards.db"
    db.parent.mkdir(parents=True)
    _make_legacy_db(db, rows=2)

    assert {"pos_x", "pos_y", "pinned"} & set(_cols(db)) == set(), "前置：老库确实没这三列"

    mod = _load_migrate_script()
    monkeypatch.setattr(sys, "argv", ["migrate", "--target", str(works), "--apply"])

    assert mod.main() == 0, "第一次应成功加列"
    cols_after, rows_after = _snapshot(db)
    assert {"pos_x", "pos_y", "pinned"} <= set(cols_after), "三列必须齐"
    assert all(x is None and y is None and int(p) == 0 for _c, x, y, p in rows_after), \
        "存量卡坐标必须是 NULL、pinned 必须是 0"

    capsys.readouterr()
    assert mod.main() == 0, "第二次（幂等复跑）同样应成功退出"
    assert _snapshot(db) == (cols_after, rows_after), "幂等复跑必须零副作用"
