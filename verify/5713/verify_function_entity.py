#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_function_entity.py
=====================
独立验收器：从系统外部核对「function 实体化」六条契约是否真的达成。

原则：
- 不 import src.*，不猜内部函数签名 —— 只依赖两条外部通道：
  1) 直连 sqlite（mode=ro，只读）：<sandbox_root>/works/<work_id>/cards.db
     （sandbox_root 从 http://127.0.0.1:5713/__instance__ 动态取，不写死路径）
  2) HTTP API：http://127.0.0.1:5713/api/...
- 只读：绝不 INSERT / UPDATE / DELETE / 建表。
- 六项检查各自独立报 PASS / FAIL / SKIP，并打印实测值。
  SKIP 只用于「表还不存在，无从检查」这一种情况。

用法：python verify_function_entity.py
退出码：全 PASS 或 全 PASS+SKIP -> 0；有 FAIL -> 1。
产物：同目录 evidence_function_entity.json（每项 name/verdict/observed）。
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error

INSTANCE_URL = "http://127.0.0.1:5713"

FUNCTION_TABLES = [
    "function_profiles",
    "function_verdicts",
    "function_alarms",
    "function_sync",
    "function_charter_history",
]

ROLE_TABLES = [
    "role_profiles",
    "role_verdicts",
    "role_alarms",
    "role_sync",
    "role_charter_history",
]

REQUIRED_PROFILE_COLS = ["function_name", "charter"]

FN_RE = re.compile(r"^fn_\d{3}$")
WORK_RE = re.compile(r"^work_\d+$")


# ---------------------------------------------------------------- HTTP 通道

def http_get(path, timeout=15):
    """GET JSON。返回 (status, parsed_or_text)。任何异常都不抛，转成 status=-1。"""
    url = INSTANCE_URL + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return -1, "HTTP 错误: %s" % (e,)


def get_sandbox_root():
    """从 __instance__ 取 sandbox_root 字段（动态，不写死路径）。"""
    status, data = http_get("/__instance__")
    if status != 200 or not isinstance(data, dict):
        sys.stderr.write("FATAL: 无法从 %s/__instance__ 获取实例信息 (status=%s, data=%r)\n"
                         % (INSTANCE_URL, status, data))
        sys.exit(2)
    sb = data.get("sandbox_root")
    if not sb:
        sys.stderr.write("FATAL: __instance__ 无 sandbox_root 字段: %r\n" % (data,))
        sys.exit(2)
    return sb


# ---------------------------------------------------------------- sqlite 通道

def open_db_ro(sandbox_root, work_id):
    path = os.path.join(sandbox_root, "works", work_id, "cards.db")
    if not os.path.exists(path):
        raise FileNotFoundError("cards.db 不存在: %s" % (path,))
    conn = sqlite3.connect("file:%s?mode=ro" % (path,), uri=True)
    return conn, path


def list_works(sandbox_root):
    works_dir = os.path.join(sandbox_root, "works")
    if not os.path.isdir(works_dir):
        return []
    out = []
    for name in sorted(os.listdir(works_dir)):
        if WORK_RE.match(name):
            if os.path.exists(os.path.join(works_dir, name, "cards.db")):
                out.append(name)
    return out


def table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(r[0]) for r in rows}


def scan_function_tables(conn):
    """扫所有表名含 function 的表，在列名以 id 结尾的列里找 fn_\\d{3} 值。
    返回 (function_tables, [(table, column, value), ...])"""
    tables = [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND lower(name) LIKE '%function%'"
    ).fetchall()]
    found = []
    for t in tables:
        try:
            cols = [str(c[1]) for c in conn.execute("PRAGMA table_info(\"%s\")" % (t,))]
        except Exception:
            continue
        id_cols = [c for c in cols if c.lower().endswith("id")]
        for ic in id_cols:
            try:
                rows = conn.execute("SELECT \"%s\" FROM \"%s\"" % (ic, t)).fetchall()
            except Exception:
                continue
            for (v,) in rows:
                if v is None:
                    continue
                s = str(v).strip()
                if FN_RE.match(s):
                    found.append((t, ic, s))
    return tables, found


