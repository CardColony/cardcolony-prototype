# -*- coding: utf-8 -*-
"""按人类团队的职能建团队（5713 真机）· 强制拆，但不规定拆法。

**为什么重做这个实验（与 forced_cabinet.py 的差别）**
上一轮（`evidence_forced_cabinet.json`，work_005）强制之下它拆出 3 个域：
`interface_contract` / `delivery_evidence` / `cost_envelope`——**全是"观察员"**，
站在旁边挑毛病，没有一个真正推动工作。
根因在材料：我给的是一堆**待审的成品卡**（现状块写满了问题），
面对"已经做完的东西"，能长出来的职能天然只有"检查"。

本轮材料换成一个**要真交付的项目**：需求→实现→测试→上线四个阶段，
埋**依赖倒置**（口径没定稿，引擎已实现）、**预算/进度冲突**（钱快花完，后两阶段没开始）、
**上线风险**（无回滚、无监控，却承诺 99.9%）。
面对"待推进的工作"，人类团队会怎么分工——这才是要看的。

**强制语只说"按人类团队的职能方式建团队"，不说设几个、不说切什么域、不说谁管什么。**

**判据（机械为主，两类区分清楚）**
1. 结构类（硬判据，零解释空间）
   - `role_count` / `exclusive_keys` 唯一 / 每张工作卡是否有人关注（无空域）
   - **每个角色是否真的下过指令**（`issue_directive` 计数）——这是"推动工作"与"只是旁观"的
     机械分水岭：`raise_alarm` 只是"我看到问题了"，`issue_directive` 才是"你去把它做掉，我验收"
2. 分类类（**启发式，只作观察，不作硬判据**）
   - 按职责书/域名的关键词粗分「监督型」与「执行型」——我会在报告里标明这是启发式，
     因为它靠词，不靠结构；最终判断由人看原文。
3. 可落地性（这条最有产品价值）
   - 角色工具面只有 read / message / directive / verdict / alarm，**没有 write_file / bash**。
     所以"你自己去写代码"这种职能在此机制下是**空壳**。
   - 机械检查：职责书里是否出现"亲自实现/编写/部署/修改代码"这类要求动手的表述，
     且该角色并未把它转成"给工作卡下指令"。出现即记 `unactionable_charter`。

用法：
    python verify/5713/human_team_design.py --base http://127.0.0.1:5714 \
        --out verify/5713/evidence_human_team_design.json
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

POLL = 10

# ── 材料：一个要真交付的项目（不是三张待审的成品卡）──────────────────────
# 每张卡是一个阶段的工作，现状写"卡在哪"，而不是"有什么毛病"。
# 埋点：
#   D1 依赖倒置：W1 口径未裁决，W2 已按某一版实现 60%
#   D2 预算/进度冲突：40 万预算已花 32 万，W3/W4 还没开始
#   D3 上线风险：无回滚、无监控，却对业务方承诺 99.9% SLA
#   D4 阻塞循环：W2 说"等测试环境"，W3 说"等引擎稳定"
PROJECT_GOAL = (
    "把对账系统 V1 做出来并上线：30 天内交付，预算 40 万，对业务方承诺 99.9% 可用性。"
)

STAGES = [
    ("W1", "需求与口径定义", """## 使命
定稿对账口径：账务范围、差异判定阈值、跨系统时间基准。

## 现状
1) 三个业务线（零售/批发/电商）各自给出过一版口径，**互不一致**；其中时间基准一项，
   零售要求 UTC 带 Z，电商要求本地 naive——同一笔交易的时间会对不上 8 小时。
2) 引擎组催得急，先按零售那版实现了（见 W2 当前进度），**口径尚未经任何人裁决**。
3) 差异判定阈值：财务说 0.01 元以内算平，风控说要按比例算，未定。
4) 卡在哪：需要一个能拍板的人来定这三处冲突，定完才能冻结。"""),
    ("W2", "核心对账引擎实现", """## 使命
实现对账引擎：拉取两侧流水、按口径逐笔比对、输出差异清单。

## 现状
1) 已实现约 60%：拉取与比对主干跑通，**差异清单输出未做**。
2) 按零售那版口径实现的（UTC 带 Z、秒级）。若口径最终不是这一版，这部分要返工。
3) 重跑与幂等未做：重复跑同一批次会产生重复差异记录。
4) 卡在哪：单元测试一个没写；等测试环境就绪才能联调（见 W3）。"""),
    ("W3", "联调与测试", """## 使命
把引擎与上下游系统联调通，跑出上线前的验收用例。

