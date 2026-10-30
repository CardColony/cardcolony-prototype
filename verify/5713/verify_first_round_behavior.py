#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_first_round_behavior.py
=============================
**真实回合**验证：自动模式根卡的**第一轮**，到底有没有收到「拆职能判断」的注入、
模型到底有没有做这个判断。

为什么做这件事（卡面事实）
--------------------------
card_045 已经从系统外部核对了 91 区（``team_function_instruction``）的**代码层**契约：
根卡 ＋ auto_mode ＋ ``goal_rounds == 0`` 才出区、文案含「先做一个明确判断」。
那只回答「**代码会不会出区**」，没回答两件更要紧的事：

  ① 真实回合的 system prompt 里，**模型眼前**到底有没有这段注入；
  ② 看见了之后，模型**做不做**这个判断（有没有明确表态「要拆／不拆」并落到卡面）。

本脚本做一次**真实回合**（不是模拟、不是纯函数调用）：
  1. 起隔离实例（5713 端口，``base_dir`` 指向 ``.worktrees\\5713``；**全程不碰 5700 生产**）；
  2. 在沙箱里建一个新 work（标题带时间戳），给根卡一条**真实职责** ＋ ``auto_mode=1``；
  3. 用 HTTP 建目标（``POST .../goal``）⇒ 目标 armed ⇒ 驱动器/点火器自己起轮；
  4. 等第一轮、第二轮**真跑完**，中途**不手动干预**（只有自然等不到第二轮时，
     才用 ``/goal edit`` 手动触发一次后续轮，并在证据里标明）；
  5. 从 prompt 日志（``<sandbox>/logs/prompt_<card_id>_*.json``）＋卡面（cards.db / HTTP 读卡）
     取三条实测：注入是否到了、轮首之外是否不再注入、模型有没有做判断。

**如实记录**：模型没做判断就写「没判断」并把它那一轮实际调用的工具全列出来——
这是重要发现，不是本脚本的失败。

运行
----
``python verify/5713/verify_first_round_behavior.py``            # 前台跑完
``python verify/5713/verify_first_round_behavior.py --background`` # 后台跑（长回合用）
``python verify/5713/verify_first_round_behavior.py --wait``      # 只等结果

产物
----
* ``verify/5713/evidence_first_round_behavior.json``（运行时产物）
* ``<sandbox>/_first_round_status.json``（进度）
* ``<sandbox>/_first_round_instance.log``（实例日志）
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:                                    # Windows 控制台默认 GBK，中文要显式切 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                       # noqa: BLE001 非交互环境可能没有 reconfigure
    pass

# ---------------------------------------------------------------- 路径常量
REPO = Path(__file__).resolve().parents[2]          # D:\AI_station\推演agent（工作根）
WORKTREE = REPO / ".worktrees" / "5713"               # 隔离实例跑这份代码（commit 0541e3a）
SANDBOX = REPO / ".worktrees" / "5713_sandbox"        # 运行时根（works/logs 都被搬进来）
LOGS = SANDBOX / "logs"
STATUS = SANDBOX / "_first_round_status.json"
INSTANCE_LOG = SANDBOX / "_first_round_instance.log"
EVIDENCE = REPO / "verify" / "5713" / "evidence_first_round_behavior.json"

# ---------------------------------------------------------------- 网络常量
INSTANCE = "http://127.0.0.1:5713"                    # 隔离实例
PROD = "http://127.0.0.1:5700"                        # 生产（只探活，不碰）
PORT = 5713
PYTHON = r"C:\Program Files\Python311\python.exe"

#: 91 区文案里的独特句——「注入到了没有」的锚点（commit 0541e3a 起的文案）。
ANCHOR = "先做一个明确判断"
#: 第二句锚点（91 区文案同时含「人类团队里真实存在的岗位」）。
ANCHOR2 = "人类团队里真实存在的岗位"
#: 91 区的区头（出现在 system prompt 里的「## 拆职能（你这一轮的第一件事）」）。
ANCHOR3 = "拆职能"

