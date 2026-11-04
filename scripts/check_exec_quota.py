# -*- coding: utf-8 -*-
"""**定期探测各执行体的免费模型 / 额度状态** ⇒ 写状态文件（2026-11-03）。

**为什么要有这个**（用户 11-03 原话）：
> 「你可以给自己设个自动任务，每隔 12 小时查一下各端点的免费模型」

**原理**：额度状态**不该靠"打一次才知道"** ⇒ 主动探测 ⇒ 状态一变 ⇒
bridge 下次 pick 就跳过（**不等它失败**）。

**探什么**（全部只读、不花钱）：
① **WorkBuddy 两端（cn / intl）**：
   - 客户端 **product config**（`~/.workbuddy*/cache/acc-product-config-v3-*.json`）
     ⇒ `/models`（标价）、`/modelPromotions`（**免费促销 + 到期日**）、
     `/modelTiers`（**Pro Only ⇒ 该账号用不了**）。
   - 客户端 **真实扣费**（`workbuddy.db` 的 `session_usage.credit_json`）⇒ 交叉验证。
② **opencode（Zen 端点）**：起一个 serve（**内置 provider**）⇒ 逐个真调免费档
   ⇒ 哪些还能用（200）、哪些额度用完（429/403）。

**产出**：`config/wb_exec_quota.json`
```json
{"updated_at": "...",
 "exhausted": {"wb-oc:deepseek-v4.1-flash-free": {"reason": "...", "at": "..."}},
 "free_models": {"wb-cn": ["hy3"], "wb-intl": [], "wb-oc": ["flint-40b-free"]},
 "notes": {...}}
```
⇒ bridge 的 `registry.is_exhausted()` 会读它（见 `src/api/wb_exec/quota_state.py`）。

**用法**：`python scripts/check_exec_quota.py`（不花钱；opencode 那段会起进程）
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone, timedelta

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# ★ 2026-11-04：**不再裸 HTTP 直连 Zen**（免费档只允许从 opencode 内部调 ⇒
#   裸直连的 400/429 是**假信号** ⇒ 会把还活着的端点误标成"额度用完"）。
OC_FREE_MODELS = ["deepseek-v4.1-flash-free", "flint-40b-free", "big-pickle",
                  "glm-5.3-flash-free", "mimo-v2.6-flash-free"]


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


# --------------------------------------------------------------- WorkBuddy 两端
def probe_workbuddy(tag: str, data_root: pathlib.Path) -> dict:
    """读客户端的 product config：标价 / 免费促销（**到期日**）/ Pro Only。"""
    out: dict = {"endpoint": tag, "ok": False}
    cache = data_root / "cache"
    files = sorted(cache.glob("acc-product-config-v3-*.json"),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        out["error"] = f"没有 product config（{cache}）"
        return out
    try:
        d = json.loads(files[-1].read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"解析失败: {exc}"
        return out
    out["ok"] = True
    out["config_date"] = d.get("date")
    out["is_oversea"] = d.get("isOversea")
    models = {str(m.get("id") or ""): str(m.get("credits") or "")
              for m in (d.get("models") or [])}
    # **免费促销**（有到期日！）⇒ 免费档的"有效期"
    promos = []
    for p in (d.get("modelPromotions") or []):
        sch = p.get("schedule") or {}
        badge = (p.get("badge") or {}).get("label") or ""
        fac = (p.get("discount") or {}).get("factor")
        # factor==0 ⇒ 真正免费（0 折）；只是"徽标"的不算
        if fac == 0:
            promos.append({"id": p.get("id"), "badge": badge,
                           "modelIds": list(p.get("modelIds") or []),
                           "validFrom": sch.get("validFrom"),
                           "validUntil": sch.get("validUntil")})
    out["free_promotions"] = promos
    out["free_models"] = sorted({m for pm in promos for m in pm["modelIds"]})
    # **Pro Only**（该账号用不了 ⇒ 不能当"最便宜"）
    pro_only = []
    for t in (d.get("modelTiers") or []):
        b = t.get("badge") or {}
        if str(b.get("label") or "") == "Pro Only":
            pro_only += [str(x) for x in (t.get("modelIds") or [])]
    out["pro_only"] = sorted(set(pro_only))
    # 两端都有的最便宜档（排除 Pro Only）
    usable = {k: v for k, v in models.items()
              if k and v not in ("", None) and k not in out["pro_only"]}

    def _num(v: str) -> float:
        try:
            return float(str(v).lstrip("x"))
        except Exception:
            return 1e9
    out["cheapest_usable"] = sorted(
        ((k, v) for k, v in usable.items() if k not in out["free_models"]),
        key=lambda kv: _num(kv[1]))[:6]
    out["model_credits"] = models
    return out


def probe_spend(tag: str, db: pathlib.Path) -> dict:
    """客户端**真实扣费**（交叉验证"标价 x0.00 是不是假免费"）。"""
    import sqlite3
    out: dict = {"endpoint": tag, "ok": False}
    if not db.exists():
        out["error"] = "数据库不在"
        return out
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        return out
    try:
        agg: dict[str, dict] = {}
        q = ("SELECT s.model, u.credit_json FROM sessions s "
             "LEFT JOIN session_usage u ON u.session_id = s.id "
             "WHERE s.model IS NOT NULL AND s.model != ''")
        for model, cj in conn.execute(q):
            spent = None
            if cj:
                try:
                    spent = sum(float(v) for v in json.loads(cj).values())
                except Exception:
                    spent = None
            a = agg.setdefault(str(model), {"n": 0, "paid": 0, "tot": 0.0})
            a["n"] += 1
            if spent is not None and spent > 0:
                a["paid"] += 1
                a["tot"] += spent
        out["ok"] = True
        out["by_model"] = {k: v for k, v in agg.items() if v["paid"] or k == "hy3"}
    finally:
        conn.close()
    return out


# ------------------------------------------------------------------- opencode
def load_key() -> str:
    """读 ``.env`` 里的 opencode key（**只读环境/文件，不打印**）。"""
    env_file = REPO / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENCODE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return os.environ.get("OPENCODE_API_KEY", "")


#: **"上游明确说额度用完了"的措辞**（Zen/opencode 实测收集）。
#: ★ **只有命中这些措辞才标记额度耗尽**；其它失败（400/超时/网络）**不算**——
#:   误判会让一个**还活着**的端点被永久摘掉（2026-11-04 就发生过：
#:   裸直连按量端点返回 400 ⇒ 把 flint-40b-free 误标成"额度用完"）。
QUOTA_WORDS = ("free usage", "free limit", "free_tier_limit", "usage limit",
               "limit reached", "quota", "rate limit", "subscribe to",
               "upgrade to")


def _is_quota_msg(msg: str) -> bool:
    low = str(msg or "").lower()
    return any(w in low for w in QUOTA_WORDS)


async def _oc_probe_one(client, model: str, provider: str,
                         work_dir: str, timeout: float = 150.0) -> dict:
    """**通过 opencode 本身**真调一次免费档（**不是裸直连**）。

    ★ **为什么必须走 opencode**（2026-11-01 实测）：Zen 的免费档
    **只允许从 opencode 内部调** ⇒ 裸 HTTP 直连按量端点会被拒
    （429「Free usage limit reached. Free models work in OpenCode」/ 400）
    ⇒ **裸直连测出来的"不可用"是假信号** ⇒ 会把还活着的端点误摘掉。
    ⇒ 这里起一个 **内置 provider** 的 serve（key 走进程环境变量
    ``OPENCODE_API_KEY``），再真调 ⇒ 拿到的才是**上游真实的**额度状态。

    返回 ``{"kind": "ok"|"quota"|"failed", "msg": str, "ms": int}``。
    """
    import asyncio as _asyncio

    from src.api.wb_exec import oc_client as oc_mod

    t0 = time.time()
    try:
        sid = await client.create_session(
            work_dir, permission=oc_mod.FULL_ALLOW_PERMISSION)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "failed", "msg": f"建会话失败 {type(exc).__name__}: {exc}"[:160],
                "ms": int((time.time() - t0) * 1000)}
    errors: list[str] = []
    text_len = 0
    done = _asyncio.Event()

    async def _watch() -> None:
        nonlocal text_len
        try:
            async for ev in client.events(work_dir):
                props = ev.get("properties") or {}
                if not isinstance(props, dict):
                    continue
                esid = str(props.get("sessionID") or "")
                if esid and esid != sid:
                    continue
                et = str(ev.get("type") or "")
                if et == "session.error":
                    err = props.get("error") or {}
                    data = err.get("data") or {}
                    errors.append(f"{err.get('name')}: {data.get('message')}")
                elif et == "session.status":
                    stt = props.get("status") or {}
                    if str(stt.get("type")) == "retry":
                        act = stt.get("action") or {}
                        errors.append(f"retry[{act.get('reason')}]: {stt.get('message')}")
                elif et == "message.part.updated":
                    pt = props.get("part") or {}
                    if str(pt.get("type")) == "text":
                        text_len += len(str(pt.get("text") or ""))
                elif et == "session.idle":
                    done.set()
                    return
        except Exception:
            return

    watcher = _asyncio.create_task(_watch())
    try:
        await client.prompt_async(sid, provider, model,
                                  "回复两个字：好", work_dir)
    except Exception as exc:  # noqa: BLE001
        watcher.cancel()
        return {"kind": "failed", "msg": f"发任务失败 {type(exc).__name__}: {exc}"[:160],
                "ms": int((time.time() - t0) * 1000)}
    try:
        await _asyncio.wait_for(done.wait(), timeout)
    except _asyncio.TimeoutError:
        watcher.cancel()
        return {"kind": "failed", "msg": f"超时（{int(timeout)}s 无结果）",
                "ms": int((time.time() - t0) * 1000)}
    finally:
        watcher.cancel()
    ms = int((time.time() - t0) * 1000)
    joined = " | ".join(errors)
    if joined and _is_quota_msg(joined):
        return {"kind": "quota", "msg": joined[:200], "ms": ms}
    if joined:
        return {"kind": "failed", "msg": joined[:200], "ms": ms}
    if text_len:
        return {"kind": "ok", "msg": f"出活 {text_len} 字", "ms": ms}
    return {"kind": "failed", "msg": "0 正文 0 报错（空结果）", "ms": ms}


async def probe_opencode_async() -> dict:
    """起一个 **内置 provider** 的 opencode ⇒ 逐个真调免费档。"""
    from src.api.wb_exec import oc_client as oc_mod
    from src.api.wb_exec import oc_pool

    out: dict = {"endpoint": "wb-oc", "ok": True, "models": {},
                 "transport": "opencode serve（内置 provider）"}
    exe = (os.environ.get("APPDATA") or "") + r"\npm\node_modules\opencode-ai\bin\opencode.exe"
    if not os.path.exists(exe):
        out["ok"] = False
        out["error"] = f"找不到 opencode 可执行文件：{exe}"
        return out
    key = load_key()
    if not key:
        out["ok"] = False
        out["error"] = "没有 OPENCODE_API_KEY（.env 与环境变量都没有）"
        return out
    work_dir = str(REPO / "logs" / "wb_exec" / "quota_probe")
    os.makedirs(work_dir, exist_ok=True)
    try:
        srv = await oc_pool.start_oc_server(
            exe, cwd=work_dir, config_path="",   # ★ 空配置 ⇒ 用**内置 provider**
            env_extra={"OPENCODE_API_KEY": key})
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"起 opencode serve 失败：{exc}"
        return out
    client = oc_mod.OcClient(srv.base_url)
    try:
        for m in OC_FREE_MODELS:
            r = await _oc_probe_one(client, m, "opencode", work_dir)
            out["models"][m] = r
    finally:
        try:
            await client.close()
        except Exception:
            pass
        try:
            srv.stop()
        except Exception:
            pass
    return out


def probe_opencode() -> dict:
    """同步壳（脚本是同步的）。"""
    import asyncio
    try:
        return asyncio.run(probe_opencode_async())
    except Exception as exc:  # noqa: BLE001
        return {"endpoint": "wb-oc", "ok": False,
                "error": f"探测异常：{type(exc).__name__}: {exc}"[:200],
                "models": {}}


# ------------------------------------------------------------------ command-code
def probe_command_code() -> dict:
    """**真调 command-code 的免费档**（2026-11-04 加）。

    ★ 为什么要探：``cmdc --list-models`` 的 FREE 标签**会过期**
      （实测 ``poolside/laguna-s-2.1-free`` 上游说「free tier has ended」）
      ⇒ **只有真调才知道哪个还活着** ⇒ 死了就让路给下一优先级。

    ★ 探测用**最小代价**：单轮、只让它答两个字（不写文件、不占磁盘）。
    """
    import asyncio
    import subprocess

    out: dict = {"endpoint": "wb-cc", "ok": True, "models": {},
                 "transport": "command-code CLI（--yolo --no-session）"}
    try:
        from src.api.wb_exec import config as wb_cfg
        cfg = wb_cfg.load()
    except Exception as exc:  # noqa: BLE001
        return {"endpoint": "wb-cc", "ok": False,
                "error": f"读配置失败：{exc}"[:200], "models": {}}
    cc_ex = cfg.executor("wb-cc")
    if cc_ex is None or not getattr(cc_ex, "cc_cli_entry", ""):
        return {"endpoint": "wb-cc", "ok": True, "models": {},
                "note": "没配 wb-cc 执行体"}
    key = load_key_cc()
    if not key:
        return {"endpoint": "wb-cc", "ok": False,
                "error": f"没有 {cc_ex.cc_api_key_env}", "models": {}}
    models = [c.model for c in cfg.candidates
              if c.executor == "wb-cc"] or list(cc_ex.cc_models)
    for m in models:
        env = dict(os.environ)
        env[cc_ex.cc_api_key_env] = key
        env["NO_COLOR"] = "1"
        argv = [cc_ex.cc_node_exe or "node", cc_ex.cc_cli_entry,
                "-p", "回复两个字：好", "-m", m,
                "--yolo", "--trust", "--skip-onboarding", "--no-session",
                "--output-format", "json", "--max-turns", "1"]
        t0 = time.time()
        try:
            r = subprocess.run(argv, cwd=str(REPO), env=env, capture_output=True,
                               text=True, timeout=180, encoding="utf-8",
                               errors="replace")
            rc, so, se = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            rc, so, se = -1, "", "超时(180s)"
        dt = int((time.time() - t0) * 1000)
        text = ""
        err = ""
        for line in (so or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                if "error" in line.lower():
                    err = line[:200]
                continue
            if o.get("type") == "result":
                text = str(o.get("finalText") or "")
                if str(o.get("subtype") or "") == "error":
                    err = str(o.get("error") or "error")[:200]
        kind = "ok" if (rc == 0 and text) else "failed"
        if err and _is_quota_msg(err):
            kind = "quota"
        out["models"][m] = {"kind": kind, "ms": dt, "msg": (err or text)[:160],
                            "rc": rc}
    return out


def load_key_cc() -> str:
    """读 ``.env`` 里的 command-code key（**只读，不打印**）。"""
    try:
        for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("COMMAND_CODE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return os.environ.get("COMMAND_CODE_API_KEY", "")





# ------------------------------------------------------------------ Devin
def probe_devin() -> dict:
    """**真调一次 Devin 的免费档**（看额度/可用性）。

    ★ 只调**表里那档**（``swe-2-max``）—— 候选表是唯一事实源，不擅自换模型。
    ★ 用**独立临时目录**⇒ 不碰任何工作目录/生产数据。
    """
    import subprocess
    import tempfile

    out: dict = {"endpoint": "wb-devin", "ok": True, "models": {}}
    try:
        from src.api.wb_exec import config as wb_cfg
        cfg = wb_cfg.load()
    except Exception as exc:  # noqa: BLE001
        return {"endpoint": "wb-devin", "ok": False,
                "error": f"读配置失败：{exc}"[:200], "models": {}}
    dev = cfg.executor("wb-devin")
    if dev is None or not getattr(dev, "devin_cli_exe", ""):
        return {"endpoint": "wb-devin", "ok": True, "models": {},
                "note": "没配 wb-devin 执行体"}
    model = ""
    for c in cfg.candidates:
        if c.executor == "wb-devin":
            model = c.model
            break
    model = model or dev.devin_model
    work = tempfile.mkdtemp(prefix="devin_quota_probe_")
    prompt = os.path.join(work, "prompt.md")
    with open(prompt, "w", encoding="utf-8") as fh:
        fh.write("在当前工作目录下创建文件 probe.txt，内容写 devin-ok，"
                 "然后读回确认。只做这一件事。")
    argv = [dev.devin_cli_exe, "-p", "--prompt-file", prompt,
            "--export", os.path.join(work, "session.json"),
            "--permission-mode", "bypass", "--model", model,
            "--respect-workspace-trust", "false", "--verbosity", "1"]
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=work, capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
        rc, so, se = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        rc, so, se = -1, "", "超时(300s)"
    dt = int((time.time() - t0) * 1000)
    blob = f"{se}\n{so}"
    low = blob.lower()
    quota = any(w in low for w in ("quota exceeded", "exceeded your quota",
                                   "insufficient credits", "free usage limit",
                                   "rate limit exceeded", "额度用完", "配额用完"))
    # ★ **Devin 的 `-p` 默认输出是纯文本**（不是 NDJSON！）⇒ 正文就是 stdout 原文。
    text = (so or "").strip()
    # ★ 成没成：rc==0 且（stdout 有正文 或 ATIF 有正文 或 产物文件落地）
    atif_ok = False
    try:
        atif = json.loads(open(os.path.join(work, "session.json"),
                               encoding="utf-8", errors="replace").read())
        atif_ok = bool(str(atif.get("final_metrics") or "") != "" or
                       bool(atif.get("steps")))
    except Exception:  # noqa: BLE001
        atif_ok = False
    produced = os.path.exists(os.path.join(work, "probe.txt"))
    ok = (rc == 0) and bool(text or atif_ok or produced)
    out["models"][model] = {
        "kind": "quota" if quota else ("ok" if ok else "failed"),
        "ms": dt, "rc": rc,
        "msg": (se[-160:] if se else text[:160]).strip(),
        "produced": produced,
    }
    # ★ **探完自清理**：把这次探测自己留下的 Devin 会话删掉
    #   （否则临时目录不在清理白名单里 ⇒ 定时任务扫不到 ⇒ 越攒越多）。
    try:
        sid = ""
        with open(os.path.join(work, "session.json"), encoding="utf-8",
                  errors="replace") as fh:
            sid = str((json.loads(fh.read()) or {}).get("session_id") or "")
        if sid:
            subprocess.run([dev.devin_cli_exe, "rm", sid, "--force"], cwd=work,
                           capture_output=True, text=True, timeout=120)
            out["models"][model]["session_cleaned"] = sid
    except Exception as exc:  # noqa: BLE001 清不掉不影响结论
        out["models"][model]["cleanup_error"] = str(exc)[:80]
    return out

# ---------------------------------------------------------------------- 主流程
def main() -> int:
    print(f"=== 探测各执行体的免费模型 / 额度状态（{now_iso()}）===")
    reports: dict[str, dict] = {}

    for tag, root, db in (
            ("wb-cn", pathlib.Path(os.environ.get("USERPROFILE", "")) / ".workbuddy",
             pathlib.Path(os.environ.get("USERPROFILE", "")) / ".workbuddy" / "workbuddy.db"),
            ("wb-intl", pathlib.Path(os.environ.get("USERPROFILE", "")) / ".workbuddy-ai",
             pathlib.Path(os.environ.get("USERPROFILE", "")) / ".workbuddy-ai" / "workbuddy.db"),
    ):
        rep = probe_workbuddy(tag, root)
        rep["spend"] = probe_spend(tag, db)
        reports[tag] = rep
        print(f"\n--- {tag} ---")
        print(f"  免费促销：{rep.get('free_promotions')}")
        print(f"  Pro Only（该账号用不了）：{rep.get('pro_only')}")
        print(f"  最便宜可用档：{rep.get('cheapest_usable')}")
        print(f"  真实扣费：{rep.get('spend', {}).get('by_model')}")

    print("\n--- wb-oc（opencode · 走 opencode 本身，内置 provider）---")
    oc = probe_opencode()
    reports["wb-oc"] = oc
    if not oc.get("ok"):
        print(f"  探测失败：{oc.get('error')}")
    for m, v in (oc.get("models") or {}).items():
        print(f"  {m:<32} {v.get('kind','?'):<7} {v.get('ms',0):>6}ms  {str(v.get('msg'))[:70]}")

    # ------------------------------------------------------------ 写状态文件
    from src.api.wb_exec import quota_state

    exhausted: dict[str, dict] = {}
    for tag in ("wb-cn", "wb-intl"):
        rep = reports.get(tag) or {}
        # 免费促销**已到期** ⇒ 该档不再算免费（但它不是"额度用完"，先只记录不屏蔽）
        for pm in rep.get("free_promotions") or []:
            vu = str(pm.get("validUntil") or "")
            if vu and vu < now_iso()[:10]:
                for m in pm.get("modelIds") or []:
                    exhausted[f"{tag}:{m}"] = {
                        "reason": f"免费促销已于 {vu[:10]} 到期",
                        "at": now_iso(), "kind": "promo_expired"}
    # ★ **只有上游明确说"额度用完了"才标记**（2026-11-04 修正）：
    #   早先这里是"HTTP 403/429 或文案含 unavailable" ⇒ **裸直连的 400**
    #   （Zen 免费档只允许从 opencode 内部调 ⇒ 裸直连必被拒）也被当成
    #   "额度用完" ⇒ **把还活着的 flint-40b-free 误摘掉**（优先级第二的执行体！）。
    # ⇒ 现在只认 **kind == "quota"**（上游原文命中额度措辞）。
    for m, v in (oc.get("models") or {}).items():
        if v.get("kind") == "quota":
            exhausted[f"wb-oc:{m}"] = {
                "reason": f"上游报额度用完：{str(v.get('msg'))[:120]}",
                "at": now_iso(), "kind": "free_tier_limit"}

    free_models = {
        tag: sorted(set((reports.get(tag) or {}).get("free_models") or []))
        for tag in ("wb-cn", "wb-intl")
    }
    free_models["wb-oc"] = sorted(
        m for m, v in (oc.get("models") or {}).items() if v.get("kind") == "ok")

    # ---- command-code（2026-11-04 加）：逐个真调免费档
    print("\n--- wb-cc（command-code · 真调免费档）---")
    cc = probe_command_code()
    reports["wb-cc"] = cc
    if not cc.get("ok"):
        print(f"  探测失败：{cc.get('error')}")
    for m, v in (cc.get("models") or {}).items():
        print(f"  {m:<44} {v.get('kind','?'):<7} {v.get('ms',0):>6}ms  "
              f"{str(v.get('msg'))[:60]}")
    # ★ 只有上游明确说"额度用完了"才标记（同 opencode 那条纪律）
    for m, v in (cc.get("models") or {}).items():
        if v.get("kind") == "quota":
            exhausted[f"wb-cc:{m}"] = {
                "reason": f"上游报额度用完：{str(v.get('msg'))[:120]}",
                "at": now_iso(), "kind": "free_tier_limit"}
    free_models["wb-cc"] = sorted(
        m for m, v in (cc.get("models") or {}).items() if v.get("kind") == "ok")

    # ---- Devin（真调一次）
    print("\n--- wb-devin（真调一次免费档）---")
    dv = probe_devin()
    reports["wb-devin"] = dv
    for m, v in (dv.get("models") or {}).items():
        print(f"  {m:<24} {v.get('kind','?'):<7} {v.get('ms',0):>6}ms  "
              f"{str(v.get('msg'))[:70]}")
        if v.get("kind") == "quota":
            exhausted[f"wb-devin:{m}"] = {
                "reason": f"上游报额度用完：{str(v.get('msg'))[:120]}",
                "at": now_iso(), "kind": "free_tier_limit"}
    if not dv.get("models"):
        print(f"  （未探测：{dv.get('error') or dv.get('note') or '无模型'}）")

    path = quota_state.write_state(exhausted, extra={
        "free_models": free_models,
        "reports": reports,
        "note": "本文件由 scripts/check_exec_quota.py 生成（每 12 小时自动跑）。"
                "bridge 读它 ⇒ 额度/免费状态一变，后续就不再调那些档。",
    })
    print(f"\n★ 状态已写入 {path}")
    print(f"  标记为不可用：{list(exhausted)}")
    print(f"  各端点当前免费档：{free_models}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
