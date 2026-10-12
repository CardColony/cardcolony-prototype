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
import hashlib
import json
import logging
import shutil
import time  # noqa: F401  (历史遗留，勿删)
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("storage.snapshot")

BASE_DIR = Path(__file__).resolve().parents[2]

# 快照根目录：与代码仓库物理分离（跨出实例目录），并按实例名派生——
# 任何在别的目录跑这套代码的实例（如推演agent_test），快照必须落自己的根，
# 不得写进正式实例的快照库（2026-10-03 实测发现硬编码名导致跨实例污染）。
# 对正式实例派生结果与原硬编码逐字相同（推演agent → 推演agent_data_snapshots）。
SNAPSHOT_ROOT = BASE_DIR.parent / f"{BASE_DIR.name}_data_snapshots"

# 受保护的数据目录（相对 BASE_DIR）
PROTECTED_DIRS = ("works",)

# 保留策略
KEEP_BATCHES = 200          # 每个 work 保留最近批次数
KEEP_DAILY_DAYS = 30        # 日备保留天数

# 内容指纹去重：记录各 work 上次快照时的指纹，内容未变则跳过（避免重复拍同一状态）
_FINGERPRINT_FILE = BASE_DIR / ".snapshot_fingerprints.json"

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


def _safe_rmtree(p: Path) -> bool:
    """删一个旧批次；失败只记日志返回 False，**绝不外抛**。

    [2026-10-12] 为什么连 BaseException 都要接：服务进程若继承了 WorkBuddy
    agent 沙箱的删除守卫 env（CODEBUDDY_SAFE_DELETE_* ＋ CODEBUDDY_TOOL_CALL_ID，
    经启动 shell 传递），shutil.rmtree 会被沙箱 sitecustomize 掉包——先过删除
    守卫 node 子进程，判负时 fail-closed ``raise SystemExit(1)``。SystemExit 是
    BaseException：create_card/snapshot 链上所有 ``except Exception`` 都拦不住它，
    它会一路穿透（asyncio.to_thread 的 future 会把线程里的 SystemExit 原样重抛
    给等待方）直到 uvicorn 底层，把用户请求打成裸 500——无 prompt 日志、无错误
    事件、无 traceback（12:36/13:22 两次 create_card 后 500 实测正是此路径）。
    保留修剪是尽力而为的维护动作：一批删不动（撞闸/权限/占用）就跳过它，
    绝不许反噬主链路。
    """
    try:
        shutil.rmtree(p, ignore_errors=True)
        return True
    except (Exception, SystemExit) as exc:  # noqa: BLE001 SystemExit＝沙箱删除守卫
        logger.warning("旧批次删除失败（跳过，不阻断）: %s (%s: %s)",
                       p, type(exc).__name__, exc)
        return False


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
            if _safe_rmtree(p):
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
            if _safe_rmtree(p):
                removed += 1
    return removed


def _fingerprint(paths: list[Path]) -> str:
    """算一组文件的「内容指纹」：路径 + 大小 + 修改时间。

    用途：判断这个 work 自上次快照以来内容有没有变——没变就不必再拍一张。
    """
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x)):
        try:
            st = p.stat()
            h.update(f"{p.relative_to(BASE_DIR)}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8"))
        except OSError:
            continue
    return h.hexdigest()


def _load_fingerprints() -> dict:
    try:
        if _FINGERPRINT_FILE.exists():
            return json.loads(_FINGERPRINT_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _save_fingerprints(fp: dict) -> None:
    try:
        _FINGERPRINT_FILE.write_text(json.dumps(fp, ensure_ascii=False, indent=2), "utf-8")
    except Exception as exc:
        logger.warning("指纹文件写入失败: %s", exc)


def snapshot_sync(reason: str = "", work_id: str | None = None) -> dict:
    """同步执行一次快照。

    work_id 给定时：只拍这一个 work（动哪拍哪）；不给定时：拍全部（供总备份/首次使用）。
    内容指纹与上次相同则跳过——避免同一状态被反复拍成几十张。

    返回 {"ok", "work_id", "files", "dir", "daily", "skipped", "reason"}
    """
    result = {"ok": False, "work_id": "-", "files": 0, "dir": "", "daily": False,
              "skipped": False, "reason": reason}
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

        # 只拍指定 work（动哪拍哪）；未指定则拍全部
        if work_id:
            if work_id not in groups:
                result["ok"] = True
                result["skipped"] = True
                return result
            groups = {work_id: groups[work_id]}

        fps = _load_fingerprints()
        stamp = _now_stamp()
        total = 0
        changed = False
        for wid, paths in groups.items():
            fp = _fingerprint(paths)
            if fps.get(wid) == fp:
                logger.info("快照跳过（内容未变）: %s 原因=%s", wid, reason)
                continue
            changed = True
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
            fps[wid] = fp
            _prune_batches(wid)

        _save_fingerprints(fps)
        result.update({"ok": True, "work_id": ",".join(groups.keys()),
                       "files": total, "dir": str(SNAPSHOT_ROOT / stamp),
                       "skipped": not changed})
        if changed:
            logger.info("快照完成: %s 文件=%s 原因=%s", result["work_id"], total, reason)
        else:
            logger.info("快照全部跳过（内容均未变）: %s", result["work_id"])
    except (Exception, SystemExit) as exc:  # noqa: BLE001 SystemExit＝沙箱删除守卫（见 _safe_rmtree）
        # 快照整体是尽力而为的留痕：任何失败（含 SystemExit 穿透）降级为告警，
        # 绝不上抛——留痕机制不配拥有杀死用户请求的能力（12:36/13:22 实测教训）。
        logger.warning("快照异常: %s (%s: %s)", reason, type(exc).__name__, exc)
    return result


async def snapshot(reason: str = "", work_id: str | None = None) -> bool:
    """异步包装（与 git_auto_commit.commit 同签名风格）。

    work_id：只拍这一个剧本（动哪拍哪）。不给定时拍全部。
    """
    try:
        r = await asyncio.to_thread(snapshot_sync, reason, work_id)
        return bool(r.get("ok"))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 含沙箱删除守卫的 SystemExit
        logger.warning("快照任务异常: %s (%s: %s)", reason, type(exc).__name__, exc)
        return False


async def commit(message: str, work_id: str | None = None) -> bool:
    """兼容旧调用名（card_store/work_store 里调的是 commit）。

    语义已从「git 提交」改为「数据快照」——名字保留是为了少改调用点。
    work_id 透传：调用方知道动的是哪个剧本时，只拍那一个。
    """
    return await snapshot(message, work_id)


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
