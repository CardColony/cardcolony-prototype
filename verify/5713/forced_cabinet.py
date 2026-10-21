# -*- coding: utf-8 -*-
"""强制拆团队之后，它到底拆成什么样？（5713 真机）

**用户的要求**：强制它做一步拆分团队，**不规定拆法**，只是强制它拆。然后看它做得怎么样。

**这条验的不是闸，是模型。** 闸（card_023 的收口闸）只负责"不拆就收不了口"；
拆几个、切什么域、盯哪些卡、职责书写什么——全由它自己定，我不给任何提示。

**与前面两臂的关系**
`appointer_judgment.py` 的 A/B 臂已经证明：零干预它不拆，问一句它才拆。
本驱动器用**同一份埋点材料、同一句 goal（A 臂原句，零组阁提示）**，
唯一差别是**闸在场**——于是能回答一个更狠的问题：

    「如果绕不过去，它会被逼出什么样的团队？」

**判据分两栏，都锚结构事实**
A. 闸是否真的逼到了（过程事实）
   - 收口被拒了几次（从事件流里数 update_goal 的 validation 失败）
   - 最终有没有组出角色才收口成功
B. 它拆得怎么样（结构事实 + 少量文本判据）
   - 拆了几个角色、域是什么
   - **覆盖度**：每张工作卡是否至少被一个角色关注（有卡没人看 = 拆漏了）
   - **重叠**：两个角色的 watch 是否高度重合且域相近（拆不清）
   - **敷衍信号**（这三条不看措辞、只看结构）：
       · 域名为空/占位（misc、general、other、all、temp…）
       · 职责书过短（< 30 字，套话居多）
       · 角色的 speak_contract 两个触发条件都为空（说不清什么时候开口）
       · 角色建成后一轮都没跑（判词 0 且 pending 长期不为 0）

用法：
    python verify/5713/forced_cabinet.py --base http://127.0.0.1:5714 \
        --out verify/5713/evidence_forced_cabinet.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appointer_judgment as aj          # 复用同一份埋点材料，保证与 A/B 臂可比

POLL = 10
# 强制组阁的 goal 后缀（**只说"必须做这一步"，不说"怎么拆"**）。
# 这是"硬强制但不规定拆法"的最短实现：不需要改 src 就能测出
# 「绕不过去的时候，它会拆成什么样的团队」。
FORCE_SUFFIX = """

