# -*- coding: utf-8 -*-
"""检查：同一个模块在所有 import 方处是否用了**同一个**版本戳。

为什么单独写一支（而不是靠 board_resident_state 的 A4）：
A4 只查「有分裂」的模块 ⇒ **只有一个 import 方**的模块查不出来。
而恰恰是那种模块最容易在批量改戳时被误伤（子串撞名：
把 ``think-stream.js`` 当成 ``stream.js`` 一起升了号 ⇒
它自己多出一个戳，等哪天第二个地方 import 它就分裂成两份实例）。

跑法：python scripts/check_module_stamps.py
"""
from __future__ import annotations

import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC_RE = re.compile(r"""(?:from\s*|import\s*)["']([^"']+\?v=\d+)["']""")
#: 改戳时要防的子串撞名：目标名出现在别的文件名**尾部**时会被误伤。
#: 这里显式列出「真身 ↔ 可能撞名的文件」，避免批量改戳时误伤。
ALIAS_TRAPS = (
    ("stream.js", "think-stream.js"),
    ("api.js", None),
)


def main() -> int:
    files = list((ROOT / "static").rglob("*.js")) + list((ROOT / "templates").rglob("*.html"))
    groups: dict[str, set[str]] = collections.defaultdict(set)
    importers: dict[str, set[str]] = collections.defaultdict(set)
    for p in files:
        txt = p.read_text(encoding="utf-8", errors="replace")
        for m in SPEC_RE.finditer(txt):
            spec = m.group(1)
            q = spec.index("?")
            raw = spec[:q]
            if not raw.startswith("."):
                continue
            abs_path = (p.parent / raw).resolve().as_posix()
            groups[abs_path].add(spec[q + 1:])
            importers[abs_path].add(p.relative_to(ROOT).as_posix())

    bad = [(k, sorted(v)) for k, v in groups.items() if len(v) > 1]
    print(f"扫描模块数 = {len(groups)}")
    print(f"戳分裂的模块 = {len(bad)}")
    for k, v in bad:
        print(f"  !! {k} ⇒ {v}  （import 方：{sorted(importers[k])}）")

    # 子串撞名自检：某个模块名是不是另一个模块名的尾部？
    names = {pathlib.Path(k).name for k in groups}
    trap_hits = []
    for target, trap in ALIAS_TRAPS:
        if trap and trap in names and trap != target:
            trap_hits.append((trap, target))
    for trap, target in trap_hits:
        trap_path = (ROOT / "static" / "esm" / "flow" / trap).resolve().as_posix()
        stamps = sorted(groups.get(trap_path, []))
        print(f"  [撞名自检] {trap} 与 {target} 名字相撞 ⇒ 改戳时必须按完整文件名匹配"
              f"（当前 {trap} 的戳 = {stamps}）")

    if bad:
        return 1
    print("  （无分裂）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