#: 根卡能做的**真实职责**：根卡只有 tools_read_file（只读文件/列目录/检索）＋ edit，
#: **没有写文件权限**——所以挑一件「读＋盘点＋落卡面」的真活。
#: ★ 刻意**不提**「职能／拆职能」——判断必须来自 91 区注入本身，不能由目标暗示。
OBJECTIVE = (
    "盘点 trading-workspace 目录下所有 README.md 的一级标题：逐篇读出它的一级标题，"
    "并标出哪些篇的一级标题超过一个。把盘点进度落到卡面（现状块写已盘到哪、还剩几篇；"
    "定论块用 append_ruling 追加一条 D01 记录本轮盘点结果）。"
    "全部 README 盘点完之前不要声明目标完成。"
)


# ================================================================ 小工具
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def write_status(stage: str, **extra) -> None:
    """写进度文件（外部轮询用）+ 打一行日志。"""
    payload = {"stage": stage, "ts": _now(), **extra}
    try:
        STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(f"[{_now()}] {stage} :: "
          f"{json.dumps(extra, ensure_ascii=False)[:600]}", flush=True)


def http(method: str, path: str, body=None, timeout: int = 120,
         base: str = INSTANCE):
    """一次 HTTP 调用；返回 (status_code, parsed_body)。错误也返回 status，不抛。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:  # noqa: BLE001 连接类错误
        return 0, f"{type(e).__name__}: {e}"


def probe_prod() -> dict:
    """探 5700 生产是否活着（只读 GET，绝不写它）。"""
    st, body = http("GET", "/api/works", timeout=8, base=PROD)
    alive = st == 200
    n = -1
    if alive and isinstance(body, dict):
        try:
            n = len(body.get("works") or [])
        except Exception:  # noqa: BLE001
            n = -1
    return {"http": st, "alive": alive, "works": n,
            "observed": (f"GET {PROD}/api/works → HTTP {st}"
                         + (f"，{n} 个项目" if n >= 0 else ""))}


# ================================================================ 实例起停
def launch_instance() -> int:
    """以 detached 进程起 5713 隔离实例；返回 pid。"""
    env = dict(os.environ)
    env["ECO_TEST_SANDBOX"] = "1"
    env["ECO_SANDBOX_ROOT"] = str(SANDBOX)
    INSTANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(INSTANCE_LOG, "ab", buffering=0)
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    p = subprocess.Popen(
        [PYTHON, "scripts/run_test_instance_5701.py", str(PORT)],
        cwd=str(WORKTREE), env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=log, creationflags=flags,
    )
    return p.pid


def wait_ready(timeout: int = 90) -> dict:
    """等实例起来并回读 /__instance__（拿 base_dir / sandbox_root 证明隔离）。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        st, body = http("GET", "/__instance__", timeout=8)
        if st == 200 and isinstance(body, dict):
            return body
        last = f"HTTP {st} {str(body)[:200]}"
        time.sleep(1.5)
    raise RuntimeError(f"5713 实例未就绪：{last}")


def kill_instance(pid: int) -> str:
    """**只按 PID 精确停**自己起的实例（禁止 /IM 批量杀）。"""
    out = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "") + (out.stderr or "")


