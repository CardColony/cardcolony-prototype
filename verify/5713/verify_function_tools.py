#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_function_tools.py
========================
独立验收器：从**系统外部**核对「function 第二步」契约是否真的达成。

本脚本**不实现功能、不改实现**——只做检查，让验收不由实现方自证。

【取数纪律】（硬约束，逐条遵守）
1. import src.* 只为取**只读事实**：注册表（TOOL_REGISTRY / TOOL_SWITCH）、
   上下文区注册表（REGION_TABLE）、模块是否可 import。**不调 handler、
   不调 storage 写函数**。
2. 数据库一律 `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`——**只读**，
   绝不 INSERT / UPDATE / DELETE / 建表。
3. sandbox_root 从 `http://127.0.0.1:5713/__instance__` 动态取，**不写死路径**。
4. HTTP 端点一律 `http://127.0.0.1:5713/api/...`。

【八项检查】
A 旧工具全消失      —— TOOL_REGISTRY 不含五个旧名
B 新工具齐备        —— TOOL_REGISTRY / TOOL_SWITCH 含五个新名
C 常量改名          —— FUNCTION_ONLY_TOOLS 在、ROLE_ONLY_TOOLS 不在
D fn_ 身份且不进 cards —— 在**新建 work** 上真建一个职能并三方核对
E function_roster 区（order 93）—— 根卡有数据出区／空清单空串／非根卡空串
F function_self  区（order 145）—— function_meta 有值出区且含职责书原文与
                                  「不在任务树」意思的句子；空值空串且不抛异常
G 配置版本          —— context_regions.json 升到 context-regions-v9 且 93/145 enabled
H 旧路径未破        —— role_* 五张表仍在；role_store / role_room 仍可 import

【输出】
· 每项一行：`[PASS|FAIL|SKIP] A 旧工具消失：实测=...`
· 末尾 `PASS n / FAIL n / SKIP n`
· 落 `verify/5713/evidence_function_tools.json`（每项 name/verdict/observed）

【退出码】全 PASS 或 PASS+SKIP → 0；有 FAIL → 1。

【纪律】不把 FAIL 改成 SKIP 来让脚本"绿"。SKIP 只用于「该项在外部通道上
确实无法观测」并写明缺什么通道。

用法：python verify/5713/verify_function_tools.py
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]          # 仓库根（.worktrees/5713）
INSTANCE_URL = "http://127.0.0.1:5713"
OUT_PATH = Path(__file__).resolve().parent / "evidence_function_tools.json"

OLD_TOOLS = (
    "create_role",
    "list_roles",
    "role_watch_cards",
    "update_role_profile",
    "role_set_duty",
)
NEW_TOOLS = (
    "create_function",
    "list_functions",
    "function_watch_cards",
    "update_function_profile",
    "set_function_duty",
)

# role_* 核心五张表（与 role_store 一致；role_directives 属额外表，只作参考）
ROLE_TABLES = (
    "role_profiles",
    "role_verdicts",
    "role_alarms",
    "role_sync",
    "role_charter_history",
)

FN_RE = re.compile(r"^fn_\d{3}$")
WORK_RE = re.compile(r"^work_\d+$")

# 哨兵：用来判「渲染文本里确实出现了职责书原文 / 职能名」
SENTINEL_NAME = "验收哨兵职能甲"
SENTINEL_CHARTER = "验收哨兵职责书·唯一串ZZY77（职责书原文判据）"

