# -*- coding: utf-8 -*-
"""[function 实体化第一步 · card_028] 判据④实证：五张 function_* 表在**存量**
work（work_001）上首次连接自动补建，且原有数据一条不变。

做法（对生产零写入）：
1. 用 sqlite3 backup API 把真实库（含 WAL 合并）拷到临时目录快照；
2. 记录快照里全部既有表的逐表计数（前后对比基线）；
3. 用**新代码**（card_db._get_conn → _ensure_schema → _ensure_tables）对快照
   建立连接——首次连接即补建；
4. 复核：五张 function_* 表已建；既有表逐表计数与基线一致；
5. 再对照**真实生产库**（只读连接）的计数，证明快照忠实反映存量。

只读核实生产库：mode=ro 连接，不写生产一个字节。快照的补建发生在临时目录。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # .worktrees/5713
sys.path.insert(0, str(ROOT))

WORK_ID = "work_001"
REAL_WORKS = ROOT.parent.parent / "works"          # 推演agent/works
REAL_DB = REAL_WORKS / WORK_ID / "cards.db"

FUNCTION_TABLES = (
    "function_profiles", "function_verdicts", "function_alarms",
    "function_sync", "function_charter_history",
)


def counts_of(db_path: Path, only_existing: bool = False) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        out: dict[str, int] = {}
        for t in sorted(tables):
            if t.startswith("sqlite_"):
                continue
            if only_existing and t in FUNCTION_TABLES:
                continue  # 基线口径：函数表不进「既有表」集合
            out[t] = int(conn.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0])
        return out
    finally:
        conn.close()


def meta_of(db_path: Path) -> dict[str, str]:
    """meta 逐键值（含 WAL 合并后的最新态）。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return {str(k): str(v) for k, v in
                conn.execute("SELECT key, value FROM meta").fetchall()}
    finally:
        conn.close()


def main() -> int:
    import json
    from src.storage import card_db

    assert REAL_DB.exists(), f"存量库不存在: {REAL_DB}"
    print(f"[1] 存量库: {REAL_DB}")

    tmp = Path(tempfile.mkdtemp(prefix="c28_fn_migrate_"))
    snap_dir = tmp / WORK_ID
    snap_dir.mkdir(parents=True)
    snap_db = snap_dir / "cards.db"

    # —— 拷快照（backup API 自动合并 WAL，页级一致）——
    src = sqlite3.connect(str(REAL_DB))
    dst = sqlite3.connect(str(snap_db))
    src.backup(dst)
    dst.close()
    src.close()
    print(f"[2] 快照（含 WAL 合并）: {snap_db}")

    # —— 基线：既有表逐表计数（快照上）——
    before = counts_of(snap_db, only_existing=True)
    real_now = counts_of(REAL_DB)
    assert before == real_now, "快照与生产库计数不一致，快照不可信"
    print(f"[3] 基线计数（与生产库只读核对一致）:")
    print(json.dumps(before, ensure_ascii=False, indent=2))

    # —— 新代码首次连接（走 _get_conn → _ensure_schema → _ensure_tables）——
    card_db.reset_caches()
    # 把 card_store.WORKS_DIR 指到快照目录（不 patch 的话 db_path 会指向真实 works）
    from src.storage import card_store
    card_store.WORKS_DIR = tmp / "works"
    (card_store.WORKS_DIR / WORK_ID).mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.move(str(snap_db), str(card_store.WORKS_DIR / WORK_ID / "cards.db"))

    def _touch(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])

    n = card_db.read_sync(WORK_ID, _touch, -1)
    assert n == before.get("cards"), "连接后 cards 计数异常"
    print(f"[4] 新代码首次连接成功（cards={n}）——_ensure_tables 已跑")

    # —— 复核 ——
    snap_after = card_store.WORKS_DIR / WORK_ID / "cards.db"
    after = counts_of(snap_after)
    for t in FUNCTION_TABLES:
        assert t in after, f"五表缺 {t}"
        assert after[t] == 0, f"{t} 应为空（只建表不动数据）"
    print("[5] 五张 function_* 表全部建出，且全空（只补建、不写数据）")

    unchanged = {t: c for t, c in after.items() if t not in FUNCTION_TABLES}
    assert unchanged == before, f"存量数据被改动！\n前={before}\n后={unchanged}"
    print("[6] 既有表逐表计数前后一致（原有数据一条不变）")
    print(json.dumps(unchanged, ensure_ascii=False, indent=2))

    # —— 用 function_store 在快照上建一个职能，证明可用 ——
    # 注意：建职能是**新业务写入**，不属判据④「首次连接补建」的数据不变断言——
    # 那一步（[6]）已证明存量一条不变。这里只验证：内容表零触碰；meta 只新增
    # fn_seq、rev 恰 +1（照 cards 建卡的记账先例），旧键值一律不变。
    import asyncio
    from src.storage import function_store
    meta_before = meta_of(snap_after)
    prof = asyncio.run(function_store.create_function(WORK_ID, "迁移实证职能"))
    assert prof["function_id"] == "fn_001"
    after2 = counts_of(snap_after)
    assert after2["function_profiles"] == 1
    for t in before:
        if t == "meta":
            continue
        assert after2[t] == before[t], f"建职能后存量表 {t} 被改动"
    assert after2["cards"] == before["cards"], "职能混进了 cards 表"
    meta_after = meta_of(snap_after)
    old_rev = int(meta_before.get("rev", "0"))
    assert meta_after.get("rev") == str(old_rev + 1), \
        f"rev 应恰 +1: {meta_before.get('rev')} → {meta_after.get('rev')}"
    for k, v in meta_before.items():
        if k == "rev":
            continue
        assert meta_after.get(k) == v, f"meta 旧键 {k} 被改动: {v} → {meta_after.get(k)}"
    assert meta_after.get("fn_seq") == "1"
    print(f"[7] 快照上建职能 {prof['function_id']} 成功；存量内容表一条不变；"
          f"meta 仅新增 fn_seq=1、rev 恰 +1；cards 仍 {after2['cards']} 行"
          f"（职能未进卡片世界）")

    card_db.reset_caches()
    print("\n判据④ PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
