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
   把三件套**拷到临时目录**再开副本跑 ``PRAGMA quick_check(1)``——
   绝不在活文件上开 SQLite（0 字节主文件+活 WAL 被打开会触发恢复行为，
   实测会把 WAL 消费掉）。副本校验失败同样进隔离+告警。
4. **告警面**：``logger.critical``（进 server.log）＋ 隔离目录 ``_NOTE.txt``
   ＋ ``logs/db_alert_{stamp}.txt``（给运维一眼能看到的地方）。

环境变量：
  ``ECO_DB_WATCH=0``       关整个看门狗（默认开）
  ``ECO_DB_WATCH_S``       stat 轮询间隔秒（默认 5）
  ``ECO_DB_CHECK_S``       完整性抽检间隔秒（默认 3600；0 ＝ 关）
"""
from __future__ import annotations

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
_alerted: set[str] = set()           # path str → 已告警（恢复前不重报）
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _quarantine_root() -> Path:
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


def _preserve(db: Path, reason: str, prev: int, detail: str) -> None:
    """把 db 三件套原样拷进隔离目录（WAL 先拷——它常是唯一活着的数据）。"""
    qdir = _quarantine_root() / f"{_stamp()}_{db.parent.name}_{db.stem}"
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "", "-shm"):   # wal 优先
            src = db.with_name(db.name + suffix)
            if src.exists():
                try:
                    shutil.copy2(src, qdir / src.name)
                except OSError as exc:
                    logger.error("[dbwatch] 隔离拷贝失败 %s: %s", src, exc)
        (qdir / "_NOTE.txt").write_text(
            f"reason={reason}\ndb={db}\nprev_size={prev}\ndetail={detail}\n"
            f"time={datetime.now().isoformat()}\n", encoding="utf-8")
    except OSError as exc:
        logger.error("[dbwatch] 隔离目录建立失败 %s: %s", qdir, exc)
    msg = (f"[dbwatch] 库异常 {reason}: {db} (前次尺寸 {prev}B) {detail} "
           f"→ 已隔离到 {qdir}")
    logger.critical(msg)
    _alert_file(msg)


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
            if prev is not None and key not in _alerted:
                if size == 0 and prev > 0:
                    _alerted.add(key)
                    _preserve(db, "ZEROED", prev,
                              f"主文件从 {prev}B 归零，WAL {wal_sz}B")
                elif size < prev * _SHRINK_RATIO and prev - size >= _SHRINK_MIN_BYTES:
                    _alerted.add(key)
                    _preserve(db, "SHRANK", prev,
                              f"{prev}B → {size}B，WAL {wal_sz}B")
            if size > 0 and key in _alerted:
                _alerted.discard(key)   # 恢复正常（如恢复操作后），警报复位
            _known[key] = size
        # 消失检测：登记过的 path 这轮 stat 不到
        for key in [k for k in list(_known) if Path(k).parent == wd]:
            p = Path(key)
            if not p.exists() and key not in _alerted:
                wal = p.with_name(p.name + "-wal")
                wal_ok = wal.exists() and wal.stat().st_size > 0
                if wal_ok:
                    _alerted.add(key)
                    _preserve(p, "VANISHED", _known.pop(key, 0),
                              "库消失但 WAL 仍在（数据只剩 WAL）")
                else:
                    _known.pop(key, None)   # 整套没了（正常清理也算），不再盯


def _integrity_probe() -> None:
    """副本抽检：拷三件套到临时目录 → 开副本 quick_check。绝不碰活文件。"""
    for wd in _scan_roots():
        for db in wd.glob("*.db"):
            try:
                if not db.exists() or db.stat().st_size == 0:
                    continue
            except OSError:
                continue
            tmp = Path(tempfile.mkdtemp(prefix="dbwatch_"))
            try:
                for suffix in ("", "-wal", "-shm"):
                    src = db.with_name(db.name + suffix)
                    if src.exists():
                        shutil.copy2(src, tmp / src.name)
                cp = tmp / db.name
                try:
                    con = sqlite3.connect(str(cp), timeout=10.0)
                    row = con.execute("PRAGMA quick_check(1)").fetchone()
                    con.close()
                    bad = row and str(row[0]).lower() not in ("ok",)
                except sqlite3.Error as exc:
                    bad, row = True, (f"open/quick_check failed: {exc}",)
                if bad:
                    _preserve(db, "INTEGRITY", _known.get(str(db), -1),
                              f"quick_check: {row[0] if row else '?'}")
            except OSError as exc:
                logger.warning("[dbwatch] 抽检副本失败 %s: %s", db, exc)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


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
