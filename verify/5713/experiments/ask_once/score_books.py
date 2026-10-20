# -*- coding: utf-8 -*-
"""任务书形态 · 细化判据（v2 指标）。

问题：v1/v2 的指标把两种东西混成了一种——
  (a) 「查什么／判什么」的**验收维度清单**（任务级，合理）；
  (b) 「用什么命令／什么参数／怎么调用工具」的**实现泄漏**（父卡把自己的知识写进子卡，正是病理）。
只有 (b) 才是 sub_agent 化的判据；(a) 在使命书里出现并不算错。

本脚本对证据里保存的子卡任务书原文重算两类指标：
  ops_leak      —— 实现泄漏命中（sqlite3/PRAGMA/mode=ro/json.load/curl/python/命令如下…）
  tool_calls    —— 明示工具名调用（read_card/write_file/python/bash/update_status…）
  artifact_spec —— 产物路径级规定（tmp/xxx.json、healthcheck_xxx/…）
  dims          —— 编号维度清单条数（1. 2. 3. …）
  accept        —— 有无验收判据
形态重分类（按泄漏为主）：
  manual  (ops_leak ≥ 3 或 tool_calls ≥ 2)   —— 操作手册（病理）
  blue    (artifact_spec ≥ 2 且 ops_leak ≤ 1) —— 蓝图（给产物契约，不给做法）
  mission (ops_leak ≤ 1 且 dims ≤ 8 且 accept) —— 使命书
  other

用法：python score_books.py evidence_v2.json evidence_v3.json [--out books.json]
"""
from __future__ import annotations

import json
import re
import sys

OPS = re.compile(r"(sqlite3|PRAGMA|mode=ro|file:.*\?mode|json\.load|json\.loads|print\(|"
                 r"os\.walk|curl|for /d|dir /b|\bpython\b|\bbash\b|命令行|命令如下|"
                 r"脚本如下|逐行解析|加密|checkpoint|VACUUM)", re.I)
TOOLC = re.compile(r"(`?(read_card|write_file|edit_file|update_status|append_ruling|"
                   r"update_shadow|create_card|send_card_message|link_cards|bash|python|"
                   r"get_goal|update_goal|read_inbox)`?)")
ARTI = re.compile(r"(tmp_restore/|tmp/|\btmp\\|healthcheck_[A-Za-z0-9_]+/|"
                  r"[A-Za-z0-9_]+\.json\b|[A-Za-z0-9_]+\.md\b)")
DIMS = re.compile(r"^\s*(\d+)[\.、\)]\s", re.M)
ACCEPT = re.compile(r"(验收|判据|通过标准|退回|核查表|成功标准)")


def score(text: str) -> dict:
    text = text or ""
    ops = len(OPS.findall(text))
    tools = len({m.group(2) for m in TOOLC.finditer(text)})
    arti = len(ARTI.findall(text))
    dims = len(DIMS.findall(text))
    accept = bool(ACCEPT.search(text))
    if ops >= 3 or tools >= 2:
        cls = "manual"
    elif arti >= 2 and ops <= 1 and accept:
        cls = "blue"
    elif ops <= 1 and dims <= 8 and accept:
        cls = "mission"
    else:
        cls = "other"
    return {"chars": len(text), "ops_leak": ops, "tool_names": tools,
            "artifact_spec": arti, "dim_items": dims, "accept": accept, "cls": cls}


def main(paths):
    out = []
    for path in paths:
        ev = json.load(open(path, encoding="utf-8"))
        row = {"evidence": path.split("\\")[-1], "model": ev.get("model"), "arms": []}
        for arm in ev.get("arms", []):
            books = []
            for c in arm.get("children", []):
                s = score(c.get("goal_text") or "")
                books.append({"card_id": c["card_id"], "title": c["title"], **s})
            n = len(books) or 1
            row["arms"].append({
                "arm": arm.get("arm"), "work_id": arm.get("work_id"),
                "phase": arm.get("phase"),
                "plan_chars": [len(arm.get("plan_pre") or ""), len(arm.get("plan_post") or "")],
                "plan_changed": arm.get("plan_changed"),
                "children_before_ask": arm.get("children_before_ask"),
                "n_children": len(books),
                "avg_ops_leak": round(sum(b["ops_leak"] for b in books) / n, 2),
                "avg_tool_names": round(sum(b["tool_names"] for b in books) / n, 2),
                "avg_artifact": round(sum(b["artifact_spec"] for b in books) / n, 2),
                "avg_dims": round(sum(b["dim_items"] for b in books) / n, 2),
                "accept_ratio": f"{sum(1 for b in books if b['accept'])}/{len(books)}",
                "cls": {k: sum(1 for b in books if b["cls"] == k)
                        for k in ("manual", "blue", "mission", "other")},
                "books": books,
            })
        out.append(row)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])