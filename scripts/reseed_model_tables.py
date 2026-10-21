# -*- coding: utf-8 -*-
"""按当前默认策略重算项目模型配置表里 **src=default** 的行。

[为什么需要一次性重算]
「表存结果」的代价：默认策略变了，已填的行不会自动跟着变。而 2026-10-21 之前的
行是用旧策略填的（普通卡不看分层 ⇒ 一律 system.default_model = minimax），
不重算就永远锁在 minimax。人手动改过的行（src=manual）**一律不动**。

用法：
    python scripts/reseed_model_tables.py --dry-run     # 只看会改哪些
    python scripts/reseed_model_tables.py               # 真写
    python scripts/reseed_model_tables.py --work work_020
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import model_config                      # noqa: E402
from src.storage import model_override, work_store    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    ap.add_argument("--work", default="", help="只处理指定项目")
    args = ap.parse_args()

    works_dir = Path(work_store.WORKS_DIR)
    if not works_dir.exists():
        print(f"works 目录不存在: {works_dir}")
        return 1

    total_rows = changed_rows = 0
    for d in sorted(works_dir.iterdir()):
        if not d.is_dir():
            continue
        wid = d.name
        if args.work and wid != args.work:
            continue
        if not (d / model_override.TABLE_FILE).exists():
            continue
        data = model_override.load(wid)
        cards = data.get("cards") or {}
        for cid, row in list(cards.items()):
            total_rows += 1
            if row.get("src") == "manual":
                continue
            new_row = model_config._policy_row(wid, cid)      # 按当前策略重算
            old_model = row.get("model") or ""
            new_model = new_row.get("model") or ""
            if old_model == new_model and (row.get("bindings") or {}) == (new_row.get("bindings") or {}):
                continue
            changed_rows += 1
            print(f"{wid}/{cid}: {old_model or '(空)'} -> {new_model or '(空)'}")
            if not args.dry_run:
                model_override.put_row(wid, cid, new_row, src="default")

    verb = "将改" if args.dry_run else "已改"
    print(f"\n扫过 {total_rows} 行，{verb} {changed_rows} 行"
          + ("（dry-run，未写盘）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
