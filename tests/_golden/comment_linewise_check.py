"""T-0009 §3.4 补充判据 · 逐字文本核验（不看块分组，只看注释文字本身）。

动机：comment_migration_table.py 按「连续注释块」分组比对块文本，拆分后
同一段注释可能被新的相邻注释行分隔成不同的块 ⇒ 分组不同即报「未找到」。
本脚本改按**标记逐个核对**：把拆前含该标记的注释文字（去空白）与拆后
含该标记的注释文字（去空白）做集合比较，只要拆后存在与拆前**任一**原
块归一后完全相同的文本即算逐字保留。
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict

OLD = os.path.join(os.environ["TEMP"], "t0009_backup", "src", "api", "routes.py")
NEW = {
    "routes.py": r"D:\AI_station\推演agent\src\api\routes.py",
    "message_runner.py": r"D:\AI_station\推演agent\src\api\message_runner.py",
}

MARK_RE = re.compile(r"(\[(?:E|T\d+|RD|phase|S\d|T-000\d)[^\]]*\]|〔[^〕]*〕)")
Q = '"' * 3


def comment_lines(path: str) -> list[str]:
    """抽出文件内所有独立注释行（`#` 开头）与 docstring 行，统一视为注释文字。"""
    out: list[str] = []
    in_doc = False
    doc_q = ""
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            s = raw.strip()
            if in_doc:
                out.append(s)
                if doc_q in s:
                    in_doc = False
                continue
            if s.startswith(Q):
                in_doc, doc_q = True, Q
                out.append(s)
                continue
            if s.startswith('"""') and s.count('"""') == 2:
                out.append(s)
                continue
            if s.startswith("#"):
                out.append(s)
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def marks_to_text(path: str) -> dict[str, set[str]]:
    """标记 → 该标记所在行的归一化文本集合。"""
    d: dict[str, set[str]] = defaultdict(set)
    for ln in comment_lines(path):
        for m in MARK_RE.findall(ln):
            d[m].add(norm(ln))
    return d


def main() -> int:
    old = marks_to_text(OLD)
    new_map = {name: marks_to_text(p) for name, p in NEW.items()}
    new_all: dict[str, set[str]] = defaultdict(set)
    for d in new_map.values():
        for k, v in d.items():
            new_all[k] |= v

    lost_marks = sorted(set(old) - set(new_all))
    print("=" * 74)
    print("[§3.4 补充] 逐行注释文字核验（按标记，不按块分组）")
    print("=" * 74)
    print(f"拆前标记种类 = {len(old)}拆后标记种类 = {len(new_all)}")
    print(f"★ 标记丢失 = {len(lost_marks)} {lost_marks if lost_marks else '（无）'}")
    print()

    ok_lines, bad_lines = 0, []
    for m in sorted(old):
        old_lines = old[m]
        new_lines = new_all.get(m, set())
        # 拆前该标记的每一行文本，都必须在拆后原样存在
        missing = [ln for ln in old_lines if ln not in new_lines]
        if not missing:
            ok_lines += 1
        else:
            bad_lines.append((m, missing))

    print(f"逐行完全对上的标记 = {ok_lines} / {len(old)}")
    if bad_lines:
        print()
        print("── 存在文字差异的标记（拆前行 → 拆后形态）──")
        for m, missing in bad_lines:
            print(f"  【{m}】缺{len(missing)} 行：")
            for ln in missing:
                # 找拆后最接近的一行
                cand = max(new_all.get(m, {"<无>"}),
                           key=lambda x: len(set(x) & set(ln)))
                print(f"     拆前: {ln}")
                print(f"     拆后: {cand}")
    print()
    print("=" * 74)
    verdict = (not lost_marks) and (not bad_lines)
    print(f"总判定：{'通过（标记 0 丢失＋逐行文字 0 差异）' if verdict else '仍有差异，见上'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())