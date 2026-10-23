# -*- coding: utf-8 -*-
"""**磁盘治理网**（[5715]）——快照体积闸 ＋ 日志预算闸。

## 为什么有这张网

2026-10-23 盘查（用户提出「是不是在无脑往硬盘里塞垃圾且完全不治理」）查出两件事：

1. **快照只按份数留，不按体积**（``KEEP_BATCHES=200``）⇒ 总量 = 200 × 单份体积，
   而单份体积随会话线性增长（cards.db ＋ events ＋ sessions 全量复制）
   ⇒ **总量无界**：实测本机快照库 50.8GB（work_018 18.0GB / work_025 29.6GB）。
2. **prompt 日志明写着「无闸」**（``prompt_log.py`` 原注释：无轮转/无长度上限/无采样）
   ⇒ 每次 LLM 调用落一个文件、永不清理 ⇒ 实测 1,855 个 / 1.17GB，只增不减。

⇒ 这张网把「无界」钉成**会红的断言**：超预算 ⇒ 必须清到预算内。

跑法：pytest tests/test_disk_governance.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.api import prompt_log
from src.storage import data_snapshot


# ================================================================ 快照：体积闸

def _make_batch(root: Path, name: str, size_bytes: int) -> Path:
    """造一份占体积的假批次（内容是稀疏文件，不真写那么多字节）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    payload = d / "works" / "work_x" / "cards.db"
    payload.parent.mkdir(parents=True, exist_ok=True)
    with payload.open("wb") as fh:
        fh.truncate(size_bytes)      # 稀疏文件：占体积但不真写
    return d


def test_snapshot_volume_cap_prunes_oldest_batches(sandbox, monkeypatch, tmp_path):
    """快照总量超预算 ⇒ 从**最旧**的非日备批次开始删，直到回到预算内。"""
    root = tmp_path / "snaps"
    monkeypatch.setattr(data_snapshot, "SNAPSHOT_ROOT", root)
    monkeypatch.setattr(data_snapshot, "KEEP_BATCH_BYTES", 10 * 1024 * 1024)   # 10MB
    monkeypatch.setattr(data_snapshot, "KEEP_BATCHES", 200)                  # 份数不是这次的闸

    wid = "work_gov"
    _make_batch(root / wid, "20260101_000000_000001", 6 * 1024 * 1024)   # 最旧
    _make_batch(root / wid, "20260102_000000_000002", 6 * 1024 * 1024)
    _make_batch(root / wid, "20260103_000000_000003", 6 * 1024 * 1024)   # 最新

    removed = data_snapshot._prune_batches(wid)
    assert removed >= 1, "超预算必须删（原来只按份数 ⇒ 体积无界）"

    left = sorted(p.name for p in (root / wid).iterdir() if p.is_dir())
    assert "20260101_000000_000001" not in left, "最旧的应先被删"
    assert "20260103_000000_000003" in left, "最新的必须留着"

    total = sum(data_snapshot._batch_bytes(p) for p in (root / wid).iterdir() if p.is_dir())
    assert total <= 10 * 1024 * 1024, f"总量必须回到预算内：{total}"


def test_snapshot_volume_cap_never_deletes_daily_backups(sandbox, monkeypatch, tmp_path):
    """体积闸**不动日备**（日备是「每天一份」的兜底，与过程批次不是一回事）。

    ⚠ 日备的**超期清理**是另一条规则（超 30 天）⇒ 本例必须用**今天**的日备名，
    否则会被那条规则删掉（那就测不到体积闸了）。
    """
    from datetime import datetime

    root = tmp_path / "snaps2"
    monkeypatch.setattr(data_snapshot, "SNAPSHOT_ROOT", root)
    monkeypatch.setattr(data_snapshot, "KEEP_BATCH_BYTES", 1024 * 1024)

    wid = "work_gov2"
    today = datetime.now().strftime("%Y%m%d")
    _make_batch(root / wid, f"daily_{today}", 4 * 1024 * 1024)
    _make_batch(root / wid, "20260101_000000_000001", 4 * 1024 * 1024)

    data_snapshot._prune_batches(wid)
    left = [p.name for p in (root / wid).iterdir() if p.is_dir()]
    assert f"daily_{today}" in left, "日备不得被体积闸删掉"
    assert "20260101_000000_000001" not in left, "过程批次应被体积闸删掉（超预算）"