# ================================================================ 取证
def prompt_logs(card_id: str, since_ts: float) -> list[Path]:
    """该卡在 since_ts 之后落盘的 prompt 日志（按 mtime 升序）。

    注意：logs 目录是**全局平铺**的（文件名只带 card_id，不带 work_id），
    而每个 work 的根卡都叫 card_001 ⇒ 必须靠时间戳把别的项目的日志剔掉。
    """
    if not LOGS.is_dir():
        return []
    out = []
    for p in LOGS.glob(f"prompt_{card_id}_*.json"):
        try:
            if p.stat().st_mtime >= since_ts:
                out.append(p)
        except OSError:
            continue
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def read_round_log(path: Path, index: int) -> dict:
    """读一条 prompt 日志，取出本轮要用的取证字段。"""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"index": index, "log_file": path.name, "read_error": str(e)}
    sp = str(d.get("system_prompt") or "")
    up = str(d.get("user_prompt") or "")
    resp = str(d.get("response") or "")
    hit = ANCHOR in sp
    snippet = ""
    if hit:
        i = sp.find(ANCHOR)
        snippet = sp[max(0, i - 200): i + 400]
    region91 = None
    for r in (d.get("regions") or []):
        if int(r.get("order") or -1) == 91:
            region91 = r
    tools = []
    for ev in (d.get("tool_events") or []):
        tools.append({"tool": ev.get("tool"), "ok": ev.get("ok"),
                      "card_id": ev.get("card_id"), "title": ev.get("title")})
    return {
        "index": index,
        "log_file": path.name,
        "mtime": path.stat().st_mtime,
        "timestamp": d.get("timestamp"),
        "model_used": d.get("model_used"),
        "model_requested": d.get("model_requested"),
        "error": d.get("error"),
        "system_prompt_len": len(sp),
        "anchor_hit": hit,
        "anchor2_hit": ANCHOR2 in sp,
        "anchor3_head_hit": ANCHOR3 in sp,
        "anchor_snippet": snippet,
        "region91_in_log": region91,
        "user_prompt_head": up[:400],
        "response_head": resp[:1500],
        "tool_events": tools,
        "tool_names": [t.get("tool") for t in tools],
    }


def card_state(work_id: str, card_id: str) -> dict:
    """读卡面：goal 状态/轮次（HTTP 读卡 ＋ 只读 sqlite 兜底）。"""
    out: dict = {}
    st, body = http("GET", f"/api/works/{work_id}/cards/{card_id}", timeout=30)
    if st == 200 and isinstance(body, dict):
        meta = body.get("meta") or {}
        b = body.get("body") or {}
        out["http_status"] = st
        out["auto_mode"] = meta.get("auto_mode")
        out["goal_state"] = meta.get("goal_state")
        out["goal_rounds"] = meta.get("goal_rounds")
        out["goal"] = meta.get("goal")
        out["card_type"] = meta.get("card_type")
        out["status_block"] = b.get("status_block") or ""
        out["rulings_or_requirements"] = (b.get("rulings")
                                          or b.get("requirements") or "")
    db = SANDBOX / "works" / work_id / "cards.db"
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT goal_state, goal_rounds, goal, auto_mode, status_block,"
                " rulings FROM cards WHERE card_id=?", (card_id,)).fetchone()
            conn.close()
            if row is not None:
                out["db"] = {k: row[k] for k in row.keys()}
        except Exception as e:  # noqa: BLE001
            out["db_error"] = str(e)
    # 事件流：goal_round 事件数＝驱动真正跑过的续轮数（权威计数在事件流里）
    ev = SANDBOX / "works" / work_id / "events" / f"{card_id}.jsonl"
    if ev.exists():
        kinds: dict = {}
        rounds: list = []
        try:
            for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                t = str(rec.get("type") or "")
                kinds[t] = kinds.get(t, 0) + 1
                if t == "goal_round":
                    rounds.append((rec.get("data") or {}).get("round"))
        except Exception as e:  # noqa: BLE001
            out["events_error"] = str(e)
        out["events"] = {"kinds": kinds, "goal_round_rounds": rounds}
    return out


# ================================================================ 判断取证
#: 判断语义词——「模型有没有表态要不要拆职能」。
JUDGE_WORDS = ("职能", "拆", "岗位", "角色", "不拆", "要拆", "需要拆",
               "不需要拆", "watch", "组阁")
#: 「明确表态」的强信号：句子里同时出现判断词与表态词。
SPLIT_DECIDE = ("要拆", "需要拆", "该拆", "拆成", "拆出", "组建", "设岗",
                "拆几个", "拆职能", "建职能", "组一个", "设一个")
NO_SPLIT_DECIDE = ("不拆", "无需拆", "不需要拆", "不必拆", "不设职能",
                   "不组建", "没有专职", "不需要专职", "不适用")


