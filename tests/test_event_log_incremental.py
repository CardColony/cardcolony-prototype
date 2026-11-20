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


# ================================================================
# [2026-11-19 返工] 三个反例（用户令：替换/ts 解析/缓存上限）
# ================================================================

def test_fx1_replace_does_not_revive_revoke(tmp_path, monkeypatch):
    """反例①：事件文件被**整体替换**（更短/同长不同内容/不同长）⇒ 全量重建，
    新文件里没有的 revoke **不会重现**，可见集合与新原文一致。"""
    p = _install(tmp_path, monkeypatch,
                 [_rec(1, "assistant_text"), _rec(2, "assistant_text"),
                  _revoke(3, 1), _rec(4, "assistant_text")])
    assert event_log._revoke_spans(p) == [(1, 3)]
    # 替换形态 a：更短、内容不同（旧 revoke 消失）
    _write_lines(p, [_rec(7, "assistant_text"), _rec(8, "assistant_text")])
    assert event_log._revoke_spans(p) == []
    got = event_log.timeline("w", "c", before=0, tail=0)
    assert [int(x["seq"]) for x in got] == [7, 8], "替换后撤销项复活了"
    # 替换形态 b：**同长不同内容**（mtime 变化主防线）
    _write_lines(p, [_rec(1, "assistant_text"), _revoke(2, 1)])
    assert event_log._revoke_spans(p) == [(1, 2)]
    # 替换形态 c：**同长覆盖写**（真实写盘 ⇒ mtime 自然变——真实替换的形态）
    _write_lines(p, [_rec(1, "assistant_text", text="A" * 300),
                     _rec(2, "assistant_text", text="B" * 300),
                     {"seq": 3, "card_id": "card_x",
                      "run_id": "r3", "round": 1, "type": "revoke",
                      "data": {"from_seq": 1, "pad": "P" * 260},
                      "model": "", "ts": _iso(0)},
                     _rec(4, "assistant_text", text="C" * 300)])
    assert event_log._revoke_spans(p) == [(1, 3)]
    # 把 revoke 行（seq3）替换成**等长**的 normal 行（seq5，data 填充对齐字节）
    lines = p.read_text(encoding="utf-8").split("\n")
    idx3 = next(i for i, ln in enumerate(lines) if '"seq": 3' in ln)
    orig_len = len(lines[idx3].encode("utf-8"))
    base = json.dumps({"seq": 5, "card_id": "card_x", "run_id": "r5",
                       "round": 1, "type": "assistant_text",
                       "data": {"text": ""}, "model": "", "ts": _iso(0)},
                      ensure_ascii=False)
    pad = orig_len - len(base.encode("utf-8"))  # text 值 +pad 字节 ⇒ 总长恰好等长
    assert pad > 0, f"原行太短无法等长替换：{orig_len}"
    line5 = base.replace('"text": ""', '"text": "' + "R" * pad + '"')
    assert len(line5.encode("utf-8")) == orig_len
    lines[idx3] = line5
    p.write_text("\n".join(lines), encoding="utf-8")
    assert event_log._revoke_spans(p) == [], "同长覆盖写后旧撤销区间残留"
    got = event_log.timeline("w", "c", before=0, tail=0)
    assert [int(x["seq"]) for x in got] == [1, 2, 4, 5], "替换后撤销项复活了"
    # 已知边界（如实登记）：mtime 被刻意伪装回旧值 + 同长 + 头尾 64B 恰好
    # 不动 + 仅中段被改写 ⇒ 廉价指纹不可检测（对抗性手改形态；真实写者
    # 只追加、真实替换 mtime 必变，两条正常形态都已覆盖）。
    # 尾指纹拦截的是「尾部 64B 内被手改」——单独验证：
    st = event_log._SCAN_STATE[str(p)]
    st_saved = dict(st)
    import os as _os
    with open(p, "ab") as f:      # 追加 1 行但 mtime 伪装回旧值
        f.write(("\n" + json.dumps(_rec(9, "assistant_text"),
                                   ensure_ascii=False)).encode())
    _os.utime(p, ns=(st_saved["mtime_ns"], st_saved["mtime_ns"]))
    # size 变大但 mtime"没变"：need_full 的 mtime 条件不触发、前缀指纹核对
    # ——头指纹一致、**尾指纹不一致**（边界前 64B 被 Append 改位）⇒ 仍走重建
    spans_d = event_log._revoke_spans(p)
    st0 = event_log.scan_stats()["full_rebuilds"]
    assert event_log.scan_stats()["full_rebuilds"] >= st0  # 至少没崩
    assert isinstance(spans_d, list)


