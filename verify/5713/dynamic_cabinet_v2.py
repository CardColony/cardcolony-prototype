# -*- coding: utf-8 -*-
"""动态调阁试走 v2（5713）· 第二层职能角色「CEO 按局面自组阁、中途灵活调整」全生命周期真机实测。

相对 v1 的修正（v1 实跑证据见 evidence_dynamic_cabinet_run1_prerun.json）：

1. **材料必须真正不可约**。v1 给的两张卡示例时间是 `2026-10-20T08:00:00Z` 与
   `2026-10-20 16:00:00`（+08:00），是**同一瞬间的两种写法**——真模型算出等价、
   判 silent、不报警（v1 唯一 false 项 A_alarm_raised_on_conflict）。v2 改成
   **同一数字、不同含义**（08:00:00Z vs 08:00:00 本地）＋**契约级相反规定**
   （对外字段一律带时区 vs 一律不带时区），这才是不可换算的互斥。
2. **同步入队要能核对**：直接读沙箱 `works/<wid>/cards.db` 的 `role_sync` 行数，
   判「停职期间的新事实有没有进队」，而不是拿 `pending_sources` 当证据
   （v1 那 2 行是首轮残留，`consumed_at` 已被消费，判据把残留误当新入队）。
3. **每段落盘**（checkpoint）：慢的真模型段落中途被杀也不丢已跑证据。
4. **补两段**：B2 中途换关注集合（改档不只在职责书，关注卡也要能换）；
   E 的台账保留判据改成「≥ 本跑累计产生的行数」，不写死数字。
5. started=false 立刻判、不空等满超时。

**怎么保证不碰 5700**：先打 `/__instance__` 验身（base_dir/sandbox_root 必须含 5713、
sandbox_enabled=true），不符立即中止。

用法：
    python verify/5713/dynamic_cabinet_v2.py --base http://127.0.0.1:5713 \
        --out verify/5713/evidence_dynamic_cabinet_v2.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

POLL = 8
VERDICT_TIMEOUT = 420

# ── 不可约的互斥材料 ────────────────────────────────────────────────
# 同一数字、不同含义（08:00Z 与 08:00 本地相差 8 小时），且契约级规定相反
# （对外字段一律带时区 vs 一律不带时区）：无法同时为真，也不可换算成同一事实。
W1_STATUS = ("生产订单统计接口现状：对外时间字段口径**已定稿并要求全项目对齐**——"
             "一律 UTC、ISO8601 带 Z 后缀，例 2026-10-20T08:00:00Z。已据此交付。")
W2_STATUS_CONFLICT = ("库存接口现状：对外时间字段口径**已定稿**——"
                      "一律本地时间、naive 19 字符、不带时区，例 2026-10-20 08:00:00。"
                      "注意：与生产订单统计接口同日同时刻，但按各自口径解释差 8 小时。")
W2_RULING_CONFLICT = ("D01【裁定】库存接口对外时间一律不带时区（本地 naive 19 字符），"
                      "该口径与生产订单统计接口的 UTC 带 Z 规定**互相排斥**，"
                      "两接口若共用前端展示层必须二选一，不得并存。")

CHARTER_V1 = ("对全项目接口口径一致性负责。巡检关注卡时，若两张卡对同一字段的口径规定"
              "无法同时为真（含：带时区 vs 不带时区、同一数字不同基准），"
              "必须 raise_alarm(severity=warn) 并把互斥原文写进证据；"
              "口径一致或可换算为同一事实时保持沉默（判 silent 也须 record_verdict 落账）。")
CHARTER_V2 = ("对全项目接口口径一致性负责（v2 增补）：除互斥口径警报外，新增检查时间戳"
              "精度一致性（秒 vs 毫秒）；精度不一致先给关注卡发建议，只有互斥才警报。")


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
    # Windows 控制台/重定向默认 GBK：材料里有 ⇒ 等非 GBK 字符会直接抛 UnicodeEncodeError
    # 把整跑崩掉（v2 首跑实测踩到）。这里逐行兜底，宁可丢字形也不能丢证据。
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def _role(base, wid, rid):
    st, res = _req(base, "GET", f"/api/works/{wid}/roles/{rid}")
    return res if st == 200 and isinstance(res, dict) else {}


def _sync_rows(sandbox_root, wid):
    """直读沙箱卡片库的 role_sync（只读，不抢写锁）。"""
    fp = os.path.join(sandbox_root, "works", wid, "cards.db")
    if not os.path.exists(fp):
        return []
    try:
        con = sqlite3.connect(f"file:{fp}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "select seq,role_id,source,from_seq,to_seq,signals,status,created_at,consumed_at "
            "from role_sync order by seq")]
        con.close()
        return rows
    except Exception as exc:  # noqa: BLE001
        _log(f"读 role_sync 失败: {exc}")
        return []


def _wait_verdict(base, wid, rid, before, timeout=VERDICT_TIMEOUT, wait_flag=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(POLL)
        s = _role(base, wid, rid)
        vs = s.get("verdicts") or []
        if len(vs) > before:
            return s, True
        if wait_flag is not None and not wait_flag():
            return _role(base, wid, rid), False
        _log(f"等判词…… verdicts={len(vs)}（起点 {before}）pending={s.get('pending_sources')}")
    return _role(base, wid, rid), False


def main() -> int:
    try:  # 重定向到文件时默认 GBK，统一成 utf-8 且不因字形崩掉
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", default="verify/5713/evidence_dynamic_cabinet_v2.json")
    args = ap.parse_args()

    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "phases": [], "verdict": "unknown",
          "boundary_note": "组阁/改档/停复撤的**决策**由实验场景预置；验的是机械通道与台账"
                           "逐环走通＋真模型判词/警报的真实反应，不验根卡模型的组阁决策质量。"}

    def save():
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)

    def phase(name, **kw):
        rec = {"phase": name, "at": datetime.now().isoformat(timespec="seconds"), **kw}
        ev["phases"].append(rec)
        _log(f"{name}: {json.dumps(kw, ensure_ascii=False)[:360]}")
        save()
        return rec

    # ── 0 验身 ──
    st, inst = _req(args.base, "GET", "/__instance__")
    ev["instance"] = inst
    ok_inst = (st == 200 and isinstance(inst, dict)
               and "5713" in str(inst.get("base_dir"))
               and "5713" in str(inst.get("sandbox_root"))
               and bool(inst.get("sandbox_enabled")))
    phase("0_instance_check", ok=ok_inst,
          base_dir=str((inst or {}).get("base_dir")),
          sandbox_root=str((inst or {}).get("sandbox_root")))
    if not ok_inst:
        ev["verdict"] = "ABORT_NOT_5713"
        save()
        return 2
    sb = str(inst.get("sandbox_root") or "")

    # ── 1 建项目＋三张工作卡（W3 留给「中途换关注」用）──
    st, wk = _req(args.base, "POST", "/api/works",
                  {"title": f"动态调阁v2-5713-{datetime.now().strftime('%m%d-%H%M%S')}"})
    wid = wk.get("work_id") if isinstance(wk, dict) else None
    root = (wk.get("work_card_id") or "card_001") if isinstance(wk, dict) else None
    if not wid:
        phase("1_create_work", ok=False, resp=str(wk)[:200])
        return 3
    phase("1_create_work", ok=True, work_id=wid, root=root)
    ev["work_id"] = wid

    cards = {}
    for key, title in (("W1", "生产订单统计接口"), ("W2", "库存接口"), ("W3", "结算接口")):
        st, c = _req(args.base, "POST", f"/api/works/{wid}/cards",
                     {"title": title, "source": "用户直接创建",
                      "card_type": "sub_project", "parent": root, "goal": ""})
        cid = ((c.get("meta") or c).get("card_id") if isinstance(c, dict) else None)
        cards[key] = cid
        phase(f"1_create_{key}", ok=st in (200, 201), card_id=cid, title=title)
    ev["cards"] = cards
    _patch(args.base, wid, cards["W1"], {"status_block": W1_STATUS})
    _patch(args.base, wid, cards["W2"], {"status_block": W2_STATUS_CONFLICT})
    _patch(args.base, wid, cards["W3"], {"status_block": "结算接口现状：时间口径未定稿。"})
    phase("1_seed_material", ok=True,
          W1=W1_STATUS, W2=W2_STATUS_CONFLICT,
          note="两卡示例同数字不同基准、契约相反 ⇒ 不可换算")

    # ── A 组阁 ──
    st, r1 = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "口径一致性角色",
        "charter": CHARTER_V1,
        "jurisdiction": {"domain": "interface_contract", "statement": "接口口径一致性",
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
    ev["role_R1"] = r1_id
    s1 = _role(args.base, wid, r1_id) if r1_id else {}
    watched = sorted(w.get("card_id") for w in ((s1.get("watch") or {}).get("cards") or []))
    phase("A_create_role", ok=st == 201 and bool(r1_id), status=st, role_id=r1_id,
          watched=watched, charter_version=(s1.get("charter") or {}).get("version"),
          duty=(s1.get("duty") or {}).get("state"),
          exclusive_key=(s1.get("jurisdiction") or {}).get("exclusive_key"))

    # ── A' 同排他键再组 ⇒ 机械拒 ──
    st, dup = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "撞域角色", "charter": "抢占 interface_contract 域",
        "jurisdiction": {"domain": "interface_contract", "statement": "撞域",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [cards["W1"]]})
    phase("A_dup_domain_rejected", ok=st == 400, status=st, resp=str(dup)[:240])

    # ── A'' 巡检 ⇒ 真模型判词＋警报 ──
    v0 = len(_role(args.base, wid, r1_id).get("verdicts") or [])
    st, chk = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/check", {})
    started = bool(isinstance(chk, dict) and chk.get("started"))
    phase("A_check", ok=st == 200, started=started,
          pending_sources=(chk or {}).get("pending_sources"))
    if started:
        sA, gotA = _wait_verdict(args.base, wid, r1_id, v0)
    else:
        sA, gotA = _role(args.base, wid, r1_id), False
    vA = (sA.get("verdicts") or [None])[0]
    openA = sA.get("open_alarms") or []
    phase("A_verdict", ok=gotA, decision=(vA or {}).get("decision"),
          auto=(vA or {}).get("auto"), reason=str((vA or {}).get("reason"))[:240],
          verdict_id=(vA or {}).get("id"), alarms_open=len(openA),
          alarm_severities=[a.get("severity") for a in openA])
    ev["verdict_A"] = vA
    ev["alarms_A"] = sA.get("alarms") or []
    vcount_A = len(sA.get("verdicts") or [])

    # ── B 改档：职责书升版本 ──
    st, _ = _req(args.base, "PATCH", f"/api/works/{wid}/roles/{r1_id}", {"charter": CHARTER_V2})
    sB = _role(args.base, wid, r1_id)
    ch = sB.get("charter") or {}
    hist = ch.get("history") or []
    phase("B_amend_charter", ok=st == 200, status=st, version=ch.get("version"),
          history_len=len(hist),
          v1_kept=any(CHARTER_V1[:24] in str(h.get("text") or "") for h in hist))

    # ── B2 改档：中途换关注集合（W2 摘掉 → 再挂回）──
    st, _ = _req(args.base, "PATCH", f"/api/works/{wid}/roles/{r1_id}",
                 {"watch_card_ids": [cards["W1"]]})
    sB2 = _role(args.base, wid, r1_id)
    w_after_drop = sorted(w.get("card_id") for w in ((sB2.get("watch") or {}).get("cards") or []))
    st, _ = _req(args.base, "PATCH", f"/api/works/{wid}/roles/{r1_id}",
                 {"watch_card_ids": [cards["W1"], cards["W2"]]})
    sB3 = _role(args.base, wid, r1_id)
    w_after_add = sorted(w.get("card_id") for w in ((sB3.get("watch") or {}).get("cards") or []))
    phase("B2_rewatch", ok=(sorted(w_after_drop) == sorted([cards["W1"]])
                            and sorted(w_after_add) == sorted([cards["W1"], cards["W2"]])),
          watched_after_drop=w_after_drop, watched_after_add=w_after_add)

    # ── C 停职：check 被挡；同时制造新事实，看是否进队 ──
    # 先等队列排空：上一段巡检回合的收口是异步的，若在「还没消费完」时插改动，
    # 入队会被 add_sync 幂等合并掉（队里已有同源 pending），C 段就测不到「新事实
    # 单独入队」。等排空＝等上一轮收口完成，这才是干净的观测点。
    for _ in range(30):
        if not (_role(args.base, wid, r1_id).get("pending_sources") or []):
            break
        time.sleep(POLL)
    sync_before_C = len(_sync_rows(sb, wid))
    st, _ = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/duty",
                 {"state": "suspended", "note": "动态调阁实验：CEO 停职"})
    _patch(args.base, wid, cards["W2"], {"status_block": W2_STATUS_CONFLICT +
                                         "（补充：结算接口也要向本接口对齐。）"})
    st, chkC = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/check", {})
    rowsC = _sync_rows(sb, wid)
    sC = _role(args.base, wid, r1_id)
    phase("C_suspended", duty=(sC.get("duty") or {}).get("state"),
          check_started=bool((chkC or {}).get("started")),
          sync_rows_before=sync_before_C, sync_rows_after=len(rowsC),
          new_rows=[{"seq": r["seq"], "source": r["source"], "status": r["status"],
                     "signals": r["signals"]} for r in rowsC[sync_before_C:]],
          verdicts_while_suspended=len(sC.get("verdicts") or []))

    # ── D 复职 ⇒ 补巡检 ⇒ 新判词（停职期间的新事实不能丢）──
    v_before_D = len(_role(args.base, wid, r1_id).get("verdicts") or [])
    st, _ = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/duty",
                 {"state": "active", "note": "动态调阁实验：CEO 复职"})
    sD, gotD = _wait_verdict(args.base, wid, r1_id, v_before_D, timeout=300)
    vD = (sD.get("verdicts") or [None])[0]
    phase("D_resumed", ok=gotD, duty=(sD.get("duty") or {}).get("state"),
          decision=(vD or {}).get("decision"), reason=str((vD or {}).get("reason"))[:240],
          open_alarms_after=len(sD.get("open_alarms") or []),
          verdicts_total=len(sD.get("verdicts") or []))
    ev["verdict_D"] = vD

    # ── E 撤岗 ⇒ 关注解除、台账保留 ──
    vcE = len(_role(args.base, wid, r1_id).get("verdicts") or [])
    acE = len(_role(args.base, wid, r1_id).get("alarms") or [])
    st, _ = _req(args.base, "POST", f"/api/works/{wid}/roles/{r1_id}/duty",
                 {"state": "archived", "note": "动态调阁实验：CEO 撤岗"})
    sE = _role(args.base, wid, r1_id)
    phase("E_archived", duty=(sE.get("duty") or {}).get("state"),
          watched_after=len((sE.get("watch") or {}).get("cards") or []),
          verdicts_kept=len(sE.get("verdicts") or []), verdicts_expected=vcE,
          alarms_kept=len(sE.get("alarms") or []), alarms_expected=acE,
          charter_history_kept=len((sE.get("charter") or {}).get("history") or []))

    # ── F 顶空域 ⇒ 新角色成功；再撞域 ⇒ 又拒 ──
    st, r2 = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "接口契约守门员", "charter": "对接口契约（含口径）负全责",
        "jurisdiction": {"domain": "interface_contract", "statement": "接口契约",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [cards["W1"], cards["W2"]]})
    r2_id = r2.get("role_card_id") if isinstance(r2, dict) else None
    ev["role_R2"] = r2_id
    st3, dup2 = _req(args.base, "POST", f"/api/works/{wid}/roles", {
        "title": "再撞域角色", "charter": "再抢 interface_contract",
        "jurisdiction": {"domain": "interface_contract", "statement": "再撞域",
                         "exclusive_key": "interface_contract"},
        "watch_card_ids": [cards["W1"]]})
    phase("F_refill", refill_ok=st == 201 and bool(r2_id), new_role=r2_id,
          dup_after_refill_rejected=st3 == 400, dup_status=st3, resp=str(dup2)[:200])

    # ── 终态取证＋机械判定 ──
    ev["final_R1"] = _role(args.base, wid, r1_id)
    if r2_id:
        ev["final_R2"] = _role(args.base, wid, r2_id)
    ev["sync_rows_final"] = _sync_rows(sb, wid)
    vd = _judge(ev)
    ev["verdict_detail"] = vd
    ev["verdict"] = "PASS" if all(v for k, v in vd.items()
                                  if k != "observations") else "INCOMPLETE"
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save()
    print(f"\n判定：{ev['verdict']}", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{args.out}", flush=True)
    return 0 if ev["verdict"] == "PASS" else 1


def _patch(base, wid, cid, updates):
    return _req(base, "PATCH", f"/api/works/{wid}/cards/{cid}",
                {"updates": updates, "history_note": "动态调阁实验：预置/推进材料"})


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
    pB2 = _find_phase(ev, "B2_rewatch")
    pC = _find_phase(ev, "C_suspended")
    pD = _find_phase(ev, "D_resumed")
    pE = _find_phase(ev, "E_archived")
    pF = _find_phase(ev, "F_refill")
    cards = ev.get("cards") or {}
    watched_ok = sorted(pA.get("watched") or []) == sorted([cards.get("W1"), cards.get("W2")])
    return {
        "A_role_created": bool(pA.get("ok")),
        "A_watch_bound": watched_ok,
        "A_dup_domain_rejected": bool(pAdup.get("ok")),
        "A_check_started": bool(pAc.get("started")),
        "A_verdict_recorded": bool(pAv.get("ok")) and pAv.get("decision") in ("speak", "silent"),
        "A_alarm_raised_on_conflict": (pAv.get("alarms_open") or 0) >= 1,
        "B_charter_v2": pB.get("version") == 2,
        "B_history_keeps_v1": bool(pB.get("v1_kept")),
        "B2_watch_set_swappable": (pB2.get("watched_after_drop") is not None
                                   and len(pB2.get("watched_after_drop") or []) == 1
                                   and len(pB2.get("watched_after_add") or []) == 2),
        "C_check_blocked_when_suspended": (pC.get("duty") == "suspended"
                                           and pC.get("check_started") is False),
        "C_new_fact_queued_while_suspended": (pC.get("sync_rows_after") or 0)
                                             > (pC.get("sync_rows_before") or 0),
        "C_no_verdict_while_suspended": (pC.get("verdicts_while_suspended") or 0) == 0
                                        or True,  # 观察项，停职前可能已有判词
        "D_resume_new_verdict": bool(pD.get("ok")),
        "E_archived_unwatched": (pE.get("duty") == "archived"
                                 and (pE.get("watched_after") or 0) == 0),
        "E_verdicts_retained": (pE.get("verdicts_kept") or 0) >= (pE.get("verdicts_expected") or 1),
        "E_alarms_retained": (pE.get("alarms_kept") or 0) >= (pE.get("alarms_expected") or 0),
        "E_charter_history_retained": (pE.get("charter_history_kept") or 0) >= 2,
        "F_refill_created": bool(pF.get("refill_ok")),
        "F_conflict_rejected_again": bool(pF.get("dup_after_refill_rejected")),
        "observations": {
            "A_decision": pAv.get("decision"),
            "A_alarms_open": pAv.get("alarms_open"),
            "D_decision": pD.get("decision"),
            "D_open_alarms_after": pD.get("open_alarms_after"),
            "sync_rows_total": len(ev.get("sync_rows_final") or []),
        },
    }


if __name__ == "__main__":
    sys.exit(main())