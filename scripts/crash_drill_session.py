# -*- coding: utf-8 -*-
"""会话写入崩溃演练脚本（迭代一·A9，部署窗口第 5 步前置证据）。

场景：追加写进行中进程被 SIGKILL（Windows＝TerminateProcess）→ 文件留下
「完好块 ＋ 尾部未闭合半块」→ 门面体检（health_check）检出 → 损坏处置
（repair_damaged_tail）修复 → 修复后 seq 单调、完好块全保留、残块旁路
damaged/ 留档（永不删）。

全程只在 --target 指定的**副本树**上进行（零生产触碰）。

用法：
    python scripts/crash_drill_session.py --target <works副本目录> --work work_X

输出：演练证据 JSON（stdout ＋ <target>/_crash_drill_report.json）。
非零退出＝演练失败（任一判据不满足）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.storage import session_facade, session_store   # noqa: E402

CHILD = r'''
import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, r"{repo}")
from src.storage import session_facade, session_store
session_store.WORKS_DIR = Path(r"{target}")

async def main(work, card):
    for i in range(30):
        await session_facade.append(work, card, {{"role": "user",
            "content": "完好基线-%03d " % i + "y" * 200}})
    # 复现「write 进行中被击杀」的撕裂形态：写半个块（无 ---END--- 收尾）
    with open(session_store._session_path(work, card), "a", encoding="utf-8") as f:
        f.write("---MSG---\nseq: 30\nactor: user\nturn: 30\n---\n被撕断的半截正文")
        f.flush()
    time.sleep(3600)   # 挂起等待父进程 SIGKILL（撕裂字节已确定性落位）

asyncio.run(main("{work}", "{card}"))
'''


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="works 副本树")
    ap.add_argument("--work", required=True, help="演练用 work_id（副本内）")
    ap.add_argument("--card", default="card_drill")
    ap.add_argument("--grace", type=float, default=1.5, help="写入秒数后击杀")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    session_store.WORKS_DIR = target
    work, card = args.work, args.card

    # 清场：演练卡会话从零开始（副本内）
    spath = session_store._session_path(work, card)
    if spath.exists():
        spath.unlink()

    code = CHILD.format(repo=str(REPO), target=str(target), work=work, card=card)
    proc = subprocess.Popen([sys.executable, "-c", code])
    await asyncio.sleep(args.grace)          # 子进程写完基线＋半块后挂起
    proc.kill()                               # SIGKILL 等价（TerminateProcess）
    proc.wait()
    evidence: dict[str, object] = {"target": str(target), "work": work,
                                   "card": card, "child_killed": True,
                                   "tear_mode": "half_block_written_then_sigkill"}

    # 1) 体检：撕裂必须被检出（尾部未闭合）
    hc = session_facade.health_check_sync(work, card)
    evidence["health_before"] = {k: hc.get(k) for k in
                                 ("exists", "blocks", "unclosed", "seq_monotonic")}
    assert hc["exists"], "击杀后会话文件不存在（fsync 写入未落任何块？）"
    assert hc.get("unclosed", 0) >= 1, \
        f"撕裂未检出：unclosed={hc.get('unclosed')}（击杀时机未落在块中间，可调 --grace）"

    # 2) 损坏处置：残块旁路留档 ＋ 尾部重建
    rp = session_facade.repair_damaged_tail(work, card)
    evidence["repair"] = {k: rp.get(k) for k in ("repaired", "fragments", "blocks_after")}
    assert rp["repaired"], "处置未执行修复"
    assert rp["fragments"], "残块必须旁路 damaged/ 留档（永不删）"

    # 3) 修复后：体检全绿 ＋ 重建块数＝完好块数（半块内容不吞进可用区）
    hc2 = session_facade.health_check_sync(work, card)
    evidence["health_after"] = {k: hc2.get(k) for k in
                                ("blocks", "damaged_count", "seq_monotonic", "unclosed")}
    assert hc2["seq_monotonic"] is True and hc2["unclosed"] == 0
    assert hc2["damaged_count"] == 0
    assert hc2["blocks"] == rp["blocks_after"]

    # 4) 修复后的文件可继续追加（seq 接续）
    blk = await session_facade.append(work, card, {"role": "user", "content": "修复后续写"})
    assert blk["seq"] == hc2["blocks"], f"续写 seq 接续失败: {blk['seq']} vs {hc2['blocks']}"
    evidence["append_after_repair"] = {"seq": blk["seq"], "ok": True}

    evidence["verdict"] = "PASS"
    out = target / "_crash_drill_report.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=1))
    print(f"[drill] PASS —— 证据: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
