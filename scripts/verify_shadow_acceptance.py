# -*- coding: utf-8 -*-
"""影子卡机制全量机械对账（V1/V2/V3/V7/V10/V11/V13，对测试实例真实 works 目录）。

V2 的「建影子前后」基准：works/ 于补跑前自生产实例逐字节镜像（生产无影子），
故测试库 canvas.json 现值与生产镜像比对＝影子建立未改画布的机械证据。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from src.api import card_tools  # noqa: E402
from src.storage import card_store  # noqa: E402

PROD_WORKS = BASE_DIR.parent / "推演agent" / "works"
TEST_WORKS = BASE_DIR / "works"
SNAP_ROOT = BASE_DIR.parent / "推演agent_test_data_snapshots"

_CARD_RE = re.compile(r"^card_(\d{3,})\.md$")
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} | {detail}")


def main() -> int:
    work_ids = sorted(p.name for p in TEST_WORKS.iterdir() if p.is_dir() and p.name.startswith("work_"))

    # V1/V13：每剧本 shadows 条数＝正卡数，文件名＝正卡编号
    for wid in work_ids:
        cards = sorted(TEST_WORKS / wid / "cards").glob("card_*.md") if False else sorted((TEST_WORKS / wid / "cards").glob("card_*.md"))
        shadows = sorted((TEST_WORKS / wid / "shadows").glob("*.md")) if (TEST_WORKS / wid / "shadows").exists() else []
        c_ids = {p.stem for p in cards}
        s_ids = {p.stem for p in shadows}
        record(f"V1/V13 {wid}: shadows={len(s_ids)} == cards={len(c_ids)}",
               len(s_ids) == len(c_ids) and s_ids == c_ids,
               f"差集={sorted(c_ids ^ s_ids)}")

    # V2：canvas.json 与生产镜像逐字节相同；context_assembler glob 条数与生产一致
    for wid in work_ids:
        tc = TEST_WORKS / wid / "canvas.json"
        pc = PROD_WORKS / wid / "canvas.json"
        same = pc.exists() and tc.exists() and tc.read_bytes() == pc.read_bytes()
        record(f"V2 {wid}: canvas.json 逐字节=生产镜像", same, f"test={tc.stat().st_size if tc.exists() else '-'}B")
        t_cnt = len(list((TEST_WORKS / wid / "cards").glob("card_*.md")))
        p_cnt = len(list((PROD_WORKS / wid / "cards").glob("card_*.md")))
        record(f"V2 {wid}: cards glob 条数不变", t_cnt == p_cnt, f"test={t_cnt} prod={p_cnt}")

    # V3：_next_seq 逐字＝(cards 内最大序号+1)，与 shadows 无关
    for wid in work_ids:
        expect = 1 + max([int(m.group(1)) for p in (TEST_WORKS / wid / "cards").glob("card_*.md")
                          if (m := _CARD_RE.match(p.name))], default=0)
        actual = card_store._next_seq(wid)
        record(f"V3 {wid}: _next_seq={actual} == 期望 {expect}", actual == expect, f"shadows 目录存在={len(list((TEST_WORKS / wid / 'shadows').glob('*.md'))) if (TEST_WORKS / wid / 'shadows').exists() else 0} 个")

    # V7：update_shadow schema 无 card_id 参数
    schemas = {s["name"]: s for s in card_tools.build_tool_schemas()}
    us = schemas.get("update_shadow", {})
    record("V7 update_shadow 已注册", bool(us), f"tools={len(schemas)}")
    record("V7 update_shadow properties 无 card_id", "card_id" not in us.get("parameters", {}).get("properties", {}),
           f"props={sorted(us.get('parameters', {}).get('properties', {}).keys())}")
    rs = schemas.get("read_shadow", {})
    record("V7 read_shadow card_id 可选", rs.get("parameters", {}).get("required", []) == [],
           f"props={sorted(rs.get('parameters', {}).get('properties', {}).keys())}")

    # V10：测试快照根 work_001 最新批次含 shadows/ 路径
    w1 = SNAP_ROOT / "work_001"
    batches = sorted([b for b in w1.iterdir() if b.is_dir() and not b.name.startswith("daily_")]) if w1.exists() else []
    if batches:
        latest = batches[-1]
        shadow_files = [p for p in latest.rglob("*") if "shadows" in p.relative_to(latest).parts]
        record("V10 最新批次含 shadows/ 路径", len(shadow_files) > 0,
               f"batch={latest.name} shadows文件数={len(shadow_files)}")
    else:
        record("V10 最新批次含 shadows/ 路径", False, f"无批次 SNAP_ROOT={SNAP_ROOT}")

    # V11：全部影子含三节（进度/产物清单/迭代历史）
    total, bad = 0, []
    for wid in work_ids:
        sd = TEST_WORKS / wid / "shadows"
        if not sd.exists():
            continue
        for p in sd.glob("*.md"):
            total += 1
            text = p.read_text(encoding="utf-8")
            if not all(h in text for h in ("## 进度", "## 产物清单", "## 迭代历史")):
                bad.append(str(p.relative_to(TEST_WORKS)))
    record(f"V11 全部 {total} 个影子含三节", total > 0 and not bad, f"缺失={bad[:3]}")

    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n机械对账结果：PASS {len(RESULTS) - len(fails)} / FAIL {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