注意：收口之前你必须先组建职能团队——用 create_role 建角色。
至于设几个、各管什么、盯哪些卡、职责书怎么写，全部由你判断，我不给建议。
这一步不能跳过，也不能用别的方式替代。"""

# goal 级强制：基线 goal（A 臂原句，零组阁提示）＋ 强制后缀
GOAL_FORCED = aj.GOAL_A + FORCE_SUFFIX

PLACEHOLDER_KEYS = {"", "misc", "general", "other", "others", "all", "temp",
                    "tmp", "default", "observer", "watch", "综合", "其他", "通用"}


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


def _log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5714")
    ap.add_argument("--out", default="verify/5713/evidence_forced_cabinet.json")
    ap.add_argument("--pin-model", default="ds-v41-flash-aliyun")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--force-mode", default="goal", choices=["goal", "gate"],
                    help="goal=在 goal 里硬性要求组阁（不改代码，立刻可跑）；"
                         "gate=依赖收口闸（需 card_023 落地并重启实例）")
    args = ap.parse_args()

    t0 = time.time()
    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "phases": [], "verdict": "unknown",
          "design": "同一份埋点材料＋A 臂原句（零组阁提示）＋**收口闸在场**；不给任何拆法提示",
          "compare_to": "evidence_appointer_A.json（同材料同 goal，但无闸：create_role 零出现）"}

    def save():
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)

    def phase(name, **kw):
        rec = {"phase": name, "at": datetime.now().isoformat(timespec="seconds"), **kw}
        ev["phases"].append(rec)
        _log(f"{name}: {json.dumps(kw, ensure_ascii=False)[:380]}")
        save()
        return rec

    st, inst = _req(args.base, "GET", "/__instance__")
    ev["instance"] = inst
    ok = (st == 200 and isinstance(inst, dict)
          and "5713" in str(inst.get("base_dir"))
          and bool(inst.get("sandbox_enabled")))
    phase("0_instance_check", ok=ok, base_dir=str((inst or {}).get("base_dir")),
          sandbox_root=str((inst or {}).get("sandbox_root")))
    if not ok:
        ev["verdict"] = "ABORT_NOT_5713"
        save()
        return 2
    sb = str(inst.get("sandbox_root") or "")

    # ── 1 项目 + 钉模型 ──
    st, wk = _req(args.base, "POST", "/api/works",
                  {"title": f"强制拆团队-{datetime.now().strftime('%m%d-%H%M%S')}"})
    wid = wk.get("work_id") if isinstance(wk, dict) else None
    root = (wk.get("work_card_id") or "card_001") if isinstance(wk, dict) else None
    if not wid:
        phase("1_create_work", ok=False, resp=str(wk)[:200])
        save()
        return 3
    phase("1_create_work", ok=True, work_id=wid, root=root)
    ev["work_id"] = wid

    if args.pin_model:
        p = os.path.join(sb, "works", wid, "model_override.json")
        try:
            data = {}
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    data = json.load(f) or {}
            data.setdefault("work", {})["default_model"] = args.pin_model
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            phase("1b_pin_model", ok=True, model=args.pin_model)
        except Exception as exc:  # noqa: BLE001
            phase("1b_pin_model", ok=False, error=str(exc)[:200])

    # ── 2 三张工作卡（与 A/B 臂逐字同款埋点材料）──
    cards = {}
    for key, title, status in (("W1", aj.W1_TITLE, aj.W1_STATUS),
                               ("W2", aj.W2_TITLE, aj.W2_STATUS),
                               ("W3", aj.W3_TITLE, aj.W3_STATUS)):
        st, c = _req(args.base, "POST", f"/api/works/{wid}/cards",
                     {"title": title, "source": "用户直接创建",
                      "card_type": "sub_project", "parent": root, "goal": ""})
        cid = ((c.get("meta") or c).get("card_id") if isinstance(c, dict) else None)
        cards[key] = cid
        if cid:
            _req(args.base, "PATCH", f"/api/works/{wid}/cards/{cid}",
                 {"updates": {"status_block": status}, "history_note": "预置现状"})
        phase(f"2_card_{key}", ok=bool(cid), card_id=cid, title=title)
    ev["cards"] = cards

    # ── 3 根卡自动模式 ＋ goal ──
    # force-mode=goal：基线句 ＋ 强制后缀（只说必须组，不说怎么拆）
    # force-mode=gate：基线句（靠闸逼）
    goal = GOAL_FORCED if args.force_mode == "goal" else aj.GOAL_A
    ev["force_mode"] = args.force_mode
    _req(args.base, "PATCH", f"/api/works/{wid}/cards/{root}",
         {"updates": {"auto_mode": 1}, "history_note": "开自动模式"})
    st, gr = _req(args.base, "POST", f"/api/works/{wid}/cards/{root}/goal", {"input": goal})
    ok_goal = bool(isinstance(gr, dict) and gr.get("ok"))
    phase("3_set_goal", ok=ok_goal, goal=goal)
    if not ok_goal:
        ev["verdict"] = "ABORT_GOAL_NOT_SET"
        save()
        return 4
    ev["goal"] = goal

    # ── 4 等它自己跑（闸会逼它；我不提示）──
    state = ""
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(POLL)
        st, m = _req(args.base, "GET", f"/api/works/{wid}/cards/{root}")
        mm = (m.get("meta") if isinstance(m, dict) else {}) or {}
        state = str(mm.get("goal_state") or "")
        st2, rl = _req(args.base, "GET", f"/api/works/{wid}/roles")
        nrole = len((rl or {}).get("roles") or []) if isinstance(rl, dict) else -1
        _log(f"等根卡…… goal_state={state} 角色数={nrole}")
        if state in ("complete", "blocked", "paused"):
            break
    phase("4_root_finished", goal_state=state)

    # 取证前 settle（本项目第 4 次踩到的坑：日志与判词都比"目标态变了"晚落盘）
    time.sleep(10)

    # ── 5 收证据 ──
    roles = _roles(args.base, wid)
    st, rc = _req(args.base, "GET", f"/api/works/{wid}/cards/{root}")
    rb = (rc.get("body") if isinstance(rc, dict) else {}) or {}
    gate = _gate_stats(sb, wid, root)
    ev["roles"] = roles
    ev["root_status"] = rb.get("status_block") or ""
    ev["root_rulings"] = rb.get("requirements") or rb.get("rulings") or ""
    ev["gate"] = gate
    ev["models"] = aj._models_used(sb, wid, t0, aj.FINGERPRINTS)
    phase("5_collected", roles=len(roles), gate=gate)

    vd = _judge(ev)
    ev["verdict_detail"] = vd
    ev["verdict"] = "COLLECTED"        # 本实验记录事实，不判"过/不过"
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    ev["elapsed_seconds"] = round(time.time() - t0, 1)
    save()
    print(f"\n判定：{ev['verdict']}（记录事实）", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{args.out}", flush=True)
    return 0


def _roles(base, wid):
    st, rl = _req(base, "GET", f"/api/works/{wid}/roles")
    out = []
    for r in ((rl or {}).get("roles") or []):
        rid = str(r.get("role_id") or "")
        st2, s = _req(base, "GET", f"/api/works/{wid}/roles/{rid}")
        s = s if isinstance(s, dict) else {}
        jur = s.get("jurisdiction") or {}
        out.append({
            "role_id": rid,
            "title": s.get("title"),
            "exclusive_key": str(jur.get("exclusive_key") or ""),
            "domain": str(jur.get("domain") or ""),
            "statement": str(jur.get("statement") or ""),
            "watch": [w.get("card_id") for w in ((s.get("watch") or {}).get("cards") or [])],
            "signals": (s.get("watch") or {}).get("signals") or [],
            "speak_contract": s.get("speak_contract") or {},
            "escalation": s.get("escalation") or {},
            "charter": (s.get("charter") or {}).get("text") or "",
            "charter_version": (s.get("charter") or {}).get("version"),
            "duty": (s.get("duty") or {}).get("state"),
            "verdicts": len(s.get("verdicts") or []),
            "alarms": len(s.get("alarms") or []),
        })
    return out


def _gate_stats(sandbox_root, wid, root):
    """闸的过程事实：收口被拒了几次、错误串是什么（从事件流取，不看模型自述）。"""
    p = os.path.join(sandbox_root, "works", wid, "events", f"{root}.jsonl")
    rejects = []
    completes = 0
    if not os.path.exists(p):
        return {"rejected": 0, "errors": [], "update_goal_calls": 0}
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = str(e.get("type"))
            d = e.get("data") or {}
            if t == "tool_call" and str(d.get("name") or "") == "update_goal":
                completes += 1
            if t == "tool_result":
                blob = json.dumps(d, ensure_ascii=False)
                if '"ok": false' in blob or '"ok":false' in blob:
                    if "收口" in blob or "validation" in blob:
                        # 只留"收口被拒"这一类（别的 validation 不算闸）
                        txt = str(d.get("error") or d.get("message") or blob)[:400]
                        if "收口" in txt or "角色" in txt:
                            rejects.append(txt)
    return {"rejected": len(rejects), "errors": rejects[:5],
            "update_goal_calls": completes}


def _judge(ev):
    roles = ev.get("roles") or []
    cards = ev.get("cards") or {}
    work_card_ids = [c for c in cards.values() if c]
    watched_all: set[str] = set()
    for r in roles:
        watched_all.update(r.get("watch") or [])

    per_card = {cid: [r["role_id"] for r in roles if cid in (r.get("watch") or [])]
                for cid in work_card_ids}
    uncovered = [cid for cid, rs in per_card.items() if not rs]

    # 域的质量
    keys = [r.get("exclusive_key") for r in roles]
    key_placeholder = [r["role_id"] for r in roles
                       if str(r.get("exclusive_key") or "").strip().lower()
                       in PLACEHOLDER_KEYS]
    dup_keys = sorted({k for k in keys if k and keys.count(k) > 1})

    # 敷衍信号：职责书过短 / 说话条件空 / 建了没跑
    thin_charter = [r["role_id"] for r in roles if len((r.get("charter") or "").strip()) < 30]
    no_trigger = []
    for r in roles:
        sc = r.get("speak_contract") or {}
        if not (sc.get("advice_when") or sc.get("alarm_when")):
            no_trigger.append(r["role_id"])
    never_ran = [r["role_id"] for r in roles
                 if int(r.get("verdicts") or 0) == 0 and r.get("duty") == "active"]

    # 重叠：两个角色 watch 完全相同的卡集合
    overlap = []
    for i in range(len(roles)):
        for j in range(i + 1, len(roles)):
            a, b = set(roles[i].get("watch") or []), set(roles[j].get("watch") or [])
            if a and a == b:
                overlap.append([roles[i]["role_id"], roles[j]["role_id"]])

    return {
        # A. 闸是否真逼到了
        "gate_rejected_times": (ev.get("gate") or {}).get("rejected"),
        "gate_error_samples": (ev.get("gate") or {}).get("errors"),
        "gate_reject_errors": (ev.get("gate") or {}).get("errors"),
        "update_goal_calls": (ev.get("gate") or {}).get("update_goal_calls"),
        "final_goal_state": (ev.get("phases") or [{}])[-2].get("goal_state")
                            if len(ev.get("phases") or []) > 1 else "",
        # B. 它拆得怎么样
        "role_count": len(roles),
        "roles": [{"id": r["role_id"], "title": r["title"],
                   "key": r["exclusive_key"], "watch": r["watch"],
                   "charter_len": len((r.get("charter") or "").strip()),
                   "verdicts": r["verdicts"]} for r in roles],
        "exclusive_keys": keys,
        "keys_unique": not dup_keys,
        "duplicate_keys": dup_keys,
        # 覆盖度（拆漏没有）
        "work_card_ids": work_card_ids,
        "cards_watched_by": per_card,
        "uncovered_cards": uncovered,
        "covers_all_work_cards": not uncovered,
        # 敷衍/拆不清
        "placeholder_keys": key_placeholder,
        "thin_charter": thin_charter,
        "no_speak_trigger": no_trigger,
        "never_ran": never_ran,
        "identical_watch_pairs": overlap,
        "models": ev.get("models"),
        "observations": {
            "root_rulings_len": len(str(ev.get("root_rulings") or "")),
            "root_status_len": len(str(ev.get("root_status") or "")),
        },
    }


if __name__ == "__main__":
    sys.exit(main())