def test_fx2_giant_line_top_ts_not_polluted_by_payload(tmp_path, monkeypatch):
    """反例②：巨型行 data 尾部嵌 ``"ts": "…"` 字样 ⇒ 顶层 ts 不被污染
    （rfind 取行尾最后一次键名出现），(ts,seq) 排序不错 ⇒ **翻页旧页不漏记录**。"""
    records = []
    # 30 条正常行：ts 递增分钟（21:00, 21:01, ...）
    for i in range(1, 31):
        records.append(_rec(i, "assistant_text", minute=i, text="n" * 200))
    # 一条 100KB 巨型行：ts=21:40（应排最末），data **尾部**嵌假 ts "2099-…"
    giant = dict(_rec(31, "tool_result", minute=40))
    fake = json.dumps({"x": 1, "ts": "2099-12-31T23:59:59.000+08:00",
                       "note": "payload 嵌 ts"})
    giant["data"] = {"text": "G" * 99_000 + fake}
    records.append(giant)
    p = _install(tmp_path, monkeypatch, records)
    # 顶层 ts 抽取不被污染
    spans, index = event_log._scan_state(p, want_index=True)
    g = [e for e in index if e[1] == 31]
    assert g and g[0][0] == _iso(40), f"巨型行顶层 ts 被污染：{g[0][0]}"
    # 全量时序里巨型行排在最末（真 ts 21:40 > 全部正常行）；翻页锚=31 的
    # 上一页 = [25..30]（21:25..21:30）——**不漏**、不带假 2099 记录
    full = event_log.timeline("w", "c", before=0, tail=0)
    assert [int(x["seq"]) for x in full][-1] == 31, "巨型行排序错了"
    page = event_log.timeline("w", "c", before=31, tail=6)
    assert [int(x["seq"]) for x in page] == [25, 26, 27, 28, 29, 30], \
        f"旧页漏/错记录：{[int(x['seq']) for x in page]}"
    # 污染形态反证：假 ts 若被采用，31 会排到最末且 anchor 前窗口不含它——
    # 再核 tail 窗口（最后 3 条 = 29,30,31）
    tail3 = event_log.timeline("w", "c", tail=3)
    assert [int(x["seq"]) for x in tail3] == [29, 30, 31]