# D 项：新建 work 的标题与待建职能名
D_WORK_TITLE = "验收器自建·function_tools 核查"
D_FUNCTION_NAME = "验收器自建职能乙"


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def _reconfigure_stdio() -> None:
    """控制台可能是 GBK 码页——强制 UTF-8 且遇不可编码字符不抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def http_request(method: str, path: str, payload=None, timeout: int = 30):
    """HTTP 调用。返回 (status, parsed_or_text)。任何异常都不抛，转成 status=-1。"""
    url = INSTANCE_URL + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except Exception:  # noqa: BLE001
                return r.status, body
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except Exception as e:  # noqa: BLE001
        return -1, "HTTP 错误: %s" % (e,)


def http_get(path: str, timeout: int = 30):
    return http_request("GET", path, None, timeout)


def http_post(path: str, payload, timeout: int = 60):
    return http_request("POST", path, payload, timeout)


def get_sandbox_root() -> str:
    """从 __instance__ 动态取 sandbox_root（不写死路径）。失败即 FATAL 退出。"""
    status, data = http_get("/__instance__")
    if status != 200 or not isinstance(data, dict):
        sys.stderr.write("FATAL: 无法从 %s/__instance__ 取实例信息 (status=%s, data=%r)\n"
                         % (INSTANCE_URL, status, data))
        sys.exit(2)
    sb = data.get("sandbox_root")
    if not sb:
        sys.stderr.write("FATAL: __instance__ 无 sandbox_root 字段: %r\n" % (data,))
        sys.exit(2)
    return str(sb)


def db_path_for(sandbox_root: str, work_id: str) -> str:
    """定位某 work 的 cards.db：优先 sandbox_root 下，回退仓库 works/ 下。"""
    cand = Path(sandbox_root) / "works" / work_id / "cards.db"
    if cand.exists():
        return str(cand)
    alt = ROOT / "works" / work_id / "cards.db"
    if alt.exists():
        return str(alt)
    return str(cand)


def open_db_ro(path: str):
    """只读连接（mode=ro）——绝不写。"""
    return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)


def table_names(conn) -> set:
    return {str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def list_works(sandbox_root: str) -> list:
    """列出 sandbox 下带 cards.db 的 work（只读目录扫描）。"""
    out = []
    for base in (Path(sandbox_root) / "works", ROOT / "works"):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if WORK_RE.match(p.name) and (p / "cards.db").exists():
                if p.name not in out:
                    out.append(p.name)
    return out


# --------------------------------------------------------------------------
# 只读事实：注册表 / 区表
# --------------------------------------------------------------------------

def load_facts():
    """取三份只读事实：card_tools（注册表+常量）、context_regions（区表）。

    只 import、只读属性；不调任何 handler / storage 写函数。
    失败不抛——返回 (facts, errors) 供各检查项自行判 FAIL/SKIP。
    """
    facts = {}
    errors = []
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        card_tools = importlib.import_module("src.api.card_tools")
        facts["card_tools"] = card_tools
        facts["TOOL_REGISTRY"] = dict(getattr(card_tools, "TOOL_REGISTRY", {}) or {})
        facts["TOOL_SWITCH"] = dict(getattr(card_tools, "TOOL_SWITCH", {}) or {})
    except Exception as e:  # noqa: BLE001
        errors.append("import src.api.card_tools 失败: %s: %s" % (type(e).__name__, e))
    try:
        context_regions = importlib.import_module("src.api.context_regions")
        facts["context_regions"] = context_regions
        facts["REGION_TABLE"] = list(getattr(context_regions, "REGION_TABLE", []) or [])
        facts["RegionCtx"] = getattr(context_regions, "RegionCtx", None)
    except Exception as e:  # noqa: BLE001
        errors.append("import src.api.context_regions 失败: %s: %s" % (type(e).__name__, e))
    facts["errors"] = errors
    return facts


def spec_by_key(region_table, key: str):
    for spec in region_table:
        if str(spec.get("key") or "") == key:
            return spec
    return None


def render_region(context_regions, region_table, key: str, ctx) -> str:
    """渲染一个区：优先走 REGION_TABLE 里该区的 render 回调（注册表公开入口），
    回退到模块级 render_{key}_region。异常照抛给调用方判定。"""
    spec = spec_by_key(region_table, key)
    if spec is None:
        raise KeyError("REGION_TABLE 无 key=%s" % key)
    fn = spec.get("render")
    if callable(fn):
        return fn(ctx)
    mod_fn = getattr(context_regions, "render_%s_region" % key, None)
    if callable(mod_fn):
        return mod_fn(ctx)
    raise RuntimeError("key=%s 既无 render 回调也无 render_%s_region 函数" % (key, key))


def make_ctx(RegionCtx, **overrides):
    """构造 RegionCtx（离线，不碰 IO）。返回 (ctx, 缺失字段名列表)。"""
    fields = {f.name for f in dataclasses.fields(RegionCtx)}
    missing = [k for k in overrides if k not in fields]
    kwargs = {k: v for k, v in overrides.items() if k in fields}
    return RegionCtx(**kwargs), missing


# --------------------------------------------------------------------------
# A / B / C：工具与常量
# --------------------------------------------------------------------------

def check_A(facts):
    name = "A 旧工具消失"
    if "TOOL_REGISTRY" not in facts:
        return "FAIL", name, "无法取 TOOL_REGISTRY（%s）" % "; ".join(facts["errors"])
    registry = facts["TOOL_REGISTRY"]
    residual = [t for t in OLD_TOOLS if t in registry]
    if residual:
        return "FAIL", name, ("TOOL_REGISTRY 仍含旧工具 %d 个：%s；注册表共 %d 件"
                              % (len(residual), ", ".join(residual), len(registry)))
    return "PASS", name, "TOOL_REGISTRY 无旧工具残留（查 %d 个旧名，注册表共 %d 件）" % (
        len(OLD_TOOLS), len(registry))


def check_B(facts):
    name = "B 新工具齐备"
    if "TOOL_REGISTRY" not in facts:
        return "FAIL", name, "无法取 TOOL_REGISTRY（%s）" % "; ".join(facts["errors"])
    registry = facts["TOOL_REGISTRY"]
    switch = facts["TOOL_SWITCH"]
    miss_reg = [t for t in NEW_TOOLS if t not in registry]
    miss_sw = [t for t in NEW_TOOLS if t not in switch]
    old_in_sw = [t for t in OLD_TOOLS if t in switch]
    if miss_reg or miss_sw or old_in_sw:
        parts = []
        if miss_reg:
            parts.append("TOOL_REGISTRY 缺新工具 %d 个：%s" % (len(miss_reg), ", ".join(miss_reg)))
        if miss_sw:
            parts.append("TOOL_SWITCH 缺新条目 %d 个：%s" % (len(miss_sw), ", ".join(miss_sw)))
        if old_in_sw:
            parts.append("TOOL_SWITCH 仍挂旧名 %d 个：%s" % (len(old_in_sw), ", ".join(old_in_sw)))
        return "FAIL", name, "；".join(parts)
    return "PASS", name, ("TOOL_REGISTRY 与 TOOL_SWITCH 均含五个新工具（%s），"
                          "且无旧名残留；注册表共 %d 件"
                          % (", ".join(NEW_TOOLS), len(registry)))


def check_C(facts):
    name = "C 常量改名"
    ct = facts.get("card_tools")
    if ct is None:
        return "FAIL", name, "无法取 card_tools 模块（%s）" % "; ".join(facts["errors"])
    has_new = hasattr(ct, "FUNCTION_ONLY_TOOLS")
    has_old = hasattr(ct, "ROLE_ONLY_TOOLS")
    if not has_new:
        return "FAIL", name, "card_tools 无 FUNCTION_ONLY_TOOLS（常量未改名）"
    new_val = getattr(ct, "FUNCTION_ONLY_TOOLS")
    if not has_old:
        return "PASS", name, "FUNCTION_ONLY_TOOLS 存在（%d 项），ROLE_ONLY_TOOLS 已消失" % (
            len(set(new_val) or []),)
    old_val = getattr(ct, "ROLE_ONLY_TOOLS")
    if set(old_val or []) == set(new_val or []):
        return "SKIP", name, ("ROLE_ONLY_TOOLS 仍存在但与 FUNCTION_ONLY_TOOLS 取值完全相同"
                             "（判定为保留别名）：旧=%s 新=%s"
                             % (sorted(set(old_val or [])), sorted(set(new_val or []))))
    return "FAIL", name, ("ROLE_ONLY_TOOLS 仍存在且与 FUNCTION_ONLY_TOOLS 不等价"
                          "（非纯别名）：旧=%s 新=%s"
                          % (sorted(set(old_val or [])), sorted(set(new_val or []))))


# --------------------------------------------------------------------------
# D：fn_ 身份且不进 cards
# --------------------------------------------------------------------------

def _extract_fn_id(payload):
    """从建职能响应里找 fn_ 形式的 id（多候选字段名）。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("function_id", "id", "card_id", "fn_id"):
        val = payload.get(key)
        if isinstance(val, str) and FN_RE.match(val.strip()):
            return val.strip()
    # 嵌一层 {"function": {...}} / {"profile": {...}}
    for key in ("function", "profile", "result", "data"):
        sub = payload.get(key)
        if isinstance(sub, dict):
            got = _extract_fn_id(sub)
            if got:
                return got
    return ""


