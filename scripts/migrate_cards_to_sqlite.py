# -*- coding: utf-8 -*-
"""卡片存储迁移 SQLite · 第二步 2a：md → 库 迁移脚本（含 2b 对账）。

╔══════════════════════════════════════════════════════════════════════════════╗
║ ⚠️ 历史一次性脚本（2026-10-14 已执行完毕，勿在常规流程里跑）                  ║
║                                                                              ║
║ 本脚本已于 2026-10-14 完成 13 个 work 的 md → SQLite 迁移（卡 94／影 94／    ║
║ 序列 11，对账差异 0），随后**卡片与影子 md 已按 2d 删除**。                    ║
║ 因此现在再跑：                                                                ║
║   · ``--dry-run`` / ``--verify``：按 md 为准对账，**md 已不存在** ⇒ 无意义；  ║
║   · ``--migrate``：脚本对「无 md 但库内有卡」**拒绝清库**（防误删已迁数据）。  ║
║ 保留本脚本只为**留痕**（迁移口径与对账逻辑的唯一实现）；不要用它做任何回退——  ║
║ 回退走快照：``python restore_from_snapshot.py``。                             ║
╚══════════════════════════════════════════════════════════════════════════════╝

**只读 md、只写库；绝不改、绝不删任何 md**（任务书 §六 红线 1）。
删 md 是 §七 2d 的单独一步、在对账通过之后。

幂等：每个 work 先清库内**数据行**（schema 不动），再按 md 重建——可反复重跑，
第二次结果与第一次逐字相同。

用法::

    python scripts/migrate_cards_to_sqlite.py --dry-run    # 只读体检（不写库）
    python scripts/migrate_cards_to_sqlite.py --migrate    # 迁移（写库）
    python scripts/migrate_cards_to_sqlite.py --verify     # 对账（库 vs md 逐字）

跑 ``--migrate`` 之前（任务书 §六 红线 3/4）::

    1) 先拍一次全量快照（回退通道）；
    2) ``python restart_safe.py --check`` 确认无在途调用 → 停服务 → 再跑本脚本。
       （不变量：迁移期间服务必须停止，消除「旧代码并发写 md ⇒ 库漏掉这次写」的竞态。）

数据口径（任务书 §四）：
- 复用现有解析函数（``hash_utils.parse_front_matter`` / ``split_body_sections``、
  ``shadow_store.split_shadow_sections``）——与旧读侧逐字同口径，不另写解析。
- 读侧归一照搬：``card_type`` 缺 → ``"线索"``；``parent`` 键缺 → ``work_card_id``。
- 编号序列进 ``meta`` 表（``seq`` ＋ ``issued``）。
- 逐卡断言「三块拼起来不丢内容」（防无区头正文被拆空）。
- 缺影子的卡补派生（与运行时 ``derive_shadow`` 同法：三节模板）。

★ 影子前置内容（**已裁定 2026-10-14 · A 案＝丢弃**，见 ``_report_shadow_preamble``）：
影子正文里「首个 ``## `` 区头之前」的内容，在库内**没有列可放**（三列＝三节）。
裁定＝**丢弃但不静默**（逐条点名 ＋ 抄录原文；原始字节另存于迁移前全量快照）。
依据：第一步交付的运行期读法本就丢弃这段内容 ⇒ 迁移不引入新行为。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.storage import card_db, card_store, shadow_store  # noqa: E402
from src.storage.hash_utils import (  # noqa: E402
    NEW_SECTION_MAPPING, OLD_SECTION_MAPPING, parse_front_matter, split_body_sections,
)

WORK_RE = re.compile(r"^work_\d{3,}$")
_KNOWN_HEADINGS = set(NEW_SECTION_MAPPING) | set(OLD_SECTION_MAPPING)
_SECTION_KEYS = ("mission", "status_block", "rulings")


class MigrationRefused(Exception):
    """撞上「任务书与实盘/机制冲突」——停手，不写任何东西。"""


# ===========================================================================
# 只读解析
# ===========================================================================

def iter_works() -> list[str]:
    """works/ 下含 work.json 的 work 目录（升序）。"""
    root = card_store.WORKS_DIR
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and WORK_RE.match(p.name) and (p / "work.json").exists())


def work_card_id(work_id: str) -> str:
    """根卡 id（读 work.json；不引 work_store 以免带进活跃度现算）。"""
    jf = card_store.WORKS_DIR / work_id / "work.json"
    try:
        return str(json.loads(jf.read_text(encoding="utf-8")).get("work_card_id") or "")
    except Exception:  # noqa: BLE001
        return ""


def _content_lines(text: str) -> Counter:
    """参与「不丢内容」比对的实质行：去掉空行，保留**未识别**的 ``## `` 行
    （它们是内容行——旧拆分器只认已知区头），去掉已知区头行（会被规范区头替换）。"""
    return Counter(l for l in text.splitlines() if l.strip() and l.strip() not in _KNOWN_HEADINGS)


def parse_card_md(path: Path, work_card: str) -> dict[str, Any]:
    """一张卡 md → {meta, body, history, raw_body}。**纯读。**"""
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(raw)
    sections = split_body_sections(body)
    # 读侧归一（与旧 read_card / _meta_of 的兜底逐字一致）
    if not meta.get("card_type"):
        meta["card_type"] = "线索"
    if "parent" not in meta:
        meta["parent"] = work_card
    blocks = {
        "mission": sections["mission"],
        "status_block": sections["status_block"],
        # 库内第三块只有一列 rulings；`## 需求文档` 形态归入同列
        # （两者同时非空 ⇒ 下面的不丢内容断言会拦下，不静默丢字）
        "rulings": sections["rulings"] or sections["requirements"],
    }
    history = [l for l in sections["history_text"].splitlines() if l.strip()]
    return {"card_id": path.stem, "meta": meta, "body": blocks,
            "history": history, "raw_body": body}


def parse_shadow_md(path: Path) -> dict[str, Any]:
    """一个影子 md → {owner, created_at, updated_at, sections, preamble}。**纯读。**"""
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(raw)
    preamble: list[str] = []
    for line in body.splitlines():
        if line.strip().startswith("## "):
            break
        if line.strip():
            preamble.append(line)
    return {
        "owner": path.stem,
        "created_at": str(meta.get("created_at") or ""),
        "updated_at": str(meta.get("updated_at") or ""),
        "sections": shadow_store.split_shadow_sections(body),
        "preamble": preamble,
    }


def read_seq(work_id: str) -> tuple[int, list[str]] | None:
    p = card_store.WORKS_DIR / work_id / ".card_id_seq.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return int(data.get("seq", 0) or 0), list(data.get("issued") or [])


def md_digest(work_ids: list[str]) -> str:
    """全部卡片/影子 md 的内容指纹（**只读**）——用于钉「md 零改动」（2a 硬判据 5）。"""
    h = hashlib.sha256()
    for wid in work_ids:
        for sub in ("cards", "shadows"):
            d = card_store.WORKS_DIR / wid / sub
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.md")):
                h.update(f"{wid}/{sub}/{p.name}".encode("utf-8"))
                h.update(p.read_bytes())
    return h.hexdigest()


# ===========================================================================
# 断言（先失败先退出：全部解析＋断言做完，才允许写库）
# ===========================================================================

def _check_no_loss(card: dict[str, Any]) -> None:
    """§四决策 7：三块 ＋ 修改历史 拼起来不得丢内容。"""
    recon = "\n".join([card["body"][k] for k in _SECTION_KEYS] + card["history"])
    lost = _content_lines(card["raw_body"]) - _content_lines(recon)
    if lost:
        raise MigrationRefused(
            f"{card['card_id']}: 三块拼起来丢内容 {sum(lost.values())} 行，例如 {list(lost)[:3]}"
        )


def _report_shadow_preamble(shadows: list[dict[str, Any]]) -> None:
    """★ 已裁定（用户 2026-10-14 · **A 案＝丢弃**）：影子正文「首个 ``## `` 区头之前」
    的内容，库内无列可放（三列＝三节）⇒ **丢弃，但不静默**——逐条点名并抄录原文。

    裁定依据：
    - 第一步交付的运行期读法（``shadow_store.split_shadow_sections``）**本就丢弃**
      这段内容 ⇒ 迁移**不引入新行为**（旧 md 时代才看得到，第一步之后已看不到）；
    - 不并入某节（会违反「各区域独立、不得混」）；
    - 不为畸形内容给表加列（其中 2 处是**过期重复 front matter**，纯垃圾）。
    原始字节另存于**迁移前全量快照**（红线 4），故丢弃可回退。
    """
    bad = [s for s in shadows if s["preamble"]]
    if not bad:
        print("[影子前置内容] 0 处 —— 无需处置")
        return
    print(f"[影子前置内容] {len(bad)} 处 —— 按裁定 A **丢弃**"
          f"（运行期读法本就丢弃；原文抄录如下，原始字节另存于迁移前快照）")
    for s in bad:
        print(f"    - {s['owner']}:")
        for ln in s["preamble"]:
            print(f"        | {ln}")


# ===========================================================================
# 逐 work 采集（只读）
# ===========================================================================

def collect_work(work_id: str) -> dict[str, Any]:
    wd = card_store.WORKS_DIR / work_id
    cards_dir, shadows_dir = wd / "cards", wd / "shadows"
    card_paths = sorted(cards_dir.glob("card_*.md")) if cards_dir.is_dir() else []
    shadow_paths = sorted(shadows_dir.glob("*.md")) if shadows_dir.is_dir() else []

    work_card = work_card_id(work_id)
    cards = [parse_card_md(p, work_card) for p in card_paths]
    for c in cards:
        _check_no_loss(c)
    shadows = [parse_shadow_md(p) for p in shadow_paths]

    card_ids = [c["card_id"] for c in cards]
    have_shadow = {s["owner"] for s in shadows}
    derived = [cid for cid in card_ids if cid not in have_shadow]

    return {
        "work_id": work_id,
        "cards": cards,
        "shadows": shadows,
        "derived_shadows": derived,
        "seq": read_seq(work_id),
        "n_card_md": len(card_paths),
        "n_shadow_md": len(shadow_paths),
    }


# ===========================================================================
# 写库（幂等：先清数据行，再重建）
# ===========================================================================

async def migrate_work(collected: dict[str, Any]) -> dict[str, Any]:
    work_id = collected["work_id"]
    wd = card_store.WORKS_DIR / work_id
    has_md = collected["n_card_md"] > 0

    if not has_md:
        # 无卡片 md：可能是空 work，也可能是「已删 md」之后的状态。
        # 后者绝不能清库（会把已迁数据抹掉）——库内已有卡就拒绝。
        existing = await card_db.run_read(
            work_id, lambda c: int(c.execute("SELECT COUNT(*) n FROM cards").fetchone()["n"]), 0)
        if existing:
            raise MigrationRefused(
                f"{work_id}: 无卡片 md 但库内已有 {existing} 张卡 —— 疑似「已删 md」之后重跑，"
                "拒绝清库（防误删已迁数据）"
            )
        await card_db.create_db(work_id, allow_unmigrated=True)
        def _empty(conn: Any) -> None:
            card_db.clear_all(conn)
            card_db.bump_rev(conn)
        await card_db.run_write(work_id, _empty)
        return {"work_id": work_id, "cards": 0, "shadows": 0, "empty": True}

    await card_db.create_db(work_id, allow_unmigrated=True)
    now = card_store._now_iso()
    template = shadow_store.split_shadow_sections(shadow_store.SHADOW_BODY_TEMPLATE)
    # 补派生影子的时间戳＝**卡的 created_at**（缺则 updated_at，再缺迁移时刻）：
    # 其余影子的 created_at 本就≈卡创建时刻（建卡即派生），这样最一致。
    card_ts = {
        c["card_id"]: str(c["meta"].get("created_at") or c["meta"].get("updated_at") or "")
        for c in collected["cards"]
    }

    def _fn(conn: Any) -> None:
        card_db.clear_all(conn)
        for c in collected["cards"]:
            meta = dict(c["meta"])
            meta["card_id"] = c["card_id"]
            meta["work_id"] = work_id
            card_db.insert_card(conn, meta, c["body"])
            for line in c["history"]:
                card_db.append_history(conn, c["card_id"], line)
        for s in collected["shadows"]:
            card_db.insert_shadow(conn, s["owner"], work_id,
                                  s["created_at"], s["updated_at"], s["sections"])
        for cid in collected["derived_shadows"]:
            # 补派生（三节模板）；时间戳见上方 card_ts（＝卡的 created_at）
            ts = card_ts.get(cid) or now
            card_db.insert_shadow(conn, cid, work_id, ts, ts, template)
        if collected["seq"] is not None:
            seq, issued = collected["seq"]
            card_db.set_meta_value(conn, "seq", str(seq))
            card_db.set_meta_value(conn, "issued", json.dumps(issued, ensure_ascii=False))
        card_db.bump_rev(conn)

    await card_db.run_write(work_id, _fn)
    return {"work_id": work_id, "cards": len(collected["cards"]),
            "shadows": len(collected["shadows"]) + len(collected["derived_shadows"]),
            "derived": collected["derived_shadows"]}


# ===========================================================================
# 2b 对账（库 vs md，逐字）
# ===========================================================================

async def verify_work(collected: dict[str, Any]) -> dict[str, Any]:
    work_id = collected["work_id"]
    diffs: list[str] = []

    def _fn(conn: Any) -> None:
        for c in collected["cards"]:
            row = card_db.get_card_row(conn, c["card_id"])
            if row is None:
                diffs.append(f"{work_id}/{c['card_id']}: 库内缺该卡")
                continue
            meta = card_db.row_to_meta(row)
            for key in ("card_type", "parent", "title", "source", "session_ref",
                        "created_at", "updated_at"):
                if str(meta.get(key, "") or "") != str(c["meta"].get(key, "") or ""):
                    diffs.append(f"{work_id}/{c['card_id']}: 字段 {key} 不一致 "
                                 f"库={meta.get(key)!r} md={c['meta'].get(key)!r}")
            for blk in _SECTION_KEYS:
                if (row[blk] or "") != c["body"][blk]:
                    diffs.append(f"{work_id}/{c['card_id']}: 正文块 {blk} 不一致")
            if card_db.get_history(conn, c["card_id"]) != c["history"]:
                diffs.append(f"{work_id}/{c['card_id']}: 修改历史不一致")
        for s in collected["shadows"]:
            row = card_db.get_shadow_row(conn, s["owner"])
            if row is None:
                diffs.append(f"{work_id}/shadow_{s['owner']}: 库内缺该影子")
                continue
            for col in ("progress", "artifacts", "iterations"):
                if (row[col] or "") != s["sections"][col]:
                    diffs.append(f"{work_id}/shadow_{s['owner']}: 节 {col} 不一致")
            for col, key in (("created_at", "created_at"), ("updated_at", "updated_at")):
                if str(row[col] or "") != str(s[key] or ""):
                    diffs.append(f"{work_id}/shadow_{s['owner']}: {col} 不一致")
        for cid in collected["derived_shadows"]:
            if card_db.get_shadow_row(conn, cid) is None:
                diffs.append(f"{work_id}/shadow_{cid}: 补派生的影子不在库内")
        n_cards = int(conn.execute("SELECT COUNT(*) n FROM cards").fetchone()["n"])
        n_sh = int(conn.execute("SELECT COUNT(*) n FROM shadows").fetchone()["n"])
        if n_cards != len(collected["cards"]):
            diffs.append(f"{work_id}: 卡数 库={n_cards} md={len(collected['cards'])}")
        want_sh = len(collected["shadows"]) + len(collected["derived_shadows"])
        if n_sh != want_sh:
            diffs.append(f"{work_id}: 影数 库={n_sh} 期望={want_sh}")
        if collected["seq"] is not None:
            seq, _ = collected["seq"]
            got = card_db.get_meta_value(conn, "seq", None)
            if str(got) != str(seq):
                diffs.append(f"{work_id}: meta.seq 库={got} md={seq}")

    await card_db.run_read(work_id, _fn, None)
    return {"work_id": work_id, "diffs": diffs}


# ===========================================================================
# 入口
# ===========================================================================

async def run(mode: str) -> int:
    works = iter_works()
    print(f"[works] {len(works)} 个: {works}")
    digest_before = md_digest(works)
    print(f"[md-digest] before = {digest_before}")

    collected = [collect_work(w) for w in works]     # 全部解析＋断言（先失败先退出）
    n_cards = sum(c["n_card_md"] for c in collected)
    n_shadows = sum(c["n_shadow_md"] for c in collected)
    n_derived = sum(len(c["derived_shadows"]) for c in collected)
    n_seq = sum(1 for c in collected if c["seq"] is not None)
    print(f"[体检] 卡片 md={n_cards} 影子 md={n_shadows} 补派生={n_derived} 编号序列={n_seq}")
    print(f"[体检] 缺影子的卡: {[d for c in collected for d in c['derived_shadows']]}")

    # ★ 已裁定（A 案）：影子「首个区头之前」的内容按裁定**丢弃**——逐条点名并抄录
    _report_shadow_preamble([s for c in collected for s in c["shadows"]])

    if mode == "dry-run":
        print("[dry-run] 只读体检完成，未写任何东西。")
        return 0

    if mode == "migrate":
        results = []
        for c in collected:
            r = await migrate_work(c)
            results.append(r)
            print(f"  [migrate] {r}")
        print(f"[migrate] 合计 卡={sum(r['cards'] for r in results)} "
              f"影={sum(r['shadows'] for r in results)}")

    # verify（--migrate 后自动跟一次；--verify 单独跑）
    all_diffs: list[str] = []
    for c in collected:
        r = await verify_work(c)
        all_diffs.extend(r["diffs"])
    print(f"[对账] 差异数 = {len(all_diffs)}")
    for d in all_diffs[:50]:
        print("   DIFF:", d)

    digest_after = md_digest(works)
    print(f"[md-digest] after  = {digest_after}")
    print(f"[md-零改动] {digest_before == digest_after}")
    return 0 if not all_diffs else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="卡片 md → SQLite 迁移（第二步 2a/2b）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只读体检，不写库")
    g.add_argument("--migrate", action="store_true", help="迁移（写库；幂等可重跑）")
    g.add_argument("--verify", action="store_true", help="只对账，不写库")
    args = ap.parse_args()
    mode = "dry-run" if args.dry_run else ("migrate" if args.migrate else "verify")
    try:
        return asyncio.run(run(mode))
    except MigrationRefused as exc:
        print(f"\n[MIGRATION REFUSED] {exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
