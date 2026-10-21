# -*- coding: utf-8 -*-
"""存量会话 → 原文表（session_blocks）补齐（2026-10-20 · 原文入库）。

为什么需要：`session_facade` 从 2026-10-20 起在 md 落盘后**双写**原文表；
此前的块只在 md 里 ⇒ 记忆条目追溯不到。本脚本把 md 里已有、库里没有的块补进来。

用法（**先 dry-run，看清楚了再落**）：

    .venv/Scripts/python.exe scripts/backfill_origin.py --dry-run
    .venv/Scripts/python.exe scripts/backfill_origin.py            # 真写

纪律：
- 只**读** md（`session_facade.read`），只**写**原文表；不动 md、不动卡片、不动记忆。
- 幂等：库里已有的 `(card_id, seq)` 跳过；重复跑不产生重复行。
- 逐 work 逐卡报告：新增 N 行 / 已有 M 行 / 失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage import card_store, origin_store, session_facade  # noqa: E402
from src.storage import work_store  # noqa: E402


def _iter_works() -> list[str]:
    """列出全部 work 目录（按目录名，不解析 work.json——读不到也照样尝试）。"""
    root = work_store.WORKS_DIR
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


async def _backfill_card(work_id: str, card_id: str, dry: bool) -> tuple[int, int, str]:
    """返回 (新增, 已有, 错误串)。"""
    try:
        blocks = await session_facade.read(work_id, card_id)
    except Exception as exc:  # noqa: BLE001 读不到就当没有
        return 0, 0, f"读会话失败: {exc}"
    if not blocks:
        return 0, 0, ""
    have = origin_store.existing_seqs(work_id, card_id)
    missing = [b for b in blocks if int(b.get("seq") or 0) not in have]
    if not missing:
        return 0, len(have), ""
    if not dry:
        origin_store.write_blocks_sync(work_id, card_id, missing)
    return len(missing), len(have), ""


async def main(dry: bool) -> int:
    works = _iter_works()
    print(f"{'[dry-run] ' if dry else ''}共 {len(works)} 个 work")
    total_new = total_have = 0
    errors: list[str] = []
    for wid in works:
        metas = card_store.iter_card_metas(wid)
        if not metas:
            continue
        for cid in sorted(metas):
            new, have, err = await _backfill_card(wid, cid, dry)
            total_new += new
            total_have += have
            if new:
                print(f"  {wid}/{cid}: 新增 {new} 行（已有 {have}）")
            if err:
                errors.append(f"{wid}/{cid}: {err}")
    print(f"\n合计：新增 {total_new} 行；库里已有 {total_have} 行；错误 {len(errors)} 条")
    for e in errors[:20]:
        print("  !", e)
    return 0 if not errors else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
