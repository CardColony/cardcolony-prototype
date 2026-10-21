# -*- coding: utf-8 -*-
"""按当前默认策略重算项目模型配置表里 **src=default** 的行。

[为什么需要重算]
「表存结果」的代价：默认策略（分层/兜底链）变了，已填的行不会自动跟着变。
人手动改过的行（src=manual）**一律不动**。

实现在 ``model_config.reseed_tables()``——设置界面「保存分层」后调的是同一个函数，
所以命令行与界面行为完全一致，不会出现两套重算逻辑。

用法：
    python scripts/reseed_model_tables.py --dry-run     # 只看会改哪些
    python scripts/reseed_model_tables.py               # 真写
    python scripts/reseed_model_tables.py --work work_020
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import model_config                      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    ap.add_argument("--work", default="", help="只处理指定项目")
    args = ap.parse_args()

    res = model_config.reseed_tables(args.work, dry_run=args.dry_run)
    for it in res.get("items") or []:
        fb = it.get("fallbacks") or []
        tail = f"  （层内兜底 {fb}）" if fb else ""
        print(f"{it['work_id']}/{it['card_id']}: {it['from'] or '(空)'} -> {it['to'] or '(空)'}{tail}")

    verb = "将改" if args.dry_run else "已改"
    print(f"\n扫过 {res.get('scanned', 0)} 行，{verb} {res.get('changed', 0)} 行"
          + ("（dry-run，未写盘）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
