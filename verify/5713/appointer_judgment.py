# -*- coding: utf-8 -*-
"""组阁判断力实验（5713）· 把「设不设角色、设几个、域怎么切、什么时候撤」交回根卡模型自己。

**为什么做这个实验**：dynamic_cabinet_v2 验的是角色机制的**机械通道与台账**，但每一步
（何时设/改/停/撤）都是场景预置的——即「验了部门可以随时设撤且不烂账，没验这个 CEO
会不会设」。而第二层职能层按 D02 不设预置清单，全部价值就压在模型的组阁判断力上。

**怎么测**：给根卡一个真实任务 + 一组埋了问题的工作卡，goal 里**不出现任何组阁提示**
（不提角色、不提职能、不提"要不要人盯"）。然后看它自己做了什么。

**判据设计的第一原则：不预设「必须组阁」**。根卡自己改卡、自己写结论、自己判定「不需要
专门角色」——都是合法答案。要判的是两件事：
  ① 埋下的问题有没有被**处理**（被谁处理都算，只要处理得对）；
  ② 局面变了以后，处理方式有没有**跟着调整**（改档/撤岗，而不是叠新角色/什么都不做）。

**埋了什么（根卡不知道这份清单存在）**：
  P1 时间口径互斥    W1 用 UTC 带 Z / W2 用本地 naive —— 无法同时为真
  P2 精度不一致      W1 秒级 / W2 毫秒级 —— 只有逐字读才发现
  P3 成本超支无人盯  W3 预算 5 万/月、实际已花 9.2 万 —— 与接口交付无关的第二个域
  P4 状态自相矛盾    W3 现状块里既写「已上线」又写「待联调」 —— 返工信号

**机械锚点**：每个问题带独一无二的内容指纹（`E-4402` / `8.7 万` / `9.2 万` / `待联调` /
`毫秒`）。判定「有没有被看到」时只查**判词与结论的正文**，不查 prompt 材料——材料里出现
只说明灌进去了，判词里出现才说明它真读懂了。（这不是关键词探针：查的是我自己埋进去的
唯一指纹是否被引用，而不是猜模型的措辞。）

**两臂**：
  A 零干预   goal 只写任务本身
  B 问一次   goal 末尾加一句「你觉得这套项目该不该有人专门盯着某个方面？你自己判断」
             —— 用来和 card_006 的「问一次」假说互证（它那边结论是：问一次改文案不改结构）

用法（5713 隔离实例在跑）：
    python verify/5713/appointer_judgment.py --arm A --base http://127.0.0.1:5713 \
        --out verify/5713/evidence_appointer_A.json
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
_T0 = 0.0
RUN_TIMEOUT = 900          # 根卡自动模式跑一个真实任务，留 15 分钟
MODEL_PIN_DEFAULT = "ds-v41-flash-aliyun"

# ── 埋的材料（指纹必须独一无二，用于事后机械判定「有没有真读到」）──────────
W1_TITLE = "订单结算接口"
W1_STATUS = (
    "订单结算接口现状：\n"
    "1) 对外时间字段口径已定稿：一律 UTC、ISO8601 带 Z 后缀，精度到秒，例 2026-10-20T08:00:00Z。\n"
    "2) 错误码约定：E-4402 = 结算批次不一致，需要人工核对后再重跑。\n"
    "3) 结算批量重跑成本：每月约 8.7 万元（超出部门约定的 6 万元/月上限）。"
)
W2_TITLE = "库存对账接口"
W2_STATUS = (
    "库存对账接口现状：\n"
    "1) 对外时间字段口径已定稿：一律本地时间、naive 19 字符、不带时区，例 2026-10-20 08:00:00。\n"
    "2) 时间戳精度：毫秒级（3 位小数），与账务系统的毫秒存储对齐。\n"
    "3) 依赖订单结算接口的 E-4402 错误码语义，但本接口按 2 位小数截断上报。"
)
W3_TITLE = "经营报表导出"
W3_STATUS = (
    "经营报表导出卡现状：\n"
    "1) 已于 2026-10-15 上线并全量交付业务方（首批 12 个报表已验收）。\n"
    "2) 待联调：与账务系统的导出通道尚未联调通过，当前报表不含坏账口径。\n"
    "3) 成本：部门给的预算是 5 万元/月；截至上月实际支出已达 9.2 万元/月（超支主要来自重复导出重跑）。"
)

# 阶段二：局面变化（互斥消失 + 新风险出现），用于看「会不会调整」而不是叠新角色
W2_STATUS_FIXED = (
    "库存对账接口现状：\n"
    "1) 对外时间字段口径已改为与订单结算接口一致：UTC、ISO8601 带 Z 后缀，精度到秒，例 2026-10-20T08:00:00Z。\n"
    "（原本地 naive、毫秒级的写法已废止，联调已完成。）\n"
    "2) 依赖订单结算接口的 E-4402 错误码语义，已对齐按 2 位小数截断上报。"
)
W3_STATUS_WORSE = (
    "经营报表导出卡现状：\n"
    "1) 已于 2026-10-15 上线并全量交付业务方（首批 12 个报表已验收）。\n"
    "2) 待联调：与账务系统的导出通道尚未联调通过，当前报表不含坏账口径。\n"
    "3) 成本：部门预算是 5 万元/月；截至本月实际支出已达 14.6 万元/月（重跑与人工补数持续叠加）。"
)

GOAL_A = """把这个项目的三张接口卡收口：确认真实状态、找出交付上的问题、能处理的处理掉、处理不了的写清卡在哪。
（不用建新卡，不要碰 5700 生产。）"""

GOAL_B = GOAL_A + """

