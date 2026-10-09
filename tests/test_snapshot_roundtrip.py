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
    wid, root = work["work_id"], work["work_card_id"]

    await card_store.update_card(wid, root, {"conclusion": "第一版正文"}, "写入第一版")
    # [T-0001 §6-4] 清指纹，确保本次快照真实执行（不是断言放宽）
    if data_snapshot._FINGERPRINT_FILE.exists():
        data_snapshot._FINGERPRINT_FILE.unlink()
    r = data_snapshot.snapshot_sync("roundtrip-A")
    assert r["ok"] is True and r["files"] > 0 and r["skipped"] is False

    stamp = Path(r["dir"]).name
    batch = data_snapshot.SNAPSHOT_ROOT / wid / stamp
    rel = f"works/{wid}/cards/{root}.md"
    src = batch / rel
    card_path = card_store._card_path(wid, root)

    assert src.exists(), f"批次内缺该卡: {src}"
    assert src.read_bytes() == card_path.read_bytes()
    assert "第一版正文" in src.read_text(encoding="utf-8")

    # 快照只装运行数据（PROTECTED_DIRS = works/），代码不进快照
    tops = {
        p.relative_to(batch).parts[0]
        for p in batch.rglob("*") if p.is_file()
    }
    assert tops == {"works"}, tops

    # 改动之后，恢复必须逐字节回到快照态
    await card_store.update_card(wid, root, {"conclusion": "第二版正文"}, "写入第二版")
    assert card_path.read_bytes() != src.read_bytes()

    res = data_snapshot.restore_from_snapshot(wid, stamp, rel)
    assert res["ok"] is True
    assert card_path.read_bytes() == src.read_bytes()

    data = await card_store.read_card(wid, root)
    assert data["body"]["conclusion"] == "第一版正文"
    # 恢复回来的文件哈希自洽 → 不得被判定为「外部修改」
    assert not any("外部修改" in line for line in data["body"]["history"])


async def test_restore_missing_file_reports_error(work):
    wid = work["work_id"]
    r = data_snapshot.restore_from_snapshot(wid, "20000101_000000_000000", "works/x.md")
    assert r["ok"] is False and "error" in r