# -*- coding: utf-8 -*-
"""异常正文隔离·存量治理脚本（2026-11-20 · 方案 §3.4/§5）。

用法：
    python scripts/quarantine_blocks.py work_073 card_057            # 只列命中清单
    python scripts/quarantine_blocks.py work_073 card_057 --apply    # 打标＋修板

流程（对齐方案「先列出命中的回合标识，确认影响范围，再更新相应板项」）：
1. 事件账（events/{card}.jsonl）里的 pseudo_call_unparsed/pseudo_call_refused/
   rescue_refused → 按 head 前缀在 session_blocks 锚定异常回复块（确认清单）；
2. 扫描未打标 assistant_text 块里含调用家具的行 → 候选清单（无事件背书，
   只列出不自动处理——人工核对样本，如方案里的 10:02 末跳）；
3. --apply：确认清单逐块 UPDATE data（quarantine＋ctx_content=
   text_tool_rescue.quarantine_view），再整卡 rebuild_board
   （板=衍生视图，重建与新增写口同口径）；
4. 打印前后对比：命中块原文/视图长度、板行数、投影长度——验收附件。

原文只在会话/事件/界面档案面；本脚本不删任何块。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # 以 src.* 包名导入（storage 内有 ..infra 相对引用）

from src.api import text_tool_rescue  # noqa: E402
from src.storage import context_board  # noqa: E402

_REASON_BY_EVENT = {
    "pseudo_call_unparsed": "pseudo_unparsed",
    "pseudo_call_refused": "pseudo_refused_strict",
    "rescue_refused": "pseudo_over_cap",
}


def _db(work_id: str) -> Path:
    return ROOT / "works_data" / work_id / "cards.db"


def _events(work_id: str, card_id: str) -> list[dict]:
    p = ROOT / "works" / work_id / "events" / f"{card_id}.jsonl"
    out = []
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if "pseudo_call" not in ln and "rescue_refused" not in ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("type") in _REASON_BY_EVENT:
            out.append(d)
    return out


def _find_block(con: sqlite3.Connection, card_id: str, head: str,
                event_ts: str) -> int | None:
    """事件 head（前 160 字符原文）→ 含它的 assistant_text 块 seq。

    head 可能太通用（"components/compos" 撞一片旧块）——取**块时间戳
    与事件时刻最近**的匹配（块在收口落盘，紧随事件）。
    """
    key = (head or "").strip()[:40]
    if not key:
        return None
    rows = con.execute(
        "SELECT seq, data FROM session_blocks "
        "WHERE card_id=? AND type='assistant_text' AND instr(content,?)>0",
        (card_id, key)).fetchall()
    best, best_dt = None, None
    for seq, data in rows:
        try:
            bts = str(json.loads(data or "{}").get("timestamp") or "")
        except Exception:
            bts = ""
        dt = abs(_sec(bts) - _sec(event_ts)) if bts and event_ts else 9e18
        if best_dt is None or dt < best_dt:
            best, best_dt = int(seq), dt
    return best


def _sec(ts: str) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def collect_confirmed(work_id: str, card_id: str) -> list[dict]:
    """事件背书的命中清单：seq / 事件ts / 原因 / 原文长。"""
    con = sqlite3.connect(_db(work_id))
    con.row_factory = sqlite3.Row
    out, seen = [], set()
    for ev in _events(work_id, card_id):
        head = str((ev.get("data") or {}).get("head") or "")
        seq = _find_block(con, card_id, head,
                          str(ev.get("ts") or ""))
        if seq is None or seq in seen:
            continue
        seen.add(seq)
        n = con.execute(
            "SELECT length(content) FROM session_blocks "
            "WHERE card_id=? AND seq=?", (card_id, seq)).fetchone()[0]
        out.append({"seq": seq, "event_ts": str(ev.get("ts") or "")[:19],
                    "reason": _REASON_BY_EVENT[ev["type"]],
                    "chars": int(n or 0)})
    con.close()
    return out


def scan_candidates(work_id: str, card_id: str) -> list[dict]:
    """无事件背书的候选：未打标 assistant_text 且内容含调用家具行。"""
    con = sqlite3.connect(_db(work_id))
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute(
            "SELECT seq, content, data FROM session_blocks "
            "WHERE card_id=? AND type='assistant_text'", (card_id,)):
        try:
            d = json.loads(r["data"] or "{}")
        except Exception:
            d = {}
        if d.get("quarantine"):
            continue
        view = text_tool_rescue.quarantine_view(str(r["content"] or ""))
        raw = str(r["content"] or "")
        # 视图显著短于原文（家具行被剔）⇒ 疑似含未解析调用文本
        if len(view) + 40 < len(raw):
            out.append({"seq": int(r["seq"]), "chars": len(raw),
                        "view_chars": len(view)})
    con.close()
    return out


def apply_marks(work_id: str, card_id: str, hits: list[dict]) -> None:
    """逐块写 quarantine＋ctx_content（json_set 并进 data），再整卡重建板。"""
    con = sqlite3.connect(_db(work_id), timeout=30)
    try:
        for h in hits:
            raw = con.execute(
                "SELECT content FROM session_blocks "
                "WHERE card_id=? AND seq=?", (card_id, h["seq"])).fetchone()[0]
            raw = str(raw or "")
            # 含仿回执（function_results/结果壳）⇒ pseudo_receipt 比事件原因
            # 更准（card_049:8 段假回执实录）。存量块不含〔系统结果〕判据——
            # 宿主真实系统结果行会落进块正文（误标先例），只信硬家具。
            reason = ("pseudo_receipt"
                      if text_tool_rescue.detect_pseudo_receipt(
                          raw, include_sys_lines=False)
                      else h["reason"])
            view = text_tool_rescue.quarantine_view(raw)
            con.execute(
                "UPDATE session_blocks SET data="
                "json_set(data,'$.quarantine',?,'$.ctx_content',?) "
                "WHERE card_id=? AND seq=?",
                (reason, view, card_id, h["seq"]))
            h["reason"], h["view_chars"] = reason, len(view)
        con.commit()
    finally:
        con.close()
    n = context_board.rebuild_board(work_id, card_id)
    print(f"[board] rebuild {card_id}: {n} 行")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    work_id, card_id = sys.argv[1], sys.argv[2]
    apply = "--apply" in sys.argv

    hits = collect_confirmed(work_id, card_id)
    print(f"=== 事件背书命中清单（{len(hits)}）===")
    for h in hits:
        print(f"  seq={h['seq']:>5} | {h['event_ts']} | {h['reason']:>22} "
              f"| 原文 {h['chars']} 字符")
    cands = scan_candidates(work_id, card_id)
    unknown = [c for c in cands
               if c["seq"] not in {h["seq"] for h in hits}]
    if unknown:
        print(f"=== 无事件候选（{len(unknown)}，人工核对，不自动处理）===")
        for c in unknown:
            print(f"  seq={c['seq']:>5} | 原文 {c['chars']} → 视图 {c['view_chars']}")

    if not apply:
        print("\n（--apply 落账：data 写 quarantine+ctx_content，整卡重建板；"
              "无事件候选需 --include-seq 显式并入）")
        return 0
    # 人工核对后显式并入的候选 seq（如 10:02 检测缺口块——核对确属调用家具
    # 才给进来，无事件背书的绝不自动处理）。
    for s in (x.split("=", 1)[1] for x in sys.argv
              if x.startswith("--include-seq=")):
        seq = int(s)
        if seq and seq not in {h["seq"] for h in hits}:
            hits.append({"seq": seq, "event_ts": "manual-review",
                         "reason": "pseudo_unparsed", "chars": -1})
    if not hits:
        print("无确认命中，不落账。")
        return 0
    apply_marks(work_id, card_id, hits)
    print("\n=== 已打标 ===")
    for h in hits:
        print(f"  seq={h['seq']:>5} | 原文 {h['chars']} → 视图 {h['view_chars']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