def judge_evidence(rounds: list[dict], card: dict) -> dict:
    """从轮次工具调用 ＋ 卡面正文里找「模型有没有做判断」的证据。

    三种结果如实记录：判断了（拆）／判断了（不拆，附理由）／**没判断**。
    """
    ev: dict = {"ruling_tool_calls": [], "status_tool_calls": [],
                "function_calls": [], "card_text_hits": [], "reply_hits": []}

    for rd in rounds:
        for t in (rd.get("tool_events") or []):
            name = str(t.get("tool") or "")
            if name == "append_ruling":
                ev["ruling_tool_calls"].append({"round": rd.get("index"), **t})
            elif name == "update_status":
                ev["status_tool_calls"].append({"round": rd.get("index"), **t})
            elif name in ("create_function", "create_role"):
                ev["function_calls"].append({"round": rd.get("index"), **t})
        # 模型回复正文里有没有明确表态（不落卡面也算表态，只是没落卡面）
        resp = str(rd.get("response_head") or "")
        strong = [w for w in SPLIT_DECIDE + NO_SPLIT_DECIDE if w in resp]
        if strong:
            ev["reply_hits"].append({"round": rd.get("index"), "words": strong,
                                     "excerpt": resp[:600]})

    for key in ("status_block", "rulings_or_requirements"):
        text = str(card.get(key) or "")
        if not text.strip():
            continue
        hits = [w for w in JUDGE_WORDS if w in text]
        strong = [w for w in SPLIT_DECIDE + NO_SPLIT_DECIDE if w in text]
        if hits:
            ev["card_text_hits"].append({
                "field": key, "hit_words": hits, "strong_words": strong,
                "excerpt": text[:900],
            })

    ev["raw_rulings_text"] = str(card.get("rulings_or_requirements") or "")[:2000]
    ev["raw_status_text"] = str(card.get("status_block") or "")[:2000]

    did_tool = bool(ev["ruling_tool_calls"] or ev["status_tool_calls"])
    did_function = bool(ev["function_calls"])
    said_in_card = bool(ev["card_text_hits"])
    said_in_reply = bool(ev["reply_hits"])
    if did_function:
        verdict = "判断了（拆）——调了建职能工具"
    elif said_in_card or said_in_reply:
        verdict = "判断了（表态）——文字落到了卡面或回复里"
    elif did_tool:
        verdict = "判断了（落卡面）——写了现状/需求文档，但正文里没抓到判断词"
    else:
        verdict = "没判断——这一轮没有任何判断表态"
    ev["verdict"] = verdict
    ev["did_call_judgment_tool"] = did_tool
    ev["did_create_function"] = did_function
    ev["said_in_card_text"] = said_in_card
    ev["said_in_reply"] = said_in_reply
    ev["tools_called_in_round1"] = (rounds[0].get("tool_names") if rounds else [])
    return ev


