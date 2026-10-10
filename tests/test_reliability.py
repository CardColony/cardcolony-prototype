# -*- coding: utf-8 -*-
"""可靠性件回归（迭代一·会话底座 · S3/A3：fsync 原子写＋打开体检＋损坏处置＋ops 通道）。

判据（机械可判定，依据设计 4.3/T078-T082 ＋ 施工件 S3）：
1. 覆盖路径 _atomic_write_text：tmp 中间名不残留、内容逐字一致（T079）；
2. 打开体检 health_check：头存在性／版本可识别／块闭合性／seq 单调性，
   V2 全量口径、V1 按不适用报告（T080）；
3. 损坏处置 repair_damaged_tail：尾部未闭合 → 残块旁路 damaged/ 留档
   （**永不删**）→ 从末个完好块重建；完好/V1 不动（T081）；
4. ops jsonl 通道：五键 ts/action/work/card/detail，append/upgrade/
   health_check/damage_repair 全留痕（T082/T152 家族）；
5. 体检与处置报告进 ops 通道。
零真实 works 触碰（conftest 铁律一）；不 import main（铁律二）。
"""
from __future__ import annotations

import json

import pytest

from src.storage import revision_store, session_facade, session_ops, session_store

W = "work_reli"


async def _seed_two_blocks(work: str = W) -> None:
    """经门面正常追加两块（user＋assistant），得到完好 V2 文件。"""
    await session_facade.append(work, "card_001", {"role": "user", "content": "问题一"})
    await session_facade.append(work, "card_001", {"role": "assistant", "content": "回答一"})


# ---------- 1：原子写 ----------

def test_atomic_write_leaves_no_tmp(sandbox):
    p = session_store.WORKS_DIR / W / "_probe" / "x.json"
    session_facade._atomic_write_text(p, "内容甲")
    assert p.read_text(encoding="utf-8") == "内容甲"
    assert not p.with_name(p.name + ".tmp").exists(), "tmp 中间名不得残留（T079）"


# ---------- 2：打开体检 ----------

async def test_health_check_v2_intact(sandbox, work):
    await _seed_two_blocks(work["work_id"])
    r = session_facade.health_check_sync(work["work_id"], "card_001")
    assert r["exists"] is True and r["format"] == 2
    assert r["header_ok"] is True and r["version_ok"] is True
    assert r["blocks"] == 2
    assert r["damaged_count"] == 0 and r["seq_monotonic"] is True
    assert r["unclosed"] == 0


async def test_health_check_reports_damaged_blocks(sandbox, work):
    await _seed_two_blocks(work["work_id"])
    path = session_store._session_path(work["work_id"], "card_001")
    # 手工追加：一个缺 seq 的残块（必字段缺失 → damaged 占位）
    # ＋一个 seq 断号块（字段齐全不 damaged，但破坏单调性检查）
    text = path.read_text(encoding="utf-8")
    text += "---MSG---\nactor: user\nturn: 9\n---\n残块正文\n---END---\n"
    text += "---MSG---\nseq: 5\nactor: user\nturn: 9\n---\n断号块\n---END---\n"
    path.write_text(text, encoding="utf-8")
    r = session_facade.health_check_sync(work["work_id"], "card_001")
    assert r["damaged_count"] == 1
    assert r["damaged"][0]["reason"] == "missing:seq"
    assert r["seq_monotonic"] is False  # 0,1 之后断号到 5


async def test_health_check_v1_reports_not_applicable(sandbox, work):
    wid = work["work_id"]
    path = session_store._session_path(wid, "card_001")
    path.write_text("创建时间：2026-10-10\n\n---MSG---\nrole: user\n\n你好\n", encoding="utf-8")
    r = session_facade.health_check_sync(wid, "card_001")
    assert r["format"] == 1
    assert r["header_ok"] is None and r["version_ok"] is None  # V1 不适用
    assert r["seq_monotonic"] is None and r["damaged_count"] == 0


def test_health_check_missing_file(sandbox):
    r = session_facade.health_check_sync(W, "card_404")
    assert r == {"exists": False, "format": None}


# ---------- 3：损坏处置 ----------

async def test_repair_damaged_tail_recovers(sandbox, work):
    wid = work["work_id"]
    await _seed_two_blocks(wid)
    path = session_store._session_path(wid, "card_001")
    good = path.read_text(encoding="utf-8")
    # 模拟写撕裂：完好全文 ＋ 半个残块（无 ---END---）
    path.write_text(good + "---MSG---\nseq: 2\nactor: assistant\nturn: 1\n---\n被撕断的半截",
                    encoding="utf-8")
    r = session_facade.repair_damaged_tail(wid, "card_001")
    assert r["exists"] is True and r["repaired"] is True
    assert len(r["fragments"]) == 1, "残块必须旁路留档"
    # 残块文件真实存在（移动留档，永不删）
    frag = session_store.WORKS_DIR / wid / "damaged" / r["fragments"][0]
    assert frag.exists() and "被撕断的半截" in frag.read_text(encoding="utf-8")
    # 重建后：两完好块保留、残块消失、体检恢复单调
    after = session_facade.health_check_sync(wid, "card_001")
    assert after["blocks"] == 2 and after["seq_monotonic"] is True
    assert after["unclosed"] == 0


async def test_repair_noop_on_intact_and_v1(sandbox, work):
    wid = work["work_id"]
    await _seed_two_blocks(wid)
    r = session_facade.repair_damaged_tail(wid, "card_001")
    assert r["repaired"] is False, "完好文件不处置"
    # V1：无 damaged 概念，不处置
    path = session_store._session_path(wid, "card_002")
    path.write_text("创建时间：x\n\n---MSG---\nrole: user\n\n旧格式\n", encoding="utf-8")
    r2 = session_facade.repair_damaged_tail(wid, "card_002")
    assert r2["format"] == 1 and r2["repaired"] is False


def test_repair_missing_file(sandbox):
    r = session_facade.repair_damaged_tail(W, "card_404")
    assert r == {"exists": False, "repaired": False, "fragments": []}


# ---------- 4/5：ops 通道 ----------

def test_ops_log_five_keys(sandbox):
    session_ops.ops_log("probe_action", W, "card_001", {"k": "v"})
    ops = session_store.WORKS_DIR / "_ops" / "session_ops.jsonl"
    assert ops.exists()
    line = json.loads(ops.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert set(line) == {"ts", "action", "work", "card", "detail"}
    assert line["action"] == "probe_action" and line["work"] == W
    assert line["detail"] == {"k": "v"}


async def test_append_logs_ops_and_upgrade_logs_revision(sandbox, work):
    wid = work["work_id"]
    # V1 种子 → 追加触发现场升格 → ops「upgrade」＋ revisions 留痕（T114/T152）
    path = session_store._session_path(wid, "card_001")
    path.write_text("创建时间：x\n\n---MSG---\nrole: user\n\n旧格式\n", encoding="utf-8")
    await session_facade.append(wid, "card_001", {"role": "user", "content": "新块"})
    ops_text = (session_store.WORKS_DIR / "_ops" / "session_ops.jsonl").read_text(encoding="utf-8")
    actions = [json.loads(l)["action"] for l in ops_text.strip().split("\n")]
    assert "upgrade" in actions and "append" in actions
    rev = session_store.WORKS_DIR / wid / "_ops" / "revisions.jsonl"
    assert rev.exists(), "升格必须留 revisions 痕（R6 通道首笔真实消费）"
    entry = json.loads(rev.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert entry["entity"] == "session" and entry["change"] == "v1_to_v2"
