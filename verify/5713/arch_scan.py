# -*- coding: utf-8 -*-
"""架构域审计扫描（B-03 路径注入点 / B-04 模块边界）——只读，不改任何文件。

口径（每条都可第三方复算，数字随 HEAD 变、判据不变）：

B-03
  C1 绝对路径字面量：src 下 ast.Constant 字符串匹配 `^[A-Za-z]:[\\\\/]` 或 `^\\\\\\\\`
     （只算代码，不算注释/docstring 以外的文本；env_store.py 的 '\\\\' 是 UNC 前缀判断，
      计入 C1 但归类为 benign）。
  C2 路径根求值点：模块顶层形如 `X = Path(__file__).resolve().parents[N]` 的赋值条数
     （N=1 或 2 都算——同一语义「项目根」被求值了几次）。
  C3 模块级路径常量：模块顶层赋值，名字以 DIR/ROOT/PATH 结尾（或含 BASE_DIR），
     且右值含 Path / 拼接口径。
  C4 运行期单根不变量（需 --runtime）：子进程 import 一批模块，取各自 BASE_DIR，
     集合大小必须为 1；同时看 file_tools.BASE_DIR（文件工具边界）是否与数据根同一。

B-04
  D1 反向边：包层级排序 storage(0) < goal(1) < permissions(2) < thinkchain(3) < api(4)，
     统计 src 内 import 的 `层低 -> 层高` 边，顶层(top) 与函数内(lazy) 分开计数。
  D2 导入期包级环：只取 top 边，按包聚合后求 SCC 是否有 size>1 的分量；
     同时报模块级 top-only SCC（两者不同：包级有环不代表模块级有环）。
  D3 跨模块私有属性访问：`<imported_name>._xxx` 形式（不以 __ 开头）。
  D4 跨模块私有 import：from 里带下划线符号。

用法：
    python verify/5713/arch_scan.py                 # 静态四项（C1-C3, D1-D4）
    python verify/5713/arch_scan.py --runtime       # 追加 C4（子进程 import）
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "src")
ORDER = {"storage": 0, "goal": 1, "permissions": 2, "thinkchain": 3, "api": 4}


def mods_map():
    out = {}
    for dp, dn, fn in os.walk(SRC):
        if "__pycache__" in dp:
            continue
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, REPO).replace("\\", "/")
                out[rel[:-3].replace("/", ".")] = p
    return out


def resolve_relative(base_mod, target, level):
    parts = base_mod.split(".")[:-1]
    up = level - 1
    parts = parts[: len(parts) - up] if up <= len(parts) else []
    if target:
        parts = parts + target.split(".")
    return ".".join(parts)


def nearest(name, mods):
    parts = name.split(".")
    while parts:
        c = ".".join(parts)
        if c in mods:
            return c
        parts.pop()
    return None


def pkg(mod):
    return mod.split(".")[1] if mod.startswith("src.") and mod.count(".") >= 1 else "root"


def scan():
    mods = mods_map()
    c1, c2, c3 = [], [], []
    d1 = []          # (src, dst, line, kind)
    d3 = []
    d4 = []
    for m, p in mods.items():
        rel = os.path.relpath(p, REPO).replace("\\", "/")
        text = open(p, encoding="utf-8").read()
        tree = ast.parse(text)

        # ---- C1
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                if re.match(r"^[A-Za-z]:[\\/]", v) or v.startswith("\\\\"):
                    c1.append((rel, node.lineno, v))

        # ---- C2 / C3 (module top-level assignments)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            seg = ast.get_source_segment(text, node.value) or ""
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                if re.search(r"parents\[\d+\]", seg) and "Path(__file__)" in seg:
                    c2.append((rel, node.lineno, t.id, seg))
                if re.search(r"(DIR|ROOT|PATH)$", t.id) or t.id == "BASE_DIR":
                    if "Path" in seg or "/" in seg or "\\" in seg or "join" in seg:
                        c3.append((rel, node.lineno, t.id, seg[:110]))

        # ---- D1 / D3 / D4
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported_names.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported_names.add((a.asname or a.name).split(".")[0])

        class V(ast.NodeVisitor):
            def __init__(self):
                self.depth = 0

            def visit_FunctionDef(self, n):
                self.depth += 1
                self.generic_visit(n)
                self.depth -= 1

            visit_AsyncFunctionDef = visit_FunctionDef

            def note(self, dst, ln):
                t = nearest(dst, mods)
                if t and t != m:
                    d1.append((m, t, ln, "top" if self.depth == 0 else "lazy"))

            def visit_Import(self, n):
                for a in n.names:
                    if a.name.startswith("src"):
                        self.note(a.name, n.lineno)

            def visit_ImportFrom(self, n):
                base = (resolve_relative(m, n.module or "", n.level)
                        if n.level else (n.module or ""))
                if not base.startswith("src"):
                    return
                for a in n.names:
                    self.note(base + "." + a.name, n.lineno)
                    if a.name.startswith("_") and not a.name.startswith("__"):
                        d4.append((m, base + "." + a.name, n.lineno))

        V().visit(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in imported_names
                    and node.attr.startswith("_") and not node.attr.startswith("__")):
                d3.append((m, node.value.id + "." + node.attr, node.lineno))

    return mods, c1, c2, c3, d1, d3, d4


def scc_sizes(edges):
    g = collections.defaultdict(set)
    for a, b in edges:
        g[a].add(b)
    nodes = set(g) | {b for _, b in edges}
    index, low, on, st, out = {}, {}, set(), [], []
    ctr = [0]

    def strong(v):
        index[v] = low[v] = ctr[0]
        ctr[0] += 1
        st.append(v)
        on.add(v)
        for w in g.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = st.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(comp)

    sys.setrecursionlimit(10000)
    for v in nodes:
        if v not in index:
            strong(v)
    return [c for c in out if len(c) > 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    mods, c1, c2, c3, d1, d3, d4 = scan()

    top = [(s, t, l) for s, t, l, k in d1 if k == "top"]
    lazy = [(s, t, l) for s, t, l, k in d1 if k == "lazy"]
    rev_t = [x for x in top if pkg(x[0]) in ORDER and pkg(x[1]) in ORDER
             and ORDER[pkg(x[0])] < ORDER[pkg(x[1])]]
    rev_l = [x for x in lazy if pkg(x[0]) in ORDER and pkg(x[1]) in ORDER
             and ORDER[pkg(x[0])] < ORDER[pkg(x[1])]]
    pkg_edges = {(pkg(s), pkg(t)) for s, t, _ in top if pkg(s) != pkg(t)}
    pkg_scc = scc_sizes(pkg_edges)
    mod_scc = scc_sizes({(s, t) for s, t, _ in top})

    attr = collections.Counter(a.split(".")[1] for _, a, _ in d3)
    mods_sort = sorted(d3, key=lambda x: (x[0], x[2]))

    rep = {
        "head_files": len(mods),
        "C1_absolute_literals": len(c1),
        "C2_root_eval_points": len(c2),
        "C3_module_level_path_consts": len(c3),
        "C3_by_name": dict(collections.Counter(x[2] for x in c3)),
        "D1_reverse_top": len(rev_t),
        "D1_reverse_lazy": len(rev_l),
        "D2_pkg_cycle_sccs": [sorted(c) for c in pkg_scc],
        "D2_module_cycle_sccs": [len(c) for c in mod_scc],
        "D3_private_attr_total": len(d3),
        "D3_top_symbols": dict(attr.most_common(8)),
        "D4_private_imports": len(d4),
    }
    if args.runtime:
        code = (
            "import sys, json\n"
            "sys.path.insert(0, r'%s')\n"
            "from src.api import file_tools, env_store, model_config, prompt_log\n"
            "from src.storage import (activity, canvas_store, card_store, data_snapshot,\n"
            "                          git_auto_commit, manuscript_store, session_store, work_store)\n"
            "vals = {}\n"
            "for m in (file_tools, env_store, model_config, prompt_log, activity, canvas_store,\n"
            "          card_store, data_snapshot, git_auto_commit, manuscript_store,\n"
            "          session_store, work_store):\n"
            "    vals[m.__name__] = str(getattr(m, 'BASE_DIR', ''))\n"
            "print(json.dumps(vals, ensure_ascii=False))\n" % REPO
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=REPO, timeout=120)
        vals = json.loads(r.stdout) if r.returncode == 0 else {"error": r.stderr[-500:]}
        rep["C4_runtime_base_dirs"] = vals
        rep["C4_distinct_roots"] = len({v for v in vals.values() if v})

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0

    print("src 模块数:", rep["head_files"])
    print("C1 绝对路径字面量:", rep["C1_absolute_literals"])
    for x in c1:
        print("   %s:%d %r" % x)
    print("C2 路径根求值点(Path(__file__).parents[N]):", rep["C2_root_eval_points"])
    for x in sorted(c2):
        print("   %s:%d %s = %s" % x)
    print("C3 模块级路径常量:", rep["C3_module_level_path_consts"], rep["C3_by_name"])
    print("D1 反向边: top=%d lazy=%d" % (rep["D1_reverse_top"], rep["D1_reverse_lazy"]))
    for x in sorted(rev_t + rev_l):
        print("   %s -> %s L%d" % x)
    print("D2 包级导入期环(SCC>1):", rep["D2_pkg_cycle_sccs"] or "无")
    print("D2 模块级 top-only 环:", rep["D2_module_cycle_sccs"] or "无")
    print("D3 跨模块私有属性访问:", rep["D3_private_attr_total"], rep["D3_top_symbols"])
    print("D4 跨模块私有 import:", rep["D4_private_imports"])
    if args.runtime:
        print("C4 运行期 BASE_DIR:", json.dumps(rep["C4_runtime_base_dirs"],
                                               ensure_ascii=False, indent=1))
        print("C4 不同根个数(=1 为合流):", rep["C4_distinct_roots"])
    return 0


if __name__ == "__main__":
    sys.exit(main())