def discover_function_paths(work_id: str) -> list:
    """从 openapi **动态发现**「建职能」的候选端点（只取 POST），再补常见命名兜底。

    动态发现是必须的：实现方可能把端点叫 /functions、/functions/create、/fns…
    写死路径会在实现方换名时误报「外部无通道」。
    """
    cands = []
    st, oa = http_get("/openapi.json")
    if st == 200 and isinstance(oa, dict):
        for p, spec in (oa.get("paths") or {}).items():
            low = str(p).lower()
            tail = low.rstrip("/").rsplit("/", 1)[-1]
            if "function" not in low and tail not in ("fn", "fns"):
                continue
            methods = [str(m).lower() for m in spec.keys()] if isinstance(spec, dict) else []
            if "post" not in methods:
                continue
            cands.append(str(p).replace("{work_id}", work_id))
    for suffix in ("/functions", "/functions/create", "/function", "/fns"):
        cands.append("/api/works/%s%s" % (work_id, suffix))
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _latest_fn_id_from_db(sandbox_root: str, work_id: str, function_name: str) -> str:
    """只读核对：查该 work 的 function_profiles 里 function_name 匹配的最新一条。

    用途：端点返回 2xx 但响应体没带 id 时，从**只读**库侧确认是否真建出来了
    （外部可观测事实，不写库）。
    """
    db_path = db_path_for(sandbox_root, work_id)
    try:
        conn = open_db_ro(db_path)
    except Exception:  # noqa: BLE001
        return ""
    try:
        if "function_profiles" not in table_names(conn):
            return ""
        row = conn.execute(
            "SELECT function_id FROM function_profiles WHERE work_id=? "
            "AND function_name=? ORDER BY function_id DESC LIMIT 1",
            (work_id, function_name)).fetchone()
        return str(row[0]) if row else ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        conn.close()


