# -*- coding: utf-8 -*-
"""前端 ?v= 版本号自动传播。

[为什么需要]
没有构建系统时，`?v=NN` 是唯一的缓存失效手段，规则是：
**一个模块内容变了 ⇒ 它自己的版本 +1 ⇒ 所有 import 它的文件必须跟着改 URL ⇒
那些文件的内容也变了 ⇒ 它们自己的版本也要 +1**（递归到入口）。
手工做这件事必然漏（实测漏过两次：漏升入口脚本 ⇒ 四个提交在浏览器里全不生效；
漏升被连带修改的中间模块 ⇒ 它内部还 import 旧 URL）。

用法：
    python scripts/bump_frontend_versions.py api.js nav_system.js   # 这两个模块内容变了
    python scripts/bump_frontend_versions.py --check                # 只检查一致性
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"

# 只匹配真正的引用点（import / script src / link href），不碰注释里的历史说明
JS_REF = re.compile(r"""(?P<pre>\bfrom\s+|\bimport\s+)(?P<q>["'])(?P<path>[^"']+?)\?v=(?P<ver>[0-9.]+)(?P=q)""")
HTML_REF = re.compile(r"""(?P<pre>\b(?:src|href)=)(?P<q>["'])(?P<path>[^"']+?)\?v=(?P<ver>[0-9.]+)(?P=q)""")


def _files() -> list[Path]:
    return list(STATIC.rglob("*.js")) + list(TEMPLATES.glob("*.html"))


def _refs(text: str, is_html: bool):
    pat = HTML_REF if is_html else JS_REF
    for m in pat.finditer(text):
        yield m


def _name(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def _vkey(v: str):
    return tuple(int(x) for x in v.split("."))


def _bump(v: str) -> str:
    parts = v.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def collect() -> dict[str, list[tuple[Path, str, str]]]:
    """模块名 → [(文件, 原始引用串, 版本)]。"""
    out: dict[str, list[tuple[Path, str, str]]] = {}
    for p in _files():
        text = p.read_text(encoding="utf-8")
        for m in _refs(text, p.suffix == ".html"):
            out.setdefault(_name(m.group("path")), []).append((p, m.group("path"), m.group("ver")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("modules", nargs="*", help="内容变了的模块文件名（会 +1 并传播）")
    ap.add_argument("--check", action="store_true", help="只检查同模块是否多版本")
    args = ap.parse_args()

    refs = collect()
    if args.check:
        bad = {k: sorted({v for _, _, v in lst}) for k, lst in refs.items() if len({v for _, _, v in lst}) > 1}
        print("同模块多版本:", bad or "无")
        return 1 if bad else 0

    # 权威版本：该模块当前被引用的最高版本
    auth: dict[str, str] = {}
    for name, lst in refs.items():
        auth[name] = max((v for _, _, v in lst), key=_vkey)

    # 起点：用户点名的模块 +1
    queue = list(args.modules)
    for m in queue:
        if m not in auth:
            print(f"警告：{m} 不在任何引用里，跳过")
        else:
            auth[m] = _bump(auth[m])

    bumped_files: set[Path] = set()
    changed = True
    rounds = 0
    while changed and rounds < 20:
        changed = False
        rounds += 1
        # 把引用统一到权威版本
        for name, lst in refs.items():
            target = auth.get(name)
            if not target:
                continue
            for p in {f for f, _, v in lst if v != target}:
                text = p.read_text(encoding="utf-8")
                is_html = p.suffix == ".html"
                pat = HTML_REF if is_html else JS_REF

                def _sub(m):
                    if _name(m.group("path")) != name:
                        return m.group(0)
                    return f'{m.group("pre")}{m.group("q")}{m.group("path")}?v={target}{m.group("q")}'
                new = pat.sub(_sub, text)
                if new != text:
                    p.write_text(new, encoding="utf-8")
                    changed = True
                    # 该文件内容变了 ⇒ 它自己的版本也要 +1（只加一次）
                    if p not in bumped_files and p.name in auth:
                        auth[p.name] = _bump(auth[p.name])
                        bumped_files.add(p)
                        queue.append(p.name)

    print(f"轮次 {rounds}，改动文件 {len(bumped_files)} 个")
    for p in sorted(bumped_files):
        print("  ", p.relative_to(ROOT))

    # 最终一致性
    refs = collect()
    bad = {k: sorted({v for _, _, v in lst}) for k, lst in refs.items() if len({v for _, _, v in lst}) > 1}
    if bad:
        print("仍有同模块多版本:", bad)
        return 1
    print("一致性 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
