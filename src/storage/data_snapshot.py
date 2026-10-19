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
import sqlite3
import threading
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


def _vacuum_into(src_db: Path, dst: Path) -> None:
    """SQLite 一致副本：``VACUUM INTO``。

    [卡片存储迁移 SQLite 2026-10-14] WAL 模式下**不能直接复制** ``cards.db``
    ——拿到的是不含最新提交的快照（最新写入还在 ``-wal`` 里，方案 §7.1.2a）。
    ``VACUUM INTO`` 从库里读出一致的完整副本（目标文件须不存在）。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    con = sqlite3.connect(str(src_db))
    try:
        con.execute("VACUUM INTO ?", (str(dst),))
    finally:
        con.close()


def _copy_protected_file(p: Path, dst: Path) -> None:
    """把一个受保护文件复制进快照批次。

    - ``*.db``（卡片库）→ ``VACUUM INTO`` 产出一致副本；
    - ``*.db-wal`` / ``*.db-shm``（瞬态旁挂）→ 跳过（内容已由 VACUUM INTO 纳入；
      但它们仍参与 ``_fingerprint``，这样 WAL 下的写入也能被探测到）；
    - 其余文件照旧 ``copy2``。
    """
    if p.suffix == ".db":
        _vacuum_into(p, dst)
        return
    if p.name.endswith(".db-wal") or p.name.endswith(".db-shm"):
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dst)


# ---------------------------------------- Windows 文件锁竞态（快照侧）
# 判据与预算**全部复用** session_facade（P1 已落地），不另写第三份。
# 为何惰性 import：session_facade → card_store → data_snapshot 是同包真环，
# 模块级 import 会成环；本仓既有惯例同样是函数内 import。


def _race_path(exc: OSError, dst: Path) -> Path:
    """撞锁的是哪个文件——优先用异常自带的 ``filename``。

    实测：``copy2`` 撞**只读侧**的源（读被放行）不失败；真正被拒的是写目标
    那一步（errno=13, winerror=None）。但源若被 share=0 独占（连读都拒），
    失败点又回到源上。故不能想当然只查一个：异常指名了哪个就读哪个。
    """
    fn = getattr(exc, "filename", None)
    if isinstance(fn, str) and fn:
        try:
            return Path(fn)
        except (TypeError, ValueError):
            return dst
    return dst


def _copy_with_lock_retry(p: Path, dst: Path, wid: str) -> str | None:
    """单个文件进快照批次；撞 Windows 锁竞态做有界重试＋指数退避。

    返回 ``None``＝成功；返回错误串＝该文件失败（**不抛任何异常**）。
    非锁竞态（真无权限、ENOENT…）**零退避立即判失败**，不得让重试掩盖真错。
    """
    from .session_facade import (
        _LOCK_RETRY_ATTEMPTS, _LOCK_RETRY_BASE_SLEEP,
        _is_windows_lock_race, _log_lock_race,
    )

    what = f"快照拷贝 {wid}/{p.name}"
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            _copy_protected_file(p, dst)
            return None
        except PermissionError as exc:
            probe = _race_path(exc, dst)
            race = _is_windows_lock_race(exc, probe)
            if attempt == _LOCK_RETRY_ATTEMPTS - 1 or not race:
                if attempt and race:
                    _log_lock_race(exc, probe, attempt, what, give_up=True)
                _drop_partial(dst)
                return f"{type(exc).__name__}: {exc}"
            if not attempt:
                _log_lock_race(exc, probe, attempt, what)
            time.sleep(_LOCK_RETRY_BASE_SLEEP * (2 ** attempt))
        except (Exception, SystemExit) as exc:  # noqa: BLE001 SystemExit＝沙箱删除守卫
            # 非锁竞态（ENOENT／只读／IO…）：**零退避**立即判该文件失败。
            # 必须在这里收口：文件在列目录后、拷贝前消失（实测 cards.db-journal）
            # 会让一个文件的消失废掉整批——那正是本批要修的「整批作废」。
            _drop_partial(dst)
            return f"{type(exc).__name__}: {exc}"
    return "重试预算耗尽"  # pragma: no cover  (循环内必 return)


def _drop_partial(dst: Path) -> None:
    """失败后清掉可能写了一半的目标文件（尽力而为，绝不外抛）。

    为什么必须清：批次目录每次按时间戳新建，目标此前不存在，删除不碰历史好数据；
    而留着半截文件比缺文件更坏——恢复时会拿到一个静默损坏的副本。
    """
    try:
        if dst.exists():
            dst.unlink()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 尽力而为
        logger.warning("半截快照文件清理失败（不影响对账）: %s (%s: %s)",
                       dst, type(exc).__name__, exc)


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


#: 快照串行锁。
#: [画布 2026-10-19] 快照改成后台调度之后，两次快照可能同时写同一目标目录，
#: 实测报 `OperationalError: output file already exists`（sqlite backup 撞车）。
#: 快照在 worker 线程里跑，所以用 threading.Lock（不是 asyncio.Lock）——
#: 它同时覆盖"后台任务"和"脚本/测试里直接调 snapshot_sync"两种调用。
_SNAPSHOT_LOCK = threading.Lock()


def snapshot_sync(reason: str = "", work_id: str | None = None) -> dict:
    """同步执行一次快照（串行：同一时刻只有一次快照在写盘）。"""
    with _SNAPSHOT_LOCK:
        return _snapshot_sync_impl(reason, work_id)


def _snapshot_sync_impl(reason: str = "", work_id: str | None = None) -> dict:
    """同步执行一次快照。

    work_id 给定时：只拍这一个 work（动哪拍哪）；不给定时：拍全部（供总备份/首次使用）。
    内容指纹与上次相同则跳过——避免同一状态被反复拍成几十张。

    返回 {"ok", "work_id", "files", "dir", "daily", "skipped", "reason"}
    """
    # failed：本次**哪些文件没进快照**的可对账清单（逐文件失败才非空）。
    # ok 的口径不变＝「这轮快照流程跑完了」，部分缺失另由 failed 表达。
    result = {"ok": False, "work_id": "-", "files": 0, "dir": "", "daily": False,
              "skipped": False, "reason": reason, "failed": []}
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
        all_failed: list[dict] = []
        for wid, paths in groups.items():
            fp = _fingerprint(paths)
            if fps.get(wid) == fp:
                logger.info("快照跳过（内容未变）: %s 原因=%s", wid, reason)
                continue
            changed = True
            bdir = _batch_dir(wid, stamp)
            # 单文件失败不再中断整批：逐个拷，失败的记账，其余照拷
            failed: list[dict] = []
            for p in paths:
                rel = p.relative_to(BASE_DIR)
                err = _copy_with_lock_retry(p, bdir / rel, wid)
                if err is None:
                    total += 1
                else:
                    failed.append(
                        {"work": wid, "file": str(rel), "stage": "batch",
                         "error": err})
            # 每日首份另存日备（只拷一次，避免膨胀）
            daily = SNAPSHOT_ROOT / wid / f"daily_{_day_stamp()}"
            if not daily.exists():
                daily_failed = []
                for p in paths:
                    rel = p.relative_to(BASE_DIR)
                    err = _copy_with_lock_retry(p, daily / rel, wid)
                    if err is not None:
                        daily_failed.append(
                            {"work": wid, "file": str(rel), "stage": "daily",
                             "error": err})
                if daily_failed:
                    # 日备目录本轮新建（上方 not daily.exists() 闸），缺文件就整份作废：
                    # 留着会被明日当成「今日已备」而永不补齐。
                    _safe_rmtree(daily)
                    failed.extend(daily_failed)
                else:
                    result["daily"] = True
            all_failed.extend(failed)
            if failed:
                # [可对账] 这批缺了哪几个文件：日志一条说清，别只留笼统的「快照异常」。
                # 分子取**去重后的缺失文件数**（同一文件可能在批次与日备两阶段各记一次）。
                missing = {f["file"] for f in failed}
                logger.warning(
                    "快照部分文件失败: %s (%d/%d): %s", wid, len(missing), len(paths),
                    "; ".join(f"{f['stage']}:{f['file']}({f['error']})"
                              for f in failed)[:800])
            else:
                # 只有**整批完整**才置指纹：缺文件的批次不许被当成完整批次
                # 而永久跳过（下次照常重试这批）；修剪同理。
                fps[wid] = fp
                _prune_batches(wid)

        _save_fingerprints(fps)
        result.update({"ok": True, "work_id": ",".join(groups.keys()),
                       "files": total, "dir": str(SNAPSHOT_ROOT / stamp),
                       "skipped": not changed,
                       "failed": all_failed})
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


#: 正在跑的合并快照任务（key＝work_id 或 "*"）
_snap_tasks: dict[str, "asyncio.Task[None]"] = {}
_snap_dirty: set[str] = set()


async def schedule(reason: str = "", work_id: str | None = None) -> None:
    """后台拍快照：**不阻塞调用方**，同 work 合并。

    [画布 2026-10-19 · 性能] 原来每个写操作都 `await snapshot(...)`——实测
    单次快照 **430~450ms**（要复制 cards.db 等整批文件），于是建卡 1.1s、
    删卡 1.6s，而且批次越积越慢（实测连续建卡 1.1s→2.9s）。
    快照是**尽力而为的留痕**（本模块自己的口径：失败只记日志、绝不上抛），
    让它阻塞用户操作是拿最贵的资源做最不重要的事。

    现在的语义：
      · 立刻返回，快照在后台跑；
      · 同一 work 已有快照在跑 ⇒ 只记一个"脏"标记，跑完再补拍一次
        （连续 10 次写只会产生 2 次快照，而不是 10 次）；
      · 无运行中的事件循环（同步脚本/测试）⇒ 退回阻塞式，行为可预期。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        await snapshot(reason, work_id)
        return

    key = work_id or "*"
    if key in _snap_tasks:
        _snap_dirty.add(key)
        return

    async def _run() -> None:
        try:
            while True:
                _snap_dirty.discard(key)
                await snapshot(reason, work_id)
                if key not in _snap_dirty:
                    break
        finally:
            _snap_tasks.pop(key, None)

    _snap_tasks[key] = asyncio.create_task(_run())


