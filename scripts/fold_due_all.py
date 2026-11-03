# -*- coding: utf-8 -*-
"""对全部到欠账线的卡顺序跑归拢（生产路径 _maybe_fold，单实例锁同款纪律）。"""
import asyncio
import logging
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:
    pass
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

from src.storage import memory_store as m  # noqa: E402

DUE = [("work_027", "card_076"), ("work_027", "card_001"),
       ("work_027", "fn_007"), ("work_027", "card_021")]


async def main() -> int:
    for wid, cid in DUE:
        st = m.load_memory(wid, cid)
        bc, lf = int(st.get("batch_count", 0) or 0), int(st.get("last_fold_check", 0) or 0)
        tops = len(m._foldable_tops(st))
        if bc - lf < m.FOLD_EVERY_BATCHES or tops < 2:
            print(f"[fold] {wid}/{cid} 不欠账或顶层不足（欠{bc-lf} 顶层{tops}）——跳过", flush=True)
            continue
        print(f">>> 归拢 {wid}/{cid} 顶层={tops} 开始 {time.strftime('%H:%M:%S')}", flush=True)
        t0 = time.time()
        await m._maybe_fold(wid, cid)
        st2 = m.load_memory(wid, cid)
        print(f"<<< 归拢 {wid}/{cid} 收口 耗时{time.time()-t0:.0f}s "
              f"条目={len(st2.get('entries') or [])} 游标={st2.get('last_fold_check')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
