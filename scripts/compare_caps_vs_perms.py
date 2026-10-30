# -*- coding: utf-8 -*-
"""能力清单 × 模式清单 与「今天实际工具面」的对照（[能力清单 2026-10-30]）。

**为什么要有这份脚本**：能力清单和模式清单都是"从今天的实际行为反推"出来的。
反推就可能推错——错一件，某张卡就少一件工具，而且没有任何现象（模型只是看不见它）。
所以这里做机械对账：拿**今天**的工具面，和「模式清单 → 能力 → 工具」展开出来的面比。

判据两条（任一不成立即退出码 1）：

1. **不漏**：今天工具面里的每一件工具，都必须被某项能力覆盖（或明列在
   ``not_capabilities`` 里）。漏了说明能力清单少收了一件工具。
2. **不越**：这张卡今天的工具面，必须是「它所属模式 + 它的卡型」所允许的能力
   展开出来的子集。越了说明模式清单给窄了（切换后会掉工具）。

用法::

    python scripts/compare_caps_vs_perms.py            # 默认 work_027
    python scripts/compare_caps_vs_perms.py work_001

只读，不写任何东西。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from src.api import card_tools  # noqa: E402


def _load(name: str) -> dict:
    return json.loads((BASE / "config" / name).read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    work_id = argv[1] if len(argv) > 1 else "work_027"
    caps_doc = _load("capabilities.json")
    modes = {p.stem: json.loads(p.read_text(encoding="utf-8"))
             for p in (BASE / "config" / "modes").glob("*.json")}

    caps = {c["code"]: c for c in caps_doc["capabilities"]}
    tool2cap = {t: code for code, c in caps.items() for t in c["impl"]["tools"]}
    not_caps = {x["tool"] for x in caps_doc.get("not_capabilities") or []}

    def expand(cap_codes) -> set[str]:
        return {t for code in cap_codes for t in caps[code]["impl"]["tools"]}

    cards = card_tools._collect_card_meta(work_id)
    if not cards:
        print(f"[compare] {work_id} 没有卡")
        return 0

    rows, bad = [], []
    for cid, meta in sorted(cards.items()):
        if cid.startswith("shadow_"):
            continue
        ctype = str(meta.get("card_type") or "")
        mode = "auto" if bool(meta.get("auto_mode")) else "manual"
        face = {t["name"] for t in card_tools.build_tool_schemas(
            card_id=cid, work_id=work_id, cards=cards)}

        uncapped = sorted(face & not_caps)
        missing = sorted(t for t in face if t not in tool2cap and t not in not_caps)
        upper_caps = (modes.get(mode, {}).get("cards") or {}).get(ctype)
        over = []
        if upper_caps is None:
            over = ["（模式清单里没有这个卡型）"]
        else:
            over = sorted(face - expand(upper_caps))

        rows.append((cid, ctype, mode, len(face), len(over), len(missing)))
        if over or missing:
            bad.append((cid, ctype, mode, over, missing))

    print(f"[compare] work={work_id} 卡数={len(rows)}")
    for cid, ctype, mode, n, n_over, n_miss in rows:
        flag = "" if (n_over == 0 and n_miss == 0) else "  ← 有差异"
        print(f"  {cid:10s} {ctype:12s} {mode:6s} 工具 {n:2d} 越界 {n_over} 未归类 {n_miss}{flag}")

    if not bad:
        print("\n[compare] 全部一致：每件工具都被能力覆盖，且都在模式清单的上限内。")
        return 0

    print("\n[compare] 差异明细：")
    for cid, ctype, mode, over, missing in bad:
        if over:
            print(f"  {cid}({ctype}/{mode}) 超出模式上限: {over}")
        if missing:
            print(f"  {cid}({ctype}/{mode}) 没有能力覆盖: {missing}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
