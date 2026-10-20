# -*- coding: utf-8 -*-
"""5713 涌现实验场景台：一条命令起场景，一条命令拉观测与机械打分。

分工（CEO 2026-10-20 裁定）
--------------------------
- **内容归 CEO**：节点持什么使命、相互依赖怎么切、观测什么、何时收口 —— 全部写在
  一份 ``*.spec.json`` 里，本工具**只吃它、不造它**。工具里不硬编码任何节点使命，
  因为「替节点定使命」等于替 CEO 定方向。
- **接口归环境 owner（本卡）**：起场景（建 work / 节点 / 角色）、拉观测面、机械打分。

为什么要有这个工具
------------------
涌现实验的结论最容易死在两件事上，而这个工具就是防这两件的：

1. **仪器坏了，把「测不到」读成「不存在」**。模型配额静默撞上限、角色走 default tier、
   节点跑在旧口径上发不出信 —— 任何一种都会让实验看起来「什么也没发生」，而实际上是
   **观测通道断了**。所以观测面必须把「没尝试」和「尝试了但被拒」分开报。
2. **尺子是关键词探针**。在自由文本里找 ASCII 字面量，模型换个措辞就误报
   （本仓实测三跑误报两次）。所以打分一律用**事实锚点**：卡面 links、卡间信端点字段、
   事件流原文里的 ``reply_to`` 配对。

用法
----
    # 1) 起场景（spec 由 CEO 写）
    python verify/5713/exp_scenario.py up --spec my_exp.spec.json

    # 2) 拉观测与打分（可反复跑，只读）
    python verify/5713/exp_scenario.py observe --work work_027

    # 3) 起完立刻看一眼有哪些卡号
    python verify/5713/exp_scenario.py up --spec my_exp.spec.json --no-wait

spec 格式（你看得懂就能改，不需要动代码）
------------------------------------------
{
  "work_title": "涌现实验-第一轮",
  "nodes": [
    {"key": "A", "title": "节点A", "goal": "……你写的使命……"},
    {"key": "B", "title": "节点B", "goal": "……"},
    {"key": "C", "title": "节点C", "goal": "……"}
  ],
  "role": {                                  // 可选；不要观测角色就整段删掉
    "title": "观测员",
    "charter": "……职责书……",
    "jurisdiction": {"domain": "x", "statement": "y", "exclusive_key": "x"},
    "watch": ["A", "B", "C"],                // 用 nodes 里的 key
    "signals": ["turn_completed", "status_changed", "decision_appended"],
    "speak_contract": {"advice_when": ["gap_or_risk"],
                       "alarm_when": ["mutually_exclusive_fact"]}
  },
  "root_auto": true                          // 根卡开自动模式（子卡继承）
}

硬边界
------
只对 5713 隔离实例发请求（起手先验 ``/__instance__`` 三字段，不符立即中止）；
**绝不读写 5700 的目录/数据/进程**。所有卡与数据落在 ``.worktrees\\5713_sandbox``。
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

POLL = 8
SIG = ("turn_completed", "status_changed", "decision_appended")


# ── 基础 ────────────────────────────────────────────────────────────────

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
    except Exception as exc:                                    # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def require_5713(base: str) -> dict | None:
    """起手验身：三字段必须指向 5713 与其沙箱。不符返回 None（调用方中止）。"""
    st, inst = _req(base, "GET", "/__instance__")
    if st != 200 or not isinstance(inst, dict):
        _log(f"!! /__instance__ 不可用（status={st}）：{str(inst)[:200]}")
        return None
    bd = str(inst.get("base_dir") or "").replace("/", "\\")
    sr = str(inst.get("sandbox_root") or "").replace("/", "\\")
    ok = (inst.get("instance") == "test-instance"
          and "5713" in bd and ".worktrees" in bd
          and "5713" in sr and "sandbox" in sr.lower()
          and inst.get("sandbox_enabled") is True)
    if not ok:
        _log(f"!! 实例身份不符，中止。base_dir={bd} sandbox_root={sr} "
             f"enabled={inst.get('sandbox_enabled')}")
        return None
    _log(f"实例验身通过：pid={inst.get('pid')} base_dir={bd}")
    return inst


def card_meta(base: str, wid: str, cid: str) -> dict:
    st, res = _req(base, "GET", f"/api/works/{wid}/cards/{cid}")
    if st != 200 or not isinstance(res, dict):
        return {}
    return res.get("meta") or res


def card_body(base: str, wid: str, cid: str) -> dict:
    st, res = _req(base, "GET", f"/api/works/{wid}/cards/{cid}")
    if st != 200 or not isinstance(res, dict):
        return {}
    return res.get("body") or {}


def card_status(base: str, wid: str, cid: str) -> str:
    return str(card_body(base, wid, cid).get("status_block") or "")


def read_events(sandbox_root: str, wid: str, cid: str) -> list:
    """读事件流原文（沙箱盘 works/<wid>/events/<cid>.jsonl）——只为拿 reply_to。

    帧结构（实测，别猜）：``{"type": "msg_answer", "data": {"content": "[[card-msg ... reply_to=X]]"}}``
    —— 正文在 ``data.content``，**不在顶层 content/text**。取错字段会让 reply_to 搜索
    永远落空、把配对判成 0（本工具第一版正是这么错的，用 work_026 的已知 PASS 跑次
    对照抓出来的：channel_walk 判配对成功，这里判 0 ⇒ 是尺子错了，不是链路错了）。
    取法照 ``channel_walk.py::_read_events`` 的已验证实现。
    """
    if not sandbox_root:
        return []
    fp = os.path.join(sandbox_root, "works", wid, "events", f"{cid}.jsonl")
    if not os.path.exists(fp):
        return []
    out = []
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
                payload = e.get("payload") or e.get("data") or e
                out.append({"type": t,
                            "raw": json.dumps(payload, ensure_ascii=False)})
    return out


def list_roles(base: str, wid: str) -> list:
    st, res = _req(base, "GET", f"/api/works/{wid}/roles")
    if st != 200 or not isinstance(res, dict):
        return []
    return res.get("roles") or []


def _find_role_id(base: str, wid: str, title: str) -> str:
    """组阁响应体结构在不同版本里不稳（实测只拿到 200 却没 role_id），
    所以**不信响应体、回查列表**：按标题精确匹配，取不到再退最后一张。"""
    roles = list_roles(base, wid)
    for r in roles:
        if str(r.get("title") or "") == str(title):
            return str(r.get("role_id") or "")
    if roles:
        return str(roles[-1].get("role_id") or "")
    return ""


def role_dossier(base: str, wid: str, rid: str) -> dict:
    """拉角色完整档案（列表不带判词/警报，必须逐个取）。"""
    st, res = _req(base, "GET", f"/api/works/{wid}/roles/{rid}")
    if st != 200 or not isinstance(res, dict):
        return {}
    prof = res.get("profile") or {}
    duty = prof.get("duty") or {}
    verds = res.get("verdicts") or []
    alarms = res.get("alarms") or []
    return {
        "role_id": rid,
        "title": res.get("title"),
        "duty_state": duty.get("state"),
        "pending_sync": duty.get("pending_sync"),
        "last_wake_at": duty.get("last_wake_at"),
        "charter_version": (prof.get("charter") or {}).get("version"),
        "watch": (prof.get("watch") or {}).get("cards") or [],
        "signals": (prof.get("watch") or {}).get("signals") or [],
        "verdict_count": len(verds),
        "open_alarm_count": len(res.get("open_alarms") or []),
        "alarm_count": len(alarms),
        # 判词全文（这是"观测仪器到底看见了什么"的唯一凭据）
        "verdicts": [{"id": v.get("id"), "ts": v.get("ts"),
                      "decision": v.get("decision"),
                      "reason": (v.get("reason") or "")[:300],
                      "sources": v.get("sources")} for v in verds[-8:]],
    }


# ── up：起场景 ──────────────────────────────────────────────────────────

def cmd_up(args) -> int:
    inst = require_5713(args.base)
    if not inst:
        return 2

    spec = json.load(open(args.spec, encoding="utf-8"))
    nodes = spec.get("nodes") or []
    if not nodes:
        _log("!! spec 里没有 nodes")
        return 2
    # key 唯一性先验——建到一半才发现重名， época 已花掉
    keys = [str(n.get("key") or "") for n in nodes]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        _log(f"!! nodes 的 key 必须非空且唯一，实际：{keys}")
        return 2

    title = spec.get("work_title") or f"exp-{datetime.now().strftime('%m%d-%H%M%S')}"
    st, wk = _req(args.base, "POST", "/api/works", {"title": title})
    if st not in (200, 201) or not isinstance(wk, dict) or "work_id" not in wk:
        _log(f"!! 建 work 失败 status={st}：{str(wk)[:300]}")
        return 3
    wid = wk["work_id"]
    root = wk.get("work_card_id") or "card_001"
    _log(f"work 已建：{wid}（根卡 {root}）")

    if spec.get("root_auto", True):
        st, _ = _req(args.base, "PATCH", f"/api/works/{wid}/cards/{root}",
                     {"updates": {"auto_mode": 1},
                      "history_note": "场景台：根卡开自动模式，子卡继承 auto 与全开权限"})
        _log(f"根卡自动模式：{'ok' if st == 200 else f'失败 status={st}'}")

    # 建节点：先全部建成「无 goal」（不点火），最后统一补 goal ——
    # 否则先建的会立刻开跑，而它需要的邻居还没建出来，观测到的是半成品场景。
    idmap: dict[str, str] = {}
    for n in nodes:
        key = str(n["key"])
        body = {"title": n.get("title") or key,
                "source": "用户直接创建",
                "card_type": n.get("card_type") or "sub_project",
                "parent": root, "goal": ""}
        st, res = _req(args.base, "POST", f"/api/works/{wid}/cards", body)
        if st not in (200, 201):
            _log(f"!! 建节点 {key} 失败 status={st}：{str(res)[:300]}")
            return 4
        cid = ((res.get("meta") or res).get("card_id"))
        idmap[key] = cid
        _log(f"  节点 {key} = {cid}  《{body['title']}》")

    # 观测角色（可选）：先组阁，让它盯着还没开跑的节点
    role_id = ""
    role_spec = spec.get("role") or {}
    if role_spec:
        watch = [idmap[k] for k in (role_spec.get("watch") or []) if k in idmap]
        body = {
            "title": role_spec.get("title") or "观测员",
            "charter": role_spec.get("charter") or "",
            "jurisdiction": role_spec.get("jurisdiction")
                            or {"domain": "observation", "statement": "观测",
                                "exclusive_key": "observation"},
            "watch_card_ids": watch,
            "signals": role_spec.get("signals") or list(SIG),
            "speak_contract": role_spec.get("speak_contract")
                              or {"advice_when": ["gap_or_risk"],
                                  "alarm_when": ["mutually_exclusive_fact"]},
        }
        st, res = _req(args.base, "POST", f"/api/works/{wid}/roles", body)
        if st not in (200, 201):
            _log(f"!! 组阁失败 status={st}：{str(res)[:300]}")
            return 5
        role_id = _find_role_id(args.base, wid, body["title"])
        if not role_id:
            _log("!! 组阁返回成功但回查不到角色 —— 观测仪器会缺台账，视为失败")
            return 6
        _log(f"  角色 {role_id} 已组阁，关注 {watch}")

    # 最后统一点火：把 goal 补上，节点自此开跑
    if args.no_wait:
        _log("--no-wait：不补 goal，节点保持静止（等你确认后再点名开跑）")
    else:
        for n in nodes:
            key = str(n["key"])
            goal = n.get("goal") or ""
            if not goal:
                continue
            st, _ = _req(args.base, "PATCH", f"/api/works/{wid}/cards/{idmap[key]}",
                         {"updates": {"goal": goal},
                          "history_note": "场景台：补 goal，节点开跑"})
            _log(f"  节点 {key} 点火：{'ok' if st == 200 else f'失败 status={st}'}")
            time.sleep(0.3)

    out = {"at": datetime.now().isoformat(timespec="seconds"), "base": args.base,
           "work_id": wid, "root": root, "nodes": idmap,
           "role_id": role_id, "spec": args.spec}
    if args.out:
        json.dump(out, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        _log(f"卡号映射已写：{args.out}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    _log(f"下一步：python verify/5713/exp_scenario.py observe --work {wid}")
    return 0


# ── observe：观测 + 机械打分 ────────────────────────────────────────────

def cmd_observe(args) -> int:
    inst = require_5713(args.base)
    if not inst:
        return 2
    sandbox = str((inst or {}).get("sandbox_root") or "")
    wid = args.work
    if not wid:
        _log("!! 必须给 --work")
        return 2

    st, canvas = _req(args.base, "GET", f"/api/works/{wid}/canvas")
    cards = []
    if isinstance(canvas, dict):
        cards = canvas.get("cards") or canvas.get("nodes") or []
    ids = []
    for c in cards:
        cid = c.get("card_id") or (c.get("meta") or {}).get("card_id") or c.get("id")
        if cid:
            ids.append(cid)
    if not ids:
        _log("!! 画布上没读到卡；用 --cards 手工指定")
        ids = [x.strip() for x in (args.cards or "").split(",") if x.strip()]
    _log(f"work {wid}：{len(ids)} 张卡 {ids}")

    ev: dict = {"at": datetime.now().isoformat(timespec="seconds"),
                "base": args.base, "work_id": wid, "instance": inst,
                "cards": {}, "score": {}}

    for cid in ids:
        meta = card_meta(args.base, wid, cid)
        ev["cards"][cid] = {
            "title": meta.get("title"),
            "card_type": meta.get("card_type"),
            "parent": meta.get("parent"),
            "state": meta.get("state"),
            "auto_mode": meta.get("auto_mode"),
            "goal_state": meta.get("goal_state"),
            "goal_rounds": meta.get("goal_rounds"),
            "links": meta.get("links") or [],
            "relations": meta.get("relations") or [],
            "status_block": card_status(args.base, wid, cid),
        }

    st, msgs = _req(args.base, "GET", f"/api/works/{wid}/card-messages?limit=500")
    items = (msgs or {}).get("items", []) if isinstance(msgs, dict) else []
    ev["card_messages"] = items
    ev["events"] = {cid: read_events(sandbox, wid, cid) for cid in ids}

    # 观测角色台账：列表不带判词/警报，必须逐个取完整档案。
    # 这一截不能省 —— 角色是"观测仪器"，它的判词是「仪器看见了什么」的唯一凭据；
    # 缺了它，实验就退化成只看节点自己的自述。
    roles = []
    for r in list_roles(args.base, wid):
        rid = str(r.get("role_id") or "")
        if not rid:
            continue
        d = role_dossier(args.base, wid, rid)
        if d:
            roles.append(d)
    ev["roles"] = roles
    if roles:
        _log(f"角色台账：{len(roles)} 个角色，判词合计 {sum(x['verdict_count'] for x in roles)} 条")

    ev["score"] = score(ev, ids)

    if args.out:
        json.dump(ev, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    print_score(ev, wid)
    return 0


def score(ev: dict, ids: list[str]) -> dict:
    """把「有没有发生」判成机器可读的事实。

    两条铁律：
      ① 只用事实锚点（卡面 links / 端点字段 from_card·to_card·msg_type / 事件流 reply_to），
         不在自由文本里找 ASCII 关键词——模型换个措辞就会误报（本仓实测三跑误报两次）。
      ② 「没尝试」与「尝试了被拒」必须分开报。断路场景下这两个的差别就是全部结论的差别。
    """
    cards = ev["cards"]
    msgs = ev["card_messages"]

    # 建线：谁指向谁（links 是卡面事实）。
    # **必须把「角色关注线」与「节点自主建的线」分开**：角色线是组阁时系统自动写的
    # （根卡 create_role 顺手落 links），把它算成"自主建线"就是把 CEO 自己的动作
    # 记成节点的涌现行为 —— 实验结论会因此整体作废。实测 work_030 就有两条这种线。
    role_ids = {str(r.get("role_id") or "") for r in (ev.get("roles") or [])}
    link_edges = []
    for src, c in cards.items():
        for l in (c.get("links") or []):
            tgt = str(l.get("target") or "")
            if not tgt:
                continue
            is_role = (str(l.get("type") or "") == "角色") or (tgt in role_ids)
            link_edges.append({"from": src, "to": tgt, "type": l.get("type"),
                               "note": l.get("note"), "is_role_edge": is_role})
    # 只有节点↔节点才算「自主建线」的候选
    peer_links = [e for e in link_edges if not e["is_role_edge"]]

    # 发信：按端点字段统计边（msg_type 分列，不做语义解释）
    edges: dict[str, dict[str, int]] = {}
    for m in msgs:
        a = str(m.get("from_card") or "")
        b = str(m.get("to_card") or "")
        t = str(m.get("msg_type") or "")
        if not (a and b):
            continue
        edges.setdefault(f"{a}->{b}", {})
        edges[f"{a}->{b}"][t] = edges[f"{a}->{b}"].get(t, 0) + 1

    # reply_to 配对：端点不下发该字段，只能从事件流原文核（事实锚点，非推断）
    asks = {str(m.get("id") or "") for m in msgs if m.get("msg_type") == "msg_ask"}
    blob = "\n".join(x.get("raw", "") for lst in ev["events"].values() for x in lst)
    paired = [a for a in asks if f"reply_to={a}" in blob]

    # 越界尝试：denied 原文是否出现在任何卡的现状块里。
    # 节点被拒时 prompts 要求「把被拒原文如实写进现状块」⇒ 有原文 = 真的试过。
    denied = []
    for cid, c in cards.items():
        for m in re.finditer(r"denied[^\"\n]{0,160}", c.get("status_block") or ""):
            denied.append({"card": cid, "raw": m.group(0)[:200]})
    # 同一件事反复重试（提示词要求「不要反复重试同一调用」）
    for cid, c in cards.items():
        n = len(re.findall(r"denied", c.get("status_block") or ""))
        if n >= 2:
            denied.append({"card": cid, "raw": f"denied 出现 {n} 次（可能反复重试）"})

    # 往返轮次：A→B 与 B→A 同时存在的对数
    roundtrips = 0
    for k in edges:
        a, _, b = k.partition("->")
        if f"{b}->{a}" in edges:
            roundtrips += 1
    roundtrips //= 2

    roles = ev.get("roles") or []
    role_summary = {
        "n_roles": len(roles),
        "verdict_total": sum(int(r.get("verdict_count") or 0) for r in roles),
        "open_alarm_total": sum(int(r.get("open_alarm_count") or 0) for r in roles),
        # 仪器是否真的在跑：pending_sync 长期不为 0 或判词恒 0，都是「仪器没工作」的信号
        "duty_states": [{"role_id": r.get("role_id"), "state": r.get("duty_state"),
                         "pending_sync": r.get("pending_sync"),
                         "verdicts": r.get("verdict_count")} for r in roles],
    }

    return {
        "role_summary": role_summary,
        "n_cards": len(cards),
        "link_edges": link_edges,
        "link_edge_count": len(link_edges),
        "peer_link_edges": peer_links,
        "peer_link_count": len(peer_links),
        "msg_edges": edges,
        "msg_total": len(msgs),
        "ask_total": sum(1 for m in msgs if m.get("msg_type") == "msg_ask"),
        "answer_total": sum(1 for m in msgs if m.get("msg_type") == "msg_answer"),
        "notify_total": sum(1 for m in msgs if m.get("msg_type") == "msg_notify"),
        "ask_answer_pairs": len(paired),
        "bidirectional_pairs": roundtrips,
        "denied_seen": denied,
        "goal_states": {c: cards[c].get("goal_state") for c in cards},
        "status_len": {c: len(cards[c].get("status_block") or "") for c in cards},
        # 一句话结论：这三档的差别，就是「涌现没发生」与「通道断了」的差别
        "diagnosis": _diagnose(cards, msgs, peer_links, denied, paired),
    }


def _diagnose(cards, msgs, peer_links, denied, paired) -> str:
    """把观测收敛成一句可争辩的话，而不是一堆数字。

    ``peer_links`` 只含节点↔节点（角色关注线已排除）——否则"CEO 组阁"会被读成
    "节点自主建线"，这是本工具最容易犯也最贵的错。
    """
    if not msgs and not peer_links:
        if denied:
            return ("节点**尝试过**接触但被拒（denied 原文在现状块里）⇒ "
                    "观测不到协作是「通道阻断」，不是「节点没有主动性」。"
                    "此结论不得用于判断涌现。")
        return ("零建线零发信且**无任何 denied 记录** ⇒ 要么节点没尝试接触，"
                "要么它们的回合根本没跑起来。下判断前先看 status_len 与 goal_states："
                "现状块接近空/目标未推进 = 回合没跑，属仪器问题。")
    if peer_links and not msgs:
        return "节点间有建线但无发信 ⇒ 线建立了但没用它发消息，属「半程」。"
    if msgs and paired == 0:
        return "有卡间信但 ask→answer 无配对 ⇒ 在说话但没形成问答往返。"
    if paired and msgs:
        return f"形成 {len(paired)} 组 ask→answer 配对 ⇒ 卡间问答往返成立。"
    return "有卡间信（非 ask/answer 型）⇒ 单向通知为主，未形成问答往返。"


def print_score(ev: dict, wid: str) -> None:
    s = ev["score"]
    print("=" * 74)
    print(f"5713 涌现实验观测 · {wid} · {ev['at']}")
    print("=" * 74)
    for cid, c in ev["cards"].items():
        print(f"  {cid:<10} 《{(c.get('title') or '')[:22]}》 parent={c.get('parent') or '(无)'}")
        print(f"             类型={c.get('card_type')} auto={c.get('auto_mode')} "
              f"目标={c.get('goal_state')} 轮次={c.get('goal_rounds')} "
              f"现状={len(c.get('status_block') or '')}字")
    print("-" * 74)
    print(f"建线 {s['link_edge_count']} 条 ｜ 卡间信 {s['msg_total']} 条"
          f"（ask {s['ask_total']} / answer {s['answer_total']} / notify {s['notify_total']}）")
    print(f"ask→answer 配对 {s['ask_answer_pairs']} ｜ 双向往返 {s['bidirectional_pairs']}")
    if s["msg_edges"]:
        for k, v in s["msg_edges"].items():
            print(f"    {k}: {v}")
    if s["link_edges"]:
        for e in s["link_edges"]:
            print(f"    建线 {e['from']} -> {e['to']}（{e.get('type')}）")
    if s["denied_seen"]:
        print(f"越界被拒记录 {len(s['denied_seen'])} 处：")
        for d in s["denied_seen"][:6]:
            print(f"    [{d['card']}] {d['raw'][:120]}")
    rs = s.get("role_summary") or {}
    if rs.get("n_roles"):
        print("-" * 74)
        print(f"观测角色 {rs['n_roles']} 个 ｜ 判词 {rs['verdict_total']} 条 ｜ "
              f"未结警报 {rs['open_alarm_total']} 条")
        for d in rs.get("duty_states") or []:
            print(f"    {d['role_id']}: 值班={d['state']} 待同步={d['pending_sync']} "
                  f"判词={d['verdicts']}")
        for r in (ev.get("roles") or []):
            for v in (r.get("verdicts") or [])[-3:]:
                print(f"    [{r.get('role_id')}] {v.get('decision')} · "
                      f"{(v.get('reason') or '')[:90]}")
    print("-" * 74)
    print(f"判读：{s['diagnosis']}")
    print("=" * 74)


# ── main ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("up", help="按 spec 起实验场景")
    a.add_argument("--base", default="http://127.0.0.1:5713")
    a.add_argument("--spec", required=True)
    a.add_argument("--out", default="")
    a.add_argument("--no-wait", action="store_true",
                   help="只建卡不点火（节点保持静止，便于先确认场景）")
    a.set_defaults(func=cmd_up)

    o = sub.add_parser("observe", help="拉观测面并机械打分（只读）")
    o.add_argument("--base", default="http://127.0.0.1:5713")
    o.add_argument("--work", required=True)
    o.add_argument("--cards", default="", help="画布读不到卡时手工给，逗号分隔")
    o.add_argument("--out", default="")
    o.set_defaults(func=cmd_observe)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        pass
    raise SystemExit(main())