# -*- coding: utf-8 -*-
"""事件流**增量撤销/索引扫描**回归网（[2026-11-19 · 性能批次二]）。

## 病根（实测）

旧撤销缓存按 ``(mtime_ns, size)`` 整体键控 ⇒ 事件文件**每追加一行**缓存即失效，
下一次 timeline **全文件重扫**（合成 109MB 卡实测 ~220ms/次；card_007 747MB
按比例秒级）。``before`` 翻页更是恒走全量 ``read()``（109MB ~360ms + 内存翻倍）。

## 本网钉住

1. **追加走增量**：只扫新增完整行——``full_rebuilds`` 不涨、``delta_scans`` 涨；
   spans 与「全量重扫」逐字一致。
2. **半行不发布**：追加未写完整的行，本次扫描看不见它；补完 \n 后下次补读。
3. **截断/替换安全重建**：文件截短、同长改写（mtime 变）⇒ 全量重建，
   撤销项不会重新出现（不复活）。
4. **翻页语义逐条等价**：``timeline(before=X, tail=N)`` 与参考实现
   （升序收集→revoke 回撤→(ts,seq) 排序→序位截→尾截）逐条一致——
   含乱序 ts、嵌套 revoke、坏行、巨型行（>64KB 走行首/行尾抽取路径）。
5. **并发追加**：多线程边追加边读，不崩、最终一致、只发布完整边界结果。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.storage import event_log

ROOT = Path(__file__).resolve().parents[1]


def _iso(minute: int) -> str:
    base = datetime(2026, 11, 19, 21, 0, tzinfo=timezone(timedelta(hours=8)))
    return (base + timedelta(minutes=minute)).isoformat(timespec="milliseconds")


def _rec(seq, typ, minute=0, text="内容"):
    return {"seq": seq, "card_id": "card_x", "run_id": f"r{seq}", "round": 1,
            "type": typ, "data": {"text": f"{text}{seq}"}, "model": "",
            "ts": _iso(minute)}


def _revoke(seq, from_seq, minute=0):
    return {"seq": seq, "card_id": "card_x", "run_id": f"r{seq}", "round": 1,
            "type": "revoke", "data": {"from_seq": from_seq}, "model": "",
            "ts": _iso(minute)}


def _append_lines(p: Path, records: list[dict]) -> None:
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()


def _write_lines(p: Path, records: list[dict]) -> None:
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                         for r in records), encoding="utf-8")


def _install(tmp_path: Path, monkeypatch, records: list[dict]) -> Path:
    p = tmp_path / "card_x.jsonl"
    _write_lines(p, records)
    monkeypatch.setattr(event_log, "event_path", lambda w, c: p)
    return p


def _visible_ref(records: list[dict]) -> list[dict]:
    """参考实现：升序收集 → revoke 回撤（区间并集）→ 可见集合。"""
    spans: list[tuple[int, int]] = []
    for r in records:
        if str(r.get("type") or "") == "revoke":
            spans.append((int(r["data"]["from_seq"]), int(r["seq"])))
    out = []
    for r in records:
        s = r.get("seq")
        if s is None or str(r.get("type") or "") == "revoke":
            continue
        if any(f <= int(s) < t for f, t in spans):
            continue
        out.append(dict(r))
    return out


def _norm(items):
    return [(int(x.get("seq") or 0), str(x.get("type") or ""),
             str(x.get("ts") or ""),
             json.dumps(x.get("data"), ensure_ascii=False, sort_keys=True))
            for x in items]


def _before_ref(records: list[dict], before: int, tail: int = 0,
                from_seq: int = 0) -> list[dict]:
    """参考实现（序位截）：可见集合按 (ts,seq) 排 → 首个 seq==before 序位截。"""
    items = [r for r in _visible_ref(records) if int(r["seq"]) >= from_seq]

    def key(r):
        ts = str(r.get("ts") or "")
        if ts and ts[0].isdigit() and "T" not in ts:
            try:
                ts = datetime.fromtimestamp(float(ts)).astimezone().isoformat()
            except (ValueError, OSError, OverflowError):
                pass
        return (ts, int(r["seq"]))

    items.sort(key=key)
    if before > 0:
        idx = next((i for i, x in enumerate(items)
                    if int(x["seq"]) == before), None)
        items = items[:idx] if idx is not None else \
            [x for x in items if int(x["seq"]) < before]
    if tail:
        items = items[-tail:]
    return items


# ---------------------------------------------------------------- 1 追加增量
def test_append_scans_delta_only(tmp_path, monkeypatch):
    p = _install(tmp_path, monkeypatch,
                 [_rec(1, "assistant_text"), _rec(2, "assistant_text"),
                  _revoke(3, 2), _rec(4, "assistant_text")])
    st0 = event_log.scan_stats()
    spans1 = event_log._revoke_spans(p)
    assert spans1 == [(2, 3)]
    st1 = event_log.scan_stats()
    assert st1["full_rebuilds"] == st0["full_rebuilds"] + 1
    assert st1["delta_scans"] == st0["delta_scans"]

    # 命中缓存：文件没变
    before_hits = event_log.scan_stats()["cache_hits"]
    assert event_log._revoke_spans(p) == spans1
    assert event_log.scan_stats()["cache_hits"] == before_hits + 1

    # 追加（含新 revoke）⇒ 增量扫描，full_rebuilds 不涨
    _append_lines(p, [_rec(5, "assistant_text"), _revoke(6, 4),
                      _rec(7, "assistant_text")])
    st2_before = event_log.scan_stats()
    spans2 = event_log._revoke_spans(p)
    st2 = event_log.scan_stats()
    assert spans2 == [(2, 3), (4, 6)]
    assert st2["full_rebuilds"] == st2_before["full_rebuilds"]
    assert st2["delta_scans"] == st2_before["delta_scans"] + 1
    assert st2["bytes_scanned"] > st2_before["bytes_scanned"]


# ---------------------------------------------------------------- 2 半行补读
def test_partial_line_is_not_published_until_complete(tmp_path, monkeypatch):
    p = _install(tmp_path, monkeypatch, [_rec(1, "assistant_text")])
    assert event_log._revoke_spans(p) == []
    # 追加半行（revoke 记录没写完，无 \n）
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write('{"seq": 2, "card_id": "card_x", "type": "revoke", '
                '"data": {"from_seq": 1}')
        f.flush()
    assert event_log._revoke_spans(p) == []      # 半行不发布
    # 补完整行
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(', "model": "", "ts": "%s"}\n' % _iso(0))
        f.flush()
    assert event_log._revoke_spans(p) == [(1, 2)]  # 下次补读发布
    # 撤销项在 timeline 里生效（seq1 被撤）
    got = event_log.timeline("w", "c", tail=10)
    assert [int(x["seq"]) for x in got] == []


# ------------------------------------------------------- 3 截断/替换重建
def test_truncate_rebuilds_and_revoke_does_not_revive(tmp_path, monkeypatch):
    p = _install(tmp_path, monkeypatch,
                 [_rec(1, "assistant_text"), _rec(2, "assistant_text"),
                  _revoke(3, 1), _rec(4, "assistant_text")])
    assert event_log._revoke_spans(p) == [(1, 3)]
    # 文件被替换成更短的原文（seq1 的 revoke 没了）
    _write_lines(p, [_rec(1, "assistant_text"), _rec(2, "assistant_text")])
    assert event_log._revoke_spans(p) == []
    got = event_log.timeline("w", "c", tail=10)
    assert [int(x["seq"]) for x in got] == [1, 2]   # 撤销项不复活

    # 同长不同内容（mtime 变化）⇒ 重建
    _write_lines(p, [_rec(9, "assistant_text"), _revoke(10, 9)])
    assert event_log._revoke_spans(p) == [(9, 10)]


# ------------------------------------------------------- 4 翻页逐条等价
def test_before_pagination_matches_reference(tmp_path, monkeypatch):
    """乱序 ts + 嵌套 revoke + 坏行 + 巨型行，索引翻页与参考实现逐条一致。

    只配 ``before>0``（索引路径/全量路径都是**全局 (ts,seq) 序位截**）；
    ``before=0, tail=N`` 走的是尾部窗口路径（文件序倒读，5715 既有契约——
    真实文件 ts 随落盘单调，两者等价；乱序 ts 是异步写抖动，不属于该路径的
    语义范围），另有单调 ts 的尾窗等价用例。
    """
    records = []
    seq = 0
    for i in range(60):
        seq += 1
        # ts 故意乱序（写乱序实况）：每 7 条时间倒退一分钟
        records.append(_rec(seq, "assistant_text", minute=(i % 7) if i % 3 else 0))
        if i in (11, 29, 47):
            seq += 1
            records.append(_revoke(seq, seq - 6))
    records.insert(20, {"type": "assistant_text", "data": {"text": "无seq"}})
    # 一条 100KB 巨型行（> _INDEX_PARSE_MAX ⇒ 走行首/行尾抽取路径）
    seq += 1
    giant = dict(_rec(seq, "tool_result"))
    giant["data"] = {"text": "G" * 100_000}
    records.append(giant)
    p = _install(tmp_path, monkeypatch, records)
    for before in (5, 30, 61, 999):
        for tail in (0, 5, 50):
            got = event_log.timeline("w", "c", before=before, tail=tail)
            ref = _before_ref(records, before, tail)
            assert _norm(got) == _norm(ref), \
                f"before={before} tail={tail} 不等价：{len(got)} vs {len(ref)}"
    # before=0 tail=0 ＝ 全量时序（走全量兜底路径，全局 (ts,seq) 排序）
    got = event_log.timeline("w", "c", before=0, tail=0)
    assert _norm(got) == _norm(_before_ref(records, 0, 0))


def test_tail_window_monotonic_ts_matches_reference(tmp_path, monkeypatch):
    """尾窗路径（真实形态：ts 随落盘单调）与参考实现逐条一致。"""
    records = [_rec(i, "assistant_text", minute=i) for i in range(1, 41)]
    records.append(_revoke(41, 35, minute=41))
    records += [_rec(i, "assistant_text", minute=i) for i in range(42, 51)]
    p = _install(tmp_path, monkeypatch, records)
    for tail in (1, 5, 20, 60):
        got = event_log.timeline("w", "c", tail=tail)
        ref = _before_ref(records, 0, tail)
        assert _norm(got) == _norm(ref), f"tail={tail} 不等价"


def test_before_then_append_page_cursor_stable(tmp_path, monkeypatch):
    """翻页锚点（序位）在追加后保持稳定——旧页内容不变。"""
    records = [_rec(i, "assistant_text", minute=i) for i in range(1, 41)]
    p = _install(tmp_path, monkeypatch, records)
    page1 = event_log.timeline("w", "c", before=40, tail=10)
    _append_lines(p, [_rec(i, "assistant_text", minute=i) for i in range(41, 61)])
    page1_again = event_log.timeline("w", "c", before=40, tail=10)
    assert _norm(page1) == _norm(page1_again)


# ------------------------------------------------------- 5 并发追加
def test_concurrent_append_publishes_complete_boundary_only(tmp_path, monkeypatch):
    """多线程边追加边读：不崩、最终 timeline 与参考实现一致。"""
    p = _install(tmp_path, monkeypatch, [_rec(i, "assistant_text")
                                         for i in range(1, 21)])
    errors: list[Exception] = []

    def _writer(base: int) -> None:
        try:
            for i in range(30):
                _append_lines(p, [_rec(base + i, "assistant_text")])
                time.sleep(0.001)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _reader() -> None:
        try:
            for _ in range(40):
                event_log.timeline("w", "c", tail=20)
                event_log._revoke_spans(p)
                time.sleep(0.002)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=_writer, args=(100,)),
          threading.Thread(target=_writer, args=(200,)),
          threading.Thread(target=_reader), threading.Thread(target=_reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == [], errors
    all_records = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                all_records.append(json.loads(ln))
    got = event_log.timeline("w", "c", tail=0)
    assert _norm(got) == _norm(_before_ref(all_records, before=0))


# ------------------------------------------------------- 6 增量性（性能判据）
def test_append_cost_does_not_grow_with_history(tmp_path, monkeypatch):
    """追加重付不随历史增长：追加后首次撤销扫描只触新增字节（stats 断言）。"""
    p = _install(tmp_path, monkeypatch,
                 [_rec(i, "assistant_text", text="x" * 200)
                  for i in range(1, 2001)])
    event_log._revoke_spans(p)
    st0 = event_log.scan_stats()
    _append_lines(p, [_rec(2001, "assistant_text", text="y" * 200)])
    event_log._revoke_spans(p)
    st1 = event_log.scan_stats()
    assert st1["delta_scans"] == st0["delta_scans"] + 1
    # 新增字节 ≈ 单行（~350B），绝不允许 ≈ 全文件（~800KB）
    delta_bytes = st1["bytes_scanned"] - st0["bytes_scanned"]
    assert delta_bytes < 4096, f"增量扫描触及 {delta_bytes}B ⇒ 又全量重扫了"
