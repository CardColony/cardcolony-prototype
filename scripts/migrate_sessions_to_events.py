# -*- coding: utf-8 -*-
"""存量会话 → 卡级事件流迁移（消息底座能力对齐 · 件 6，2026-10-13）。

**做什么**：把 ``works/{work}/sessions/session_NNN.md``（V2 块档案）翻译成
``works/{work}/events/card_NNN.jsonl``（卡级事件流），让存量对话也能走
``GET /api/card/{id}/timeline`` 一条恢复入口。

**映射表**（type 全集见任务书 §二）：

| 块形态                       | 事件 type                                  |
|------------------------------|--------------------------------------------|
| actor=human                  | user_message（data={"content": ...}）      |
| actor=assistant              | assistant_text                             |
| actor=card:NNN（卡间信）     | msg_ask / msg_answer / msg_notify（按信头 type）|
| actor=tool + behavior=triage | judgment                                   |
| behavior=plan / revision     | plan_created（修订＝计划再产生）            |
| behavior=step/execute_step/think_step | step_advanced（步完成）            |
| behavior=final               | synthesize（收口终态与步成果归拢）          |

未登记的 behavior **跳过并计入报告**（不猜语义）；其正文仍在会话文件里，
一处字节都没丢。

**不做什么**：只读会话文件，**不改不删任何存量字节**。

用法：
    # 演练（零写入，只报数）
    python scripts/migrate_sessions_to_events.py --dry-run
    # 实迁（迁移前自动备份目标文件到 --backup-dir，缺省 _backups/events_migration_<ts>）
    python scripts/migrate_sessions_to_events.py
    python scripts/migrate_sessions_to_events.py --work work_002
    python scripts/migrate_sessions_to_events.py --force        # 已有事件文件也重迁
    # 回滚演练（按 manifest 还原：有备份→还原；无备份→移入备份目录留档，永不删）
    python scripts/migrate_sessions_to_events.py --rollback _backups/events_migration_<ts>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api import card_msg  # noqa: E402  卡间信头解析（脚本层可依赖 api 层）
from src.storage import card_store, event_log, session_facade, session_store  # noqa: E402

# behavior → 事件 type（登记表；未登记→跳过并报数）
BEHAVIOR_MAP = {
    "triage": "judgment",
    "plan": "plan_created",
    "revision": "plan_created",
    "step": "step_advanced",
    "execute_step": "step_advanced",
    "think_step": "step_advanced",
    "final": "synthesize",
}

# 信头 type 值＝注册表的类型码（现网实测恒为 msg_* 形态，如 msg_ask）；
# 未前缀形态一并收纳（前向兼容），未命中→msg_notify 兜底并在报告里体现。
MSG_TYPE_MAP = {
    "msg_ask": "msg_ask", "ask": "msg_ask",
    "msg_answer": "msg_answer", "answer": "msg_answer",
    "msg_notify": "msg_notify", "notify": "msg_notify",
}


def _card_id_of(session_file: Path) -> str:
    """session_NNN.md → card_NNN（与 session_store._session_path 反函数）。"""
    return session_file.stem.replace("session_", "card_", 1)


def _block_to_record(blk: dict[str, Any], seq: int) -> dict[str, Any] | None:
    """一个会话块 → 一条事件记录（不可映射→None，调用方计入 skipped）。"""
    actor = str(blk.get("actor") or "")
    content = str(blk.get("content") or "")
    run_id = str(blk.get("pair_id") or "")
    try:
        round_no = int(blk.get("turn") or 0)
    except (TypeError, ValueError):
        round_no = 0
    base = {"seq": seq, "run_id": run_id, "round": round_no}

    if actor == "human":
        return {**base, "type": "user_message", "data": {"content": content}}
    if actor == "assistant":
        return {**base, "type": "assistant_text", "data": {"content": content}}
    if actor.startswith("card:"):
        hdr = card_msg.parse_header(content) or {}
        mtype = str(hdr.get("type") or "")
        ev = MSG_TYPE_MAP.get(mtype, "msg_notify")
        return {**base, "type": ev, "data": {"content": content}}
    # 环节留痕块（actor=tool）：按 behavior 登记映射
    behavior = str(blk.get("behavior") or "")
    if behavior in BEHAVIOR_MAP:
        return {**base, "type": BEHAVIOR_MAP[behavior],
                "data": {"content": content, "behavior": behavior}}
    return None


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    """整文件原子写（tmp→os.replace，data_snapshot/memory_store 同款先例）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