def check_D(sandbox_root):
    """在**新建 work** 上真建一个职能，核对 fn_ 身份 + 不进 cards 表 + 不在 canvas。"""
    name = "D fn_ 身份且不进 cards"
    probe_log = []

    # —— 步骤1：HTTP 新建 work（外部通道，非写库直连）——
    # 幂等：按标题复用上次留下的专用 work，避免每次跑都堆一个空剧本。
    work_id = ""
    root_card_id = ""
    st, wlist = http_get("/api/works")
    if st == 200 and isinstance(wlist, dict):
        for w in (wlist.get("works") or []):
            if isinstance(w, dict) and str(w.get("title") or "") == D_WORK_TITLE:
                work_id = str(w.get("work_id") or "")
                root_card_id = str(w.get("work_card_id") or "")
                if work_id:
                    probe_log.append("复用既有 work=%s（根卡=%s）" % (work_id, root_card_id))
                    break
    if not work_id:
        st, work = http_post("/api/works", {"title": D_WORK_TITLE})
        if st not in (200, 201) or not isinstance(work, dict) or not work.get("work_id"):
            return "SKIP", name, ("无法在外部新建 work 触发核查（POST /api/works status=%s, resp=%r）"
                                  "——缺「HTTP 建 work」通道" % (st, str(work)[:200]))
        work_id = str(work.get("work_id"))
        root_card_id = str(work.get("work_card_id") or "")
        probe_log.append("新建 work=%s（根卡=%s）" % (work_id, root_card_id))

    # —— 步骤2：探测「建职能」的外部通道 ——
    # 端点**从 openapi 动态发现**（实现方可能换名），再补常见命名兜底。
    # 无 execute_tool 之类的工具面外部触发口 ⇒ 若 HTTP 也不存在，本项 SKIP。
    paths = discover_function_paths(work_id)
    bodies = [
        {"name": D_FUNCTION_NAME},
        {"function_name": D_FUNCTION_NAME},
        {"title": D_FUNCTION_NAME, "charter": SENTINEL_CHARTER},
        {"function_name": D_FUNCTION_NAME, "charter": SENTINEL_CHARTER},
    ]
    created_id = ""
    hit_path = ""
    for path in paths:
        for body in bodies:
            st2, resp = http_post(path, body)
            probe_log.append("POST %s body=%s → status=%s resp=%s"
                             % (path, sorted(body.keys()), st2, str(resp)[:120]))
            if st2 in (200, 201):
                got = _extract_fn_id(resp)
                if got:
                    created_id = got
                    hit_path = path
                    break
                # 2xx 但响应体没带 id ⇒ 只读回查库里是否真建出来了
                got = _latest_fn_id_from_db(sandbox_root, work_id, D_FUNCTION_NAME)
                if got:
                    created_id = got
                    hit_path = path
                    probe_log.append("响应体无 id，但只读库查到 function_profiles 有 %s" % got)
                    break
        if created_id:
            break

    if not created_id:
        return "SKIP", name, (
            "外部无「建职能」触发通道，缺什么：HTTP 侧需有建职能端点"
            "（openapi 动态发现＋常见命名兜底共探 %d 条路径：%s），"
            "或提供可从外部触发模型工具 create_function 的执行口"
            "（当前 openapi 无 execute_tool 类端点）。探测记录：%s"
            % (len(paths), ", ".join(paths), " | ".join(probe_log)))

    # —— 步骤3：三方核对 ——
    id_ok = bool(FN_RE.match(created_id))

    db_path = db_path_for(sandbox_root, work_id)
    cards_count = -1
    db_note = ""
    try:
        conn = open_db_ro(db_path)
        try:
            cards_count = int(conn.execute(
                "SELECT count(*) FROM cards WHERE card_id=?", (created_id,)).fetchone()[0])
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        db_note = "（cards 表核对失败: %s: %s）" % (type(e).__name__, e)

    st3, canvas = http_get("/api/works/%s/canvas" % work_id)
    canvas_hit = False
    canvas_ids = []
    if st3 == 200 and isinstance(canvas, dict):
        for el in (canvas.get("cards") or []):
            if isinstance(el, dict):
                cid = str(el.get("card_id") or "")
                if cid:
                    canvas_ids.append(cid)
                if cid == created_id:
                    canvas_hit = True
    else:
        canvas_ids = ["<canvas 不可读 status=%s>" % st3]

    ok = id_ok and cards_count == 0 and not canvas_hit and db_note == ""
    observed = ("work=%s 建出 id=%s（形如 fn_\\d{3}：%s）；cards 表命中 %s 条；"
                "canvas 命中：%s；canvas 卡片 %d 张%s"
                % (work_id, created_id, "是" if id_ok else "否",
                   cards_count, "是" if canvas_hit else "否",
                   len(canvas_ids), db_note))
    if ok:
        return "PASS", name, observed
    fail_parts = []
    if not id_ok:
        fail_parts.append("id 不符 fn_\\d{3}")
    if cards_count != 0:
        fail_parts.append("cards 表命中 %s 条（应为 0）" % cards_count)
    if canvas_hit:
        fail_parts.append("canvas 仍含该 id")
    if db_note:
        fail_parts.append(db_note.strip("（）"))
    return "FAIL", name, "；".join(fail_parts) + "｜" + observed


