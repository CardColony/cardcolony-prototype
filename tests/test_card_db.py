# -*- coding: utf-8 -*-
"""[卡片存储迁移 SQLite · 第一步 1a] 库地基硬判据（任务书 §七 1a）。

四条硬判据逐条落地：
1. 建库建表成功；
2. 并发写不报 ``database is locked``；
3. ``card_db`` 内**无模块级路径常量**（grep 可验——否则 conftest 沙箱失效）；
4. 「有 md 无库」时**明确报错**（防误重启致静默空数据）。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.storage import card_db, card_store, work_store

CARD_DB_SRC = Path(__file__).resolve().parents[1] / "src" / "storage" / "card_db.py"


# ---------------------------------------------------------------- 1：建库建表

async def test_creates_db_and_tables(work):
    wid = work["work_id"]
    db = card_db.db_path(wid)
    assert db.exists() and db.name == "cards.db"

    def _fn(conn):
        return {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }

    tables = await card_db.run_read(wid, _fn, set())
    assert {"cards", "card_history", "shadows", "meta"} <= tables
    # 索引（「好查」落点）
    def _idx(conn):
        return {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}

    idx = await card_db.run_read(wid, _idx, set())
    assert {"idx_cards_parent", "idx_cards_type", "idx_cards_state",
            "idx_cards_work", "idx_shadows_work"} <= idx


# ---------------------------------------------------------------- 2：并发写

async def test_concurrent_writes_no_database_locked(work):
    """并发写同一 work：库层不得报 ``database is locked``。"""
    wid = work["work_id"]

    def _one(i: int):
        def _fn(conn):
            card_db.set_meta_value(conn, f"probe_{i}", str(i))
            card_db.bump_rev(conn)
        return card_db.run_write(wid, _fn)

    await asyncio.gather(*[_one(i) for i in range(24)])
    rev = card_db.read_rev(wid)
    assert isinstance(rev, int) and rev >= 24


# ---------------------------------------------------------------- 3：无模块级路径常量

def test_no_module_level_path_constants():
    """card_db 不得有模块级 Path 常量——否则 conftest 沙箱 patch 不覆盖它，
    测试会写进真实 works/（任务书 §六 红线 3 / 方案 §7.1.5）。"""
    src = CARD_DB_SRC.read_text(encoding="utf-8")
    # 去掉 docstring 与注释后，模块顶层不得出现 `XXX = Path(...)`
    offenders = re.findall(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*Path\(", src, re.M)
    assert offenders == [], f"card_db 出现模块级 Path 常量: {offenders}"
    assert "WORKS_DIR =" not in src
    # 路径一律调用时从 card_store.WORKS_DIR 派生
    assert "card_store.WORKS_DIR" in src


# ---------------------------------------------------------------- 4：有 md 无库

async def test_has_md_without_db_raises(sandbox):
    """有 cards/*.md 但无 cards.db → 明确报错，不静默建空库、不静默返空。"""
    wid = "work_777"
    wd = card_store.WORKS_DIR / wid
    (wd / "cards").mkdir(parents=True, exist_ok=True)
    (wd / "cards" / "card_001.md").write_text("---\ncard_id: card_001\n---\n",
                                              encoding="utf-8")
    # work.json 必须在（否则 create_card 先报「剧本不存在」，测不到迁移自检）
    work_store.write_work(wid, {"work_id": wid, "title": "未迁移树",
                                "work_card_id": "", "ending_derived": False,
                                "created_at": "2026-10-14T00:00:00+08:00"})
    # 读：明确报错（不返空）
    with pytest.raises(card_db.CardsNotMigratedError):
        await card_store.read_card(wid, "card_001")
    with pytest.raises(card_db.CardsNotMigratedError):
        card_store.iter_card_metas(wid)
    # 且**不得**被读操作静默建出空库（新库位 works_data/ 也不得出现）
    assert not (wd / "cards.db").exists()
    assert not card_db.db_path(wid).exists()
    # 写：同样明确报错（不静默建空库）
    r = await card_store.create_card(wid, "新卡", "模型生成", parent="")
    assert "error" in r and "尚未迁移" in r["error"], r
    assert not (wd / "cards.db").exists()
    assert not card_db.db_path(wid).exists()


async def test_no_db_and_no_md_is_empty_not_error(sandbox):
    """既无库也无遗留 md（全新 work）→ 读返空，不报错。"""
    wid = "work_778"
    (card_store.WORKS_DIR / wid).mkdir(parents=True, exist_ok=True)
    assert card_store.iter_card_metas(wid) == {}
    assert card_store.card_exists(wid, "card_001") is False
    assert card_db.read_rev(wid) is None


# ---------------------------------------------------------------- 附带：rev 单调

async def test_rev_monotonic_and_moves_on_card_write(work):
    wid, root = work["work_id"], work["work_card_id"]
    r1 = card_db.read_rev(wid)
    await card_store.update_card(wid, root, {"status_block": "变了"}, "改")
    r2 = card_db.read_rev(wid)
    assert r1 is not None and r2 is not None and r2 > r1


# ---------------------------------------------------------------- 附带：闸 3 未失效

async def test_gate3_rejects_unknown_owner(typed_registry, work):
    """闸 3（工具卡 owner）改查库后仍有效：owner 指向不存在的卡 → 拒，且库内零新行。"""
    wid = work["work_id"]
    before = len(card_store.iter_card_metas(wid))
    r = await card_store.create_card(
        wid, "工具卡", "模型生成", parent="", card_type="tool",
        owner_card_id="card_999",
    )
    assert "error" in r and "不存在" in r["error"], r
    assert len(card_store.iter_card_metas(wid)) == before
