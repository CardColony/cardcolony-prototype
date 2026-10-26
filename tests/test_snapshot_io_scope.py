# -*- coding: utf-8 -*-
"""快照机制不再「疯狂读写硬盘」的回归网（[P26 · 2026-10-26]）

## 线上症状（实测，不是推测）

· 服务 5700 **在监听但不响应**：请求 24 秒连不上（`/api/health` 也失败）
· 磁盘 `%DiskTime 1122%`
· 服务端日志被**同一个原因**刷屏：
  `快照跳过（可变状态未变）: work_028 … work_050 原因=[snapshot][work_027] shadow delete: card_080`
  ⇒ **一次影子删除**触发了对**所有 work** 的逐个指纹扫描
· 用户原话：「不是一���浏览器卡，是不同浏览器都卡死 ⇒ 服务层面的问题」——**判断正确**

## 三条病根（都修了）

| 编号 | 病根 | 修法 |
|---|---|---|
| R1 | **14 个调用点全都没传 `work_id`** ⇒ 每次写操作都扫**所有** work | 逐个补上 `work_id`（动哪拍哪） |
| R2 | 列目录用 `rglob("*")` ⇒ 把 events/sessions 的**每个文件**都枚举一遍（1666 个），而它们本来就不参与指纹 | 遍历时**剪掉留痕目录**；留痕改为**按需**取（只有日备/manifest 要） |
| R3 | 「强制立刻拍」的分支**绕过合并机制** ⇒ 每次含 delete 的写都新开一个任务排队等 `_SNAPSHOT_LOCK` ⇒ **占满默认线程池** ⇒ 服务的其它 `to_thread`（**timeline**！）全排队 ⇒ 全部请求不响应 | 强制分支也**登记进 `_snap_tasks`** ⇒ 同一 key 同时最多一个任务 |

## 本网钉住的不变量

| 编号 | 不变量 |
|---|---|
| P1 | 每个 `data_snapshot.schedule(...)` 调用点都带 `work_id`（AST 扫全仓） |
| P2 | 强制分支必须走 `_snap_tasks` 合并（不许再无条件 create_task） |
| P3 | 列目录必须**剪掉**留痕目录（`_LOG_DIR_NAMES`） |
| P4 | 日备仍必须**含留痕**（恢复点完整）——剪枝不许漏掉它 |

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _schedule_call_sites() -> list[tuple[str, int, bool]]:
    """全仓扫 data_snapshot.schedule(...) 的调用点：[(文件, 行号, 是否带 work_id)]"""
    out = []
    for p in SRC.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "schedule"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "data_snapshot"):
                has = len(n.args) >= 2 or any(k.arg == "work_id" for k in n.keywords)
                out.append((str(p.relative_to(ROOT)), n.lineno, has))
    return out


def test_p1_every_schedule_call_passes_work_id():
    """P1：每个调用点都带 work_id（否则每次写都扫所有 work）。"""
    sites = _schedule_call_sites()
    assert sites, "没找到 data_snapshot.schedule 调用点"
    bad = [f"{f}:{ln}" for f, ln, has in sites if not has]
    assert not bad, (
        "这些 schedule 调用点没传 work_id ⇒ 每次写操作都会扫**所有** work 的状态文件"
        f"（线上实测：35+ work ⇒ 磁盘打满 ⇒ 服务不响应）：{bad}")


def test_p2_force_branch_uses_the_coalescing_registry():
    """P2：强制分支必须登记进 _snap_tasks（否则任务无限堆、占满线程池）。"""
    from src.storage import data_snapshot
    src = inspect.getsource(data_snapshot.schedule)
    force_at = src.find("if _force_now(reason):")
    assert force_at >= 0, "schedule 里应有强制分支"
    seg = src[force_at:force_at + 2000]
    assert "_snap_tasks[key] = " in seg, (
        "强制分支必须把任务登记进 _snap_tasks（否则每次含 delete 的写都新开一个任务"
        "⇒ 排队等 _SNAPSHOT_LOCK ⇒ 占满默认线程池 ⇒ timeline 等 to_thread 全排队"
        "⇒ 服务不响应）")
    assert "if key in _snap_tasks:" in seg, "强制分支应先看该 key 是否已有任务在跑"


def test_p3_file_walk_prunes_log_dirs():
    """P3：列目录必须剪掉只追加留痕目录（它们不参与指纹也不被拷）。"""
    from src.storage import data_snapshot
    src = inspect.getsource(data_snapshot._iter_state_files)
    assert "_LOG_DIR_NAMES" in src, (
        "列目录必须剪掉留痕目录（events/sessions/…）——"
        "原来 rglob 把每个留痕文件都枚举了一遍（1666 个），而它们白枚举")


def test_p4_daily_backup_still_includes_log_files():
    """P4：日备（全量恢复点）仍必须含留痕——剪枝不许漏掉它。"""
    from src.storage import data_snapshot
    assert hasattr(data_snapshot, "_iter_work_log_files"), "应有按 work 取留痕的口子"
    src = inspect.getsource(data_snapshot._snapshot_sync_impl)
    assert "_iter_work_log_files(wid)" in src, "manifest/日备应按需取留痕"
    assert "list(state_paths) + list(log_paths)" in src, (
        "日备的文件集必须＝可变状态 ＋ 留痕（恢复点要完整）")