# --------------------------------------------------------------------------
# E / F：两个新上下文区
# --------------------------------------------------------------------------

def _root_ctx_kwargs(work_card_id="card_root_001", roster=None, function_meta=None):
    """根卡视角的 ctx 入参（同时给 cur_meta/cur/card_id 三种口径，兼容不同实现）。

    探针记录**同时带全部常见别名键**（function_id/id/name/function_name/title/
    charter/charter_text…）：验收器不该因猜错字段名而误判实现方。
    """
    kw = {
        "work_id": "work_probe",
        "card_id": work_card_id,
        "work_card_id": work_card_id,
        "cur_meta": {"card_id": work_card_id, "card_type": "project", "parent": "",
                     "project_class": "rd_project",
                     "auto_mode": 1, "state": "active"},
        "cur": {"card_id": work_card_id, "card_type": "project", "parent": "",
                "title": "探针根卡", "auto_mode": 1, "state": "active"},
        "cards": [{"card_id": work_card_id, "title": "探针根卡", "card_type": "project",
                   "parent": ""}],
    }
    if roster is not None:
        kw["function_roster"] = roster
    if function_meta is not None:
        kw["function_meta"] = function_meta
    return kw


def _probe_roster_entry():
    """一条职能记录（带全部别名键，哨兵串唯一）。"""
    return {
        "function_id": "fn_001",
        "id": "fn_001",
        "name": SENTINEL_NAME,
        "function_name": SENTINEL_NAME,
        "title": SENTINEL_NAME,
        "charter": {"text": SENTINEL_CHARTER, "version": 1},
        "charter_text": SENTINEL_CHARTER,
        "text": SENTINEL_CHARTER,
        "duty": {"state": "active"},
        "state": "active",
        "jurisdiction": {"scope": "验收域"},
        "watch": {"card_ids": []},
    }