async def migrate_work(work_id: str, *, dry_run: bool, force: bool,
                       backup_root: Path | None) -> dict[str, Any]:
    """迁移单个 work，返回统计与 manifest 条目。"""
    sessions_dir = session_store.WORKS_DIR / work_id / "sessions"
    stat: dict[str, Any] = {
        "work_id": work_id, "cards": 0, "records": 0,
        "by_type": {}, "skipped": 0, "skipped_behaviors": {},
        "entries": [],
    }
    if not sessions_dir.exists():
        return stat
    for sf in sorted(sessions_dir.glob("session_*.md")):
        card_id = _card_id_of(sf)
        blocks = await session_facade.read(work_id, card_id)
        if not blocks:
            continue
        records: list[dict[str, Any]] = []
        for blk in blocks:
            rec = _block_to_record(blk, len(records) + 1)
            if rec is None:
                stat["skipped"] += 1
                b = str(blk.get("behavior") or blk.get("actor") or "?")
                stat["skipped_behaviors"][b] = stat["skipped_behaviors"].get(b, 0) + 1
                continue
            records.append(rec)
        if not records:
            continue
        target = event_log.event_path(work_id, card_id)
        existed = target.exists()
        if existed and not force:
            continue  # 幂等：已有事件文件不覆盖（不重复迁移）
        entry = {
            "work_id": work_id, "card_id": card_id,
            "target": str(target), "backup": None, "existed": existed,
        }
        if dry_run:
            stat["cards"] += 1
            stat["records"] += len(records)
            for r in records:
                stat["by_type"][r["type"]] = stat["by_type"].get(r["type"], 0) + 1
            stat["entries"].append(entry)
            continue
        # 迁移前备份：目标已存在→先复制留档（零删除，回滚可还原）
        if existed and backup_root is not None:
            bpath = backup_root / work_id / f"{card_id}.jsonl"
            bpath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, bpath)
            entry["backup"] = str(bpath)
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        _atomic_write_lines(target, lines)
        stat["cards"] += 1
        stat["records"] += len(records)
        for r in records:
            stat["by_type"][r["type"]] = stat["by_type"].get(r["type"], 0) + 1
        stat["entries"].append(entry)
    return stat


def rollback(manifest_path: Path) -> dict[str, Any]:
    """按 manifest 回滚：有备份→还原；无备份（本次新建）→移入备份目录留档。

    **永不删字节**：回滚是「还原到迁移前状态」，新建文件移进备份目录而不是
    删除（card_store.py:4 铁律精神延伸）。
    """
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent / "rolled_back"
    result = {"restored": 0, "removed": 0, "skipped": 0}
    for entry in data.get("entries", []):
        target = Path(entry["target"])
        if not target.exists():
            result["skipped"] += 1
            continue
        if entry.get("backup"):
            shutil.copy2(entry["backup"], target)
            result["restored"] += 1
            continue
        dest = base / entry["work_id"] / f"{entry['card_id']}.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))
        result["removed"] += 1
    return result


async def main_async(args: argparse.Namespace) -> int:
    if args.rollback:
        res = rollback(Path(args.rollback) / "manifest.json"
                       if (Path(args.rollback) / "manifest.json").exists()
                       else Path(args.rollback))
        print(json.dumps({"rollback": res}, ensure_ascii=False, indent=2))
        return 0

    works = [args.work] if args.work else sorted(
        p.name for p in session_store.WORKS_DIR.iterdir()
        if (p / "work.json").exists()
    )
    backup_root: Path | None = None
    if not args.dry_run:
        backup_root = Path(args.backup_dir) if args.backup_dir else (
            ROOT / "_backups" / f"events_migration_{datetime.now().strftime('%Y%m%dT%H%M%S')}")
        backup_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "dry_run": args.dry_run, "force": args.force,
        "backup_root": str(backup_root) if backup_root else None,
        "works": [], "totals": {"cards": 0, "records": 0, "skipped": 0},
        "by_type": {},
    }
    for wid in works:
        stat = await migrate_work(wid, dry_run=args.dry_run, force=args.force,
                                  backup_root=backup_root)
        summary["works"].append(stat)
        summary["totals"]["cards"] += stat["cards"]
        summary["totals"]["records"] += stat["records"]
        summary["totals"]["skipped"] += stat["skipped"]
        for k, v in stat["by_type"].items():
            summary["by_type"][k] = summary["by_type"].get(k, 0) + v
    if backup_root is not None:
        entries = [e for s in summary["works"] for e in s["entries"]]
        (backup_root / "manifest.json").write_text(
            json.dumps({"generated_at": datetime.now().astimezone().isoformat(),
                        "entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        summary["manifest"] = str(backup_root / "manifest.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="存量会话 → 卡级事件流迁移")
    ap.add_argument("--work", default="", help="只迁指定 work_id（缺省全部）")
    ap.add_argument("--dry-run", action="store_true", help="只报数，零写入")
    ap.add_argument("--force", action="store_true", help="已有事件文件也重迁（先备份）")
    ap.add_argument("--backup-dir", default="", help="备份目录（缺省 _backups/events_migration_<ts>）")
    ap.add_argument("--rollback", default="", help="按 manifest（或其所在目录）回滚")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
