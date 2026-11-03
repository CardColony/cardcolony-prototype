# -*- coding: utf-8 -*-
"""记忆全量重凝驱动（2026-10-31 · 用户令）。

背景：现存记忆条目全是坏管线（_FOLD_MODEL_OVERRIDE 崩溃、逐块兜底、旧 prompt）
的产物，质量不保证 ⇒ 用户令：全部清掉、用修好的管线重新凝练一遍。

三个子命令：
    backup          每卡记忆状态（chain/mark/extra/archive/nodes）备份到
                    logs/memory_backup_<date>/ ——清账前必跑
    wipe            清账：memory / memory_nodes / memory_archive 三表 + fold_cache
                    （原文会话块零触碰——那是唯一事实源，重凝的来处）
    drain           补凝：按未凝量从大到小逐卡跑批；**每卡必留一个窗口**
                    （剩余 ≤ 单批窗口预算即停，绝不凝完）——用户令
    status          只读盘点：每卡 mark/未凝 token/批数

用法：
    python scripts/memory_recondense.py backup
    python scripts/memory_recondense.py wipe
    python scripts/memory_recondense.py drain [--cards card_001,card_002] [--fold-every 5]
    python scripts/memory_recondense.py status

可续跑：drain 以 mark 为游标，中断后重跑自动接着付。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

from src.storage import card_db, memory_domain, memory_store as m, session_store  # noqa: E402
from src.storage import work_store  # noqa: E402

LOGDIR = Path(BASE_DIR) / "logs"


def _all_cards() -> list[tuple[str, str]]:
    """[(work_id, card_id)]——全部 works × cards（有会话或已有记忆的）。"""
    out = []
    works = sorted(p for p in session_store.WORKS_DIR.iterdir()
                   if p.is_dir() and not p.name.startswith(("_", ".")))
    for wp in works:
        wid = wp.name
        if work_store.read_work(wid) is None:
            continue
        def _fn(conn):
            try:
                return [r["card_id"] for r in conn.execute("SELECT card_id FROM cards")]
            except Exception:
                return []
        try:
            for cid in (card_db.read_sync(wid, _fn, []) or []):
                out.append((wid, cid))
        except Exception:
            continue
    return out


def _has_memory(wid: str, cid: str) -> bool:
    def _fn(conn):
        row = conn.execute("SELECT card_id FROM memory WHERE card_id=?", (cid,)).fetchone()
        return row is not None
    try:
        return bool(card_db.read_sync(wid, _fn, False))
    except Exception:
        return False


def cmd_backup() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = LOGDIR / f"memory_backup_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for wid, cid in _all_cards():
        if not _has_memory(wid, cid):
            continue
        st = m.load_memory(wid, cid)
        arc = memory_domain.load_archive(wid, cid)
        payload = {"work_id": wid, "card_id": cid, "state": st,
                   "archive_rows": [{"seq": k[0], "role": k[1], **v}
                                    for k, v in (arc or {}).items()]}
        (dest / f"{wid}__{cid}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
        print(f"[backup] {wid}/{cid} mark={st.get('mark')} 条目={len(st.get('entries') or [])}", flush=True)
    print(f"=== 备份完成 {n} 卡 → {dest}", flush=True)
    return 0


def cmd_wipe() -> int:
    # [2026-10-31 · 根因修复] 遗留 JSON（works/{wid}/memory/{cid}.json）不隔离，
    # load_memory 的懒迁移就会把旧管线条目**原样灌回 DB**——清账永远清不干净
    #（实测：21 张卡的 10-21~10-27 旧条目就是这么还魂的）。先隔离再清。
    legacy_root = session_store.WORKS_DIR
    quarantine = LOGDIR / f"memory_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}_legacy_json"
    moved = 0
    for jf in sorted(legacy_root.glob("*/memory/*.json")):
        dest = quarantine / jf.parent.parent.name
        dest.mkdir(parents=True, exist_ok=True)
        jf.rename(dest / jf.name)
        moved += 1
    if moved:
        print(f"[wipe] 遗留 JSON 已隔离 {moved} 个 → {quarantine}", flush=True)
    n = 0
    for wid, cid in _all_cards():
        if not _has_memory(wid, cid):
            continue

        def _fn(conn, _cid=cid):
            conn.execute("DELETE FROM memory WHERE card_id=?", (_cid,))
            conn.execute("DELETE FROM memory_nodes WHERE card_id=?", (_cid,))
            conn.execute("DELETE FROM memory_archive WHERE card_id=?", (_cid,))
            return 1

        card_db._write_sync(wid, _fn)  # noqa: SLF001 清账写事务
        n += 1
        print(f"[wipe] {wid}/{cid} 已清（chain/nodes/archive；原文会话未动）", flush=True)
    cache = LOGDIR / "fold_cache"
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
        print("[wipe] fold_cache 已清", flush=True)
    print(f"=== 清账完成 {n} 卡（重凝以 mark=0 起步）", flush=True)
    return 0


def cmd_status() -> int:
    print(f"{'work':10} {'card':14} {'mark':>6} {'未凝tok':>8} {'批':>4} {'窗口线':>7}")
    for wid, cid in _all_cards():
        st = m.load_memory(wid, cid)
        mark = int(st.get("mark", 0) or 0)
        pending = m._read_origin(wid, cid, mark)
        toks = m._context_meter(pending)
        if not pending and not st.get("entries"):
            continue
        print(f"{wid:10} {cid:14} {mark:>6} {toks:>8} "
              f"{int(st.get('batch_count', 0) or 0):>4} {m.window_budget_tokens():>7}", flush=True)
    return 0


async def _drain_card(wid: str, cid: str, *, fold_every: int, max_batches: int) -> dict:
    """单卡补凝：跑到「剩一个窗口」为止。返回账目。

    [跨进程互斥 2026-10-31] 5700 服务器在跑、其扫描器每 300s 也会触发凝练
    （多进程无共享锁）⇒ 开工前给该卡写 6h 失败账（DB 里的 condense_debt，
    服务器侧 condense_turn_end 会读它并跳过）——本 drain 成为该卡唯一写者；
    收工清账把卡还给服务器。drain 自己写状态不经 debt 检查。"""
    import time as _t
    st0 = m.load_memory(wid, cid)
    st0["condense_debt"] = {"failed_at": "drain-lease",
                            "next_retry_ts": _t.time() + 6 * 3600,
                            "reason": "drain 租约（补凝进行中，服务器勿动）"}
    await m.save_memory(wid, cid, st0)
    print(f"[drain] {wid}/{cid} 租约已挂（服务器 6h 内不碰这张卡）", flush=True)
    t0 = time.time()
    batches = 0
    mark = int(m.load_memory(wid, cid).get("mark", 0) or 0)
    # [2026-10-31 · 用户纠偏] **继承既有设置**，不发明停机线：
    #   触发线 K（CONDENSE_TRIGGER_TOKENS，10 万）＝要不要凝的判据——未到线不凝；
    #   单批窗口（CONDENSE_WINDOW_BUDGET_TOKENS，6 万）＝每次送凝多少；
    #   一次触发只跑一批（源侧 2026-09-09 裁定）⇒ drain＝把「自然触发」按序重放：
    #   到线才凝、每次一批，留下的尾巴自然落在 4 万～10 万——这就是「未凝窗口」。
    K = m.trigger_k_tokens()
    win = m.window_budget_tokens()
    total0 = m._context_meter(m._read_origin(wid, cid, 0))
    while batches < max_batches:
        pending = m._read_origin(wid, cid, mark)
        toks = m._context_meter(pending)
        if toks < K:                         # 未到触发线 ⇒ 停（留下的就是未凝窗口）
            print(f"[drain] {wid}/{cid} 剩余 {toks} tok < 触发线 {K} —— 留窗停", flush=True)
            break
        window = m._truncate_window(pending, win)
        print(f"[drain] {wid}/{cid} 批{batches + 1} 未凝 {toks} tok ≥ 线 {K}，本批 {len(window)} 块", flush=True)
        ok = await m._condense_one_batch(wid, cid, window, mark, batch_no=batches + 1)
        if not ok:
            print(f"[drain] {wid}/{cid} 批{batches + 1} 失败——停（失败账在，重跑续）", flush=True)
            break
        batches += 1
        # [2026-11-01 · 修账] drain 直调 _condense_one_batch 绕过了
        # _run_condense_pipeline 的 batch_count 累加 ⇒ 归拢游标永不欠账、
        # 归拢线从未触发。每批如实 +1（游标语义与生产一致）。
        _st = m.load_memory(wid, cid)
        _st["batch_count"] = int(_st.get("batch_count", 0) or 0) + 1
        await m.save_memory(wid, cid, _st)
        mark = int(m.load_memory(wid, cid).get("mark", 0) or 0)
        if fold_every > 0 and batches % fold_every == 0:
            try:
                await m._maybe_fold(wid, cid)
            except Exception as exc:  # noqa: BLE001 归拢失败不挡凝练
                print(f"[drain] {wid}/{cid} 归拢异常（不挡）: {exc}", flush=True)
    st = m.load_memory(wid, cid)
    st.pop("condense_debt", None)          # 收工：还卡给服务器
    await m.save_memory(wid, cid, st)
    remaining = m._context_meter(m._read_origin(wid, cid, int(st.get("mark", 0) or 0)))
    summary = {"work": wid, "card": cid, "batches": batches,
               "duration_s": round(time.time() - t0), "mark": st.get("mark"),
               "entries": len(st.get("entries") or []),
               "orig_total_tok": total0, "remaining_tok": remaining}
    print(f"[drain] === {wid}/{cid} 完成 {summary}", flush=True)
    return summary


async def cmd_drain(cards: list[tuple[str, str]] | None, fold_every: int,
                    max_batches: int) -> int:
    targets = cards or _all_cards()
    # 未凝量降序（大卡先排）
    sized = []
    for wid, cid in targets:
        pending = m._read_origin(wid, cid, int(m.load_memory(wid, cid).get("mark", 0) or 0))
        toks = m._context_meter(pending)
        if toks >= m.trigger_k_tokens():      # 到触发线才需要补凝（继承触发线设置）
            sized.append((toks, wid, cid))
    sized.sort(reverse=True)
    print(f"[drain] 待补凝 {len(sized)} 卡（>窗口线）："
          + ", ".join(f"{w}/{c}={t}" for t, w, c in sized[:10]) + " …", flush=True)
    results = []
    for toks, wid, cid in sized:
        results.append(await _drain_card(wid, cid, fold_every=fold_every,
                                         max_batches=max_batches))
    print("[drain] === 全部完成", flush=True)
    for r in results:
        print(f"  {r['work']}/{r['card']}: 批={r['batches']} 条目={r['entries']} "
              f"耗时={r['duration_s']}s 余窗={r['remaining_tok']}tok", flush=True)
    return 0


# [单实例锁 2026-10-31 · 事故教训] 原版 dsh 单飞是单进程机制（只有一个宿主跑
# 凝练）；移植版服务器＋离线脚本可能并存 ⇒ 跨进程互斥落在共享存储（锁文件，
# PID＋活性检查）：抢不到就**拒绝跑**，绝不并联。
LOCK_PATH = LOGDIR / "recondense_drain.lock"


def _lock_acquire() -> bool:
    try:
        import psutil
    except ImportError:
        psutil = None
    if LOCK_PATH.exists():
        try:
            info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            alive = bool(psutil and psutil.pid_exists(int(info.get("pid") or 0)))
            if alive:
                print(f"[lock] 已有 drain 在跑（pid={info['pid']}，自 {info['since']}）——拒绝并联",
                      flush=True)
                return False
            print("[lock] 发现死锁残留（持有者已死）——接管", flush=True)
        except Exception:  # noqa: BLE001
            pass
    LOCK_PATH.write_text(json.dumps(
        {"pid": os.getpid(), "since": datetime.now().isoformat(timespec="seconds")}),
        encoding="utf-8")
    return True


def _lock_release() -> None:
    try:
        if LOCK_PATH.exists():
            info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            if int(info.get("pid") or 0) == os.getpid():
                LOCK_PATH.unlink()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backup", "wipe", "drain", "status"])
    ap.add_argument("--cards", help="逗号分隔 card_id（drain 限定范围）")
    ap.add_argument("--fold-every", type=int, default=5, help="每 N 批挂一次归拢（0=不归拢）")
    ap.add_argument("--max-batches", type=int, default=500, help="单卡批数硬顶（防呆）")
    a = ap.parse_args()
    if a.cmd == "backup":
        return cmd_backup()
    if a.cmd == "wipe":
        return cmd_wipe()
    if a.cmd == "status":
        return cmd_status()
    if not _lock_acquire():
        return 2
    try:
        cards = None
        if a.cards:
            want = {c.strip() for c in a.cards.split(",") if c.strip()}
            cards = [(w, c) for w, c in _all_cards() if c in want]
        return asyncio.run(cmd_drain(cards, a.fold_every, a.max_batches))
    finally:
        _lock_release()


if __name__ == "__main__":
    sys.exit(main())
