# -*- coding: utf-8 -*-
"""【已废弃 · 2026-10-15 · 勿再执行】canvas.json → layout.json 用户坐标迁移脚本。

**已废弃（2026-10-15）**：位置数据已并入 cards 表（``pos_x``/``pos_y``/
``pinned`` 三列，见 ``src/storage/card_db.py`` 的 ``POSITION_COLUMNS``），
``layout.json`` 这条存放点已整体退役——**本脚本使命终结**。

它当年要搬的两端现在都不存在了：
- 源端 ``canvas.json.positions``：已清空，且 load/rebuild 路径不再写它；
- 目标端 ``layout.json``：不再被任何代码路径创建（``load_layout``/
  ``save_layout`` 两个函数已删）。

**执行它只会造出一个没人读、没人认领的孤儿文件**——恰好是本次重构要消灭的
那类"第二个真相来源"。故：留档备查，不删历史（要查当时为什么这么设计），
但**不要再运行**。真要改位置数据，去 cards 表；
加列/补列走 ``scripts/migrate_cards_add_position_columns.py``。

（以下为当时的原始实现，逐字保留，仅供追溯。）

────────────────────────────────────────────────────────────────────────
canvas.json → layout.json 用户坐标一次性迁移脚本（画布第二步 D4）。

职责：把**指定 works 树**（必须是副本或部署窗口内已停写的生产树）下各
``work_*/canvas.json`` 的 ``positions``（用户钉住坐标，唯一需要迁移的用户数据）
搬进同目录 ``layout.json``（``{"version": 1, "positions": {...}}``）。

铁律（沿 ``migrate_sessions_v1_to_v2.py`` 先例）：
- **默认 dry-run 只出清单，``--apply`` 才写盘**（dry-run 零落盘）；
- **any-fail-abort**：先全部扫描＋计划于内存，任何一份失败＝零写入、非零退出、
  逐 work 清单里标出失败原因；
- **幂等可重跑**：layout.json 已含的卡坐标跳过（只补缺失项）；无坐标的 work
  不建空 layout.json（稀疏原则，L6）；
- **源 canvas.json 一个字节不动**（只读；回退＝删 layout.json 即完全回退）；
- 本脚本**不 import 生产默认路径**——目标树必须显式 ``--target`` 给出；
  纯 pathlib/json 处理，不 import src（迁移期不依赖运行时代码形态）。

用法：
    python scripts/migrate_canvas_positions.py --target <works树> [--apply]

输出：stdout 逐 work 清单（JSON 行）＋ 汇总行；退出码 0=成功 1=有失败 2=参数错。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LAYOUT_VERSION = 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="works 树（副本或停写的生产树）")
    ap.add_argument("--apply", action="store_true", help="真写盘（缺省 dry-run）")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not target.exists() or not list(target.glob("work_*")):
        print(f"[migrate] --target 必须指向 works 树: {target}", file=sys.stderr)
        return 2

    plan: list[tuple[dict, Path | None, dict | None]] = []
    failures: list[str] = []

    for cpath in sorted(target.glob("work_*/canvas.json")):
        work_id = cpath.parent.name
        lpath = cpath.parent / "layout.json"
        item: dict = {"work": work_id, "source_file": cpath.name}
        try:
            src = json.loads(cpath.read_text(encoding="utf-8"))
            positions = src.get("positions") or {}
            if not isinstance(positions, dict):
                raise ValueError(f"canvas.json positions 形态非法: {type(positions)!r}")
            # 幂等：layout.json 已有的卡坐标跳过，只补缺失项（重跑结果不变）
            existing: dict = {}
            if lpath.exists():
                try:
                    doc = json.loads(lpath.read_text(encoding="utf-8"))
                    if isinstance(doc, dict) and isinstance(doc.get("positions"), dict):
                        existing = doc["positions"]
                except Exception as exc:  # noqa: BLE001 已有文件读不出＝按空处理（与 load_layout 同纪律）
                    print(f"[migrate] WARN {work_id} 既有 layout.json 读取失败按空处理: {exc!r}",
                          file=sys.stderr)
            pending = {k: v for k, v in positions.items() if k not in existing}
            item["source_positions"] = len(positions)
            item["to_migrate"] = len(pending)
            item["already_migrated"] = len(positions) - len(pending)
            if not pending:
                item["action"] = "skip" if positions else "none"
                plan.append((item, None, None))
                continue
            item["action"] = "migrate"
            merged = dict(existing)
            merged.update(pending)
            plan.append((item, lpath, {"version": LAYOUT_VERSION, "positions": merged}))
        except Exception as exc:  # noqa: BLE001 any-fail-abort
            item["action"] = "FAIL"
            item["error"] = repr(exc)
            failures.append(f"{work_id}: {exc!r}")
            plan.append((item, None, None))

    # any-fail-abort：任一失败 → 零写入、非零退出
    if failures:
        for it, _, _ in plan:
            print(json.dumps(it, ensure_ascii=False))
        print(f"[migrate] FAIL {len(failures)} 项，零写入（any-fail-abort）", file=sys.stderr)
        for f in failures:
            print(f"  FAIL {f}", file=sys.stderr)
        return 1

    if args.apply:
        for it, lpath, doc in plan:
            if doc is None:
                continue
            tmp = lpath.with_name(lpath.name + ".migrating")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(lpath)
            it["written"] = True

    total_migrate = sum(it.get("to_migrate", 0) for it, _, _ in plan)
    mode = "APPLY" if args.apply else "DRY-RUN"
    for it, _, _ in plan:
        print(json.dumps(it, ensure_ascii=False))
    print(f"[migrate] {mode} 完成：work {len(plan)} 个，应迁移坐标 {total_migrate} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
