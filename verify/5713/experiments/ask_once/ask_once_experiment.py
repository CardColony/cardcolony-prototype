# -*- coding: utf-8 -*-
"""「问一次」拆分实验驱动器（5713 沙箱 · 真机实测）。

测什么：一张自动模式根卡接到同一份复杂任务时——
  · 对照组（control）：方案被直接批复「按此执行」；
  · 实验组（ask）    ：批复前先被「问一次」（不含任何拆分知识、不给对立面锚点的
                        裸问：这拆法有没有问题？有没有更好的拆法？），允许改方案后再批复；
两组最终派出的子卡任务书（create_card 的 goal/title 原文）形态是否有机械可判的差异。

安全：全程只打 5713 隔离实例（__instance__ 三验不符即中止）；所有 work 建在
5713_sandbox 数据根内；证据收集完毕后把实验 work 里所有卡 stop + auto_mode=0，
防止沙箱里的子卡无限空转烧模型额度。

用法（实例已在 5713 端口监听后）：
    python ask_once_experiment.py --base http://127.0.0.1:5713 \
        --out evidence_ask_once_<ts>.json --log run.log
设计为后台长跑：由调用方 nohup/start 起，轮询 run.log 与 evidence 文件看进度。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

POLL = 15                     # 轮询间隔（秒）
PLAN_WAIT = 30 * 60           # 等方案落现状块+pause 的最长等待
DONE_WAIT = 45 * 60           # 批复后等收尾的最长等待
STABLE_ROUNDS = 4             # 子卡数连续 N 轮不变视为拆分完成

# ── 任务（两组逐字相同；方案→pause→等批复 的流程要求是公共底座，不是干预）──
TASK = """你在 5713 沙箱实例里。任务：完成一次「实验环境数据健康巡检」并交付巡检报告。

巡检范围（都在你所在实例的沙箱数据内）：
- works 卡片数据的完整性（卡数据、事件流、会话留痕是否缺块/损坏/不可读）
- 快照与恢复路径（数据快照是否在位、能否据此还原）
- 运行日志的留痕质量（logs 是否可读、能否据此复盘一次运行）

交付物：一份巡检报告（发现的问题清单＋每条严重度＋修复建议＋「哪些是好的」），写成一个文件，并在你的现状块里给出路径。

这件事由你全权负责：怎么组织力量、要不要拆给子卡、拆几张，都由你定。

硬约束：所有读写只在你所在实例的沙箱数据范围内；不许碰 5700。