## 现状
1) 测试环境尚未就绪：依赖的安全域审批走了两周，**卡在外部审批**。
2) 无自动化用例。计划手工跑 3 个业务线各 20 笔真实流水样例。
3) 卡在哪：等引擎稳定（W2 说差异清单还没做），引擎说等测试环境——**两边在互相等**。"""),
    ("W4", "上线与运维切换", """## 使命
把系统切上线，保证切换期账务不丢不重，出问题能退回。

## 现状
1) **无回滚方案**：老系统要停机切换，切一半失败怎么办没写。
2) **无监控告警**：上线后怎么知道它在正常跑，没有设计。
3) 对业务方承诺了 99.9% 可用性（年停机 ≤ 8.76 小时），但当前架构下**这个数字没有依据**。
4) 预算：项目共 40 万，已花 32 万（主要是 W2 的两轮返工）；W3 与 W4 尚未开始。"""),
]

# 强制语：只说"按人类团队的方式建团队"，不给任何拆法
FORCE_SUFFIX = """

开工之前，先按**人类团队里职能分工的方式**把这支团队建起来——用 create_role 建角色，
把各个职能设计清楚。至于设几个职能、每个职能管什么、盯哪些卡、职责书怎么写，
全部由你判断，我不给建议。这一步不能跳过。"""

GOAL_FORCED = PROJECT_GOAL + FORCE_SUFFIX

# 启发式词表（**只用于观察分类，不作硬判据**）
WATCH_WORDS = ("观察", "监督", "审计", "审核", "检查", "评审", "把关", "守门",
               "一致性", "红线", "证据", "复核", "盯")
EXEC_WORDS = ("需求", "设计", "实现", "开发", "编码", "测试", "联调", "上线",
              "部署", "运维", "交付", "发布", "建设", "推进", "项目管理", "进度")
# "要亲自动手"的表述——此机制下角色无写文件/执行能力，出现即可能空壳
HANDS_ON_WORDS = ("亲自实现", "自己编写", "自己写代码", "你负责实现", "直接修改代码",
                  "由你部署", "你来部署", "亲自编码", "直接写")


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
    ap.add_argument("--out", default="verify/5713/evidence_human_team_design.json")
    ap.add_argument("--pin-model", default="ds-v41-flash-aliyun")
    ap.add_argument("--timeout", type=int, default=1500)
    args = ap.parse_args()

    t0 = time.time()
    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "phases": [], "verdict": "unknown",
          "design": "材料＝一个要真交付的项目（需求/实现/测试/上线四阶段＋依赖倒置/预算冲突/上线风险）；"
                    "goal 只强制『按人类团队的职能方式建团队』，不规定设几个、不规定切什么域",
          "compare_to": "evidence_forced_cabinet.json（同机制，但材料是『待审成品卡』⇒ 只长出观察员）"}

    def save():
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)

    def phase(name, **kw):
        rec = {"phase": name, "at": datetime.now().isoformat(timespec="seconds"), **kw}
        ev["phases"].append(rec)
        _log(f"{name}: {json.dumps(kw, ensure_ascii=False)[:360]}")
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

    # ── 1 建项目 ＋ 钉模型 ──
    st, wk = _req(args.base, "POST", "/api/works",
                  {"title": f"人类团队职能-{datetime.now().strftime('%m%d-%H%M%S')}"})
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

    # ── 2 四个阶段卡（使命＋现状写"卡在哪"，不是"有什么毛病"）──
    cards = {}
    for key, title, body in STAGES:
        st, c = _req(args.base, "POST", f"/api/works/{wid}/cards",
                     {"title": title, "source": "用户直接创建",
                      "card_type": "sub_project", "parent": root, "goal": ""})
        cid = ((c.get("meta") or c).get("card_id") if isinstance(c, dict) else None)
        cards[key] = cid
        if cid:
            _req(args.base, "PATCH", f"/api/works/{wid}/cards/{cid}",
                 {"updates": {"status_block": body}, "history_note": "预置阶段现状"})
        phase(f"2_card_{key}", ok=bool(cid), card_id=cid, title=title)
    ev["cards"] = cards
    ev["stage_titles"] = {k: t for k, t, _ in STAGES}

    # ── 3 根卡自动模式 ＋ 强制 goal ──
    _req(args.base, "PATCH", f"/api/works/{wid}/cards/{root}",
         {"updates": {"auto_mode": 1}, "history_note": "开自动模式"})
    st, gr = _req(args.base, "POST", f"/api/works/{wid}/cards/{root}/goal",
                  {"input": GOAL_FORCED})
    ok_goal = bool(isinstance(gr, dict) and gr.get("ok"))
    phase("3_set_goal", ok=ok_goal, goal=GOAL_FORCED)
    if not ok_goal:
        ev["verdict"] = "ABORT_GOAL_NOT_SET"
        save()
        return 4
    ev["goal"] = GOAL_FORCED

    # ── 4 等它自己跑 ──
    state = ""
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(POLL)
        st, m = _req(args.base, "GET", f"/api/works/{wid}/cards/{root}")
        mm = (m.get("meta") if isinstance(m, dict) else {}) or {}
        state = str(mm.get("goal_state") or "")
        st2, rl = _req(args.base, "GET", f"/api/works/{wid}/roles")
        nrole = len((rl or {}).get("roles") or []) if isinstance(rl, dict) else -1
        _log(f"等根卡…… goal_state={state} 职能数={nrole}")
        if state in ("complete", "blocked", "paused"):
            break
    phase("4_root_finished", goal_state=state)

    # settle：日志与判词都比"目标态变了"晚落盘（本项目反复踩到的坑）
    time.sleep(12)

    # ── 5 取证 ──
    roles = _roles(args.base, wid, sb, t0)
    st, rc = _req(args.base, "GET", f"/api/works/{wid}/cards/{root}")
    rb = (rc.get("body") if isinstance(rc, dict) else {}) or {}
    ev["roles"] = roles
    ev["root_status"] = rb.get("status_block") or ""
    ev["root_rulings"] = rb.get("requirements") or rb.get("rulings") or ""
    ev["models"] = _models_used(sb, wid, t0)
    phase("5_collected", roles=len(roles))

    vd = _judge(ev)
    ev["verdict_detail"] = vd
    ev["verdict"] = "COLLECTED"
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    ev["elapsed_seconds"] = round(time.time() - t0, 1)
    save()
    print(f"\n判定：{ev['verdict']}（记录事实）", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{args.out}", flush=True)
    return 0


def _role_toolface(sandbox_root, rid, since_ts):
    """角色实际拿到的工具面（从 prompt 日志的 system_prompt 取，结构事实）。

    2026-10-21 起角色被补了 tools_read_file（只读四件）。本函数回答：
    这次跑它到底有没有这项能力、有没有真的用上——
    「补了能力但一次没调用」与「没能力」，在判词质量上是一回事。
    """
    logs = os.path.join(sandbox_root, "logs")
    face = {"has_read_file": False, "has_list_files": False,
            "has_search_files": False, "has_glob_files": False, "scanned": 0}
    if not os.path.isdir(logs):
        return face
    for n in os.listdir(logs):
        if not (n.startswith(f"prompt_{rid}_") and n.endswith(".json")):
            continue
        fp = os.path.join(logs, n)
        try:
            if os.path.getmtime(fp) < since_ts - 5:
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        sp = str(d.get("system_prompt") or "")
        face["scanned"] += 1
        face["has_read_file"] |= "read_file" in sp
        face["has_list_files"] |= "list_files" in sp
        face["has_search_files"] |= "search_files" in sp
        face["has_glob_files"] |= "glob_files" in sp
    return face


def _tool_calls(sandbox_root, wid, rid, since_ts):
    """角色事件流里真调过哪些工具（行为层）。"""
    p = os.path.join(sandbox_root, "works", wid, "events", f"{rid}.jsonl")
    out = []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or '"tool_call"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(e.get("type")) != "tool_call":
                continue
            ts = str(e.get("ts") or "")
            if since_ts and ts:
                try:
                    if datetime.fromisoformat(ts).timestamp() < since_ts - 5:
                        continue
                except ValueError:
                    pass
            d = e.get("data") or {}
            nm = str(d.get("name") or d.get("tool") or "")
            if nm:
                out.append(nm)
    return out


def _roles(base, wid, sandbox_root="", since_ts=0.0):
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
            "statement": str(jur.get("statement") or ""),
            "watch": [w.get("card_id") for w in ((s.get("watch") or {}).get("cards") or [])],
            "signals": (s.get("watch") or {}).get("signals") or [],
            "speak_contract": s.get("speak_contract") or {},
            "charter": (s.get("charter") or {}).get("text") or "",
            "duty": (s.get("duty") or {}).get("state"),
            "verdicts": len(s.get("verdicts") or []),
            "alarms": len(s.get("alarms") or []),
            "directives": [{"id": d.get("id"), "target": d.get("target_card_id"),
                            "instruction": str(d.get("instruction") or "")[:160],
                            "status": d.get("status")}
                           for d in (s.get("directives") or [])],
            "toolface": (_role_toolface(sandbox_root, rid, since_ts)
                         if sandbox_root else {}),
            "tool_calls": (_tool_calls(sandbox_root, wid, rid, since_ts)
                           if sandbox_root else []),
        })
    return out


def _models_used(sandbox_root, wid, since_ts):
    """本 run 真调用自证：时间窗 ＋ 材料指纹归属（并行 work 的日志同目录，必须认回自己）。"""
    fp_tips = ("99.9", "40 万", "32 万", "依赖倒置")
    seen = {}
    logs = os.path.join(sandbox_root, "logs")
    if not os.path.isdir(logs):
        return seen
    for n in os.listdir(logs):
        if not (n.startswith("prompt_") and n.endswith(".json")):
            continue
        p = os.path.join(logs, n)
        try:
            if os.path.getmtime(p) < since_ts:
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        blob = json.dumps({"s": d.get("system_prompt"), "u": d.get("user_prompt"),
                           "p": d.get("prior_messages")}, ensure_ascii=False)
        if sum(1 for t in fp_tips if t in blob) < 2:
            continue
        m = str(d.get("model_used") or "").strip()
        if m:
            seen[m] = seen.get(m, 0) + 1
    return seen


def _classify(role, all_text):
    """启发式分类：看职责里有没有"推动工作"的动词 vs 只有"看"。**只作观察，不作硬判据。**"""
    text = all_text
    has_exec = any(w in text for w in EXEC_WORDS)
    has_watch = any(w in text for w in WATCH_WORDS)
    if has_exec and not has_watch:
        return "执行型"
    if has_watch and not has_exec:
        return "监督型"
    if has_exec and has_watch:
        return "混合"
    return "未分类"


def _judge(ev):
    roles = ev.get("roles") or []
    cards = ev.get("cards") or {}
    work_ids = [c for c in cards.values() if c]

    watched: dict[str, list[str]] = {cid: [] for cid in work_ids}
    for r in roles:
        for cid in r.get("watch") or []:
            if cid in watched:
                watched[cid].append(r["role_id"])
    uncovered = [cid for cid, rs in watched.items() if not rs]

    keys = [r.get("exclusive_key") for r in roles]
    dup = sorted({k for k in keys if k and keys.count(k) > 1})

    # 结构硬判据：有没有真的"下指令"（推动工作），还是只会 alert（旁观）
    per_role = []
    for r in roles:
        text = " ".join([str(r.get("title") or ""), r.get("statement") or "",
                         r.get("charter") or ""])
        per_role.append({
            "id": r["role_id"],
            "title": r.get("title"),
            "key": r.get("exclusive_key"),
            "watch": r.get("watch"),
            "charter_len": len((r.get("charter") or "").strip()),
            "verdicts": r.get("verdicts"),
            "alarms": r.get("alarms"),
            "directive_count": len(r.get("directives") or []),
            "directive_targets": sorted({(d.get("target") or "") for d in (r.get("directives") or [])}),
            "type_heuristic": _classify(r, text),      # ← 启发式，非硬判据
            "hands_on_phrase": any(w in (r.get("charter") or "") for w in HANDS_ON_WORDS),
        })

    types = [x["type_heuristic"] for x in per_role]
    return {
        # 一、结构硬判据
        "role_count": len(roles),
        "exclusive_keys": keys,
        "keys_unique": not dup,
        "duplicate_keys": dup,
        "cards_watched_by": watched,
        "uncovered_cards": uncovered,
        "covers_all_cards": not uncovered,
        "roles_with_directives": sum(1 for x in per_role if x["directive_count"] > 0),
        "total_directives": sum(x["directive_count"] for x in per_role),
        "roles_that_judged": sum(1 for x in per_role if (x["verdicts"] or 0) > 0),
        # 二、分类（启发式，报告里要标明）
        "type_heuristic_counts": {t: types.count(t) for t in set(types)} if types else {},
        "has_exec_type": any(t in ("执行型", "混合") for t in types),
        "only_watch_types": bool(types) and all(t == "监督型" for t in types),
        # 三、可落地性
        "unactionable_hands_on": [x["id"] for x in per_role if x["hands_on_phrase"]],
        "per_role": per_role,
        # 四、只读核验能力（2026-10-21 新增能力，看这次跑有没有用上）
        "roles_with_file_tools": sum(
            1 for r in roles
            if (r.get("toolface") or {}).get("has_read_file")
            or (r.get("toolface") or {}).get("has_list_files")),
        "roles_that_called_file_tools": sum(
            1 for r in roles
            if any(c in ("read_file", "list_files", "search_files", "glob_files")
                   for c in (r.get("tool_calls") or []))),
        "file_tool_usage": {r["role_id"]: [
            c for c in (r.get("tool_calls") or [])
            if c in ("read_file", "list_files", "search_files", "glob_files")]
            for r in roles},
        "models": ev.get("models"),
        "observations": {
            "root_rulings_len": len(str(ev.get("root_rulings") or "")),
            "root_status_len": len(str(ev.get("root_status") or "")),
        },
    }


if __name__ == "__main__":
    sys.exit(main())