# -*- coding: utf-8 -*-
"""推演agent画布 v1 - FastAPI 入口（Phase 1 骨架）。

启动时初始化数据目录、方法论文件、canvas.json、git 仓库与 .server.pid。
"""
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent

# 1. 加载 .env
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("writing-agent")


def init_directories() -> None:
    """a. 创建数据目录（不存在则创建）。

    Phase 7：剧本数据在 works/{work_id}/ 下，但保留 methodology/logs 兜底。
    根目录的 cards/sessions 由旧版创建，迁移后由脚本搬入 works/。
    """
    for name in ("logs", "works"):
        (BASE_DIR / name).mkdir(exist_ok=True)


def init_methodology() -> None:
    """[无内容预设改造 2026-10-02] 方法论库已停用（自我繁殖污染链），
    不再创建任何文件。函数保留以维持 startup_init 结构兼容。
    """
    return


def init_canvas() -> None:
    """c. 初始化 canvas.json。

    Phase 7：canvas.json 移至 works/{work_id}/canvas.json，由 canvas_store
    在 read/rebuild 时自动创建，启动时不再在根目录创建空 canvas.json。
    保留空函数以维持 startup_init 结构兼容。
    """
    return


def init_git() -> None:
    """d. git 检测与初始化提交。git 不可用时仅告警，服务照常启动。"""
    try:
        subprocess.run(
            ["git", "--version"],
            cwd=BASE_DIR, capture_output=True, check=True, timeout=15,
        )
    except Exception:
        logger.warning("git不可用，自动提交暂停")
        return

    try:
        if (BASE_DIR / ".git").exists():
            return  # 仓库已存在：启动时不做任何提交
        subprocess.run(["git", "init"], cwd=BASE_DIR, capture_output=True, check=True, timeout=30)
        subprocess.run(["git", "add", "-A"], cwd=BASE_DIR, capture_output=True, timeout=60)
        subprocess.run(
            ["git", "commit", "-m", "[auto] project: 初始化推演agent"],
            cwd=BASE_DIR, capture_output=True, timeout=60,
        )
    except Exception as exc:
        logger.warning("git初始化提交失败: %s", exc)


def write_pid() -> None:
    """e. 写入 .server.pid。"""
    (BASE_DIR / ".server.pid").write_text(str(os.getpid()), encoding="utf-8")


def startup_init() -> None:
    init_directories()
    init_methodology()
    init_canvas()
    init_git()
    write_pid()
    # [迭代二 S1/R2-③] 孤儿板三查（任务书 S1 步骤 6「进程启动扫 plans/ 全目录」
    # ——终态删/卡无删/卡活留，零时间戳；fail-open：扫描异常不阻断启动）。
    try:
        from src.thinkchain.board import sweep_all_orphans
        _sweep = sweep_all_orphans()
        if _sweep.get("removed_total"):
            print(f"[startup] 孤儿板清理 {_sweep['removed_total']} 块", flush=True)
    except Exception as _exc:  # noqa: BLE001
        print(f"[startup] 孤儿板扫描失败（不阻断启动）: {_exc}", flush=True)


app = FastAPI(title="推演agent画布 v1")

startup_init()

from src.api.routes import router as api_router  # noqa: E402  (需在 .env 加载后导入)

app.include_router(api_router)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/work/{work_id}", response_class=HTMLResponse)
async def work_page(work_id: str) -> HTMLResponse:
    """Phase 8：剧本画布页。work_id 注入到 HTML 占位符。"""
    html = (BASE_DIR / "templates" / "work.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html.replace("{{WORK_ID}}", work_id))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=5700)
