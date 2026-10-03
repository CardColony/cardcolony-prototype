# -*- coding: utf-8 -*-
"""数据快照：把运行数据从 git 追踪中解放出来的替代留痕机制。

与 git 仓库物理分离（D:\\AI_station\\推演agent_data_snapshots\\），
git 的任何分支操作都碰不到它。

设计要点：
- 变更文件按 <work_id>/<时间戳>/<相对路径> 复制保存
- 保留策略：每 work 保留最近 KEEP_BATCHES 个批次；每个自然日保留一个日备
- 失败只记日志，绝不抛异常中断主流程（与 git_auto_commit 同一纪律）
- 完全独立于 git：git 坏了它照常工作
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("storage.snapshot")

BASE_DIR = Path(__file__).resolve().parents[2]

# 快照根目录：与代码仓库物理分离（跨出 推演agent 目录）
SNAPSHOT_ROOT = BASE_DIR.parent / "推演agent_data_snapshots"

# 受保护的数据目录（相对 BASE_DIR）
PROTECTED_DIRS = ("works",)

# 保留策略
KEEP_BATCHES = 200          # 每个 work 保留最近批次数
KEEP_DAILY_DAYS = 30        # 日备保留天数

_CST = timezone(timedelta(hours=8))


def _now_stamp() -> str:
    return datetime.now(_CST).strftime("%Y%m%d_%H%M%S_%f")[:22]


def _day_stamp() -> str:
    return datetime.now(_CST).strftime("%Y%m%d")


def _iter_protected_files() -> list[Path]:
    """列出所有受保护数据文件（works/ 下的全部文件）。"""
    out: list[Path] = []
    for d in PROTECTED_DIRS:
        root = BASE_DIR / d
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file():
                out.append(p)
    return out


def _batch_dir(work_id: str, stamp: str) -> Path:
    return SNAPSHOT_ROOT / work_id / stamp


def _prune_batches(work_id: str) -> int:
    """按保留策略清理旧批次；返回删除批次数。"""
    wd = SNAPSHOT_ROOT / work_id
    if not wd.exists():
        return 0
    batches = sorted([p for p in wd.iterdir() if p.is_dir()], key=lambda p: p.name)
    removed = 0
    if len(batches) > KEEP_BATCHES:
        for p in batches[: len(batches) - KEEP_BATCHES]:
            if p.name.startswith("daily_"):
                continue
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    # 日备超期清理
    cutoff = datetime.now(_CST).date() - timedelta(days=KEEP_DAILY_DAYS)
    for p in batches:
        if not p.name.startswith("daily_"):
            continue
        try:
            d = datetime.strptime(p.name.replace("daily_", ""), "%Y%m%d").date()
        except Exception:
            continue
        if d < cutoff:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return removed


def snapshot_sync(reason: str = "") -> dict:
    """同步执行一次全量快照（受保护目录下的全部文件）。

    返回 {"ok": bool, "work_id": str, "files": int, "dir": str, "daily": bool}
    """
    result = {"ok": False, "work_id": "-", "files": 0, "dir": "", "daily": False, "reason": reason}
    try:
        files = _iter_protected_files()
        if not files:
            result["ok"] = True
            return result

        # 按 work 分组（works/work_001/... → work_001）
        groups: dict[str, list[Path]] = {}
        for p in files:
            rel = p.relative_to(BASE_DIR)
            parts = rel.parts
            wid = parts[1] if len(parts) > 1 else "_root"
            groups.setdefault(wid, []).append(p)

        stamp = _now_stamp()
        total = 0
        for wid, paths in groups.items():
            bdir = _batch_dir(wid, stamp)
            for p in paths:
                rel = p.relative_to(BASE_DIR)
                dst = bdir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                total += 1
            # 每日首份另存日备（只拷一次，避免膨胀）
            daily = SNAPSHOT_ROOT / wid / f"daily_{_day_stamp()}"
            if not daily.exists():
                for p in paths:
                    rel = p.relative_to(BASE_DIR)
                    dst = daily / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                result["daily"] = True
            _prune_batches(wid)

        result.update({"ok": True, "work_id": ",".join(groups.keys()),
                       "files": total, "dir": str(SNAPSHOT_ROOT / stamp)})
        logger.info("快照完成: %s 文件=%s 原因=%s", result["work_id"], total, reason)
    except Exception as exc:
        logger.warning("快照异常: %s (%s)", reason, exc)
    return result


async def snapshot(reason: str = "") -> bool:
    """异步包装（与 git_auto_commit.commit 同签名风格）。

    兼容 commit(message) 的调用方式：把 message 当作 reason 记录。
    """
    try:
        r = await asyncio.to_thread(snapshot_sync, reason)
        return bool(r.get("ok"))
    except Exception as exc:
        logger.warning("快照任务异常: %s (%s)", reason, exc)
        return False


async def commit(message: str) -> bool:
    """兼容旧调用名（card_store/work_store 里调的是 commit）。

    语义已从「git 提交」改为「数据快照」——名字保留是为了少改调用点。
    """
    return await snapshot(message)


def restore_from_snapshot(work_id: str, stamp: str, rel_path: str, target: Path | None = None) -> dict:
    """从指定批次恢复单个文件（工具函数，供人工/脚本使用）。"""
    src = SNAPSHOT_ROOT / work_id / stamp / rel_path
    if not src.exists():
        return {"ok": False, "error": f"快照不存在: {src}"}
    dst = target or (BASE_DIR / rel_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"ok": True, "from": str(src), "to": str(dst), "size": dst.stat().st_size}


def list_snapshots(work_id: str | None = None) -> list[dict]:
    """列出快照批次（供恢复时选择）。"""
    out = []
    roots = [SNAPSHOT_ROOT / work_id] if work_id else [p for p in SNAPSHOT_ROOT.iterdir() if p.is_dir()] if SNAPSHOT_ROOT.exists() else []
    for wd in roots:
        if not wd.exists():
            continue
        for b in sorted(wd.iterdir()):
            if not b.is_dir():
                continue
            n = sum(1 for _ in b.rglob("*") if _.is_file())
            out.append({"work_id": wd.name, "stamp": b.name, "files": n})
    return out