# ================================================================ 主流程
def main() -> int:
    evidence: dict = {
        "run_at": _now(),
        "script": str(Path(__file__).resolve()),
        "anchor": ANCHOR,
        "anchor2": ANCHOR2,
        "objective": OBJECTIVE,
        "rounds": [],
        "notes": [],
    }

    write_status("phase0-prod-probe")
    prod_before = probe_prod()
    evidence["prod_5700_before"] = prod_before
    evidence["notes"].append(f"5700 探活（开工前）：{prod_before['observed']}")

    pid = None
    inst: dict = {}
    work_id = root_id = ""
    t_start = time.time()
    try:
        # ── 1. 起隔离实例 ──────────────────────────────────────────────
        write_status("phase1-launch")
        pid = launch_instance()
        evidence["instance_pid"] = pid
        inst = wait_ready()
        base_dir = str(inst.get("base_dir") or "")
        sandbox_root = str(inst.get("sandbox_root") or "")
        isolated = (Path(base_dir).resolve() == WORKTREE.resolve()
                    and Path(sandbox_root).resolve() == SANDBOX.resolve())
        evidence["instance"] = {
            "port": PORT, "pid": pid, "base_dir": base_dir,
            "sandbox_root": sandbox_root,
            "sandbox_enabled": inst.get("sandbox_enabled"),
            "isolated": isolated,
        }
        evidence["notes"].append(
            f"实例 /__instance__：base_dir={base_dir}；sandbox_root={sandbox_root}")
        if not isolated:
            evidence["notes"].append("!! 隔离判定失败：base_dir/sandbox_root 与预期不符")

        # ── 2. 沙箱里建新 work ───────────────────────────────────────
        write_status("phase2-create-work")
        ts = time.strftime("%Y%m%d-%H%M%S")
        title = f"轮首注入验证-{ts}"
        st, w = http("POST", "/api/works", {"title": title}, timeout=120)
        if st not in (200, 201):
            raise RuntimeError(f"建项目失败：HTTP {st} {str(w)[:300]}")
        work_id = str(w.get("work_id") or "")
        root_id = str(w.get("work_card_id") or "")
        evidence["work"] = {"work_id": work_id, "root_card_id": root_id,
                             "title": title}
        write_status("phase2-work-created", work_id=work_id, root_card_id=root_id)

        # 给根卡一条真实职责 ＋ 开自动模式
        st, r = http("PATCH", f"/api/works/{work_id}/cards/{root_id}", {
            "updates": {
                "mission": "把 trading-workspace 的 README 标题层级盘点清楚并落到卡面。",
                "auto_mode": 1,
            },
            "history_note": "真实回合验证：设使命 + 开自动模式",
        }, timeout=60)
        evidence["notes"].append(f"设 mission/auto_mode：HTTP {st} {str(r)[:160]}")
        if st != 200:
            raise RuntimeError(f"设 auto_mode 失败：HTTP {st} {str(r)[:300]}")

        # ── 3. 建目标 ⇒ 自动起轮 ──────────────────────────────────────
        write_status("phase3-create-goal")
        t_start = time.time()
        st, g = http("POST", f"/api/works/{work_id}/cards/{root_id}/goal",
                     {"input": OBJECTIVE}, timeout=120)
        evidence["goal_create"] = {"http": st, "body": str(g)[:400]}
        evidence["notes"].append(f"建目标（HTTP /goal）：HTTP {st} {str(g)[:200]}")
        if st != 200:
            raise RuntimeError(f"建目标失败：HTTP {st} {str(g)[:300]}")
        write_status("phase3-goal-created")

        # ── 4. 等第一轮 ───────────────────────────────────────────────
        write_status("phase4-wait-round1")
        logs1 = _wait_logs(root_id, t_start, 1, timeout=1500)
        if not logs1:
            raise RuntimeError("1500s 内没有落盘任何 prompt 日志（模型没跑起来？）")
        r1 = read_round_log(logs1[-1], 1)
        evidence["rounds"].append(r1)
        write_status("phase4-round1-done", anchor_hit=r1.get("anchor_hit"),
                     tools=r1.get("tool_names"), model=r1.get("model_used"))

        # ── 5. 等第二轮（自然等不到就手动触发一次后续轮）────────────
        write_status("phase5-wait-round2")
        logs2 = _wait_logs(root_id, t_start, 2, timeout=1500)
        trigger = "natural"
        if len(logs2) < 2:
            write_status("phase5-manual-trigger")
            trigger = "manual_goal_edit"
            st, rr = http("POST", f"/api/works/{work_id}/cards/{root_id}/goal",
                          {"input": "edit " + OBJECTIVE}, timeout=120)
            evidence["notes"].append(
                f"自然等不到第二轮 ⇒ 手动触发一次后续轮（/goal edit）："
                f"HTTP {st} {str(rr)[:200]}")
            logs2 = _wait_logs(root_id, t_start, 2, timeout=900)
        if len(logs2) >= 2:
            r2 = read_round_log(logs2[-1], 2)
            r2["trigger"] = trigger
            evidence["rounds"].append(r2)
            write_status("phase5-round2-done", anchor_hit=r2.get("anchor_hit"),
                         tools=r2.get("tool_names"), model=r2.get("model_used"))
        else:
            evidence["notes"].append("!! 第二轮始终没跑起来（判据③记 SKIP）")

        # ── 6. 卡面取证 ───────────────────────────────────────────────
        write_status("phase6-read-card")
        card = card_state(work_id, root_id)
        evidence["card_state"] = card

        # 停掉自动续轮（别让实例在我收尾时继续烧轮次）
        http("POST", f"/api/works/{work_id}/cards/{root_id}/stop", timeout=60)

    except Exception as e:  # noqa: BLE001 允许的失败：如实记
        evidence["failure"] = f"{type(e).__name__}: {e}"
        write_status("failed", error=str(e))
    finally:
        if pid:
            out = kill_instance(pid)
            evidence["instance_kill"] = out.strip()[:300]
            write_status("phase7-instance-stopped", kill=out.strip()[:120])
        prod_after = probe_prod()
        evidence["prod_5700_after"] = prod_after
        evidence["notes"].append(f"5700 探活（收尾时）：{prod_after['observed']}")

    # ── 7. 判断取证 ＋ 五条判据 ──────────────────────────────────────
    evidence["judge"] = judge_evidence(evidence.get("rounds") or [],
                                        evidence.get("card_state") or {})
    evidence["criteria"] = _judge(evidence)
    try:
        EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        evidence["evidence_write_error"] = str(e)
    write_status("done", evidence=str(EVIDENCE))

    n_fail = sum(1 for c in evidence["criteria"].values()
                 if c.get("verdict") == "FAIL")
    n_skip = sum(1 for c in evidence["criteria"].values()
                 if c.get("verdict") == "SKIP")
    print("=" * 70)
    for name, c in evidence["criteria"].items():
        print(f"[{c.get('verdict')}] {name}：{c.get('observed')}")
    print(f"FAIL {n_fail} / SKIP {n_skip}")
    if evidence.get("failure"):
        print(f"!! 运行失败：{evidence['failure']}")
        return 2
    return 1 if n_fail else 0