async def commit(message: str, work_id: str | None = None) -> bool:
    """兼容旧调用名（card_store/work_store 里调的是 commit）。

    语义已从「git 提交」改为「数据快照」——名字保留是为了少改调用点。
    work_id 透传：调用方知道动的是哪个剧本时，只拍那一个。
    """
    return await snapshot(message, work_id)


def restore_from_snapshot(work_id: str, stamp: str, rel_path: str, target: Path | None = None) -> dict:
    """从指定批次恢复单个文件（工具函数，供人工/脚本使用）。

    [卡片存储迁移 SQLite] 恢复 ``*.db`` 时一并清掉旁挂 ``-wal``/``-shm``
    ——副本内容已含当时提交，留着旧 WAL 会与新库文件错配。
    """
    src = SNAPSHOT_ROOT / work_id / stamp / rel_path
    if not src.exists():
        return {"ok": False, "error": f"快照不存在: {src}"}
    dst = target or (BASE_DIR / rel_path)
    # [卡片存储迁移 SQLite] 库文件被外部替换前，先关掉 card_db 的长活连接
    # （否则它仍握着旧的 -wal/-shm，恢复不生效）。
    if dst.suffix == ".db":
        try:
            from . import card_db
            card_db.close_work(work_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复前关闭库连接失败（不阻断）%s: %s", work_id, exc)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if dst.suffix == ".db":
        for suffix in ("-wal", "-shm"):
            side = dst.with_name(dst.name + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError as exc:  # noqa: BLE001
                logger.warning("恢复后清理旁挂文件失败（不阻断）%s: %s", side, exc)
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
