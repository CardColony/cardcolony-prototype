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
import os
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
# [5715 · 治理] 允许**用环境变量把快照库指到别的盘**（如 ECO_SNAPSHOT_ROOT=D:\bigdisk\snapshots）：
# 派生规则是「实例目录的平级」，所以**实例装在 C 盘时快照就长在 C 盘**——
# 机制本身不认盘的大小（2026-10-19 WorkBuddy 迁 D 盘时踩过同类坑：
# 国际版原本在 C 盘真实目录，把 C 盘撑爆）。给一个显式出口，别再靠搬家。
_env_root = os.environ.get("ECO_SNAPSHOT_ROOT", "").strip()
SNAPSHOT_ROOT = (Path(_env_root) if _env_root
                 else BASE_DIR.parent / f"{BASE_DIR.name}_data_snapshots")

# 受保护的数据目录（相对 BASE_DIR）
PROTECTED_DIRS = ("works",)

# 保留策略
KEEP_BATCHES = 200          # 每个 work 保留最近批次数（过程批次＝只拷可变状态，很小）
# 〔5715 · 机制改造〕日备是**全量**（含留痕）⇒ 一份就是整个会话的大小
# （实测那个 22 小时会话：354MB/份）⇒ 留 30 天＝10.6GB/work，太肥。
# ⇒ 默认只留 **7 天**；更早的完整复盘点靠「周备」或 git 兜底。
KEEP_DAILY_DAYS = 7         # 日备保留天数
# [5715 · 治理] 每 work 的快照**体积预算**（字节）。0＝不设闸。
# 为什么要它：只按份数留 ⇒ 总量 = 200 × 单份体积，而单份体积随会话线性增长
# （cards.db ＋ events ＋ sessions 全量复制）⇒ **总量无界**。
# 实测本机快照库 50.8GB（work_018 18.0GB / work_025 29.6GB）——「只计数不计量」
# 就是没有治理。默认 2GB/work：够回看几天的过程，又不会把盘吃穿。
KEEP_BATCH_BYTES = 2 * 1024 * 1024 * 1024

#: 体积核算的频次（每 N 次修剪才算一次体积）——算体积要遍历批次目录，
#: 每次快照都算会把快照路径拖慢（实测全量测试 173s → 280s+）。
VOLUME_PRUNE_EVERY = 16
_volume_counter = [0]

# 内容指纹去重：记录各 work 上次快照时的指纹，内容未变则跳过（避免重复拍同一状态）
_FINGERPRINT_FILE = BASE_DIR / ".snapshot_fingerprints.json"

_CST = timezone(timedelta(hours=8))


def _now_stamp() -> str:
    return datetime.now(_CST).strftime("%Y%m%d_%H%M%S_%f")[:22]


def _day_stamp() -> str:
    return datetime.now(_CST).strftime("%Y%m%d")


def _iter_work_log_files(wid: str) -> list[Path]:
    """某个 work 的**只追加留痕**文件（events/sessions/…）。

    〔P26 · 2026-10-26〕只在**真要拍这个 work** 时才调用（日备/manifest 需要它），
    判据与 `_is_log_file` **逐字一致**（路径里含任一留痕目录名即算留痕）。
    """
    out: list[Path] = []
    for d in PROTECTED_DIRS:
        root = BASE_DIR / d / wid
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and _is_log_file(p):
                out.append(p)
    return out


def _iter_protected_files() -> list[Path]:
    """列出所有受保护数据文件（works/ 下的**可变状态**文件）。

    〔P26 · 2026-10-26 · 治「疯狂读写硬盘」〕
    **遍历时剪掉只追加留痕目录**（events/sessions/…）：原来这里 rglob("*") 把这些
    目录里的**每个文件**都枚举了一遍（实测 1666 个），而它们本来就不参与指纹、
    也不被拷进批次 ⇒ 白枚举。留痕改由 `_iter_work_log_files(wid)` **按需**取
    （只有日备/manifest 需要 ⇒ 不该在每次写操作时枚举）。

    ⚠️ **入口名保留**（旧调用点与测试都 mock 它）⇒ 语义在**内容**上收窄为
    「可变状态」，行为逐字等价（留痕照旧只进日备）。

    ⚠️ 日备（全量恢复点）**必须**含留痕 ⇒ 它走 `_iter_work_log_files`，
    不是这个函数。
    """
    return _iter_state_files(None)