另外：你觉得这套项目该不该有人专门盯着某个方面？你自己判断，需要就做，不需要就说明为什么不需要。"""


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
    ap.add_argument("--arm", default="A", choices=["A", "B"])
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", default="")
    ap.add_argument("--pin-model", default=MODEL_PIN_DEFAULT)
    ap.add_argument("--timeout", type=int, default=RUN_TIMEOUT)
    ap.add_argument("--phase2", default="1", help="1=做完就改局面再观察一轮；0=只跑第一阶段")
    args = ap.parse_args()
    out = args.out or f"verify/5713/evidence_appointer_{args.arm}.json"

    global _T0
    t0 = time.time()
    _T0 = t0
    ev = {"arm": args.arm, "started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "phases": [], "verdict": "unknown",
          "planted": {"P1_tz_conflict": ["UTC", "本地时间", "naive"],
                      "P2_precision": ["秒", "毫秒"],
                      "P3_cost_overrun": ["9.2 万", "5 万元/月", "14.6 万"],
                      "P4_status_contradiction": ["上线", "待联调"]},
          "note": "本实验不预设「必须组阁」：根卡自己处理/明确判定不需要，都是合法答案。"}

    def save():
        with open(out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)

    def phase(name, **kw):
        rec = {"phase": name, "at": datetime.now().isoformat(timespec="seconds"), **kw}
        ev["phases"].append(rec)
        _log(f"{name}: {json.dumps(kw, ensure_ascii=False)[:400]}")
        save()
        return rec

    # ── 0 验身 ──
    st, inst = _req(args.base, "GET", "/__instance__")
    ev["instance"] = inst
    ok = (st == 200 and isinstance(inst, dict)
          and "5713" in str(inst.get("base_dir"))
          and "5713" in str(inst.get("sandbox_root"))
          and bool(inst.get("sandbox_enabled")))
    phase("0_instance_check", ok=ok, base_dir=str((inst or {}).get("base_dir")))
    if not ok:
        ev["verdict"] = "ABORT_NOT_5713"
        save()
        return 2
    sb = str(inst.get("sandbox_root") or "")

    # ── 1 建项目 + 钉模型 ──
    st, wk = _req(args.base, "POST", "/api/works",
                  {"title": f"组阁判断力-{args.arm}-{datetime.now().strftime('%m%d-%H%M%S')}"})
    wid = wk.get("work_id") if isinstance(wk, dict) else None
    root = (wk.get("work_card_id") or "card_001") if isinstance(wk, dict) else None
    if not wid:
        phase("1_create_work", ok=False, resp=str(wk)[:200])
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

    # ── 2 铺三张工作卡（材料带埋点）──
    cards = {}
    for key, title, status in (("W1", W1_TITLE, W1_STATUS),
                               ("W2", W2_TITLE, W2_STATUS),
                               ("W3", W3_TITLE, W3_STATUS)):
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

    # ── 3 根卡开自动模式 + 给 goal（**不含任何组阁提示**）──
    goal = GOAL_B if args.arm == "B" else GOAL_A
    st, pr = _req(args.base, "PATCH", f"/api/works/{wid}/cards/{root}",
                  {"updates": {"auto_mode": 1},
                   "history_note": "开自动模式（组阁判断力实验）"})
    phase("3_patch_auto", ok=st == 200)
    # 该端点是「人面 goal 命令」：body={"input": "<objective>"} 即建目标（源码 goal/command.py
    # _parse：非 clear/pause/resume/edit 前缀一律当 create）。不是 {"objective":...}。
    st, gr = _req(args.base, "POST", f"/api/works/{wid}/cards/{root}/goal",
                  {"input": goal})
    ok_goal = bool(isinstance(gr, dict) and gr.get("ok"))
    phase("3_set_goal", ok=ok_goal, status=st, arm=args.arm, goal_len=len(goal),
          resp_text=str((gr or {}).get("text") if isinstance(gr, dict) else gr)[:200])
    if not ok_goal:
        _log("建目标失败 —— 继续跑也没意义（根卡不会自转）")
        ev["verdict"] = "ABORT_GOAL_NOT_SET"
        save()
        return 4
    ev["goal"] = goal

    # ── 4 等根卡自己跑（它会不会自己想到组阁，就看这一段）──
    deadline = time.time() + args.timeout
    state = ""
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

    # ── 5 阶段一取证 ──
    ev["phase1"] = _snapshot(args.base, wid, root, sb, cards)
    phase("5_phase1_collected", roles=len(ev["phase1"]["roles"]))

    # ── 6 阶段二：改局面，看它会不会跟着调整 ──
    if args.phase2 == "1":
        _req(args.base, "PATCH", f"/api/works/{wid}/cards/{cards['W2']}",
             {"updates": {"status_block": W2_STATUS_FIXED},
              "history_note": "局面变化：库存接口口径改为与结算接口一致，互斥已消除"})
        _req(args.base, "PATCH", f"/api/works/{wid}/cards/{cards['W3']}",
             {"updates": {"status_block": W3_STATUS_WORSE},
              "history_note": "局面变化：报表导出成本进一步超支到 14.6 万/月"})
        phase("6_situation_changed", ok=True,
              note="P1 互斥消除；P3 成本恶化 (9.2→14.6)")
        # 再点一次根卡，让它有余地调整（改档/撤岗/补角色都算）
        # 用 goal 命令的 edit 把同一目标「重新交给」根卡（不是新目标，保持可比）
        st_e, ed = _req(args.base, "POST", f"/api/works/{wid}/cards/{root}/goal",
                        {"input": "edit " + goal})
        phase("6_rearm", ok=bool(isinstance(ed, dict) and ed.get("ok")), status=st_e,
              resp_text=str((ed or {}).get("text") if isinstance(ed, dict) else ed)[:200])
        deadline2 = time.time() + min(args.timeout, 480)
        while time.time() < deadline2:
            time.sleep(POLL)
            st, m = _req(args.base, "GET", f"/api/works/{wid}/cards/{root}")
            mm = (m.get("meta") if isinstance(m, dict) else {}) or {}
            s2 = str(mm.get("goal_state") or "")
            _log(f"等根卡二审…… goal_state={s2}")
            if s2 in ("complete", "blocked", "paused"):
                state = s2
                break
        ev["phase2"] = _snapshot(args.base, wid, root, sb, cards)
        phase("6_phase2_collected", roles=len(ev["phase2"]["roles"]))

    # ── 7 判据 ──
    vd = _judge(ev)
    ev["verdict_detail"] = vd
    ev["verdict"] = "COLLECTED"   # 本实验不设「过/不过」：判断力没有单一正确答案，只有事实
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    ev["elapsed_seconds"] = round(time.time() - t0, 1)
    save()
    print(f"\n判定：{ev['verdict']}（本实验记录事实，不判通过）", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{out}", flush=True)
    return 0


def _snapshot(base, wid, root, sb, cards):
    """一个阶段的全部结构事实（角色档案、判断簿、警报簿、根卡现状/定论、事件流里的工具调用）。"""
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
            "jurisdiction_statement": (s.get("jurisdiction") or {}).get("statement"),
            "watch": [w.get("card_id") for w in ((s.get("watch") or {}).get("cards") or [])],
            "signals": (s.get("watch") or {}).get("signals"),
            "speak_contract": s.get("speak_contract") or {},
            "escalation": s.get("escalation") or {},
            "duty": (s.get("duty") or {}).get("state"),
            "charter_version": (s.get("charter") or {}).get("version"),
            "charter_text": (s.get("charter") or {}).get("text"),
            "verdicts": [{"id": v.get("id"), "decision": v.get("decision"),
                          "reason": v.get("reason"), "sources": v.get("sources")}
                         for v in (s.get("verdicts") or [])],
            "alarms": [{"id": a.get("id"), "severity": a.get("severity"),
                        "status": a.get("status"), "title": a.get("title"),
                        "detail": a.get("detail")} for a in (s.get("alarms") or [])],
        })
    st, rc = _req(base, "GET", f"/api/works/{wid}/cards/{root}")
    rb = (rc.get("body") if isinstance(rc, dict) else {}) or {}
    return {
        "roles": roles,
        "root_status": rb.get("status_block") or "",
        "root_rulings": rb.get("requirements") or rb.get("rulings") or "",
        "tool_calls": _tool_calls(sb, wid, root),
        "models": _models_used(sb, wid, _T0),
    }


def _tool_calls(sandbox_root, wid, cid):
    """根卡事件流里出现过的工具名（证明它到底想过/做过什么，而不是只写在正文里）。"""
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
            d = e.get("data") or {}
            name = str(d.get("name") or d.get("tool") or "")
            if name:
                out.append(name)
    return out


def _models_used(sandbox_root, wid, since_ts=0.0):
    """本 run 实际用到的模型（**只认本次时间窗内的日志**）。

    第一版漏了时间过滤，把同一沙箱里别的 work（甚至别的卡在跑的实验）的 prompt 日志
    一起统计进来，于是出现 k3/minimax/deepseek-flash 混杂——那是假象。实测教训：
    **统计口径不圈定时间窗/范围，就是把别人的账记到自己头上。**
    """
    seen = {}
    logs = os.path.join(sandbox_root, "logs")
    if os.path.isdir(logs):
        for n in os.listdir(logs):
            if not (n.startswith("prompt_") and n.endswith(".json")):
                continue
            fp = os.path.join(logs, n)
            try:
                if os.path.getmtime(fp) < since_ts:
                    continue
                with open(fp, encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            m = str(d.get("model_used") or "").strip()
            if m:
                seen[m] = seen.get(m, 0) + 1
    return seen


def _hit(text, needles):
    t = str(text or "")
    return any(n in t for n in needles)


def _judge(ev):
    """只出事实，不打「对/错」。判断力没有唯一正解，但下面每一项都能被人核。"""
    p1 = ev.get("phase1") or {}
    p2 = ev.get("phase2") or {}
    roles1 = p1.get("roles") or []
    roles2 = p2.get("roles") or []
    verdict_text = " ".join(str(v.get("reason") or "") + " " + json.dumps(v.get("sources") or [], ensure_ascii=False)
                            for r in roles1 for v in (r.get("verdicts") or []))
    conclude_text = str(p1.get("root_rulings") or "") + " " + str(p1.get("root_status") or "")
    handled_text = verdict_text + " " + conclude_text + " " + " ".join(
        json.dumps(a, ensure_ascii=False) for r in roles1 for a in (r.get("alarms") or []))
    p2_ids = {r.get("role_id") for r in roles2}
    p1_ids = {r.get("role_id") for r in roles1}
    return {
        # ① 有没有组阁（不预设必须），以及组了几个
        "summoned": len(roles1) > 0,
        "role_count_p1": len(roles1),
        "exclusive_keys": [r.get("exclusive_key") for r in roles1],
        "exclusive_keys_unique": (len({r.get("exclusive_key") for r in roles1 if r.get("exclusive_key")})
                                  == len([r for r in roles1 if r.get("exclusive_key")])),
        # ② 组出来的是不是「能说话的角色」：写了触发条件、绑了关注卡、职责书非空
        "charters_written": all(bool(str(r.get("charter_text") or "").strip()) for r in roles1) if roles1 else None,
        "speak_contract_has_triggers": [
            {"role": r.get("role_id"),
             "advice_when": (r.get("speak_contract") or {}).get("advice_when"),
             "alarm_when": (r.get("speak_contract") or {}).get("alarm_when")}
            for r in roles1],
        "watch_bound": [bool(r.get("watch")) for r in roles1],
        # ③ 埋下的问题有没有被处理（谁处理都算）
        "P1_tz_conflict_seen": _hit(handled_text, ["UTC", "本地", "naive", "时区"]),
        "P2_precision_seen": _hit(handled_text, ["毫秒", "精度"]),
        "P3_cost_seen": _hit(handled_text, ["9.2", "14.6", "超支", "成本", "预算"]),
        "P4_contradiction_seen": _hit(handled_text, ["待联调", "上线", "联调"]),
        "root_concluded_anything": bool(str(p1.get("root_rulings") or "").strip()),
        "root_tool_calls": p1.get("tool_calls"),
        # ④ 局面变了以后有没有调整
        "phase2_ran": bool(p2),
        "roles_added_p2": sorted(p2_ids - p1_ids),
        "roles_removed_p2": sorted(p1_ids - p2_ids),
        "charter_version_changed_p2": (
            {r.get("role_id"): r.get("charter_version") for r in roles2} !=
            {r.get("role_id"): r.get("charter_version") for r in roles1}) if p2 else None,
        "duty_changed_p2": (
            {r.get("role_id"): r.get("duty") for r in roles2} !=
            {r.get("role_id"): r.get("duty") for r in roles1}) if p2 else None,
        "any_adjustment_p2": bool(p2) and (
            bool(p2_ids - p1_ids) or bool(p1_ids - p2_ids)
            or ({r.get("role_id"): r.get("charter_version") for r in roles2} !=
                {r.get("role_id"): r.get("charter_version") for r in roles1})
            or ({r.get("role_id"): r.get("duty") for r in roles2} !=
                {r.get("role_id"): r.get("duty") for r in roles1})),
        "models_observed": p1.get("models"),
    }


if __name__ == "__main__":
    sys.exit(main())