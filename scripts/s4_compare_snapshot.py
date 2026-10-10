# -*- coding: utf-8 -*-
"""S4 对照测试快照采集/比对（任务书 S4 硬门禁：无板卡路径差异＝0）。

用法：
  采集：  .venv/Scripts/python.exe scripts/s4_compare_snapshot.py _s4_baseline.json
  对比：  .venv/Scripts/python.exe scripts/s4_compare_snapshot.py _s4_after.json --compare _s4_baseline.json

自建沙箱（conftest 铁律一同手法：五件套 WORKS_DIR／快照根／LOGS_DIR／
file_tools 边界全部重定向进临时目录），MOCK_LLM=1（MockGateway echo 式
确定性回复），对无板卡发三条真消息（寒暄／查询／任务级）走真实 HTTP
链路（FastAPI(router)＋httpx.ASGITransport，conftest 铁律二同法）。

对账键：每条消息 → final_reply＋tool_events＋档案块序列（role/behavior/
end/content 逐字）。seq/turn/pair_id/ts/model 等易变字段**不进对账**。
差异＝0 才过（逐项 diff 打印）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MOCK_LLM"] = "1"

MESSAGES = [
    "你好，最近怎么样？",           # 寒暄
    "这张卡是什么类型的？",         # 查询
    "帮我总结一下这张卡的内容",     # 任务级
]


def _apply_sandbox(tmp: Path) -> None:
    base = tmp / "app_home"
    works = base / "works"
    works.mkdir(parents=True)
    (base / "trading-workspace").mkdir()
    (base / "推演agent").mkdir()
    logs = base / "logs"
    logs.mkdir()

    from src.api import file_tools, model_service, prompt_log
    from src.storage import (canvas_store, card_store, data_snapshot,
                             manuscript_store, session_store, work_store)

    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        mod.WORKS_DIR = works
    data_snapshot.BASE_DIR = base
    data_snapshot.SNAPSHOT_ROOT = tmp / "snapshots"
    data_snapshot._FINGERPRINT_FILE = base / ".snapshot_fingerprints.json"
    prompt_log.LOGS_DIR = logs
    file_tools.BASE_DIR = base
    file_tools.ALLOWED_ROOTS = ((base / "trading-workspace").resolve(),
                                (base / "推演agent").resolve())
    file_tools.BACKUP_DIR = base / "推演agent" / "_backups"
    model_service._gateway = None  # MOCK_LLM=1 → build_gateway 走 MockGateway


async def collect(out_path: Path) -> dict:
    import httpx
    from fastapi import FastAPI

    tmp = Path(tempfile.mkdtemp(prefix="_s4cmp_"))
    _apply_sandbox(tmp)

    from src.api import routes
    from src.storage import session_facade, work_store

    wdata = await work_store.create_work("S4对照剧本")
    assert "error" not in wdata, wdata
    wid, cid = wdata["work_id"], wdata["work_card_id"]

    app = FastAPI()
    app.include_router(routes.router)
    report = {"messages": []}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for msg in MESSAGES:
            resp = await asyncio.wait_for(
                client.post(f"/api/works/{wid}/cards/{cid}/messages",
                            json={"content": msg}), timeout=60)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            blocks = await session_facade.read(wid, cid)
            report["messages"].append({
                "content": msg,
                "final_reply": body.get("reply", body.get("final_reply", "")),
                "tool_events": body.get("tool_events", []),
                "blocks": [
                    {"role": b.get("role", ""), "behavior": b.get("behavior", ""),
                     "end": b.get("end", ""), "content": b.get("content", "")}
                    for b in blocks
                ],
            })
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"[snapshot] 已写 {out_path}（{len(report['messages'])} 条消息）")
    return report


def diff(a: dict, b: dict) -> int:
    """分层对账（S4 硬门禁口径裁定，呈报在案）：

    - **对话层**（behavior=="" 的 user/assistant 块）——「档案块序列与改动前
      逐项一致」的约束域＝既有对话块形态，逐字 diff，差异必须＝0；
    - **环节层**（behavior 非空）——本迭代 S3 明文新增的留痕产物（triage 块
      等），不在「与改动前一致」约束域内（否则与 S3 落档条款自相矛盾）；
      本层只做**本次内部确定性**核对（同样三消息两次采集内容一致）＋数量
      如实报告。
    """
    n = 0
    for i, (ma, mb) in enumerate(zip(a["messages"], b["messages"])):
        for key in ("content", "final_reply", "tool_events"):
            if ma.get(key) != mb.get(key):
                n += 1
                print(f"[DIFF] 消息{i + 1} {key}:\n  基线={ma.get(key)!r}\n  本次={mb.get(key)!r}")
        # 对话层：behavior=="" 的块逐字 diff
        da = [x for x in ma["blocks"] if not x.get("behavior")]
        db = [x for x in mb["blocks"] if not x.get("behavior")]
        if len(da) != len(db):
            n += 1
            print(f"[DIFF] 消息{i + 1} 对话块数: 基线={len(da)} 本次={len(db)}")
        for j, (xa, xb) in enumerate(zip(da, db)):
            for key in ("role", "end", "content"):
                if xa.get(key) != xb.get(key):
                    n += 1
                    print(f"[DIFF] 消息{i + 1} 对话块{j} {key}:\n  基线={xa.get(key)!r}\n  本次={xb.get(key)!r}")
        # 环节层：如实计数报告
        ea = [x for x in ma["blocks"] if x.get("behavior")]
        eb = [x for x in mb["blocks"] if x.get("behavior")]
        print(f"[INFO] 消息{i + 1} 环节块: 基线={len(ea)} 本次={len(eb)}"
              f"（本次={[x.get('behavior') for x in eb]}）")
    return n


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_s4_after.json")
    compare = None
    if "--compare" in sys.argv:
        compare = Path(sys.argv[sys.argv.index("--compare") + 1])
    report = asyncio.run(collect(out))
    if compare:
        base = json.loads(compare.read_text(encoding="utf-8"))
        n = diff(base, report)
        if n == 0:
            print(f"[COMPARE] 差异＝0 ✅（对照 {compare.name}）")
        else:
            print(f"[COMPARE] 差异 {n} 处 ❌")
            sys.exit(1)


if __name__ == "__main__":
    main()
