# -*- coding: utf-8 -*-
"""观测路径（按实例派生）＋ archive 接线 回归（2026-10-20）。

判据：
O1 观测根**按实例派生**（不同实例标签 ⇒ 不同目录；env 显式优先于端口）
O2 阶段打点一行 JSON、失败不抛
O3 archive：有 process/docPaths 的节点写行，没有的不写；幂等；可按键读回
"""
from __future__ import annotations

import json

from src.storage import card_store, memory_domain as md, obs_paths, work_store


# ---------------------------------------------------------------- O1

def test_obs_root_derived_by_instance(sandbox, monkeypatch):
    monkeypatch.setenv("ECO_INSTANCE_TAG", "5700")
    a = obs_paths.obs_root()
    monkeypatch.setenv("ECO_INSTANCE_TAG", "5717")
    b = obs_paths.obs_root()
    print("A:", a, "B:", b)
    assert a != b, "不同实例 ⇒ 不同目录（dsh 硬编码路径事故的修法）"
    assert a.name == "5700" and b.name == "5717"


def test_obs_tag_prefers_explicit_over_port(sandbox, monkeypatch):
    monkeypatch.setenv("ECO_PORT", "5799")
    monkeypatch.delenv("ECO_INSTANCE_TAG", raising=False)
    assert obs_paths.obs_instance_tag() == "port5799"
    monkeypatch.setenv("ECO_INSTANCE_TAG", "explicit")
    assert obs_paths.obs_instance_tag() == "explicit"


def test_obs_paths_under_logs(sandbox, monkeypatch):
    monkeypatch.setenv("ECO_INSTANCE_TAG", "t1")
    p = obs_paths.obs_monitor_path("usage")
    assert p.name == "usage.jsonl"
    assert str(sandbox.logs) in str(p), "观测产物必须落在本实例 logs 下"


# ---------------------------------------------------------------- O2

def test_append_stage_line_writes_json(sandbox, monkeypatch):
    monkeypatch.setenv("ECO_INSTANCE_TAG", "t2")
    ok = obs_paths.append_stage_line("s1", "tool.memory_correct", "in", {"in": "x"})
    assert ok is True
    path = obs_paths.obs_stage_log_path("s1")
    rec = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["stage"] == "tool.memory_correct" and rec["dir"] == "in" and rec["in"] == "x"


def test_append_stage_line_never_raises(sandbox, monkeypatch):
    monkeypatch.setenv("ECO_INSTANCE_TAG", "t3")
    monkeypatch.setattr(obs_paths, "obs_stage_log_path",
                        lambda sid: __import__("pathlib").Path("/\\0bad/x.jsonl"))
    assert obs_paths.append_stage_line("s1", "x", "in") is False, "写不进去也不能抛"


# ---------------------------------------------------------------- O3

async def test_archive_wiring(sandbox):
    wdata = await work_store.create_work("归档剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡", "用户直接创建")
    chain = [
        md.new_node("s1", 1, "有描述层", kind="decision",
                    process="过程正文", docPaths=["a.md"], docRefs=[{"path": "a.md", "seq": 1}]),
        md.new_node("s1", 2, "无描述层", kind="fact"),
    ]
    assert md.save_archive(wid, "card_001", chain) == 1, "只有带描述层的节点写行"
    md.save_archive(wid, "card_001", chain)               # 幂等
    got = md.load_archive(wid, "card_001")
    assert list(got) == [(1, "gist")]
    assert got[(1, "gist")]["process"] == "过程正文"
    assert got[(1, "gist")]["docPaths"] == ["a.md"]
    one = md.load_archive(wid, "card_001", seq=1)
    assert one[(1, "gist")]["docRefs"] == [{"path": "a.md", "seq": 1}]