def _iter_state_files(work_id: str | None = None) -> list[Path]:
    """列出**可变状态**文件（**遍历时剪掉只追加留痕目录**；给了 work_id 则**只看那一个 work**）。

    〔P26 · 2026-10-26 · 治「疯狂读写硬盘」〕
    病根：原来算指纹前对**每个** work 做 ``rglob("*")`` ⇒ 把 events/ sessions/ 这些
    **只追加留痕**目录里的**每一个文件**都枚举了一遍（实测 1656 个文件）；
    而这些文件本来就会被 `_split_state_and_log` 分到「留痕」侧、**不参与指纹也不被拷**
    ⇒ 枚举了也白枚举。
    ⇒ 改成**不进**留痕目录 + **只看指定 work** ⇒ 结果逐字相同、IO 大幅下降。

    ⇒ 配合调用点补上 ``work_id``（P26 同批）：一次写操作原本要扫**所有** work
    （实测 35+ 个 ⇒ 磁盘 %DiskTime 1122% ⇒ 服务不响应、网页全卡），
    现在只扫**被改的那一个** work。
    """
    out: list[Path] = []
    for d in PROTECTED_DIRS:
        root = (BASE_DIR / d / work_id) if work_id else (BASE_DIR / d)
        if not root.exists():
            continue
        for sub in os.walk(root):
            # 剪枝：留痕目录不进（它只追加、快照不拷它 ⇒ 枚举无意义）
            sub[1][:] = [x for x in sub[1] if x not in _LOG_DIR_NAMES]
            for name in sub[2]:
                out.append(Path(sub[0]) / name)
    return out


#: 留痕目录名（**只追加**、快照拷它没有意义——它只会越来越长）
_LOG_DIR_NAMES = ("events", "sessions", "_ops", "memory", "logs")


def _is_log_file(p: Path) -> bool:
    """这个文件是不是「只追加留痕」？

    〔5715 · 机制改造〕原来的快照把**整个会话状态**全量拷一份，其中绝大部分
    是 events/*.jsonl、sessions/*.md 这类**只追加**的文件——它们不会「变旧」，
    快照拷它们等于把同一份历史反复抄 N 遍（实测：一份 240MB 的批次里，
    绝大部分是只追加的留痕；cards.db 才 1MB）。

    ⇒ 过程批次只拷**可变状态**（cards.db 等）；留痕交给**日备**（每天一份全量）。
    """
    try:
        rel = p.relative_to(BASE_DIR)
    except ValueError:
        return True
    return any(part in _LOG_DIR_NAMES for part in rel.parts)


