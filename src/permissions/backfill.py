# -*- coding: utf-8 -*-
"""存量卡 perms 回溯（backfill · card_036 交付物 1）。

**为什么需要**：perms 在**建卡时算一次写死进卡**（``policy.compute_perms``，
见 ``card_store.create_card`` 建卡路径），运行时只读卡上固化值、不现查配置
（``policy.has_perm``，[裁定 4]）。配置演进（``config/permissions.json``
version 1→9，project/sub_project/topic 三类管理卡摘掉 tools_file/tools_web）
后，**存量卡的 perms 仍是旧版本配置的快照**——仍握着写文件/跑命令的执行权；
而既有 ``scripts/refresh_perms.py`` 按 2026-10-17 旧口径**跳过 auto_mode=1
的卡**（2026-10-19 permexp 之后 auto 卡建卡/切换已改回类型分权、不再全开），
双重缺口导致这些存量卡永远停在全开/旧快照。

本模块提供**显式、离线、人工触发**的一次性回填：按**当前**配置重算每张卡
应有的 ``(perms, perms_version)``，与现存比对产出 diff；``dry_run=True``
**一个字都不写库**。

**边界（任务书硬约束）**：
- 不改 ``policy.compute_perms()`` 的默认行为——回填只改**存量卡固化值**，
  运行时读卡语义零变化；
- 不新增「按卡更新 perms」的存储原语——``card_store.update_card`` 的
  ``_UPDATABLE_META_KEYS`` 白名单本就含 ``perms``/``perms_version``，走单一
  写门（留修改历史、更新 hash），是现成的最小原语；
- auto 卡按**当前建卡语义**（类型分权）处理，不跳过——与
  ``card_store.create_card``（L953-956「auto_mode 只保留自动续轮语义」）和
  ``update_card`` auto 切换（L1169-1172「0/1 一律类型分权重算」）同一口径；
- 只写 ``perms`` / ``perms_version`` 两字段，title/parent/auto_mode/goal/
  body 等一概不动。

**三个入口**（调用方按上下文选）：
- ``compute_perms_report(work_id_or_path)``：**同步纯计算**，永不写库（含逐卡
  diff）。适合先看 diff；
- ``async apply_perms_report(report)``：**异步应用**，逐卡走单一写门。适合
  事件循环内（pytest-asyncio 测试）await；
- ``recompute_perms_for_work(work_id_or_path, dry_run=True)``：**任务书签名
  的同步总入口**。dry_run=True＝只算不写；dry_run=False＝算完再写——
  无运行中事件循环时直接 ``asyncio.run``；有运行中事件循环时（如 pytest
  asyncio_mode=auto 的测试）改在**独立线程**跑独立 loop，避免嵌套。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("permissions.backfill")

#: apply 时的修改历史留痕行
HISTORY_NOTE = "[perms 回填] 按当前 permissions.json 对齐（backfill）"


class TargetPermsUnresolvable(Exception):
    """目标权限解析失败——**fail-safe**：pc 解析为空，或 compute_perms 返回空集。

    空集在 ``compute_perms`` 里的语义是「该 project_class × card_type 未配置 ⇒
    全拒」，**不是**「应回填成空权限」。把"查不到"当成"目标为空"是这一类 bug 的
    通病（card_039 返工实证：pc 两路都空时旧代码生成『去掉全部开关』的 diff，
    一旦 --apply 就把该卡权限清空、失去所有工具能力）。凡解析不出目标，必须
    **跳过该卡并记入 report.failed**，绝不生成 diff。
    """


def resolve_work_id(work_id_or_path: str) -> str:
    """把 ``work_018`` 或 ``<路径>/work_018`` 归一成 work_id（目录名）。

    校验：目标 work 目录存在（含 cards.db），否则 ValueError。只读不写。
    """
    from ..storage import card_db, card_store

    raw = str(work_id_or_path or "").strip()
    if not raw:
        raise ValueError("work 不能为空（传 work_id 或 work 目录路径）")
    p = Path(raw)
    if p.name.startswith("work_"):
        work_id = p.name
        if p.is_dir():
            # 传的是路径：直接校验该路径（不要求等于 WORKS_DIR 下）。
            # [库搬出施工目录 2026-11-07] cards.db 可能在 data/{wid}/（新位）
            # 也可能还没搬（旧位）——两处都算「有库」。
            if not ((p / card_db._DB_NAME).exists()
                    or card_db.db_path(work_id).exists()):
                raise ValueError(f"work 目录不存在或缺少 cards.db: {p}")
            return work_id
        # 传的是纯 work_id：校验库在（新位 data/ 或旧位 works/ 皆可）
        work_dir = card_store.WORKS_DIR / work_id
        if not work_dir.is_dir() or not (
                (work_dir / card_db._DB_NAME).exists()
                or card_db.db_path(work_id).exists()):
            raise ValueError(
                f"work {work_id} 不存在或缺少 cards.db: {work_dir}"
            )
        return work_id
    raise ValueError(f"无法识别 work 标识: {raw!r}（须为 work_NNN 或以 work_NNN 结尾的路径）")


def _target_perms(work_id: str, meta: dict[str, Any],
                  root_project_class: str) -> tuple[list[str], int]:
    """按当前配置算这张卡应有的 ``(perms, perms_version)``。

    与 ``card_store.create_card`` / ``update_card`` 的取法逐字同口径：
    project_class 取卡上值、缺省继承根卡；card_type 用卡上值；一律走
    ``policy.compute_perms``（**自动模式卡也不例外**——2026-10-19 后
    auto 不再全开，见模块 docstring）。

    **fail-safe（card_039 返工）**：下列任一情况**抛 ``TargetPermsUnresolvable``**，
    由调用方跳过该卡并记入 report 的 ``failed``，**绝不生成 diff**：
      - ``pc`` 解析为空（卡上没有、根卡也取不到）；
      - ``policy.compute_perms(...)`` 返回的 perms 为**空列表**。
    理由：空集在 ``compute_perms`` 里是「该 project_class × card_type 未配置 ⇒
    全拒」，不是「应回填成空权限」；把"查不到"当成"目标为空"会清空该卡全部权限。
    """
    from . import policy

    pc = str(meta.get("project_class") or "") or root_project_class
    card_type = str(meta.get("card_type") or "") or "线索"  # 与 _meta_from_row 兜底同口径
    if not pc:
        raise TargetPermsUnresolvable(
            f"pc 解析失败：卡上无 project_class，根卡也取不到"
            f"（root_pc={root_project_class!r}）")
    perms, version = policy.compute_perms(pc, card_type)
    if not perms:
        raise TargetPermsUnresolvable(
            f"目标权限为空集：compute_perms({pc!r}, {card_type!r}) 返回空"
            f"（该组合未配置 ⇒ 全拒，不能回填成空权限）")
    return perms, version


def _root_project_class_with_fallback(work_id: str,
                                      cards: dict[str, dict[str, Any]]) -> str:
    """取本树根卡的 project_class；``card_store._root_project_class`` 取不到时兜底。

    **为什么加兜底（card_039 返工）**：``card_store._root_project_class`` 的实现是
    「读 work.json 的 work_card_id → 读该卡 meta 的 project_class」——一旦
    work.json 缺失/损坏/work_card_id 指向不存在的卡，它就返回空串，**即使卡表里
    根卡明明带着 project_class**（实测沙箱：card_001 有 project_class='rd_project'，
    但 _root_project_class 返回 ''）。兜底直接从卡表找根卡（card_type='project'
    或 parent 为空）读它的 project_class 字段，让 pc 在「work.json 不可靠」时仍能
    解析出来。两路都取不到才返回空串，交由 _target_perms 的 fail-safe 记 failed。
    """
    from ..storage import card_store

    pc = card_store._root_project_class(work_id)
    if pc:
        return pc
    for cid, m in (cards or {}).items():
        if str(m.get("card_type") or "") == "project" or not m.get("parent"):
            v = str(m.get("project_class") or "")
            if v:
                return v
    return ""


def compute_perms_report(work_id_or_path: str) -> dict[str, Any]:
    """**同步纯计算**：遍历一个 work 的全部卡，按当前配置重算 perms 并与
    现存比对，产出 report。**永不写库**（只经 ``iter_card_metas`` 只读全表）。

    report 结构：
        {
          "work_id": str, "config_version": int,
          "total": int, "unchanged": int,
          "changed": [
            {"card_id", "card_type", "project_class", "auto_mode",
             "cur_perms", "target_perms", "added", "removed",
             "cur_version", "target_version"},
            ...
          ],
          "failed": [{"card_id", "error"}],
        }

    ``added`` = 目标有、现存无的开关；``removed`` = 现存有、目标无的开关
    （集合差，输出排序稳定）。现存 perms 为空/未固化 ⇒ 从空集起算。

    ``failed``（card_039 返工新增语义）：pc 解析失败（卡上没有、根卡也取不到）
    或 ``compute_perms`` 返回空列表（该组合未配置 ⇒ 全拒）的卡——**跳过、不生成
    diff**。空集不是「应回填成空权限」，把"查不到"当成"目标为空"会清空该卡全部
    权限。调用方（verify/5713/backfill_perms.py）必须在 failed 非空时拒绝 --apply。
    """
    from ..storage import card_store
    from . import policy

    work_id = resolve_work_id(work_id_or_path)

    cards = card_store.iter_card_metas(work_id)
    if not cards:
        raise ValueError(f"work {work_id} 没有可读的卡（iter_card_metas 为空）")

    root_pc = _root_project_class_with_fallback(work_id, cards)

    report: dict[str, Any] = {
        "work_id": work_id,
        "config_version": policy.config_version(),
        "total": len(cards),
        "unchanged": 0,
        "changed": [],
        "failed": [],
    }

    for card_id in sorted(cards):
        meta = cards[card_id]
        cur_perms = meta.get(policy.PERM_KEY)
        cur_perms = list(cur_perms) if isinstance(cur_perms, (list, tuple, set)) else []
        cur_version = meta.get(policy.PERM_VERSION_KEY)
        try:
            cur_version = int(cur_version) if cur_version is not None else None
        except (TypeError, ValueError):
            cur_version = None

        # [card_039 返工] fail-safe：pc 解析失败/目标为空集 ⇒ 跳过该卡记 failed，
        # 绝不生成 diff（空集语义是「全拒」，不是「应回填成空权限」）。
        try:
            target_perms, target_version = _target_perms(work_id, meta, root_pc)
        except TargetPermsUnresolvable as exc:
            report["failed"].append({"card_id": card_id, "error": str(exc)})
            continue

        cur_set = {str(s) for s in cur_perms}
        target_set = {str(s) for s in target_perms}
        if cur_set == target_set and cur_version == target_version:
            report["unchanged"] += 1
            continue

        report["changed"].append({
            "card_id": card_id,
            "card_type": str(meta.get("card_type") or ""),
            "project_class": str(meta.get("project_class") or "") or root_pc,
            "auto_mode": 1 if bool(meta.get("auto_mode")) else 0,
            "cur_perms": sorted(cur_perms),
            "target_perms": list(target_perms),
            "added": sorted(target_set - cur_set),
            "removed": sorted(cur_set - target_set),
            "cur_version": cur_version,
            "target_version": target_version,
        })
    return report


async def apply_perms_report(report: dict[str, Any]) -> int:
    """**异步应用**：对 report 里每张待改卡走 ``card_store.update_card``
    单一写门写 ``perms`` / ``perms_version``。返回成功写入的卡数；
    单卡失败记 logger 并继续（不中断整批，与 ``scripts/refresh_perms.py``
    失败语义一致）。只写两字段，其它不动。
    """
    from ..storage import card_store
    from . import policy

    done = 0
    for item in report["changed"]:
        card_id = item["card_id"]
        try:
            res = await card_store.update_card(
                report["work_id"], card_id,
                {policy.PERM_KEY: item["target_perms"],
                 policy.PERM_VERSION_KEY: item["target_version"]},
                HISTORY_NOTE,
            )
        except Exception as exc:  # noqa: BLE001 单卡失败不阻断整批
            logger.error("[backfill] 写失败 %s/%s: %s",
                         report["work_id"], card_id, exc)
            report.setdefault("failed", []).append(
                {"card_id": card_id, "error": str(exc)})
            continue
        if isinstance(res, dict) and res.get("error"):
            logger.error("[backfill] 写失败 %s/%s: %s",
                         report["work_id"], card_id, res["error"])
            report.setdefault("failed", []).append(
                {"card_id": card_id, "error": res["error"]})
            continue
        done += 1
    return done


def recompute_perms_for_work(work_id_or_path: str,
                             dry_run: bool = True) -> dict[str, Any]:
    """**任务书签名 · 同步总入口**。

    ``dry_run=True``（缺省）：只算 diff，**一个字都不写库**。
    ``dry_run=False``：算完再写——无运行中事件循环时直接
    ``asyncio.run(apply_perms_report(...))``；有运行中事件循环时
    （pytest asyncio_mode=auto 的测试用例）改在**独立线程**跑独立 loop，
    避免 ``asyncio.run`` 嵌套报错。返回值在 report 上补 ``dry_run`` /
    ``applied`` 两键。
    """
    report = compute_perms_report(work_id_or_path)
    report["dry_run"] = bool(dry_run)
    if dry_run or not report["changed"]:
        report["applied"] = 0
        return report

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        report["applied"] = asyncio.run(apply_perms_report(report))
    else:
        report["applied"] = _apply_in_thread(report)
    return report


def _apply_in_thread(report: dict[str, Any]) -> int:
    """有运行中事件循环时：起独立线程跑独立 loop 执行 apply，同步等待结果。

    线程池里 ``asyncio.run`` 会建一条全新 loop，与调用方线程的 loop 互不
    嵌套；``update_card`` 内部 ``asyncio.to_thread`` 走线程池，card_db 连接
    按库路径/线程隔离，跨线程写同一 work 库由 card_db 的锁串行化，安全。
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_apply_in_new_loop, report)
        return fut.result()


def _apply_in_new_loop(report: dict[str, Any]) -> int:
    return asyncio.run(apply_perms_report(report))
