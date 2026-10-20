# -*- coding: utf-8 -*-
"""造一张大规模画布工程（压测夹具）——**只写沙箱**，零触碰生产 works/。

用途：画布性能/交互优化需要比 work_002（39 卡）大得多的树。

**沙箱纪律（本脚本的硬约束）**：
`card_store.WORKS_DIR` 默认指向「本文件所在仓库」的 works/。在 5711 工作树里跑
脚本时，那等于 `<worktree>/works`——既不是生产、也不是 5711 服务读的那份
（服务读 `_sandbox/works`）。本脚本因此**在 import storage 之后立刻**把所有
模块的 WORKS_DIR 显式改写到 `ECO_SANDBOX_ROOT/works`，与
`scripts/run_test_instance_5701.py::redirect_roots()` 同一手法。

用法：
    ECO_SANDBOX_ROOT=<sandbox> python scripts/seed_scale_work.py --cards 300
    → 在沙箱里建一棵 N 卡树，打印 WORK_ID
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from src.storage import card_store, work_store  # noqa: E402
from src.storage import canvas_store, manuscript_store, session_store  # noqa: E402


def redirect_to_sandbox() -> Path:
    """把全部运行时根改写到沙箱。**必须在任何建卡动作之前**。"""
    root = Path(os.environ.get("ECO_SANDBOX_ROOT", "").strip() or (BASE_DIR / "_sandbox"))
    root = root.resolve()
    works = root / "works"
    works.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        mod.WORKS_DIR = works
    return works


def _cid(out: dict) -> str:
    """create_card 返回的是 read_card 的结果（{"meta":..., "body":...}），卡号在 meta 里。"""
    meta = out.get("meta") or {}
    return str(meta.get("card_id") or out.get("card_id") or "")


async def seed(total: int, project_class: str = "rd_project") -> str:
    res = await work_store.create_work(f"压测工程 {total} 卡", project_class=project_class)
    if res.get("error"):
        raise SystemExit(f"建 work 失败: {res['error']}")
    work_id = res["work_id"]
    root_id = res.get("work_card_id") or ""
    print(f"[seed] work={work_id} root={root_id}")

    made = 1
    frontier = [root_id]
    depth = 0
    while made < total and frontier:
        depth += 1
        nxt = []
        for parent in frontier:
            for i in range(4):
                if made >= total:
                    break
                out = await card_store.create_card(
                    work_id, f"D{depth}-{parent[-3:]}-{i}", source="用户直接创建",
                    parent=parent, skip_kick=True,
                )
                if out.get("error"):
                    print(f"[seed] 建卡失败: {out['error']}")
                    continue
                made += 1
                nxt.append(_cid(out))
        if depth >= 3:
            for parent in frontier:
                while made < total:
                    out = await card_store.create_card(
                        work_id, f"leaf-{made}", source="用户直接创建",
                        parent=parent, skip_kick=True,
                    )
                    if out.get("error"):
                        break
                    made += 1
            break
        frontier = nxt
    print(f"[seed] 完成：{work_id} 共 {made} 张卡")
    return work_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=300)
    a = ap.parse_args()
    works = redirect_to_sandbox()
    print(f"[seed] 沙箱 works = {works}")
    wid = asyncio.run(seed(a.cards))
    print(f"WORK_ID={wid}")