def _probe_function_meta():
    """一个职能自己的档案（带全部别名键）。"""
    meta = _probe_roster_entry()
    meta["work_id"] = "work_probe"
    return meta


def check_E(facts):
    """E：function_roster 区（order 93）。"""
    name = "E function_roster 区"
    if "REGION_TABLE" not in facts:
        return "FAIL", name, "无法取 REGION_TABLE（%s）" % "; ".join(facts["errors"])
    table = facts["REGION_TABLE"]
    RegionCtx = facts.get("RegionCtx")
    if RegionCtx is None:
        return "FAIL", name, "无法取 RegionCtx（%s）" % "; ".join(facts["errors"])
    cr = facts.get("context_regions")

    spec = spec_by_key(table, "function_roster")
    if spec is None:
        return "FAIL", name, ("REGION_TABLE 无 key=function_roster（现有 key: %s）"
                              % ", ".join(str(s.get("key")) for s in table))
    order = spec.get("order")
    if order != 93:
        return "FAIL", name, "function_roster 的 order=%r（应为 93）" % (order,)

    # —— 字段存在性 ——
    fields = {f.name for f in dataclasses.fields(RegionCtx)}
    if "function_roster" not in fields:
        return "FAIL", name, ("order=93 已登记，但 RegionCtx 缺 function_roster 字段"
                              "（现有字段 %d 个，无该字段）——区无法取数" % len(fields))

    roster = [_probe_roster_entry()]

    # (a) 根卡 + 有数据 → 非空且含职能名
    try:
        ctx, _miss = make_ctx(RegionCtx, **_root_ctx_kwargs(roster=roster))
        text = render_region(cr, table, "function_roster", ctx)
    except Exception as e:  # noqa: BLE001
        return "FAIL", name, "根卡渲染 function_roster 抛异常: %s: %s" % (type(e).__name__, e)
    if not (text or "").strip():
        return "FAIL", name, "根卡＋有职能数据时渲染为空串（应列出职能清单）"
    if SENTINEL_NAME not in text:
        return "FAIL", name, "根卡渲染非空但不含职能名「%s」；实渲染=%r" % (
            SENTINEL_NAME, text[:200])

    # (b) 空清单 → 空串
    try:
        ctx, _miss = make_ctx(RegionCtx, **_root_ctx_kwargs(roster=[]))
        text_empty = render_region(cr, table, "function_roster", ctx)
    except Exception as e:  # noqa: BLE001
        return "FAIL", name, "空清单渲染抛异常: %s: %s" % (type(e).__name__, e)
    if (text_empty or "").strip():
        return "FAIL", name, "function_roster=[] 时渲染非空（应为空串）：%r" % text_empty[:200]

    # (c) 非根卡 → 空串（显式设 parent，兼容「按 parent 判根卡」的实现）
    try:
        kw = _root_ctx_kwargs(roster=roster)
        kw["card_id"] = "card_child_002"
        kw["cur_meta"] = {"card_id": "card_child_002", "card_type": "sub_project",
                          "parent": "card_root_001", "auto_mode": 1, "state": "active"}
        kw["cur"] = {"card_id": "card_child_002", "card_type": "sub_project",
                     "parent": "card_root_001", "title": "探针子卡", "auto_mode": 1}
        kw["cards"] = [{"card_id": "card_root_001", "title": "探针根卡",
                        "card_type": "project", "parent": ""},
                       {"card_id": "card_child_002", "title": "探针子卡",
                        "card_type": "sub_project", "parent": "card_root_001"}]
        ctx, _miss = make_ctx(RegionCtx, **kw)
        text_nonroot = render_region(cr, table, "function_roster", ctx)
    except Exception as e:  # noqa: BLE001
        return "FAIL", name, "非根卡渲染抛异常: %s: %s" % (type(e).__name__, e)
    if (text_nonroot or "").strip():
        return "FAIL", name, "非根卡渲染非空（该区只该在根卡出现）：%r" % text_nonroot[:200]

    return "PASS", name, ("order=93 已登记；根卡有数据出区且含职能名「%s」（%d 字）；"
                          "空清单出空串；非根卡出空串" % (SENTINEL_NAME, len(text)))