流程要求（机械，必须照做）：
1. 先勘探环境，再定拆分方案；
2. 在建任何子卡之前，把拆分方案写进你的现状块（每张子卡一行：标题＋一句话使命＋验收判据；若判断不需要拆，写明理由）；
3. 然后 update_goal(pause) 等批复——没收到批复前不许建任何子卡；
4. 收到批复后按批复推进；子事收口后你自己验收，再 update_goal(complete)。"""

REPLIES = {
    # 对照组：零内容批复
    "control": "批复：方案收到，按此执行。",
    # 实验组：裸「问一次」——不含拆分知识、不提 sub_agent、不给对立面锚点
    "ask": ("先别执行。重看一遍你的拆分方案：这个拆法有没有问题？"
            "有没有更好的拆法？把你想清楚后的结论（改哪里、为什么这么改；"
            "或为什么不改）写进现状块，然后再报一次方案等我批复。"),
    # 实验组二次批复（ask 组重报方案后放行）
    "ask_approve": "批复：按你重报的方案执行。",
}


def _req(base, method, path, body=None, timeout=60):
    url = base.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


class Logger:
    def __init__(self, path):
        self.fp = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if self.fp:
            self.fp.write(line + "\n")
            self.fp.flush()


def get_card(base, wid, cid):
    st, res = _req(base, "GET", f"/api/works/{wid}/cards/{cid}")
    if st != 200 or not isinstance(res, dict):
        return {}, {}
    meta = res.get("meta") if isinstance(res.get("meta"), dict) else res
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    return meta, body


def list_cards(base, wid):
    """canvas → [{card_id,parent,title,...}]；取不到就暴力探测 card_001..card_060。"""
    st, res = _req(base, "GET", f"/api/works/{wid}/canvas")
    cards = []
    if st == 200 and isinstance(res, dict):
        raw = res.get("cards") or res.get("items") or []
        if isinstance(raw, dict):
            raw = list(raw.values())
        for c in raw:
            if isinstance(c, dict) and (c.get("card_id") or c.get("id")):
                cards.append(c)
    if cards:
        return cards
    out = []
    for i in range(1, 61):
        cid = f"card_{i:03d}"
        meta, _ = get_card(base, wid, cid)
        if meta:
            meta.setdefault("card_id", cid)
            out.append(meta)
    return out


# ── 任务书形态机械指标 ─────────────────────────────────────────────────────
RE_STEP = re.compile(r"(第[一二三四五六七八九\d]+步|步骤\s*\d|^\s*\d+[\.、\)]\s)", re.M)
RE_HOWTO = re.compile(
    r"(运行|执行以下|调用|复制|重命名|打开文件|编写一个|安装|cd\s|python\s|curl|"
    r"命令行|脚本如下|命令如下|先.{0,12}再|然后运行|依次)")
RE_MISSION = re.compile(
    r"(负责|全权|交付|验收判据|验收标准|建成|健康|就绪|兜底|所有权|"
    r"你接手|由你定|你自己判断|保障|守护)")
RE_ACCEPT = re.compile(r"(验收|判据|通过标准|可核|PASS|检查通过|成功标准)")


def score_book(text):
    text = text or ""
    steps = len(RE_STEP.findall(text))
    howto = len(RE_HOWTO.findall(text))
    mission = len(RE_MISSION.findall(text))
    accept = bool(RE_ACCEPT.search(text))
    if steps >= 2 or howto >= 3:
        cls = "subagent_like"      # 步骤书/操作单
    elif mission >= 1 and steps == 0 and howto <= 1:
        cls = "mission_like"       # 真实使命＋验收
    else:
        cls = "mixed"
    return {"chars": len(text), "step_markers": steps, "howto_hits": howto,
            "mission_hits": mission, "has_acceptance": accept, "cls": cls}


def read_create_card_calls(sandbox_root, wid, cid):
    """从根卡事件流取 create_card 工具调用的原参数（任务书第一现场）。"""
    fp = os.path.join(sandbox_root, "works", wid, "events", f"{cid}.jsonl")
    out = []
    if not os.path.exists(fp):
        return out
    with open(fp, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(e.get("type")) != "tool_call":
                continue
            d = e.get("data") or {}
            if str(d.get("tool")) not in ("create_card", "create_coder_card"):
                continue
            args = d.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            out.append({"tool": d.get("tool"), "args": args or {},
                        "ts": e.get("ts"), "seq": e.get("seq")})
    return out


def run_arm(base, arm, log, sandbox_root):
    rec = {"arm": arm, "steps": []}

    def step(name, **kw):
        rec["steps"].append({"step": name,
                             "at": datetime.now().isoformat(timespec="seconds"), **kw})
        log(f"[{arm}] {name}: {json.dumps(kw, ensure_ascii=False)[:240]}")

    # 1 建 work + 根卡开自动
    title = f"拆分实验-{arm}-{datetime.now().strftime('%m%d-%H%M%S')}"
    st, wk = _req(base, "POST", "/api/works", {"title": title})
    if st not in (200, 201) or not isinstance(wk, dict) or "work_id" not in wk:
        step("create_work", ok=False, status=st, resp=str(wk)[:200])
        rec["verdict"] = "aborted"
        return rec
    wid = wk["work_id"]
    root = wk.get("work_card_id") or "card_001"
    rec["work_id"], rec["root"] = wid, root
    step("create_work", ok=True, work_id=wid, root=root)

    st, pr = _req(base, "PATCH", f"/api/works/{wid}/cards/{root}",
                  {"updates": {"auto_mode": 1}, "history_note": "拆分实验：根卡开自动模式"})
    step("patch_root_auto", ok=st == 200)
    if st != 200:
        rec["verdict"] = "aborted"
        return rec

    # 2 发任务（首条消息＝goal，自动开跑）
    st, mr = _req(base, "POST", f"/api/works/{wid}/cards/{root}/messages",
                  {"content": TASK}, timeout=900)
    step("send_task", status=st, resp=str(mr)[:200])

    # 3 等「方案落现状块 + pause」（若它不守流程直接建卡，记 post_hoc 并照样注入）
    t0 = time.time()
    plan_pre, phase = "", "pre_split"
    while time.time() - t0 < PLAN_WAIT:
        time.sleep(POLL)
        meta, body = get_card(base, wid, root)
        gs = str(meta.get("goal_state") or "")
        status_txt = str(body.get("status_block") or "")
        kids = [c for c in list_cards(base, wid)
                if str(c.get("parent") or "") == root]
        log(f"[{arm}] 等方案…… goal_state={gs} 现状{len(status_txt)}字 子卡{len(kids)}")
        if kids:
            phase = "post_hoc"          # 没等批复就拆了：问一次变成事后追问
            break
        if gs == "paused" and status_txt.strip():
            plan_pre = status_txt
            break
    rec["phase"] = phase
    rec["plan_pre"] = plan_pre
    step("plan_captured", phase=phase, plan_chars=len(plan_pre))
    if not plan_pre and phase != "post_hoc":
        meta, body = get_card(base, wid, root)
        rec["plan_pre"] = str(body.get("status_block") or "")
        step("plan_timeout_fallback", chars=len(rec["plan_pre"]))

    # 4 注入（对照＝批复；实验＝裸问一次）
    st, rr = _req(base, "POST", f"/api/works/{wid}/cards/{root}/messages",
                  {"content": REPLIES[arm]}, timeout=900)
    step("inject_reply", arm=arm, status=st, resp=str(rr)[:200])

    # ask 组：等它重报方案（现状块变化）后再批复放行
    if arm == "ask":
        t1 = time.time()
        plan_post = ""
        while time.time() - t1 < PLAN_WAIT:
            time.sleep(POLL)
            meta, body = get_card(base, wid, root)
            gs = str(meta.get("goal_state") or "")
            status_txt = str(body.get("status_block") or "")
            kids = [c for c in list_cards(base, wid)
                    if str(c.get("parent") or "") == root]
            log(f"[ask] 等重报…… goal_state={gs} 现状{len(status_txt)}字 子卡{len(kids)}")
            if kids:
                break
            if status_txt.strip() and status_txt != plan_pre and gs == "paused":
                plan_post = status_txt
                break
        rec["plan_post"] = plan_post
        rec["plan_changed"] = bool(plan_post) and plan_post != plan_pre
        step("replan_captured", changed=rec["plan_changed"],
             post_chars=len(plan_post))
        st, ar = _req(base, "POST", f"/api/works/{wid}/cards/{root}/messages",
                      {"content": REPLIES["ask_approve"]}, timeout=900)
        step("ask_approve", status=st, resp=str(ar)[:200])

    # 5 等拆分落地（子卡出现且数量稳定）或根卡收口
    t2 = time.time()
    last_n, stable = -1, 0
    while time.time() - t2 < DONE_WAIT:
        time.sleep(POLL)
        meta, _ = get_card(base, wid, root)
        gs = str(meta.get("goal_state") or "")
        cards = list_cards(base, wid)
        kids = [c for c in cards if str(c.get("parent") or "") == root]
        n = len(kids)
        stable = stable + 1 if n == last_n and n > 0 else 0
        last_n = n
        log(f"[{arm}] 等拆分…… goal_state={gs} 子卡{n} 稳定{stable}/{STABLE_ROUNDS}")
        if gs in ("complete", "blocked"):
            step("root_finished", goal_state=gs)
            break
        if stable >= STABLE_ROUNDS:
            step("children_stable", n=n)
            break

    # 6 收证据：子卡任务书（卡面 + 事件流 create_card 原文双源）
    cards = list_cards(base, wid)
    kids = [c for c in cards if str(c.get("parent") or "") == root]
    books = []
    for c in kids:
        cid = str(c.get("card_id") or c.get("id"))
        meta, body = get_card(base, wid, cid)
        goal_txt = str(meta.get("goal") or c.get("goal") or "")
        books.append({
            "card_id": cid,
            "title": str(meta.get("title") or c.get("title") or ""),
            "card_type": str(meta.get("card_type") or c.get("card_type") or ""),
            "goal_state": str(meta.get("goal_state") or ""),
            "goal_text": goal_txt,
            "score": score_book(goal_txt),
        })
    calls = read_create_card_calls(sandbox_root, wid, root)
    for call in calls:
        a = call["args"]
        call["score"] = score_book(str(a.get("goal") or "") + "\n"
                                   + str(a.get("content") or ""))
    rec["children"] = books
    rec["create_card_calls"] = calls
    meta, body = get_card(base, wid, root)
    rec["root_final_status"] = str(body.get("status_block") or "")
    rec["root_goal_state"] = str(meta.get("goal_state") or "")
    step("evidence_collected", children=len(books), create_calls=len(calls))

    # 7 清场：本 work 所有卡 stop + auto_mode=0（防沙箱子卡空转烧额度）
    for c in cards:
        cid = str(c.get("card_id") or c.get("id"))
        _req(base, "POST", f"/api/works/{wid}/cards/{cid}/stop", {}, timeout=15)
        _req(base, "PATCH", f"/api/works/{wid}/cards/{cid}",
             {"updates": {"auto_mode": 0}, "history_note": "拆分实验收尾：停自动"}, timeout=15)
    step("cleanup_done", cards=len(cards))

    # 8 机械判定（只判形态差异，不下价值判断）
    def agg(items):
        if not items:
            return {}
        return {
            "n": len(items),
            "mission_like": sum(1 for b in items if b["score"]["cls"] == "mission_like"),
            "subagent_like": sum(1 for b in items if b["score"]["cls"] == "subagent_like"),
            "mixed": sum(1 for b in items if b["score"]["cls"] == "mixed"),
            "with_acceptance": sum(1 for b in items if b["score"]["has_acceptance"]),
            "avg_step_markers": round(sum(b["score"]["step_markers"] for b in items) / len(items), 2),
            "avg_howto_hits": round(sum(b["score"]["howto_hits"] for b in items) / len(items), 2),
        }
    rec["summary"] = agg(books)
    rec["verdict"] = "collected"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--arms", default="control,ask")
    args = ap.parse_args()
    log = Logger(args.log or None)

    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "arms": []}

    st, inst = _req(args.base, "GET", "/__instance__")
    ok_inst = (st == 200 and isinstance(inst, dict)
               and inst.get("instance") == "test-instance"
               and "5713" in str(inst.get("base_dir"))
               and bool(inst.get("sandbox_enabled")))
    ev["instance"] = inst
    if not ok_inst:
        log(f"实例身份不符，中止: {str(inst)[:300]}")
        ev["verdict"] = "aborted_bad_instance"
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        return 2
    sandbox_root = str(inst.get("sandbox_root") or "")
    log(f"实例核验通过 sandbox_root={sandbox_root}")

    for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
        if arm not in ("control", "ask"):
            log(f"未知臂 {arm}，跳过")
            continue
        log(f"=== 开始臂 {arm} ===")
        try:
            rec = run_arm(args.base, arm, log, sandbox_root)
        except Exception as exc:  # noqa: BLE001
            rec = {"arm": arm, "verdict": "error", "error": f"{type(exc).__name__}: {exc}"}
            log(f"[{arm}] 异常: {rec['error']}")
        ev["arms"].append(rec)
        # 每臂结束就落一次盘（防中途挂掉丢证据）
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        log(f"=== 臂 {arm} 结束 verdict={rec.get('verdict')} ===")

    # 组间对照汇总
    by = {a["arm"]: a for a in ev["arms"]}
    if "control" in by and "ask" in by:
        ev["compare"] = {"control": by["control"].get("summary"),
                         "ask": by["ask"].get("summary"),
                         "ask_plan_changed": by["ask"].get("plan_changed"),
                         "ask_phase": by["ask"].get("phase"),
                         "control_phase": by["control"].get("phase")}
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    ev["verdict"] = "done"
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    log("全部完成，证据落盘 " + args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
