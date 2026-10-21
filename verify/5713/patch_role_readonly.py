# -*- coding: utf-8 -*-
"""把「四个只读文件工具」挂到 tools_read_file 开关上（可重放补丁 + 自检）。

**为什么做成脚本而不是直接改**
本仓 5713 工作树是**多卡共用**的，`src/api/card_tools.py` 经常同时被两三张卡改。
实测：我在 11:05 把 4 行改好，11:07 文件被别人写到一半（语法错误行号 2507→603→600
一直在跳），我的改动虽然还在，但随时可能被对方整文件覆盖。
⇒ 把这 4 行做成**幂等、可重放**的补丁：文件稳定后跑一次，立刻回到正确状态。

**改什么（只改这 4 行，别的一概不碰）**
    "read_file":    SWITCH_TOOLS_FILE → SWITCH_TOOLS_READ_FILE
    "list_files":   SWITCH_TOOLS_FILE → SWITCH_TOOLS_READ_FILE
    "search_files": SWITCH_TOOLS_FILE → SWITCH_TOOLS_READ_FILE
    "glob_files":   SWITCH_TOOLS_FILE → SWITCH_TOOLS_READ_FILE
**不改**：write_file / edit_file / delete_file / move_file / rename_file /
bash / python —— 它们必须继续挂 tools_file（读写档），离树职能角色拿不到。

用法：
    python verify/5713/patch_role_readonly.py           # 应用（幂等）
    python verify/5713/patch_role_readonly.py --check   # 只检查，返回码 0=已就位
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

TARGETS = ("read_file", "list_files", "search_files", "glob_files")
MUST_STAY = ("write_file", "edit_file", "delete_file", "move_file",
             "rename_file", "bash", "python")


def _path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "src", "api", "card_tools.py"))


def _syntax_ok(src: str) -> tuple[bool, str]:
    try:
        ast.parse(src)
        return True, ""
    except SyntaxError as e:
        return False, f"line {e.lineno}: {e.msg}"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不写")
    args = ap.parse_args()
    p = _path()

    src = open(p, encoding="utf-8", errors="replace").read()
    ok, err = _syntax_ok(src)
    if not ok:
        print(f"[BLOCKED] card_tools.py 当前语法就不对（{err}）。")
        print("          说明有别的卡正在写这个文件——等它写完再跑本脚本。")
        print("          本脚本**不会**去修补别人的半成品（那是别人的活，且会被再次覆盖）。")
        return 3

    cr = "\r\n" if "\r\n" in src else "\n"
    changed = []
    for name in TARGETS:
        old = f'"{name}": policy.SWITCH_TOOLS_FILE,'
        new = f'"{name}": policy.SWITCH_TOOLS_READ_FILE,'
        if old in src:
            src = src.replace(old, new, 1)
            changed.append(name)

    # 反向保护：必须仍在 tools_file 的那几件，一件都不许被改走
    broken = [n for n in MUST_STAY
              if f'"{n}": policy.SWITCH_TOOLS_READ_FILE,' in src]
    if broken:
        print(f"[ABORT] 这些工具被错误地改成了只读档：{broken}")
        print("        它们需要写/执行能力，必须留在 tools_file。未写入任何改动。")
        return 4

    if args.check:
        ready = not changed
        print("[READY] 4 件只读工具已挂在 tools_read_file 上。" if ready
              else f"[PENDING] 待改：{changed}")
        # 同时核对必须保留的 7 件仍在 tools_file
        missing = [n for n in MUST_STAY if f'"{n}": policy.SWITCH_TOOLS_FILE,' not in src]
        if missing:
            print(f"[WARN] 这些没找到 tools_file 挂载（可能语法不同）：{missing}")
        return 0 if ready else 1

    if not changed:
        print("[NOOP] 已经是目标状态。")
        return 0

    ok2, err2 = _syntax_ok(src)
    if not ok2:
        print(f"[ABORT] 替换后语法异常（{err2}），未写入。")
        return 5
    open(p, "wb").write(src.encode("utf-8"))
    print(f"[DONE] 已改：{changed}")
    print("       现在角色只要 perms 含 tools_read_file，就能看见这 4 件工具；")
    print("       而 perms 里没有 tools_file ⇒ 看不见 write_file/bash/python。")
    return 0


if __name__ == "__main__":
    sys.exit(main())