def _split_state_and_log(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """把受保护文件分成（可变状态, 只追加留痕）。"""
    state: list[Path] = []
    log: list[Path] = []
    for p in paths:
        (log if _is_log_file(p) else state).append(p)
    return state, log


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


def _batch_bytes(p: Path) -> int:
    """一个批次目录的总体积（字节）。"""
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _prune_batches(work_id: str, force: bool = False) -> int:
    """按保留策略清理旧批次；返回删除批次数。

    [5715 · 治理] 原来只有**份数**上限（KEEP_BATCHES=200）⇒ 总量 = 200 × 单份体积
    ⇒ 单份体积随会话线性增长（cards.db ＋ events ＋ sessions 全量复制）⇒
    **总量无界**：实测本机快照库 50.8GB（work_018 18.0GB / work_025 29.6GB，
    每份 91.9MB / 151.4MB）⇒ 「只计数不计量」＝ 无治理。
    ⇒ 追加**体积闸**：每 work 的快照总量超过 KEEP_BATCH_BYTES 就从**最旧**的非日备
    批次开始删，直到回到预算内（日备是「每天一份」的兜底，不参与这个删法）。
    """
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
                _drop_manifest(p)
                removed += 1
    # [5715 · 治理] 体积闸：总量超预算 ⇒ 从最旧的非日备批次开始删
    #
    # ⚠ 算体积要**遍历批次目录**（每个批次里是 cards.db/events/sessions 若干文件）
    # ⇒ 每次快照都算 ⇒ 快照路径变慢（实测全量测试从 173s 涨到 280s+）。
    # ⇒ 按频次摊销：每 VOLUME_PRUNE_EVERY 次才做一次体积核算（闸仍有界，只是
    #    滞后最多 N 个批次——N 很小，磁盘占用不会跑飞）。
    if KEEP_BATCH_BYTES > 0:
        _volume_counter[0] += 1
        if force or _volume_counter[0] % VOLUME_PRUNE_EVERY == 0:
            # 重算（上面可能已删过几份）
            live = sorted([p for p in wd.iterdir() if p.is_dir()], key=lambda p: p.name)
            sizes = {p: _batch_bytes(p) for p in live}
            total = sum(sizes.values())
            for p in live:
                if total <= KEEP_BATCH_BYTES:
                    break
                if p.name.startswith("daily_"):
                    continue          # 日备不参与这个删法
                if _safe_rmtree(p):
                    _drop_manifest(p)
                    total -= sizes.get(p, 0)
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
    # 孤儿 manifest 清扫：批次目录没了、manifest 还在（失败批次会留下这种）
    # ⇒ 顺手带走，免得攒一地孤儿 json。
    for mf in wd.glob("*.manifest.json"):
        stem = mf.name[: -len(".manifest.json")]
        if not (wd / stem).is_dir():
            try:
                mf.unlink()
            except (Exception, SystemExit):  # noqa: BLE001 尽力而为
                pass
    return removed


def _fingerprint(paths: list[Path]) -> str:
    """算一组文件的「内容指纹」：路径 + 大小 + 修改时间。

    用途：判断这个 work 自上次快照以来内容有没有变——没变就不必再拍一张。
    〔5715 · 机制改造〕调用方只传**可变状态**文件 ⇒ 只追加留痕的变化不触发新批次。

    〔5715 · 自我更正〕时间戳用**纳秒**（``st_mtime_ns``）而不是整秒
    （``int(st_mtime)``）：整秒精度下，**同一秒内的两次改动**（改卡 → 立刻再改）
    会被判成「没变」⇒ 快照被跳过 ⇒ 那一次改动没有任何恢复点。实测踩中：
    同一秒内写两遍 cards.db，第二份批次根本没产生。
    """
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x)):
        try:
            st = p.stat()
            h.update(
                f"{p.relative_to(BASE_DIR)}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8"))
        except OSError:
            continue
    return h.hexdigest()


# ------------------------------------------------------------------ 为什么**没有**最小间隔
# 〔5715 · 机制改造 · 自我更正〕这里本来有一道「30 分钟最小间隔」（+「删卡/恢复/收口
# 强制拍」的例外）。**已撤**，三条理由，全部有实测/契约依据：
#
# ① 它砍掉了产品里写明的既有契约：
#    · 「清指纹 ⇒ 本次必须真拍」（tests/test_snapshot_roundtrip.py 的注释原话）；
#    · 「上批失败 ⇒ 下次必须重试，缺文件的批次不许被当成完整批次而永久跳过」
#      （tests/test_data_snapshot_lockresilience.py::test_e1）。
#    间隔闸把这两种情况一律判成 skipped ⇒ 全量跑出 **7 条新红**（已复现并修回）。
# ② 它是**限量**不是治本：体积的真正来源是「把只追加的留痕反复全量拷」（已用
#    「过程批次只拷可变状态」治掉：单份 173MB→59MB（work_025）、105MB→12MB
#    （work_018））。剩下的量级由**份数上限 200** 自然封顶：最重的 work_025
#    200×59MB=11.7GB、work_018 200×12MB=2.4GB、其余 24 个 work 各 <1GB
#    ⇒ 全库 ≈15GB（改前实测 49.8GB）。而 D 盘余量 949GB ⇒ **根本用不着限量**。
# ③ 它给全库挖了 30 分钟**盲窗**：这段时间内的改卡/删卡不在任何过程批次里
#    （「删卡」能靠 reason 关键字绕开，「改卡」不能），一旦此刻进程挂掉就丢。
#
# ⇒ 现在的判据只剩一条：**可变状态的内容指纹没变 ⇒ 不拍**；变了 ⇒ 立刻拍。