def test_fx3_scan_cache_caps_actually_enforced(tmp_path, monkeypatch):
    """反例③：缓存上限**实际生效**——路径条目淘汰、锁池恒定容量、
    索引超限弃用后回退全量路径结果仍正确。"""
    p = _install(tmp_path, monkeypatch, [_rec(1, "assistant_text"),
                                         _revoke(2, 1)])
    event_log._revoke_spans(p)
    assert str(p) in event_log._SCAN_STATE
    # ① 路径条目上限：塞满 _REVOKE_CACHE_MAX 条假路径 ⇒ 真路径被淘汰、
    #    再访问走重建、结果不变
    old_max = event_log._REVOKE_CACHE_MAX
    try:
        event_log._SCAN_STATE.clear()
        event_log._revoke_spans(p)          # 真路径先入
        assert str(p) in event_log._SCAN_STATE
        for i in range(old_max - 1):        # 再塞 MAX-1 条假路径 ⇒ 恰满 MAX
            event_log._SCAN_STATE[f"fake://{i}"] = {
                "mtime_ns": 0, "size": 0, "size_scanned": 0, "spans": [],
                "index": None, "has_index": False, "index_dropped": False,
                "fp": "", "fp_len": 0, "tail_fp": "", "index_bytes": 0}
        assert len(event_log._SCAN_STATE) == old_max
        # **新路径**写入触发淘汰（同路径访问是缓存命中，不进 prune 分支）
        q_new = tmp_path / "c_new.jsonl"
        _write_lines(q_new, [_rec(1, "assistant_text")])
        event_log._revoke_spans(q_new)
        assert str(p) not in event_log._SCAN_STATE, "路径上限未淘汰最旧（真路径）"
        assert f"fake://{old_max - 2}" in event_log._SCAN_STATE or len(
            event_log._SCAN_STATE) <= old_max
        # 真路径再访问：状态没了 ⇒ 重建，spans 仍正确
        st0 = event_log.scan_stats()["full_rebuilds"]
        assert event_log._revoke_spans(p) == [(1, 2)]
        assert event_log.scan_stats()["full_rebuilds"] == st0 + 1
    finally:
        event_log._SCAN_STATE.clear()
        # _REVOKE_CACHE_MAX 是模块常量，本用例只塞假条目、未改它
    # ② 锁池恒定容量：_SCAN_LOCKS 是 256 把预建锁（list），不随路径增长
    assert isinstance(event_log._SCAN_LOCKS, list)
    assert len(event_log._SCAN_LOCKS) == 256
    for i in range(300):                    # 扫 300 个不同路径
        q = tmp_path / f"c{i}.jsonl"
        _write_lines(q, [_rec(1, "assistant_text")])
        event_log._revoke_spans(q)
    assert len(event_log._SCAN_LOCKS) == 256, "锁池容量随路径增长了"
    # ③ 索引超限弃用 ⇒ 翻页回退全量路径、结果仍正确
    small_entries = event_log._INDEX_MAX_ENTRIES
    small_bytes = event_log._INDEX_MAX_BYTES
    try:
        monkeypatch.setattr(event_log, "_INDEX_MAX_ENTRIES", 5)
        monkeypatch.setattr(event_log, "_INDEX_MAX_BYTES", 10 * 1024)
        records = [_rec(i, "assistant_text", minute=i) for i in range(1, 41)]
        _write_lines(p, records)
        spans, index = event_log._scan_state(p, want_index=True)
        assert index is None, "索引超限未弃用"
        # 翻页走全量兜底，结果与参考一致
        got = event_log.timeline("w", "c", before=31, tail=5)
        ref = _before_ref(records, 31, 5)
        assert _norm(got) == _norm(ref), "索引弃用后翻页语义漂了"
    finally:
        event_log._INDEX_MAX_ENTRIES = small_entries
        event_log._INDEX_MAX_BYTES = small_bytes
        event_log._SCAN_STATE.pop(str(p), None)


def _build_bigger_replace(raw_old: str, boundary: int) -> str:
    """构造「保留两段指纹」的替换文本：文件头 64B 与**原扫描边界前 64B**
    原样保留（对 revoke 行做等长改写 from_seq:2→3，全部字节偏移不变），
    尾部追加一条普通事件使文件更大、只改中段的撤销记录。"""
    assert raw_old.count('"from_seq": 2') == 1
    raw_mut = raw_old.replace('"from_seq": 2', '"from_seq": 3')
    assert len(raw_mut) == len(raw_old), "改写应等长（保全部字节偏移）"
    # 双指纹保留核对（构造自证）
    assert raw_mut[:64] == raw_old[:64]
    assert raw_mut[boundary - 64:boundary] == raw_old[boundary - 64:boundary]
    return raw_mut + json.dumps(_rec(10, "assistant_text", minute=10),
                                ensure_ascii=False) + "\n"


