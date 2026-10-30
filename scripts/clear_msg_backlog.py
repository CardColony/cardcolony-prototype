# -*- coding: utf-8 -*-
"""清空存量卡间消息积压：把每张卡的读取水位预推到当前最大 seq。

**为什么需要这个脚本**

2026-10-23 前的投递路径（`deliver_now` / K3）把待处理消息全文拼进 prompt 却
**不推读取水位**；水位只在模型主动调 `read_inbox(scope="unread")` 时才前进。
于是模型看过、甚至处理过的消息永远留在「待处理」里，实测 work_025 积压 299 条
（最老 10-22 11:58），card_029 一次投递 46 条 = prompt 的 98%。

投递路径已改为「投递即消费」（`card_db.take_pending`）。但**存量**那批如果不管，
下一次投递会把该卡的全部旧消息再完整发一遍（一次性）。

本脚本把那批存量标记为已读——**不投递**。

**做了什么 / 没做什么**

- 做：`meta.msg_seen_<card>` 推到扫描时刻该卡的 `MAX(seq)`。
- 没做：**不删任何行**。`messages` 表原样保留，历史可回溯；
  `read_inbox(scope="all")` 仍能翻到全部往来。
- 审计：每张被跳过的卡写一行到 `works/_ops/msg_backlog_cleared.jsonl`
  （work / card / 跳过条数 / 水位 / 扫描时刻 / 最老一条时间），可复核。

**幂等**：水位只增不减，再跑一次会算 0 条待跳过。

**并发安全**：WAL + `busy_timeout`，每个 work 用 `BEGIN IMMEDIATE` 事务；
扫描与推水位在同一事务内 ⇒ 扫描期间新到的信（seq 更大）不会被误跳过。

用法::

    python scripts/clear_msg_backlog.py            # 干跑，只报告
    python scripts/clear_msg_backlog.py --apply    # 真正推进水位
    python scripts/clear_msg_backlog.py --work work_025 --apply
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKS = ROOT / "works"
AUDIT = WORKS / "_ops" / "msg_backlog_cleared.jsonl"

_BUSY_TIMEOUT_S = 10.0


def _seen(conn: sqlite3.Connection, card_id: str) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", ("msg_seen_" + str(card_id),)
    ).fetchone()
    if row is None:
        return -1
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return -1


def _advance(conn: sqlite3.Connection, card_id: str, seq: int) -> None:
    """水位只增不减。"""
    if int(seq) <= _seen(conn, card_id):
        return
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("msg_seen_" + str(card_id), str(int(seq))),
    )


def scan_work(db: Path, apply: bool, before: str) -> list[dict]:
    """返回该 work 被跳过（或将要被跳过）的卡清单。

    只跳 **created_at < before** 的存量。按 seq 升序走，遇到第一条「不早于 before」
    的就停——水位只推到这个连续旧前缀的末尾，更新的消息（seq 更大）保持 pending，
    照常投递。这样正在跑的工作（消息每分钟在流动）一条都不会被吞。
    """
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db), timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    out: list[dict] = []
    try:
        conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
        conn.execute("PRAGMA journal_mode=WAL")
        # 扫描与推水位在同一写事务内：期间新到的信（seq 更大）不会被误跳。
        conn.execute("BEGIN IMMEDIATE")
        try:
            cards = [r[0] for r in conn.execute(
                "SELECT DISTINCT target_card_id FROM messages"
                " WHERE target_card_id IS NOT NULL AND target_card_id != ''")]
            now = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
            for cid in cards:
                seen = _seen(conn, cid)
                rows = conn.execute(
                    "SELECT seq, created_at FROM messages"
                    " WHERE target_card_id = ? AND seq > ? ORDER BY seq",
                    (cid, seen)).fetchall()
                if not rows:
                    continue
                # 连续旧前缀：遇到第一条不早于 before 的就停
                upto = None
                n_old = 0
                oldest = ""
                for r in rows:
                    if str(r["created_at"] or "") < before:
                        upto = int(r["seq"] or 0)
                        n_old += 1
                        if not oldest:
                            oldest = str(r["created_at"] or "")
                    else:
                        break
                if upto is None or n_old <= 0:
                    continue
                out.append({
                    "work": db.parent.name,
                    "card": cid,
                    "skipped": n_old,
                    "remaining": len(rows) - n_old,
                    "seen_before": seen,
                    "seen_after": upto,
                    "oldest": oldest,
                    "before": before,
                    "at": now,
                })
                if apply:
                    _advance(conn, cid, upto)
            conn.execute("COMMIT" if apply else "ROLLBACK")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清空存量卡间消息积压（推进读取水位）")
    ap.add_argument("--apply", action="store_true", help="真正推进水位（缺省只报告）")
    ap.add_argument("--work", default="", help="只处理某个 work（缺省全部）")
    ap.add_argument(
        "--before", default="",
        help="只跳过 created_at 早于此时间（ISO）的消息；缺省＝1 小时前。"
             "此时间之后的消息一律不动（正在跑的工作不受影响）")
    args = ap.parse_args(argv)

    if args.before:
        before = args.before
    else:
        before = (_dt.datetime.now().astimezone()
                  - _dt.timedelta(hours=1)).isoformat(timespec="seconds")

    dbs = sorted(WORKS.glob("work_*/cards.db"))
    if args.work:
        dbs = [d for d in dbs if d.parent.name == args.work]
    if not dbs:
        print("没有找到 cards.db")
        return 1

    all_rows: list[dict] = []
    for db in dbs:
        all_rows.extend(scan_work(db, args.apply, before))

    if not all_rows:
        print(f"没有需要跳过的存量消息（{before} 之前无未读）。")
        return 0

    total = sum(r["skipped"] for r in all_rows)
    mode = "已应用" if args.apply else "干跑（未改动）"
    print(f"=== {mode} === 跳过 {len(all_rows)} 张卡的 {total} 条存量消息"
          f"（created_at < {before}）\n")
    print(f"{'work':10} {'卡':10} {'跳过':>4} {'保留':>4} {'水位':>8} → {'':<8}  最老一条")
    for r in all_rows:
        print(f"{r['work']:10} {r['card']:10} {r['skipped']:>4} {r['remaining']:>4} "
              f"{r['seen_before']:>8} → {r['seen_after']:<8}  {r['oldest'][:19]}")

    if not args.apply:
        print(f"\n干跑结束。要真正应用：加 --apply")
        return 0

    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n已应用：{total} 条存量消息移出收件箱（行未删，仍在 messages 表）。")
    print(f"审计留痕：{AUDIT.relative_to(ROOT)}（{len(all_rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