def _write_manifest(batch_dir: Path, payload: dict) -> None:
    """写批次的 manifest（这批是什么、留痕在哪）——可恢复性的凭据。

    〔5715 · 自我更正〕manifest **写在批次目录旁边**（``<stamp>.manifest.json``），
    不写进批次目录里：批次目录的既有契约是「**只装运行数据**（只有 works/…）」——
    （tests/test_snapshot_roundtrip.py::test_snapshot_restore_roundtrip 断言批次顶层
    集合恰为 ``{"works"}``）。把 manifest 塞进批次目录会破坏这条契约，也会让
    ``list_snapshots`` 的「文件数」统计把元数据算成数据。
    """
    try:
        path = batch_dir.with_name(batch_dir.name + ".manifest.json")
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 manifest 写不了不阻断
        logger.warning("[snapshot] manifest 写入失败 dir=%s: %s", batch_dir, exc)


def _drop_manifest(batch_dir: Path) -> None:
    """删批次时一并删掉旁边的 manifest（否则会攒一地孤儿 json）。"""
    try:
        path = batch_dir.with_name(batch_dir.name + ".manifest.json")
        if path.exists():
            path.unlink()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 尽力而为
        logger.warning("manifest 删除失败（不影响对账）: %s (%s: %s)",
                       batch_dir, type(exc).__name__, exc)


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
        # 〔P26 · 2026-10-26 · 治「疯狂读写硬盘」〕**只枚举可变状态**（剪掉留痕目录）。
        # 原来这里对每个 work 做 rglob("*") ⇒ 把 events/sessions 下的每个文件都枚举一遍
        # （几万次 stat）⇒ 而它们本来就不参与指纹也不被拷 ⇒ 白枚举。
        # 留痕（manifest 的 omitted_log_files ＋ 日备全量）改为**只在真要拍这个 work
        # 时**才取（见下面 `_iter_work_log_files`）⇒ 结果逐字相同、IO 大幅下降。
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
            # 〔5715 · 机制改造〕**只按「可变状态」算指纹**：
            # 只追加的留痕（events/sessions）变了**不触发**新批次
            # ⇒ 写操作再多，也不会把同一份历史反复抄 N 遍。
            # 〔P26〕files 已来自剪枝遍历 ⇒ 全是「可变状态」⇒ 不必再分类。
            state_paths = paths
            log_paths: list[Path] = []
            fp = _fingerprint(state_paths)
            if fps.get(wid) == fp:
                logger.info("快照跳过（可变状态未变）: %s 原因=%s", wid, reason)
                continue
            changed = True
            bdir = _batch_dir(wid, stamp)
            # 单文件失败不再中断整批：逐个拷，失败的记账，其余照拷
            failed: list[dict] = []
            # 〔5715 · 机制改造〕**过程批次只拷可变状态**（留痕交给日备）
            for p in state_paths:
                rel = p.relative_to(BASE_DIR)
                err = _copy_with_lock_retry(p, bdir / rel, wid)
                if err is None:
                    total += 1
                else:
                    failed.append(
                        {"work": wid, "file": str(rel), "stage": "batch",
                         "error": err})
            # 写 manifest：说清这批是什么、留痕在哪（可恢复性的凭据）
            # 〔P26〕留痕清单**现在才取**（只有真要拍这个 work 时才需要 ⇒ 不白扫）。
            log_paths = _iter_work_log_files(wid)
            _write_manifest(bdir, {
                "mode": "state-only",
                "created": stamp,
                "reason": reason,
                "state_files": [p.relative_to(BASE_DIR).as_posix() for p in state_paths],
                "omitted_log_files": [p.relative_to(BASE_DIR).as_posix() for p in log_paths],
                "log_files_note": "只追加留痕未入本批；完整留痕见日备 daily_<日期>",
            })
            # 每日首份另存日备（**全量**：含留痕 ⇒ 完整复盘点）
            daily = SNAPSHOT_ROOT / wid / f"daily_{_day_stamp()}"
            if not daily.exists():
                daily_failed = []
                # 日备＝**全量**（含留痕 ⇒ 完整复盘点）⇒ 文件集＝可变状态 ＋ 留痕
                for p in list(state_paths) + list(log_paths):
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

#: 每个 key 上次**拍完**的时刻（``time.monotonic``）。纯内存，重启后重来。
_snap_last_at: dict[str, float] = {}

