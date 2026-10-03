# -*- coding: utf-8 -*-
"""存量卡补影子（影子卡机制 · 改件五，一次性脚本）。

对 works/ 下全部剧本的每一张正卡派生影子——调 shadow_store.derive_shadow
（与 create_card 钩点同一共用函数，幂等：影子已存在则跳过不覆盖）。
纪律（与 4.8③ 同族）：
  - 单卡失败：记录 work_id/card_id 与异常原文后跳过继续
  - 结束打印成功／失败计数
  - 不设重试、不设超时、不设轮次上限

用法：python backfill_shadows.py
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from src.storage import shadow_store, work_store  # noqa: E402


async def main() -> int:
    works_dir = BASE_DIR / "works"
    work_ids = sorted(
        p.name for p in works_dir.iterdir()
        if p.is_dir() and p.name.startswith("work_")
    ) if works_dir.exists() else []
    print(f"待补跑剧本 {len(work_ids)} 个: {work_ids}")

    ok_count = 0
    fail_count = 0
    skip_count = 0
    for wid in work_ids:
        cards_dir = works_dir / wid / "cards"
        if not cards_dir.exists():
            print(f"[skip] {wid}: 无 cards 目录")
            continue
        for card_path in sorted(cards_dir.glob("card_*.md")):
            cid = card_path.stem
            try:
                result = await shadow_store.derive_shadow(wid, cid)
                if "error" in result:
                    print(f"[fail] {wid}/{cid}: {result['error']}")
                    fail_count += 1
                elif result.get("existed"):
                    skip_count += 1
                else:
                    print(f"[ok] {wid}/{cid} -> {result['path']}")
                    ok_count += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[fail] {wid}/{cid}: {exc}")
                traceback.print_exc()
                fail_count += 1

    print(f"补跑结束：新建 {ok_count}，已存在跳过 {skip_count}，失败 {fail_count}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
