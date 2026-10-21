# -*- coding: utf-8 -*-
"""「问一次」拆分实验驱动器 v2（5713 沙箱 · 真机实测）。

与 v1 的差别：
  1. 支持 `--model`：建 work 后给根卡写**卡级模型覆盖**（default_model），
     规避 depth_0 默认 kimi-k3 的配额墙；两臂用同一模型 ⇒ 组间可比。
  2. 错误早退：注入请求返回 5xx / 事件流出现 error 帧 ⇒ 该臂立刻收场记
     verdict=model_error，不再空转 45 分钟（v1 踩到：kimi-k3 配额用尽后干等）。
  3. 三臂：control / ask（拆前问一次）/ ask_post（拆完后再问一次，看自我修正）。
  4. 现状块时间线：每轮记（时间, 字数, goal_state, 子卡数），证据可回放。

安全：只打 5713 隔离实例（__instance__ 三验）；证据每臂增量落盘；
收尾 stop 全部实验卡 + auto_mode=0，防沙箱子卡空转烧额度。

用法：
    python ask_once_experiment_v2.py --base http://127.0.0.1:5713 \
        --model ds-flash-official --arms control,ask \
        --out evidence_v2.json --log run_v2.log
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

POLL = 15
PLAN_WAIT = 25 * 60
DONE_WAIT = 35 * 60
POST_ASK_WAIT = 15 * 60
STABLE_ROUNDS = 4

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

# 干预文本（逐字固定，两臂共享其余一切）
REPLY_APPROVE = "批复：方案收到，按此执行。"
REPLY_ASK = ("先别执行。重看一遍你的拆分方案：这个拆法有没有问题？有没有更好的拆法？"
             "把你想清楚后的结论（改哪里、为什么这么改；或为什么不改）写进现状块，"
             "然后再报一次方案等我批复。")
REPLY_ASK_APPROVE = "批复：按你重报的方案执行。"
REPLY_ASK_POST = ("现在停一下，看你已经派出去的子卡：这个拆法有没有问题？"
                  "有没有更好的拆法？如果该改，就动手改（改任务书、合并、删掉重派都可以），"
                  "并把改了什么、为什么改写进现状块。")

# ── gate 臂：建卡前的「机械预审闸门」────────────────────────────────────────
# 任务在公共 TASK 之上加一条**流程**要求（不是拆分知识）：建卡前把每张子卡任务书
# 全文贴进现状块，等逐张批复；驱动器按机械判据检查实现泄漏，不合格打回重写。
TASK_GATE = TASK + """

