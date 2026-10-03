# -*- coding: utf-8 -*-
"""分支操作守卫：在承载运行数据的目录上，执行任何 git 分支操作前必须过这一关。

背景（2026-10-03 15:01 事故）：
  在正式环境（承载 works/ 运行数据）里执行 `git checkout dev`，
  git 重写工作区全部受追踪文件 → works/ 被整体回退到旧快照
  → 用户对话回退、已删卡复活、新建卡消失。

用法：
  python checkout_guard.py                 # 检查当前目录能否安全做分支操作
  python checkout_guard.py --cmd "checkout dev"   # 带拟执行命令，输出判定与理由
  退出码：0 = 放行；1 = 拒绝（禁止执行）

机械判据（不判内容、不靠记忆）：
  R1  works/ 是否仍被 git 追踪 → 被追踪则拒绝（这是事故的直接靶子）
  R2  工作区是否有未提交的运行数据变更 → 有则拒绝（分支操作会覆盖它们）
  R3  当前目录是否是承载运行数据的正式环境（存在 works/ 且存在 .git）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def sh(*args) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=str(BASE), capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def check() -> tuple[bool, list[str]]:
    """返回 (是否放行, 理由列表)"""
    reasons: list[str] = []
    blocked = False

    if not (BASE / ".git").exists():
        return True, ["非 git 仓库，无分支操作风险"]

    # R1: works/ 是否仍被追踪
    rc, out = sh("ls-files", "works/")
    tracked = [l for l in out.splitlines() if l.strip()]
    if tracked:
        blocked = True
        reasons.append(
            f"R1 拒绝：works/ 仍有 {len(tracked)} 个文件被 git 追踪——"
            f"任何分支操作都会重写它们（这正是 15:01 事故的成因）。"
            f"先执行剥离（git rm --cached -r works/ + .gitignore 加 works/）。"
        )
    else:
        reasons.append("R1 通过：works/ 已不被追踪，分支操作碰不到运行数据")

    # R2: 工作区是否有未提交的运行数据变更
    rc, out = sh("status", "--porcelain", "works/")
    dirty = [l for l in out.splitlines() if l.strip()]
    if dirty:
        blocked = True
        reasons.append(
            f"R2 拒绝：工作区有 {len(dirty)} 处运行数据未提交变更——"
            f"分支操作会覆盖它们（未提交 = 无副本 = 可能永久丢失）。"
        )
    else:
        reasons.append("R2 通过：工作区无未提交的运行数据变更")

    # R3: 环境性质
    if (BASE / "works").exists() and (BASE / ".env").exists():
        reasons.append("R3 提示：本目录是承载运行数据的环境（有 works/ 与 .env）——分支操作风险等级最高")
    return (not blocked), reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", default="", help="拟执行的分支操作命令，仅用于输出")
    a = ap.parse_args()

    ok, reasons = check()
    print("=" * 70)
    print(f"分支操作守卫  ·  目录: {BASE}")
    if a.cmd:
        print(f"拟执行: git {a.cmd}")
    print("=" * 70)
    for r in reasons:
        print(f"  {r}")
    print("-" * 70)
    if ok:
        print("  【判定】放行")
        sys.exit(0)
    else:
        print("  【判定】拒绝执行——先处置上述阻断项")
        print("  （永久禁令：禁止在承载运行数据的目录上做任何 git 分支操作）")
        sys.exit(1)


if __name__ == "__main__":
    main()
