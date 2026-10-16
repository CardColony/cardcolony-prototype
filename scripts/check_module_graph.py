# -*- coding: utf-8 -*-
"""模块图与 DOM 依赖判据脚本（长期可用）。

用途
----
1) 模块图：给定入口文件，沿静态 import 求**传递闭包**（剥离 `?v=` 查询串），
   输出该入口真正会加载的模块清单。
2) DOM 依赖：对闭包内每个文件，抽取 `getElementById("X")` / `querySelector("...")`
   字面量，输出该入口的 DOM 依赖清单（供模板拆分核对）。
3) 双页纯度断言（`--check`）：画布页模块图 ∌ workspace/stream；对话页 ∌ canvas。

用法
----
  python scripts/check_module_graph.py --entry static/esm/main.js
  python scripts/check_module_graph.py --entry static/esm/main_chat.js --dom
  python scripts/check_module_graph.py --check

口径
----
- 只认**静态** `import ... from "..."` 与 `import "..."`；动态 import() 与注释内
  import 不计（行内 `//` 之后的文本先剥掉）。
- 路径按文件所在目录解析；`?v=NN` 一律剥离后作为模块身份。
- 只统计字面量字符串参数；`querySelector(variable)` 记不到（如实计为 0）。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IMPORT_RE = re.compile(r"""^\s*import\s+(?:[^'"]*?\sfrom\s+)?['"]([^'"]+)['"]""", re.M)
BY_ID_RE = re.compile(r"""getElementById\(\s*['"]([^'"]+)['"]""")
QS_RE = re.compile(r"""querySelector(?:All)?\(\s*['"]([^'"]+)['"]""")


def _strip_comment(line: str) -> str:
    """剥掉行内 // 之后的注释（不处理块注释里的 //，够用）。"""
    i = line.find("//")
    return line if i < 0 else line[:i]


def _resolve(base_file: str, spec: str) -> str | None:
    """把 import 说明符解析为仓库内绝对路径；外部包返回 None。"""
    spec = spec.split("?")[0].split("#")[0]
    if not spec.startswith("."):
        return None
    p = os.path.normpath(os.path.join(os.path.dirname(base_file), spec))
    return p if os.path.isfile(p) else None


def closure(entry: str) -> list[str]:
    """入口文件的 import 传递闭包（含入口自身），按发现顺序。"""
    seen: list[str] = []
    stack = [os.path.normpath(entry)]
    while stack:
        f = stack.pop(0)
        if f in seen:
            continue
        seen.append(f)
        try:
            text = io.open(f, encoding="utf-8").read()
        except OSError:
            continue
        for line in text.split("\n"):
            m = IMPORT_RE.match(_strip_comment(line))
            if not m:
                continue
            r = _resolve(f, m.group(1))
            if r and r not in seen:
                stack.append(r)
    return seen


def dom_deps(files: list[str]) -> dict[str, list[str]]:
    """{文件: [DOM 选择器...]}（字面量口径）。"""
    out: dict[str, list[str]] = {}
    for f in files:
        try:
            text = io.open(f, encoding="utf-8").read()
        except OSError:
            continue
        hits = sorted(set(BY_ID_RE.findall(text)) | set(QS_RE.findall(text)))
        if hits:
            out[f] = hits
    return out


def rel(p: str) -> str:
    return os.path.relpath(p, BASE).replace("\\", "/")


# 允许「查了但允许缺席」的 id：代码里带 null 守卫（if (el) ...），缺席是设计而非缺件。
OPTIONAL_IDS = {
    "newCardBtn",   # 对话页无建卡钮：nav_system 守卫后改跳画布页
    "childTitle",   # 对话页 inspector 自建；canvas 页缺席由 nav_system 的 if (el) 守卫
    "workspace",    # 画布页无对话容器：canvas.js hideWorkspace 的 if (ws) 守卫
}


def _template_ids(path: str) -> set[str]:
    text = io.open(path, encoding="utf-8").read()
    return set(re.findall(r'id="([^"]+)"', text))


_SELF_ID_RE = re.compile(r"""id\s*=\s*["']([^"']+)["']|\.id\s*=\s*["']([^"']+)["']""")


def _self_created_ids(files: list[str]) -> set[str]:
    """闭包内 JS 自己创建/赋值的 id（innerHTML 模板串、el.id = "x"）——不算页面缺件。"""
    out: set[str] = set()
    for f in files:
        try:
            text = io.open(f, encoding="utf-8").read()
        except OSError:
            continue
        for a, b in _SELF_ID_RE.findall(text):
            out.add(a or b)
    return out


def dom_check() -> int:
    """双页 DOM 依赖闭合：入口闭包内所有 getElementById 的 id 必须在**该页模板**里存在，
    或由闭包内 JS 自己创建（自建 id 不算缺件），或属带 null 守卫的跨页可选查找。"""
    pages = [("static/esm/main.js", "templates/work.html"),
             ("static/esm/main_chat.js", "templates/card.html")]
    fails = []
    for entry, tpl in pages:
        files = closure(os.path.join(BASE, entry))
        ids: set[str] = set()
        for f in files:
            try:
                ids |= set(BY_ID_RE.findall(io.open(f, encoding="utf-8").read()))
            except OSError:
                pass
        have = _template_ids(os.path.join(BASE, tpl))
        selfmade = _self_created_ids(files)
        missing = sorted(ids - have - selfmade - OPTIONAL_IDS)
        print("%-24s 需 %2d 个 id（自建 %d）/ 模板有 %2d 个" % (entry, len(ids), len(selfmade), len(have)))
        if missing:
            fails.append("%s 缺件: %s" % (tpl, missing))
    for f in fails:
        print("FAIL:", f)
    print("DOM_CHECK:", "PASS" if not fails else "FAIL")
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", help="入口文件（相对仓库根）")
    ap.add_argument("--dom", action="store_true", help="同时输出 DOM 依赖清单")
    ap.add_argument("--check", action="store_true", help="双页纯度断言")
    ap.add_argument("--dom-check", action="store_true", help="双页 DOM 依赖闭合断言")
    args = ap.parse_args()

    if args.dom_check:
        return dom_check()

    if args.check:
        canvas_entry = os.path.join(BASE, "static", "esm", "main.js")
        chat_entry = os.path.join(BASE, "static", "esm", "main_chat.js")
        fails = []
        for entry, forbidden in ((canvas_entry, ("workspace.js", "stream.js")),
                                 (chat_entry, ("canvas.js",))):
            if not os.path.isfile(entry):
                fails.append("入口不存在: %s" % rel(entry))
                continue
            names = [os.path.basename(p) for p in closure(entry)]
            hit = [n for n in names if n in forbidden]
            print("%-28s -> %d 模块" % (rel(entry), len(names)))
            if hit:
                fails.append("%s 不应包含: %s" % (rel(entry), hit))
        for f in fails:
            print("FAIL:", f)
        print("CHECK:", "PASS" if not fails else "FAIL")
        return 0 if not fails else 1

    if not args.entry:
        ap.print_help()
        return 2
    entry = os.path.join(BASE, args.entry)
    files = closure(entry)
    print("入口: %s" % args.entry)
    print("模块图（%d）:" % len(files))
    for f in files:
        print("  " + rel(f))
    if args.dom:
        deps = dom_deps(files)
        allsel = sorted({s for v in deps.values() for s in v})
        print("\nDOM 依赖（%d 个选择器）:" % len(allsel))
        for s in allsel:
            print("  " + s)
        print("\n按文件:")
        for f, v in deps.items():
            print("  %s: %s" % (rel(f), ", ".join(v)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