补充流程要求（机械，必须照做）：
5. 方案里每张子卡要贴出**任务书全文草案**（标题＋使命＋范围＋验收判据，一字不改地贴），
   供逐张预审；预审通过前不许建卡。"""

REPLY_GATE_REJECT = (
    "预审不通过：你贴出的子卡任务书里含**实现细节**（用什么命令/参数、怎么调用工具、"
    "按什么顺序操作）——这些是你自己的知识，不是子卡的使命。\n"
    "重写你的任务书草案，只保留三样东西：①这张子卡为什么存在、到底负责哪一块（使命）；"
    "②它的交付物是什么；③验收判据（可以机械核对的）。\n"
    "做法、命令、参数、日志字段名、工具调用序列一律删掉。改完把新草案贴进现状块再报。")

# 实现泄漏判据（与 score_books.py 的 ops_leak 同源）
RE_OPS = re.compile(
    r"(sqlite3|PRAGMA|mode=ro|file:.*\?mode|json\.load|json\.loads|print\(|os\.walk|"
    r"curl|for /d|dir /b|\bpython\b|\bbash\b|命令行|命令如下|脚本如下|逐行解析|"
    r"checkpoint|VACUUM|write_file|update_status|read_card|get_goal)", re.I)


def ops_leak(text):
    return len(RE_OPS.findall(text or ""))


# ── HTTP ──────────────────────────────────────────────────────────────────
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


def events_of(sandbox_root, wid, cid):
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
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def has_model_error(evs, since_ts):
    """事件流里 since_ts 之后是否有 error 帧（配额/调用失败）。"""
    for e in evs:
        if str(e.get("type")) != "error":
            continue
        if str(e.get("ts") or "") >= since_ts:
            return str((e.get("data") or {}).get("error") or "")[:300]
    return ""


def read_create_card_calls(sandbox_root, wid, cid):
    """根卡事件流里 create_card / create_coder_card 的工具调用原参数。"""
    out = []
    for e in events_of(sandbox_root, wid, cid):
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


# ── 形态机械指标 ───────────────────────────────────────────────────────────
RE_STEP = re.compile(r"(第[一二三四五六七八九\d]+步|步骤\s*\d|^\s*\d+[\.、\)]\s)", re.M)
RE_HOWTO = re.compile(
    r"(运行以下|执行以下|调用|复制|重命名|打开文件|编写一个|安装|cd\s|python\s|curl|"
    r"命令行|脚本如下|命令如下|先.{0,12}再|然后运行|依次|逐行|用\s*sqlite3|PRAGMA|"
    r"file:.*mode=ro|统计行数|解析失败行号)")
RE_MISSION = re.compile(
    r"(负责|全权|交付|验收判据|验收标准|建成|健康|就绪|兜底|所有权|"
    r"你接手|由你定|你自己判断|保障|守护)")
RE_ACCEPT = re.compile(r"(验收|判据|通过标准|可核|PASS|检查通过|成功标准|核查表)")


def score_book(text):
    text = text or ""
    steps = len(RE_STEP.findall(text))
    howto = len(RE_HOWTO.findall(text))
    mission = len(RE_MISSION.findall(text))
    accept = bool(RE_ACCEPT.search(text))
    if steps >= 2 or howto >= 3:
        cls = "subagent_like"
    elif mission >= 1 and steps == 0 and howto <= 1:
        cls = "mission_like"
    else:
        cls = "mixed"
    return {"chars": len(text), "step_markers": steps, "howto_hits": howto,
            "mission_hits": mission, "has_acceptance": accept, "cls": cls}


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


def _post_message(base, wid, root, content):
    return _req(base, "POST", f"/api/works/{wid}/cards/{root}/messages",
                {"content": content}, timeout=1200)


def run_arm(base, arm, log, sandbox_root, model):
    rec = {"arm": arm, "model": model, "timeline": [], "steps": []}

    def step(name, **kw):
        rec["steps"].append({"step": name,
                             "at": datetime.now().isoformat(timespec="seconds"), **kw})
        log(f"[{arm}] {name}: {json.dumps(kw, ensure_ascii=False)[:240]}")

    # 1 建 work + 根卡开自动 + 卡级模型覆盖
    title = f"拆分实验v2-{arm}-{datetime.now().strftime('%m%d-%H%M%S')}"
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

    if model:
        st, mo = _req(base, "PUT", f"/api/settings/model-overrides/{wid}/card/{root}",
                      {"default_model": model})
        step("set_card_model", ok=st == 200, status=st, resp=str(mo)[:160], model=model)
        if st != 200:
            rec["verdict"] = "aborted"
            return rec

    # 2 发任务（首条消息＝goal）
    st, mr = _post_message(base, wid, root, TASK_GATE if arm == "gate" else TASK)
    step("send_task", status=st, resp=str(mr)[:160])
    if st >= 500:
        rec["verdict"] = "model_error"
        rec["error"] = str(mr)[:300]
        return rec

    # 3 等「方案落现状块 + pause」或「它直接建卡」（post_hoc）
    t0 = time.time()
    plan_pre, phase = "", "pre_split"
    while time.time() - t0 < PLAN_WAIT:
        time.sleep(POLL)
        meta, body = get_card(base, wid, root)
        gs = str(meta.get("goal_state") or "")
        status_txt = str(body.get("status_block") or "")
        cards = list_cards(base, wid)
        kids = [c for c in cards if str(c.get("parent") or "") == root]
        rec["timeline"].append({"t": datetime.now().isoformat(timespec="seconds"),
                                "stage": "wait_plan", "goal_state": gs,
                                "status_chars": len(status_txt), "children": len(kids)})
        log(f"[{arm}] 等方案…… goal_state={gs} 现状{len(status_txt)}字 子卡{len(kids)}")
        if kids:
            phase = "post_hoc"
            break
        if gs == "paused" and status_txt.strip():
            plan_pre = status_txt
            break
    rec["phase"] = phase
    rec["plan_pre"] = plan_pre
    step("plan_captured", phase=phase, plan_chars=len(plan_pre))
    if not plan_pre and phase != "post_hoc":
        _, body = get_card(base, wid, root)
        rec["plan_pre"] = str(body.get("status_block") or "")
        step("plan_timeout_fallback", chars=len(rec["plan_pre"]))

    # 4 干预
    # 4.5 gate 臂：建卡前的机械预审（判据＝实现泄漏），不合格打回重写
    if arm == "gate":
        cur = plan_pre
        for attempt in range(3):
            leak = ops_leak(cur)
            step("gate_audit", attempt=attempt, ops_leak=leak, chars=len(cur))
            if leak < 3 or phase == "post_hoc":
                break
            mark0 = datetime.now().isoformat(timespec="seconds")
            st, gr = _post_message(base, wid, root, REPLY_GATE_REJECT)
            step("gate_reject", attempt=attempt, status=st, resp=str(gr)[:160])
            err0 = has_model_error(events_of(sandbox_root, wid, root), mark0)
            if st >= 500 or err0:
                rec["verdict"] = "model_error"
                rec["error"] = err0 or str(gr)[:300]
                step("model_error_detected", error=rec["error"][:200])
                _cleanup(base, wid, log, rec, step)
                return rec
            t = time.time()
            newplan = ""
            while time.time() - t < PLAN_WAIT:
                time.sleep(POLL)
                meta, body = get_card(base, wid, root)
                gs = str(meta.get("goal_state") or "")
                txt = str(body.get("status_block") or "")
                kids = [c for c in list_cards(base, wid)
                        if str(c.get("parent") or "") == root]
                rec["timeline"].append({"t": datetime.now().isoformat(timespec="seconds"),
                                        "stage": "wait_gate_rewrite", "goal_state": gs,
                                        "status_chars": len(txt), "children": len(kids)})
                log(f"[gate] 等重写…… goal_state={gs} 现状{len(txt)}字 子卡{len(kids)}")
                if kids:
                    phase = "post_hoc"
                    rec["phase"] = phase
                    break
                if txt and txt != cur and gs == "paused":
                    newplan = txt
                    break
            rec.setdefault("gate_rounds", []).append({
                "attempt": attempt, "leak_before": leak, "chars_before": len(cur),
                "leak_after": ops_leak(newplan), "chars_after": len(newplan)})
            step("gate_round", attempt=attempt, leak_before=leak,
                 leak_after=ops_leak(newplan), chars_after=len(newplan))
            if not newplan:
                break
            cur = newplan
        rec["plan_gate_final"] = cur
        rec["gate_leak_before"] = ops_leak(plan_pre)
        rec["gate_leak_after"] = ops_leak(cur)
        plan_pre = cur

    mark = datetime.now().isoformat(timespec="seconds")
    if arm == "control":
        st, rr = _post_message(base, wid, root, REPLY_APPROVE)
        step("inject_approve", status=st, resp=str(rr)[:160])
    elif arm == "ask":
        st, rr = _post_message(base, wid, root, REPLY_ASK)
        step("inject_ask", status=st, resp=str(rr)[:160])
    else:  # ask_post / gate：先批复（与 control 同）
        st, rr = _post_message(base, wid, root, REPLY_APPROVE)
        step("inject_approve", status=st, resp=str(rr)[:160])

    err = has_model_error(events_of(sandbox_root, wid, root), mark)
    if st >= 500 or err:
        rec["verdict"] = "model_error"
        rec["error"] = err or str(rr)[:300]
        step("model_error_detected", error=rec["error"][:200])
        _cleanup(base, wid, log, rec, step)
        return rec

    # 5 ask 臂：等它重报方案（现状块变化）后再批复放行
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
            rec["timeline"].append({"t": datetime.now().isoformat(timespec="seconds"),
                                    "stage": "wait_replan", "goal_state": gs,
                                    "status_chars": len(status_txt), "children": len(kids)})
            log(f"[ask] 等重报…… goal_state={gs} 现状{len(status_txt)}字 子卡{len(kids)}")
            if kids:
                phase = "post_hoc"
                rec["phase"] = phase
                break
            if status_txt.strip() and status_txt != plan_pre and gs == "paused":
                plan_post = status_txt
                break
        rec["plan_post"] = plan_post
        rec["plan_changed"] = bool(plan_post) and plan_post != plan_pre
        step("replan_captured", changed=rec["plan_changed"], post_chars=len(plan_post))
        mark2 = datetime.now().isoformat(timespec="seconds")
        st, ar = _post_message(base, wid, root, REPLY_ASK_APPROVE)
        step("ask_approve", status=st, resp=str(ar)[:160])
        err = has_model_error(events_of(sandbox_root, wid, root), mark2)
        if st >= 500 or err:
            rec["verdict"] = "model_error"
            rec["error"] = err or str(ar)[:300]
            step("model_error_detected", error=rec["error"][:200])
            _cleanup(base, wid, log, rec, step)
            return rec

    # 6 等拆分落地（子卡数稳定）或根卡收口
    t2 = time.time()
    last_n, stable = -1, 0
    while time.time() - t2 < DONE_WAIT:
        time.sleep(POLL)
        meta, _ = get_card(base, wid, root)
        gs = str(meta.get("goal_state") or "")
        kids = [c for c in list_cards(base, wid)
                if str(c.get("parent") or "") == root]
        n = len(kids)
        stable = stable + 1 if n == last_n and n > 0 else 0
        last_n = n
        rec["timeline"].append({"t": datetime.now().isoformat(timespec="seconds"),
                                "stage": "wait_split", "goal_state": gs,
                                "children": n})
        log(f"[{arm}] 等拆分…… goal_state={gs} 子卡{n} 稳定{stable}/{STABLE_ROUNDS}")
        if gs in ("complete", "blocked"):
            step("root_finished", goal_state=gs)
            break
        if stable >= STABLE_ROUNDS:
            step("children_stable", n=n)
            break

    # 7 ask_post 臂：拆完再问一次，看它是否自我修正
    if arm == "ask_post":
        n_before = len([c for c in list_cards(base, wid)
                        if str(c.get("parent") or "") == root])
        rec["children_before_ask"] = n_before
        mark3 = datetime.now().isoformat(timespec="seconds")
        st, pr2 = _post_message(base, wid, root, REPLY_ASK_POST)
        step("inject_ask_post", status=st, resp=str(pr2)[:160], children_before=n_before)
        t3 = time.time()
        while time.time() - t3 < POST_ASK_WAIT:
            time.sleep(POLL)
            meta, body = get_card(base, wid, root)
            gs = str(meta.get("goal_state") or "")
            kids = [c for c in list_cards(base, wid)
                    if str(c.get("parent") or "") == root]
            rec["timeline"].append({"t": datetime.now().isoformat(timespec="seconds"),
                                    "stage": "wait_post_ask", "goal_state": gs,
                                    "children": len(kids),
                                    "status_chars": len(str(body.get("status_block") or ""))})
            log(f"[ask_post] 等自我修正…… goal_state={gs} 子卡{len(kids)}")
            if gs in ("complete", "blocked"):
                break
            if len(kids) != n_before:
                # 子卡数变了＝真的动了拆分，再等一轮看稳定
                time.sleep(POLL * 2)
                break

    # 8 收证据
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
    rec["summary"] = agg(books)
    step("evidence_collected", children=len(books), create_calls=len(calls),
         summary=rec["summary"])

    _cleanup(base, wid, log, rec, step)
    rec["verdict"] = "collected"
    return rec


def _cleanup(base, wid, log, rec, step):
    cards = list_cards(base, wid)
    for c in cards:
        cid = str(c.get("card_id") or c.get("id"))
        _req(base, "POST", f"/api/works/{wid}/cards/{cid}/stop", {}, timeout=20)
        _req(base, "PATCH", f"/api/works/{wid}/cards/{cid}",
             {"updates": {"auto_mode": 0}, "history_note": "拆分实验收尾：停自动"}, timeout=20)
    step("cleanup_done", cards=len(cards))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--arms", default="control,ask")
    ap.add_argument("--model", default="", help="根卡卡级模型覆盖（两臂必须同值）")
    args = ap.parse_args()
    log = Logger(args.log or None)

    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "model": args.model, "arms": []}

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
    log(f"实例核验通过 sandbox_root={sandbox_root} model={args.model or '(tier 默认)'}")

    for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
        if arm not in ("control", "ask", "ask_post", "gate"):
            log(f"未知臂 {arm}，跳过")
            continue
        log(f"=== 开始臂 {arm} ===")
        try:
            rec = run_arm(args.base, arm, log, sandbox_root, args.model)
        except Exception as exc:  # noqa: BLE001
            rec = {"arm": arm, "verdict": "error", "error": f"{type(exc).__name__}: {exc}"}
            log(f"[{arm}] 异常: {rec['error']}")
        ev["arms"].append(rec)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        log(f"=== 臂 {arm} 结束 verdict={rec.get('verdict')} ===")

    by = {a["arm"]: a for a in ev["arms"]}
    cmp_ = {}
    for a, b in (("control", "ask"), ("ask", "ask_post"), ("control", "ask_post"), ("ask", "gate"), ("control", "gate")):
        if a in by and b in by:
            cmp_[f"{a}_vs_{b}"] = {
                "summary_a": by[a].get("summary"), "summary_b": by[b].get("summary"),
                "phase_a": by[a].get("phase"), "phase_b": by[b].get("phase"),
                "plan_changed_b": by[b].get("plan_changed"),
            }
    ev["compare"] = cmp_
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    ev["verdict"] = "done"
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    log("全部完成，证据落盘 " + args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())