#: 后台快照的**合并窗口**（秒）。窗口内的连续写**只拍一份**，且拍的是**最后**那次的态。
#:
#: 〔5715 · 自我更正 · 第二次〕这个窗口**必须放在调度层（schedule），不能放 API 层
#: （snapshot_sync）**：
#: · API 是「显式调用」⇒ 契约是「调用即执行」（清指纹必须真拍、上批失败下次必须重试）
#:   —— 这两条契约曾被我塞进 API 的「30 分钟间隔」里砍掉，全量直接多 7 条红。
#: · 调度层是「跟着写走」⇒ 允许把窗口内的连续写合并成一份，**不丢恢复点**
#:   （trailing edge：窗口结束时拍的那份装的是最后那次写入后的状态）。
#:
#: 为什么必须有窗口（实测）：没有窗口时「每次写都真拍」⇒ 全量测试从 157s 涨到 253s
#: （一次写 = 拷一份 cards.db ＋ 列目录 ＋ 写指纹 ＋ 修剪），并且会给有时限的用例
#: （如「建卡即首跑 4 秒内产出」）带来后台抖动。
SNAPSHOT_COALESCE_SECONDS = int(
    os.environ.get("ECO_SNAPSHOT_COALESCE_SECONDS") or 30)

#: **强制立刻拍**的理由关键字（不可逆/突变时刻 ⇒ 不等窗口）。
#: 生产里的 reason 串（card_store / shadow_store / work_store）：
#:   "[snapshot][work_001] create: card_x"   建卡   → 不强制
#:   "[snapshot][work_001] update: card_x"   改卡   → 不强制
#:   "[snapshot][work_001] delete: card_x"   删卡   → **强制**（不可逆）
#:   "[snapshot][work_001] restore: card_x"  恢复   → **强制**
#:   "[snapshot][work_001] shadow delete: …" 影子删 → **强制**（含 "delete"）
FORCE_REASONS = ("delete", "restore", "finalize", "收口", "删除", "恢复")


def _force_now(reason: str) -> bool:
    r = str(reason or "").lower()
    return any(k in r for k in FORCE_REASONS)


def _coalesce_wait(key: str) -> float:
    """还要等多久才拍（秒）。0＝现在拍。"""
    last = _snap_last_at.get(key)
    if last is None:
        return 0.0          # 第一次：立即拍
    return max(0.0, SNAPSHOT_COALESCE_SECONDS - (time.monotonic() - last))


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
      · **合并窗口**（默认 30 秒）：窗口内的连续写合并成一份，拍的是**最后**那次的态
        （trailing edge ⇒ 不丢恢复点）；删卡/恢复/收口**不等窗口，当场拍**；
      · 无运行中的事件循环（同步脚本/测试）⇒ 退回阻塞式，行为可预期。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        await snapshot(reason, work_id)
        return

    key = work_id or "*"
    if _force_now(reason):
        # 不可逆/突变时刻（删卡、恢复、收口）：**当场拍**，不等合并窗口。
        # 单独起任务，不打断正在等窗口的那个（醒来后那次会因为指纹相同而自动
        # 跳过 ⇒ 不会多出一份）。
        #
        # 〔P26 · 2026-10-26 · 治「服务不响应」〕**同一 key 已有快照在跑 ⇒ 不再新开任务**。
        # 原来这里**无条件** create_task ⇒ 每次「含 delete 的写」（删卡/删影子/恢复）
        # 都排一个新任务，而 `snapshot_sync` 用 `_SNAPSHOT_LOCK` **串行** ⇒
        # 这些任务**全占着默认线程池的 worker 等锁** ⇒ 线程池被占满
        # ⇒ 服务的其它 `asyncio.to_thread` 工作（**timeline**、nav 指纹…）全部排队
        # ⇒ **服务在监听却不响应**（实测请求 24 秒连不上）⇒ **所有浏览器都卡死**
        # （用户原话：「不是一���浏览器卡，是不同浏览器都卡死 ⇒ 服务层面的问题」——判断正确）。
        # ⇒ 改成与普通分支同款：已在跑就只标"脏"，跑完再补拍一次（合并语义不变、
        #   但不再堆任务占线程）。强制语义「当场拍」仍成立：正在跑的那次就是当场，
        #   补拍紧随其后。
        if key in _snap_tasks:
            _snap_dirty.add(key)
            return

        async def _force_run() -> None:
            try:
                # 〔P26〕与普通分支同款：**一个 key 同时最多一个快照任务**。
                # 期间又来了强制请求 ⇒ 标脏 ⇒ 这一轮结束后立刻补拍（不等合并窗口）。
                while True:
                    _snap_dirty.discard(key)
                    await snapshot(reason, work_id)
                    _snap_last_at[key] = time.monotonic()
                    if key not in _snap_dirty:
                        break
            except Exception as exc:  # noqa: BLE001 尽力而为
                logger.warning("强制快照失败（不影响对账）: %s (%s: %s)",
                               reason, type(exc).__name__, exc)

        _snap_tasks[key] = asyncio.create_task(_force_run())
        # 〔P26〕登记进 `_snap_tasks` ⇒ 后续同 key 的请求走「标脏」而不是再开一个任务。
        # （原来强制分支**从不登记** ⇒ 每次都新开 ⇒ 任务堆着等锁 ⇒ 占满线程池。）
        _snap_tasks[key].add_done_callback(lambda _t: _snap_tasks.pop(key, None))
        return

    if key in _snap_tasks:
        _snap_dirty.add(key)
        return

    async def _run() -> None:
        try:
            while True:
                _snap_dirty.discard(key)
                wait = _coalesce_wait(key)
                if wait > 0:
                    # 合并窗口：等窗口满再拍。窗口里的新写会被并进来 ⇒
                    # 这一份装的是**最后**那次写入后的状态。
                    await asyncio.sleep(wait)
                await snapshot(reason, work_id)
                _snap_last_at[key] = time.monotonic()
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


