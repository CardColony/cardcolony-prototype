# -*- coding: utf-8 -*-
"""通道试走（5713）· 真机实测驱动器。

**测什么**：一个 agent 能否**自主**走完「发现另一张卡 → 建通信线 → 发消息 → 收到应答」。
不是单测、不是打桩——全程打真 HTTP，模型是真模型，落库真落库。

**怎么保证不碰 5700 生产**：
  · 跑的是 `.worktrees/5713` 这份代码（分支 5713）；
  · 实例由 `ECO_TEST_SANDBOX=1` + `ECO_SANDBOX_ROOT=<5713_sandbox>` 启动，
    全部运行时根（works/ 卡库/ 会话/ 快照/ 日志）被 `run_test_instance_5701.py::
    redirect_roots()` 搬进独立沙箱根；
  · 首次请求先打 `/__instance__`，核对 `base_dir` 与 `sandbox_root` 都是 5713 的，
    否则立刻中止（进程身份三验里的一验）。

**角色设计**（为什么这么分）：
  · 测卡A＝**发起方**，带 goal 的自动模式卡 ⇒ 建卡即自动开跑，自主完成前四步；
  · 测卡B＝**应答方**，`auto_mode` 从父卡继承（＝全开权限、有发信工具）但**不给
    goal** ⇒ 它不会自己空转跑轮次，只在 A 的信到达时被 K3 调起，由**它自己的模型
    回合**作答。这样「应答」是 B 自主发的，不是 S4 兜底代答。

用法（先起实例，再跑本脚本；用法见 verify/5713/README.md）：
    python verify/5713/channel_walk.py --base http://127.0.0.1:5713 --out verify/5713/evidence_channel_walk.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

TIMEOUT_DEFAULT = 900          # 整条链路最长等 15 分钟（真模型多轮，留够）
POLL = 10

# ── 发起方 A 的目标（原样进 goal，模型据此自主行动）────────────────────────
GOAL_A = """通道试走（你是测卡A＝发起方）。按 1→4 走完就收口；不许建卡、不许删卡、不许碰文件。
1) 发现：在上下文「卡片清单」里找到标题含「测卡B」的那张卡，记下它的 card_id（以清单为准，不要猜）。
2) 建线：调 link_cards(target_id=那张测卡B的 card_id, type=关联, note=通道试走：A→B)。
3) 发问：调 send_card_message(target_card_id=那张测卡B的 card_id, msg_type=msg_ask,
   content=「通道试走提问：请用一句话回答——你自己这张卡的标题是什么？回答里请带上暗号 5713。」)
4) 收应答：你发的信被回答后你会被自动唤醒。用 read_inbox(direction="incoming") 找到
   reply_to 指向你那条询问的 msg_answer，读出它回答的内容。
5) 留证：调 update_status，把「通道试走完成」＋测卡B的 card_id＋你的询问 msg_id＋
   对方应答 msg_id＋对方回答的原文，一起写进你的现状块。
