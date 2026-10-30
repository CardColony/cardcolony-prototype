# -*- coding: utf-8 -*-
"""安装 git 提交闸（[harness 第 1 步 2026-10-28]）。

`.git/hooks/` 不进版本控制 ⇒ 换机器/换 worktree 就没了。故把钩子脚本放进仓库
（`scripts/hooks/pre-commit`），用本脚本装到 `.git/hooks/`——这样"提交不通过"
才是可复现的，而不是某台机器上的偶然配置。

用法：
    python scripts/install_hooks.py          # 安装/更新
    python scripts/install_hooks.py --check  # 只比对，不写
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "scripts" / "hooks" / "pre-commit"
DST = ROOT / ".git" / "hooks" / "pre-commit"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"[install_hooks] 源文件不存在: {SRC}", file=sys.stderr)
        return 2
    if not (ROOT / ".git").exists():
        print(f"[install_hooks] 不是 git 仓库: {ROOT}", file=sys.stderr)
        return 2

    src_bytes = SRC.read_bytes()
    if args.check:
        if not DST.exists():
            print("[install_hooks] 未安装")
            return 1
        same = DST.read_bytes() == src_bytes
        print("[install_hooks] 已安装且一致" if same else "[install_hooks] 已安装但内容不同")
        return 0 if same else 1

    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)
    # 可执行位（Windows 上 git 不依赖它，但 *nix 依赖）
    try:
        os.chmod(DST, 0o755)
    except OSError:
        pass
    print(f"[install_hooks] 已安装 → {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