# ---------------------------------------------------------------- 六项检查

def check_A(conn):
    """A 号段：库里是否存在形如 fn_\\d{3} 的 id（扫 function 表、id 列）。"""
    tables, found = scan_function_tables(conn)
    if not tables:
        return "SKIP", "无 function_* 表，无从检查号段"
    if found:
        ids = sorted({v for _, _, v in found})
        detail = "找到 %d 个 fn_ id: %s（来源: %s）" % (
            len(ids), ", ".join(ids),
            ", ".join("%s.%s" % (t, c) for t, c, _ in found))
        return "PASS", detail
    return "FAIL", "function 表存在（%s）但未找到任何 fn_\\d{3} 号段 id" % ", ".join(tables)


def check_B(conn):
    """B 五张表存在：sqlite_master 里五张 function 表是否都在。"""
    tabs = table_names(conn)
    present = [t for t in FUNCTION_TABLES if t in tabs]
    missing = [t for t in FUNCTION_TABLES if t not in tabs]
    if not missing:
        return "PASS", "五张表都在: %s" % ", ".join(present)
    return "FAIL", "缺失 %d 张: %s；已有: %s" % (
        len(missing), ", ".join(missing), ", ".join(present) if present else "无")


def check_C(conn):
    """C function_profiles 有必需列：function_name 与 charter。"""
    tabs = table_names(conn)
    if "function_profiles" not in tabs:
        return "SKIP", "function_profiles 表不存在，无从检查列"
    try:
        cols = [str(c[1]) for c in conn.execute("PRAGMA table_info(\"function_profiles\")")]
    except Exception as e:
        return "FAIL", "读取 function_profiles 列失败: %s" % (e,)
    missing = [c for c in REQUIRED_PROFILE_COLS if c not in cols]
    if not missing:
        return "PASS", "含必需列: %s（全部列: %s）" % (", ".join(REQUIRED_PROFILE_COLS), ", ".join(cols))
    return "FAIL", "缺列: %s（现有列: %s）" % (", ".join(missing), ", ".join(cols))


def check_D(conn, main_work):
    """D 不进卡片世界：每个 fn_ id 不在 cards 表、不在 canvas 列表。"""
    tables, found = scan_function_tables(conn)
    if not tables:
        return "SKIP", "无 function_* 表，无从检查"
    fn_ids = sorted({v for _, _, v in found})
    if not fn_ids:
        return "PASS", "无 fn_ id 存在（空集无违例对象）"

    # (a) cards 表核对
    cards_hits = []
    try:
        for fid in fn_ids:
            n = conn.execute("SELECT count(*) FROM cards WHERE card_id=?", (fid,)).fetchone()[0]
            if n > 0:
                cards_hits.append((fid, n))
    except Exception as e:
        return "FAIL", "核对 cards 表失败: %s" % (e,)

    # (b) canvas 核对
    status, canvas = http_get("/api/works/%s/canvas" % (main_work,))
    canvas_hits = []
    if status != 200 or not isinstance(canvas, dict):
        return "FAIL", "canvas API 不可用(status=%s)，无法核对 canvas 不含 fn_ id" % (status,)
    canvas_ids = set()
    for el in canvas.get("cards", []) or []:
        cid = el.get("card_id")
        if cid:
            canvas_ids.add(str(cid))
    canvas_hits = [fid for fid in fn_ids if fid in canvas_ids]

    if not cards_hits and not canvas_hits:
        return "PASS", "%d 个 fn_ id 均不在 cards 表与 canvas（fn ids: %s）" % (len(fn_ids), ", ".join(fn_ids))
    parts = []
    if cards_hits:
        parts.append("在 cards 表中: " + ", ".join("%s(x%d)" % (f, n) for f, n in cards_hits))
    if canvas_hits:
        parts.append("在 canvas 中: " + ", ".join(canvas_hits))
    return "FAIL", "; ".join(parts)


