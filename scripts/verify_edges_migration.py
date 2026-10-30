"""存量 work 的三线边表迁移验证。

**只读生产库**：把 `cards.db` 拷到临时文件再操作，原库一个字节不碰。

验四件事：
1. 建表成功，三张表都在
2. 迁移后行数 == cards 字段里解析出的条目数（不多不少）
3. 幂等：连跑两次行数不变
4. 读出来与字段一致：随机抽样比对 parent/links/relations

用法：``.venv/Scripts/python.exe scripts/verify_edges_migration.py work_027``
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage import card_db  # noqa: E402


def _expected(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """从 cards 字段里数出「应该有多少条边」。"""
    n_parent = 0
    n_links = 0
    n_rel = 0
    for r in conn.execute("SELECT parent, links, relations FROM cards"):
        if str(r["parent"] or "").strip():
            n_parent += 1
        n_links += len(card_db._json_list(r["links"]))
        n_rel += len(card_db._json_list(r["relations"]))
    return n_parent, n_links, n_rel


def _actual(conn: sqlite3.Connection) -> tuple[int, int, int]:
    def c(t: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    return c("card_parent"), c("card_links"), c("card_relations")


def main(work: str) -> int:
    src = Path("works") / work / "cards.db"
    if not src.exists():
        print(f"✗ 找不到 {src}")
        return 2
    tmp = Path(tempfile.gettempdir()) / f"edges_verify_{work}.db"
    shutil.copyfile(src, tmp)
    print(f"拷贝到 {tmp}（原库未动）")

    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    problems: list[str] = []

    # ---- 1. 建表
    for ddl in card_db._EDGES_DDL:
        conn.execute(ddl)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("card_parent", "card_links", "card_relations"):
        if t not in names:
            problems.append(f"表没建出来：{t}")
    print(f"建表：{sorted(n for n in names if n.startswith('card_'))}")

    # ---- 2. 迁移 + 行数核对
    want = _expected(conn)
    card_db._migrate_edges_from_cards(conn)
    conn.commit()
    got = _actual(conn)
    print(f"应有（字段解析）：parent={want[0]} links={want[1]} relations={want[2]}")
    print(f"实有（边表行数）：parent={got[0]} links={got[1]} relations={got[2]}")
    if got != want:
        problems.append(f"行数不符：want={want} got={got}")

    # ---- 3. 幂等
    card_db._migrate_edges_from_cards(conn)
    conn.commit()
    got2 = _actual(conn)
    if got2 != got:
        problems.append(f"不幂等：第二次跑变成 {got2}")
    else:
        print(f"幂等：第二次跑仍是 {got2}")

    # ---- 4. 抽样比对
    mismatch = 0
    for r in conn.execute("SELECT card_id, parent, links, relations FROM cards"):
        cid = r["card_id"]
        e = card_db.edges_of(conn, cid)
        pid = str(r["parent"] or "").strip()
        if pid and str(e.get("parent") or "") != pid:
            mismatch += 1
            print(f"  ✗ {cid} parent：字段={pid} 表={e.get('parent')}")
        f_links = {str(x.get("target")) for x in card_db._json_list(r["links"])}
        t_links = {str(x.get("target")) for x in (e.get("links") or [])}
        if f_links != t_links:
            mismatch += 1
            print(f"  ✗ {cid} links：字段={sorted(f_links)} 表={sorted(t_links)}")
        f_rel = {str(x.get("target")) for x in card_db._json_list(r["relations"])}
        t_rel = {str(x.get("target")) for x in (e.get("relations") or [])}
        if f_rel != t_rel:
            mismatch += 1
            print(f"  ✗ {cid} relations：字段={sorted(f_rel)} 表={sorted(t_rel)}")
    print(f"逐卡比对：{mismatch} 处不一致")
    if mismatch:
        problems.append(f"逐卡比对有 {mismatch} 处不一致")

    # ---- 5. 反向查询能力（建表的收益）
    try:
        sample = conn.execute(
            "SELECT to_card FROM card_relations LIMIT 1").fetchone()
        if sample is not None:
            tgt = sample["to_card"]
            print(f"反向查询示例：谁依赖 {tgt} → {card_db.dependents_of(conn, tgt)}")
        s2 = conn.execute("SELECT to_card FROM card_links LIMIT 1").fetchone()
        if s2 is not None:
            t2 = s2["to_card"]
            print(f"反向查询示例：谁连了 {t2} → {card_db.linkers_of(conn, t2)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"反向查询失败：{exc}")

    conn.close()
    print("-" * 50)
    if problems:
        print(f"✗ {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "work_027"))