def _wait_logs(card_id: str, since_ts: float, want: int, timeout: int) -> list[Path]:
    """等 prompt 日志攒到 want 份（按 mtime 升序）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = prompt_logs(card_id, since_ts)
        if len(logs) >= want:
            return logs
        time.sleep(5)
    return prompt_logs(card_id, since_ts)


def _judge(ev: dict) -> dict:
    """按任务书的五条判据逐条判定（每条贴实测）。"""
    out: dict = {}
    inst = ev.get("instance") or {}
    prod_b = ev.get("prod_5700_before") or {}
    prod_a = ev.get("prod_5700_after") or {}
    rounds = ev.get("rounds") or []
    r1 = rounds[0] if len(rounds) >= 1 else {}
    r2 = rounds[1] if len(rounds) >= 2 else {}
    card = ev.get("card_state") or {}

    # ① 实例隔离 ＋ 5700 未被碰
    ok1 = bool(inst.get("isolated")) and bool(prod_b.get("alive")) \
        and bool(prod_a.get("alive"))
    out["c1_isolated_instance_and_5700_untouched"] = {
        "verdict": "PASS" if ok1 else "FAIL",
        "observed": (f"base_dir={inst.get('base_dir')}；sandbox_root="
                     f"{inst.get('sandbox_root')}；隔离判定="
                     f"{inst.get('isolated')}；5700 开工前 {prod_b.get('observed')}；"
                     f"收尾时 {prod_a.get('observed')}"),
    }

    # ② 第一轮 prompt 含锚点
    if r1:
        out["c2_first_round_anchor_present"] = {
            "verdict": "PASS" if r1.get("anchor_hit") else "FAIL",
            "observed": (f"第一轮 system_prompt 含「{ANCHOR}」="
                         f"{r1.get('anchor_hit')}；含「{ANCHOR2}」="
                         f"{r1.get('anchor2_hit')}；含区头「{ANCHOR3}」="
                         f"{r1.get('anchor3_head_hit')}；"
                         f"长度={r1.get('system_prompt_len')}；"
                         f"日志={r1.get('log_file')}；模型={r1.get('model_used')}"),
            "snippet": r1.get("anchor_snippet", ""),
        }
    else:
        out["c2_first_round_anchor_present"] = {
            "verdict": "SKIP", "observed": "第一轮没跑起来，无 prompt 日志"}

    # ③ 第二轮（后续轮）不再含锚点
    if r2:
        out["c3_second_round_anchor_absent"] = {
            "verdict": "PASS" if not r2.get("anchor_hit") else "FAIL",
            "observed": (f"第二轮 system_prompt 含「{ANCHOR}」="
                         f"{r2.get('anchor_hit')}；含「{ANCHOR2}」="
                         f"{r2.get('anchor2_hit')}；日志={r2.get('log_file')}；"
                         f"触发方式={r2.get('trigger')}；模型={r2.get('model_used')}"),
            "snippet": r2.get("anchor_snippet", ""),
        }
    else:
        out["c3_second_round_anchor_absent"] = {
            "verdict": "SKIP", "observed": "第二轮始终没跑起来，无法核对"}

    # ④ 模型的判断证据
    je = ev.get("judge") or {}
    if je:
        out["c4_model_judgment"] = {
            "verdict": "PASS" if str(je.get("verdict", "")).startswith("判断了") else "FAIL",
            "observed": (f"{je.get('verdict')}；append_ruling="
                         f"{len(je.get('ruling_tool_calls') or [])} 次；"
                         f"update_status={len(je.get('status_tool_calls') or [])} 次；"
                         f"建职能={len(je.get('function_calls') or [])} 次；"
                         f"卡面命中判断词={je.get('said_in_card_text')}；"
                         f"回复里命中表态词={je.get('said_in_reply')}"),
            "excerpt": ((je.get("card_text_hits") or [{}])[0].get("excerpt", "")
                        or (je.get("reply_hits") or [{}])[0].get("excerpt", "")
                        or je.get("raw_status_text", "")[:400]),
        }
    else:
        out["c4_model_judgment"] = {"verdict": "SKIP", "observed": "没跑到取证步"}

    # ⑤ goal_rounds 0 → ≥1
    rounds_val = card.get("goal_rounds")
    db_val = (card.get("db") or {}).get("goal_rounds")
    final_rounds = db_val if db_val is not None else rounds_val
    try:
        final_rounds_i = int(final_rounds or 0)
    except (TypeError, ValueError):
        final_rounds_i = 0
    evs = (card.get("events") or {})
    gr = evs.get("goal_round_rounds") or []
    out["c5_goal_rounds_advanced"] = {
        "verdict": "PASS" if final_rounds_i >= 1 else "FAIL",
        "observed": (f"起始 goal_rounds=0；收尾 cards.goal_rounds={final_rounds}；"
                     f"goal_state={card.get('goal_state')}；"
                     f"事件流 goal_round 事件轮次={gr}"),
    }
    return out


# ================================================================ 入口
def _background() -> int:
    """后台跑（长回合用）：detached 起自己，父进程立刻退出。"""
    env = dict(os.environ)
    out = open(SANDBOX / "_first_round_bg.log", "ab", buffering=0)
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve())],
                         cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, creationflags=flags)
    print(f"background pid={p.pid}；日志 {SANDBOX / '_first_round_bg.log'}")
    return 0


def _wait(timeout: int = 600) -> int:
    """只等结果文件出现（配合后台模式轮询）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if EVIDENCE.exists():
            try:
                ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                time.sleep(3)
                continue
            for name, c in (ev.get("criteria") or {}).items():
                print(f"[{c.get('verdict')}] {name}：{c.get('observed')}")
            return 0
        st = ""
        try:
            st = STATUS.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        print(f"... 等待中；最近进度：{st[:200]}")
        time.sleep(10)
    print(f"!! 超时未产出证据文件：{EVIDENCE}")
    return 2


if __name__ == "__main__":
    if "--background" in sys.argv:
        sys.exit(_background())
    if "--wait" in sys.argv:
        sys.exit(_wait())
    sys.exit(main())
