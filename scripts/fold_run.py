# -*- coding: utf-8 -*-
"""归拢三步的单次驱动（诊断 + 执行两用）。

## 为什么要这个脚本

`_maybe_fold` 内部调模型时，如果连接静默关闭，`STREAM_IDLE_TIMEOUT=None`
会让它无限等，而日志里只有「批开始」，看不出卡在哪一步。

本脚本把三步拆开单独跑，并且：
1. **每步产出落盘**（`logs/fold_s1.txt` / `fold_s2.txt` / `fold_s3.txt`）——
   重试某一步时不必重跑前面的 17 分钟；
2. **每 30 秒打一次无帧心跳**——能区分「在等」和「已死」；
3. 心跳里带上**本进程的 TCP 连接状态**——连接没了就说明请求根本不在路上。

用法：
    python scripts/fold_run.py <card_id> [--skip-s1] [--skip-s2]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:  # noqa: BLE001
    pass

logging.disable(logging.CRITICAL)

from src.api import model_config, model_service  # noqa: E402
from src.storage import memory_store as m  # noqa: E402

LOGDIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGDIR, exist_ok=True)


def _conns() -> str:
    """本进程当前 TCP 连接摘要（无 psutil 时返回空串）。"""
    try:
        import psutil  # type: ignore

        me = psutil.Process()
        out = []
        for c in me.net_connections(kind="tcp"):
            if c.status != "ESTABLISHED":
                continue
            try:
                out.append(f"{c.raddr.ip}:{c.raddr.port}")
            except Exception:  # noqa: BLE001
                continue
        return ",".join(out) or "无"
    except Exception:  # noqa: BLE001
        return "?"


async def run(name: str, system: str, user: str, out_path: str,
              model: str,
              tools: list[dict] | None = None,
              effort: str | None = None) -> dict:
    gw = model_service.get_gateway()
    print(f">>> {name} 开始 {time.strftime('%H:%M:%S')} 输入 {len(user)} 字"
          f"{f' 思考={effort}' if effort else ''}", flush=True)

    last = [time.time()]
    stop = asyncio.Event()

    async def watchdog() -> None:
        while not stop.is_set():
            await asyncio.sleep(30)
            if stop.is_set():
                return
            gap = time.time() - last[0]
            print(f"  [wd] {name} 无帧 {gap:.0f}s  连接={_conns()}", flush=True)

    wd = asyncio.create_task(watchdog())
    t0 = time.time()
    n = 0
    types: dict[str, int] = {}
    clen = 0
    final: dict = {}
    try:
        kw = {"tools": tools} if tools else {}
        if effort:
            kw["reasoning_effort"] = effort
        async for ev in gw.stream_call(model, system, user, **kw):
            last[0] = time.time()
            ct = ev.get("chunk_type")
            if ct:
                n += 1
                types[ct] = types.get(ct, 0) + 1
                if ct == "content":
                    clen += len(str(ev.get("text") or ""))
                elif ct == "tool-call":
                    # 逐帧落盘工具参数片段，防最终 JSON 截断时丢失全量内容。
                    with open(out_path + ".toolcalls.jsonl", "a", encoding="utf-8") as tf:
                        tf.write(json.dumps({k: ev.get(k) for k in ("chunk_type", "index", "id", "name", "arguments_delta")}, ensure_ascii=False) + "\\n")
                if n % 200 == 0:
                    print(f"  [{name}] {time.time()-t0:5.0f}s 帧{n} {types} 正文≈{clen}", flush=True)
            else:
                final = ev
                with open(out_path + ".final.json", "w", encoding="utf-8") as ff:
                    json.dump(final, ff, ensure_ascii=False, indent=2)
    finally:
        stop.set()
        wd.cancel()

    text = str(final.get("content") or "")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"<<< {name} 完成 {time.strftime('%H:%M:%S')} 耗时{time.time()-t0:.0f}s "
          f"帧{n} {types} finish={final.get('finish_reason')} tokens={final.get('tokens_used')} "
          f"正文={len(text)}字 toolcalls={len(final.get('tool_calls') or [])} → {os.path.basename(out_path)}",
          flush=True)
    return final


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("card_id")
    ap.add_argument("--work", default="work_027")
    ap.add_argument("--skip-s1", action="store_true", help="复用 logs/fold_s1.txt")
    ap.add_argument("--skip-s2", action="store_true", help="复用 logs/fold_s2.txt")
    ap.add_argument("--model", help="指定本次归拢模型；缺省使用 condense 绑定")
    ap.add_argument("--s3-system", help="仅 step3 覆盖 system prompt 文件；转填续调时使用")
    ap.add_argument("--s3-effort", help="step3 思考档位（off=关闭；转填是机械任务）")
    a = ap.parse_args()
    model = a.model or model_config.pick_one("condense")
    print(f"归拢模型: {model}", flush=True)

    p1 = os.path.join(LOGDIR, "fold_s1.txt")
    p2 = os.path.join(LOGDIR, "fold_s2.txt")
    p3 = os.path.join(LOGDIR, "fold_s3.txt")

    st = m.load_memory(a.work, a.card_id)
    tops = m._foldable_tops(st)
    top_seqs = {int((t.get("anchor") or {}).get("seq") or 0) for _i, t in tops}
    print(f"卡 {a.card_id}  可归拢顶层 {len(tops)} 条", flush=True)

    # ── step1 只判断 ──
    if a.skip_s1 and os.path.exists(p1):
        t1 = open(p1, encoding="utf-8").read()
        print(f">>> step1 复用 {p1}（{len(t1)} 字）", flush=True)
    else:
        r1 = await run("step1", m.FOLD_STEP1_PROMPT, m._render_fold_step1(st), p1, model)
        t1 = str(r1.get("content") or "")
    lines = [x.strip() for x in t1.splitlines() if x.strip()]
    last_line = lines[-1] if lines else ""
    print("step1 末行:", last_line or "(空)", flush=True)
    if "结论：无合并" in last_line:
        print("=== judge-empty（模型判定无可合并）===", flush=True)
        return 0

    members = {int(h) for h in re.findall(r"\[seq\s+(\d+)\]", t1)} & top_seqs
    print(f"step1 点名成员 {len(members)} 个", flush=True)

    # ── step2 只合并 ──
    if a.skip_s2 and os.path.exists(p2):
        t2 = open(p2, encoding="utf-8").read()
        print(f">>> step2 复用 {p2}（{len(t2)} 字）", flush=True)
    else:
        s2in = f"【第一步产出】\n{t1}\n\n{m._render_fold_step2(st, members)}"
        with open(os.path.join(LOGDIR, "fold_s2_in.txt"), "w", encoding="utf-8") as f:
            f.write(s2in)
        r2 = await run("step2", m.FOLD_STEP2_PROMPT, s2in, p2, model)
        t2 = str(r2.get("content") or "")

    # ── step3 只转填 ──
    s3_system = (open(a.s3_system, encoding="utf-8").read()
                 if a.s3_system else m.FOLD_STEP3_PROMPT)
    if a.s3_system:
        print(f"step3 system 覆盖自 {a.s3_system}", flush=True)
    r3 = await run("step3", s3_system, t2, p3, model, tools=[m.APPLY_FOLDS_TOOL],
                   effort=a.s3_effort)
    calls = r3.get("tool_calls") or []
    print("step3 tool_calls:", json.dumps(calls, ensure_ascii=False)[:800], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
