# -*- coding: utf-8 -*-
"""把存量卡迁移到「模式 + 能力挂载」（[能力底座 2026-10-30]）。

**做什么**：给每张卡算出 ``mode``（manual / auto）与 ``abilities``（能力 code 数组），
让工具面从此由「模式上限 ∩ 卡上挂载」决定，而不是由 ``perms`` 那 19 个开关决定。

**怎么保证不改变行为**：算完先不写库，把 abilities 临时塞进卡 meta 里，走一遍真实的
``build_tool_schemas``，和迁移前的工具面**逐字比对**。差一件就列出来、默认不写。

    python scripts/migrate_cards_to_capabilities.py            # 只比对（dry-run）
    python scripts/migrate_cards_to_capabilities.py --apply    # 比对通过才写库

abilities 的算法（保持等价）：

    perms 覆盖的能力 ＝ 该能力的工具里，**至少一件**的开关在卡的 perms 里
                      ∪ 该能力的工具**全都没有开关**（这类本来就不由 perms 管）
    abilities       ＝ 上面这集合 ∩ 该模式该卡型的上限

「至少一件」而不是「全部」：今天的工具面是**逐工具**过滤的，同一能力里有的工具有开关、
有的没有（如「看卡」= read_card + read_linked），按能力粒度只能取"覆盖到就挂"。
是否等价，由逐卡比对说了算。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from src.api import card_tools  # noqa: E402
from src.infra import capability_registry as cap  # noqa: E402
from src.storage import work_store  # noqa: E402


def _perms_caps(meta: dict) -> set[str]:
    """这张卡的 perms 覆盖到哪些能力。

    用 ``policy.has_perm`` 而不是裸查 perms 列表——它带**单向隐含**
    （``tools_file`` ⇒ 具备 ``tools_read_file``），裸查会把「看文件」判成没授权，
    实测 coder 卡会因此丢掉 list_files/read_file/search_files/glob_files 四件。
    """
    from src.permissions import policy

    out: set[str] = set()
    for c in cap.capabilities():
        tools = c["impl"].get("tools") or []
        if not tools:
            continue
        switches = [card_tools.TOOL_SWITCH.get(t) for t in tools]
        if any(s is None for s in switches) or any(
                policy.has_perm(meta, s) for s in switches if s):
            out.add(c["code"])
    return out


def _abilities_of(meta: dict, mode: str) -> list[str] | None:
    ctype = str(meta.get("card_type") or "")
    upper = cap.mode_upper(mode, ctype)
    if upper is None:
        return None                      # 该模式没配这个卡型 ⇒ 不迁移，留给人工
    allowed = set(upper) & _perms_caps(meta)
    return sorted(allowed)


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    works = [w.get("work_id") for w in work_store.list_works() if w.get("work_id")]
    if not cap.load().get("enabled"):
        print("[migrate] 能力清单不可用，退出")
        return 2

    total = changed = skipped = 0
    diffs: list[str] = []
    plan: list[tuple[str, str, str, list[str]]] = []

    for wid in works:
        cards = card_tools._collect_card_meta(wid)
        if not cards:
            continue
        # 迁移前的面（此刻 abilities 都还是 None）
        before = {cid: {t["name"] for t in card_tools.build_tool_schemas(
            card_id=cid, work_id=wid, cards=cards)}
            for cid in cards if not cid.startswith("shadow_")}

        for cid in before:
            meta = cards[cid]
            mode = "auto" if bool(meta.get("auto_mode")) else "manual"
            ab = _abilities_of(meta, mode)
            if ab is None:
                skipped += 1
                diffs.append(f"{wid}/{cid} 卡型 {meta.get('card_type')} 在模式 {mode} 里没有上限")
                continue
            total += 1
            # 模拟：把 abilities 塞进 meta 再算一遍
            cards[cid]["abilities"] = ab
            after = {t["name"] for t in card_tools.build_tool_schemas(
                card_id=cid, work_id=wid, cards=cards)}
            cards[cid]["abilities"] = None
            if after != before[cid]:
                lost = sorted(before[cid] - after)
                gained = sorted(after - before[cid])
                diffs.append(f"{wid}/{cid}（{meta.get('card_type')}/{mode}）"
                             f" 少: {lost or '无'} 多: {gained or '无'}")
            else:
                changed += 1
                plan.append((wid, cid, mode, ab))

    print(f"[migrate] work {len(works)} 个 | 卡 {total} 张 | 等价 {changed} | 有差异 {len(diffs)} | 跳过 {skipped}")
    if diffs:
        print("\n有差异的卡（前 30 条）：")
        for d in diffs[:30]:
            print("  " + d)

    if not apply:
        print("\n[migrate] dry-run：没有写库。加 --apply 才写。")
        return 0 if not diffs else 1
    if diffs:
        print("\n[migrate] 有差异 ⇒ 拒绝写库（先看差异是不是可接受）")
        return 1

    import json
    import sqlite3

    from src.storage import card_db

    by_work: dict[str, list[tuple[str, str, list[str]]]] = {}
    for wid, cid, mode, ab in plan:
        by_work.setdefault(wid, []).append((cid, mode, ab))

    # 直连 sqlite 写，不走 card_store.update_card：那条路要 async 写事务 +
    # 事件循环（脚本里 asyncio.run 会卡住实测 3 分钟不动）。这里只改两列，
    # 一个 work 一个事务，几十毫秒。
    done = 0
    for wid, rows in by_work.items():
        db = BASE / "works" / wid / "cards.db"
        if not db.exists():
            print(f"[migrate] 跳过 {wid}（没有 cards.db）")
            continue
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row      # bump_rev 按列名取行
        try:
            conn.execute("BEGIN IMMEDIATE")
            for cid, mode, ab in rows:
                conn.execute(
                    "UPDATE cards SET mode=?, abilities=?, updated_at=datetime('now')"
                    " WHERE card_id=?",
                    (mode, json.dumps(ab, ensure_ascii=False), cid))
            card_db.bump_rev(conn)
            conn.commit()
            done += len(rows)
        except Exception as exc:  # noqa: BLE001 一个 work 失败不挡其余
            conn.rollback()
            print(f"[migrate] {wid} 写入失败（已回滚）: {exc}")
        finally:
            conn.close()

    print(f"\n[migrate] 已写入 {done} 张卡（{len(by_work)} 个 work）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
