# -*- coding: utf-8 -*-
"""回填事件时间戳（events/*.jsonl 的 ``ts``）。

**为什么需要**：``ts`` 是 2026-10-17 对话界面商业化改造时新增的落盘字段
（见 ``src/storage/event_log._normalize``）。此前的事件记录没有时间，界面因此
无法显示消息时间、也做不了日期分隔。旧数据的时间并没有丢——它一直在
``sessions/*.md`` 的每个消息块头里（``ts:`` 键，写入时刻）。

**做什么**：把会话文件里逐条消息的 ``ts``，按「同 actor 顺序一一对应」回填到
事件流里对应的 ``user_message`` / ``assistant_text`` 记录上。

**不做什么**（边界，宁可留空也不编）：
  · 只回填 ``user_message`` 与 ``assistant_text`` 两类；过程事件（tool_call /
    round_start 等）**不猜时间**——它们没有对应的会话块。
  · 条数对不上（会话块与事件记录数量不等）⇒ 整卡跳过并报告，不硬配。
  · 已有 ``ts`` 的记录不动（幂等，可重复执行）。
  · 不改事件顺序、不改 seq、不动 data。

用法：
    python scripts/backfill_event_ts.py            # 全部 work，试运行（只报告）
    python scripts/backfill_event_ts.py --apply    # 真正写入（写前备份 .bak）
    python scripts/backfill_event_ts.py --work work_002 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

WORKS = BASE_DIR / "works"

MSG_TYPES = {"user_message": "human", "assistant_text": "assistant"}


def norm_actor(raw: str) -> str:
    """两代会话格式的 actor 归一：v1 写 role=user/assistant，v2 写 actor=human/assistant。"""
    a = (raw or "").strip().lower()
    if a in ("human", "user"):
        return "human"
    if a in ("assistant", "ai", "model"):
        return "assistant"
    return a


def parse_session_ts(path: Path) -> list[tuple[str, str]]:
    """解析会话文件的 (actor, ts) 序列（v1/v2 两代格式都认）。

    v2: ``---MSG---`` 头里是 ``key: value`` 行，``ts`` 直接可读。
    v1: 同样以 ``---MSG---`` 分块，头字段为 ``role`` / ``timestamp``。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for chunk in text.split("---MSG---")[1:]:
        head, _, rest = chunk.partition("\n---\n")
        if not rest:
            # 兜底：分隔行可能带 \r\n
            head, _, rest = chunk.partition("\r\n---\r\n")
        kv: dict[str, str] = {}
        for line in head.splitlines():
            line = line.strip()
            if not line or line.startswith("---"):
                continue
            k, sep, v = line.partition(":")
            if sep:
                kv[k.strip()] = v.strip()
        actor = kv.get("actor") or kv.get("role") or ""
        ts = kv.get("ts") or kv.get("timestamp") or ""
        if actor and ts:
            out.append((actor, ts))
    return out


def backfill_card(ev_path: Path, sess_path: Path, apply: bool) -> dict:
    """回填一张卡；返回统计。"""
    try:
        lines = ev_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"error": f"读事件失败: {exc}"}
    recs = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            recs.append(json.loads(ln))
        except json.JSONDecodeError:
            recs.append(None)   # 坏行原样保留（fail-open，同读侧口径）

    seq_pairs = parse_session_ts(sess_path)
    if not seq_pairs:
        return {"error": "会话文件无可用时间戳"}

    # 按角色分桶后**分别**顺序匹配：会话文件里可能夹着卡间消息块等非对话块，
    # 只要「user 块数 == user_message 数」且「assistant 块数 == assistant_text 数」
    # 就能一一对上，不要求两侧总条数相等（那会把这类卡全部误判成不匹配）。
    sess_users = [ts for (a, ts) in seq_pairs if norm_actor(a) == "human"]
    sess_assts = [ts for (a, ts) in seq_pairs if norm_actor(a) == "assistant"]
    ev_users = [i for i, r in enumerate(recs) if isinstance(r, dict) and r.get("type") == "user_message"]
    ev_assts = [i for i, r in enumerate(recs) if isinstance(r, dict) and r.get("type") == "assistant_text"]
    if len(ev_users) != len(sess_users) or len(ev_assts) != len(sess_assts):
        return {
            "error": (f"条数不匹配（user 事件 {len(ev_users)} ≠ 会话 {len(sess_users)}；"
                      f"assistant 事件 {len(ev_assts)} ≠ 会话 {len(sess_assts)}）"),
            "skipped": True,
        }

    changed = 0
    for idx_list, ts_list in ((ev_users, sess_users), (ev_assts, sess_assts)):
        for i, ts in zip(idx_list, ts_list):
            rec = recs[i]
            if rec.get("ts"):
                continue
            rec["ts"] = ts
            changed += 1
    total = len(ev_users) + len(ev_assts)

    if changed and apply:
        bak = ev_path.with_suffix(ev_path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(ev_path, bak)
        tmp = ev_path.with_suffix(ev_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in recs:
                if r is None:
                    continue
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ev_path)

    return {"filled": changed, "total": total}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只报告）")
    ap.add_argument("--work", default="", help="只处理某个 work_id")
    args = ap.parse_args()

    if not WORKS.exists():
        print(f"找不到 works 目录: {WORKS}")
        return 1

    works = [WORKS / args.work] if args.work else sorted(p for p in WORKS.iterdir() if p.is_dir())
    grand_filled = grand_total = grand_skip = 0
    for w in works:
        ev_dir = w / "events"
        ss_dir = w / "sessions"
        if not ev_dir.is_dir():
            continue
        for ev in sorted(ev_dir.glob("*.jsonl")):
            cid = ev.stem
            sess = ss_dir / f"session_{cid.split('_')[-1]}.md"
            if not sess.exists():
                sess = ss_dir / f"{cid}.md"
            if not sess.exists():
                print(f"  [skip] {w.name}/{cid}: 无对应会话文件")
                grand_skip += 1
                continue
            st = backfill_card(ev, sess, args.apply)
            if st.get("error"):
                print(f"  [skip] {w.name}/{cid}: {st['error']}")
                grand_skip += 1
                continue
            grand_filled += st["filled"]
            grand_total += st["total"]
            if st["filled"]:
                print(f"  [{'写入' if args.apply else '待写'}] {w.name}/{cid}: "
                      f"{st['filled']}/{st['total']} 条补上时间")

    print(f"\n合计：可补 {grand_filled} 条（消息总数 {grand_total}），跳过 {grand_skip} 个文件"
          f"{'，已写入' if args.apply else '，未写入（加 --apply 落盘）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