def _restore_sqlite_content(src: Path, dst: Path) -> str | None:
    """用 SQLite 的 **backup API** 把快照库的内容写回 live 库。

    〔5715 · 机制改造 · 根因修复〕为什么不能只靠「复制文件 + 删旁挂」：

    · WAL 模式下 live 库旁边有 ``cards.db-wal``/``-shm``；**删不掉**是常态
      （长活连接还握着它们，Windows 直接 WinError 32「另一个程序正在使用」）。
    · **删不掉的旧 WAL 会把旧数据复活**：文件已经换成快照内容了，一读却是旧值
      （实测：``restore_from_snapshot`` 返回 ok=True，内容却没回滚 ⇒ 静默失败）。
    · backup API 走 SQLite 自己的写事务把快照内容写进 live 库，旧 WAL 帧被新
      事务覆盖 ⇒ 内容**真的**回到快照态，且不依赖删文件。

    返回 ``None``＝成功；返回错误串＝失败（**不静默装成功**）。
    """
    scon = dcon = None
    try:
        scon = sqlite3.connect(str(src))
        dcon = sqlite3.connect(str(dst), timeout=10.0)
        dcon.execute("PRAGMA busy_timeout=10000")
        scon.backup(dcon)
        return None
    except Exception as exc:  # noqa: BLE001 失败要如实报
        return f"{type(exc).__name__}: {exc}"
    finally:
        for con in (scon, dcon):
            try:
                if con is not None:
                    con.close()
            except Exception:  # noqa: BLE001
                pass


def restore_from_snapshot(work_id: str, stamp: str, rel_path: str, target: Path | None = None) -> dict:
    """从指定批次恢复单个文件（工具函数，供人工/脚本使用）。

    [卡片存储迁移 SQLite] 恢复 ``*.db`` 时**用 backup API 回写内容**（不靠删
    ``-wal``）：旧 WAL 删不掉时会把旧数据复活 ⇒ 恢复看着成功、实际没回滚。
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
        # 内容对齐（真正生效的那一步）：旧 WAL 帧不得把旧数据复活
        err = _restore_sqlite_content(src, dst)
        if err:
            # 不静默装成功：恢复没生效就明说
            return {"ok": False, "error": f"恢复库内容失败（live 库内容未回滚）: {err}",
                    "from": str(src), "to": str(dst)}
        # 内容已对齐后，旁挂文件只是占地方 ⇒ 尽力清（清不掉也不影响正确性）
        for suffix in ("-wal", "-shm"):
            side = dst.with_name(dst.name + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError as exc:  # noqa: BLE001
                logger.warning("恢复后清理旁挂文件失败（内容已对齐，不影响）%s: %s", side, exc)
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
