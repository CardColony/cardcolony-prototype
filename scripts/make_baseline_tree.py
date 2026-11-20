# -*- coding: utf-8 -*-
"""搭一棵「批次一之前」的静态资源树（`_baseline/static` ＋ `_baseline/templates`）。

为什么需要：性能回交要「同条件前后读数」⇒ 必须有一棵**改动前**的前端树，
和改动后的树在同一台隔离实例上量。

口径（严格）：
  · 批次一**没动过**的文件 ⇒ 直接用当前工作区的那一份；
  · 批次一**动过**的文件 ⇒ 用「批次一之前」的版本：
      - 批次前工作区 == HEAD 的文件 ⇒ 取 HEAD 的内容（api.js / model_picker.js /
        rule_panel.js / chat-view.js / workspace.js）；
      - 批次前工作区就与 HEAD 不同（stream.js：既存的「系统结果独立行」渲染块）
        ⇒ 取 HEAD 的内容 **加上**那块既存改动（用 git apply 打补丁，别手抄）。
  ★ **后端不动**：批次一的后端改动（窄投影读口 / 白名单）对**读路径**是加法，
    5707 单进程实例又不装 web 转发代理（install_web_proxy 只在 ECO_ROLE=web 生效）
    ⇒ 用同一套后端量前后，前端差异才是被测变量。

用法：python scripts/make_baseline_tree.py
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "_baseline"

#: 批次一改过、且「批次前 == HEAD」的前端文件 ⇒ 取 HEAD。
TAKE_HEAD = [
    "static/esm/api.js",
    "static/esm/model_picker.js",
    "static/esm/rule_panel.js",
    "static/esm/flow/chat-view.js",
    "static/workspace.js",
]

#: 批次一改过、且批次前就与 HEAD 不同的文件 ⇒ HEAD ＋ 既存改动（按标记挑 hunk）。
#: 标记＝既存改动独有的那行注释（用它挑 hunk，避免手抄出错）。
HEAD_PLUS_MARKER = {
    "static/stream.js": "超长会话漏调专项",
}


def sh(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True).stdout


def split_hunks(patch: str) -> tuple[str, list[str]]:
    """把 unified diff 拆成 (头部, [hunk 文本…])。"""
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


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "static").mkdir(parents=True, exist_ok=True)
    (OUT / "templates").mkdir(parents=True, exist_ok=True)

    # ① 当前工作区的 static/ 与 templates/ 整份拷过去（= 批次一之后的树）
    shutil.copytree(ROOT / "static", OUT / "static", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.gif"))
    shutil.copytree(ROOT / "templates", OUT / "templates", dirs_exist_ok=True)

    # ② 批次一改过、批次前 == HEAD 的 ⇒ 换回 HEAD 的内容
    for rel in TAKE_HEAD:
        content = sh("show", f"HEAD:{rel}")
        if not content:
            print(f"[skip] HEAD 里没有 {rel}")
            continue
        (OUT / rel).write_bytes(content)
        print(f"[head] {rel}")

    # ③ 批次前就与 HEAD 不同的 ⇒ HEAD ＋ 既存 hunk
    for rel, marker in HEAD_PLUS_MARKER.items():
        content = sh("show", f"HEAD:{rel}")
        (OUT / rel).write_bytes(content)
        patch = sh("diff", "HEAD", "--", rel).decode("utf-8", errors="replace")
        head_txt, hunks = split_hunks(patch)
        keep = [h for h in hunks if marker in h]
        if not keep:
            print(f"[warn] {rel} 找不到标记 {marker!r} 的 hunk ⇒ 退回纯 HEAD")
            continue
        filtered = head_txt + "".join(keep)
        pf = OUT / (rel + ".patch")
        pf.write_text(filtered, encoding="utf-8", newline="")
        r = subprocess.run(["git", "apply", "-p1", "--whitespace=nowarn", str(pf)],
                           cwd=str(OUT), capture_output=True)
        pf.unlink(missing_ok=True)
        if r.returncode != 0:
            print(f"[warn] {rel} 打既存补丁失败：{r.stderr.decode('utf-8', errors='replace')[:300]}")
            continue
        print(f"[head+既有] {rel}  （既存 hunk {len(keep)} 个）")

    print(f"[done] 基线树就绪：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
