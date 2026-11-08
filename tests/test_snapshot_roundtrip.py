# -*- coding: utf-8 -*-
"""不变量二：快照 → 恢复往返一致（card_012 工作项 3）。

步骤二要动 data_snapshot.py（路径分家、增量、加锁），动之前必须先把
两条钉住：①往返后文件逐字节还原；②快照只装运行数据，不装代码。
"""
from __future__ import annotations

from pathlib import Path

from src.storage import card_store, data_snapshot


async def test_snapshot_batches_are_per_work(work):
    wid = work["work_id"]
    # [T-0001 §6-4] 指纹去重是既有行为：清指纹，确保本次快照真实执行（不是断言放宽）
    if data_snapshot._FINGERPRINT_FILE.exists():
        data_snapshot._FINGERPRINT_FILE.unlink()
    r = data_snapshot.snapshot_sync("batch-check")
    assert r["ok"] is True and r["files"] > 0 and r["skipped"] is False

    # 注意：返回的 dir 字段是 SNAPSHOT_ROOT/<stamp>，真实批次在
    # SNAPSHOT_ROOT/<work_id>/<stamp> —— 字段与真实路径不一致，已记入影子卡。
    stamp = Path(r["dir"]).name
    batch = data_snapshot.SNAPSHOT_ROOT / wid / stamp
    assert batch.is_dir()

    listed = data_snapshot.list_snapshots(wid)
    assert any(item["stamp"] == stamp and item["files"] > 0 for item in listed)


async def test_snapshot_restore_roundtrip(work):
    """[卡片存储迁移 SQLite] 快照装的是库文件 ``cards.db``（VACUUM INTO 一致副本），
    不再是卡片 md。往返后库内容逐字还原。"""
    import sqlite3

    wid, root = work["work_id"], work["work_card_id"]

    await card_store.update_card(wid, root, {"conclusion": "第一版正文"}, "写入第一版")
    # [T-0001 §6-4] 清指纹，确保本次快照真实执行（不是断言放宽）
    if data_snapshot._FINGERPRINT_FILE.exists():
        data_snapshot._FINGERPRINT_FILE.unlink()
    r = data_snapshot.snapshot_sync("roundtrip-A")
    assert r["ok"] is True and r["files"] > 0 and r["skipped"] is False

    stamp = Path(r["dir"]).name
    batch = data_snapshot.SNAPSHOT_ROOT / wid / stamp
    # [库搬出施工目录 2026-11-07] 真库在 works_data/{wid}/——批内路径同源；
    # restore 的 rel_path 仍传旧路径可测「两朝代回退」分支。
    rel = f"works_data/{wid}/cards.db"
    src = batch / rel
    assert src.exists(), f"批次内缺库副本: {src}"

    # 副本必须是**可用** SQLite 库（WAL 下直接 copy 会拿到不含最新提交的副本）
    con = sqlite3.connect(str(src))
    try:
        row = con.execute(
            "SELECT status_block FROM cards WHERE card_id = ?", (root,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None and "第一版正文" in (row[0] or "")

    # 快照只装运行数据（PROTECTED_DIRS = works/ + works_data/），代码不进快照
    tops = {
        p.relative_to(batch).parts[0]
        for p in batch.rglob("*") if p.is_file()
    }
    assert tops <= {"works", "works_data"}, tops

    # 改动之后，恢复必须回到快照态（库内容逐字还原）
    await card_store.update_card(wid, root, {"conclusion": "第二版正文"}, "写入第二版")
    assert (await card_store.read_card(wid, root))["body"]["status_block"] == "第二版正文"

    res = data_snapshot.restore_from_snapshot(wid, stamp, rel)
    assert res["ok"] is True
    data = await card_store.read_card(wid, root)
    # [销红 2026-10-12] C.4（S2-5 卡面新四键裁定）：body 读取侧须用 status_block
    # （写入侧 update_card 仍兼容旧键 conclusion 并归一到新键）。
    assert data["body"]["status_block"] == "第一版正文"


async def test_restore_missing_file_reports_error(work):
    wid = work["work_id"]
    r = data_snapshot.restore_from_snapshot(wid, "20000101_000000_000000", "works/x.md")
    assert r["ok"] is False and "error" in r