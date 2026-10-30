"""把会话文件里的块补进 `session_blocks` 表（原文库回填）。

## 为什么要

`session_blocks` 是 md 落盘后的**同步副本**（`session_facade.append` 末尾调
`origin_store.write_blocks_sync`），但那个双写是 **2026-10-20** 才加的——
此前的块从未写过库。实测 `work_027`：

```
card_021   sqlite 3220 行 / 文件 4008 块  → 缺 788
fn_004     sqlite 1960 行 / 文件 2104 块  → 缺 144
card_061   sqlite 1157 行 / 文件 1157 块  → 一致（建得晚）
```

凝练要改成「只看 sqlite」的话，先把库补全，否则会漏掉这几百个块。

## 怎么补

对每张有会话文件的卡：`session_facade.read` 读全部块 → `write_blocks_sync` 写。
`INSERT OR REPLACE` 幂等，已有的行被同内容覆盖，缺的补上。

## 安全

**只写 `session_blocks`，不碰会话文件、不碰 cards**。跑之前先备份 cards.db。

用法：
```
.venv/Scripts/python.exe scripts/backfill_session_blocks.py            # 全部 work
.venv/Scripts/python.exe scripts/backfill_session_blocks.py work_027   # 指定 work
```
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.storage import origin_store, session_facade  # noqa: E402


def _db_count(work_id: str, card_id: str) -> int:
    p = _ROOT / "works" / work_id / "cards.db"
    if not p.exists():
        return -1
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return int(c.execute(
            "SELECT COUNT(*) FROM session_blocks WHERE card_id=?",
            (card_id,)).fetchone()[0])
    except sqlite3.Error:
        return -1
    finally:
        c.close()


async def _one_work(work_id: str) -> tuple[int, int, int]:
    """补一个 work。返回 (卡数, 补前总行, 补后总行)。"""
    sess_dir = _ROOT / "works" / work_id / "sessions"
    if not sess_dir.is_dir():
        return (0, 0, 0)
    cards: list[str] = []
    for f in sess_dir.glob("session_*.md"):
        stem = f.stem[len("session_"):]
        cards.append(f"card_{stem}" if stem.isdigit() else stem)

    before = sum(max(_db_count(work_id, c), 0) for c in cards)
    fixed = 0
    for cid in cards:
        try:
            blocks = await session_facade.read(work_id, cid)
        except Exception as exc:  # noqa: BLE001 单卡读失败不挡其余
            print(f"    ✗ {cid} 读会话失败：{exc}")
            continue
        if not blocks:
            continue
        n = _db_count(work_id, cid)
        if n >= len(blocks):
            continue                     # 已齐，跳过（幂等且省时）
        try:
            origin_store.write_blocks_sync(work_id, cid, blocks)
            fixed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ {cid} 写库失败：{exc}")
    after = sum(max(_db_count(work_id, c), 0) for c in cards)
    return (len(cards), before, after)


async def main(argv: list[str]) -> int:
    only = argv[1] if len(argv) > 1 else ""
    works = ([only] if only else
             sorted(p.name for p in (_ROOT / "works").glob("work_*")
                    if (p / "cards.db").exists()))
    print(f"回填 {len(works)} 个 work（只写 session_blocks，不动会话文件与 cards）")

    # 备份
    for w in works:
        db = _ROOT / "works" / w / "cards.db"
        if db.exists():
            bak = db.with_suffix(".db.bak_backfill")
            if not bak.exists():
                shutil.copyfile(db, bak)
    print("已备份为 cards.db.bak_backfill（已存在则跳过）\n")

    tot_cards = tot_before = tot_after = 0
    for w in works:
        n, b, a = await _one_work(w)
        if n == 0:
            continue
        delta = a - b
        flag = "" if delta == 0 else f"  ← 补 {delta}"
        print(f"  {w:12s} 卡 {n:3d}  行 {b:6d} → {a:6d}{flag}")
        tot_cards += n
        tot_before += b
        tot_after += a
    print("-" * 56)
    print(f"合计：卡 {tot_cards}，行 {tot_before} → {tot_after}（补 {tot_after - tot_before}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
