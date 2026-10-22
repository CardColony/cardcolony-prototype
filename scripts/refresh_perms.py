# -*- coding: utf-8 -*-
"""权限刷新（scripts/refresh_perms.py · 权限底座重做 2026-10-12）。

**用途**：把卡上固化的 ``perms`` / ``perms_version`` 对齐到当前
``config/permissions.json``。

**为什么要有它**（用户裁定 4）：权限在**建卡时算一次写死进卡**，运行时只读卡上
的 perms、不再查配置。类型不会变，但「这个类型配哪些权限」会随迭代变——
所以改配置的人要做两件事：

    ① 把 config/permissions.json 的 version +1；
    ② 跑本脚本。

脚本按 ``perms_version`` 判落后：只重写 version 落后（或缺字段）的卡，
version 已对齐的卡直接跳过 —— **幂等，重复跑无副作用**。

存量卡此前从未固化过 perms（字段全缺），所以对它们来说第一次跑＝回填。

用法：
    python scripts/refresh_perms.py                  # 刷新全部 work
    python scripts/refresh_perms.py --work work_002  # 只刷一个 work
    python scripts/refresh_perms.py --dry-run        # 只报会改哪些，不动盘

约定：
- **只写 perms / perms_version 两个字段**，正文与其它 front matter 一概不动；
- 走 card_store.update_card（单一写门），逐张留修改历史与快照；
- 不删任何文件；遇到单卡失败计 skipped 并继续，不中断整批。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.permissions import policy  # noqa: E402
from src.storage import card_store  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("refresh_perms")

HISTORY_NOTE = "[权限刷新] perms → v{version}"


def _list_works() -> list[str]:
    """列出全部 work_id（按目录名排序）。"""
    if not card_store.WORKS_DIR.exists():
        return []
    return sorted(p.name for p in card_store.WORKS_DIR.iterdir()
                  if p.is_dir() and p.name.startswith("work_"))


async def refresh_one_work(work_id: str, target_version: int,
                           apply: bool) -> dict[str, Any]:
    """刷一个 work，返回 {total, updated, skipped, unchanged, failed}。"""
    stats = {"total": 0, "updated": 0, "unchanged": 0, "failed": 0}
    cards = card_store.iter_card_metas(work_id)
    if not cards:
        return stats
    # 工牌继承链：project_class 只挂根卡，同树子卡从根卡继承
    # （与 card_store.create_card 的取法逐字同口径）
    root_pc = _root_project_class_of(work_id)
    for card_id in sorted(cards):
        stats["total"] += 1
        meta = cards[card_id]
        # [自动卡刷新口径 2026-10-22 · 修] 原注释写的是 2026-10-17 的口径
        # （auto_mode ⇒ auto_perms() 全开），但 **2026-10-19 实验 permexp 已撤销**
        # 那条：card_store.create_card 里现在是「与普通卡同一份类型分权
        # （compute_perms），auto_mode 只保留自动续轮语义」。
        # 脚本却还按旧口径跳过 ⇒ 所有自动卡（几乎全部 coder / 子卡）**永远停在
        # 建卡那天的权限版本**，配置怎么改都不生效。实测 work_018 的 8 张 coder
        # 全是 auto=1 / v6，配置已到 v12，它们连 send_card_message 都拿不到。
        # ⇒ 只保留「旧口径写成全开（v-1）」的卡不动（不缩小既有授权），其余照常刷新。
        if bool(meta.get("auto_mode")) \
                and meta.get(policy.PERM_VERSION_KEY) == policy.AUTO_PERMS_VERSION:
            stats.setdefault("auto_skipped", 0)
            stats["auto_skipped"] += 1
            logger.info("[refresh] 跳过旧全开口径的自动卡 %s/%s（perms_version=-1）",
                        work_id, card_id)
            continue
        project_class = str(meta.get("project_class") or "") or root_pc
        card_type = str(meta.get("card_type", "") or "")
        perms, version = policy.compute_perms(project_class, card_type)
        cur_perms = meta.get(policy.PERM_KEY)
        cur_version = meta.get(policy.PERM_VERSION_KEY)
        if cur_version == version and cur_perms == perms:
            stats["unchanged"] += 1
            continue
        if not apply:
            stats["updated"] += 1   # dry-run：计入「待改」
            logger.info(
                "[dry-run] %s/%s type=%s/%s → perms=%s v%s（旧 %s）",
                work_id, card_id, project_class, card_type,
                perms, version, cur_version,
            )
            continue
        try:
            res = await card_store.update_card(
                work_id, card_id,
                {policy.PERM_KEY: perms, policy.PERM_VERSION_KEY: version},
                HISTORY_NOTE.format(version=version),
            )
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            logger.error("刷新失败 %s/%s: %s", work_id, card_id, exc)
            continue
        if isinstance(res, dict) and res.get("error"):
            stats["failed"] += 1
            logger.error("刷新失败 %s/%s: %s", work_id, card_id, res["error"])
            continue
        stats["updated"] += 1
    return stats


def _root_project_class_of(work_id: str) -> str:
    """本树根卡的 project_class（复用 card_store 的唯一口径）。"""
    try:
        return card_store._root_project_class(work_id)
    except Exception:  # noqa: BLE001
        return ""


async def main_async(args: argparse.Namespace) -> int:
    version = policy.config_version()
    works = [args.work] if args.work else _list_works()
    if not works:
        print(f"没有可刷的 work（WORKS_DIR={card_store.WORKS_DIR}）")
        return 1

    print(f"权限配置 version = {version}（{policy.PERMISSIONS_PATH}）")
    print(f"目标 work 数 = {len(works)}；模式 = {'dry-run' if args.dry_run else '写入'}")
    totals = {"total": 0, "updated": 0, "unchanged": 0, "failed": 0}
    for work_id in works:
        st = await refresh_one_work(work_id, version, apply=not args.dry_run)
        for k in totals:
            totals[k] += st[k]
        print(
            f"  {work_id}: 卡 {st['total']}｜改 {st['updated']}｜"
            f"无需动 {st['unchanged']}｜失败 {st['failed']}"
        )
    print(
        f"合计：卡 {totals['total']}｜改 {'待改' if args.dry_run else '已改'} "
        f"{totals['updated']}｜无需动 {totals['unchanged']}｜失败 {totals['failed']}"
    )
    return 0 if totals["failed"] == 0 else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="按 permissions.json 刷新卡上固化的 perms")
    ap.add_argument("--work", default="", help="只刷指定 work_id，缺省刷全部")
    ap.add_argument("--dry-run", action="store_true", help="只报会改哪些卡，不落盘")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
