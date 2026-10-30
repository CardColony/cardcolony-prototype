# -*- coding: utf-8 -*-
"""记忆节点独立编号体系 · 一次性回填（2026-10-29）。

## 做什么

把 `memory` 表 `chain` 列里的 CausalNode 数组，摊平进独立表 `memory_nodes`，
并为每条生成自包含编号：

    {work_id}/{card_id}/B{批次:04d}/{批内序:04d}        ← L1 结论节点
    {work_id}/{card_id}/B{批次:04d}/{批内序:04d}/d      ← L2 描述节点（父编号 + /d）

## 时间戳与批次从哪来

`chain` 里的 CausalNode 没有 `occurred_at`（迁移时丢的），但
`works/<w>/memory/<card>.json` 备份里每条 L1 都带微秒级时间戳，且与库里的
L1 **逐条同序同文本**（2026-10-29 实测 card_001/021/061 全对得上）。

同一批凝练写入的条目共享**同一个**时间戳 ⇒ 按时间戳去重即可还原批次。
批次号按时间升序从 1 发，零填充 4 位 ⇒ 编号字典序 == 批次序 == 时间序。

## 边界（不猜）

- 备份文件不存在，或备份条数与库里 L1 条数不一致 ⇒ **该卡不套时间**：
  `occurred_at=''`、`batch=0`、`ordinal` 按数组原序。批次 0 在渲染侧按「远期」处理。
- L2 节点没有独立时间戳，继承其父 L1（`anchor.seq` 相同且 role=gist）的批次与时间。

## 幂等

按 `(work_id, card_id)` 先 DELETE 再 INSERT，可反复执行。
`--dry-run` 只算不写。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from typing import Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DDL = """
CREATE TABLE IF NOT EXISTS memory_nodes (
    id            TEXT PRIMARY KEY,
    work_id       TEXT NOT NULL,
    card_id       TEXT NOT NULL,
    batch         INTEGER NOT NULL DEFAULT 0,
    ordinal       INTEGER NOT NULL DEFAULT 0,
    occurred_at   TEXT NOT NULL DEFAULT '',
    level         INTEGER NOT NULL DEFAULT 1,
    role          TEXT NOT NULL DEFAULT 'gist',
    gist          TEXT NOT NULL DEFAULT '',
    anchor_seq    INTEGER NOT NULL DEFAULT 0,
    covered_seqs  TEXT NOT NULL DEFAULT '[]',
    upper_id      TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT '',
    process       TEXT NOT NULL DEFAULT '',
    start_text    TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    lesson        TEXT NOT NULL DEFAULT '',
    sunk          INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT NOT NULL DEFAULT '',
    extra         TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL DEFAULT ''
)
"""

IDX = [
    "CREATE INDEX IF NOT EXISTS idx_mn_card ON memory_nodes(work_id, card_id, batch, ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_mn_time ON memory_nodes(work_id, card_id, occurred_at)",
]

#: chain 节点里已经单独成列的键；其余一律进 extra，保证不丢字段
KNOWN = {
    "anchor", "gist", "status", "kind", "coveredSeqs", "level", "role",
    "upper", "lower", "process", "start", "result", "lesson",
    "sunk", "supersededBy", "degraded", "merged", "lastUsedAt",
}


def _load_backup_times(work_dir: str, card_id: str) -> Optional[list[str]]:
    """读备份 JSON，返回 L1 条目的时间戳列表（按原数组序）。取不到返回 None。"""
    path = os.path.join(work_dir, "memory", f"{card_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    entries = data.get("entries")
    if not isinstance(entries, list):
        return None
    out: list[str] = []
    for e in entries:
        if not isinstance(e, dict):
            return None
        t = str(e.get("occurred_at") or "").strip()
        if not t:
            return None
        out.append(t)
    return out or None


def _seq_of(node: dict[str, Any]) -> int:
    a = node.get("anchor")
    if isinstance(a, dict):
        try:
            return int(a.get("seq", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def plan_card(work_id: str, card_id: str, chain: list[dict[str, Any]],
              times: Optional[list[str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把一条链算成 memory_nodes 行（纯函数，不写库）。"""
    l1_idx = [i for i, n in enumerate(chain)
              if int(n.get("level", 1) or 1) == 1]
    use_time = bool(times) and len(times) == len(l1_idx)

    # L1 下标 → 时间
    tmap: dict[int, str] = {}
    if use_time:
        for pos, idx in enumerate(l1_idx):
            tmap[idx] = times[pos]  # type: ignore[index]

    # 时间 → 批次号（升序，从 1 起）；无时间的整卡批次为 0
    uniq = sorted({t for t in tmap.values()})
    batch_of_time = {t: i + 1 for i, t in enumerate(uniq)}

    # 父 seq → L1 下标（供 L2 继承）
    parent_by_seq: dict[int, int] = {}
    for idx in l1_idx:
        parent_by_seq.setdefault(_seq_of(chain[idx]), idx)

    rows: list[dict[str, Any]] = []
    ordinal_of_batch: dict[int, int] = {}
    id_of_idx: dict[int, str] = {}
    unknown_ord = 0

    for idx, node in enumerate(chain):
        level = int(node.get("level", 1) or 1)
        role = str(node.get("role") or "gist")
        if level == 1:
            batch = batch_of_time.get(tmap.get(idx, ""), 0) if use_time else 0
            occurred = tmap.get(idx, "") if use_time else ""
        else:
            pidx = parent_by_seq.get(_seq_of(node))
            batch = (batch_of_time.get(tmap.get(pidx, ""), 0)
                     if (use_time and pidx is not None) else 0)
            occurred = tmap.get(pidx, "") if (use_time and pidx is not None) else ""

        ordinal_of_batch[batch] = ordinal_of_batch.get(batch, 0) + 1
        ordv = ordinal_of_batch[batch]
        if batch == 0:
            unknown_ord += 1
            ordv = unknown_ord

        base_id = f"{work_id}/{card_id}/B{batch:04d}/{ordv:04d}"
        if level == 1:
            node_id = base_id
            id_of_idx[idx] = node_id
        else:
            pidx = parent_by_seq.get(_seq_of(node))
            parent_id = id_of_idx.get(pidx, "") if pidx is not None else ""
            node_id = (parent_id + "/d") if parent_id else (base_id + "/d")

        upper = node.get("upper")
        upper_id = ""
        if isinstance(upper, list) and upper and isinstance(upper[0], dict):
            uidx = parent_by_seq.get(_seq_of({"anchor": upper[0]}))
            upper_id = id_of_idx.get(uidx, "") if uidx is not None else ""

        rows.append({
            "id": node_id,
            "work_id": work_id,
            "card_id": card_id,
            "batch": batch,
            "ordinal": ordv,
            "occurred_at": occurred,
            "level": level,
            "role": role,
            "gist": str(node.get("gist") or ""),
            "anchor_seq": _seq_of(node),
            "covered_seqs": json.dumps(node.get("coveredSeqs") or [], ensure_ascii=False),
            "upper_id": upper_id,
            "status": str(node.get("status") or ""),
            "kind": str(node.get("kind") or ""),
            "process": str(node.get("process") or ""),
            "start_text": str(node.get("start") or ""),
            "result": str(node.get("result") or ""),
            "lesson": str(node.get("lesson") or ""),
            "sunk": 1 if node.get("sunk") else 0,
            "superseded_by": str(node.get("supersededBy") or ""),
            "extra": json.dumps({k: v for k, v in node.items() if k not in KNOWN},
                                ensure_ascii=False),
            "updated_at": "",
        })

    stat = {
        "card_id": card_id,
        "total": len(chain),
        "l1": len(l1_idx),
        "l2": len(chain) - len(l1_idx),
        "batches": len(uniq) if use_time else 0,
        "timed": use_time,
        "time_note": "" if use_time else ("无备份" if not times else "条数不符"),
    }
    return rows, stat


