# -*- coding: utf-8 -*-
"""5713 边界扫描器 —— 「代码锁死开发机」与「模块私下互相依赖」的机械取证。

只读：本脚本不写业务数据、不改仓库文件（除 --out 指定的 JSON 报告）。
用法::

    cd D:\\AI_station\\推演agent\\.worktrees\\5713
    python verify/5713/boundary_scan.py --out verify/5713/boundary_scan_<sha>.json

每一项判定都落到 file:line，第三方按同一 HEAD 重跑得同一结果。
六类取证：

    C1 绝对路径字面量   —— 运行时代码里写死的开发机路径（区分「代码」与「注释/docstring」）
    C2 路径根常量清单   —— 有多少份互相独立的 BASE_DIR / WORKS_DIR / SNAPSHOT_ROOT ...
    C3 跨模块改全局     —— 启动器/测试靠 monkeypatch 别人模块的常量来搬家
    C4 导入图           —— 包级依赖矩阵、反向依赖、SCC 环、私有符号导入、导入期注册
    C5 硬编码端点       —— 端口 / 本机服务 URL
    C6 运行期解析值     —— 真正 import 一遍，看这些常量落到哪个绝对路径（子进程，不写盘）
"""
from __future__ import annotations

import argparse
import ast
import collections
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 范围

RUNTIME_PREFIXES = ("src/", "scripts/", "monitor/")
RUNTIME_TOP = ("main.py", "restart_safe.py", "restore_from_snapshot.py", "restore_card.py",
               "backfill_shadows.py", "check_before_delete.py", "checkout_guard.py")
SKIP_PREFIXES = ("docs/", "verify/", "tests/", "_s0_shot/")

ROOT_CONST_NAMES = {
    "BASE_DIR", "WORKS_DIR", "SNAPSHOT_ROOT", "_FINGERPRINT_FILE", "LOGS_DIR",
    "BACKUP_DIR", "ALLOWED_ROOTS", "PERMISSIONS_PATH", "EXEC_MODE_PATH",
    "SCRIPTS_ROOT", "PROJECT_NAME", "DATA_ROOT", "SANDBOX_ROOT",
}

# 盘符绝对路径：前面不能是字母数字（否则把 https:// 误判成 s:/）
ABS_PATH_RX = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?:[^\s\"'<>|,;)\]]*)")
PORT_RX = re.compile(r"\b(?:port\s*=\s*(\d{3,5})|PORT\s*=\s*(\d{3,5})|:(\d{4,5})\b)")
LOOPBACK_RX = re.compile(r"(127\.0\.0\.1|localhost|0\.0\.0\.0)(?::(\d{2,5}))?")


def tracked_files(wt: Path) -> list[str]:
    p = subprocess.run(["git", "ls-files", "-z"], cwd=str(wt), capture_output=True)
    return [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f.strip()]


def is_runtime(f: str) -> bool:
    if f.startswith(SKIP_PREFIXES) or not f.endswith(".py"):
        return False
    return f.startswith(RUNTIME_PREFIXES) or f in RUNTIME_TOP


def mod_name(f: str) -> str:
    return f[:-3].replace("/", ".")


def pkg_of(mod: str) -> str:
    parts = mod.split(".")
    return parts[1] if len(parts) > 1 else "(root)"


# ---------------------------------------------------------------- C1

def scan_abs_paths(wt: Path, files: list[str]) -> dict:
    code_hits, doc_hits = [], []
    for f in files:
        if not (f.endswith(".py") or f.endswith((".js", ".bat", ".ini", ".html"))):
            continue
        if f.startswith(SKIP_PREFIXES):
            continue
        if not (f.startswith(RUNTIME_PREFIXES) or f in RUNTIME_TOP
                or f.startswith(("static/", "templates/", "config/")) or f == "pytest.ini"):
            continue
        src = (wt / f).read_text(encoding="utf-8", errors="replace")
        lines = src.splitlines()
        # 注释区间（tokenize；失败则退化为空）
        comment_lines: set[int] = set()
        if f.endswith(".py"):
            try:
                for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                    if tok.type == tokenize.COMMENT:
                        comment_lines.add(tok.start[0])
            except Exception:
                pass
        # docstring 区间（模块/类/函数）
        doc_spans: list[tuple[int, int]] = []
        if f.endswith(".py"):
            try:
                tree = ast.parse(src)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        body = getattr(node, "body", [])
                        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                                and isinstance(body[0].value.value, str):
                            e = body[0]
                            doc_spans.append((e.lineno, getattr(e, "end_lineno", e.lineno)))
            except Exception:
                pass
        for i, line in enumerate(lines, 1):
            for m in ABS_PATH_RX.finditer(line):
                raw = m.group(0)
                in_doc = (not f.endswith(".py")) or i in comment_lines or \
                         any(a <= i <= b for a, b in doc_spans)
                rec = {"file": f, "line": i, "literal": raw.rstrip(".,;:)"),
                       "text": line.strip()[:200]}
                (doc_hits if in_doc else code_hits).append(rec)
    return {"code": code_hits, "doc_or_comment": doc_hits}


