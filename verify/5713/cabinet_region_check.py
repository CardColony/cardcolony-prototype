# -*- coding: utf-8 -*-
"""第二层区（order 175）真机验收：**这个区到底有没有改变根卡的行为**。

**要回答的问题**
`docs/5713-组阁判断力实验-报告-20261020.md` 记的失效点是「会设、不会调」：
局面变了（新增一张冲突卡）后，根卡 `list_roles` → `read_card` 读过角色卡，
仍然不改档、不撤岗、不扩关注范围（`evidence_appointer_B_newcard.json`，work_047）。

本驱动器跑**同一份材料、同一句 goal、同一个模型**，唯一差别是代码里有 `role_cabinet` 区
（order 175，每轮把阁员现状＋机械检测到的局面变化摊到根卡眼前）。于是：

    work_047（无区）  ⟷  本次（有区）

**判据分两层，都不看模型措辞**
1. **注入层**（证明区真的进了上下文）：根卡 prompt 日志 `system_prompt` 里出现本区标题
   ——这是我写死的字符串，不是猜模型用词。
2. **行为层**（证明区起了作用）：第 2 阶段新增卡之后，角色 `watch` 是否纳入了新卡 /
   职责书是否升版 / 角色集合是否变化；**外加**根卡是否真的调了调阁工具
   （`role_watch_cards` / `update_role_profile` / `role_set_duty`）——工具名是结构事实。

用法（隔离实例在跑）：
    python verify/5713/cabinet_region_check.py --base http://127.0.0.1:5714 \
        --out verify/5713/evidence_cabinet_region.json
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
import appointer_judgment as aj          # 复用同一份埋点材料，保证与 work_047 可比

POLL = 10
REGION_TITLE = "第二层（职能角色）"        # 我写死的区标题（注入层判据用它）
ADJUST_TOOLS = ("role_watch_cards", "update_role_profile", "role_set_duty")


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
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", default="verify/5713/evidence_cabinet_region.json")
    ap.add_argument("--pin-model", default="ds-v41-flash-aliyun")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    t0 = time.time()
    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "phases": [], "verdict": "unknown",
          "compare_to": "evidence_appointer_B_newcard.json（同为 B 臂，但那次代码里没有 175 区）"}

    def save():
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)

    def phase(name, **kw):
        rec = {"phase": name, "at": datetime.now().isoformat(timespec="seconds"), **kw}
        ev["phases"].append(rec)
        _log(f"{name}: {json.dumps(kw, ensure_ascii=False)[:380]}")
        save()
        return rec

    # ── 0 验身（必须是 5713 的隔离实例，允许 5714 端口）──
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
                  {"title": f"175区验收-{datetime.now().strftime('%m%d-%H%M%S')}"})
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
        data = {}
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    data = json.load(f) or {}
            data.setdefault("work", {})["default_model"] = args.pin_model
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            phase("1b_pin_model", ok=True, model=args.pin_model)
        except Exception as exc:  # noqa: BLE001
            phase("1b_pin_model", ok=False, error=str(exc)[:200])

    # ── 2 三张工作卡（与 work_047 同一份埋点材料）──
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

    # ── 3 根卡自动模式 ＋ goal（B 臂原句，与 work_047 逐字相同）──
    goal = aj.GOAL_B
    _req(args.base, "PATCH", f"/api/works/{wid}/cards/{root}",
         {"updates": {"auto_mode": 1}, "history_note": "开自动模式"})
    st, gr = _req(args.base, "POST", f"/api/works/{wid}/cards/{root}/goal", {"input": goal})
    ok_goal = bool(isinstance(gr, dict) and gr.get("ok"))
    phase("3_set_goal", ok=ok_goal, arm="B", goal_len=len(goal))
    if not ok_goal:
        ev["verdict"] = "ABORT_GOAL_NOT_SET"
        save()
        return 4
    ev["goal"] = goal

    # ── 4 等根卡第一轮收口 ──
    state = _wait_root(args.base, wid, root, args.timeout)
    phase("4_root_finished", goal_state=state)
    _settle(args.base, wid, root, "phase1")
    ev["phase1"] = _snapshot(args.base, wid, root, sb, cards, t0)
    phase("5_phase1_collected", roles=len(ev["phase1"]["roles"]),
          region_injected=ev["phase1"]["region_injected"])

    # ── 6 局面变化：新增一张冲突卡（不改已有卡，避开与根卡编辑撞车）──
    st, c = _req(args.base, "POST", f"/api/works/{wid}/cards",
                 {"title": aj.W4_TITLE, "source": "用户直接创建",
                  "card_type": "sub_project", "parent": root, "goal": ""})
    w4 = ((c.get("meta") or c).get("card_id") if isinstance(c, dict) else None)
    if w4:
        _req(args.base, "PATCH", f"/api/works/{wid}/cards/{w4}",
             {"updates": {"status_block": aj.W4_STATUS}, "history_note": "新接口加入项目"})
    cards["W4"] = w4
    ev["cards"] = cards
    phase("6_situation_changed", ok=bool(w4), card_id=w4, title=aj.W4_TITLE)

    # ── 7 把同一目标重新交给根卡（不是新目标，保持可比）──
    t_rearm = time.time()
    st_e, ed = _req(args.base, "POST", f"/api/works/{wid}/cards/{root}/goal",
                    {"input": "edit " + goal})
    phase("7_rearm", ok=bool(isinstance(ed, dict) and ed.get("ok")), status=st_e)
    state2 = _wait_root(args.base, wid, root, min(args.timeout, 600))
    phase("8_root_finished_p2", goal_state=state2)
    _settle(args.base, wid, root, "phase2")
    ev["phase2"] = _snapshot(args.base, wid, root, sb, cards, t_rearm)
    phase("9_phase2_collected", roles=len(ev["phase2"]["roles"]))

    # ── 10 判据 ──
    vd = _judge(ev)
    ev["verdict_detail"] = vd
    ev["verdict"] = "COLLECTED"
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    ev["elapsed_seconds"] = round(time.time() - t0, 1)
    save()
    print(f"\n判定：{ev['verdict']}", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{args.out}", flush=True)
    return 0


def _settle(base, wid, root, why=""):
    """回合收口后**再等一段**才取证。

    这是我在本项目里第 4 次踩同一类坑（dynamic_cabinet_v2 §11.4、appointer_judgment §8、
    cabinet_region_check 首次跑），根因固定在两处时间差：
      · 判词/现状在回合**中途**落账，prompt 日志在回合**结束**才写；
      · 队列消费与 duty 记账在收口之后还有一小段异步尾巴。
    以「goal_state 变成 complete」或「判词出现了」当采集触发点，就会漏掉尾段
    （实测：phase1 `region_injected` scanned=0、models 为空，因为日志比快照晚 6 秒落盘）。
    所以统一在取证前：等 pending 排空 + 给一段写入窗口。
    """
    deadline = time.time() + 90
    while time.time() < deadline:
        st, rl = _req(base, "GET", f"/api/works/{wid}/roles")
        rows = (rl or {}).get("roles") or [] if isinstance(rl, dict) else []
        pend = 0
        for r in rows:
            st2, s = _req(base, "GET", f"/api/works/{wid}/roles/{r.get('role_id')}")
            if isinstance(s, dict):
                pend += int((s.get("duty") or {}).get("pending_sync") or 0)
        if pend == 0:
            break
        time.sleep(5)
    time.sleep(15)      # prompt 日志写入窗口
    _log(f"settle 完成({why})")


def _wait_root(base, wid, root, timeout):
    deadline = time.time() + timeout
    state = ""
    while time.time() < deadline:
        time.sleep(POLL)
        st, m = _req(base, "GET", f"/api/works/{wid}/cards/{root}")
        mm = (m.get("meta") if isinstance(m, dict) else {}) or {}
        state = str(mm.get("goal_state") or "")
        st2, rl = _req(base, "GET", f"/api/works/{wid}/roles")
        nrole = len((rl or {}).get("roles") or []) if isinstance(rl, dict) else -1
        _log(f"等根卡…… goal_state={state} 角色数={nrole}")
        if state in ("complete", "blocked", "paused"):
            break
    return state


def _snapshot(base, wid, root, sb, cards, since_ts):
    """一次阶段的全部结构事实：阁员档案／判断簿／根卡正文／工具调用／区是否注入。"""
    st, rl = _req(base, "GET", f"/api/works/{wid}/roles")
    roles = []
    for r in ((rl or {}).get("roles") or []):
        rid = str(r.get("role_id") or "")
        st2, s = _req(base, "GET", f"/api/works/{wid}/roles/{rid}")
        s = s if isinstance(s, dict) else {}
        roles.append({
            "role_id": rid,
            "title": s.get("title"),
            "exclusive_key": (s.get("jurisdiction") or {}).get("exclusive_key"),
            "watch": [w.get("card_id") for w in ((s.get("watch") or {}).get("cards") or [])],
            "duty": (s.get("duty") or {}).get("state"),
            "charter_version": (s.get("charter") or {}).get("version"),
            "verdicts": len(s.get("verdicts") or []),
            "alarms": len(s.get("alarms") or []),
        })
    st, rc = _req(base, "GET", f"/api/works/{wid}/cards/{root}")
    rb = (rc.get("body") if isinstance(rc, dict) else {}) or {}
    calls = _tool_calls(sb, wid, root, since_ts)
    return {
        "roles": roles,
        "root_status": rb.get("status_block") or "",
        "tool_calls": calls,
        "adjust_tools_used": [c for c in calls if c in ADJUST_TOOLS],
        "region_injected": _region_in_prompt(sb, since_ts),
        "models": aj._models_used(sb, wid, since_ts, aj.FINGERPRINTS),
    }


def _tool_calls(sandbox_root, wid, cid, since_ts):
    p = os.path.join(sandbox_root, "works", wid, "events", f"{cid}.jsonl")
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
            name = str(d.get("name") or d.get("tool") or "")
            if name:
                out.append(name)
    return out


def _region_in_prompt(sandbox_root, since_ts):
    """注入层判据：根卡 prompt 日志的 system_prompt 里有没有**我写死的区标题**。

    这是结构事实（我自己写的字符串），不是拿关键词去猜模型措辞。
    同时回报命中的日志文件名，便于复核。
    """
    logs = os.path.join(sandbox_root, "logs")
    hits = []
    total = 0
    if not os.path.isdir(logs):
        return {"found": False, "hits": [], "scanned": 0}
    for n in sorted(os.listdir(logs)):
        if not (n.startswith("prompt_card_001") and n.endswith(".json")):
            continue
        fp = os.path.join(logs, n)
        try:
            if os.path.getmtime(fp) < since_ts - 5:
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        total += 1
        if REGION_TITLE in str(d.get("system_prompt") or ""):
            hits.append(n)
    return {"found": bool(hits), "hits": hits[-3:], "scanned": total}


def _judge(ev):
    p1 = ev.get("phase1") or {}
    p2 = ev.get("phase2") or {}
    r1 = p1.get("roles") or []
    r2 = p2.get("roles") or []
    ids1 = {r.get("role_id") for r in r1}
    ids2 = {r.get("role_id") for r in r2}
    w4 = (ev.get("cards") or {}).get("W4")
    vmap1 = {r.get("role_id"): r.get("charter_version") for r in r1}
    vmap2 = {r.get("role_id"): r.get("charter_version") for r in r2}
    dmap1 = {r.get("role_id"): r.get("duty") for r in r1}
    dmap2 = {r.get("role_id"): r.get("duty") for r in r2}
    watched_new = [{"role": r.get("role_id"), "watch": r.get("watch"),
                    "watched_new_card": w4 in (r.get("watch") or [])}
                   for r in r2]
    return {
        # 注入层：区真的进了根卡上下文
        "region_injected_p1": bool((p1.get("region_injected") or {}).get("found")),
        "region_injected_p2": bool((p2.get("region_injected") or {}).get("found")),
        "region_hits_p2": (p2.get("region_injected") or {}).get("hits"),
        # 基线对齐：B 臂该组阁（work_044/047 两跑都组了）
        "role_created_p1": len(r1) > 0,
        "exclusive_keys": [r.get("exclusive_key") for r in r1],
        # 行为层：局面变了之后有没有调整
        "w4_card_id": w4,
        "new_card_watched_p2": watched_new,
        "charter_version_changed_p2": vmap1 != vmap2,
        "duty_changed_p2": dmap1 != dmap2,
        "roles_added_p2": sorted(ids2 - ids1),
        "roles_removed_p2": sorted(ids1 - ids2),
        "adjust_tools_used_p2": (p2.get("adjust_tools_used") or []),
        # 排他键保全：改档之后 exclusive_key 不能丢（真机实测过它会被整字典覆盖擦掉）
        "exclusive_key_kept_p2": [r.get("exclusive_key") for r in r2],
        "any_adjustment_p2": bool(
            any(x.get("watched_new_card") for x in watched_new)
            or (vmap1 != vmap2) or (dmap1 != dmap2)
            or (ids2 - ids1) or (ids1 - ids2)
            or (p2.get("adjust_tools_used") or [])),
        "models_p1": p1.get("models"),
        "models_p2": p2.get("models"),
    }


if __name__ == "__main__":
    sys.exit(main())