def run(dry_run: bool) -> int:
    works = sorted(glob.glob(os.path.join(BASE_DIR, "works", "*", "cards.db")))
    total_rows = 0
    for db in works:
        work_dir = os.path.dirname(db)
        work_id = os.path.basename(work_dir)
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            mem = con.execute("SELECT card_id, chain FROM memory").fetchall()
        except sqlite3.Error:
            con.close()
            continue
        if not mem:
            con.close()
            continue

        all_rows: list[dict[str, Any]] = []
        stats: list[dict[str, Any]] = []
        for r in mem:
            card_id = r["card_id"]
            try:
                chain = json.loads(r["chain"] or "[]")
            except (TypeError, ValueError):
                print(f"  !! {work_id}/{card_id} chain 解析失败，跳过")
                continue
            if not isinstance(chain, list):
                continue
            times = _load_backup_times(work_dir, card_id)
            rows, st = plan_card(work_id, card_id, chain, times)
            all_rows.extend(rows)
            stats.append(st)

        print(f"== {work_id}  {len(stats)} 卡 / {len(all_rows)} 节点")
        for st in stats:
            flag = "" if st["timed"] else f"  [无时间戳:{st['time_note']} ⇒ 批次0]"
            print(f"   {st['card_id']:<14} 总{st['total']:>4}  L1 {st['l1']:>4}"
                  f"  L2 {st['l2']:>3}  批 {st['batches']:>2}{flag}")

        if not dry_run:
            con.execute(DDL)
            for stmt in IDX:
                con.execute(stmt)
            con.executemany(
                "DELETE FROM memory_nodes WHERE work_id=? AND card_id=?",
                [(work_id, st["card_id"]) for st in stats])
            cols = list(all_rows[0].keys()) if all_rows else []
            if cols:
                sql = ("INSERT OR REPLACE INTO memory_nodes(" + ",".join(cols) +
                       ") VALUES(" + ",".join("?" * len(cols)) + ")")
                con.executemany(sql, [[r[c] for c in cols] for r in all_rows])
            con.commit()
            got = con.execute(
                "SELECT count(*) FROM memory_nodes WHERE work_id=?", (work_id,)).fetchone()[0]
            print(f"   已写入 {got} 行")
        con.close()
        total_rows += len(all_rows)

    print(f"\n合计 {total_rows} 节点" + ("（dry-run，未写库）" if dry_run else "（已写库）"))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(run(args.dry_run))
