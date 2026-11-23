# -*- coding: utf-8 -*-
"""搭一棵「批次一之前」的静态资源树（`_baseline/static` ＋ `_baseline/templates`）。

★ [2026-11-20 返工] **基线必须钉在固定的提交上**，不能读浮动的 HEAD：
   另一条施工线在 17:38 把整棵工作区 `git add -A` 提交了（b3a15e5），
   **我这一批的改动也被卷了进去** ⇒ 若基线从 HEAD 取文件，重新生成时
   会把「批次一之前」这棵树**污染成改动之后** ⇒ 对照失去意义。

   ⇒ 本脚本的口径（严格、可复算）：
     ① **基线提交** `BASE_COMMIT`（批次一之前的那次提交）——固定值；
     ② 批次一**我**改过的文件 ⇒ 一律取 `BASE_COMMIT` 的版本；
     ③ 「基线提交之后、批次一之前」**必须保留的既存改动** ⇒ 由
        `scripts/baseline_keep.patch` 显式承载（**checked-in 的补丁**，
        不在运行时猜、不依赖悬空提交）。
        ★ 该补丁只有一处：`static/stream.js` 的「超长会话漏调专项」渲染块
          （另一条施工线在批次一之前加的，必须留着）。
        补丁文件由 `python scripts/make_baseline_tree.py --gen-patch` 生成
        （生成源：固定的基线提交 → 固定的「批次一之前的工作区快照」提交）。

用法：
  python scripts/make_baseline_tree.py             # 生成基线树
  python scripts/make_baseline_tree.py --gen-patch # 只重新生成补丁文件
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "_baseline"
BLOCK = ROOT / "scripts" / "baseline_keep_block.js.txt"

#: ★ 基线提交：**批次一之前**的最近一次提交。换基线只改这一行。
BASE_COMMIT = "8f1ce45"

#: ★「批次一之前的工作区快照」提交（当时我的批次一改动也在里面 ⇒ 用它
#: 只为**取出别的施工线的既存改动**）。它是一次已 drop 的 stash
#: （悬空提交）⇒ 取完料落成独立文件（`baseline_keep_block.js.txt`）
#: ⇒ 之后不再依赖它（悬空提交随时可能被 gc）。
KEEP_COMMIT = "defb96f53c725b8e6068bfc9c8f6da514299a59c"

#: 既存改动块在基线文件里的**插入锚**：块接在这一行**后面**。
#: ★ 用**内容**定位而不是行号/补丁：git patch 靠行号+上下文定位 ⇒ 文件一变
#:   就错位（实测零上下文补丁把 49 行插到了文件末尾 ⇒ 语法炸：
#:   "Unexpected token 'case'"，整棵树白跑 20 轮）。
KEEP_ANCHOR = 'Math.round((d.bytes || 0) / 1024) + "KB" : null);'

#: 批次一**我**改过、基线要回退到 BASE_COMMIT 的文件。
#: ★ 覆盖两类：① 内容改动（共享 GET / 窄投影 / 滚动独占 / 右栏懒加载…）
#:   ② **只改了版本戳**的那些（canvas.js / home.js / nav_render.js /
#:   nav_system.js / main.js / main_chat.js —— 它们也要回基线，
#:   否则基线树里会出现「同一个模块两个戳」）。
REVERT_TO_BASE = [
    "static/esm/api.js",
    "static/esm/model_picker.js",
    "static/esm/rule_panel.js",
    "static/esm/flow/chat-view.js",
    "static/workspace.js",
    "static/stream.js",
    "static/canvas.js",
    "static/esm/home.js",
    "static/esm/nav_render.js",
    "static/esm/nav_system.js",
    "static/esm/main.js",
    "static/esm/main_chat.js",
    # [累计对照·批二] 装配器（延迟归并/批尾配对/位次界/组重放全在这份）
    "static/esm/assembler.js",
    # [累计对照·批三] mergeRunningSet 与 temp.running 消费口径
    "static/esm/state_layers.js",
]


def sh(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True).stdout


def split_hunks(patch: str) -> tuple[str, list[str]]:
    lines = patch.splitlines(keepends=True)
    head: list[str] = []
    hunks: list[str] = []
    cur: list[str] | None = None
    for ln in lines:
        if ln.startswith("@@"):
            if cur is not None:
                hunks.append("".join(cur))
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
        else:
            head.append(ln)
    if cur is not None:
        hunks.append("".join(cur))
    return "".join(head), hunks


def gen_patch() -> int:
    """从固定的基线提交 → 固定的快照提交里，挑出**既存改动**那块，生成补丁文件。"""
    raw = sh("diff", "-U0", BASE_COMMIT, KEEP_COMMIT, "--", "static/stream.js")
    patch = raw.decode("utf-8", errors="replace")
    head, hunks = split_hunks(patch)
    keep = [h for h in hunks if KEEP_ANCHOR in h]
    if not keep:
        print(f"[fail] 快照里找不到锚 {KEEP_ANCHOR!r} ⇒ 补丁无法生成")
        return 1
    PATCH.write_text(head + "".join(keep), encoding="utf-8", newline="")
    print(f"[ok] 已生成 {PATCH.name}（{len(keep)} 个 hunk，锚={KEEP_ANCHOR!r}）")
    return 0


def main() -> int:
    if "--gen-patch" in sys.argv:
        return gen_patch()
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "static").mkdir(parents=True, exist_ok=True)
    (OUT / "templates").mkdir(parents=True, exist_ok=True)

    # ① 整份拷当前工作区的 static/ 与 templates/（批次一没动过的文件保持当前版）
    shutil.copytree(ROOT / "static", OUT / "static", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.gif"))
    shutil.copytree(ROOT / "templates", OUT / "templates", dirs_exist_ok=True)

    # ② 我改过的文件 ⇒ 回退到固定的基线提交
    for rel in REVERT_TO_BASE:
        content = sh("show", f"{BASE_COMMIT}:{rel}")
        if not content:
            print(f"[warn] {BASE_COMMIT} 里没有 {rel}")
            continue
        (OUT / rel).write_bytes(content)
    print(f"[base] {len(REVERT_TO_BASE)} 个文件 ← {BASE_COMMIT}")

    # ③ 按**锚点**插回「必须保留的既存改动块」（内容定位 ⇒ 不会错位）
    if not BLOCK.exists():
        print(f"[fail] 缺 {BLOCK.name} ⇒ 先跑 scripts/extract_baseline_block.py")
        return 1
    block_lines = BLOCK.read_text(encoding="utf-8").splitlines()
    target = OUT / "static" / "stream.js"
    lines = target.read_text(encoding="utf-8").splitlines()
    at = next((i for i, l in enumerate(lines) if KEEP_ANCHOR in l), None)
    if at is None:
        print(f"[fail] 基线 stream.js 里找不到插入锚（{KEEP_ANCHOR!r}）")
        return 1
    lines[at + 1:at + 1] = block_lines
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    print(f"[keep] 既存改动块已插到第 {at+2} 行（{len(block_lines)} 行，{BLOCK.name}）")

    # ④ 自检：基线树里不该有批次一的标记
    import re
    markers = ["_sharedGet", "getCardMeta", "ensureCardFull", "watchInspectorOpen",
               "视图独占", "_lastGood", "wantWid", "_cardFullKey", "bodyKeys",
               "api.js?v=57", "stream.js?v=81", "workspace.js?v=150",
               "rule_panel.js?v=48", "model_picker.js?v=9", "chat-view.js?v=8",
               "holdSignal", "_dropEntry", "signal: signal"]
    bad = []
    for p in (OUT / "static").rglob("*.js"):
        t = p.read_text(encoding="utf-8", errors="replace")
        for mk in markers:
            if mk in t:
                bad.append(f"{p.relative_to(OUT).as_posix()} ← {mk}")
    if bad:
        print("[fail] 基线树里仍有批次一标记：")
        for b in bad:
            print("   " + b)
        return 1
    print("[ok] 基线树自检通过（无批次一标记）")
    print(f"[done] 基线树就绪：{OUT}（基线提交 {BASE_COMMIT} ＋ {BLOCK.name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