做完调 update_goal(complete)。
若某一步被拒（denied 等），把被拒原文如实写进现状块再 complete，不要反复重试同一调用。"""


def _req(base: str, method: str, path: str, body: dict | None = None,
         timeout: int = 30):
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


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def _status_of(base: str, wid: str, cid: str) -> str:
    """取卡的「现状」块原文（read_card 契约：{meta, body:{mission,status_block,...}}）。"""
    st, res = _req(base, "GET", f"/api/works/{wid}/cards/{cid}")
    if st != 200 or not isinstance(res, dict):
        return ""
    return str((res.get("body") or {}).get("status_block") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5713")
    ap.add_argument("--out", default="verify/5713/evidence_channel_walk.json")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_DEFAULT)
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    ev: dict = {"started_at": datetime.now().isoformat(timespec="seconds"),
                "base": args.base, "steps": [], "verdict": "unknown"}

    def step(name: str, **kw) -> None:
        rec = {"step": name, "at": datetime.now().isoformat(timespec="seconds"), **kw}
        ev["steps"].append(rec)
        _log(f"{name}: {json.dumps(kw, ensure_ascii=False)[:300]}")

    # ── 0 进程身份三验：确认落在 5713 的隔离实例上 ────────────────────────
    st, inst = _req(args.base, "GET", "/__instance__")
    ev["instance"] = inst
    if st != 200 or not isinstance(inst, dict):
        step("instance_check", ok=False, status=st, resp=str(inst)[:300])
        print("实例未就绪，中止", flush=True)
        return 2
    ok_inst = (inst.get("instance") == "test-instance"
               and "5713" in str(inst.get("base_dir"))
               and bool(inst.get("sandbox_enabled")))
    step("instance_check", ok=ok_inst, base_dir=inst.get("base_dir"),
         sandbox_root=inst.get("sandbox_root"), pid=inst.get("pid"))
    if not ok_inst:
        print("实例身份不符（不是 5713 隔离实例），中止", flush=True)
        return 2

    # ── 1 建项目 ─────────────────────────────────────────────────────────
    title = args.title or f"通道试走-5713-{datetime.now().strftime('%m%d-%H%M%S')}"
    st, wk = _req(args.base, "POST", "/api/works", {"title": title})
    ev["work"] = wk
    if st not in (200, 201) or not isinstance(wk, dict) or "work_id" not in wk:
        step("create_work", ok=False, status=st, resp=str(wk)[:300])
        return 3
    wid = wk["work_id"]
    root = wk.get("work_card_id") or "card_001"
    step("create_work", ok=True, work_id=wid, root_card=root)

    # ── 2 根卡开自动模式（子卡据此继承 auto_mode → 全开权限、有发信工具）──
    st, pr = _req(args.base, "PATCH", f"/api/works/{wid}/cards/{root}",
                  {"updates": {"auto_mode": 1},
                   "history_note": "通道试走：根卡开自动模式，子卡继承"})
    step("patch_root_auto", ok=st == 200, status=st, resp=str(pr)[:200])
    if st != 200:
        return 4

    # ── 3 先建应答方 B（无 goal ⇒ 不空转，等信到达才被调起）──────────────
    st, cb = _req(args.base, "POST", f"/api/works/{wid}/cards",
                  {"title": "测卡B-通道试走应答方", "source": "用户直接创建",
                   "card_type": "sub_project", "parent": root, "goal": ""})
    if st not in (200, 201):
        step("create_card_B", ok=False, status=st, resp=str(cb)[:300])
        return 5
    bid = (cb.get("meta") or cb).get("card_id")
    step("create_card_B", ok=True, card_id=bid,
         auto_mode=(cb.get("meta") or cb).get("auto_mode"),
         goal=(cb.get("meta") or cb).get("goal"))

    # ── 4 再建发起方 A（带 goal ⇒ 建成即自动开跑）────────────────────────
    st, ca = _req(args.base, "POST", f"/api/works/{wid}/cards",
                  {"title": "测卡A-通道试走发起方", "source": "用户直接创建",
                   "card_type": "sub_project", "parent": root, "goal": GOAL_A})
    if st not in (200, 201):
        step("create_card_A", ok=False, status=st, resp=str(ca)[:300])
        return 6
    aid = (ca.get("meta") or ca).get("card_id")
    step("create_card_A", ok=True, card_id=aid, target_B=bid,
         auto_mode=(ca.get("meta") or ca).get("auto_mode"),
         goal_set=bool((ca.get("meta") or ca).get("goal")))
    ev["cards"] = {"A": aid, "B": bid, "root": root}

    # ── 5 等链路自己走完（A 被唤醒→建线→发问→B 被调起→B 应答→A 再被唤醒）──
    deadline = time.time() + args.timeout
    done = False
    while time.time() < deadline:
        time.sleep(POLL)
        a_status = _status_of(args.base, wid, aid)
        st_nav, nav = _req(args.base, "GET", f"/api/works/{wid}/card-messages?limit=200")
        msgs = (nav or {}).get("items", []) if isinstance(nav, dict) else []
        kinds = sorted({str(m.get("type") or "") for m in msgs})
        _log(f"等链路…… A现状 {len(a_status)} 字；卡间消息 {len(msgs)} 条 {kinds}")
        if "通道试走完成" in a_status:
            done = True
            break
        # A 已收口（complete）也算这一轮跑完，交给人看证据
        st_meta, meta = _req(args.base, "GET", f"/api/works/{wid}/cards/{aid}")
        if isinstance(meta, dict):
            m = meta.get("meta") or meta
            if str(m.get("goal_state") or "") in ("complete", "blocked"):
                _log(f"A 目标态已停：{m.get('goal_state')}")
                done = True
                break
    ev["waited_seconds"] = round(args.timeout - max(0, deadline - time.time()), 1)

    # ── 6 收证据（照抄，不做解释）────────────────────────────────────────
    st, canvas = _req(args.base, "GET", f"/api/works/{wid}/canvas")
    ev["canvas"] = canvas
    st, msgs = _req(args.base, "GET", f"/api/works/{wid}/card-messages?limit=200")
    ev["card_messages"] = msgs
    ev["status_A"] = _status_of(args.base, wid, aid)
    ev["status_B"] = _status_of(args.base, wid, bid)
    st, ma = _req(args.base, "GET", f"/api/works/{wid}/cards/{aid}")
    st, mb = _req(args.base, "GET", f"/api/works/{wid}/cards/{bid}")
    ev["meta_A"] = (ma.get("meta") if isinstance(ma, dict) else ma)
    ev["meta_B"] = (mb.get("meta") if isinstance(mb, dict) else mb)

    # ── 7 事件流原文（取 reply_to：端点不下发该字段，而"B 答的是 A 那条信"正是本试走要证的一环）
    sandbox_root = str((ev.get("instance") or {}).get("sandbox_root") or "")
    ev["events_A"] = _read_events(sandbox_root, wid, aid)
    ev["events_B"] = _read_events(sandbox_root, wid, bid)

    # ── 8 机械判定（只判"有没有"，不下价值判断）──────────────────────────
    ev["verdict_detail"] = _judge(ev, aid, bid)
    vd = ev["verdict_detail"]
    ev["verdict"] = "PASS" if all(v for k, v in vd.items() if k != "counts") else "INCOMPLETE"
    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    print(f"\n判定：{ev['verdict']}", flush=True)
    print(json.dumps(vd, ensure_ascii=False, indent=2), flush=True)
    print(f"证据：{args.out}", flush=True)
    return 0 if ev["verdict"] == "PASS" else 1


def _judge(ev: dict, aid: str, bid: str) -> dict:
    """从证据里机械判四步有没有发生。

    字段名对齐 ``/card-messages`` 端点真实形状：``{id, from_card, to_card,
    msg_type, type_display, summary, ts}``。首版按 ``type/from/to`` 取，取到全空，
    把一条**真跑通**的链路误判成 INCOMPLETE（5713 首跑实测踩到）——此处按原字段取。
    端点不下发 ``reply_to``，故"B 答的是 A 那条信"的配对证据从事件流原文（头行）取。
    """
    def _links(m: dict | None) -> list:
        return list((m or {}).get("links") or [])

    items = list(((ev.get("card_messages") or {}).get("items") or []))
    ask = [m for m in items if m.get("msg_type") == "msg_ask"
           and m.get("from_card") == aid and m.get("to_card") == bid]
    ans = [m for m in items if m.get("msg_type") == "msg_answer"
           and m.get("from_card") == bid and m.get("to_card") == aid]

    # 应答帧的**头行**落在收件方（A）的事件流里：`from=<B> reply_to=<A 的询问 id>`。
    # 故两侧事件流合起来看（只看 B 侧取不到 reply_to——5713 首跑实测踩到）。
    blob = "\n".join(str(x) for x in
                     (list(ev.get("events_A") or []) + list(ev.get("events_B") or [])))
    ask_id = str(ask[0].get("id") if ask else "")
    reply_ok = (bool(ask_id)
                and f"from={bid} reply_to={ask_id}" in blob)

    return {
        "A_found_B": bool(bid),
        "A_built_link_to_B": any(str(l.get("target")) == bid for l in _links(ev.get("meta_A"))),
        "A_sent_ask": len(ask) > 0,
        "B_answered": len(ans) > 0,
        # B 的应答是 B 自己发的（不是 S4 兜底代答）：B 自己的现状块里写了这条 msg_answer
        "B_self_answered_wrote_status": "msg_answer" in (ev.get("status_B") or ""),
        "answer_reply_to_matches_ask": reply_ok,
        "A_wrote_evidence_in_status": "通道试走完成" in (ev.get("status_A") or ""),
        "counts": {"ask": len(ask), "answer": len(ans), "all_items": len(items)},
    }


def _read_events(sandbox_root: str, wid: str, cid: str) -> list:
    """读某卡事件流原文（沙箱盘上 ``works/<wid>/events/<cid>.jsonl`` 的 msg_* 帧）。

    只为取 ``reply_to``：端点 /card-messages 不下发该字段，而"B 答的是 A 那条信"
    正是本试走要证的那一环，必须落到原文而非推断。
    """
    import os

    if not sandbox_root:
        return []
    fp = os.path.join(sandbox_root, "works", wid, "events", f"{cid}.jsonl")
    if not os.path.exists(fp):
        return []
    out: list = []
    with open(fp, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = str(e.get("type") or "")
            if t.startswith("msg_") or t == "card_message":
                out.append(e.get("payload") or e.get("data") or e)
    return out


if __name__ == "__main__":
    sys.exit(main())