def check_E(sandbox_root, main_work):
    """E 存量 work 不被破坏：连接前后 cards 行数不变、原有表一个不少。"""
    db_path = os.path.join(sandbox_root, "works", main_work, "cards.db")

    def snapshot():
        conn = sqlite3.connect("file:%s?mode=ro" % (db_path,), uri=True)
        try:
            n = conn.execute("SELECT count(*) FROM cards").fetchone()[0]
            tabs = table_names(conn)
        finally:
            conn.close()
        return n, tabs

    try:
        n0, t0 = snapshot()
    except Exception as e:
        return "FAIL", "连接前统计失败: %s" % (e,)

    # 找一张存在的卡片用于触发连接
    trigger_card = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % (db_path,), uri=True)
        row = conn.execute("SELECT card_id FROM cards ORDER BY rowid LIMIT 1").fetchone()
        conn.close()
        trigger_card = row[0] if row else None
    except Exception as e:
        return "FAIL", "读取触发用卡片失败: %s" % (e,)

    if not trigger_card:
        return "FAIL", "%s 的 cards 表为空，无法触发连接" % (main_work,)

    status, _ = http_get("/api/works/%s/cards/%s" % (main_work, trigger_card), timeout=20)
    time.sleep(0.5)

    try:
        n1, t1 = snapshot()
    except Exception as e:
        return "FAIL", "连接后统计失败: %s" % (e,)

    missing = sorted(t0 - t1)
    ok = (n0 == n1) and not missing
    observed = "work=%s 触发GET /api/works/%s/cards/%s (status=%s)；cards行数 before=%d after=%d；表 before=%d after=%d；缺失原表=%s" % (
        main_work, main_work, trigger_card, status, n0, n1, len(t0), len(t1),
        ", ".join(missing) if missing else "无")
    if ok:
        return "PASS", observed
    return "FAIL", observed


def check_F(conn):
    """F 旧 role_* 仍在：五张 role 表是否都在（至少 role_profiles）。"""
    tabs = table_names(conn)
    present = [t for t in ROLE_TABLES if t in tabs]
    missing = [t for t in ROLE_TABLES if t not in tabs]
    if not missing:
        return "PASS", "五张 role_* 表都在: %s" % ", ".join(present)
    return "FAIL", "缺失 %d 张: %s；已有: %s" % (
        len(missing), ", ".join(missing), ", ".join(present) if present else "无")


# ---------------------------------------------------------------- 主流程

def main():
    sb = get_sandbox_root()
    works = list_works(sb)
    if not works:
        sys.stderr.write("FATAL: sandbox_root(%s) 下无任何存量 work\n" % (sb,))
        sys.exit(2)
    # 主检查 work：优先 work_001（存量代表），否则取第一个存量 work
    main_work = "work_001" if "work_001" in works else works[0]

    conn, db_path = open_db_ro(sb, main_work)

    checks = [
        ("A 号段", check_A(conn)),
        ("B 五张表存在", check_B(conn)),
        ("C profiles必需列", check_C(conn)),
        ("D 不进卡片世界", check_D(conn, main_work)),
        ("E 存量work不被破坏", check_E(sb, main_work)),
        ("F 旧role_*仍在", check_F(conn)),
    ]
    conn.close()

    # 打印
    n_pass = n_fail = n_skip = 0
    items = []
    for name, (verdict, observed) in checks:
        print("[%s] %s：实测=%s" % (verdict, name, observed))
        if verdict == "PASS":
            n_pass += 1
        elif verdict == "FAIL":
            n_fail += 1
        elif verdict == "SKIP":
            n_skip += 1
        items.append({"name": name, "verdict": verdict, "observed": observed})

    print("PASS %d / FAIL %d / SKIP %d" % (n_pass, n_fail, n_skip))

    # evidence 落盘
    evidence = {
        "work_id": main_work,
        "sandbox_root": sb,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "items": items,
        "summary": {"pass": n_pass, "fail": n_fail, "skip": n_skip},
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "evidence_function_entity.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)

    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
