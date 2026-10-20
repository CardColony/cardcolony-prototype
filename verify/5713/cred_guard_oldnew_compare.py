# -*- coding: utf-8 -*-
"""「旧代码失败 / 新代码通过」两次跑的证据采集（card_013 返工）。

做法：把 HEAD 版的 credential_guard.py 临时换进工作树跑一遍新用例（=旧代码），
再换回工作树当前版跑一遍（=新代码）。跑完必恢复（finally 兜住）。

**落盘前掩码**：旧跑失败时 pytest 会把 `missing`（装着真实 `.env` 的值）打进
断言细节——这份证据要进版本控制，故任何采集集里的真值一律替换成
`<masked len=N>` 再落盘。明文凭证不进提交文件是本任务的硬边界。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\AI_station\推演agent\.worktrees\5713")
# 基线钉死：旧实现＝card_012 那一版（提交 e639da4）。**绝不能写 HEAD**——本卡
# 一提交，HEAD 就变成新代码了，"旧代码跑"实际跑的是新代码，两次跑次一起绿，
# 前后对照当场失效（2026-10-21 本卡实测踩到，证据作废重做）。
OLD_REF = "e639da4"
TARGET = REPO / "src" / "api" / "credential_guard.py"
OUT = REPO / "verify" / "5713" / "evidence_cred_coverage_oldnew.txt"
REAL_ENV = Path(r"D:\AI_station\推演agent\.env")
TESTS = ["tests/test_credential_guard_coverage.py", "-q", "--no-header",
         "-p", "no:cacheprovider"]


def _mask(text: str) -> str:
    """把真实 .env 的每个值替换成 <masked len=N>（只保长度，不保内容）。"""
    if not REAL_ENV.is_file():
        return text
    sys.path.insert(0, str(REPO))
    from src.api import credential_guard
    values = sorted(credential_guard._harvest_file(REAL_ENV), key=len, reverse=True)
    for v in values:
        if v and v in text:
            text = text.replace(v, f"<masked len={len(v)}>")
    # 二次兜底：`KEY=值` 整体形态也被采集过，上面已覆盖；此处只再扫一遍裸串
    return text


def run(label: str, mask) -> str:
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(REPO)})
    p = subprocess.run([sys.executable, "-m", "pytest", *TESTS], cwd=str(REPO),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    chunk = f"===== {label} (pytest exit={p.returncode}) =====\n{out[-3500:]}\n"
    print(mask(chunk), flush=True)
    return chunk


def summary(out: str) -> str:
    for line in reversed(out.strip().splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            return line.strip()
    return "?"


def main() -> int:
    new_bytes = TARGET.read_bytes()
    old_bytes = subprocess.run(["git", "show", f"{OLD_REF}:src/api/credential_guard.py"],
                               cwd=str(REPO), capture_output=True, check=True).stdout
    # 自检：基线里不该有 project_root（那是本次新增），当前工作树必须有——
    # 两边都不是这一形态就说明基线取错了，直接中止（宁可不产证据，也不产假证据）。
    if b"def project_root()" in old_bytes:
        raise SystemExit(f"基线 {OLD_REF} 里已经有 project_root，取错基线了")
    if b"def project_root()" not in new_bytes:
        raise SystemExit("工作树里没有 project_root，改法没落地？")
    print(f"baseline={OLD_REF} has_project_root=False  worktree_has_project_root=True",
          flush=True)
    pieces: list[str] = []
    try:
        TARGET.write_bytes(old_bytes)
        pieces.append(run("OLD CODE @ HEAD", _mask))
        TARGET.write_bytes(new_bytes)
        pieces.append(run("NEW CODE @ working tree", _mask))
    finally:
        TARGET.write_bytes(new_bytes)

    body = _mask("\n".join(pieces))
    tail = f"\n===== SUMMARY =====\nOLD: {summary(pieces[0])}\nNEW: {summary(pieces[1])}\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(body + tail, encoding="utf-8")
    print(tail, flush=True)
    print("evidence:", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())