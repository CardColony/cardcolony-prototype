# -*- coding: utf-8 -*-
"""[迭代一·会话底座 S2 验收 2] 门面级旧读对账。

对真实 works 树的会话文件**先整树复制到临时目录**（真实树只读、零触碰），
在副本上并行跑旧 `session_store.read_session` 与门面 `session_facade.read`，
逐块比对五键（role/content/timestamp/model/pair_id）＋块数。
验收判据：diff 计数 = 0（任务书 S2 验收标准 2 / 设计判据 1 / T034）。

纪律：
- 不 import main（conftest 铁律二同源）；
- 沙箱手法与 conftest 铁律一同源：改写 session_store.WORKS_DIR 模块属性，
  门面经动态属性访问跟随重定向；
- 真实 works 树只读，副本用完即焚（TemporaryDirectory）。

用法（仓库根）：
    .venv/Scripts/python.exe scripts/reconcile_session_facade.py
产物：stdout 明细 ＋ markdown 报告落盘（默认 D:\\AI_station\\底座差距研究\\）。
退出码：0＝全绿；1＝存在 diff。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.storage import session_facade, session_store  # noqa: E402

FIVE_KEYS = ("role", "content", "timestamp", "model", "pair_id")
DEFAULT_REPORT_DIR = r"D:\AI_station\底座差距研究"


def discover(works: Path) -> list[tuple[str, str, Path]]:
    """枚举 works 树全部会话文件 → (work_id, card_id, 源路径)。"""
    out: list[tuple[str, str, Path]] = []
    for wdir in sorted(works.glob("work_*")):
        sdir = wdir / "sessions"
        if not sdir.exists():
            continue
        for f in sorted(sdir.glob("session_*.md")):
            seq = f.stem.split("_", 1)[1]
            out.append((wdir.name, f"card_{seq}", f))
    return out


async def reconcile(pairs: list[tuple[str, str, Path]]):
    rows: list[dict] = []
    total_diff = 0
    for wid, cid, src in pairs:
        old = await session_store.read_session(wid, cid)
        new = await session_facade.read(wid, cid)
        diffs: list[str] = []
        if len(old) != len(new):
            diffs.append(f"块数不等 old={len(old)} new={len(new)}")
        else:
            for i, (o, n) in enumerate(zip(old, new)):
                for k in FIVE_KEYS:
                    if o.get(k) != n.get(k):
                        diffs.append(f"块{i}.{k}: old={o.get(k)!r} new={n.get(k)!r}")
        total_diff += len(diffs)
        rows.append({"work": wid, "card": cid, "blocks": len(old),
                     "diffs": diffs})
    return rows, total_diff


def main() -> int:
    real_works = ROOT / "works"
    if not real_works.exists():
        print(f"[FATAL] works 树不存在: {real_works}")
        return 2
    pairs = discover(real_works)
    print(f"[对账] 发现会话文件 {len(pairs)} 个（work_001~006 实测口径应为 70）")

    with tempfile.TemporaryDirectory(prefix="facade_reconcile_") as td:
        tmp_works = Path(td) / "works"
        shutil.copytree(real_works, tmp_works)
        session_store.WORKS_DIR = tmp_works  # 沙箱重定向（铁律一同手法；真实树只读）
        rows, total_diff = asyncio.run(reconcile(pairs))

    bad = [r for r in rows if r["diffs"]]
    print(f"[对账] 文件数={len(rows)}  块数合计={sum(r['blocks'] for r in rows)}  "
          f"diff 总数={total_diff}  异常文件数={len(bad)}")
    for r in bad:
        print(f"  [DIFF] {r['work']}/{r['card']} blocks={r['blocks']}")
        for d in r["diffs"][:5]:
            print(f"      {d}")
    verdict = "全部等价（diff=0）——门面级旧读对账通过" if total_diff == 0 else "存在 diff——对账不通过"

    lines = [
        "# 迭代一·会话底座 S1/S2 门面级旧读对账输出",
        "",
        f"- 时点：{datetime.now().isoformat(timespec='seconds')}",
        f"- 对象：真实 works 树整树副本（{len(pairs)} 个会话文件，真实树只读）",
        "- 比对：旧 `session_store.read_session` vs 门面 `session_facade.read`，"
        "五键（role/content/timestamp/model/pair_id）逐块＋块数",
        f"- **结论：{verdict}**",
        f"- diff 总数：{total_diff}；异常文件数：{len(bad)}",
        "",
        "| work | card | 块数 | diff 数 | 明细（前 3） |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        detail = "；".join(r["diffs"][:3]).replace("|", "\\|") if r["diffs"] else "—"
        lines.append(f"| {r['work']} | {r['card']} | {r['blocks']} | {len(r['diffs'])} | {detail} |")
    report = Path(DEFAULT_REPORT_DIR) / "迭代一-S1S2-门面级旧读对账输出-20261010.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[对账] 报告已落盘: {report}")
    return 0 if total_diff == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
