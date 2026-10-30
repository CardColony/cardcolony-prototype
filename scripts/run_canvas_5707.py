# -*- coding: utf-8 -*-
"""5707 画布开发实例启动器（独立数据根，零触碰 5700 生产）。

为什么单独写一支而不复用 `run_test_instance_5701.py`：
  1. 那支的沙箱根是 `tempfile.mkdtemp()`——**每次重启数据全丢**。画布改造需要
     反复刷新同一批卡片看效果，必须持久。
  2. 那支的默认端口/沙箱前缀都写着 5701 口径，容易被误认为可以碰生产。

本支的三条硬约束（与 5700 的关系）：
  - **不 import main.py**：main 在模块导入期跑 `startup_init()`（`init_git` 会
    `git add -A` + commit、`write_pid` 会覆盖 `.server.pid`）。本支照抄 app 装配，
    只跳过 startup_init。
  - **数据根整体搬进本工作树**：`<worktree>/_data5707/`，生产的 `works/` 与
    `logs/` 一个字节都不写。
  - **端口默认 5707**，可传参。

用法（在 worktree 根目录）：
  .venv/Scripts/python.exe scripts/run_canvas_5707.py [port]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# [编码 2026-10-25] 与 main.py 同款：Windows stdout 是 GBK+strict ⇒ 含「⇒」的
# print() 会炸（会把真实异常吃掉）。本脚本**故意不 import main** ⇒ 这里单独调。
from src.infra.console_io import ensure_utf8_console  # noqa: E402

ensure_utf8_console()

#: 数据根：默认本工作树下，可用 ECO_5707_DATA 覆盖。
DATA_ROOT = Path(os.environ.get("ECO_5707_DATA") or (BASE_DIR / "_data5707")).resolve()

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")   # 与 main.py 同款：先加载 .env 再导入路由


def redirect_roots() -> Path:
    """把全部运行时根搬到 `_data5707/`，使本实例**零触碰**生产 works/logs。"""
    works = DATA_ROOT / "works"
    logs = DATA_ROOT / "logs"
    sandbox = DATA_ROOT / "sandbox"
    for d in (works, logs, sandbox):
        d.mkdir(parents=True, exist_ok=True)

    from src.api import file_tools, prompt_log
    from src.storage import (
        canvas_store, card_store, data_snapshot, memory_store,
        manuscript_store, session_store, work_store,
    )

    for mod in (card_store, work_store, canvas_store, session_store,
                manuscript_store, memory_store):
        if hasattr(mod, "WORKS_DIR"):
            mod.WORKS_DIR = works

    data_snapshot.BASE_DIR = DATA_ROOT
    data_snapshot.SNAPSHOT_ROOT = DATA_ROOT / "snapshots"
    data_snapshot._FINGERPRINT_FILE = DATA_ROOT / ".snapshot_fingerprints.json"

    prompt_log.LOGS_DIR = logs

    # 文件工具边界收进沙箱：画布开发不需要模型碰真实盘。
    file_tools.BASE_DIR = sandbox
    file_tools.ALLOWED_ROOTS = (sandbox.resolve(),)
    file_tools.BACKUP_DIR = sandbox / "_backups"

    os.environ["_ECO_5707_DATA_ROOT"] = str(DATA_ROOT)
    return DATA_ROOT


_DATA = redirect_roots()

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from src.api.routes import router as api_router  # noqa: E402
from src.api.settings_api import router as settings_router  # noqa: E402
from src.api.asset_cache import install_asset_cache  # noqa: E402

app = FastAPI(title="推演agent 5707 画布开发实例")

app.include_router(api_router)
app.include_router(settings_router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
# 与 main.py 一致：静态资源回源校验（否则改样式看的是旧文件）。
install_asset_cache(app)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """与 main.py 同步：根地址渲染 home.html。"""
    html = (BASE_DIR / "templates" / "home.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


def _render_work(work_id: str) -> str:
    """与 main.py 逐行一致（[阶段3 A3.2] 画布页只注入 WORK_ID）。"""
    html = (BASE_DIR / "templates" / "work.html").read_text(encoding="utf-8")
    return html.replace("{{WORK_ID}}", work_id).replace("{{CARD_ID}}", "")


def _render_card(work_id: str, card_id: str) -> str:
    """与 main.py 逐行一致（对话页渲染 card.html）。"""
    html = (BASE_DIR / "templates" / "card.html").read_text(encoding="utf-8")
    return html.replace("{{WORK_ID}}", work_id).replace("{{CARD_ID}}", card_id)


@app.get("/work/{work_id}", response_class=HTMLResponse)
async def work_page(work_id: str) -> HTMLResponse:
    if work_id == "draft":
        return HTMLResponse(content=_render_card("draft", "draft"))
    return HTMLResponse(content=_render_work(work_id))


@app.get("/work/{work_id}/card/{card_id}", response_class=HTMLResponse)
async def work_card_page(work_id: str, card_id: str) -> HTMLResponse:
    return HTMLResponse(content=_render_card(work_id, card_id))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page() -> HTMLResponse:
    html = (BASE_DIR / "templates" / "settings.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/__instance__")
async def instance_marker() -> dict:
    """进程身份探针：确认请求落在 5707 开发实例，而不是 5700 生产。"""
    return {
        "instance": "canvas-5707",
        "pid": os.getpid(),
        "base_dir": str(BASE_DIR),
        "data_root": str(_DATA),
        "worktree": True,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5707
    print(f"[5707] base={BASE_DIR}  data={_DATA}  port={port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port)
