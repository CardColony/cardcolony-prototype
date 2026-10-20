# -*- coding: utf-8 -*-
"""臂行为分析：把「问一次」之后根卡**实际动了什么**从事件流里抠出来。

驱动器只记 create_card 调用与卡面形态；但真正区分「改判」与「只改措辞」的是
注入之后的动作序列：link_cards / send_card_message / write_file / append_ruling /
update_status / create_card / delete_card / resume_child …

用法：
    python analyze_arm.py --evidence evidence_v3.json [--out analysis_v3.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ACTION_TOOLS = {
    "create_card", "create_coder_card", "delete_card", "edit_card",
    "update_status", "append_ruling", "update_shadow", "link_cards", "link_rel",
    "unlink_cards", "send_card_message", "resume_child", "update_goal",
    "write_file", "edit_file", "create_goal",
}


def load_events(sandbox_root, wid, cid):
    fp = os.path.join(sandbox_root, "works", wid, "events", f"{cid}.jsonl")
    if not os.path.exists(fp):
        return []
    out = []
    with open(fp, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def summarize_arm(ev, sandbox_root, arm):
    wid = arm.get("work_id")
    root = arm.get("root") or "card_001"
    evs = load_events(sandbox_root, wid, root)

    # 注入时刻：事件流里 user_message 中除首条任务外的那些
    injects = []
    for e in evs:
        if str(e.get("type")) != "user_message":
            continue
        txt = str((e.get("data") or {}).get("content") or "")
        injects.append({"ts": str(e.get("ts")), "content": txt[:120]})
    marks = [i["ts"] for i in injects[1:]]  # 首条是任务本身

    def stage_of(ts):
        n = sum(1 for m in marks if m <= ts)
        return f"after_inject_{n}" if n else "before_inject"

    actions = []
    for e in evs:
        if str(e.get("type")) != "tool_call":
            continue
        d = e.get("data") or {}
        tool = str(d.get("tool"))
        if tool not in ACTION_TOOLS:
            continue
        args = d.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        args = args or {}
        brief = (args.get("title") or args.get("card_id") or args.get("target_id")
                 or args.get("action") or args.get("path") or "")
        actions.append({"ts": str(e.get("ts")), "stage": stage_of(str(e.get("ts"))),
                        "tool": tool, "brief": str(brief)[:80]})

    counts = {}
    for a in actions:
        counts[a["tool"]] = counts.get(a["tool"], 0) + 1
    post = [a for a in actions if a["stage"] != "before_inject"]

    return {
        "arm": arm.get("arm"), "work_id": wid, "model": arm.get("model"),
        "injects": injects[1:],
        "action_counts_total": counts,
        "action_counts_after_inject": {k: sum(1 for a in post if a["tool"] == k)
                                       for k in {a["tool"] for a in post}},
        "actions_after_inject": post,
        "plan_chars_pre": len(arm.get("plan_pre") or ""),
        "plan_chars_post": len(arm.get("plan_post") or ""),
        "plan_changed": arm.get("plan_changed"),
        "children_summary": arm.get("summary"),
        "children_before_ask": arm.get("children_before_ask"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--sandbox", default=r"D:\AI_station\推演agent\.worktrees\5713_sandbox")
    args = ap.parse_args()
    ev = json.load(open(args.evidence, encoding="utf-8"))
    sandbox_root = str((ev.get("instance") or {}).get("sandbox_root") or args.sandbox)
    out = {"evidence": os.path.basename(args.evidence),
           "model": ev.get("model"), "arms": []}
    for arm in ev.get("arms", []):
        out["arms"].append(summarize_arm(ev, sandbox_root, arm))
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
        print(f"写入 {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())