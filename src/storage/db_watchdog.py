# -*- coding: utf-8 -*-
"""库看门狗（work_027 cards.db 被原地清零事故 · 2026-11-07）。

事故复盘：``works/work_027/cards.db``（3.89GB，全项目唯一真相）在 23:05:51
被某进程原地清零成 0 字节。系统**一声没吭**——第一个警报是「下次读失败」，
等发现时 WAL 已悬在死文件上继续写了一个小时。

本模块是那个缺口：

1. **stat 哨兵**（每 ``ECO_DB_WATCH_S`` 秒，默认 5）：盯 ``data/{wid}/*.db``
   ＋迁移期兼容 ``works/{wid}/*.db``，记各库最近一次尺寸。
   判据：
     - 尺寸由 >0 跌到 0            → ``ZEROED``（原地清零，本案）
     - 尺寸腰斩以上且缩 ≥16MB      → ``SHRANK``（外部截断/改写）
     - 库消失但 ``-wal`` 还在      → ``VANISHED``（数据只剩 WAL）
   只盯「突变」：fresh 库 0 字节是 WAL 模式正常态（checkpoint 前主文件可为空），
   不告警——**基线不报警，变化才报警**。
2. **异常即刻隔离**：命中即把 ``{db,-wal,-shm}`` 三件套拷进
   ``db_quarantine/{时间戳}_{wid}_{库名}/``（WAL 先拷——它常是唯一活着的数据），
   附 ``_NOTE.txt`` 记录判据与前后尺寸。隔离目录只增不覆（时间戳命名）。
3. **周期完整性抽检**（每 ``ECO_DB_CHECK_S`` 秒，默认 3600；0=关）：
   优先检查已发布日备。无日备时从活库只读生成一致副本，归零库只作应急取证。
   快照、活库及抽检暂缓分别记账。
4. **告警面**：``logger.critical``（进 server.log）＋ 隔离目录 ``_NOTE.txt``
   ＋ ``logs/db_alert_{stamp}.txt``（给运维一眼能看到的地方）。

环境变量：
  ``ECO_DB_WATCH=0``       关整个看门狗（默认开）
  ``ECO_DB_WATCH_S``       stat 轮询间隔秒（默认 5）
  ``ECO_DB_CHECK_S``       完整性抽检间隔秒（默认 3600；0 ＝ 关）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("storage.dbwatch")

_INTERVAL = float(os.environ.get("ECO_DB_WATCH_S") or 5.0)
_CHECK_S = float(os.environ.get("ECO_DB_CHECK_S") or 3600.0)
_ENABLED = os.environ.get("ECO_DB_WATCH", "1").strip() != "0"

#: 尺寸跌幅判据：比基线缩这么多且绝对量达标 → SHRANK（防 VACUUM 类良性收窄误报，
#: 也防几十 KB 的小波动刷屏）。
_SHRINK_RATIO = 0.5
_SHRINK_MIN_BYTES = 16 * 1024 * 1024

_known: dict[str, int] = {}          # path str → 上次尺寸
_stop = threading.Event()
_thread: Optional[threading.Thread] = None

#: 隔离副本体积预算（默认 20GiB；ECO_DB_QUAR_GB 可调）：隔离目录是
#: 「磁盘治理对象」不是无底洞——2026-11-22 实测 28 份共 137GiB，其中 27 份
#: 走过旧版非一致性抽检路径。应急三件套（ZEROED/SHRANK/VANISHED）
#: 不受此闸管（真灾难取证必须留）。
_QUAR_BUDGET = float(os.environ.get("ECO_DB_QUAR_GB") or 20.0) * (1024 ** 3)
#: 应急判据：此类保全绕过预算闸（WAL 常是唯一活着的数据）。
_FORENSIC_REASONS = ("ZEROED", "SHRANK", "VANISHED")


def _quarantine_root() -> Path:
    """隔离目录根。``ECO_DB_QUAR_ROOT`` 可指到归档盘（隔离副本体积大，
    2026-11-22 实测 28 份共 137GiB——与快照库同级的磁盘治理对象）。"""
    env = os.environ.get("ECO_DB_QUAR_ROOT", "").strip()
    if env:
        return Path(env)
    from . import card_store
    return card_store.WORKS_DIR.parent / "db_quarantine"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _alert_file(msg: str) -> None:
    """告警落一份到 logs/（运维面）。"""
    try:
        from . import card_store
        logs = card_store.WORKS_DIR.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"db_alert_{_stamp()}.txt").write_text(msg, encoding="utf-8")
    except OSError:
        pass


def _incidents_path() -> Path:
    return _quarantine_root() / "_incidents.json"


def _load_incidents() -> dict[str, dict]:
    try:
        data = json.loads(_incidents_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_incidents(inc: dict[str, dict]) -> None:
    path = _incidents_path()
    pending = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps(inc, ensure_ascii=False, indent=1), encoding="utf-8")
        pending.replace(path)
    except OSError as exc:
        logger.error("[dbwatch] 事故台账写入失败: %s", exc)


def _dir_bytes(p: Path) -> int:
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


def _quarantine_free_bytes() -> float:
    """隔离区还剩多少预算字节（负值＝已超）。

    只算台账登记或带 .managed 标志的新取证目录。
    旧版遗留的 137GiB 历史副本不算进本预算（它们是治理对象不是新事故；
    不然迁完当天预算就被存量打满、新取证永远跳过）。
    ``dir`` 记相对名（本根内）或绝对路径（如快照故障指向原文件）——
    只把落在本根里的计入。"""
    root = _quarantine_root().resolve()
    registered = {str(rec.get("dir") or "") for rec in _load_incidents().values()}
    used = 0
    if root.is_dir():
        for p in root.iterdir():
            if p.is_dir() and p.resolve().parent == root and (
                    p.name in registered or str(p) in registered or (p / ".managed").is_file()):
                used += _dir_bytes(p)
    return _QUAR_BUDGET - used


def _preserve(db: Path, reason: str, prev: int, detail: str,
              consistent: bool = False) -> None:
    """异常副本保全。

    〔2026-11-22 治理〕三闸：
    1. **事故并案**：同库同判据的未结事故只更台账（count/last），不再
       复制一份——此前每次抽检假失败都留一份几 GiB 副本（137GiB 事故源）。
    2. **体积预算**：隔离总量 ``ECO_DB_QUAR_GB``；超了不复制（仍告警＋
       写明跳过原因）。应急判据（``_FORENSIC_REASONS``）豁免——那是
       真灾难取证，WAL 常是唯一活着的数据。
    3. ``consistent=True`` ⇒ 走 SQLite 一致备份接口拿副本（活文件三件套
       分时刻拼接是假损坏根源）；False ⇒ 应急原样直拷（WAL 优先）。
    """
    key = str(db)
    inc = _load_incidents()
    rec = inc.get(key)
    same_case = rec and rec.get("reason") == reason and not rec.get("resolved")
    now = datetime.now().isoformat(timespec="seconds")
    first = rec.get("first", now) if same_case else now
    count = int(rec.get("count") or 1) + 1 if same_case else 1
    if same_case and rec.get("result") != "skipped":
        rec["count"] = count
        rec["last"] = now
        _save_incidents(inc)
        msg = (f"[dbwatch] 库异常 {reason} 并案(第{rec['count']}次): {db} {detail}"
               f" → 沿用 {rec.get('dir')}（不再复制）")
        logger.critical(msg)
        _alert_file(msg)
        return

    need = 0
    for suffix in ("-wal", "", "-shm"):
        src = db.with_name(db.name + suffix)
        try:
            if src.exists():
                need += src.stat().st_size
        except OSError:
            pass
    forensic = reason in _FORENSIC_REASONS
    over = (not forensic) and need > _quarantine_free_bytes()
    root = _quarantine_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        disk_short = need > shutil.disk_usage(root).free
    except OSError:
        disk_short = True
    qdir = root / f"{_stamp()}_{db.parent.name}_{db.stem}"
    n = 1
    while qdir.exists():   # 同秒同库再发案 ⇒ 目录加序号不并错
        n += 1
        qdir = root / f"{_stamp()}_{db.parent.name}_{db.stem}_{n}"
    if over or disk_short:
        why = "目标盘不可用或空间不足" if disk_short else "新取证预算不足"
        inc[key] = {"reason": reason, "result": "skipped", "skip_reason": why,
                    "need_bytes": need, "first": first, "last": now,
                    "count": count, "resolved": False}
        _save_incidents(inc)
        msg = f"[dbwatch] 库异常 {reason}: {db} {detail} → 隔离跳过（{why}，需 {need}B）"
        logger.critical(msg)
        _alert_file(msg)
        return
    complete = False
    failed: list[str] = []
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / ".managed").touch()
        if consistent:
            # 一致备份副本（单文件）：开活库 backup() 到隔离目录。
            # 开活库连接本身安全（日常连接常态如此）；backup() 对并发写安全。
            src_con = dst_con = None
            try:
                src_con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True,
                                          timeout=15.0)
                dst_con = sqlite3.connect(str(qdir / db.name))
                src_con.backup(dst_con)
                complete = True
            except sqlite3.Error as exc:
                # backup 失败 ≠ 崩溃哨兵——记 partial 取证事实
                failed.append(f"backup:{exc}")
            finally:
                for c in (dst_con, src_con):
                    try:
                        if c is not None:
                            c.close()
                    except Exception:  # noqa: BLE001
                        pass
        else:
            # 〔2026-11-22 返修〕complete 只认**应拷尽拷**：存在的三件套
            # 全部拷成才算完整；只出 WAL/主库失败都标 partial＋缺件清单。
            expected = [s for s in ("-wal", "", "-shm")
                        if db.with_name(db.name + s).exists()]
            for suffix in expected:
                src = db.with_name(db.name + suffix)
                try:
                    shutil.copy2(src, qdir / src.name)
                except OSError as exc:
                    failed.append(suffix or ".db")
                    logger.error("[dbwatch] 隔离拷贝失败 %s: %s", src, exc)
            if "" not in expected:
                failed.append("missing:.db")
            complete = not failed
        (qdir / "_NOTE.txt").write_text(
            f"reason={reason}\ndb={db}\nprev_size={prev}\ndetail={detail}\n"
            f"consistent={consistent}\n"
            f"result={'complete' if complete else 'partial'}\n"
            f"failed={','.join(failed) if failed else '-'}\n"
            f"time={datetime.now().isoformat()}\n", encoding="utf-8")
    except OSError as exc:
        complete = False
        logger.error("[dbwatch] 隔离目录建立失败 %s: %s", qdir, exc)
    inc[key] = {"reason": reason, "dir": qdir.name,
                "result": "complete" if complete else "partial",
                "first": first,
                "last": datetime.now().isoformat(timespec="seconds"),
                "count": count, "resolved": False}
    _save_incidents(inc)
    msg = (f"[dbwatch] 库异常 {reason}: {db} (前次尺寸 {prev}B) {detail} "
           f"→ 隔离{'完整' if complete else '未完整'} {qdir}")
    logger.critical(msg)
    _alert_file(msg)


def _resolve_incident_key(key: str, reasons: tuple[str, ...]) -> None:
    """按台账 key 结案（快照案 ``snap:`` 前缀键、活库案库路径键）。"""
    inc = _load_incidents()
    rec = inc.get(key)
    if rec and rec.get("reason") in reasons and not rec.get("resolved"):
        rec["resolved"] = True
        rec["resolved_at"] = datetime.now().isoformat(timespec="seconds")
        _save_incidents(inc)


def _resolve_incident(db: Path, reasons: tuple[str, ...]) -> None:
    """活库恢复健康/活库抽检通过 ⇒ 同库这些判据的未结事故标记 resolved。

    **只销库路径键**——快照案（``snap:`` 键）与它同库不同主体，不归本闸管。
    """
    _resolve_incident_key(str(db), reasons)


def _scan_roots() -> list[Path]:
    """要盯的库目录：data/{wid} ＋ 迁移期兼容 works/{wid}。"""
    from . import card_store
    roots = []
    for base in (card_store.db_root(), card_store.WORKS_DIR):
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and d.name != "db_quarantine":
                    roots.append(d)
    return roots


def _tick() -> None:
    for wd in _scan_roots():
        for db in wd.glob("*.db"):
            if not db.name.endswith(".db"):
                continue
            key = str(db)
            try:
                size = db.stat().st_size
            except OSError:
                continue  # 列出后消失：下轮以 VANISHED 捕获（若曾登记）
            prev = _known.get(key)
            wal = db.with_name(db.name + "-wal")
            wal_sz = 0
            try:
                wal_sz = wal.stat().st_size if wal.exists() else 0
            except OSError:
                pass
            if prev is not None:
                if size == 0 and prev > 0:
                    _preserve(db, "ZEROED", prev,
                              f"主文件从 {prev}B 归零，WAL {wal_sz}B")
                elif size < prev * _SHRINK_RATIO and prev - size >= _SHRINK_MIN_BYTES:
                    _preserve(db, "SHRANK", prev,
                              f"{prev}B → {size}B，WAL {wal_sz}B")
            _known[key] = size
        # 消失检测：登记过的 path 这轮 stat 不到
        for key in [k for k in list(_known) if Path(k).parent == wd]:
            p = Path(key)
            if not p.exists():
                wal = p.with_name(p.name + "-wal")
                wal_ok = wal.exists() and wal.stat().st_size > 0
                if wal_ok:
                    _preserve(p, "VANISHED", _known.pop(key, 0),
                              "库消失但 WAL 仍在（数据只剩 WAL）")
                else:
                    _known.pop(key, None)   # 整套没了（正常清理也算），不再盯


def _sweep_probe_temps(max_age_s: float = 3600.0) -> int:
    """清掉 ``dbwatch_*`` 残留临时目录（连接未关 → rmtree 被吞的历史产物，
    2026-11-22 实测单个残留 7.64GiB）。只清超时老目录（在途的不碰）。"""
    removed = 0
    tmp_root = Path(tempfile.gettempdir())
    try:
        for d in tmp_root.iterdir():
            if not (d.is_dir() and d.name.startswith("dbwatch_")):
                continue
            try:
                if time.time() - d.stat().st_mtime < max_age_s:
                    continue
                shutil.rmtree(d)
                removed += 1
            except OSError as exc:
                logger.warning("[dbwatch] 残留抽检目录清理失败 %s: %s", d, exc)
    except OSError:
        pass
    return removed


def _check_db_file(db_path: Path) -> str:
    """对**静态**库文件跑 ``quick_check(1)``；返回 "ok" 或错误描述。"""
    con = None
    try:
        con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
        row = con.execute("PRAGMA quick_check(1)").fetchone()
        return "ok" if row and str(row[0]).lower() == "ok" \
            else str(row[0] if row else "?")
    except sqlite3.Error as exc:
        return f"open/quick_check failed: {exc}"
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:  # noqa: BLE001
            pass


def _latest_daily_db_copies() -> list[tuple[str, Path]]:
    """各 work 最新一份日备里的一致性整库副本（``VACUUM INTO`` 产物）。

    返回 ``(work_id, 副本路径)``——wid 直接取快照根的子目录名，
    不从子树路径深度推导（布局变也不受影响）。
    找不到快照根/日备 ⇒ 空表（调用方走活库一致备份抽检）。
    """
    try:
        from . import data_snapshot
        root = data_snapshot.SNAPSHOT_ROOT
    except Exception:  # noqa: BLE001
        return []
    found: list[tuple[str, Path]] = []
    try:
        if not root.is_dir():
            return []
        for wd in root.iterdir():
            if not wd.is_dir():
                continue
            dailies = sorted((p for p in wd.iterdir() if p.name.startswith("daily_")),
                             reverse=True)
            for daily in dailies:
                files = data_snapshot.daily_snapshot_files(daily)
                if files is not None:
                    found.extend((wd.name, p) for p in files if p.suffix == ".db")
                    break
    except OSError:
        pass
    return found


def _probe_via_backup(db: Path) -> str | None:
    """活库 → SQLite ``backup()`` 一致副本（临时目录）→ quick_check。

    以 SQLite 当前页数预算，包含 WAL 中已提交的页面。空间或资源不足时返回
    None 暂缓检查，避免把环境问题当作数据库损坏再复制一份。
    """
    tmp = None
    src = dst = None
    try:
        src = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=15.0)
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        needed = src.execute("PRAGMA page_count").fetchone()[0] * page_size
        root = _quarantine_root()
        root.mkdir(parents=True, exist_ok=True)
        budget = min(_quarantine_free_bytes(), shutil.disk_usage(root).free)
        if needed > budget:
            logger.warning("[dbwatch] 抽检暂缓 %s: 副本需 %dB，可用预算 %.0fB",
                           db, needed, budget)
            return None
        tmp = Path(tempfile.mkdtemp(prefix="dbwatch_", dir=root))
        (tmp / ".managed").touch()
        dst_path = tmp / db.name
        dst = sqlite3.connect(str(dst_path))

        def check_budget(_status, _remaining, total):
            if total * page_size > budget:
                raise OSError("库增长超出抽检预算")

        src.backup(dst, pages=1024, progress=check_budget)
        dst.close()
        dst = None
        return _check_db_file(dst_path)
    except sqlite3.Error as exc:
        errcode = getattr(exc, "sqlite_errorcode", 0) & 255
        if errcode not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            logger.warning("[dbwatch] 抽检暂缓 %s: %s", db, exc)
            return None
        return f"open/backup failed: {exc}"
    except OSError as exc:
        logger.warning("[dbwatch] 抽检暂缓 %s: %s", db, exc)
        return None
    finally:
        for c in (dst, src):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001
                pass
        if tmp is not None:
            try:
                shutil.rmtree(tmp)
            except OSError as exc:
                logger.warning("[dbwatch] 抽检临时目录清理失败 %s: %s", tmp, exc)


def _integrity_probe() -> None:
    """周期完整性抽检（``ECO_DB_CHECK_S``）。

    〔2026-11-22 改判据〕优先**核验已发布的一致备份**（日备 ``VACUUM INTO``
    副本就在盘上，直接 ``quick_check``——零新增空间，查的是「恢复点到底
    能不能用」这一真问题）；没有日备的库才回落到活库 ``backup()`` 一致
    副本抽检。损坏结论现在指向真实一致副本，不再来自拼接假快照。
    """
    _sweep_probe_temps()
    snapshot_dbs = _latest_daily_db_copies()
    pending_forensic = {key for key, rec in _load_incidents().items()
                        if rec.get("reason") in _FORENSIC_REASONS
                        and not rec.get("resolved")}
    covered_live: set[str] = set()
    for wid, cp in snapshot_dbs:
        live = _find_live_db(wid, cp.name)
        result = _check_db_file(cp)
        # 〔2026-11-22 返修〕日备副本与活库**分别记账**：
        # 快照案 key="snap:{cp}"、reason=SNAP_INTEGRITY——那份恢复点坏是
        # 它自己的事，活库过检不能替它销案（反例复现：坏日备仍坏，活库
        # 检查通过却把案子结了）。
        skey = f"snap:{cp}"
        if result == "ok":
            if live is not None:
                covered_live.add(str(live))
            _resolve_incident_key(skey, ("SNAP_INTEGRITY",))
            continue
        # 恢复点损坏——副本本身就在盘上，直接指路，不再复制
        msg = (f"[dbwatch] 日备恢复点 quick_check 失败: {cp} ({result})"
               f"{' 活库=' + str(live) if live else ''}")
        logger.critical(msg)
        _alert_file(msg)
        inc = _load_incidents()
        rec = inc.get(skey)
        if rec and rec.get("reason") == "SNAP_INTEGRITY" \
                and not rec.get("resolved"):
            rec["count"] = int(rec.get("count") or 1) + 1
            rec["last"] = datetime.now().isoformat(timespec="seconds")
            _save_incidents(inc)
        else:
            inc[skey] = {"reason": "SNAP_INTEGRITY", "dir": str(cp),
                         "live": str(live) if live else "",
                         "first": datetime.now().isoformat(timespec="seconds"),
                         "last": datetime.now().isoformat(timespec="seconds"),
                         "count": 1, "resolved": False}
            _save_incidents(inc)
    for wd in _scan_roots():
        for db in wd.glob("*.db"):
            key = str(db)
            if key in covered_live:
                continue
            try:
                if not db.exists() or db.stat().st_size == 0:
                    continue
            except OSError:
                continue
            result = _probe_via_backup(db)
            if result is None:
                continue
            if result == "ok":
                # 结构健康只证明当前库可读，无法证明缩水或归零前的记录已恢复。
                _resolve_incident(db, ("INTEGRITY",))
                continue
            if key in pending_forensic:
                logger.warning("[dbwatch] 活库异常仍待处理 %s: %s", db, result)
                continue
            _preserve(db, "INTEGRITY", _known.get(str(db), -1),
                      f"quick_check(一致副本): {result}", consistent=True)


def _find_live_db(wid: str, name: str) -> Optional[Path]:
    """按 work_id＋库文件名找活库（works_data 新址 → works 旧址）。"""
    from . import card_store
    for base in (card_store.db_root() / wid, card_store.WORKS_DIR / wid):
        cand = base / name
        try:
            if cand.exists():
                return cand
        except OSError:
            pass
    return None


def _loop() -> None:
    last_check = time.time()
    while not _stop.wait(_INTERVAL):
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001 哨兵自己绝不能死
            logger.error("[dbwatch] stat 轮异常（下轮照跑）: %s", exc)
        if _CHECK_S > 0 and time.time() - last_check >= _CHECK_S:
            last_check = time.time()
            try:
                _integrity_probe()
            except Exception as exc:  # noqa: BLE001
                logger.error("[dbwatch] 抽检异常（下轮照跑）: %s", exc)


def start() -> bool:
    """起看门狗线程（幂等）。返回是否成功启动。"""
    global _thread
    if not _ENABLED:
        logger.info("[dbwatch] ECO_DB_WATCH=0，看门狗未启用")
        return False
    if _thread and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="db-watchdog", daemon=True)
    _thread.start()
    logger.warning("[dbwatch] 看门狗已启动：stat 每 %ss，抽检每 %ss",
                   _INTERVAL, _CHECK_S or "关")
    return True


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=_INTERVAL + 2)
