# -*- coding: utf-8 -*-
"""5713 存量卡 perms 回溯 · 人工迁移脚本（card_036 交付物 2）。

**用途**：对指定 work 做**一次性、显式、离线**的 perms 回填——把存量卡
固化在卡上的 ``perms`` / ``perms_version`` 对齐到**当前**
``config/permissions.json``（v9：project/sub_project/topic 已摘
tools_file/tools_web）。核心逻辑在 ``src/permissions/backfill.py`` 的
``recompute_perms_for_work``，本脚本只做命令行壳＋**apply 前自动备份**。

用法（**人工执行**）：
    python verify/5713/backfill_perms.py --work work_018            # dry-run（缺省）
    python verify/5713/backfill_perms.py --work D:\\...\\works\\work_018
    python verify/5713/backfill_perms.py --work work_018 --apply    # 真写（先自动备份）

**硬约束（任务书）**：
- 缺省 ``--dry-run``：只报 diff，**一个字都不写库**；
- ``--apply`` 前**自动备份**该 work 的 cards.db——用 SQLite ``Connection.backup``
  API（照 card_028 用过、verify/5713/c28_function_migration_check.py L77 的
  先例），自动合并 WAL、页级一致；备份路径打印到 stdout，失败即中止、不 apply；
- **禁止在运行中的实例（127.0.0.1:5713）上执行 --apply**；验证一律在自建
  沙箱 work 副本上做；
- 备份落在 ``<WORKS_DIR>/_backup_perms/<work_id>/``（works 根下，不进 work
  目录，避免被数据快照/画布扫描吞掉）。

**[card_039 返工 · failed 可见性]**
- ``report.failed``（pc 解析失败 / 目标为空集而跳过的卡）**逐条打印**，不只留在
  JSON；
- **只要 ``failed`` 非空，``--apply`` 一律拒绝执行**（先纯算检查、失败即返回
  非零退出码 2），除非显式加 ``--force``（跳过 failed 卡、只回填可解析的卡）。
  理由：静默跳过等于下次还以为全好了——failed 必须被人看见。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def backup_cards_db(work_id: str) -> Path:
    """SQLite backup API 在线备份该 work 的 cards.db（自动合并 WAL）。"""
    from src.storage import card_db, card_store

    db = card_db.db_path(work_id)
    if not db.exists():
        raise FileNotFoundError(f"cards.db 不存在: {db}")
    ts = time.strftime("%Y%m%dT%H%M%S")
    dest_dir = card_store.WORKS_DIR / "_backup_perms" / work_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"cards.db.{ts}"
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # 页级一致、自动合并 -wal 内容
    finally:
        dst.close()
        src.close()
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"备份失败（目标为空/不存在）: {dest}")
    return dest


def _fmt_switches(sw: list[str]) -> str:
    return ", ".join(sw) if sw else "-"


def print_diff_table(report: dict) -> None:
    rows = report["changed"]
    if not rows:
        print("  （无待改卡）")
        return
    header = (f"{'card_id':<10} {'card_type':<14} {'pc':<14} {'auto':<5} "
              f"{'ver 旧→新':<12} {'removed（去掉）':<40} {'added（新增）'}")
    print("  " + header)
    print("  " + "-" * len(header))
    for r in rows:
        ver = f"{r['cur_version']}→{r['target_version']}"
        print(f"  {r['card_id']:<10} {r['card_type']:<14} {r['project_class']:<14} "
              f"{r['auto_mode']:<5} {ver:<12} {_fmt_switches(r['removed']):<40} "
              f"{_fmt_switches(r['added'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description="5713 存量卡 perms 回溯（人工执行）")
    ap.add_argument("--work", required=True,
                    help="work id（work_018）或 work 目录路径")
    ap.add_argument("--works-dir", default="",
                    help="works 根目录（缺省＝本仓库 works）；用于对指定沙箱/数据"
                         "目录跑（如 5713_sandbox 的 works）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报 diff、不写库（缺省即 dry-run）")
    ap.add_argument("--apply", action="store_true",
                    help="真写：apply 前自动备份 cards.db，再逐卡走单一写门回填")
    ap.add_argument("--force", action="store_true",
                    help="[card_039] failed 非空时仍强制 apply（跳过 failed 卡，"
                         "只回填可解析的卡）——默认 failed 非空即拒绝 apply")
    args = ap.parse_args()

    if args.dry_run and args.apply:
        print("[backfill] --dry-run 与 --apply 互斥，请二选一", file=sys.stderr)
        return 2

    from src.permissions import backfill
    from src.storage import card_store, card_db, work_store

    if args.works_dir:
        card_store.WORKS_DIR = Path(args.works_dir)
        work_store.WORKS_DIR = Path(args.works_dir)  # work_card_id_of 读 work.json 也要同一根
    card_db.reset_caches()  # 清掉可能指向旧库路径的缓存连接

    work_id = backfill.resolve_work_id(args.work)
    apply = args.apply
    mode = "APPLY（写库）" if apply else "DRY-RUN（只报不写）"
    print(f"[backfill] work={work_id} 模式={mode}")
    print(f"[backfill] WORKS_DIR={card_store.WORKS_DIR}")

    # [card_039 返工] 先纯算一次 report，检查 failed：
    # failed 非空 ⇒ 默认拒绝 --apply（静默跳过等于下次还以为全好了），
    # 除非显式 --force（跳过 failed 卡、只回填可解析的卡）。
    pre = backfill.compute_perms_report(work_id)
    if apply and pre["failed"] and not args.force:
        print("[backfill] 存在无法解析的卡（pc 解析失败 / 目标为空集），"
              "拒绝 --apply：", file=sys.stderr)
        for f in pre["failed"]:
            print(f"  {f['card_id']}: {f['error']}", file=sys.stderr)
        print("[backfill] 如需强制回填其余卡，请显式加 --force", file=sys.stderr)
        return 2

    if apply:
        try:
            bak = backup_cards_db(work_id)
            print(f"[backfill] 已自动备份 cards.db（含 WAL 合并）-> {bak}")
        except Exception as exc:  # noqa: BLE001 备份失败即中止，绝不 apply
            print(f"[backfill] 备份失败，中止 apply: {exc}", file=sys.stderr)
            return 2

    report = backfill.recompute_perms_for_work(work_id, dry_run=not apply)
    print(f"[backfill] 配置 version={report['config_version']} 卡总数="
          f"{report['total']} 待改={len(report['changed'])} "
          f"无需动={report['unchanged']} 已写={report['applied']}")

    if report["failed"]:
        print("[backfill] 失败卡（已跳过、未写库）：")
        for f in report["failed"]:
            print(f"  {f['card_id']}: {f['error']}")

    print("[backfill] 逐卡 diff：")
    print_diff_table(report)

    if apply:
        print(f"[backfill] apply 完成：{report['applied']} 张卡已写 "
              f"perms/perms_version；备份在 _backup_perms/{work_id}/")
    else:
        print("[backfill] dry-run 完成：未写任何库。确认 diff 后加 --apply 执行回填。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
