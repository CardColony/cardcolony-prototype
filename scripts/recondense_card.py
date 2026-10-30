# -*- coding: utf-8 -*-
"""单卡全量重凝（2026-10-29）。

## 为什么要有它

三步凝练（S2a→L1→L2）接入之前，生产走的是「一次调用 + 模型自填 anchor」，留下两类脏数据：

1. **锚点重复**：全库 362 条（`card_001` 53、`fn_007` 137、`card_076` 43…）；
2. **没有 L2**：物化撞 I1 被整批拒写。

去重会丢信息（同一锚点下两条结论内容不同），所以正解是**用新机制重凝一次**——
原始会话块一直在 `works/<w>/sessions/`，随时可重跑。

## 做什么

```
1. 备份 cards.db（sqlite 在线备份）
2. 清空该卡 entries、mark 归 0（除非 --keep-entries）
3. 循环 _read_origin → _run_condense_pipeline，直到未凝段为空或水位不推进
```

失败即停、mark 保留进度，可重复执行续跑。

## 代价

每批 = 3 次模型调用（S2a / L1 / L2）。用 `--dry-run` 先看要跑多少批。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 必须加载 .env：否则窗口预算/触发线取的是代码缺省值（30 万），
# 而项目实际配的是 6 万——估算会差 5 倍。
try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:  # noqa: BLE001 没有 dotenv 就用既有环境
    pass

from src.storage import memory_store  # noqa: E402

# 让每一批的进度可见：不加这行，logger 的 info 全被吞掉，只能看到最后一行，
# 「跑到哪了、是不是卡住了」全靠猜。
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _backup(wid: str) -> str:
    """在线备份该 work 的 cards.db（sqlite backup API，WAL 安全）。"""
    src_path = os.path.join(BASE_DIR, "works", wid, "cards.db")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(BASE_DIR, "_backups", f"recondense_{wid}_{stamp}")
    os.makedirs(outdir, exist_ok=True)
    dst_path = os.path.join(outdir, "cards.db")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    dst.close()
    src.close()
    return outdir


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--card", required=True)
    ap.add_argument("--max-batches", type=int, default=50,
                    help="每次管线调用最多连跑几批（默认 50，跑完继续下一轮）")
    ap.add_argument("--dry-run", action="store_true", help="只估算，不动数据")
    ap.add_argument("--wipe", action="store_true",
                    help="清空旧条目（默认**保留**：只重置水位，重凝时逐批替换）")
    ap.add_argument("--resume", action="store_true",
                    help="续跑：**不重置水位**，从当前 mark 继续（中断后接着跑用）")
    a = ap.parse_args()

    wid, cid = a.work, a.card
    st = memory_store.load_memory(wid, cid)
    mark = int(st.get("mark") or 0)
    ents = st.get("entries") or []
    # 重凝要的是**全部历史**：按 mark=0 取全量块来估算，不是按当前 mark 取剩余。
    all_blocks = memory_store._read_origin(wid, cid, 0)   # noqa: SLF001
    rest_blocks = memory_store._read_origin(wid, cid, mark)  # noqa: SLF001
    total = memory_store._context_meter(all_blocks)        # noqa: SLF001
    rest = memory_store._context_meter(rest_blocks)        # noqa: SLF001
    budget = memory_store.window_budget_tokens()
    batches = max(1, -(-total // budget)) if total else 0

    print(f"{wid}/{cid}")
    print(f"  mark={mark}  旧条目={len(ents)}")
    print(f"  全量历史={len(all_blocks)} 块 / ≈{total} token  ⇒ 约 {batches} 批"
          f" × 3 次调用 = {batches * 3} 次模型调用")
    print(f"  （当前 mark 之后还有 {len(rest_blocks)} 块 / ≈{rest} token 未凝）")
    print(f"  窗口预算={budget} token（来自 .env；缺省值 300000）")
    if not all_blocks:
        print("  该卡没有会话块——无可重凝内容")
        return 0
    if a.dry_run:
        print("  （dry-run，未动数据）")
        return 0

    outdir = _backup(wid)
    print(f"  已备份 → {outdir}")

    # ★ 默认**不清空**旧条目，只把水位归 0：
    #   重凝时 `_drop_overlapping` 会按窗口逐批替换旧条目，所以中途失败/被打断时
    #   数据仍在（新旧混合），绝不会出现「清空了又没凝成」的空记忆。
    #   要彻底清空须显式 `--wipe`；中断后接着跑用 `--resume`（不动水位）。
    if a.resume:
        print(f"  续跑：从 mark={mark} 继续（旧条目 {len(ents)} 条保留）")
    else:
        keep = [] if a.wipe else ents
        await memory_store.save_memory(wid, cid, {
            "card_id": cid, "mark": 0, "entries": keep,
            "batch_count": 0, "last_fold_check": 0, "folded": []})
        mark = 0
        print(f"  水位归 0；{'已清空旧条目' if a.wipe else f'保留旧条目 {len(keep)} 条（逐批替换）'}")

    os.environ["CONDENSE_MAX_BATCHES"] = str(a.max_batches)
    # ★ 按机制停手：**未凝量不足触发线就不再凝**（触发线 = CONDENSE_TRIGGER_TOKENS，
    #   缺省 10 万；一批窗口 = CONDENSE_WINDOW_BUDGET_TOKENS，缺省 6 万）。
    #   凝一批后未凝自然落到 4~10 万之间，这就是「未凝对话留空间」——
    #   这个数由机制定，不是人拍的。原实现循环到「未凝为空」才停，把该留的也吃光了。
    _trigger = memory_store.trigger_k_tokens()
    t0 = time.time()
    rounds = 0
    while True:
        pending = memory_store._read_origin(wid, cid, mark)  # noqa: SLF001
        if not pending:
            break
        _rest = memory_store._context_meter(pending)         # noqa: SLF001
        if _rest < _trigger:
            print(f"  剩余未凝 {_rest} token < 触发线 {_trigger}——按机制停手")
            break
        await memory_store._run_condense_pipeline(wid, cid, pending, mark)
        cur = memory_store.load_memory(wid, cid)
        new_mark = int(cur.get("mark") or 0)
        if new_mark <= mark:
            print(f"  水位未推进（mark 停在 {mark}）——停止，可稍后续跑")
            break
        mark = new_mark
        rounds += 1
        n_l1 = sum(1 for e in (cur.get("entries") or [])
                   if int(e.get("level", 1) or 1) == 1)
        n_l2 = sum(1 for e in (cur.get("entries") or [])
                   if int(e.get("level", 1) or 1) > 1)
        print(f"  轮 {rounds}: mark={mark}  结论={n_l1}  细节={n_l2}  "
              f"耗时 {time.time() - t0:.0f}s")

    # 收尾：剩余未凝段对应的旧条目摘掉——那一段现在按「原文」对待，
    # 不该再同时挂一条旧结论（否则同一段既有原文又有旧摘要，等于重复）。
    rest = memory_store._read_origin(wid, cid, mark)  # noqa: SLF001
    if rest:
        lo = int(rest[0].get("seq") or 0)
        hi = int(rest[-1].get("seq") or 0)
        cur = memory_store.load_memory(wid, cid)
        ents = cur.get("entries") or []
        kept = memory_store._drop_overlapping(ents, lo, hi)  # noqa: SLF001
        if len(kept) != len(ents):
            await memory_store.save_memory(wid, cid, {
                "card_id": cid, "mark": int(cur.get("mark") or 0), "entries": kept,
                "batch_count": cur.get("batch_count", 0),
                "last_fold_check": cur.get("last_fold_check", 0),
                "folded": cur.get("folded", [])})
            print(f"  摘掉覆盖未凝段 [{lo},{hi}] 的旧条目 {len(ents) - len(kept)} 条")

    cur = memory_store.load_memory(wid, cid)
    ents = cur.get("entries") or []
    n_l1 = sum(1 for e in ents if int(e.get("level", 1) or 1) == 1)
    n_l2 = sum(1 for e in ents if int(e.get("level", 1) or 1) > 1)
    print(f"\n完成：mark={cur.get('mark')}  结论={n_l1}  细节={n_l2}  "
          f"总耗时 {(time.time() - t0) / 60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
