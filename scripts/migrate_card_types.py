# -*- coding: utf-8 -*-
"""存量类型迁移 · 映射预览（只读）与执行（--apply，唯一写门）

用法：
    # 预览（只读，默认）
    MOCK_LLM=1 .venv/Scripts/python.exe -m scripts.migrate_card_types --preview
    # 执行（先快照 → 唯一写门逐卡迁移 → 后快照 → 复核）
    MOCK_LLM=1 .venv/Scripts/python.exe -m scripts.migrate_card_types --apply --tag research-migrate

**默认是预览模式**：只读生产 works/，输出逐卡映射方案 + 冲突项，不写盘。
确认无误后加 `--apply` 才真正执行（执行前强制再取一次快照）。

历史批次：
  T7（rd 树）：work_002 的 67 张存量「线索」→ sub_project（已执行完毕）。
  研究树迁移（[研究项目词表与迁移] 任务书 T2）：work_001/work_003 的
  「线索」→ topic（用户 2026-10-08 裁定「研究项目的子卡统一叫课题卡」）。
两批共用本脚本；映射关系固化为 LEGACY_MIGRATION_BY_WORK 常量（可改）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.storage.card_store import (  # noqa: E402
    _meta_of, _card_path, load_type_registry, _l1_all_codes,
)
from src.storage import work_store  # noqa: E402

WORKS_DIR = BASE_DIR / "works"


# ================================================================
# 项目分类裁定（用户 2026-10-08 16:09 口头定案）
#   work_001 股权财政      → research_project
#   work_002 项目管理agent  → rd_project（研发，词表已定义）
#   work_003 记忆机制论文  → research_project
# ================================================================
PROJECT_CLASS_BY_WORK: dict[str, str] = {
    "work_001": "research_project",
    "work_002": "rd_project",
    "work_003": "research_project",
}

# ================================================================
# 「线索」迁移目标映射（固化为可改常量，与 PROJECT_CLASS_BY_WORK 同模式）。
#   rd_project        → sub_project（T7 批次，已执行；保留条目保证幂等重跑安全）
#   research_project  → topic（研究树批次，[研究项目词表与迁移] 任务书 T2——
#     用户 2026-10-08 裁定：「研究项目的子卡统一叫课题卡」）
# 未列出的 work → 线索子卡不动（⚠ 人工确认）。
# ================================================================
LEGACY_MIGRATION_BY_WORK: dict[str, str] = {
    "work_001": "topic",
    "work_002": "sub_project",
    "work_003": "topic",
}


def collect() -> list[dict]:
    """只读扫描全部卡的当前类型与拟定目标。"""
    reg = load_type_registry()
    out: list[dict] = []
    for wd in sorted(WORKS_DIR.glob("work_*")):
        wid = wd.name
        if work_store.read_work(wid) is None:
            continue
        wc_id = work_store.work_card_id_of(wid)
        wc_meta = _meta_of(wid, wc_id) or {}
        cur_pc = str(wc_meta.get("project_class", "") or "")
        target_pc = PROJECT_CLASS_BY_WORK.get(wid, "")
        pc_needs_confirm = (not target_pc)
        # [研究树批次] 暂缓条件＝本树 project_class 未定（防御面）。
        # research_project 词表已立项（topic），不再按「非 rd」一律暂缓。
        subtree_deferred = (not target_pc)
        for p in sorted((wd / "cards").glob("card_*.md")):
            m = _meta_of(wid, p.stem) or {}
            cur_type = str(m.get("card_type", "") or "")
            is_root = p.stem == wc_id
            row = {
                "work_id": wid, "card_id": p.stem,
                "title": str(m.get("title", "")),
                "is_root": is_root,
                "cur_type": cur_type,
                "cur_project_class": cur_pc,
                "pc_needs_confirm": pc_needs_confirm and is_root,
                # [研究树批次] research 树词表已立项（topic），子卡不再一律暂缓：
                # 「线索」按 LEGACY_MIGRATION_BY_WORK 归 topic；暂缓仅剩
                # 「pc 未定」的树（防御面保留，当前三棵树 pc 全部已标）。
                "subtree_deferred": (not is_root) and (not target_pc),
                "owner_card_id": str(m.get("owner_card_id", "") or ""),
                "parent": str(m.get("parent", "") or ""),
            }
            if is_root:
                row["to_type"] = cur_type              # 根卡类型不动
                row["to_project_class"] = target_pc     # 只补 L0
                row["rule"] = (f"根卡：类型保留，project_class → {target_pc}"
                               if target_pc else "根卡：项目分类未定，本批不改")
            elif subtree_deferred:
                row["to_type"] = cur_type              # pc 未定的树：防御性暂缓
                row["to_project_class"] = ""
                row["rule"] = ("⏸ 项目分类未定，子卡暂缓")
            elif cur_type == "线索":
                mapped = LEGACY_MIGRATION_BY_WORK.get(wid, "")
                if mapped:
                    row["to_type"] = mapped
                    row["to_project_class"] = ""
                    row["rule"] = f"线索（语义即「没选」）→ {mapped}（按 LEGACY_MIGRATION_BY_WORK）"
                else:
                    row["to_type"] = cur_type
                    row["rule"] = f"⚠ {wid} 未在 LEGACY_MIGRATION_BY_WORK 中，需人工确认"
            elif cur_type in _l1_all_codes(reg):
                # [T2 扩展修正] 已是合法 L1 词表值（如 work_002 的 32 张
                # sub_project）→ 无需迁移、无需人工确认（旧版误标 ⚠）。
                row["to_type"] = cur_type
                row["to_project_class"] = ""
                row["rule"] = "已是词表值，不动"
            else:
                row["to_type"] = cur_type              # 其他 legacy 值不动，人工确认
                row["to_project_class"] = ""
                row["rule"] = f"⚠ 非「线索」legacy 值「{cur_type}」：需人工确认"
            out.append(row)
    return out


def _apply_migration(rows: list[dict]) -> int:
    """真正执行迁移。返回改动卡数。

    写路径**只走 card_store.update_card**（唯一写门，会过 _write_gate），
    不直接改文件——保持与运行时同一条路、同一套闸、同一份快照留痕。
    [研究树批次] 迁移目标不再限定 rd_project：按 LEGACY_MIGRATION_BY_WORK
    逐树取目标类型（词表未立项的树不在表内 ⇒ 不动，防御面保留）。
    """
    import asyncio
    from src.storage import card_store

    changed = 0

    async def _run() -> int:
        n = 0
        for r in rows:
            wid, cid = r["work_id"], r["card_id"]
            if r["is_root"]:
                # 根卡：只补 L0 project_class，子卡不动
                if r["cur_project_class"] == r["to_project_class"]:
                    continue
                upd = {"project_class": r["to_project_class"]}
            else:
                # 子卡：仅当本树在 LEGACY_MIGRATION_BY_WORK 有映射时才归类
                mapped = LEGACY_MIGRATION_BY_WORK.get(wid, "")
                if not mapped:
                    continue          # 未立项映射的树：不动
                if r["to_type"] == r["cur_type"]:
                    continue
                upd = {"card_type": r["to_type"]}
            res = await card_store.update_card(
                wid, cid, upd, f"[存量类型迁移] {r['cur_type']} → {upd}")
            if isinstance(res, dict) and "error" in res:
                print(f"  ✗ {wid}/{cid} 失败：{res['error']}")
                continue
            n += 1
        return n

    changed = asyncio.run(_run())
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--work", help="只处理指定 work_id")
    ap.add_argument("--tag", default="migrate",
                    help="快照 reason 标签（如 research-migrate；快照 reason=[pre-<tag>]）")
    args = ap.parse_args()

    rows = collect()
    if args.work:
        rows = [r for r in rows if r["work_id"] == args.work]

    if not args.apply:
        # ============ 预览模式（只读） ============
        print(f"=== 存量类型迁移 · 映射预览（共 {len(rows)} 张卡）===")
        by_work: dict[str, list[dict]] = {}
        for r in rows:
            by_work.setdefault(r["work_id"], []).append(r)
        for wid in sorted(by_work):
            rs = by_work[wid]
            print(f"\n--- {wid}（{len(rs)} 张）---")
            for r in rs:
                flag = "⚠" if r["rule"].startswith("⚠") else " "
                print(f" {flag} {r['card_id']}  「{r['title'][:26]}」"
                      f"  {r['cur_type']} → {r['to_type']}"
                      f"{'  +pc=' + r['to_project_class'] if r['to_project_class'] else ''}")
        # 冲突与告警
        warns = [r for r in rows if r["rule"].startswith("⚠")]
        deferred = [r for r in rows if r.get("subtree_deferred")]
        pc_pending = [r for r in rows if r.get("pc_needs_confirm")]
        to_change = [r for r in rows
                     if (r["is_root"] and r["cur_project_class"] != r["to_project_class"])
                     or (not r["is_root"] and r["to_type"] != r["cur_type"])]
        print(f"\n=== 统计 ===")
        print(f"  总卡数           : {len(rows)}")
        print(f"  根卡             : {sum(1 for r in rows if r['is_root'])}")
        print(f"  本批将改动       : {len(to_change)}")
        print(f"⏸ 子卡暂缓       : {len(deferred)}")
        print(f"  ⚠ 类型需人工确认 : {len(warns)}")
        print(f"  ⚠ 根卡 pc 未定   : {len(pc_pending)}")
        if deferred:
            by_w: dict[str, int] = {}
            for r in deferred:
                by_w[r["work_id"]] = by_w.get(r["work_id"], 0) + 1
            print("\n=== 子卡暂缓（项目分类未定）===")
            for w, c in sorted(by_w.items()):
                print(f"  {w}：{c} 张子卡本批不动")
        if warns:
            print("\n=== 需人工确认清单（这些不会被自动改）===")
            for r in warns:
                print(f"  {r['work_id']}/{r['card_id']}  「{r['title']}」 type={r['cur_type']}")
        print(f"\n预览模式：未写任何文件。确认后加 --apply --tag {args.tag} 执行。")
        return 0

    # ============ 执行模式（--apply） ============
    from src.storage import data_snapshot
    print(f"=== 存量类型迁移 · 执行（tag={args.tag}）===")
    print("第 1 步：迁移前快照…", flush=True)
    for wid in sorted({r["work_id"] for r in rows}):
        r = data_snapshot.snapshot_sync(reason=f"[pre-{args.tag}]", work_id=wid)
        # [诚实打印] 指纹去重会 skip（状态已被上一批快照捕获）——如实报，
        # 不把 skipped 说成"已落"。迁移前状态兜底：/tmp 全量备份。
        print(f"  {wid}: ok={r['ok']} skipped={r['skipped']} files={r['files']}")
    print("  （快照走独立目录不走 git；skipped=内容已被上一批捕获）")

    print("第 2 步：逐卡经唯一写门 update_card 迁移…", flush=True)
    n = _apply_migration(rows)
    print(f"  实际改动 {n} 张卡")

    print("第 3 步：迁移后快照…", flush=True)
    for wid in sorted({r["work_id"] for r in rows}):
        r = data_snapshot.snapshot_sync(reason=f"[post-{args.tag}]", work_id=wid)
        print(f"  {wid}: ok={r['ok']} skipped={r['skipped']} files={r['files']}")

    print("第 4 步：复核…")
    from src.storage.card_store import _meta_of as _m
    ok = bad = 0
    for r in rows:
        m = _m(r["work_id"], r["card_id"]) or {}
        got_type = str(m.get("card_type", "") or "")
        got_pc = str(m.get("project_class", "") or "")
        if r["is_root"]:
            good = (got_pc == r["to_project_class"])
        elif r.get("subtree_deferred"):
            good = (got_type == r["cur_type"])   # 暂缓＝原样不动
        else:
            good = (got_type == r["to_type"])
        ok += 1 if good else 0
        bad += 0 if good else 1
        if not good:
            print(f"  ✗ {r['work_id']}/{r['card_id']} 期望 type={r['to_type']}"
                  f" pc={r['to_project_class']} 实际 type={got_type} pc={got_pc}")
    print(f"\n=== 结果：{ok} 张符合预期，{bad} 张不符===")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
