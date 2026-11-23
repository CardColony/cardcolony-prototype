"""A9 · work_073 ACC-1 候选窗口三臂回放（2026-11-22）。

用法：python scripts/a9_acc1_replay.py [manifest] [--out report.json]
口径：manifest replay_notes——同一时间窗/材料、按事件时间推进、每次只供
该时点可见材料；requirements_missing 不补当前正文；标签/后见更正只供判分。

输出：三臂各自的**本次回放实测**调用计数与首判坐标；缺标签 ⇒ 误报/漏报
标「资料不足」，不臆造判分结论。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT = Path("D:/AI_station/推演agent_data_iso/a9/work_073-20261119-acc1/manifest.json")


def load_events(manifest: dict) -> list[dict]:
    root = Path(manifest_path).parent
    events = []
    for r in manifest["records"]:
        if r.get("type") != "event_slice":
            continue
        p = root / r["local"]
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        if h != r.get("sha256"):
            raise SystemExit(f"sha256 不符: {p}")
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    events.sort(key=lambda e: (e.get("ts") or "", e.get("seq") or 0))
    return events


def visible_at(events: list[dict], ts: str) -> list[dict]:
    return [e for e in events if (e.get("ts") or "") <= ts]


def first_signal(events: list[dict]) -> dict | None:
    """首个已知输入坐标（manifest known_input_coordinate）；按事件时间。"""
    k = {"card_id": "card_790", "seq": 2287}
    for e in events:
        if e.get("card_id") == k["card_id"] and e.get("seq") == k["seq"]:
            return {"ts": e["ts"], "card_id": e["card_id"], "seq": e["seq"],
                    "type": e["type"]}
    return None


class StrategyModel:
    """本次回放的**实际模型调用**记录器（模拟调用，不接真实网关）。

    每次 decide() 计一次调用，记录触发时间、当时可见材料指纹与判断。
    无判分标签 ⇒ 判断只记「观察到信号/未观察到」，不给对错结论。
    """
    def __init__(self, arm: str):
        self.arm = arm
        self.calls: list[dict] = []

    def decide(self, ts: str, visible: list[dict], question: str) -> dict:
        import hashlib as _h
        material_fp = _h.sha256(
            "|".join(f"{e['card_id']}:{e['seq']}" for e in visible).encode()
        ).hexdigest()[:12]
        call = {"arm": self.arm,
                "at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "kind": question,
                "visible_events": len(visible), "material_fp": material_fp,
                "judgement": "资料不足（无标签/判分标准）"}
        self.calls.append(call)
        return call


def arms(events: list[dict], signal: dict | None,
         scenario: dict | None = None) -> list[dict]:
    """同一时钟驱动三臂：事件按时间推进，**到期点由绝对时间**注入。

    [第六轮返工] 修正：①到期回看只看绝对时钟越过 due_at，不看 turn_end；
    ②weak_wake 按 item_id 去重，同事项只唤醒强模型一次。
    """
    from datetime import datetime, timedelta, timezone

    scenario = scenario or {}
    if scenario:
        events = list(events)
        if scenario.get("due_at"):
            events.append({"card_id": "_clock", "seq": -1,
                           "ts": scenario["due_at"],
                           "due_at": scenario["due_at"],
                           "type": "_clock", "data": {}})
        # 弱模型判断提请：按可见材料里是否出现已知输入坐标
        if scenario.get("weak_wake") and signal:
            events.append({"card_id": signal["card_id"],
                           "seq": signal["seq"] + 0.5,
                           "ts": signal["ts"], "type": "weak_signal",
                           "data": {"weak_wake": {
                               "item_id": scenario.get("item_id", "acc1")}}})

    def _ts(e):
        t = str(e.get("ts") or e.get("due_at") or "")
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        except Exception:
            return None

    stamps = [x for x in (_ts(e) for e in events) if x is not None]
    base = min(stamps) if stamps else datetime(2026, 11, 19, 20, 0,
                                               tzinfo=timezone.utc)
    # [第六轮返工] 到期点**只由绝对到期时间**产生（事件 due_at /
    # next_review_at），普通 turn_end 不携带到期 ⇒ 不回看。
    # 空事件场景由 selftest 显式注入 due_at 哨兵事件。
    def _due_ts(e):
        raw = e.get("due_at") or e.get("next_review_at") or ""
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None
    explicit_due = [x for e in events for x in [_due_ts(e)]
                    if x is not None]
    due = explicit_due[0] if explicit_due else None
    clock = sorted(set(stamps + ([due] if due else [])))

    def _vis(t):
        return [e for e in events
                if (_ts(e) is not None and _ts(e) <= t)]

    out = []
    for name in ("strong_every_batch", "review_on_due_only",
                 "weak_observe_plus_review"):
        model = StrategyModel(name)
        reviewed = False
        strong_woken: set[str] = set()
        for t in clock:
            vis = _vis(t)
            if name == "strong_every_batch":
                for e in vis:
                    if (e.get("type") == "round_start"
                            and id(e) not in getattr(model, "_done", set())):
                        model.decide(t, vis, "strong_batch")
                        model._done = getattr(model, "_done", set()) | {id(e)}
            elif name == "review_on_due_only":
                if due is not None and t >= due and not reviewed:
                    model.decide(t, vis, "strong_review")
                    reviewed = True
            else:  # weak_observe_plus_review
                for e in vis:
                    ww = (e.get("data") or {}).get("weak_wake")
                    if not ww or id(e) in getattr(model, "_done", set()):
                        continue
                    model._done = getattr(model, "_done", set()) | {id(e)}
                    model.decide(t, vis, "weak_observe")
                    item = str(ww.get("item_id") or e.get("seq"))
                    if item not in strong_woken:
                        strong_woken.add(item)
                        model.decide(t, vis, "strong_wake")   # 同事项只唤醒一次
                if due is not None and t >= due and not reviewed:
                    model.decide(t, vis, "strong_review")
                    reviewed = True
        calls = model.calls
        out.append({
            "arm": name,
            "calls_total": len(calls),
            "strong_calls": sum(1 for c in calls
                                if c["kind"].startswith("strong")),
            "weak_calls": sum(1 for c in calls
                              if c["kind"] == "weak_observe"),
            "review_calls": sum(1 for c in calls
                                if c["kind"] == "strong_review"),
            "strong_wakes": len(strong_woken),
            "schedule": calls,
            "first_valid_judgement": None,
            "delay_from_first_signal": "资料不足（无判断标签与有效结论事件）",
        })
    return out


def schedule_selftest() -> list[dict]:
    """A9 调度夹具：**直接调用 arms() 回放函数**，不自行计算预期次数。

    三条件：①空事件也到期回看；②普通 turn_end 不触发回看；
    ③三条同事项弱提请只唤醒强模型一次。
    """
    base = "2026-11-19T20:00:00+00:00"
    cases = [
        ("due_with_no_events",
         [{"card_id": "c1", "seq": 1, "ts": base,
           "due_at": base, "type": "_clock", "data": {}}], 1),
        ("plain_turn_end_no_review",
         [{"card_id": "c1", "seq": 1, "ts": base,
           "type": "turn_end", "data": {}}], 0),
        ("weak_wake_dedupe",
         [{"card_id": "c1", "seq": i + 1, "ts": base,
           "type": "turn_end", "data": {"weak_wake": {"item_id": "i1"}}}
          for i in range(3)], 1),
    ]
    # 事件 20:00 / 到期 20:30 ⇒ 回看必须发生在 20:30
    due_at = "2026-11-19T20:30:00+00:00"
    due_case = arms([{"card_id": "c1", "seq": 1, "ts": base,
                      "due_at": due_at, "type": "_clock", "data": {}}], None)
    ra = next(a for a in due_case if a["arm"] == "review_on_due_only")
    due_calls = [c for c in ra["schedule"] if c["kind"] == "strong_review"]
    results = []
    for name, events, expected in cases:
        out = arms(events, None)           # 直接驱动实际回放函数
        review_arm = next(a for a in out if a["arm"] == "review_on_due_only")
        weak_arm = next(a for a in out
                        if a["arm"] == "weak_observe_plus_review")
        got = (review_arm["review_calls"] if name != "weak_wake_dedupe"
               else weak_arm.get("strong_wakes",
                                 sum(1 for c in weak_arm["schedule"]
                                     if c["kind"] == "strong_wake")))
        results.append({"case": name, "expected": expected, "got": got,
                        "pass": got == expected,
                        "arms_scheduled": [a["calls_total"] for a in out]})
    results.append({"case": "review_at_due_not_event_time",
                    "event_ts": base, "due_at": due_at,
                    "review_at": due_calls[0]["at"] if due_calls else None,
                    "pass": bool(due_calls)
                    and due_calls[0]["at"].startswith("2026-11-19T20:30")})
    # 场景三臂：同一材料/到期/弱提请 ⇒ 三组都调度，弱唤醒恰好一次
    sc_events = [{"card_id": "c1", "seq": 1, "ts": base,
                  "type": "round_start", "data": {}},
                 {"card_id": "c1", "seq": 2, "ts": due_at,
                  "type": "_clock", "data": {}}]
    sc = {"due_at": due_at, "weak_wake": True, "item_id": "acc1"}
    sc_out = arms(sc_events,
                  {"card_id": "c1", "seq": 2, "ts": base}, sc)
    nonzero = all(a["calls_total"] > 0 for a in sc_out)
    weak_arm = next(a for a in sc_out
                    if a["arm"] == "weak_observe_plus_review")
    results.append({"case": "three_arms_all_scheduled",
                    "totals": [a["calls_total"] for a in sc_out],
                    "weak_wakes": weak_arm.get("strong_wakes"),
                    "pass": nonzero and weak_arm.get("strong_wakes") == 1})
    return results


def main() -> None:
    global manifest_path
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?", default=str(DEFAULT))
    ap.add_argument("--out", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scenario", default="",
                    help="JSON: {due_at, weak_wake, item_id}；同一时钟跑齐三臂")
    a = ap.parse_args()
    if a.selftest:
        print(json.dumps(schedule_selftest(), ensure_ascii=False, indent=2))
        return
    sc = json.loads(a.scenario) if a.scenario else None
    manifest_path = Path(a.manifest)
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    events = load_events(m)
    sig = first_signal(events)
    reqs = [{"card_id": r["card_id"], "revision": r.get("revision"),
             "local": r.get("local")} for r in m["records"]
            if r.get("type") == "requirements_revision"]
    missing = [r["card_id"] for r in m["records"]
               if r.get("type") == "requirements_missing"]
    report = {
        "window": {"start": m["start_inclusive"], "end": m["end_exclusive"]},
        "events_total": len(events),
        "requirements": reqs,
        "requirements_missing": missing,
        "known_input": sig,
        "arms": arms(events, sig, sc),
        "scenario": sc,
        "labels": "资料不足（窄副本无样本标签/留出样本/判分标准）",
        "usage": "资料不足（事件无 usage/token 字段；本次为策略动作数，非 token 计量）",
        "verdict": "资料不足：无法得出误报/漏报与首次有效判断延迟的对照结论",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"written: {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
