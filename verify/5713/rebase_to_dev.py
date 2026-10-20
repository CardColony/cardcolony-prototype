# -*- coding: utf-8 -*-
"""把 5713 分支跟到 dev（或指定提交）——一条命令、可回退、出错自动还原。

为什么单独写一个脚本
--------------------
本仓 5713 的 worktree 是**多张卡共用**的：别的卡会在这里改文件、跑真机实验。
手工 `git rebase` 的风险不是冲突（预演证明很难冲突），而是三件事：
  ① 工作区有别人未提交的改动 → rebase 直接拒，或更糟：被带入重放；
  ② 中途冲突 → 分支停在 detached 状态，把正在跑实验的卡弄懵；
  ③ rebase 完没留痕 → 事后说不清「我跑的代码是哪份」。

所以这里把三件事都做死：**先验干净 → 记回退点 → 失败自动 abort → 成功打印对账信息**。

用法
----
    # 跟到当前 dev 头
    python verify/5713/rebase_to_dev.py

    # 跟到指定提交（回溯 / 复现某次）
    python verify/5713/rebase_to_dev.py --onto f190b38

    # 只看会做什么，不动手
    python verify/5713/rebase_to_dev.py --dry-run

退出码：0=已跟上（或本来就是）/ 2=有阻碍（工作区脏、冲突、不在 5713 分支）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def sh(args: list[str], cwd: str) -> tuple[int, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    ap.add_argument("--onto", default="dev", help="跟到哪个提交（默认 dev）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    repo = a.repo

    # ① 必须在 5713 分支上（不要在 detached / 别的分支上做）
    rc, branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    if rc != 0:
        print(f"[x] {repo} 不是 git 仓库")
        return 2
    print(f"[·] 当前分支：{branch}")
    if branch != "5713":
        print("[x] 不在 5713 分支上——本脚本只动 5713，别的情况请人工处理")
        return 2

    # ② 工作区必须没有 tracked 改动（untracked 不算，那些是别人的产物）
    rc, dirty = sh(["git", "status", "--porcelain"], repo)
    tracked = [x for x in dirty.splitlines() if x.strip() and not x.startswith("??")]
    untracked = [x for x in dirty.splitlines() if x.startswith("??")]
    if tracked:
        print("[x] 工作区有未提交的（tracked）改动，rebase 会把它们搅进来 —— 先让改动的主人提交或撤回：")
        for x in tracked:
            print("      " + x)
        return 2
    if untracked:
        print(f"[·] 有 {len(untracked)} 项未跟踪文件（不影响 rebase，保留）：")
        for x in untracked[:5]:
            print("      " + x)

    # ③ 回退点：先记下当前 HEAD，并确保它至少还能从分支/标签找到
    rc, head_before = sh(["git", "rev-parse", "HEAD"], repo)
    if rc != 0:
        print("[x] 读不到 HEAD")
        return 2
    print(f"[·] rebase 前 HEAD：{head_before[:9]}")

    rc, onto_sha = sh(["git", "rev-parse", "--short", a.onto], repo)
    if rc != 0:
        print(f"[x] 解析不了目标提交 {a.onto}")
        return 2

    # ④ 已经是祖先就什么都不做（幂等）
    rc, _ = sh(["git", "merge-base", "--is-ancestor", a.onto, "HEAD"], repo)
    if rc == 0:
        print(f"[ok] {a.onto}({onto_sha.strip()}) 已经是 HEAD 的祖先 —— 无需 rebase")
        rc2, behind = sh(["git", "rev-list", "--count", "HEAD.." + a.onto], repo)
        print(f"     落后 {behind.strip()} 个提交")
        return 0

    rc, will = sh(["git", "log", "--oneline", "--reverse", f"{a.onto}..HEAD"], repo)
    n_commits = len([x for x in will.splitlines() if x.strip()])
    print(f"[·] 目标：{a.onto}({onto_sha.strip()})；要重放 {n_commits} 个提交")
    if a.dry_run:
        print("[dry-run] 到此为止，未动手")
        return 0

    # ⑤ 留一个可回退的分支指针（成本为零，价值极高：rebase 完想回退只需 checkout）
    tag = f"5713-prerebase-{head_before[:9]}"
    rc, _ = sh(["git", "branch", "-f", tag, head_before], repo)
    print(f"[·] 回退点：分支 {tag}" + ("" if rc == 0 else "（创建失败，请手工记下上面的 HEAD）"))

    # ⑥ 动手；失败必须自动 abort，别把工作区留在半路
    rc, out = sh(["git", "-c", "user.name=rebase-tool", "-c", "user.email=tool@local",
                  "rebase", a.onto], repo)
    if rc != 0:
        print("[x] rebase 失败/冲突：")
        print(out[-2000:])
        rc2, _ = sh(["git", "rebase", "--abort"], repo)
        print("[·] 已执行 rebase --abort 还原" + ("（成功）" if rc2 == 0 else "（失败，需人工处理）"))
        return 2

    rc, head_after = sh(["git", "rev-parse", "HEAD"], repo)
    rc, behind = sh(["git", "rev-list", "--count", "HEAD.." + a.onto], repo)
    rc, ahead = sh(["git", "rev-list", "--count", f"{a.onto}..HEAD"], repo)
    rc, cont = sh(["git", "merge-base", "--is-ancestor", a.onto, "HEAD"], repo)

    print("=" * 64)
    print(f"[ok] rebase 完成：{head_before[:9]} → {head_after[:9]}")
    print(f"     目标 {a.onto}({onto_sha.strip()}) 在祖先链上：{cont == 0}")
    print(f"     落后 {behind.strip()} / 领先 {ahead.strip()}")
    print(f"     回退：git checkout 5713 && git reset --hard {tag}")
    print("-" * 64)
    print("下一步（按顺序）：")
    print("  1) 重跑验收网并按集合比：")
    print("     python verify/5713/health_check.py --expected-base " + onto_sha.strip() + " \\")
    print("         --with-tests --baseline-worktree ..\\_base<对应基线>")
    print("  2) 真机复跑通道试走（实例须重启后才加载新代码）：")
    print("     python verify/5713/channel_walk.py --base http://127.0.0.1:5713 \\")
    print("         --out verify/5713/evidence_channel_walk_<sha>.json")
    print("  3) 把新基点/新 HEAD/新数字写回卡片的现状块（基点声明必须改）。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())