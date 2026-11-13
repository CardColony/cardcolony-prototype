# -*- coding: utf-8 -*-
"""事件流 ``timeline`` 的**尾部窗口**回归网（[5715 · 治「切卡卡死」]）。

## 病根（实测）

`event_log.timeline()` 为了让 revoke（软删）按全量时序生效，对**全文件**逐行
``json.loads``（长会话 47492 行 / 13.8MB ⇒ **0.25~1.6 秒**），而且它是
**同步函数**、跑在 async 端点里 ⇒ **堵死事件循环** ⇒ 期间所有请求排队 ⇒
页面「没反应」、越点越堵、只能刷新。

## 修法

`tail=N` 路径改成：整文件按字节读一次（IO 便宜，十几 MB 约 20ms）⇒ 切行 ⇒
**只解析尾部需要的那些** ⇒ 收够 N 条就停；撤销语义用**区间并集**判定，
与改前「升序先收后撤」**逐字等价**。

## 本网钉住

1. **逐条等价**：有 revoke / 无 revoke / seq 缺失 / seq 重复，各档 tail 都与
   「全量＋撤销」的参考实现**逐条一致**（顺序、内容、条数）。
2. **不再全量解析**：解析次数 ≪ 文件行数（性能判据，防止以后有人改回全量）。
3. **不堵事件循环**：调用方（routes）必须把它放进线程（源码级断言）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.storage import event_log

ROOT = Path(__file__).resolve().parents[1]


def _write_events(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "card_x.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                 encoding="utf-8")
    return p


def _reference(records: list[dict], tail: int = 0, before: int = 0) -> list[dict]:
    """参考实现＝**改前**的语义：升序收集 → revoke 回撤 → before 截 → tail 截。

    与改前的 ``event_log.read()`` 对齐：**seq 缺失的记录被跳过**（改前
    ``if s is None or s < from_seq: continue``）。
    """
    items: list[dict] = []
    for rec in records:
        s = rec.get("seq")
        s = int(s) if isinstance(s, int) and not isinstance(s, bool) else (
            int(s) if isinstance(s, str) and s.isdigit() else None)
        if s is None:
            continue
        if str(rec.get("type") or "") in event_log.CONTROL_TYPES:
            f = (rec.get("data") or {}).get("from_seq")
            try:
                f = int(f)
            except (TypeError, ValueError):
                continue
            items = [x for x in items if int(x.get("seq") or 0) < f]
            continue
        items.append(dict(rec))
    if before:
        items = [x for x in items if int(x.get("seq") or 0) < int(before)]
    if tail:
        items = items[-int(tail):]
    return items


def _install(tmp_path: Path, monkeypatch, records: list[dict]):
    """把事件文件装到 event_log 认的位置，并返回该路径。"""
    p = _write_events(tmp_path, records)
    monkeypatch.setattr(event_log, "event_path", lambda w, c: p)
    return p


def _norm(items):
    return [(int(x.get("seq") or 0), str(x.get("type") or ""),
             json.dumps(x.get("data"), ensure_ascii=False, sort_keys=True))
            for x in items]


def test_tail_matches_reference_without_revoke(tmp_path, monkeypatch):
    records = [{"seq": i, "type": "assistant_text", "data": {"content": f"第{i}句"}}
               for i in range(1, 51)]
    _install(tmp_path, monkeypatch, records)
    for tail in (1, 7, 40, 50, 999):
        got = event_log.timeline("w", "c", tail=tail)
        assert _norm(got) == _norm(_reference(records, tail=tail)), f"tail={tail} 不等价"


def test_tail_matches_reference_with_revoke(tmp_path, monkeypatch):
    """**软删语义**：revoke{from_seq:N} 撤掉 N 起、到 revoke 自身之前的那一段；
    revoke **之后**的新记录照常进窗口。"""
    records = [
        {"seq": 1, "type": "user_message", "data": {"content": "问1"}},
        {"seq": 2, "type": "assistant_text", "data": {"content": "答1"}},
        {"seq": 3, "type": "user_message", "data": {"content": "问2"}},
        {"seq": 4, "type": "assistant_text", "data": {"content": "答2"}},
        {"seq": 5, "type": "revoke", "data": {"from_seq": 2}},
        {"seq": 6, "type": "user_message", "data": {"content": "问3"}},
        {"seq": 7, "type": "assistant_text", "data": {"content": "答3"}},
        {"seq": 8, "type": "user_message", "data": {"content": "问4"}},
        {"seq": 9, "type": "assistant_text", "data": {"content": "答4"}},
    ]
    _install(tmp_path, monkeypatch, records)
    for tail in (1, 2, 3, 4, 5, 8, 20):
        got = event_log.timeline("w", "c", tail=tail)
        assert _norm(got) == _norm(_reference(records, tail=tail)), f"tail={tail} 不等价"


def test_tail_matches_reference_with_nested_revoke(tmp_path, monkeypatch):
    """**嵌套 revoke**（后一个 from_seq 更小）⇒ 区间并集，仍须等价。"""
    records = [
        {"seq": 1, "type": "assistant_text", "data": {"content": "a"}},
        {"seq": 2, "type": "assistant_text", "data": {"content": "b"}},
        {"seq": 3, "type": "revoke", "data": {"from_seq": 2}},
        {"seq": 4, "type": "assistant_text", "data": {"content": "c"}},
        {"seq": 5, "type": "revoke", "data": {"from_seq": 1}},
        {"seq": 6, "type": "assistant_text", "data": {"content": "d"}},
        {"seq": 7, "type": "assistant_text", "data": {"content": "e"}},
    ]
    _install(tmp_path, monkeypatch, records)
    for tail in (1, 2, 3, 5, 10):
        got = event_log.timeline("w", "c", tail=tail)
        assert _norm(got) == _norm(_reference(records, tail=tail)), f"tail={tail} 不等价"


def test_tail_matches_reference_with_broken_and_duplicate_seq(tmp_path, monkeypatch):
    """**坏行**与**seq 重复**（同一条 tool_call 落多条 result）也要等价。"""
    records = [
        {"seq": 1, "type": "tool_result", "data": {"tool": "x"}},
        {"seq": 2, "type": "tool_result", "data": {"tool": "x"}},
        {"seq": 2, "type": "tool_result", "data": {"tool": "x"}},
        {"type": "tool_result", "data": {"tool": "no-seq"}},
        {"seq": 3, "type": "tool_result", "data": {"tool": "y"}},
        {"seq": 4, "type": "tool_result", "data": {"tool": "y"}},
    ]
    _install(tmp_path, monkeypatch, records)
    for tail in (1, 2, 3, 4, 6, 9):
        got = event_log.timeline("w", "c", tail=tail)
        assert _norm(got) == _norm(_reference(records, tail=tail)), f"tail={tail} 不等价"


def test_tail_path_does_not_parse_whole_file(tmp_path, monkeypatch):
    """**性能判据**：tail 路径不得逐行解析全文件（那是秒级卡死的根因）。"""
    n = 4000
    records = [{"seq": i, "type": "tool_result", "data": {"tool": "t", "i": i}}
               for i in range(1, n + 1)]
    _install(tmp_path, monkeypatch, records)
    real_loads = event_log._loads
    calls = {"n": 0}

    def counting_loads(s, *a, **kw):
        calls["n"] += 1
        return real_loads(s, *a, **kw)

    # 只 patch **模块内别名**（不碰全局 json——patch 全局会把 pytest 自己拖慢几十倍）
    monkeypatch.setattr(event_log, "_loads", counting_loads)
    got = event_log.timeline("w", "c", tail=50)
    assert len(got) == 50
    # 允许少量余量（revoke 扫描用子串命中，不解析），但**不允许**接近全量行数
    assert calls["n"] < n // 4, f"tail 路径解析了 {calls['n']} 次（文件 {n} 行）⇒ 又退回全量了"


def test_routes_must_call_timeline_in_a_thread():
    """**不许堵事件循环**：routes 的 timeline 端点必须把它放进线程。"""
    src = (ROOT / "src" / "api" / "routes.py").read_text(encoding="utf-8")
    assert re.search(r"asyncio\.to_thread\(\s*event_log\.timeline", src), (
        "routes.get_card_timeline 必须用 asyncio.to_thread 调 event_log.timeline"
        "（同步调用会堵死事件循环 ⇒ 切卡时所有请求排队 ⇒ 页面像死了）")


def test_tail_reads_last_line_without_trailing_newline(tmp_path, monkeypatch):
    """[2026-11-15 · mmap 倒读] 文件**末行不带 \\n**（写入中途被看到的样子）——
    倒逐行必须把末行也收进去，不能把它当成「不完整的尾巴」丢掉。"""
    records = [{"seq": i, "type": "assistant_text", "data": {"content": f"第{i}句"}}
               for i in range(1, 11)]
    p = tmp_path / "card_x.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records),
                 encoding="utf-8")          # 注意：结尾**没有**换行
    monkeypatch.setattr(event_log, "event_path", lambda w, c: p)
    got = event_log.timeline("w", "c", tail=3)
    assert [int(x["seq"]) for x in got] == [8, 9, 10], got
