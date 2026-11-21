# -*- coding: utf-8 -*-
"""从「批次一之前的工作区快照」里抽出**必须保留的既存改动块**，存成独立文件。

为什么用「独立文件 ＋ 锚点插入」而不是 git patch：
  · git patch 靠**行号 + 上下文**定位 ⇒ 上下文一变（文件长了几行）就错位；
    实测零上下文补丁把 49 行插到了文件末尾（语法直接炸：Unexpected token 'case'）。
  · 锚点插入＝按**内容**定位 ⇒ 文件怎么变都不会跑偏，且插入点一眼可读。

抽的是什么：另一条施工线在批次一**之前**加的「超长会话漏调专项」渲染块
（static/stream.js 的 system_result 等 case）。它必须留在基线里 ——
否则基线就不是「批次一之前的产品」。

用法：python scripts/make_baseline_tree.py --gen-patch
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "scripts" / "baseline_keep_block.js.txt"

#: ★ 基线提交（批次一之前）。
BASE_COMMIT = "8f1ce45"
#: ★「批次一之前的工作区快照」提交（一次已 drop 的 stash ⇒ 悬空提交）。
#:   只用它来**取料**（别的施工线的既存改动），取完就落成独立文件 ⇒
#:   之后不再依赖它（悬空提交随时可能被 gc）。
KEEP_COMMIT = "defb96f53c725b8e6068bfc9c8f6da514299a59c"

#: 既存改动块的**锚**：它是接在这行后面的（case 组的最后一个 case 之后）。
ANCHOR = '(d.bytes || 0) > 0 ? Math.round(d.bytes / 1024) + "KB" : null);'
#: 块的**起始标记**（块的首行）与**结束边界**（块的下一行＝原文件的 default:）。
BLOCK_START = "// [超长会话漏调专项 2026-11-20 · 用户复验] 系统结果独立行"
BLOCK_END = "default:"


def main() -> int:
    # ★ 取「**含既存改动标记的那个 hunk**的新增行」⇒ 正好是那块插入
    #   （不是文件里的一段连续区间——那个区间里混着**本来就存在**的 case，
    #     取多了就坏；也不能取全部新增行——那会把批次一自己的改动也带上）。
    raw = subprocess.run(["git", "diff", "-U0", BASE_COMMIT, KEEP_COMMIT, "--", "static/stream.js"],
                         cwd=str(ROOT), capture_output=True).stdout.decode("utf-8", "replace")
    hunks: list[list[str]] = []
    cur: list[str] | None = None
    for ln in raw.splitlines():
        if ln.startswith("@@"):
            if cur is not None:
                hunks.append(cur)
            cur = []
        elif cur is not None:
            cur.append(ln)
    if cur is not None:
        hunks.append(cur)
    pick = next((h for h in hunks if any(BLOCK_START in x for x in h)), None)
    if pick is None:
        print("[fail] diff 里找不到含既存改动标记的 hunk")
        return 1
    added = [ln[1:] for ln in pick if ln.startswith("+") and not ln.startswith("+++")]
    if not added or BLOCK_START not in added[0]:
        print(f"[fail] 新增块首行不是预期标记：{added[0][:80] if added else '(空)'}")
        return 1
    OUT.write_text("\n".join(added) + "\n", encoding="utf-8", newline="")
    print(f"[ok] 已抽出既存改动块 {len(added)} 行 → {OUT.name}")
    print(f"     首行：{added[0].strip()[:70]}")
    print(f"     末行：{added[-1].strip()[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
