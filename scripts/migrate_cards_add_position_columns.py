# -*- coding: utf-8 -*-
"""cards 表加位置三列（pos_x / pos_y / pinned）——画布 UI 重构 · 阶段 1.1。

背景：卡片位置不再另开 ``works/{work_id}/layout.json`` 存放（原"画布第二步 D1"
的拆存储设计），位置并入 cards 表——**位置是卡片自身属性**（任务书约束 4），
与 title/parent 同级。本脚本给 13 个存量库补上这三列。

纪律（沿 ``migrate_canvas_positions.py`` / ``migrate_cards_to_sqlite.py`` 先例）：

1. **默认 dry-run 只出清单，``--apply`` 才落 DDL**（dry-run 零写入：只
   ``PRAGMA table_info`` 读结构）。
2. **幂等可重跑**：已有该列即跳过（判据＝``PRAGMA table_info(cards)``），
   反复跑结果不变；**ADD COLUMN 不读写任何既有行**，故重跑零副作用。
3. **any-fail-abort**：先全部扫描并把计划算在内存里，任何一个库打不开／形态
   非法＝**零 ALTER**、非零退出、逐库清单里标出失败原因。
4. **DDL 单一真相**：列定义从 ``src.storage.card_db.POSITION_COLUMNS`` 导入，
   **不在本脚本另抄一份**——运行时 ``_ensure_columns`` 的自动升级路径与本脚本
   必须补的是同一组列，两处各写一遍迟早漂移（抄一份＝迟早有人只改其中一处）。
   ★ 这是本脚本**唯一** import src 的理由，且它只 import 纯常量模块
   （``card_db`` 不在 import 期开任何库、不碰 ``main``）。
5. **收尾自检**：``--apply`` 后逐库复核列在位，并统计存量行的
   ``pos_x/pos_y IS NULL`` 与 ``pinned=0`` 是否如约（未摆过的卡不该有坐标）。

用法：
    python scripts/migrate_cards_add_position_columns.py [--target <works树>] [--apply]

``--target`` 缺省＝本仓 ``works/``（可用它先在副本上演练）。
输出：stdout 逐库清单（JSON 行）＋ 汇总行；退出码 0=成功 1=有失败 2=参数错 3=导入失败。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# DDL 单一真相：与运行时自动升级路径共用一份定义（见模块 docstring 纪律 4）
try:
    sys.path.insert(0, str(ROOT))
    from src.storage.card_db import POSITION_COLUMNS
except Exception as exc:  # noqa: BLE001 导入失败宁可不跑，也不拿过期副本凑数
    print(
        f"[migrate] FAIL 无法导入 src.storage.card_db.POSITION_COLUMNS: {exc!r}\n"
        f"          本脚本拒绝内置列定义副本（会与运行时漂移），请在仓库根执行。",
        file=sys.stderr,
    )
    raise SystemExit(3)

_BUSY_TIMEOUT_S = 5.0


def _inspect(db: Path) -> dict:
    """读该库的 cards 表结构：返回 {work, db, has_table, missing[], total_cols}。"""
    work_id = db.parent.name
    item: dict = {"work": work_id, "db": str(db), "missing": []}
    conn = sqlite3.connect(str(db), timeout=_BUSY_TIMEOUT_S)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("PRAGMA table_info(cards)").fetchall()
        item["has_table"] = bool(rows)
        if not rows:
            # 表还不存在＝没有任何卡片数据（或还没建库）：无从加列，也不该建
            item["action"] = "skip_no_cards_table"
            return item
        have = {r["name"] for r in rows}
        item["total_cols"] = len(have)
        item["missing"] = [col for col, _ddl in POSITION_COLUMNS if col not in have]
        item["action"] = "alter" if item["missing"] else "skip_already_present"
        return item
    finally:
        conn.close()


def _apply_one(db: Path, missing: list[str]) -> dict:
    """对该库执行缺列 ALTER，并复核结果。返回复核记录。"""
    ddl_map = dict(POSITION_COLUMNS)
    added: list[str] = []
    conn = sqlite3.connect(str(db), timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
        for col in missing:
            # 列名与 DDL 均来自 POSITION_COLUMNS（代码内常量，非外部输入）
            conn.execute(f"ALTER TABLE cards ADD COLUMN {ddl_map[col]}")
            added.append(col)
        rows = conn.execute("PRAGMA table_info(cards)").fetchall()
        have = {r["name"] for r in rows}
        after = {col: (col in have) for col, _ in POSITION_COLUMNS}
        # 存量行取值自检：未摆过的卡坐标必须仍是 NULL、pinned 必须是 0
        nullxy = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE pos_x IS NULL AND pos_y IS NULL"
        ).fetchone()[0]
        zero_pin = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE COALESCE(pinned, 0) = 0"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        return {"added": added, "after": after, "rows": total,
                "rows_pos_null": nullxy, "rows_pinned_0": zero_pin}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="works 树（缺省＝本仓 works/；可先在副本上演练）")
    ap.add_argument("--apply", action="store_true", help="真加列（缺省 dry-run）")
    args = ap.parse_args()

    target = (Path(args.target).resolve() if args.target else ROOT / "works").resolve()
    if not target.is_dir():
        print(f"[migrate] --target 必须指向 works 树: {target}", file=sys.stderr)
        return 2

    dbs = sorted(target.glob("work_*/cards.db"))
    if not dbs:
        print(f"[migrate] 目标树下没有 work_*/cards.db: {target}", file=sys.stderr)
        return 2

    # ---- 扫描＋计划（内存里，零写入）----
    plan: list[dict] = []
    failures: list[str] = []
    for db in dbs:
        try:
            plan.append(_inspect(db))
        except Exception as exc:  # noqa: BLE001 any-fail-abort
            item = {"work": db.parent.name, "db": str(db), "action": "FAIL",
                    "error": repr(exc)}
            plan.append(item)
            failures.append(f"{db.parent.name}: {exc!r}")

    if failures:
        for it in plan:
            print(json.dumps(it, ensure_ascii=False))
        print(f"[migrate] FAIL {len(failures)} 个库，零 ALTER（any-fail-abort）", file=sys.stderr)
        for f in failures:
            print(f"  FAIL {f}", file=sys.stderr)
        return 1

    if args.apply:
        for it in plan:
            if it.get("action") != "alter":
                continue
            db = Path(it["db"])
            try:
                it["apply"] = _apply_one(db, it["missing"])
                it["action"] = "altered"
            except Exception as exc:  # noqa: BLE001
                it["action"] = "FAIL"
                it["error"] = repr(exc)
                failures.append(f"{it['work']}: {exc!r}")

    mode = "APPLY" if args.apply else "DRY-RUN"
    todo = sum(1 for it in plan if it.get("action") == "alter")
    for it in plan:
        print(json.dumps(it, ensure_ascii=False))
    print(f"[migrate] {mode} 完成：库 {len(plan)} 个，待加列/已加列 {todo} 个")

    if failures:
        print(f"[migrate] FAIL {len(failures)} 个库在写入阶段出错（已加的列不回滚，"
              f"重跑幂等可续）", file=sys.stderr)
        for f in failures:
            print(f"  FAIL {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
