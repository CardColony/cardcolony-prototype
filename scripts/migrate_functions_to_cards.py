# -*- coding: utf-8 -*-
"""[职能实体化 A 案 2026-10-22 · 用户裁定] 给存量职能补 cards 行。

**背景（用户裁定 A）**：职能＝「cards 表里有一行、但呈现层（画布/卡树/统计）不露面」
的实体。为什么必须有一行：perms 分权、build_ctx 上下文装配、_run_message 回合、
create_card、spawn 巡检**全部按 card_id 焊死**；把职能踢出 cards 表，等于一次性把
它踢出这一切——实测 fn 只能看 146 字上下文、跑不了回合、建不了卡。

**做法**：
1. `function_profiles` 补一列 `card_id`（幂等；card_db 是禁改文件，故在本脚本里
   ALTER TABLE，与 `_h_report_done` 补 coder_final 同一手法）。
2. 对每个 `card_id` 为空的职能：建一张 `card_type="function"` 的卡（parent 恒空＝离树，
   `_normalize_parent` 已把 function 与 project/role 同档处理），再回填 `card_id`。

**不动的东西**：`function_profiles` 原有列（jurisdiction/watch/speak_contract/duty/
charter）一个不改——职能语义仍住在那张表；cards 行只承载基础设施。

用法：
    python scripts/migrate_functions_to_cards.py --dry-run   # 只看会建哪些
    python scripts/migrate_functions_to_cards.py
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.storage import card_db, card_store, function_store, work_store  # noqa: E402


async def _ensure_column(work_id: str) -> bool:
    """给 function_profiles 补 card_id 列（幂等）。返回是否新加。"""
    added = {"v": False}

    def _fn(conn: sqlite3.Connection) -> None:
        cols = {str(c["name"]) for c in conn.execute(
            "PRAGMA table_info(function_profiles)").fetchall()}
        if "card_id" not in cols:
            conn.execute(
                "ALTER TABLE function_profiles ADD COLUMN card_id TEXT NOT NULL DEFAULT ''")
            added["v"] = True

    await card_db.run_write(work_id, _fn)
    return added["v"]


async def _set_card_id(work_id: str, function_id: str, card_id: str) -> None:
    def _fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE function_profiles SET card_id=? WHERE work_id=? AND function_id=?",
            (card_id, work_id, function_id))

    await card_db.run_write(work_id, _fn)


async def _find_existing(work_id: str, title: str) -> list[str]:
    """该 work 里同名 function 卡的 card_id 列表（清理旧版 card_NNN 行用）。"""
    def _fn(conn: sqlite3.Connection) -> list[str]:
        return [str(r["card_id"]) for r in conn.execute(
            "SELECT card_id FROM cards WHERE work_id=? AND card_type='function' "
            "AND title=? AND state!='deleted'", (work_id, title)).fetchall()]

    return await card_db.run_write(work_id, _fn)


async def _card_exists(work_id: str, card_id: str) -> bool:
    def _fn(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM cards WHERE work_id=? AND card_id=?",
            (work_id, card_id)).fetchone() is not None

    return await card_db.run_write(work_id, _fn)


async def migrate_work(work_id: str, dry_run: bool) -> tuple[int, int, int]:
    """返回 (职能总数, 已挂卡, 本次新建)。"""
    if dry_run:
        profs = function_store.list_profiles(work_id)
        has = sum(1 for p in profs if str(p.get("card_id") or ""))
        return len(profs), has, len(profs) - has

    await _ensure_column(work_id)
    profs = function_store.list_profiles(work_id)
    created = 0
    for prof in profs:
        # 已经是 fn_NNN 身份（新口径）⇒ 无需动；card_NNN 是上一版遗留，要重做。
        _cur = str(prof.get("card_id") or "")
        if _cur.startswith("fn_"):
            continue
        fid = str(prof.get("function_id") or "")
        name = str(prof.get("function_name") or fid)
        # [用户令「不能是 card_id，这样会很乱」] 职能的唯一身份＝function_id（fn_NNN）。
        # cards 行的主键就用 fn_NNN ⇒ 事件流/会话/档案/左栏全是同一个号，不出现 card_NNN。
        if await _card_exists(work_id, fid):
            await _set_card_id(work_id, fid, fid)
            print(f"  = {fid}「{name}」已有 cards 行（id=fn）")
            continue
        # 清掉上一版用 card_NNN 建的旧行（只删 type=function 的同名卡）
        for old in await _find_existing(work_id, name):
            await card_store.delete_card(work_id, old)
            print(f"  - 清理旧行 {old}（{name}）")
        r = await card_store.create_card(
            work_id, name, "系统派生", card_type="function", card_id=fid)
        if "error" in r:
            print(f"  ✗ {fid}「{name}」建卡失败: {r['error']}")
            continue
        cid = str((r.get("meta") or {}).get("card_id") or "")
        await _set_card_id(work_id, fid, cid)
        created += 1
        print(f"  ✓ {fid}「{name}」→ {cid}")
    profs = function_store.list_profiles(work_id)
    has = sum(1 for p in profs if str(p.get("card_id") or ""))
    return len(profs), has, created


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--work", default="", help="只处理一个 work")
    args = ap.parse_args()

    if args.work:
        wids = [args.work]
    else:
        wids = [w.get("work_id", "") for w in work_store.list_works()]
    tot = has = cre = 0
    for wid in wids:
        if not wid:
            continue
        n, h, c = await migrate_work(wid, args.dry_run)
        if n:
            print(f"{wid}: 职能 {n}｜已挂卡 {h}｜本次{'待建' if args.dry_run else '新建'} {c}")
        tot += n
        has += h
        cre += c
    print(f"\n合计：职能 {tot}｜已挂卡 {has}｜{'待建' if args.dry_run else '新建'} {cre}")


if __name__ == "__main__":
    asyncio.run(main())
