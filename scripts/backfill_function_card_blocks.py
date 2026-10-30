# -*- coding: utf-8 -*-
"""回填存量职能卡的三块正文（改前建卡时落的是普通卡占位模板）。

**问题**（2026-10-23 用户指出）：
职能卡建卡时 `ensure_function_card` 一个块都不传 ⇒ 落
`card_store.NEW_CARD_SKELETON` 的通用占位模板：
    ## 使命   （本卡作用/目标、授权来源；≤5行；立项定稿，改动需用户确认）
    ## 现状   （当前状况快照：做到哪、什么状态、卡在哪…）
    ## 需求文档（本卡全部需求的确定版…）
卡面看起来和普通项目卡一模一样。角色卡不是这么建的——它显式拼 body
（使命＝职责书全文 / 现状＝值班中 / 需求文档＝空）。

**已修**：`ensure_function_card` 改为调 `_function_card_body()` 拼同样的形态。
**本脚本**：把**存量**那些仍是占位模板的职能卡按同一形态回填。

判据（只动真正没填过的卡）：`mission` 以 `（本卡作用/目标、授权来源` 开头。
已经有人写过内容的卡一律不碰。

用法::

    python scripts/backfill_function_card_blocks.py            # 干跑
    python scripts/backfill_function_card_blocks.py --apply    # 写入
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLACEHOLDER_PREFIX = "（本卡作用/目标、授权来源"


def _split_body(body: str) -> tuple[str, str, str]:
    """把 `## 使命 / ## 现状 / ## 需求文档` 正文切成三块。"""
    out = {"mission": "", "status_block": "", "requirements": ""}
    cur = ""
    buf: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("## "):
            head = s[3:].strip()
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur = {"使命": "mission", "现状": "status_block",
                   "需求文档": "requirements"}.get(head, "")
            buf = []
            continue
        buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out["mission"], out["status_block"], out["requirements"]


def _find_placeholder(work_id: str) -> list[tuple[str, str]]:
    db = ROOT / "works" / work_id / "cards.db"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return [(r["card_id"], r["title"] or "") for r in con.execute(
            "SELECT card_id, title, mission FROM cards WHERE card_type='function'")]
    finally:
        con.close()


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填存量职能卡三块正文")
    ap.add_argument("--apply", action="store_true", help="真正写入（缺省只报告）")
    ap.add_argument("--work", default="", help="只处理某个 work")
    args = ap.parse_args(argv)

    from src.api import card_tools
    from src.storage import card_store

    works = sorted(p.name for p in (ROOT / "works").glob("work_*") if p.is_dir())
    if args.work:
        works = [w for w in works if w == args.work]

    rows: list[tuple[str, str, str]] = []
    for w in works:
        for cid, title in _find_placeholder(w):
            try:
                card = await card_store.read_card(w, cid)
                mission = str(((card or {}).get("body") or {}).get("mission") or "")
            except Exception:
                mission = ""
            if not str(mission or "").startswith(PLACEHOLDER_PREFIX):
                continue
            rows.append((w, cid, title))

    if not rows:
        print("没有需要回填的职能卡（都不是占位模板）。")
        return 0

    print(f"=== {'已应用' if args.apply else '干跑（未改动）'} ==="
          f" {len(rows)} 张职能卡仍是占位模板\n")
    ok = 0
    for w, cid, title in rows:
        body = await card_tools._function_card_body(w, cid)
        mission, status_block, requirements = _split_body(body)
        print(f"  {w:10} {cid:8} {title[:18]:18} "
              f"使命 {len(mission):>4} 字符 | 现状 {len(status_block):>3} | 需求 {len(requirements):>3}")
        if not args.apply:
            continue
        res = await card_store.update_card(
            w, cid,
            {"mission": mission, "status_block": status_block, "requirements": requirements},
            "职能卡三块回填：建卡时误落普通卡占位模板（2026-10-23）",
        )
        if "error" in res:
            print(f"      !! 写入失败: {res['error']}")
        else:
            ok += 1

    if args.apply:
        print(f"\n已回填 {ok}/{len(rows)} 张。")
    else:
        print("\n干跑结束。要真正写入：加 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