def test_snapshot_root_can_be_pointed_to_another_disk(monkeypatch):
    """快照根**可以用环境变量指到别的盘**（实例在 C 盘时别把快照也长在 C 盘）。"""
    import importlib

    monkeypatch.setenv("ECO_SNAPSHOT_ROOT", r"D:\bigdisk\snapshots")
    mod = importlib.reload(data_snapshot)
    try:
        assert str(mod.SNAPSHOT_ROOT).replace("\\", "/") == "D:/bigdisk/snapshots", \
            f"环境变量应生效：{mod.SNAPSHOT_ROOT}"
    finally:
        monkeypatch.delenv("ECO_SNAPSHOT_ROOT", raising=False)
        importlib.reload(data_snapshot)      # 还原模块态，别污染别的用例


# ================================================================ 日志：预算闸

def test_prompt_log_budget_prunes_oldest(sandbox, monkeypatch, tmp_path):
    """prompt 日志超份数/体积预算 ⇒ 从**最旧**开始删（最新的留）。"""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(prompt_log, "LOGS_DIR", logs)
    monkeypatch.setattr(prompt_log, "PROMPT_LOG_MAX_FILES", 3)
    monkeypatch.setattr(prompt_log, "PROMPT_LOG_MAX_BYTES", 1024 * 1024 * 1024)

    for i in range(6):
        p = logs / f"prompt_card_00{i}.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))   # 固定 mtime ⇒ 顺序确定

    removed = prompt_log._prune_prompt_logs()
    assert removed >= 3, f"超份数必须删：{removed}"
    left = sorted(p.name for p in logs.glob("prompt_*.json"))
    assert len(left) == 3, f"应删到上限：{left}"
    assert left[-1] == "prompt_card_005.json", f"最新的必须留：{left}"


def test_prompt_log_budget_respects_byte_cap(sandbox, monkeypatch, tmp_path):
    """体积预算也生效（不只是份数）。"""
    logs = tmp_path / "logs2"
    logs.mkdir()
    monkeypatch.setattr(prompt_log, "LOGS_DIR", logs)
    monkeypatch.setattr(prompt_log, "PROMPT_LOG_MAX_FILES", 1000)
    monkeypatch.setattr(prompt_log, "PROMPT_LOG_MAX_BYTES", 4096)

    for i in range(4):
        p = logs / f"prompt_card_0{i}.json"
        p.write_text("x" * 2048, encoding="utf-8")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))

    prompt_log._prune_prompt_logs()
    total = sum(p.stat().st_size for p in logs.glob("prompt_*.json"))
    assert total <= 4096, f"总量必须回到预算内：{total}"


def test_prompt_log_prune_ignores_other_logs(sandbox, monkeypatch, tmp_path):
    """只清 prompt 日志，**不动别的日志**（perm_log 等不是这个闸的靶子）。"""
    logs = tmp_path / "logs3"
    logs.mkdir()
    monkeypatch.setattr(prompt_log, "LOGS_DIR", logs)
    monkeypatch.setattr(prompt_log, "PROMPT_LOG_MAX_FILES", 0)
    monkeypatch.setattr(prompt_log, "PROMPT_LOG_MAX_BYTES", 0)

    (logs / "prompt_card_00.json").write_text("{}", encoding="utf-8")
    other = logs / "perm_log.jsonl"
    other.write_text("{}\n", encoding="utf-8")

    prompt_log._prune_prompt_logs()
    assert not (logs / "prompt_card_00.json").exists(), "超限的 prompt 日志应被删"
    assert other.exists(), "别的日志不得被误删"
