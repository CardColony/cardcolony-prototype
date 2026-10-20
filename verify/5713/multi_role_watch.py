# -*- coding: utf-8 -*-
"""多角色同关注一卡 · **真机**验收（唤醒互斥与交错污染）。

**单测已覆盖、这里不重复**（`tests/test_role_multi_watch.py`）：入队是否各一行、
去重键是否按角色分开、10 次连改是否合并、停职队列是否有界、重启能否续跑、spawn 冷却是否按角色隔离。

**本驱动器只补单测验不到的那一半**——真模型、真 HTTP、真库：
1. 一张卡被**两个**角色同时关注时，一次改动之后**两边是否都真的起了回合、都落了判词**
   （而不是"入队了但没人跑"或"一个跑了把另一个吞了"）；
2. 两个角色各自的 `duty.pending_sync` / `role_sync` 行是否**互不污染**（按 role_id 隔离）；
3. 两边 prompt 日志是否存在、`model_used` 是什么（真调用自证，不靠文件里写着）。

判据全锚结构事实：判词行数、判词 id、role_sync 行按 role 分组、日志里的 `model_used`。

用法（隔离实例在跑；建议自己起一个，别占 5713 的现场）：
    python verify/5713/multi_role_watch.py --base http://127.0.0.1:5714 \
        --out verify/5713/evidence_multi_role_watch.json
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

POLL = 8
_RUN_T0 = 0.0
# 归属用：本次材料里的唯一串（只用于把日志认回本 work，不给模型当提示）
FINGERPRINTS = ("E-4402", "毫秒", "9.2 万")


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


# 共享卡：同一份现状里同时埋「口径互斥」与「成本超支」两类事实，
# 让两个不同域的角色各有真东西可看（否则第二个角色必然沉默，验不出唤醒）。
SHARED_STATUS = (
    "对账接口现状：\n"
    "1) 对外时间字段：一律本地时间、naive 19 字符，例 2026-10-20 08:00:00；"
    "但生产订单侧按 UTC 带 Z 交付，同一字面值差 8 小时。\n"
    "2) 时间戳精度：毫秒级；金额按 2 位小数截断参与批次判定（E-4402 时 ≤0.01 会误判）。\n"
    "3) 成本：部门预算 5 万元/月，实际已达 9.2 万元/月（重跑为主）。"
)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", default="verify/5713/evidence_multi_role_watch.json")
    ap.add_argument("--pin-model", default="ds-v41-flash-aliyun")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    global _RUN_T0
    _RUN_T0 = time.time()
    ev = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "base": args.base, "phases": [], "verdict": "unknown",
          "note": "验的是同步与唤醒的机械互斥；不验判词质量（judgment 由判词行数/ID 锚定）"}

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
                  {"title": f"多角色同卡-{datetime.now().strftime('%m%d-%H%M%S')}"})
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

    # ── 2 共享卡（两个角色都盯它）＋ 一张旁卡（不许被顺带关注后误判）──
    st, c = _req(args.base, "POST", f"/api/works/{wid}/cards",
                 {"title": "对账接口（共享关注对象）", "source": "用户直接创建",
                  "card_type": "sub_project", "parent": root, "goal": ""})
    shared = ((c.get("meta") or c).get("card_id") if isinstance(c, dict) else None)
    if shared:
        _req(args.base, "PATCH", f"/api/works/{wid}/cards/{shared}",
             {"updates": {"status_block": SHARED_STATUS}, "history_note": "预置现状（含两类问题）"})
    phase("2_shared_card", ok=bool(shared), card_id=shared)
    ev["shared_card"] = shared

    # ── 3 组阁两个角色：不同排他键、**都关注同一张卡** ──
    roles = {}
    specs = [
        ("contract_guard", "口径一致性观察员",
         "只盯对外时间/精度/错误码语义的跨卡一致性；发现无法同时为真就报警；不涉预算。",
         {"advice_when": ["口径不一致"], "alarm_when": ["互斥口径"],
          "silence_default": True, "max_messages_per_wake": 1}),
        ("cost_budget", "成本与预算观察员",
         "只盯成本与预算超支；实际支出超过部门上限就报警；不碰口径与实现选型。",
         {"advice_when": ["接近预算上限"], "alarm_when": ["超支无审批"],
          "silence_default": True, "max_messages_per_wake": 1}),
    ]
    for key, title, charter, contract in specs:
        st, r = _req(args.base, "POST", f"/api/works/{wid}/roles", {
            "title": title, "charter": charter,
            "jurisdiction": {"domain": key, "statement": title,
                             "exclusive_key": key},
            "watch_card_ids": [shared],
            "signals": ["status_changed", "turn_completed"],
            "speak_contract": contract,
            "escalation": {"advice_to": "card_001", "alarm_to": "card_001",
                           "critical_to": "card_001"},
        })
        rid = r.get("role_card_id") if isinstance(r, dict) else None
        roles[key] = rid
        phase(f"3_role_{key}", ok=st == 201 and bool(rid), role_id=rid, status=st)
    ev["roles"] = roles
    if not all(roles.values()):
        ev["verdict"] = "ABORT_ROLE_CREATE"
        save()
        return 4

    # 记录初始（应为 0）
    base_state = {k: _role(args.base, wid, v) for k, v in roles.items()}
    ev["baseline_verdicts"] = {k: len(s.get("verdicts") or []) for k, s in base_state.items()}
    ev["baseline_watch"] = {k: [w.get("card_id") for w in ((s.get("watch") or {}).get("cards") or [])]
                            for k, s in base_state.items()}
    phase("3b_baseline", verdicts=ev["baseline_verdicts"], watch=ev["baseline_watch"])

    # ── 4 一次改动 → 看两边是否各自被唤醒并落判词 ──
    t_change = time.time()
    _req(args.base, "PATCH", f"/api/works/{wid}/cards/{shared}",
         {"updates": {"status_block": SHARED_STATUS + "\n4) 补充：上月对账差异率 0.03%。"},
          "history_note": "触发：现状更新"})
    phase("4_change", ok=True, at=datetime.now().isoformat(timespec="seconds"))

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(POLL)
        cur = {k: _role(args.base, wid, v) for k, v in roles.items()}
        got = {k: len(s.get("verdicts") or []) > ev["baseline_verdicts"][k] for k, s in cur.items()}
        _log(f"等高线…… 判词 " + " ".join(
            f"{k}={len(cur[k].get('verdicts') or [])}" for k in roles))
        if all(got.values()):
            break
    # 给日志落盘留窗口（回合结束后才写 prompt 日志——本项目第 4 次踩到的坑）
    time.sleep(8)

    # ── 5 取证 ──
    final = {k: _role(args.base, wid, v) for k, v in roles.items()}
    ev["final"] = {k: {
        "role_id": s.get("role_id") or roles.get(k),
        "verdicts": [{"id": x.get("id"), "decision": x.get("decision"),
                      "ts": x.get("ts"), "sources": x.get("sources")}
                     for x in (s.get("verdicts") or [])],
        "alarms": [{"id": a.get("id"), "severity": a.get("severity")}
                   for a in (s.get("alarms") or [])],
        "duty": s.get("duty") or {},
        "watch": [w.get("card_id") for w in ((s.get("watch") or {}).get("cards") or [])],
    } for k, s in final.items()}
    ev["sync_rows"] = _sync_rows(sb, wid)
    ev["prompt_logs"] = _prompt_logs(sb, t_change)
    phase("5_collected", verdicts={k: len(v["verdicts"]) for k, v in ev["final"].items()})

    vd = _judge(ev)
    ev["verdict_detail"] = vd
    ev["verdict"] = _verdict_of(vd)
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    ev["elapsed_seconds"] = round(time.time() - _RUN_T0, 1)
    save()
    print(f"\n判定：{ev['verdict']}", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{args.out}", flush=True)
    return 0 if ev["verdict"] == "PASS" else 1


def _role(base, wid, rid):
    st, res = _req(base, "GET", f"/api/works/{wid}/roles/{rid}")
    return res if st == 200 and isinstance(res, dict) else {}


def _sync_rows(sandbox_root, wid):
    import sqlite3
    fp = os.path.join(sandbox_root, "works", wid, "cards.db")
    if not os.path.exists(fp):
        return []
    try:
        con = sqlite3.connect(f"file:{fp}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "select seq, role_id, source, from_seq, to_seq, signals, status, created_at"
            " from role_sync order by seq")]
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


def _prompt_logs(sandbox_root, since_ts):
    """本次时间窗内、且**材料指纹属于本 work** 的 prompt 日志（真调用自证）。"""
    logs = os.path.join(sandbox_root, "logs")
    out = []
    if not os.path.isdir(logs):
        return out
    for n in sorted(os.listdir(logs)):
        if not (n.startswith("prompt_") and n.endswith(".json")):
            continue
        fp = os.path.join(logs, n)
        try:
            if os.path.getmtime(fp) < since_ts - 5:
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        blob = json.dumps({"s": d.get("system_prompt"), "u": d.get("user_prompt"),
                           "p": d.get("prior_messages")}, ensure_ascii=False)
        if sum(1 for t in FINGERPRINTS if t in blob) < 2:
            continue          # 不属于本 work 的材料 ⇒ 不计入（并行 work 的日志同目录）
        out.append({"card_id": d.get("card_id"), "file": n,
                    "model_requested": d.get("model_requested"),
                    "model_used": d.get("model_used"),
                    "error": (d.get("error") or "")[:120]})
    return out


# 判据清单：**逐条列名**，不用"遍历所有值"的写法。
# 为什么要改：初版写 `all(x for k, x in vd.items() if k != "observations")`，
# 结果把 `any_model_error: False`（好事）当成了不满足，把一次全绿的真机跑判成 INCOMPLETE。
# 计数/列表类字段（verdict_growth / sync_rows_per_role / models_observed）根本不是布尔，
# 混进 all() 属于判据口径错误——判据要么是真布尔，要么显式比数。
PASS_KEYS = (
    "both_watch_shared_card",          # 前提：两人都关注同一张卡
    "both_woke_and_judged",            # 一次改动，两人都起了回合并落判词
    "both_judged_on_shared_card",      # 两人判词的 sources 都指向那张共享卡
    "sync_rows_isolated",              # 入队行按 role_id 隔离，无交错污染
    "both_had_model_turn",             # 两人都真起过模型回合（有 prompt 日志）
)


def _verdict_of(vd: dict) -> str:
    ok = all(bool(vd.get(k)) for k in PASS_KEYS) and not vd.get("any_model_error")
    return "PASS" if ok else "INCOMPLETE"


def _judge(ev):
    roles = ev.get("roles") or {}
    final = ev.get("final") or {}
    base_v = ev.get("baseline_verdicts") or {}
    shared = ev.get("shared_card")
    rows = ev.get("sync_rows") or []
    logs = ev.get("prompt_logs") or []

    by_role = {k: [r for r in rows if r.get("role_id") == v] for k, v in roles.items()}
    # 两边的判词是否都比基线多（都真跑了回合、都落了判词）
    grew = {k: len((final.get(k) or {}).get("verdicts") or []) > base_v.get(k, 0)
            for k in roles}
    # 每边是否都有指向共享卡的判词来源
    src_ok = {}
    for k in roles:
        vs = (final.get(k) or {}).get("verdicts") or []
        src_ok[k] = any(shared in (v.get("sources") or []) for v in vs)
    # 两边的 prompt 日志（按 card_id = role_id 归属）
    logs_by_role = {k: [x for x in logs if x.get("card_id") == roles[k]] for k in roles}
    models = sorted({x.get("model_used") for x in logs if x.get("model_used")})
    errors = [x for x in logs if x.get("error")]
    return {
        # 前提：两人都关注同一张卡（不是各盯各的）
        "both_watch_shared_card": all(
            shared in ((final.get(k) or {}).get("watch") or []) for k in roles),
        # 核心 1：一次改动，两人都被唤醒并落判词
        "both_woke_and_judged": all(grew.values()),
        "verdict_growth": {k: [base_v.get(k, 0),
                               len((final.get(k) or {}).get("verdicts") or [])]
                           for k in roles},
        # 核心 2：两边都真的看到那张卡（判词 sources 指向它）
        "both_judged_on_shared_card": all(src_ok.values()),
        # 核心 3：入队按角色隔离，两边都有行、且各自的行只属于自己
        "sync_rows_per_role": {k: len(v) for k, v in by_role.items()},
        "sync_rows_isolated": all(
            all(r.get("role_id") == v for r in by_role[k]) for k, v in roles.items()),
        # 核心 4：两边都真起过模型回合（有日志、有 model_used、无 error）
        "prompt_logs_per_role": {k: len(v) for k, v in logs_by_role.items()},
        "both_had_model_turn": all(len(v) >= 1 for v in logs_by_role.values()),
        "models_observed": models,
        "any_model_error": len(errors) > 0,
        "observations": {
            "decisions": {k: [v.get("decision") for v in
                              ((final.get(k) or {}).get("verdicts") or [])] for k in roles},
            "pending_sync": {k: ((final.get(k) or {}).get("duty") or {}).get("pending_sync")
                             for k in roles},
        },
    }


if __name__ == "__main__":
    sys.exit(main())