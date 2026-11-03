# -*- coding: utf-8 -*-
"""线上卡归拢清账：对 work_027 内 tops≥触发线 的**活卡**逐卡实跑 `_maybe_fold`。

流程：
  1. 全量备份 memory 表 → logs/fold_backup_<ts>.json（可回滚）；
  2. 逐卡（tops 降序）循环归拢，直到 tops < 触发线 或 单轮无净降层（judge-empty/
     管线失败不硬推）；
  3. 每卡结束跑不变量体检 + 渲染体积对比。

真模型调用、真写库。`card_001_foldtest` 影子卡排除在外。

用法：
    python scripts/fold_live_run.py [--work work_027] [--cards a,b,c]
    python scripts/fold_live_run.py --backup-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s")

from src.storage import memory_domain, memory_store as m  # noqa: E402
from src.api import context_regions  # noqa: E402

EXCLUDE = {"card_001_foldtest"}
MAX_ROUNDS = 6


def _db(work: str) -> str:
    return os.path.join(BASE_DIR, "works", work, "cards.db")


def _backup(work: str) -> str:
    """memory 表全量快照（chain/mark/batch_starts）。"""
    conn = sqlite3.connect(_db(work))
    rows = conn.execute(
        "SELECT card_id, chain, condense_mark, fold_mark, batch_starts"
        " FROM memory").fetchall()
    conn.close()
    out = {cid: {"chain": chain, "condense_mark": cm,
                 "fold_mark": fm, "batch_starts": bs}
           for cid, chain, cm, fm, bs in rows}
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BASE_DIR, "logs", f"fold_backup_{work}_{ts}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"work": work, "ts": ts, "rows": out},
                  f, ensure_ascii=False)
    return path


def _stats(work: str, cid: str) -> dict:
    st = m.load_memory(work, cid)
    tops = m._foldable_tops(st)
    synth = sum(1 for _i, t in tops
                if int((t.get("anchor") or {}).get("seq") or 0)
                >= m.SYNTH_SEQ_BASE)
    rows = m.load_nodes(work, cid)
    text = context_regions.render_memory_region(rows)
    return {"tops": len(tops), "digest_tops": synth,
            "entries": len(st.get("entries") or []),
            "render_bytes": len(text.encode("utf-8")),
            "state": st}


async def _fold_card(work: str, cid: str, trigger: int) -> list[str]:
    """循环归拢一卡直到收敛；返回进度行。"""
    log: list[str] = []
    before = _stats(work, cid)
    log.append(f"── {cid}: tops={before['tops']} "
               f"entries={before['entries']} render={before['render_bytes']}B")
    for rd in range(1, MAX_ROUNDS + 1):
        cur = _stats(work, cid)
        if cur["tops"] < trigger:
            log.append(f"   tops={cur['tops']} < {trigger}，已收敛")
            break
        t0 = time.time()
        try:
            await m._maybe_fold(work, cid)
        except Exception as exc:  # noqa: BLE001 单卡失败不挡其他卡
            log.append(f"   轮{rd} 异常中止: {exc!r}（游标未动，可重跑）")
            break
        dt = time.time() - t0
        nxt = _stats(work, cid)
        log.append(f"   轮{rd}: tops {cur['tops']}→{nxt['tops']} "
                   f"({dt:.0f}s) render={nxt['render_bytes']}B")
        if nxt["tops"] >= cur["tops"]:
            log.append("   无净降层（judge-empty 或管线失败），本卡停止")
            break
    st = _stats(work, cid)["state"]
    viol = memory_domain.check_invariants(st.get("entries") or [])
    log.append(f"   终: tops={_stats(work, cid)['tops']} "
               f"digest={_stats(work, cid)['digest_tops']} "
               f"违例={len(viol)}{(' ' + str(viol[:2])) if viol else ''}")
    return log


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="work_027")
    ap.add_argument("--cards", default=None,
                    help="只跑列出的卡（逗号分隔），缺省=tops≥触发线全部")
    ap.add_argument("--backup-only", action="store_true")
    a = ap.parse_args()
    trigger = int(os.environ.get("CONDENSE_FOLD_TOP_TRIGGER", "40"))

    bak = _backup(a.work)
    print(f"[backup] {bak}", flush=True)
    if a.backup_only:
        return 0

    if a.cards:
        targets = [c.strip() for c in a.cards.split(",") if c.strip()]
    else:
        conn = sqlite3.connect(_db(a.work))
        ids = [r[0] for r in conn.execute("SELECT card_id FROM memory")]
        conn.close()
        targets = []
        for cid in ids:
            if cid in EXCLUDE:
                continue
            if len(m._foldable_tops(m.load_memory(a.work, cid))) >= trigger:
                targets.append(cid)
        targets.sort(key=lambda c: -len(m._foldable_tops(
            m.load_memory(a.work, c))))
    print(f"[targets] {len(targets)} 张卡过线（trigger={trigger}）: "
          + ", ".join(targets), flush=True)

    t_all = time.time()
    for cid in targets:
        for line in await _fold_card(a.work, cid, trigger):
            print(line, flush=True)
    print(f"[done] 全程 {time.time() - t_all:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
