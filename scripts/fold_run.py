# -*- coding: utf-8 -*-
"""归拢管线（s1 分组裁决 → s2' 每组线程综合）的单次干跑驱动（诊断用）。

## 为什么要这个脚本

`_maybe_fold` 内部调模型时，如果连接静默关闭，`STREAM_IDLE_TIMEOUT=None`
会让它无限等，而日志里只有「批开始」，看不出卡在哪一步。

本脚本把各步拆开单独跑（**干跑：解析报告、不落库、不推游标**），并且：
1. **每步产出落盘**（`logs/fold_s1.txt` / `logs/fold_g{i}.txt`＋`fold_g{i}_in.txt`）——
   重试某一步时不必重跑前面的几十分钟；
2. **每 30 秒打一次无帧心跳**——能区分「在等」和「已死」；
3. 心跳里带上**本进程的 TCP 连接状态**——连接没了就说明请求根本不在路上。

用法：
    python scripts/fold_run.py <card_id> [--skip-s1] [--group N]
"""
from __future__ import annotations

import argparse
import asyncio
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
    ap.add_argument("--group", type=int, default=None, help="只跑第 N 组（0 起）")
    ap.add_argument("--model", help="指定本次归拢模型；缺省使用 condense 绑定")
    a = ap.parse_args()
    model = a.model or model_config.pick_one("condense")
    print(f"归拢模型: {model}", flush=True)

    p1 = os.path.join(LOGDIR, "fold_s1.txt")

    st = m.load_memory(a.work, a.card_id)
    tops = m._foldable_tops(st)
    top_seqs = {int((t.get("anchor") or {}).get("seq") or 0) for _i, t in tops}
    by_seq = {int((t.get("anchor") or {}).get("seq") or 0): t for _i, t in tops}
    print(f"卡 {a.card_id}  可归拢顶层 {len(tops)} 条", flush=True)

    # ── s1 分组裁决（自由推理＋JSON尾）──
    if a.skip_s1 and os.path.exists(p1):
        t1 = open(p1, encoding="utf-8").read()
        print(f">>> s1 复用 {p1}（{len(t1)} 字）", flush=True)
    else:
        r1 = await run("s1", m.FOLD_STEP1_PROMPT,
                       m._build_fold_messages(m._render_fold_step1(st)), p1, model)
        t1 = str(r1.get("content") or "")
    verdict, groups = m._parse_fold_groups(t1, top_seqs)
    print(f"s1 裁决: {verdict}  组数: {len(groups)}", flush=True)
    if verdict != "merge":
        print("=== 无合并可执行 / 产出不可解析（干跑不提交）===", flush=True)
        return 0
    for i, g in enumerate(groups):
        print(f"  组{i}: {g['label'] or '(无名)'}  成员 {g['members']}", flush=True)

    # ── s2' 每组一次线程综合（干跑：解析报告，不落库）──
    cap = m.fold_group_cap()
    for i, g in enumerate(groups):
        if a.group is not None and i != a.group:
            continue
        mems = sorted((by_seq[s] for s in g["members"] if s in by_seq),
                      key=m._group_order_key)[:cap]
        allowed = {int((x.get("anchor") or {}).get("seq") or 0) for x in mems}
        s2in = (f"【线程】{g.get('label') or ''}\n\n"
                + m._render_fold_group(st, mems))
        pin = os.path.join(LOGDIR, f"fold_g{i}_in.txt")
        with open(pin, "w", encoding="utf-8") as f:
            f.write(s2in)
        pout = os.path.join(LOGDIR, f"fold_g{i}.txt")
        r2 = await run(f"group{i}", m.FOLD_STEP2_GROUP_PROMPT, s2in, pout, model)
        kind, payload = m._parse_fold_digest(str(r2.get("content") or ""),
                                             allowed, a.card_id)
        if kind == "merge":
            print(f"  组{i} → merge  covers={[c['seq'] for c in payload['covers']]} "
                  f"标题={payload['title'][:40]}", flush=True)
        else:
            print(f"  组{i} → {kind}  {str(payload)[:200]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
