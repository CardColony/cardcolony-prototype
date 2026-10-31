# -*- coding: utf-8 -*-
"""从数据快照恢复（works/ 退出 git 追踪后的唯一回退通道）。

用法：
  python restore_from_snapshot.py --list                    # 列出全部快照批次
  python restore_from_snapshot.py --list --work work_001     # 列某 work 的批次
  python restore_from_snapshot.py --work work_001 --stamp 20261003_153122_628962
                                                             # 恢复整个批次（先备份当前状态）
  python restore_from_snapshot.py --work work_001 --stamp <批次> --file works/work_001/cards/card_007.md
                                                             # 只恢复单个文件

安全设计：
  - 恢复前先把「当前 works/」整体备份到 D:\\AI_station\\推演agent_backups\\pre_restore_<时间戳>\\
  - 恢复是逐文件 copy2，不做删除（即只补不删；若需删文件请手动）
  - **例外**：``.db``（卡片库）走 SQLite **backup API** 回写内容，不走 copy2——
    WAL 模式下 live 库旁的 ``cards.db-wal`` 常被进程占着删不掉，而**删不掉的旧 WAL
    会把旧数据复活**（文件换成快照内容、一读却是旧值 ⇒ 恢复看着成功、实际没回滚）。
  - 〔2026-10-30〕**过程批次的库只含「会变的表」**（只追加的会话原文块/事件流/
    回收站不进过程批次，只在日备里）⇒ 恢复它**必须逐表合并**（历史表不动），
    整文件替换会一次抹掉 live 库几百 MB 的会话历史。判据：库里没有那几张历史表。
  - 全程不碰 git（正是为了摆脱 git 分支操作的连带伤害）
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
SNAPSHOT_ROOT = BASE_DIR.parent / "推演agent_data_snapshots"
BACKUP_ROOT = BASE_DIR.parent / "推演agent_backups"
_CST = timezone(timedelta(hours=8))

# 〔2026-10-30〕**过程批次的库只含状态表**（只追加的会话原文块/事件流/回收站
# 不进过程批次）⇒ 整文件替换会把 live 库的会话历史一起抹掉 ⇒ 状态库必须
# **逐表合并**恢复（历史表不动）。判据与合并逻辑复用模块里的那一份，避免两处实现。
from src.storage.data_snapshot import (  # noqa: E402
    _is_state_only_db,
    _restore_state_db,
)


def _restore_db(src: Path, dst: Path) -> str | None:
    """把快照里的 SQLite 库回写进 live 库。

    走 backup API（不是复制文件）：复制文件后，**删不掉的旧 WAL 会把旧数据复活**
    ⇒ 恢复返回成功、内容却没回滚（静默失败，实测踩过）。
    返回 ``None``＝成功；返回错误串＝失败（**如实报，不装成功**）。
    """
    if _is_state_only_db(src):
        # 〔2026-10-30〕状态库（过程批次）⇒ **合并**恢复：只覆盖状态表，
        # 历史表（session_blocks/work_events）不动 ⇒ 否则恢复一次抹掉 440MB 历史。
        return _restore_state_db(src, dst)
    scon = dcon = None
    try:
        scon = sqlite3.connect(str(src))
        dcon = sqlite3.connect(str(dst), timeout=10.0)
        dcon.execute("PRAGMA busy_timeout=10000")
        scon.backup(dcon)
        # 内容已对齐 ⇒ 旁挂文件只是占地方，删不掉也不影响正确性
        for suffix in ("-wal", "-shm"):
            side = dst.with_name(dst.name + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    finally:
        for con in (scon, dcon):
            try:
                if con is not None:
                    con.close()
            except Exception:  # noqa: BLE001
                pass


def _restore_one(src: Path, dst: Path) -> str | None:
    """恢复单个文件；``.db`` 走 backup API，其余 copy2。返回错误串或 None。"""
    if dst.suffix == ".db":
        return _restore_db(src, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return None


def _stamp() -> str:
    return datetime.now(_CST).strftime("%Y%m%d_%H%M%S")


def list_batches(work: str | None = None) -> list[tuple[str, str, int]]:
    out = []
    if not SNAPSHOT_ROOT.exists():
        return out
    dirs = [SNAPSHOT_ROOT / work] if work else [p for p in SNAPSHOT_ROOT.iterdir() if p.is_dir()]
    for wd in dirs:
        if not wd.exists():
            continue
        for b in sorted(wd.iterdir()):
            if b.is_dir():
                out.append((wd.name, b.name, sum(1 for _ in b.rglob("*") if _.is_file())))
    return out


def backup_current() -> Path:
    dst = BACKUP_ROOT / f"pre_restore_{_stamp()}"
    dst.mkdir(parents=True, exist_ok=True)
    w = BASE_DIR / "works"
    if w.exists():
        shutil.copytree(w, dst / "works", dirs_exist_ok=True)
    return dst


def restore_batch(work: str, stamp: str) -> dict:
    src = SNAPSHOT_ROOT / work / stamp
    if not src.exists():
        return {"ok": False, "error": f"快照批次不存在: {src}"}
    bk = backup_current()
    n, skipped = 0, 0
    errors: list[str] = []
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)          # 形如 works/work_001/cards/card_007.md
        dst = BASE_DIR / rel
        if dst.exists():
            skipped += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        err = _restore_one(p, dst)
        if err:
            errors.append(f"{rel}: {err}")
            continue
        n += 1
    # 失败如实报：恢复没生效就不能说「恢复成功」
    return {"ok": not errors, "files": n, "overwritten": skipped, "backup": str(bk),
            "from": str(src), **({"errors": errors} if errors else {})}


def restore_file(work: str, stamp: str, rel: str) -> dict:
    src = SNAPSHOT_ROOT / work / stamp / rel
    if not src.exists():
        return {"ok": False, "error": f"快照内不存在该文件: {src}"}
    dst = BASE_DIR / rel
    if dst.exists():
        bk = backup_current()
    else:
        bk = None
    dst.parent.mkdir(parents=True, exist_ok=True)
    err = _restore_one(src, dst)
    if err:
        return {"ok": False, "error": f"恢复失败（live 库内容未回滚）: {err}",
                "to": str(dst), "backup": str(bk) if bk else "（原文件不存在，无需备份）"}
    return {"ok": True, "to": str(dst), "size": dst.stat().st_size,
            "backup": str(bk) if bk else "（原文件不存在，无需备份）"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--work")
    ap.add_argument("--stamp")
    ap.add_argument("--file")
    a = ap.parse_args()

    if a.list or not a.stamp:
        print(f"快照根: {SNAPSHOT_ROOT}")
        rows = list_batches(a.work)
        if not rows:
            print("  （无批次）")
        for w, s, n in rows:
            print(f"  {w:<14}{s:<28}{n} 文件")
        return

    if a.file:
        r = restore_file(a.work, a.stamp, a.file)
    else:
        r = restore_batch(a.work, a.stamp)
    print(r)


if __name__ == "__main__":
    main()
