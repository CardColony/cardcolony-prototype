# -*- coding: utf-8 -*-
"""巨块补凝（二版分块凝 · 2026-10-31）。

把此前被墓碑/巨提示糊弄过去的超大原文块，按**分块凝**真正重凝：
删旧条目（锚点=原块号）→ 读块 → _condense_one_batch 内部分块＋按预算逐片三段 →
coveredSeqs 记原块号、mark 护栏不回退。

用法：python scripts/redigest_giant.py work_027 card_076 3 5 10
"""
from __future__ import annotations

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
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

from src.storage import memory_store as m, memory_domain as md  # noqa: E402


async def redigest(wid: str, cid: str, seqs: list[int]) -> int:
    blocks = {int(b["seq"]): b for b in m._read_origin(wid, cid, 0)}
    for seq in seqs:
        b = blocks.get(int(seq))
        if b is None:
            print(f"[redigest] {wid}/{cid} seq={seq} 无此块——跳过", flush=True)
            continue
        # 删旧条目（锚点=seq：可能是墓碑，也可能是巨提示糊弄产物）
        st = m.load_memory(wid, cid)
        before = len(st.get("entries") or [])
        st["entries"] = [e for e in (st.get("entries") or [])
                         if int((e.get("anchor") or {}).get("seq") or 0) != int(seq)]
        st["mark"] = min(int(st.get("mark", 0) or 0), int(seq))   # 让 _condense_one_batch 自己推进
        await m.save_memory(wid, cid, st)
        print(f"[redigest] {wid}/{cid} seq={seq} 旧条目已删（{before}→{len(st['entries'])}），"
              f"开始分块重凝（{m._msg_meter_tokens({'content': b.get('content')})} tok）", flush=True)
        t0 = time.time()
        window = [b]
        ok = await m._condense_one_batch(wid, cid, window, int(seq))
        st2 = m.load_memory(wid, cid)
        chain = md.load_chain(wid, cid)
        viol = md.check_invariants(chain)
        new_for_seq = [e for e in (st2.get("entries") or [])
                       if int((e.get("anchor") or {}).get("seq") or 0) >= 2_000_000_000
                       and int(seq) in (e.get("coveredSeqs") or [])]
        print(f"[redigest] === {wid}/{cid} seq={seq} {'OK' if ok else '失败'} "
              f"耗时{time.time()-t0:.0f}s 段条目={len(new_for_seq)} 违例={len(viol)}", flush=True)
        if not ok:
            return 1
    return 0


def main() -> int:
    wid, cid, seqs = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3:]]
    return asyncio.run(redigest(wid, cid, seqs))


if __name__ == "__main__":
    sys.exit(main())
