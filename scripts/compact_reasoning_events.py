# -*- coding: utf-8 -*-
"""思考事件**合并压缩**工具（[5715] · 性能）。

## 为什么要有这个工具

2026-10-20「5700 对话页卡死」的根因之一：思考流**逐帧落盘** ⇒ 一张卡的
事件日志里 87% 是 reasoning（实测 card_005：17,574 条里 16,795 条是 reasoning，
3.68MB）⇒ 回放时逐条 rev++ 重渲染 ⇒ 主线程卡死。

**写侧**已修（`src/api/event_stream.py`：按 (card, run, round, model) 累积、
到轮界合并落一条 ⇒ 新的思考不再刷屏）。但**存量**那些逐帧写下的日志还在，
回放照样慢 ⇒ 需要一次性把它们合并。

本工具做的事：把**连续且同 (run_id, round, model)** 的 reasoning 事件
**合并成一条**（文本按原顺序拼接），其余事件**原样保留、不动顺序**。

## 安全纪律（生产数据，改前必读）

1. **正在写的卡不动**：文件在 N 秒内被写过的 ⇒ 跳过（默认 120s）。
2. **先备份**：原文件复制成 ``<file>.bak-<时间戳>``，再原子替换（tmp → os.replace）。
3. **只合并 reasoning**：其它事件类型的顺序、条数、内容**一字不动**。
4. **默认只报告不写**：要真写必须显式加 ``--apply``。
5. **可复算**：合并是纯函数（见 ``merge_records``），同一输入必得同一输出。

## 用法

    # 看看会怎样（不写）
    python scripts/compact_reasoning_events.py --work work_018

    # 真写（先备份，再原子替换）
    python scripts/compact_reasoning_events.py --work work_018 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    """**按运行目录**定位仓库根（不是脚本所在目录）。

    理由：本工具要动的是**某个部署的数据根**（生产在主树、开发在 worktree）。
    若按脚本位置解析，跑在 worktree 里就会去动 worktree 的空数据根 ⇒
    「什么都没找到」还容易被误读成「没有要压的」。
    ⇒ 优先用**当前工作目录**（它就是那个部署的根），否则退回脚本所在仓库。
    """
    cwd = Path.cwd()
    if (cwd / "src" / "storage" / "session_store.py").exists():
        return cwd
    return ROOT


# ---------------------------------------------------------------- 纯逻辑

def merge_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把**连续且同 (run_id, round, model)** 的 reasoning 事件合并成一条。

    · 只有 reasoning 参与合并；其它事件**原样保留、顺序不动**。
    · 合并键＝(run_id, round, model)＋**必须相邻**（不相邻就不合——
      免得把中间隔着的别的语义揉进一段思考里）。
    · 合并后的事件保留**第一条**的 seq/run_id/round/model/ts（落盘记录的
      身份字段以首条为准），文本按原顺序拼接。
    """
    out: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("type") != "reasoning":
            out.append(rec)
            continue
        data = rec.get("data") or {}
        text = str(data.get("text") or "")
        if out:
            last = out[-1]
            ldata = last.get("data") or {}
            same = (
                last.get("type") == "reasoning"
                and str(last.get("run_id") or "") == str(rec.get("run_id") or "")
                and int(last.get("round") or 0) == int(rec.get("round") or 0)
                and str(last.get("model") or "") == str(rec.get("model") or "")
            )
            if same:
                merged = dict(last)
                mdata = dict(ldata)
                mdata["text"] = str(ldata.get("text") or "") + text
                merged["data"] = mdata
                out[-1] = merged
                continue
        out.append(dict(rec))
    return out


def read_records(path: Path) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                recs.append({"type": "<坏行>", "data": {"text": line}})
    return recs


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- CLI

def _event_root(work_id: str) -> Path:
    from src.storage import session_store

    return session_store.WORKS_DIR / work_id / "events"


def compact_file(path: Path, apply: bool, skip_seconds: float) -> tuple[int, int]:
    """压缩一个事件文件；返回 (原条数, 新条数)。"""
    age = time.time() - path.stat().st_mtime
    if age < skip_seconds:
        print(f"  跳过（{age:.0f}s 前还在写）：{path}")
        return (0, 0)
    records = read_records(path)
    merged = merge_records(records)
    before, after = len(records), len(merged)
    if before == after:
        print(f"  无需压缩：{path}（{before} 条）")
        return (before, after)
    if apply:
        bak = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, bak)
        write_records(path, merged)
        print(f"  已压缩：{path}  {before} → {after} 条（备份 {bak.name}）")
    else:
        print(f"  将压缩：{path}  {before} → {after} 条（加 --apply 才写）")
    return (before, after)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="合并事件日志里逐帧写下的 reasoning 事件")
    ap.add_argument("--work", default="", help="work_id（空＝扫全部）")
    ap.add_argument("--card", default="", help="card_id（空＝该 work 全部卡）")
    ap.add_argument("--apply", action="store_true", help="真写（默认只报告）")
    ap.add_argument("--skip-seconds", type=float, default=120.0,
                    help="最近 N 秒内被写过的文件跳过（默认 120s）")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(_repo_root()))
    root = _event_root(args.work) if args.work else None
    if root is None:
        from src.storage import session_store

        base = session_store.WORKS_DIR
        files = sorted(base.glob("*/events/*.jsonl"))
    else:
        files = sorted(root.glob("*.jsonl"))
    if args.card:
        files = [f for f in files if f.stem == args.card]
    if not files:
        print("没有匹配的事件文件")
        return 0

    print(f"扫描 {len(files)} 个事件文件（apply={args.apply}）")
    tb = ta = 0
    for f in files:
        b, a = compact_file(f, args.apply, args.skip_seconds)
        tb += b
        ta += a
    print(f"合计：{tb} → {ta} 条（省下 {tb - ta} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
