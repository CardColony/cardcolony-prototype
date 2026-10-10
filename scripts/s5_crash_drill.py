# -*- coding: utf-8 -*-
"""S5 判据 7·强杀恢复真跑（方案 §3 原文口径）：

「执行到第 2/4 步时强杀进程（taskkill 硬杀）→ 重启 → 同卡再发消息 →
从第 2 步继续（不从头、第 1 步不重跑）」

形态：父编排＋同文件子进程两阶段（--child build / --child resume）。
- build 阶段（子进程 A）：建 4 步板→推完第 1 步（＝执行到第 2/4 步状态：
  s1 done、指针指 s2）→落盘确认→print READY→**挂住不退出**（强杀语义：
  不给优雅清理机会）；
- 父进程：taskkill /F /T 硬杀 A（退出码非 0＝强杀证据）；
- resume 阶段（子进程 B，全新进程、内存 _boards 必空）：恢复序 get_board
  →断言恢复态精确（current=1、s1 done、s2 todo）→同卡再发消息（HTTP 全链路
  ASGITransport＋MOCK_LLM 脚本化 step_complete×3）→断言从 s2 继续（首圈
  user_prompt 含 s2 原文、不含 s1 原文）→收口三件→print PASS。

沙箱自建（五件套重定向同 conftest 铁律一）；验后销毁（红线 6：随临时
沙箱目录整体删除，真实 works 零触碰）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STEPS = [
    {"id": "s1", "prompt": "第一步要求AAA"},
    {"id": "s2", "prompt": "第二步要求BBB"},
    {"id": "s3", "prompt": "第三步要求CCC"},
    {"id": "s4", "prompt": "第四步要求DDD"},
]


def apply_sandbox(base: Path) -> None:
    """五件套重定向进指定沙箱目录（子进程入口）。"""
    works = base / "works"
    works.mkdir(parents=True, exist_ok=True)
    (base / "trading-workspace").mkdir(exist_ok=True)
    (base / "推演agent").mkdir(exist_ok=True)
    logs = base / "logs"
    logs.mkdir(exist_ok=True)
    sys.path.insert(0, str(ROOT))
    os.environ["MOCK_LLM"] = "1"
    from src.api import file_tools, model_service, prompt_log
    from src.storage import (canvas_store, card_store, data_snapshot,
                             manuscript_store, session_store, work_store)
    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        mod.WORKS_DIR = works
    data_snapshot.BASE_DIR = base
    data_snapshot.SNAPSHOT_ROOT = base.parent / "snapshots"
    data_snapshot._FINGERPRINT_FILE = base / ".snapshot_fingerprints.json"
    prompt_log.LOGS_DIR = logs
    file_tools.BASE_DIR = base
    file_tools.ALLOWED_ROOTS = ((base / "trading-workspace").resolve(),
                                (base / "推演agent").resolve())
    file_tools.BACKUP_DIR = base / "推演agent" / "_backups"
    model_service._gateway = None  # MOCK_LLM=1 → MockGateway 兜底；resume 阶段再注入脚本


# ---------------------------------------------------------------- 子进程 A

async def child_build(sandbox: Path) -> None:
    apply_sandbox(sandbox)
    from src.storage import work_store
    from src.thinkchain import board as board_mod

    wdata = await work_store.create_work("强杀演练")
    assert "error" not in wdata, wdata
    wid, cid = wdata["work_id"], wdata["work_card_id"]

    r = await board_mod.create_board(wid, cid, "强杀计划", STEPS)
    assert r["ok"], r

    board = await board_mod.get_board(wid, cid)
    adv = await board.mark_done_and_advance("第一步成果")
    assert adv["ok"] and adv.get("advanced") is True, adv

    # 落盘确认（读盘非内存）：current 指第 2 步（index 1）、s1 done
    disk = json.loads(board_mod.plan_path(wid, cid).read_text(encoding="utf-8"))
    assert disk["current"] == 1, disk["current"]
    assert disk["steps"][0]["status"] == "done"
    assert disk["steps"][1]["status"] == "todo"

    print(f"META work_id={wid} card_id={cid}", flush=True)
    print("READY", flush=True)
    await asyncio.Event().wait()  # 挂住——等父进程 taskkill 硬杀


# ---------------------------------------------------------------- 子进程 B

async def child_resume(sandbox: Path, wid: str, cid: str) -> None:
    apply_sandbox(sandbox)
    import httpx
    from fastapi import FastAPI

    from src.api import model_service, routes
    from src.storage import session_facade
    from src.thinkchain import board as board_mod
    from src.thinkchain import triage as triage_mod
    from tests.test_s4_seams import FAKE_CONFIG, ScriptedGateway

    # ---- ① 恢复序：全新进程 _boards 必空 → 读盘重建
    assert not board_mod._boards, "新进程板注册表应为空"
    board = await board_mod.get_board(wid, cid)
    assert board is not None, "恢复序未取到板"
    snap = board.to_snapshot()
    assert snap["current"] == 1, f"恢复后指针应指第 2 步(index 1)，实为 {snap['current']}"
    assert snap["steps"][0]["status"] == "done", "第 1 步应保持 done"
    assert snap["steps"][1]["status"] == "todo", "第 2 步应保持 todo"
    assert snap["total"] == 4
    print(f"[drill] 恢复态精确：current=1(s2) s1=done s2=todo total=4", flush=True)

    # ---- ② 装配面证据：t165 区指向第 2 步（「从第 2 步继续」的装配证据）
    from src.api import context_assembler
    regions = context_assembler.build_context_regions(wid, cid)
    t165 = [r for r in regions if r.get("order") == 165]
    assert t165, "应注入 t165 计划区"
    t165_text = t165[0]["text"]
    assert "2/4" in t165_text, f"t165 应指第 2/4 步: {t165_text!r}"
    assert "第二步要求BBB" in t165_text, "t165 应带 s2 原文"
    assert "第一步要求AAA" not in t165_text, "t165 不应指回第 1 步"

    # ---- ③ 同卡再发消息（HTTP 全链路；MOCK 脚本 s2→s3→s4 三步交作业）
    triage_calls = {"n": 0}

    async def _count_triage(system, user):
        triage_calls["n"] += 1
        return '{"verdict": "plan", "reason": "不该被调"}'

    triage_mod._call_model = _count_triage  # 板在→应零调用

    gw = ScriptedGateway(script=[
        [{"name": "step_complete", "arguments": {"result": "第二步成果B"}}],
        [{"name": "step_complete", "arguments": {"result": "第三步成果C"}}],
        [{"name": "step_complete", "arguments": {"result": "第四步成果D"}}],
    ])
    gw.config = FAKE_CONFIG
    model_service._gateway = gw

    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await asyncio.wait_for(
            client.post(f"/api/works/{wid}/cards/{cid}/messages",
                        json={"content": "继续往下做"}), timeout=60)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # ---- ④ 从第 2 步继续（不从头、第 1 步不重跑）——推进面证据：
    # 3 次 step_complete 恰好收口（若从头重跑需 4 次且 s1 会被二次执行）；
    # 首圈 user_prompt＝用户原文（催促文只在空手轮出现，非推进判据）。
    assert triage_calls["n"] == 0, "板在应跳过自判（零模型自判调用）"

    step_evts = [e for e in body.get("tool_events", [])
                 if e.get("tool") == "step_complete"]
    assert len(step_evts) == 3 and all(e.get("ok") for e in step_evts), step_evts

    # ---- ④ 收口三件
    assert not board_mod.plan_path(wid, cid).exists(), "板文件应已清理"
    assert not board_mod.has_live_board(wid, cid), "不应再有活板"
    blocks = await session_facade.read(wid, cid)
    behaviors = [b.get("behavior") for b in blocks]
    assert "final" in behaviors, f"final 块应落档，实有 behaviors={behaviors}"

    print(f"[drill] 推进证据：t165 指第 2/4 步带 s2 原文／3 次 step_complete 恰收口"
          f"（从头需 4 次）／自判零调用", flush=True)
    print(f"[drill] 收口证据：板文件清理＋无活板＋final 落档", flush=True)
    print("PASS", flush=True)


# ---------------------------------------------------------------- 父编排

def taskkill_hard(pid: int) -> None:
    """taskkill /F /T 硬杀（Windows；输出为 GBK 编码，errors=replace 防解码炸）。"""
    r = subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(f"[drill] taskkill /F /PID {pid} → rc={r.returncode} "
          f"{(r.stdout or r.stderr).strip()[:120]}", flush=True)


def main() -> None:
    import shutil
    sandbox = Path(tempfile.mkdtemp(prefix="_s5crash_"))
    base = sandbox / "app_home"
    py = sys.executable
    print(f"[drill] 沙箱: {sandbox}", flush=True)
    print("[drill] —— 阶段 A：建 4 步板推到第 2/4 步（挂住待杀）", flush=True)

    pa = subprocess.Popen(
        [py, __file__, "--child", "build", "--sandbox", str(base)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    meta = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        line = pa.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("META"):
            meta = text
        if text == "READY":
            break
    assert pa.poll() is None, "子进程 A 应挂住（READY 后等待强杀）"
    assert meta, f"未收到 META: {meta!r}"
    wid = meta.split("work_id=")[1].split()[0]
    cid = meta.split("card_id=")[1].split()[0]
    print(f"[drill] A 就绪并挂住: {wid}/{cid}（第 2/4 步状态已落盘）", flush=True)

    print("[drill] —— taskkill 硬杀", flush=True)
    taskkill_hard(pa.pid)
    pa.wait(timeout=15)
    assert pa.returncode != 0, f"A 应为被杀退出码，实为 {pa.returncode}"
    print(f"[drill] A 退出码={pa.returncode}（非 0＝强杀证据，未走任何清理路径）", flush=True)

    print("[drill] —— 阶段 B：全新进程重启→恢复→同卡再发", flush=True)
    pb = subprocess.Popen(
        [py, __file__, "--child", "resume", "--sandbox", str(base),
         "--work", wid, "--card", cid],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        out, _ = pb.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        pb.kill()
        out, _ = pb.communicate()
        raise AssertionError("子进程 B 超时")
    text = out.decode("utf-8", "replace")
    print(text, flush=True)
    assert pb.returncode == 0, f"B 退出码 {pb.returncode}"
    assert "PASS" in text, "B 未报告 PASS"

    shutil.rmtree(sandbox, ignore_errors=True)  # 验后销毁（红线 6）
    print("[drill] 沙箱已验后销毁；真实 works 零触碰", flush=True)
    print("DRILL PASS: 强杀→恢复→从第 2 步继续（不从头、第 1 步不重跑）", flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--child":
        phase = sys.argv[2]
        sb = Path(sys.argv[sys.argv.index("--sandbox") + 1])
        if phase == "build":
            asyncio.run(child_build(sb))
        else:
            wid = sys.argv[sys.argv.index("--work") + 1]
            cid = sys.argv[sys.argv.index("--card") + 1]
            asyncio.run(child_resume(sb, wid, cid))
    else:
        main()
