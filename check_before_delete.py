# -*- coding: utf-8 -*-
"""删除废弃剧本前的机械判据检查器（SOP_清理废弃剧本.md 的可执行版本）。

用法：python check_before_delete.py <work_id>

输出：判定 = 禁止删除 / 可删除，并逐条给出判据命中情况。
判据（任一命中 → 禁止删除，上桌问用户）：
  R1 该剧本不在 TEMP_LEDGER.md 台账内
  R2 session 内存在 role: user 消息
  R3 work.json 的 title 不含临时标记词
  R4 创建时间距今 < 24h，或距最近一次用户活动 < 2h
"""
import sys, os, glob, json, subprocess, time

BASE = os.path.dirname(os.path.abspath(__file__))
TEMP_MARKS = ("临时", "_e2e", "test", "验证", "回归", "补测")


def _read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def ledger_ids():
    t = _read(os.path.join(BASE, "TEMP_LEDGER.md"))
    ids = set()
    for line in t.splitlines():
        s = line.strip()
        if s.startswith("|") and "work_" in s:
            for cell in s.split("|"):
                c = cell.strip()
                if c.startswith("work_"):
                    ids.add(c)
    return ids


def check(work_id):
    wdir = os.path.join(BASE, "works", work_id)
    res = {}
    res["exists"] = os.path.isdir(wdir)
    if not res["exists"]:
        return res

    # R1 台账
    ids = ledger_ids()
    res["R1_in_ledger"] = work_id in ids

    # R2 用户消息
    u = a = 0
    for s in glob.glob(os.path.join(wdir, "sessions", "*.md")):
        t = _read(s)
        u += t.count("role: user")
        a += t.count("role: assistant")
    res["R2_user_msgs"] = u
    res["R2_asst_msgs"] = a

    # R3 标题临时标记词
    title = ""
    wj = os.path.join(wdir, "work.json")
    if os.path.exists(wj):
        try:
            title = json.load(open(wj, encoding="utf-8")).get("title", "")
        except Exception:
            pass
    res["title"] = title
    res["R3_title_has_mark"] = any(m in title for m in TEMP_MARKS)

    # R4 时间距离
    created = None
    if os.path.exists(wj):
        try:
            created = json.load(open(wj, encoding="utf-8")).get("created_at")
        except Exception:
            pass
    age_h = None
    if created:
        try:
            from datetime import datetime
            c = datetime.fromisoformat(created)
            age_h = (datetime.now(c.tzinfo) - c).total_seconds() / 3600.0
        except Exception:
            pass
    res["created_at"] = created
    res["R4_age_hours"] = age_h

    # 用户最近活动（任意剧本最新 session 写入）
    latest = 0.0
    for s in glob.glob(os.path.join(BASE, "works", "*", "sessions", "*.md")):
        latest = max(latest, os.stat(s).st_mtime)
    active_h = (time.time() - latest) / 3600.0 if latest else 999
    res["user_last_activity_hours_ago"] = round(active_h, 2)

    # 裁决
    blocks = []
    if not res["R1_in_ledger"]:
        blocks.append("R1 不在临时品台账内（台账外一律是用户资产）")
    if res["R2_user_msgs"] > 0:
        blocks.append("R2 session 内有 %d 条用户消息" % res["R2_user_msgs"])
    if not res["R3_title_has_mark"]:
        blocks.append("R3 标题无临时标记词（title=%r）" % title)
    if age_h is not None and (age_h < 24 or active_h < 2):
        blocks.append("R4 时间距离过近：创建距今 %.2fh / 用户最近活动 %.2fh 前" % (age_h, active_h))
    res["blocks"] = blocks
    res["verdict"] = "禁止删除" if blocks else "可删除"
    return res


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python check_before_delete.py <work_id>")
        sys.exit(2)
    r = check(sys.argv[1])
    print(json.dumps(r, ensure_ascii=False, indent=2))