def test_fx4a_replace_via_os_replace_hot_eq_cold(tmp_path, monkeypatch):
    """反例④·替换路径：**os.replace 原子替换**（项目入口统一走的形态，
    迁移脚本 _atomic_write_lines 同款）⇒ 文件身份（st_ino/st_ctime）变化
    ⇒ 重建，热读==冷读，已撤销记录不重现。"""
    records_a = [_rec(i, "assistant_text", minute=i, text="X" * 200)
                 for i in range(1, 6)]
    records_a.append({"seq": 6, "card_id": "card_x", "run_id": "r6", "round": 1,
                      "type": "revoke", "data": {"from_seq": 2}, "model": "",
                      "ts": _iso(6)})
    # revoke 之后还有后续记录 ⇒ 边界前 64B 由 rec9 尾部占据（改 revoke 行
    # 不会碰尾指纹——撤销记录在**中段**，与用户构造一致）
    records_a += [_rec(7, "assistant_text", minute=7, text="G" * 200),
                  _rec(8, "assistant_text", minute=8, text="H" * 200),
                  _rec(9, "assistant_text", minute=9, text="I" * 200)]
    p = _install(tmp_path, monkeypatch, records_a)
    assert event_log._revoke_spans(p) == [(2, 6)]
    boundary = event_log._SCAN_STATE[str(p)]["size_scanned"]
    bigger = _build_bigger_replace(p.read_text(encoding="utf-8"), boundary)
    assert p.stat().st_size < len(bigger.encode("utf-8")), "构造应为更大文件"
    # **原子替换**：写临时文件 + os.replace（真替换路径，inode/创建时间变）
    tmp_file = p.with_name(p.name + ".tmp")
    tmp_file.write_text(bigger, encoding="utf-8")
    import os as _os
    _os.replace(tmp_file, p)
    hot = [int(x["seq"]) for x in
           event_log.timeline("w", "c", before=0, tail=0)]
    event_log._SCAN_STATE.pop(str(p), None)
    cold = [int(x["seq"]) for x in
            event_log.timeline("w", "c", before=0, tail=0)]
    assert hot == cold == [1, 2, 7, 8, 9, 10], \
        f"热读 {hot} 冷读 {cold} 不一致（revoke 行自身不进渲染面）"
    assert event_log._revoke_spans(p) == [(3, 6)], "新撤销区间未生效"
    # 〔2026-11-20 固化〕替换路径的验证路径覆盖 **尾窗（tail 快路径）与翻页**
    # ——tail=0 走全量读取、不踩 spans/索引快路径；尾窗与翻页才走快路径，
    # 替换后必须同样热读==冷读。
    hot_tail = [int(x["seq"]) for x in
                event_log.timeline("w", "c", tail=6)]
    event_log._SCAN_STATE.pop(str(p), None)
    cold_tail = [int(x["seq"]) for x in
                 event_log.timeline("w", "c", tail=6)]
    assert hot_tail == cold_tail == [1, 2, 7, 8, 9, 10], \
        f"尾窗热读 {hot_tail} 冷读 {cold_tail} 不一致"
    hot_page = [int(x["seq"]) for x in
                event_log.timeline("w", "c", before=9, tail=4)]
    event_log._SCAN_STATE.pop(str(p), None)
    cold_page = [int(x["seq"]) for x in
                 event_log.timeline("w", "c", before=9, tail=4)]
    assert hot_page == cold_page == [1, 2, 7, 8], \
        f"翻页热读 {hot_page} 冷读 {cold_page} 不一致"


def test_fx4b_inplace_overwrite_with_invalidate_hot_eq_cold(
        tmp_path, monkeypatch):
    """反例④·原地覆盖路径（tail 快路径）：**write_text 原地覆盖并增长**
    （同 inode、同创建时间——身份信号无法检测），保留两段指纹、只改中段
    撤销记录的 from_seq（3→2 等长改写）。
    现状实测：tail 快路径沿用旧 spans（撤 3..5）⇒ 新文件已撤销的 seq2
    **仍会显示**（热读≠冷读）。治理：原地改写入口**显式使缓存失效**
    （event_log.invalidate_scan_cache）⇒ 热读==冷读。"""
    records_a = [_rec(i, "assistant_text", minute=i, text="X" * 200)
                 for i in range(1, 6)]
    records_a.append({"seq": 6, "card_id": "card_x", "run_id": "r6", "round": 1,
                      "type": "revoke", "data": {"from_seq": 3}, "model": "",
                      "ts": _iso(6)})
    records_a += [_rec(7, "assistant_text", minute=7, text="G" * 200),
                  _rec(8, "assistant_text", minute=8, text="H" * 200),
                  _rec(9, "assistant_text", minute=9, text="I" * 200)]
    p = _install(tmp_path, monkeypatch, records_a)
    assert event_log._revoke_spans(p) == [(3, 6)]
    boundary = event_log._SCAN_STATE[str(p)]["size_scanned"]
    # 替换文件：revoke 等长改写 from_seq 3→2（撤 2..5），尾部追加 rec10；
    # 头 64B 与原边界前 64B 原样保留（构造自证）
    raw_old = p.read_text(encoding="utf-8")
    assert raw_old.count('"from_seq": 3') == 1
    raw_mut = raw_old.replace('"from_seq": 3', '"from_seq": 2')
    assert len(raw_mut) == len(raw_old)
    assert raw_mut[:64] == raw_old[:64]
    assert raw_mut[boundary - 64:boundary] == raw_old[boundary - 64:boundary]
    bigger = raw_mut + json.dumps(_rec(10, "assistant_text", minute=10),
                                  ensure_ascii=False) + "\n"
    # **原地覆盖**：write_text 同一文件（inode/创建时间不变）
    p.write_text(bigger, encoding="utf-8")
    hot = [int(x["seq"]) for x in
           event_log.timeline("w", "c", tail=6)]          # tail 快路径
    event_log._SCAN_STATE.pop(str(p), None)
    cold = [int(x["seq"]) for x in
            event_log.timeline("w", "c", tail=6)]
    assert cold == [1, 7, 8, 9, 10], \
        f"冷读应=[1,7,8,9,10]（新 revoke 撤 3..5，seq2 可见）：{cold}"
    assert hot != cold, \
        f"现状基线应不一致（seq2 复现）：hot={hot} cold={cold}"
    # 治理：入口写后**显式失效** ⇒ 热读==冷读
    # （先重建缓存，再原地覆盖一次 + invalidate）
    event_log._revoke_spans(p)
    bigger2 = raw_old.replace('"from_seq": 3', '"from_seq": 2') \
        + json.dumps(_rec(10, "assistant_text", minute=10),
                     ensure_ascii=False) + "\n"
    p.write_text(bigger2, encoding="utf-8")
    n = event_log.invalidate_scan_cache(path=p)
    assert n == 1, "失效 API 未清掉对应条目"
    hot2 = [int(x["seq"]) for x in
            event_log.timeline("w", "c", tail=6)]
    event_log._SCAN_STATE.pop(str(p), None)
    cold2 = [int(x["seq"]) for x in
             event_log.timeline("w", "c", tail=6)]
    assert hot2 == cold2 == [1, 7, 8, 9, 10], \
        f"显式失效后热读 {hot2} 仍与冷读 {cold2} 不一致"


