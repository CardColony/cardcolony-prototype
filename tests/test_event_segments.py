# -*- coding: utf-8 -*-
"""事件层分段标记制补充回归（迭代一·会话底座 · S5/A5：持久化通道＋回合锚，T093/T099/T110/T145）。

主判据面（journal 不清零／分段标记帧／seq 单一取号）在
test_token_stream_isolation.py 已按 T092/T094/T095/T097 钉死；
本件补两块：
1. R5 过程线持久化通道（T099/T110/T145）：persist_process 配置位默认 off；
   on 时主 journal 帧逐帧落 works/{work}/process/{card}.jsonl（追加式），
   格式不随开关变化；off＝零落盘；
2. (pair_id, seq, turn) 三锚（T093）：锚由 run_start/round_start 就地登记、
   帧统一附带。
零真实 works 触碰；不 import main。
"""
from __future__ import annotations

import json

import pytest

from src.api import event_stream as es
from src.storage import session_store

W, C = "work_seg", "card_seg"


def setup_function(_):
    es._journals.clear()
    es._subscribers.clear()
    es._seq_counters.clear()
    es._token_journals.clear()
    es._token_subscribers.clear()
    es._anchors.clear()


def teardown_function(_):
    es.set_persist_process(False)   # 测试拨动的开关一律还原


# ---------- 1：R5 持久化通道 ----------

def test_persist_process_off_by_default(sandbox):
    """配置位缺省 off（config/session_base.json）；off＝零落盘。"""
    assert es._PERSIST_PROCESS is False
    es.emit(W, C, "run_start", {})
    es.emit(W, C, "tool_call", {"tool": "x"})
    p = session_store.WORKS_DIR / W / "process" / f"{C}.jsonl"
    assert not p.exists(), "off 时零落盘（T110）"


def test_persist_process_writes_frames_when_on(sandbox):
    es.set_persist_process(True)
    es.emit(W, C, "run_start", {"pair_id": "p1"})
    es.emit(W, C, "tool_call", {"tool": "x"})
    es.emit(W, C, "done", {})
    p = session_store.WORKS_DIR / W / "process" / f"{C}.jsonl"
    assert p.exists()
    frames = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n")]
    assert [f["type"] for f in frames] == ["run_start", "tool_call", "done"]
    assert [f["seq"] for f in frames] == [2, 3, 4], "主 journal 帧（含 run 段标记占 seq=1）"
    assert set(frames[0]) == {"seq", "ts", "type", "data", "pair_id", "turn"}, \
        "格式不随开关变化：帧形态＝journal 帧原样"


def test_persist_process_toggle_back_stops_writing(sandbox):
    es.set_persist_process(True)
    es.emit(W, C, "run_start", {})
    es.set_persist_process(False)
    es.emit(W, C, "done", {})
    p = session_store.WORKS_DIR / W / "process" / f"{C}.jsonl"
    assert p.exists()
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1, "关后不再追加（拨开关只改行为不改格式，T145）"


# ---------- 2：三锚（T093） ----------

def test_frames_carry_turn_anchors(sandbox):
    es.emit(W, C, "run_start", {"pair_id": "p9"})
    evt1 = es.emit(W, C, "tool_call", {})
    assert evt1["pair_id"] == "p9", "回合锚随 run_start 登记"
    assert evt1["turn"] == 0
    es.emit(W, C, "round_start", {"round": 3})
    evt2 = es.emit(W, C, "tool_result", {})
    assert evt2["turn"] == 3, "轮锚随 round_start 就地更新"
    assert evt2["pair_id"] == "p9", "回合锚跨轮保持"
    seg = [e for e in es.token_journal_of(W, C) if (e.get("data") or {}).get("segment")]
    assert seg[-1]["turn"] == 3, "标记帧同样携带三锚"


def test_frames_without_run_start_have_no_anchor_keys(sandbox):
    """未登记锚（无 run_start 的裸帧）不伪造锚——键缺省不携带。"""
    evt = es.emit(W, C, "tool_call", {})
    assert "pair_id" not in evt and "turn" not in evt
