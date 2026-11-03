# -*- coding: utf-8 -*-
"""真账影子卡归拢实测：克隆一张卡的记忆+原文到影子卡，在影子上实跑 `_maybe_fold`。

不碰活卡：影子卡 id 后缀 `_foldtest`，memory/session_blocks 全量复制；
跑完报告 tops 前后、digest 产物、不变量体检。影子卡与缓存产物留待人工检查。

用法：
    python scripts/fold_shadow_run.py card_001 [--work work_027] [--clone-only]
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from src.storage import card_db, memory_domain, memory_store as m  # noqa: E402


def _clone(work: str, src: str, dst: str) -> dict:
    """克隆 memory 行 + session_blocks + memory_nodes 到影子卡（幂等重跑先清）。"""
    st = m.load_memory(work, src)
    if not st.get("entries"):
        raise SystemExit(f"{src} 无记忆，克隆中止")
    st2 = copy.deepcopy(st)
    st2["card_id"] = dst

    def _fn(conn):
        conn.execute("DELETE FROM session_blocks WHERE card_id=?", (dst,))
        conn.execute("DELETE FROM memory WHERE card_id=?", (dst,))
        conn.execute("DELETE FROM memory_nodes WHERE card_id=?", (dst,))
        n = conn.execute(
            "INSERT INTO session_blocks(card_id, seq, type, time, content, data) "
            "SELECT ?, seq, type, time, content, data FROM session_blocks "
            "WHERE card_id=?", (dst, src)).rowcount
        return n

    blocks = card_db._write_sync(work, _fn)  # noqa: SLF001 影子卡克隆写
    return {"state": st2, "blocks": blocks}


def _stats(work: str, cid: str) -> dict:
    st = m.load_memory(work, cid)
    tops = m._foldable_tops(st)
    synth = sum(1 for _i, t in tops
                if int((t.get("anchor") or {}).get("seq") or 0) >= m.SYNTH_SEQ_BASE)
    return {"entries": len(st.get("entries") or []), "tops": len(tops),
            "digest_tops": synth, "last_fold_check": st.get("last_fold_check"),
            "batch_count": st.get("batch_count"), "state": st}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src_card")
    ap.add_argument("--work", default="work_027")
    ap.add_argument("--clone-only", action="store_true", help="只克隆不跑")
    ap.add_argument("--shadow", default=None, help="影子卡 id（缺省 <src>_foldtest）")
    a = ap.parse_args()
    shadow = a.shadow or (a.src_card + "_foldtest")

    info = _clone(a.work, a.src_card, shadow)
    print(f"[clone] {a.src_card} → {shadow}  blocks={info['blocks']}  "
          f"entries={len(info['state']['entries'])}", flush=True)
    await m.save_memory(a.work, shadow, info["state"])   # 落 memory+memory_nodes
    before = _stats(a.work, shadow)
    print(f"[before] tops={before['tops']} (digest={before['digest_tops']}) "
          f"entries={before['entries']} fold_check={before['last_fold_check']}",
          flush=True)
    if a.clone_only:
        return 0

    t0 = time.time()
    await m._maybe_fold(a.work, shadow)
    dt = time.time() - t0
    after = _stats(a.work, shadow)
    st = after["state"]
    print(f"\n[after {dt:.0f}s] tops={after['tops']} (digest={after['digest_tops']}) "
          f"entries={after['entries']} fold_check={after['last_fold_check']}",
          flush=True)

    digests = [e for e in st.get("entries") or []
               if int((e.get("anchor") or {}).get("seq") or 0) >= m.SYNTH_SEQ_BASE
               and int(e.get("level") or 1) == 1 and not e.get("upper")]
    print(f"[digests] 顶层 digest 共 {len(digests)} 条：", flush=True)
    for d in digests:
        cov = d.get("coveredSeqs") or []
        print(f"  seq={d['anchor']['seq']} 成员={len(d.get('lower') or [])} "
              f"覆盖={len(cov)} 起{min(cov) if cov else '?'}~{max(cov) if cov else '?'} "
              f"kind={d.get('kind')} lesson={bool(d.get('lesson'))} "
              f"docs={len(d.get('docPaths') or [])} gist={(d.get('gist') or '')[:60]}",
              flush=True)
    viol = memory_domain.check_invariants(st.get("entries") or [])
    print(f"[invariants] 违例 {len(viol)} 条{('  ' + str(viol[:3])) if viol else ''}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
