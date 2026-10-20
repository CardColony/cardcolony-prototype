# -*- coding: utf-8 -*-
"""动态调阁试走（5713）· 第二层职能角色「CEO 按局面自组阁、中途灵活调整」全生命周期真机实测。

**测什么**：不是测单个角色会不会干活（5710 已证），而是测 card_004 定论 D02 的验收口径——
「动态调阁全生命周期」在 5713 基线上是否逐环走通且逐环留账：

  A 组阁   根卡组阁一个持排他责任域的角色（HTTP 面＝根卡工具同一执行体）
  A' 排他  同排他键再组第二个 ⇒ 机械拒（400，责任域已被占用）
  A'' 巡检  人工 check ⇒ 真模型巡检 ⇒ 必落判词（speak/silent 都落账），互斥材料应触发警报
  B 改档   中途改职责书 ⇒ 版本升 2、v1 原文留在版本史（调阁不丢历史）
  C 停职   suspended ⇒ check 被挡（started=false）但同步队列仍进（事实不丢）
  D 复职   active ⇒ 立即补巡检 ⇒ 新判词落账（材料已对齐 ⇒ 应判 silent / 自主 correct 旧警报，观察）
  E 撤岗   archived ⇒ 关注连线解除，但判断簿/警报簿台账保留（撤岗留痕）
  F 顶空域 同排他键组新角色 ⇒ 成功（archived 不占域）；再组第三个同键 ⇒ 又被机械拒

**怎么保证不碰 5700**：与 channel_walk.py 同一套——先打 /__instance__ 验身
（base_dir/sandbox_root/sandbox_enabled 三者必须是 5713 的），不符立即中止。

**根卡在哪**：本驱动器走 HTTP 组阁面（POST /works/{wid}/roles），其执行体与根卡工具
create_role 是同一个（routes.py:1320 注释「与根卡工具 create_role 同一执行体」）——
即「CEO/根卡组阁」的机械通道，区别只在没有真模型根卡做决策。决策内容（何时设/改/撤）
由实验场景预置，这是本实验的如实边界，写进证据。

用法（5713 隔离实例已在跑时）：
    python verify/5713/dynamic_cabinet.py --base http://127.0.0.1:5713 \
        --out verify/5713/evidence_dynamic_cabinet.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

POLL = 8
VERDICT_TIMEOUT = 420          # 单轮真模型巡检最长等 7 分钟

# ── 场景材料（两工作卡互斥口径，5710 真模型实测会被判 mutually_exclusive_fact）──
W1_STATUS = ("生产订单统计接口现状：时间口径已定稿——全部用 UTC，ISO8601 带 Z 后缀，"
             "例 2026-10-20T08:00:00Z。对外文档与实现已按此交付。")
W2_STATUS_CONFLICT = ("库存接口现状：时间口径已定稿——全部用本地时间，naive 19 字符，"
                      "例 2026-10-20 16:00:00（不带时区）。下游对账已按此跑批。")
W2_STATUS_ALIGNED = ("库存接口现状：时间口径已改为与生产订单统计接口一致——全部用 UTC，"
                     "ISO8601 带 Z 后缀，例 2026-10-20T08:00:00Z。下游对账已切换。")

CHARTER_V1 = ("对全项目接口口径一致性负责。巡检关注卡时，若发现两张卡的时间/格式口径"
              "互相排斥（无法同时为真），必须 raise_alarm(severity=warn) 并把互斥原文写进证据；"
              "口径一致或不构成互斥时保持沉默（判 silent 也须 record_verdict 落账）。")
CHARTER_V2 = ("对全项目接口口径一致性负责（v2 增补）：除互斥口径警报外，新增检查时间戳"
              "精度一致性（秒 vs 毫秒）；精度不一致时先给关注卡发建议，只有互斥才警报。")


def _req(base, method, path, body=None, timeout=30):
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _role(base, wid, rid):
    st, res = _req(base, "GET", f"/api/works/{wid}/roles/{rid}")
    return res if st == 200 and isinstance(res, dict) else {}


def _wait_verdict(base, wid, rid, before, timeout=VERDICT_TIMEOUT):
    """等判断簿行数超过 before；返回 (最新summary, 是否等到)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(POLL)
        s = _role(base, wid, rid)
        vs = s.get("verdicts") or []
        _log(f"等判词…… verdicts={len(vs)}（起点 {before}） pending={s.get('pending_sources')}")
        if len(vs) > before:
            return s, True
    return _role(base, wid, rid), False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", default="verify/5713/evidence_dynamic_cabinet.json")
    args = ap.parse_args()

    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "phases": [], "verdict": "unknown",
          "boundary_note": "组阁/改档/停复撤的决策由实验场景预置（HTTP 面与根卡工具同一执行体），"
                           "本实验验的是机械通道与台账逐环走通，不验根卡模型的组阁决策质量。"}

    def phase(name, **kw):
        rec = {"phase": name, "at": datetime.now().isoformat(timespec="seconds"), **kw}
        ev["phases"].append(rec)
        _log(f"{name}: {json.dumps(kw, ensure_ascii=False)[:360]}")
        return rec

    # ── 0 验身 ──────────────────────────────────────────────────────────
    st, inst = _req(args.base, "GET", "/__instance__")
    ok_inst = (st == 200 and isinstance(inst, dict)
               and "5713" in str(inst.get("base_dir"))
               and "5713" in str(inst.get("sandbox_root"))
               and bool(inst.get("sandbox_enabled")))
    phase("0_instance_check", ok=ok_inst, base_dir=str((inst or {}).get("base_dir")),
          sandbox_root=str((inst or {}).get("sandbox_root")))
    ev["instance"] = inst
    if not ok_inst:
        ev["verdict"] = "ABORT_NOT_5713"
        _save(args.out, ev)
        return 2

    # ── 1 建项目＋两张互斥工作卡 ─────────────────────────────────────────
    st, wk = _req(args.base, "POST", "/api/works",
                  {"title": f"动态调阁-5713-{datetime.now().strftime('%m%d-%H%M%S')}"})
    wid = wk.get("work_id") if isinstance(wk, dict) else None
    root = (wk.get("work_card_id") or "card_001") if isinstance(wk, dict) else None
    if not wid:
        phase("1_create_work", ok=False, status=st, resp=str(wk)[:200])
        _save(args.out, ev)
        return 3
    phase("1_create_work", ok=True, work_id=wid, root=root)

    cards = {}
    for key, title in (("W1", "生产订单统计接口"), ("W2", "库存接口")):
        st, c = _req(args.base, "POST", f"/api/works/{wid}/cards",
                     {"title": title, "source": "用户直接创建",
                      "card_type": "sub_project", "parent": root, "goal": ""})
        cid = ((c.get("meta") or c).get("card_id") if isinstance(c, dict) else None)
        cards[key] = cid
        phase(f"1_create_{key}", ok=st in (200, 201), card_id=cid, title=title)
    _patch_status(args.base, wid, cards["W1"], W1_STATUS)
    _patch_status(args.base, wid, cards["W2"], W2_STATUS_CONFLICT)
    phase("1_seed_conflict_material", ok=True, W1_status=W1_STATUS, W2_status=W2_STATUS_CONFLICT)

    # ── A 组阁 R1 ────────────────────────────────────────────────────────
    st, r1 = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "口径一致性角色",
        "charter": CHARTER_V1,
        "jurisdiction": {"domain": "interface_contract",
                         "statement": "接口口径一致性",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [cards["W1"], cards["W2"]],
        "signals": ["turn_completed", "status_changed", "decision_appended"],
        "speak_contract": {"advice_when": ["gap_or_risk"],
                           "alarm_when": ["mutually_exclusive_fact"],
                           "silence_default": True, "max_messages_per_wake": 2},
        "escalation": {"advice_to": "source_card", "alarm_to": "human",
                       "critical_to": "root_and_human"},
    })
    r1_id = r1.get("role_card_id") if isinstance(r1, dict) else None
    s1 = _role(args.base, wid, r1_id) if r1_id else {}
    watched = [w.get("card_id") for w in ((s1.get("watch") or {}).get("cards") or [])]
    phase("A_create_role", ok=st == 201 and bool(r1_id), status=st, role_id=r1_id,
          watched=watched, charter_version=(s1.get("charter") or {}).get("version"),
          duty=(s1.get("duty") or {}).get("state"),
          exclusive_key=(s1.get("jurisdiction") or {}).get("exclusive_key"))
    ev["role_R1"] = r1_id
    ev["cards"] = cards

    # ── A' 同排他键再组 ⇒ 机械拒 ────────────────────────────────────────
    st, dup = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "撞域角色", "charter": "抢占 interface_contract 域",
        "jurisdiction": {"domain": "interface_contract", "statement": "撞域",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [cards["W1"]]})
    phase("A_dup_domain_rejected", ok=st == 400, status=st, resp=str(dup)[:240])

    # ── A'' 人工巡检 ⇒ 真模型判词＋警报 ─────────────────────────────────
    s0 = _role(args.base, wid, r1_id)
    v_before = len(s0.get("verdicts") or [])
    st, chk = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/check", {})
    started = bool(isinstance(chk, dict) and chk.get("started"))
    phase("A_check", ok=st == 200, started=started,
          pending_sources=(chk or {}).get("pending_sources"))
    sA, gotA = _wait_verdict(args.base, wid, r1_id, v_before)
    vA = (sA.get("verdicts") or [None])[0]
    alarmsA = sA.get("alarms") or []
    openA = sA.get("open_alarms") or []
    phase("A_verdict", ok=gotA, decision=(vA or {}).get("decision"),
          auto=(vA or {}).get("auto"), reason=str((vA or {}).get("reason"))[:200],
          verdict_id=(vA or {}).get("id"),
          alarms_open=len(openA),
          alarm_severities=[a.get("severity") for a in openA])
    ev["verdict_A"] = vA
    ev["alarms_A"] = alarmsA

    # ── B 改档（职责书升版本）───────────────────────────────────────────
    st, pb = _req(args.base, "PATCH", f"/api/works/{wid}/roles/{r1_id}",
                  {"charter": CHARTER_V2})
    sB = _role(args.base, wid, r1_id)
    ch = sB.get("charter") or {}
    hist = ch.get("history") or []
    phase("B_amend_charter", ok=st == 200, status=st,
          version=ch.get("version"), history_len=len(hist),
          v1_kept=any(CHARTER_V1[:20] in str(h.get("text") or "") for h in hist))

    # ── C 停职 ⇒ check 被挡，但同步仍进队 ───────────────────────────────
    st, _ = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/duty",
                 {"state": "suspended", "note": "动态调阁实验：CEO 停职"})
    _patch_status(args.base, wid, cards["W2"], W2_STATUS_ALIGNED)   # 制造新事实
    st, chkC = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/check", {})
    sC = _role(args.base, wid, r1_id)
    phase("C_suspended", duty=(sC.get("duty") or {}).get("state"),
          check_started=bool((chkC or {}).get("started")),
          pending_sources=(chkC or {}).get("pending_sources"),
          pending_sync=(sC.get("duty") or {}).get("pending_sync"))

    # ── D 复职 ⇒ 立即补巡检 ⇒ 新判词（材料已对齐，观察 silent/correct）───
    v_beforeD = len((sC.get("verdicts")) or [])
    st, _ = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/duty",
                 {"state": "active", "note": "动态调阁实验：CEO 复职"})
    sD, gotD = _wait_verdict(args.base, wid, r1_id, v_beforeD)
    vD = (sD.get("verdicts") or [None])[0]
    openD = sD.get("open_alarms") or []
    corrected = [a for a in (sD.get("alarms") or []) if a.get("status") != "open"]
    phase("D_resumed", ok=gotD, duty=(sD.get("duty") or {}).get("state"),
          decision=(vD or {}).get("decision"), reason=str((vD or {}).get("reason"))[:200],
          open_alarms_after=len(openD), corrected_alarms=len(corrected))
    ev["verdict_D"] = vD

    # ── E 撤岗 ⇒ 关注解除，台账保留 ─────────────────────────────────────
    st, _ = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/duty",
                 {"state": "archived", "note": "动态调阁实验：CEO 撤岗"})
    sE = _role(args.base, wid, r1_id)
    watchedE = (sE.get("watch") or {}).get("cards") or []
    phase("E_archived", duty=(sE.get("duty") or {}).get("state"),
          watched_after=len(watchedE),
          verdicts_kept=len(sE.get("verdicts") or []),
          alarms_kept=len(sE.get("alarms") or []),
          charter_history_kept=len((sE.get("charter") or {}).get("history") or []))

    # ── F 顶空域 ⇒ 新角色成功；再撞域 ⇒ 又拒 ────────────────────────────
    st, r2 = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "接口契约守门员", "charter": "对接口契约（含口径）负全责",
        "jurisdiction": {"domain": "interface_contract", "statement": "接口契约",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [cards["W1"], cards["W2"]]})
    r2_id = r2.get("role_card_id") if isinstance(r2, dict) else None
    st3, dup2 = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "再撞域角色", "charter": "再抢 interface_contract",
        "jurisdiction": {"domain": "interface_contract", "statement": "再撞域",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [cards["W1"]]})
    phase("F_refill", refill_ok=st == 201 and bool(r2_id), new_role=r2_id,
          dup_after_refill_rejected=st3 == 400, dup_status=st3, resp=str(dup2)[:240])
    ev["role_R2"] = r2_id

    # ── 终态取证与机械判定 ──────────────────────────────────────────────
    ev["final_R1"] = _role(args.base, wid, r1_id)
    if r2_id:
        ev["final_R2"] = _role(args.base, wid, r2_id)
    vd = _judge(ev)
    ev["verdict_detail"] = vd
    ev["verdict"] = "PASS" if all(v for k, v in vd.items() if k != "observations") else "INCOMPLETE"
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _save(args.out, ev)
    print(f"\n判定：{ev['verdict']}", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{args.out}", flush=True)
    return 0 if ev["verdict"] == "PASS" else 1


def _patch_status(base, wid, cid, text):
    return _req(base, "PATCH", f"/api/works/{wid}/cards/{cid}",
                {"updates": {"status_block": text}, "history_note": "动态调阁实验：预置材料"})


def _save(out, ev):
    with open(out, "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)


def _find_phase(ev, name):
    for p in ev.get("phases") or []:
        if p.get("phase") == name:
            return p
    return {}


def _judge(ev):
    pA = _find_phase(ev, "A_create_role")
    pAdup = _find_phase(ev, "A_dup_domain_rejected")
    pAc = _find_phase(ev, "A_check")
    pAv = _find_phase(ev, "A_verdict")
    pB = _find_phase(ev, "B_amend_charter")
    pC = _find_phase(ev, "C_suspended")
    pD = _find_phase(ev, "D_resumed")
    pE = _find_phase(ev, "E_archived")
    pF = _find_phase(ev, "F_refill")
    cards = ev.get("cards") or {}
    return {
        "A_role_created": bool(pA.get("ok")),
        "A_watch_bound": sorted(pA.get("watched") or []) == sorted([cards.get("W1"), cards.get("W2")]),
        "A_dup_domain_rejected": bool(pAdup.get("ok")),
        "A_check_started": bool(pAc.get("started")),
        "A_verdict_recorded": bool(pAv.get("ok")) and pAv.get("decision") in ("speak", "silent"),
        "A_alarm_raised_on_conflict": (pAv.get("alarms_open") or 0) >= 1,
        "B_charter_v2": pB.get("version") == 2,
        "B_history_keeps_v1": bool(pB.get("v1_kept")),
        "C_check_blocked_when_suspended": (pC.get("duty") == "suspended"
                                           and pC.get("check_started") is False),
        "C_sync_still_queued": bool(pC.get("pending_sources")) or (pC.get("pending_sync") or 0) >= 1,
        "D_resume_new_verdict": bool(pD.get("ok")),
        "E_archived_unwatched": (pE.get("duty") == "archived"
                                 and (pE.get("watched_after") or 0) == 0),
        "E_ledgers_retained": (pE.get("verdicts_kept") or 0) >= 2
                              and (pE.get("charter_history_kept") or 0) >= 2,
        "F_refill_created": bool(pF.get("refill_ok")),
        "F_conflict_rejected_again": bool(pF.get("dup_after_refill_rejected")),
        "observations": {
            "D_decision_when_aligned": pD.get("decision"),
            "D_corrected_alarms": pD.get("corrected_alarms"),
            "D_open_alarms_after": pD.get("open_alarms_after"),
        },
    }


if __name__ == "__main__":
    sys.exit(main())
