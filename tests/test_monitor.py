# -*- coding: utf-8 -*-
"""用量监控（照抄 dsh `lifecycle/monitor.ts` 账本）。

判据：
M1 record_usage 分配每会话 callSeq（1 起、逐次 +1、会话间独立）
M2 影子对照同轮入账（old/new 估算＝字符/4）
M3 record_condense_batch / record_depth 落各自账本
M4 打点失败不抛（writer 抛异常也只告警）
M5 默认写到本实例 obs/monitor/*.jsonl
"""
from __future__ import annotations

import json

from src.storage import monitor, obs_paths


def _sink() -> tuple[list[str], object]:
    lines: list[str] = []
    monitor.set_writer(lines.append)
    return lines, None


# ---------------------------------------------------------------- M1

def test_call_seq_per_session():
    lines, _ = _sink()
    monitor.reset_call_seq()
    a1 = monitor.record_usage(session_id="s1", model="m", prompt_tokens=10)
    a2 = monitor.record_usage(session_id="s1", model="m", prompt_tokens=20)
    b1 = monitor.record_usage(session_id="s2", model="m", prompt_tokens=30)
    assert (a1["callSeq"], a2["callSeq"], b1["callSeq"]) == (1, 2, 1), "每会话独立计数"
    assert len(lines) == 3
    assert json.loads(lines[0])["promptTokens"] == 10


def test_call_seq_explicit_wins():
    lines, _ = _sink()
    monitor.reset_call_seq()
    e = monitor.record_usage(session_id="s1", model="m", prompt_tokens=1, call_seq=99)
    assert e["callSeq"] == 99


# ---------------------------------------------------------------- M2

def test_shadow_estimates():
    lines, _ = _sink()
    monitor.reset_call_seq()
    e = monitor.record_shadow(session_id="s1", old_text="a" * 400, new_text="b" * 100)
    assert e["model"] == "shadow"
    assert e["shadowOldTokens"] == 100 and e["shadowNewTokens"] == 25, "字符/4"
    assert e["promptTokens"] == 25


# ---------------------------------------------------------------- M3

def test_condense_batch_and_depth():
    trace: list[str] = []
    depth: list[str] = []
    monitor.set_writer(trace.append, monitor.CONDENSE_TRACE_NAME)
    monitor.set_writer(depth.append, monitor.DEPTH_NAME)
    monitor.record_condense_batch(session_id="s1", batch_no=3, budget=1000,
                                  window_seqs=6, consumed_seqs=6, new_mark=12, nodes=2)
    monitor.record_depth(session_id="s1", depth=3, nodes=9)
    r = json.loads(trace[0])
    assert (r["batchNo"], r["newMark"], r["nodes"]) == (3, 12, 2)
    assert json.loads(depth[0])["depth"] == 3


# ---------------------------------------------------------------- M4

def test_writer_failure_does_not_raise():
    def _boom(_line: str) -> None:
        raise RuntimeError("sink down")
    monitor.set_writer(_boom)
    # 不抛就算过（打点不得拖垮主链路）
    monitor.record_usage(session_id="s9", model="m", prompt_tokens=1)


# ---------------------------------------------------------------- M5

def test_default_writer_lands_in_instance_obs(sandbox, monkeypatch):
    monkeypatch.setenv("ECO_INSTANCE_TAG", "mon")
    monitor.reset_writer()
    monitor.reset_call_seq()
    monitor.record_usage(session_id="s1", model="m", prompt_tokens=7)
    path = obs_paths.obs_monitor_path(monitor.USAGE_NAME)
    assert path.exists(), "默认落到本实例 obs/monitor/usage.jsonl"
    assert json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])["promptTokens"] == 7