def _has_off_tree_sentence(text: str):
    """判「不在任务树」意思的句子：任一行同时含『树』与否定词。

    放宽口径：实现方的措辞可能是「职能不挂在你的任务树下」「不在任务树里」
    「离树观察者」等，不该因固定措辞而误判；但必须同句内同时出现
    『树』与否定词，否则「这条职责是树上的工作」这类句子会被算作命中。
    """
    marks = ["不", "非", "外", "离"]
    for line in (text or "").splitlines():
        if "树" in line and any(m in line for m in marks):
            return line.strip()
    return ""


def check_F(facts):
    """F：function_self 区（order 145）。"""
    name = "F function_self 区"
    if "REGION_TABLE" not in facts:
        return "FAIL", name, "无法取 REGION_TABLE（%s）" % "; ".join(facts["errors"])
    table = facts["REGION_TABLE"]
    RegionCtx = facts.get("RegionCtx")
    if RegionCtx is None:
        return "FAIL", name, "无法取 RegionCtx（%s）" % "; ".join(facts["errors"])
    cr = facts.get("context_regions")

    spec = spec_by_key(table, "function_self")
    if spec is None:
        return "FAIL", name, ("REGION_TABLE 无 key=function_self（现有 key: %s）"
                              % ", ".join(str(s.get("key")) for s in table))
    order = spec.get("order")
    if order != 145:
        return "FAIL", name, "function_self 的 order=%r（应为 145）" % (order,)

    fields = {f.name for f in dataclasses.fields(RegionCtx)}
    if "function_meta" not in fields:
        return "FAIL", name, ("order=145 已登记，但 RegionCtx 缺 function_meta 字段"
                              "（现有字段 %d 个，无该字段）——区无法取数" % len(fields))

    meta = _probe_function_meta()

    # (a) 有 function_meta → 非空 + 含职责书原文 + 含「不在任务树」意思
    try:
        ctx, _miss = make_ctx(RegionCtx, **_root_ctx_kwargs(function_meta=meta))
        text = render_region(cr, table, "function_self", ctx)
    except Exception as e:  # noqa: BLE001
        return "FAIL", name, "function_meta 有值时渲染抛异常: %s: %s" % (type(e).__name__, e)
    if not (text or "").strip():
        return "FAIL", name, "function_meta 有值时渲染为空串（应渲染职能自己的职责）"
    if SENTINEL_CHARTER not in text:
        return "FAIL", name, "渲染非空但不含职责书原文（哨兵串缺失）；实渲染=%r" % text[:200]
    line = _has_off_tree_sentence(text)
    if not line:
        return "FAIL", name, ("渲染不含「不在任务树」意思的句子"
                              "（未见同时含『任务树/卡树』与否定词的行）；实渲染=%r" % text[:300])

    # (b) 空 function_meta → 空串且不抛异常
    try:
        ctx, _miss = make_ctx(RegionCtx, **_root_ctx_kwargs(function_meta={}))
        text_empty = render_region(cr, table, "function_self", ctx)
    except Exception as e:  # noqa: BLE001
        return "FAIL", name, "function_meta={} 时渲染抛异常: %s: %s" % (type(e).__name__, e)
    if (text_empty or "").strip():
        return "FAIL", name, "function_meta={} 时渲染非空（应为空串）：%r" % text_empty[:200]

    return "PASS", name, ("order=145 已登记；有 function_meta 时出区且含职责书原文哨兵、"
                          "并含「不在任务树」意思句：「%s」；空 function_meta 出空串且不抛异常"
                          % line[:60])


# --------------------------------------------------------------------------
# G：配置版本
# --------------------------------------------------------------------------