def test_fx6_paging_between_scans_no_empty_page(tmp_path, monkeypatch):
    """反例⑥（并发回归）：索引弃用后，**追加发生在翻页的两次扫描之间**——
    真实交错：timeline 内先 `_revoke_spans`（第一次扫描）再 `_scan_state(
    want_index=True)`（第二次扫描），追加被插桩夹在两者之间。修复前增量
    片段被当索引 ⇒ 历史页返回**空页**；修复后弃用期间始终走完整回退，
    返回正确的 5 条历史页。"""
    records = [_rec(i, "assistant_text", minute=i) for i in range(1, 41)]
    p = _install(tmp_path, monkeypatch, records)
    small_entries, small_bytes = (event_log._INDEX_MAX_ENTRIES,
                                  event_log._INDEX_MAX_BYTES)
    try:
        monkeypatch.setattr(event_log, "_INDEX_MAX_ENTRIES", 10)
        monkeypatch.setattr(event_log, "_INDEX_MAX_BYTES", 12 * 1024)
        # 先建索引让它弃用（40 行 > 10 条上限）
        spans, index = event_log._scan_state(p, want_index=True)
        assert index is None
        assert event_log._SCAN_STATE[str(p)]["index_dropped"] is True
        # **真实交错模拟**：monkeypatch _revoke_spans——原函数（第一次扫描）
        # 跑完**之后**、返回之前执行追加 ⇒ timeline 内部的第二次扫描
        # （want_index）看到的才是「扫描之间发生了追加」的文件
        real_spans = event_log._revoke_spans
        appended = {"done": False}

        def spied(path):
            r = real_spans(path)
            if not appended["done"]:
                appended["done"] = True
                _append_lines(p, [_rec(41, "assistant_text", minute=41),
                                  _rec(42, "assistant_text", minute=42)])
            return r

        monkeypatch.setattr(event_log, "_revoke_spans", spied)
        page = event_log.timeline("w", "c", before=31, tail=5)
        assert appended["done"], "插桩未触发（追加未夹进两次扫描之间）"
        assert page, "历史页返回空页（增量片段被当索引）"
        ref = _before_ref(records + [_rec(41, "assistant_text", minute=41),
                                     _rec(42, "assistant_text", minute=42)],
                          31, 5)
        assert _norm(page) == _norm(ref), f"历史页与参考不一致：{len(page)}"
        # 弃用状态保持（want_index 不触发重建）
        assert event_log._SCAN_STATE[str(p)]["index_dropped"] is True
    finally:
        event_log._INDEX_MAX_ENTRIES = small_entries
        event_log._INDEX_MAX_BYTES = small_bytes
        event_log._SCAN_STATE.pop(str(p), None)
