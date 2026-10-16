# -*- coding: utf-8 -*-
"""《对话流画板架构》C 组 T6 —— 内存 journal「50 轮滚动窗」的机制层单测。

被测：`src/api/event_stream._trim_journals`（由 `emit()` 在 **run_start** 时调用）。

判据（任务书 §六 T6 的六条 ＋ 铁律①磁盘零改）：

- ① 51 轮后主 journal 恰含 50 轮、首元素为**第 2 轮** run_start；
- ② 无 run_start 的 journal 不裁；
- ③ 裁剪后 seq 继续单调；
- ④ `journal_last_seq` 语义不变（恒＝末帧 seq，只前进）；
- ⑤ 逐字流同步裁且 segment 界正确（轮界＝`data.segment=="run"`）；
- ⑥ `FLOW_JOURNAL_MAX_RUNS` 可配生效（含非法值回落）；
- ⑦ 铁律①：磁盘照落全量（裁剪只动内存 list，不碰落盘路径）。

每例用独立的 (work, card) 键，避免共享模块级 dict；fixture 用完即清。
"""
from __future__ import annotations

import pytest

from src.api import event_stream as es

WID, CID = "w_t6", "card_t6"


@pytest.fixture()
def card():
    """独占 (work, card) 键；用例结束把该卡的模块级状态清干净。"""
    for book in (es._journals, es._token_journals, es._seq_counters, es._anchors):
        book.pop((WID, CID), None)
    yield WID, CID
    for book in (es._journals, es._token_journals, es._seq_counters, es._anchors):
        book.pop((WID, CID), None)


def _round(r: int) -> None:
    """造一轮：run_start（顺带落逐字流 run 分段帧）＋ 一条过程事件 ＋ 一条逐字增量。"""
    es.emit(WID, CID, "run_start", {"pair_id": f"p{r}"})
    es.emit(WID, CID, "context", {"regions": ["a"], "bytes": r})
    es.emit_token(WID, CID, "content_chunk", f"第{r}轮正文")


def _run_starts(book) -> list[dict]:
    return [e for e in book if e.get("type") == "run_start"]


# ---------------------------------------------------------------- ① 窗口＝最近 50 轮

def test_t6_window_keeps_last_50_rounds(card):
    for r in range(1, 52):          # 51 轮
        _round(r)
    j = es._journals[(WID, CID)]
    assert len(_run_starts(j)) == 50, "51 轮后应恰含 50 轮"
    assert j[0]["type"] == "run_start", "窗口必须对齐在轮首（不是切在轮中）"
    assert j[0]["data"]["pair_id"] == "p2", "首元素应是**第 2 轮**的 run_start（只丢第 1 轮）"
    assert j[-1]["data"]["bytes"] == 51, "末轮照旧是第 51 轮"


# ---------------------------------------------------------------- ② 无 run_start 不裁

def test_t6_no_run_start_no_trim(card):
    for i in range(200):
        es.emit(WID, CID, "context", {"bytes": i})   # 全程无 run_start
    assert len(es._journals[(WID, CID)]) == 200, "没有轮界就无从裁——不得误裁"


# ---------------------------------------------------------------- ③ 裁剪后 seq 单调

def test_t6_seq_monotonic_after_trim(card):
    for r in range(1, 52):
        _round(r)
    seqs = [e["seq"] for e in es._journals[(WID, CID)]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "裁剪后 seq 仍严格单调"
    before = seqs[-1]
    es.emit(WID, CID, "context", {"bytes": 99})
    after = es._journals[(WID, CID)][-1]["seq"]
    assert after > before, "裁剪后新帧 seq 继续增长（计数器不重置）"


# ---------------------------------------------------------------- ④ journal_last_seq 不变

def test_t6_journal_last_seq_semantics_unchanged(card):
    for r in range(1, 51):
        _round(r)
    seq_before = es.journal_last_seq(WID, CID)
    assert seq_before > 0
    es.emit(WID, CID, "run_start", {"pair_id": "p51"})   # 触发裁剪
    j = es._journals[(WID, CID)]
    assert es.journal_last_seq(WID, CID) == j[-1]["seq"], "语义不变：恒＝末帧 seq"
    assert es.journal_last_seq(WID, CID) > seq_before, "只前进、不回退（裁剪不动末帧）"
    assert len(_run_starts(j)) == 50


# ---------------------------------------------------------------- ⑤ 逐字流同步裁

def test_t6_token_journal_trimmed_at_run_segment(card):
    for r in range(1, 52):
        _round(r)
    tj = es._token_journals[(WID, CID)]
    segs = [e for e in tj if (e.get("data") or {}).get("segment") == "run"]
    assert len(segs) == 50, "逐字流同步裁到 50 轮"
    assert (tj[0].get("data") or {}).get("segment") == "run", "窗口对齐在轮首（segment=run 分段帧）"
    assert tj[0]["pair_id"] == "p2", "首帧是第 2 轮的 run 分段帧"
    texts = [e["data"].get("text") for e in tj if e.get("type") == "content_chunk"]
    assert "第1轮正文" not in texts, "第 1 轮的逐字帧已随轮裁掉"
    assert "第51轮正文" in texts, "末轮逐字帧仍在"


# ---------------------------------------------------------------- ⑥ 上限可配

def test_t6_max_runs_configurable(card, monkeypatch):
    monkeypatch.setenv("FLOW_JOURNAL_MAX_RUNS", "2")
    assert es._journal_max_runs() == 2, "环境变量可配生效"
    for r in range(1, 6):           # 5 轮
        _round(r)
    j = es._journals[(WID, CID)]
    assert len(_run_starts(j)) == 2, "FLOW_JOURNAL_MAX_RUNS=2 ⇒ 只保 2 轮"
    assert j[0]["data"]["pair_id"] == "p4", "保的是最后 2 轮（p4 / p5）"
    monkeypatch.setenv("FLOW_JOURNAL_MAX_RUNS", "not-a-number")
    assert es._journal_max_runs() == 50, "非法值回落缺省 50"


# ---------------------------------------------------------------- ⑦ 铁律① 磁盘零改

def test_t6_disk_persist_unaffected(card, monkeypatch):
    """磁盘照落全量：51 轮的落盘调用一条不少，第 1 轮也在。"""
    from src.storage import event_log
    calls: list[dict] = []
    monkeypatch.setattr(event_log, "append_bg", lambda w, c, rec: calls.append(rec))
    for r in range(1, 52):
        _round(r)
    # 每轮 2 条落盘（run_start / context）；逐字流的 content_chunk 按设计不落盘（裁定 C7）
    assert len(calls) == 51 * 2, "每轮 2 条、51 轮共 102 条落盘调用"
    assert calls[0]["type"] == "run_start" and calls[0]["data"].get("pair_id") == "p1", \
        "第 1 轮照旧落盘（内存裁掉它不影响磁盘）"
    assert len(_run_starts(es._journals[(WID, CID)])) == 50, "内存侧同时已裁到 50 轮"