def check_G():
    name = "G 配置版本"
    cfg_path = ROOT / "config" / "context_regions.json"
    if not cfg_path.exists():
        return "FAIL", name, "配置文件不存在: %s" % cfg_path
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return "FAIL", name, "配置文件解析失败: %s: %s" % (type(e).__name__, e)
    ver = str((cfg.get("_meta") or {}).get("version") or "")
    regions = cfg.get("regions") or {}
    problems = []
    if ver != "context-regions-v9":
        problems.append("_meta.version=%r（应为 context-regions-v9）" % ver)
    detail = []
    for key, want_order in (("function_roster", 93), ("function_self", 145)):
        item = regions.get(key)
        if not isinstance(item, dict):
            problems.append("regions 无 %s 条目" % key)
            continue
        got_order = item.get("order")
        enabled = item.get("enabled")
        if got_order != want_order:
            problems.append("%s.order=%r（应为 %d）" % (key, got_order, want_order))
        if enabled is not True:
            problems.append("%s.enabled=%r（应为 true）" % (key, enabled))
        detail.append("%s{order=%r,enabled=%r}" % (key, got_order, enabled))
    if problems:
        return "FAIL", name, "；".join(problems) + "｜版本=%s，配置条目: %s" % (
            ver, ", ".join(detail) if detail else "无")
    return "PASS", name, "_meta.version=context-regions-v9；%s 均 enabled 且 order 正确" % (
        ", ".join(detail))


# --------------------------------------------------------------------------
# H：旧路径未破
# --------------------------------------------------------------------------

def check_H(sandbox_root):
    name = "H 旧路径未破"
    works = list_works(sandbox_root)
    if not works:
        return "FAIL", name, "sandbox 下无任何带 cards.db 的 work（无法核对 role_* 表）"

    hit_work = ""
    missing_all = {}
    scanned = 0
    for wid in works:
        db_path = db_path_for(sandbox_root, wid)
        try:
            conn = open_db_ro(db_path)
            try:
                tabs = table_names(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            continue
        scanned += 1
        missing = [t for t in ROLE_TABLES if t not in tabs]
        if not missing:
            hit_work = wid
            break
        missing_all[wid] = missing
    if not hit_work:
        return "FAIL", name, ("扫了 %d 个 work 的 cards.db，五张 role_* 表从未同时齐备；"
                              "缺表样本: %s" % (scanned, json.dumps(missing_all, ensure_ascii=False)[:300]))

    db_path = db_path_for(sandbox_root, hit_work)
    conn = open_db_ro(db_path)
    try:
        tabs = table_names(conn)
        extra = sorted(t for t in tabs if t.startswith("role_") and t not in ROLE_TABLES)
    finally:
        conn.close()

    import_res = []
    for mod in ("src.storage.role_store", "src.api.role_room"):
        try:
            importlib.import_module(mod)
            import_res.append("%s: import OK" % mod)
        except Exception as e:  # noqa: BLE001
            import_res.append("%s: import 失败 %s: %s" % (mod, type(e).__name__, e))
    bad = [r for r in import_res if "失败" in r]
    if bad:
        return "FAIL", name, "五张 role_* 表齐备（work=%s），但模块 import 失败：%s" % (
            hit_work, " | ".join(bad))
    return "PASS", name, ("work=%s 的 cards.db 五张 role_* 表齐备（%s；额外 role_* 表: %s）；"
                          "%s" % (hit_work, ", ".join(ROLE_TABLES),
                                  ", ".join(extra) if extra else "无",
                                  " | ".join(import_res)))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    _reconfigure_stdio()
    sandbox_root = get_sandbox_root()
    facts = load_facts()

    # 各 check_* 统一返回 (verdict, name, observed)
    results = []
    for fn in (check_A, check_B, check_C):
        results.append(fn(facts))
    results.append(check_D(sandbox_root))
    results.append(check_E(facts))
    results.append(check_F(facts))
    results.append(check_G())
    results.append(check_H(sandbox_root))

    n_pass = n_fail = n_skip = 0
    items = []
    for verdict, name, observed in results:
        print("[%s] %s：实测=%s" % (verdict, name, observed))
        if verdict == "PASS":
            n_pass += 1
        elif verdict == "FAIL":
            n_fail += 1
        else:
            n_skip += 1
        items.append({"name": name, "verdict": verdict, "observed": observed})

    print("PASS %d / FAIL %d / SKIP %d" % (n_pass, n_fail, n_skip))

    evidence = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "instance": INSTANCE_URL,
        "sandbox_root": sandbox_root,
        "repo_root": str(ROOT),
        "items": items,
        "summary": {"pass": n_pass, "fail": n_fail, "skip": n_skip},
    }
    try:
        OUT_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print("证据已落盘: %s" % OUT_PATH)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("证据落盘失败: %s: %s\n" % (type(e).__name__, e))

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
