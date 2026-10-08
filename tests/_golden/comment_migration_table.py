"""T-0009 §3.4 注释迁移对照表生成（只读比对，不改 src）。

铁律2：所有 [E]/[T11]/[T12]/[T14]/RD 编号注释从routes.py 搬到新位置时
**逐字复制，不重述、不改写、不丢**。本脚本机械核验：
  1. 抽取「编号标记」集合（[E]/[T11]/[T12]/[T14]/[T14-x]/[RD-x]/〔代拍…〕等）
  2. 旧文件（拆分前备份）vs 新两文件（routes.py + message_runner.py）
  3. 输出：丢失清单（须为空）、逐条旧位置→新位置对照
判据按「标记＋其所在注释块的归一化文本」比对，不靠行号（行号会漂）。
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict

OLD = os.path.join(os.environ["TEMP"], "t0009_backup", "src", "api", "routes.py")
NEW_ROUTES = r"D:\AI_station\推演agent\src\api\routes.py"
NEW_RUNNER = r"D:\AI_station\推演agent\src\api\message_runner.py"

# 编号标记：形如 [E] [E-探针制] [T11] [T12-R3] [T14-1-1] [RD-03] [T-0007] 等
MARK_RE = re.compile(r"(\[(?:E|T\d+|RD|phase|S\d|T-000\d)[^\]]*\]|〔[^〕]*〕)")


def blocks_with_marks(path: str) -> dict[str, list[str]]:
    """抽「注释块 → 其内出现的编号标记」映射。

    一个注释块＝连续注释行（允许空行分隔的注释段视为不同块）。
    """
    out: dict[str, list[str]] = defaultdict(list)
    block: list[str] = []
    in_block = False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            ln = raw.rstrip("\n")
            s = ln.strip()
            if s.startswith("#"):
                block.append(s)
                in_block = True
                continue
            # 非注释行：结算当前块
            if in_block and block:
                text = " ".join(block)
                for m in MARK_RE.findall(text):
                    out[m].append(text)
                block, in_block = [], False
            # 非注释非空行也结算（防漏）
            if s and in_block:
                block, in_block = [], False
    if block:
        text = " ".join(block)
        for m in MARK_RE.findall(text):
            out[m].append(text)
    return dict(out)


def marks_only(path: str) -> set[str]:
    return set(blocks_with_marks(path).keys())


def norm(s: str) -> str:
    """归一化：压掉内部空白差异（只用于对照展示，不改内容）。"""
    return re.sub(r"\s+", " ", s).strip()


def main() -> int:
    old_marks = marks_only(OLD)
    new_marks = marks_only(NEW_ROUTES) | marks_only(NEW_RUNNER)

    lost = sorted(old_marks - new_marks)
    added = sorted(new_marks - old_marks)

    print("=" * 74)
    print("[§3.4] 注释编号标记迁移核验")
    print("=" * 74)
    print(f"拆分前 routes.py 编号标记种类 = {len(old_marks)}")
    print(f"拆分后（routes + runner）种类  = {len(new_marks)}")
    print()
    print(f"★ 丢失（拆前有、拆后无）= {len(lost)}")
    if lost:
        for m in lost:
            print(f"    !! {m}")
    else:
        print("    （空—— 0 丢失）")
    print()
    print(f"新增（拆后有、拆前无）= {len(added)}")
    for m in added:
        print(f"    ++ {m}")

    print()
    print("=" * 74)
    print("[对照表] 拆前出现位置 → 拆后落点（逐标记）")
    print("=" * 74)
    old_blocks = blocks_with_marks(OLD)
    r_blocks = blocks_with_marks(NEW_ROUTES)
    n_blocks = blocks_with_marks(NEW_RUNNER)
    stayed = migrated = 0
    for m in sorted(old_marks):
        # 逐字找一条在拆后仍存在的同文本块
        hit = None
        for txt in old_blocks.get(m, []):
            key = norm(txt)
            if key in {norm(t) for t in r_blocks.get(m, [])}:
                hit = ("routes.py", key); break
            if key in {norm(t) for t in n_blocks.get(m, [])}:
                hit = ("message_runner.py", key); break
        if hit is None:
            print(f"  {m:22s} → 【未找到逐字相同的块】")
        else:
            where, key = hit
            print(f"  {m:22s} → {where}")
            stayed += 1
    print()
    print(f"逐字对上的标记数 = {stayed} / {len(old_marks)}")
    print("=" * 74)
    ok = not lost and stayed == len(old_marks)
    print(f"总判定：{'通过（0 丢失且逐字全对上）' if ok else '不通过（见上方清单）'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())