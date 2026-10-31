# -*- coding: utf-8 -*-
"""归拢续跑驱动（生产路径 · 2026-10-31）。

与 scripts/fold_run.py（三步分步诊断）不同，本脚本走**生产代码路径**
`memory_store._maybe_fold`——入口含 archive 一次性迁移（F17）、三步调用
（F8 容错：静默判死/截断必抛/重试梯/熔断）、降层建边提交（F1/F2）。

用法：
    python scripts/fold_resume.py <card_id> [--work work_027] [--loop N]

`--loop N`：连跑 N 次归拢 job（每次跑完游标推进，逐次重新打开游标——
存量欠账一次跑不完时的排水模式；缺省 1 次，行为与自然触发完全一致）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

from src.storage import memory_store as m          # noqa: E402
from src.storage import memory_domain as md        # noqa: E402


def _heartbeat(tag: str) -> threading.Timer:
    """30s 心跳：证明进程活着（区分「在跑」与「挂死」）。"""
    def _beat():
        print(f"[hb] {time.strftime('%H:%M:%S')} {tag} 仍在跑", flush=True)
        t = _heartbeat(tag)
        t.daemon = True
        t.start()
    t = threading.Timer(30.0, _beat)
    t.daemon = True
    return t


def _summary(work_id: str, card_id: str) -> None:
    st = m.load_memory(work_id, card_id)
    entries = [e for e in (st.get("entries") or []) if isinstance(e, dict)]
    tops = m._foldable_tops(st)
    chain = md.load_chain(work_id, card_id)
    viol = md.check_invariants(chain)
    synth = sum(1 for e in entries
                if int((e.get("anchor") or {}).get("seq") or 0) >= m.SYNTH_SEQ_BASE)
    print(f"\n===== 账目 card={card_id} =====", flush=True)
    print(f"entries={len(entries)} 可归拢顶层={len(tops)} 合成锚点节点={synth} "
          f"批数={st.get('batch_count')} fold游标={st.get('last_fold_check')} "
          f"mark={st.get('mark')}", flush=True)
    print(f"链不变量违例={len(viol)}" + (f": {viol[:3]}" if viol else ""), flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("card_id")
    ap.add_argument("--work", default="work_027")
    ap.add_argument("--loop", type=int, default=1,
                    help="连跑几次归拢 job（0=跑到无可归拢或判定无果为止）")
    args = ap.parse_args()
    wid, cid = args.work, args.card_id

    t = _heartbeat(f"fold-resume {wid}/{cid}")
    t.start()
    try:
        n_run = 0
        max_loop = args.loop if args.loop > 0 else 999
        while n_run < max_loop:
            st0 = m.load_memory(wid, cid)
            tops0 = len(m._foldable_tops(st0))
            if tops0 < 2:
                print(f"[loop {n_run + 1}] 可归拢顶层 {tops0} < 2 —— 无账可排", flush=True)
                break
            print(f"\n>>> 归拢 job {n_run + 1}/{max_loop} 开始 "
                  f"{time.strftime('%H:%M:%S')} 可归拢={tops0}", flush=True)
            t0 = time.time()
            await m._maybe_fold(wid, cid)
            st1 = m.load_memory(wid, cid)
            tops1 = len(m._foldable_tops(st1))
            committed = tops1 < tops0
            print(f"<<< 归拢 job {n_run + 1} 收口 {time.strftime('%H:%M:%S')} "
                  f"耗时{time.time() - t0:.0f}s 可归拢 {tops0}→{tops1} "
                  f"{'（有提交）' if committed else '（无变化：无果/游标未开/失败，见上方日志）'}",
                  flush=True)
            n_run += 1
            if not committed:
                break      # 无提交（judge-empty/abort）⇒ 不空转
        _summary(wid, cid)
        return 0
    finally:
        t.cancel()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
