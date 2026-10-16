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
# [模型设置第二步 2026-10-12] 设置接口（供应商/模型/角色绑定 + 密钥 + 备份还原）
from src.api.settings_api import router as settings_router  # noqa: E402

app.include_router(api_router)
app.include_router(settings_router)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """[首页两态 2026-10-15] 根地址＝首页（旧项目列表页已整体取消，见 home.html）。

    后端只负责渲染空态首页骨架，**跳转判定全在前端**（static/esm/home.js）：
    「上次停留的卡」记在 localStorage 的 nav.recent.v1 里，服务端读不到，
    放在后端判会把用户永远钉在首页、违反方案定稿 v2 的第 3 条目标。
    前端三态：有最近记录→跳上次对话页／无记录但有项目→跳最近活跃 work 对话页／
    全空→停在空态首页。判定所需的 /api/works 端点本就已存在，前端直接调用。
    """
    html = (BASE_DIR / "templates" / "home.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


def _render_work(work_id: str, card_id: str = "") -> str:
    """work 页模板渲染：注入 WORK_ID 与 CARD_ID。

    [阶段2 A2.1 2026-10-17] 对话独立地址 /work/{wid}/card/{cid} 与画布页 /work/{wid}
    共用同一份 work.html（阶段 3 才拆模板），故两处占位符一并注入；画布页 CARD_ID 为空串。
    """
    html = (BASE_DIR / "templates" / "work.html").read_text(encoding="utf-8")
    return html.replace("{{WORK_ID}}", work_id).replace("{{CARD_ID}}", card_id)


@app.get("/work/{work_id}", response_class=HTMLResponse)
async def work_page(work_id: str) -> HTMLResponse:
    """Phase 8：剧本画布页。work_id 注入到 HTML 占位符。"""
    return HTMLResponse(content=_render_work(work_id))


@app.get("/work/{work_id}/card/{card_id}", response_class=HTMLResponse)
async def work_card_page(work_id: str, card_id: str) -> HTMLResponse:
    """[阶段2 A2.1] 对话独立页：URL 即页面身份（刷新各回各家）。阶段 2 仍渲染 work.html。"""
    return HTMLResponse(content=_render_work(work_id, card_id))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page() -> HTMLResponse:
    """[模型设置第二步 2026-10-12] 模型设置页：供应商/模型/角色绑定，保存即热生效。"""
    html = (BASE_DIR / "templates" / "settings.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.exception_handler(Exception)
async def _capture_unhandled(request, exc):
    """[2026-10-12] 未捕获异常留痕：此前 500 只进服务控制台，磁盘零记录，
    排障只能靠猜（12:36 create_card 后 500 实测：无 prompt 日志、无审计之外
    任何痕迹）。现机械记录 ts/方法/路径/异常类型/信息/完整 traceback 到
    logs/server_errors.jsonl；响应体带真因（前端直接显示，不再只显示 HTTP 500）。
    不改变任何既有行为——只在原本会裸 500 的路径上生效。"""
    import traceback as _tb
    from fastapi.responses import JSONResponse as _JSONResponse
    try:
        rec = {
            "ts": datetime.now().astimezone().isoformat(),
            "method": request.method,
            "path": str(request.url.path),
            "exc_type": type(exc).__name__,
            "exc_msg": str(exc),
            "traceback": _tb.format_exc(),
        }
        p = BASE_DIR / "logs" / "server_errors.jsonl"
        p.parent.mkdir(exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 留痕失败不阻断错误响应本身
        pass
    return _JSONResponse(status_code=500,
                         content={"error": f"{type(exc).__name__}: {exc}"})


class _BaseExceptionCapture:
    """[2026-10-12] BaseException 捕获中间件。

    为什么单靠 exception_handler 不够：Starlette 的 ServerErrorMiddleware 只抓
    ``Exception``；``asyncio.CancelledError``（Python 3.8+ 起继承 BaseException）
    等会整个穿透，uvicorn 底层 ``except BaseException`` 只回一句纯文本
    "Internal Server Error"——零 traceback、零 prompt 日志，排障只能猜
    （12:36/13:22 两次 create_card 后 500 实测正是此路径：连 exception_handler
    都没触发）。本中间件挂在用户中间件位（ServerErrorMiddleware 之内、
    ExceptionMiddleware 之外），BaseException 一律：留痕 → 响应带真因 → 继续
    上抛（不吞 cancellation，结构化并发语义不变）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def _send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except BaseException as exc:  # noqa: BLE001 含 CancelledError 等
            import traceback as _tb
            from fastapi.responses import JSONResponse as _JSONResponse
            try:
                rec = {
                    "ts": datetime.now().astimezone().isoformat(),
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "exc_type": type(exc).__name__,
                    "exc_msg": str(exc),
                    "traceback": _tb.format_exc(),
                    "via": "base_exception_middleware",
                }
                p = BASE_DIR / "logs" / "server_errors.jsonl"
                p.parent.mkdir(exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001 留痕失败不阻断错误响应本身
                pass
            if not response_started:
                resp = _JSONResponse(
                    status_code=500,
                    content={"error": f"{type(exc).__name__}: {exc}"})
                await resp(scope, receive, send)
            raise  # 继续上抛：uvicorn 照旧记它的账，cancellation 语义不被吞


app.add_middleware(_BaseExceptionCapture)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=5700)
