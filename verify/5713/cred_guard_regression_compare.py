# -*- coding: utf-8 -*-
"""回归对照：HEAD（旧代码）与工作树（新代码）各跑一遍同一批用例。

跑的这批就是任务书点名的回归面：
  tests/test_credential_exec_guard.py（原 11 条）
  tests/test_credential_guard_coverage.py（本卡新增 4 条）
  tests/test_file_boundaries.py  tests/test_file_scope_keys.py
  tests/test_perm_gateway_acceptance.py  tests/test_cardperm_*.py

为什么后台跑：整批耗时超过单次命令的 120s 上限，故本脚本 detach 起来写日志，
调用方轮询日志即可。落盘前掩码真实 .env 的值（副本进版本控制）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\AI_station\推演agent\.worktrees\5713")
# 基线钉死到 card_012 那一版（e639da4），不能写 HEAD（理由见 oldnew_compare 同处）。
OLD_REF = "e639da4"
TARGET = REPO / "src" / "api" / "credential_guard.py"
OUT = REPO / "verify" / "5713" / "evidence_cred_regression.txt"
REAL_ENV = Path(r"D:\AI_station\推演agent\.env")
FILES = [
    "tests/test_credential_exec_guard.py",
    "tests/test_credential_guard_coverage.py",
    "tests/test_file_boundaries.py",
    "tests/test_file_scope_keys.py",
    "tests/test_perm_gateway_acceptance.py",
    "tests/test_cardperm_live.py",
    "tests/test_cardperm_readopen.py",
]


def _mask(text: str) -> str:
    if not REAL_ENV.is_file():
        return text
    sys.path.insert(0, str(REPO))
    from src.api import credential_guard
    for v in sorted(credential_guard._harvest_file(REAL_ENV), key=len, reverse=True):
        if v and v in text:
            text = text.replace(v, f"<masked len={len(v)}>")
    return text


def run(tag: str) -> str:
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(REPO)})
    cmd = [sys.executable, "-m", "pytest", *FILES, "-q", "--no-header",
           "-p", "no:cacheprovider", "-rf"]
    p = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return f"===== {tag} (pytest exit={p.returncode}) =====\n{out[-6000:]}\n"


def last_line(out: str) -> str:
    for line in reversed(out.strip().splitlines()):
        s = line.strip()
        if " passed" in s or " failed" in s or " error" in s:
            return s
    return "?"


def main() -> int:
    new_bytes = TARGET.read_bytes()
    old_bytes = subprocess.run(["git", "show", f"{OLD_REF}:src/api/credential_guard.py"],
                               cwd=str(REPO), capture_output=True, check=True).stdout
    if b"def project_root()" in old_bytes or b"def project_root()" not in new_bytes:
        raise SystemExit("基线/工作树取反了，中止（见 OLD_REF 处注释）")
    print(f"baseline={OLD_REF} has_project_root=False  worktree_has_project_root=True",
          flush=True)
    shutil.copy2(TARGET, REPO / "_credential_guard_new.bak")
    parts: list[str] = []
    try:
        TARGET.write_bytes(old_bytes)
        parts.append(run("OLD CODE @ HEAD"))
        TARGET.write_bytes(new_bytes)
        parts.append(run("NEW CODE @ working tree"))
    finally:
        TARGET.write_bytes(new_bytes)
        (REPO / "_credential_guard_new.bak").unlink(missing_ok=True)

    body = _mask("\n".join(parts))
    tail = (f"\n===== SUMMARY =====\nOLD: {last_line(parts[0])}\n"
            f"NEW: {last_line(parts[1])}\n")
    OUT.write_text(body + tail, encoding="utf-8")
    print(body[-1200:], flush=True)
    print(tail, flush=True)
    print("evidence:", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())