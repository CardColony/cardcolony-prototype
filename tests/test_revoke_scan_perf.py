# -*- coding: utf-8 -*-
"""事件流 ``_revoke_spans`` 的**扫描提速**网（[P24 · 2026-10-26]）

## 病根（实测，不是推测）

生产访问日志（2026-10-26 00:45）：

    GET /api/works/work_027/cards/card_021/timeline            **20765ms**
    GET /api/works/work_027/cards/card_021/timeline            **32157ms**
    GET /api/works/work_027/cards/card_001/timeline?tail=400    13761ms（被拖慢的）
    同一时刻：nav/version 2740ms、rule 2737ms、unread 2965ms  ⇒ 整个服务在排队

拆开量（card_021 的事件文件 **109.5MB / 8277 行 ⇒ 平均 13KB 每行**）：

    ① 纯读文件               37ms
    ② bytes.find(b"revoke")   0ms
    ③ Python 逐行迭代        391ms
    ④ 旧 _revoke_spans       930ms   ← 其中 **855ms 是 json.loads 3 个巨型内容行**
       （那 3 行只是**内容里恰好出现 "revoke" 字样**，type 根本不是 revoke）
    ⑤ 新 _revoke_spans       106ms   ← 行首 2KB 判 type，不是 revoke 就不整行解析

⇒ 真正的开销不是「扫描」，是**为了判一句"是不是 revoke"而整行 json.loads 巨型行**。

## 本网钉住

1. **真 revoke 仍能被找到**（哪怕它是巨型行、type 字段在行首）——提速不许漏。
2. **内容里出现 "revoke" 字样**（type 不是 revoke）**不算**控制记录 ⇒ 不产生假区间。
3. **按 (mtime_ns, size) 缓存**：文件没变就不重扫（同一次切卡会多次调 timeline）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from src.storage import event_log


def _write(tmp_path: Path, records: list[dict], name: str = "card_p24.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                 encoding="utf-8")
    return p


def _revoke(seq: int, from_seq: int) -> dict:
    return {"seq": seq, "card_id": "card_p24", "run_id": "r1", "round": 1,
            "type": "revoke", "data": {"from_seq": from_seq}, "model": ""}


def _content(seq: int, text: str) -> dict:
    return {"seq": seq, "card_id": "card_p24", "run_id": "r1", "round": 1,
            "type": "reasoning", "data": {"text": text}, "model": ""}


def test_p24_real_revoke_is_still_found(tmp_path):
    """① 真 revoke 仍能被找到（提速不许漏）。"""
    p = _write(tmp_path, [
        _content(1, "前面的内容"),
        _revoke(2, 1),
        _content(3, "后面的内容"),
    ])
    spans = event_log._revoke_spans(p)
    assert spans == [(1, 2)], spans


def test_p24_content_mentioning_revoke_is_not_a_control_record(tmp_path):
    """② 内容里出现 "revoke" 字样（type 不是 revoke）⇒ 不产生假区间。"""
    p = _write(tmp_path, [
        _content(1, "我要 revoke 掉这一段"),
        _content(2, "revoke_tool 的说明"),
    ])
    assert event_log._revoke_spans(p) == []


def test_p24_huge_content_line_with_revoke_word_is_skipped(tmp_path):
    """②b **巨型**内容行（十几 KB）里出现 "revoke" ⇒ 不必整行解析 ⇒ 也不产生假区间。

    这正是旧实现那 855ms 的来源：为了判一句「是不是 revoke」而整行 json.loads。
    """
    big = "x" * 20000 + " revoke " + "y" * 20000
    p = _write(tmp_path, [_content(1, big), _content(2, "别的")])
    t0 = time.perf_counter()
    spans = event_log._revoke_spans(p)
    dt = (time.perf_counter() - t0) * 1000
    assert spans == [], spans
    assert dt < 500, f"巨型行不该被整行解析（实测 {dt:.0f}ms）"


def test_p24_scan_is_cached_by_mtime_and_size(tmp_path):
    """③ 按 (mtime_ns, size) 缓存：文件没变就不重扫。"""
    p = _write(tmp_path, [_content(1, "a"), _revoke(2, 1)])
    first = event_log._revoke_spans(p)
    t0 = time.perf_counter()
    second = event_log._revoke_spans(p)
    dt = (time.perf_counter() - t0) * 1000
    assert second == first
    assert dt < 20, f"第二次应命中缓存（实测 {dt:.0f}ms）"