# ---------------------------------------------------------------- C2

def scan_root_constants(wt: Path, files: list[str]) -> list[dict]:
    out = []
    for f in files:
        if not is_runtime(f):
            continue
        try:
            tree = ast.parse((wt / f).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:            # 只要模块级
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target]
            for t in targets:
                if t.id in ROOT_CONST_NAMES:
                    try:
                        val = ast.unparse(node.value)
                    except Exception:
                        val = "<unparseable>"
                    out.append({"file": f, "line": node.lineno, "name": t.id,
                                "value": val[:220],
                                "kind": "abs" if ABS_PATH_RX.search(val) else
                                        ("file_derived" if "__file__" in val else "other")})
    return out


# ---------------------------------------------------------------- C3

def scan_cross_module_writes(wt: Path, files: list[str]) -> list[dict]:
    out = []
    for f in files:
        if not f.endswith(".py") or f.startswith(SKIP_PREFIXES):
            continue
        if not (f.startswith(("src/", "scripts/")) or f in RUNTIME_TOP or f.startswith("tests/")):
            continue
        try:
            tree = ast.parse((wt / f).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("src."):
                        aliases[a.asname or a.name.split(".")[0]] = a.name
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                base = node.module or ""
                if not base.startswith("src"):
                    continue
                for a in node.names:
                    aliases[a.asname or a.name] = base + ("." + a.name if a.name != "*" else "")
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                            and t.value.id in aliases:
                        out.append({"file": f, "line": node.lineno,
                                    "target": f"{t.value.id}.{t.attr}",
                                    "module": aliases[t.value.id]})
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "setattr" and node.args \
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases:
                attr = ast.unparse(node.args[1]) if len(node.args) > 1 else "?"
                out.append({"file": f, "line": node.lineno,
                            "target": f"setattr({node.args[0].id}, {attr}, ...)",
                            "module": aliases[node.args[0].id]})
    return out


# ---------------------------------------------------------------- C4

def scan_imports(wt: Path, files: list[str]) -> dict:
    recs = []
    for f in files:
        if not is_runtime(f):
            continue
        try:
            tree = ast.parse((wt / f).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        spans = [(n.lineno, getattr(n, "end_lineno", n.lineno)) for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        mod = mod_name(f)

        def resolve(node):
            cur = mod.split(".")[:-1]
            if node.level:
                base = cur[: len(cur) - (node.level - 1)]
                return ".".join(base + ([node.module] if node.module else []))
            return node.module or ""

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                lazy = any(a <= node.lineno <= b for a, b in spans)
                for a in node.names:
                    if a.name.startswith("src."):
                        recs.append({"file": f, "line": node.lineno, "target": a.name,
                                     "symbol": None, "lazy": lazy})
            elif isinstance(node, ast.ImportFrom):
                lazy = any(a <= node.lineno <= b for a, b in spans)
                t = resolve(node)
                if not t.startswith("src"):
                    continue
                for a in node.names:
                    recs.append({"file": f, "line": node.lineno, "target": t,
                                 "symbol": a.name, "lazy": lazy})

    nodes = sorted({mod_name(r["file"]) for r in recs})

    def build(only_top: bool):
        adj: dict[str, set[str]] = collections.defaultdict(set)
        for r in recs:
            if only_top and r["lazy"]:
                continue
            m = mod_name(r["file"])
            cands = {r["target"]} | {n for n in nodes if n.startswith(r["target"] + ".")}
            for x in cands:
                if x != m:
                    adj[m].add(x)
        return adj

    def scc(adj, ns):
        idx, low, on, st, out, ctr = {}, {}, {}, [], [], [0]

        def strong(v):
            work = [(v, iter(sorted(adj.get(v, ()))))]
            idx[v] = low[v] = ctr[0]; ctr[0] += 1
            st.append(v); on[v] = True
            while work:
                node, it = work[-1]
                adv = False
                for w in it:
                    if w not in idx:
                        idx[w] = low[w] = ctr[0]; ctr[0] += 1
                        st.append(w); on[w] = True
                        work.append((w, iter(sorted(adj.get(w, ()))))); adv = True; break
                    elif on.get(w):
                        low[node] = min(low[node], idx[w])
                if adv:
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == idx[node]:
                    comp = []
                    while True:
                        w = st.pop(); on[w] = False; comp.append(w)
                        if w == node:
                            break
                    out.append(comp)

        for v in ns:
            if v not in idx:
                strong(v)
        return [c for c in out if len(c) > 1]

    adj_all = build(False)
    adj_top = build(True)

    pkgs = sorted({pkg_of(n) for n in nodes})
    padj: dict[str, set[str]] = collections.defaultdict(set)
    for m, ts in adj_all.items():
        for t in ts:
            if pkg_of(m) != pkg_of(t):
                padj[pkg_of(m)].add(pkg_of(t))

    edges = collections.Counter()
    reverse = []
    for r in recs:
        a, b = pkg_of(mod_name(r["file"])), pkg_of(r["target"])
        if a != b:
            edges[(a, b)] += 1
    for r in recs:
        a, b = pkg_of(mod_name(r["file"])), pkg_of(r["target"])
        if (a, b) in {("storage", "api"), ("storage", "permissions"),
                      ("permissions", "api"), ("goal", "api"),
                      ("thinkchain", "api"), ("api", "(root)")}:
            reverse.append({"file": r["file"], "line": r["line"], "edge": f"{a}->{b}",
                            "target": r["target"] + ("." + r["symbol"] if r["symbol"] else ""),
                            "lazy": r["lazy"]})

    private = [{"file": r["file"], "line": r["line"],
                "target": f"{r['target']}.{r['symbol']}"}
               for r in recs if r["symbol"] and r["symbol"].startswith("_")
               and not r["symbol"].startswith("__")]

    # 导入期注册：import 后直接调 register_into(某全局表)
    reg = []
    for f in files:
        if not is_runtime(f):
            continue
        try:
            tree = ast.parse((wt / f).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = node.value.func
                if isinstance(fn, ast.Attribute) and "regist" in fn.attr:
                    reg.append({"file": f, "line": node.lineno,
                                "call": ast.unparse(node.value)[:160]})

    return {
        "statements": len(recs),
        "lazy_statements": sum(1 for r in recs if r["lazy"]),
        "modules": len(nodes),
        "package_edges": [{"from": a, "to": b, "n": n}
                          for (a, b), n in sorted(edges.items())],
        "reverse_edges": reverse,
        "scc_runtime": sorted(scc(adj_all, nodes), key=lambda c: -len(c)),
        "scc_import_time": sorted(scc(adj_top, nodes), key=lambda c: -len(c)),
        "scc_package_runtime": [c for c in scc(padj, pkgs)],
        "scc_package_import_time": [c for c in scc(build_pkg(adj_top, nodes, pkg_of), pkgs)],
        "private_symbol_imports": private,
        "import_time_registrations": reg,
    }


def build_pkg(adj, nodes, pk):
    out = collections.defaultdict(set)
    for m, ts in adj.items():
        for t in ts:
            if pk(m) != pk(t):
                out[pk(m)].add(pk(t))
    return out


# ---------------------------------------------------------------- C5

def scan_endpoints(wt: Path, files: list[str]) -> dict:
    ports, loop = [], []
    for f in files:
        if not (f.endswith(".py") or f.endswith(".bat")) or f.startswith(SKIP_PREFIXES):
            continue
        if not is_runtime(f) and f not in ("start.bat", "stop.bat"):
            continue
        for i, line in enumerate((wt / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in PORT_RX.finditer(line):
                v = next(g for g in m.groups() if g)
                if 1024 <= int(v) <= 65535:
                    ports.append({"file": f, "line": i, "port": int(v),
                                  "text": line.strip()[:160]})
            for m in LOOPBACK_RX.finditer(line):
                loop.append({"file": f, "line": i, "host": m.group(1),
                             "port": m.group(2), "text": line.strip()[:160]})
    return {"ports": ports, "loopback": loop}


# ---------------------------------------------------------------- C6

def runtime_probe(wt: Path, python_exe: str) -> dict:
    code = (
        "import json,sys\n"
        f"sys.path.insert(0, r'{wt}')\n"
        "import src.api.file_tools as ft, src.storage.data_snapshot as ds\n"
        "import src.storage.card_store as cs, src.storage.work_store as ws\n"
        "import src.storage.canvas_store as cv, src.storage.session_store as ss\n"
        "import src.api.prompt_log as pl, src.api.env_store as es\n"
        "import src.permissions.policy as pol, src.toolcard.script_loader as sl\n"
        "out = {\n"
        " 'file_tools.BASE_DIR': str(ft.BASE_DIR),\n"
        " 'file_tools.ALLOWED_ROOTS': [str(x) for x in ft.ALLOWED_ROOTS],\n"
        " 'file_tools.BACKUP_DIR': str(ft.BACKUP_DIR),\n"
        " 'data_snapshot.BASE_DIR': str(ds.BASE_DIR),\n"
        " 'data_snapshot.SNAPSHOT_ROOT': str(ds.SNAPSHOT_ROOT),\n"
        " 'data_snapshot._FINGERPRINT_FILE': str(ds._FINGERPRINT_FILE),\n"
        " 'card_store.WORKS_DIR': str(cs.WORKS_DIR),\n"
        " 'work_store.WORKS_DIR': str(ws.WORKS_DIR),\n"
        " 'canvas_store.WORKS_DIR': str(cv.WORKS_DIR),\n"
        " 'session_store.WORKS_DIR': str(ss.WORKS_DIR),\n"
        " 'prompt_log.LOGS_DIR': str(pl.LOGS_DIR),\n"
        " 'env_store.BACKUP_DIR': str(es.BACKUP_DIR),\n"
        " 'policy.PERMISSIONS_PATH': str(pol.PERMISSIONS_PATH),\n"
        " 'toolcard.SCRIPTS_ROOT': str(sl.SCRIPTS_ROOT),\n"
        "}\n"
        "print(json.dumps(out, ensure_ascii=True))\n"
    )
    p = subprocess.run([python_exe, "-c", code], cwd=str(wt), capture_output=True)
    if p.returncode != 0:
        return {"ok": False, "stderr": p.stderr.decode("utf-8", "replace")[-1500:]}
    try:
        return {"ok": True, "values": json.loads(p.stdout.decode("utf-8", "replace"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "raw": p.stdout.decode("utf-8", "replace")[:800], "err": str(exc)}


# ---------------------------------------------------------------- main

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--worktree", default=os.getcwd())
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    wt = Path(args.worktree).resolve()

    files = tracked_files(wt)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt),
                          capture_output=True).stdout.decode().strip()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(wt),
                            capture_output=True).stdout.decode().strip()

    report = {
        "meta": {
            "worktree": str(wt), "branch": branch, "head": head,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "python": sys.version.split()[0], "tracked_files": len(files),
            "dirty": subprocess.run(["git", "status", "--porcelain"], cwd=str(wt),
                                    capture_output=True).stdout.decode("utf-8", "replace").strip().splitlines(),
        },
        "C1_abs_path_literals": scan_abs_paths(wt, files),
        "C2_root_constants": scan_root_constants(wt, files),
        "C3_cross_module_writes": scan_cross_module_writes(wt, files),
        "C4_imports": scan_imports(wt, files),
        "C5_endpoints": scan_endpoints(wt, files),
        "C6_runtime_resolution": runtime_probe(wt, sys.executable),
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")

    c1 = report["C1_abs_path_literals"]
    c4 = report["C4_imports"]
    print(f"head={head} branch={branch} tracked={len(files)}")
    print(f"C1 abs-path literals: code={len(c1['code'])} comment/doc={len(c1['doc_or_comment'])}")
    for r in c1["code"]:
        print(f"   P  {r['file']}:{r['line']}  {r['literal']}")
    print(f"C2 root constants: {len(report['C2_root_constants'])} "
          f"(abs={sum(1 for r in report['C2_root_constants'] if r['kind']=='abs')}, "
          f"file_derived={sum(1 for r in report['C2_root_constants'] if r['kind']=='file_derived')})")
    print(f"C3 cross-module writes: {len(report['C3_cross_module_writes'])}")
    print(f"C4 module statements={c4['statements']} (lazy={c4['lazy_statements']}), "
          f"reverse edges={len(c4['reverse_edges'])}, "
          f"private imports={len(c4['private_symbol_imports'])}, "
          f"import-time registrations={len(c4['import_time_registrations'])}")
    print(f"   SCC runtime: {[len(c) for c in c4['scc_runtime']]}")
    print(f"   SCC import-time: {[len(c) for c in c4['scc_import_time']]}")
    print(f"   package SCC runtime: {c4['scc_package_runtime']}")
    print(f"   package SCC import-time: {c4['scc_package_import_time']}")
    print(f"C5 ports={len(report['C5_endpoints']['ports'])} loopback={len(report['C5_endpoints']['loopback'])}")
    c6 = report["C6_runtime_resolution"]
    print(f"C6 runtime resolution ok={c6.get('ok')}")
    if c6.get("ok"):
        for k, v in c6["values"].items():
            print(f"   {k} = {v}")
    if args.out:
        print(f"json -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())