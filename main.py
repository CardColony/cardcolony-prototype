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

# 〔P27 · 2026-10-26〕**服务端日志落盘**（logs/server.log）。
# 原先只在启动它的控制台里 ⇒ 用户看不到 ⇒「我不知道有没有日志」⇒ 排障只能靠猜。
# 实现见 src/infra/server_log.py（单独成模块 ⇒ 可单测，不必 import main）。
try:
    from src.infra.server_log import install_file_logging
    install_file_logging(BASE_DIR)
except Exception as _exc:  # noqa: BLE001 日志装不上也不阻断启动
    print(f"[startup] 服务端日志落盘失败（不阻断）: {_exc}", flush=True)

# [编码 2026-10-25] Windows 上 stdout 默认是 **GBK 且 strict** ⇒ 任何含
# 「⇒」「→」这类字符的 print() 都会抛 UnicodeEncodeError（stdout 是 strict，
# stderr 是 backslashreplace ⇒ 只有 stdout 真的炸）。⇒ 统一切到 UTF-8。
from src.infra.console_io import ensure_utf8_console

ensure_utf8_console()


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


# [服务自报 2026-10-25] 记下**启动那一刻**关键源文件的 mtime ＋ git HEAD。
# 为什么要有：改了后端要重启才生效，而「到底重启了没」全靠人猜。
# ⇒ `/api/health` 把这份戳报出来，和盘上文件 mtime 一比就知道要不要重启
# （盘上 mtime 比服务报的**新** ⇒ 服务跑的是旧代码）。
def _build_stamp() -> dict:
    import subprocess
    files = {}
    for rel in ("main.py", "src/api/routes.py", "src/api/access_log.py"):
        p = BASE_DIR / rel
        try:
            files[rel] = os.path.getmtime(p)
        except OSError:
            files[rel] = 0.0
    head = ""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=BASE_DIR,
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001 git 不可用只影响显示
        head = ""
    return {
        "started": datetime.now().astimezone().isoformat(),
        "files": files,
        "git_head": head,
    }


app.state.build_stamp = _build_stamp()
try:
    from src.api.access_log import install_access_log
    install_access_log(app)
except Exception as _exc:  # noqa: BLE001 日志装不上也不阻断启动
    print(f"[startup] 访问日志中间件安装失败（不阻断）: {_exc}", flush=True)


@app.on_event("startup")
async def _start_auto_scheduler() -> None:
    """[调度改造 2026-10-19 · 5704 承载力] 启动建卡点火调度器。

    容量取 ``config.models.yaml`` 的 ``concurrency.default``（缺省 3）：
    同时最多 N 个回合在跑，其余排队。建卡行为不变，只是不瞬间爆发。
    """
    try:
        from src.api import model_config, scheduler
        limit = 3
        try:
            cfg = model_config.load().get("concurrency") or {}
            limit = int(cfg.get("default") or 3)
        except Exception:  # noqa: BLE001 配置读不到就用 3
            limit = 3
        scheduler.start(limit)
    except Exception as exc:  # noqa: BLE001 调度器起不来不影响主服务
        print(f"[startup] 调度器启动失败（不阻断）: {exc}", flush=True)
    # [纯后台凝练 2026-10-22] 与前台回合无关：后台每 N 秒按触发线扫一遍、到线就凝。
    try:
        from src.storage import memory_store as _mem
        _mem.start_condense_sweeper()
    except Exception as exc:  # noqa: BLE001 扫描器失败不阻断启动
        print(f"[startup] 后台凝练扫描器启动失败（不阻断）: {exc}", flush=True)
    # [角色机制 5710 v2] 重启续跑：同步队列在 SQLite，重启后把 active 角色的
    # pending 同步重新唤醒。fail-open，不阻断启动。
    try:
        import asyncio as _asyncio
        from src.api import role_room as _role_room
        _asyncio.create_task(_role_room.startup_sweep_all())
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 角色续跑启动失败（不阻断）: {exc}", flush=True)
    # [预设职能 P2 2026-10-24] 职能观点队列的重启续跑(目录启用的 work 才有队列;
    # 空队列 ⇒ no-op)。fail-open,不阻断启动。
    try:
        import asyncio as _asyncio
        from src.api import function_room as _function_room
        _asyncio.create_task(_function_room.startup_sweep_all())
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 职能续跑启动失败（不阻断）: {exc}", flush=True)


@app.on_event("startup")
async def _start_goal_driver() -> None:
    """[goal 域 2026-10-20 · 照抄 dsh-goal] 装载续轮驱动。

    原版是插件 ``apply``：订阅 ``agent/status`` 与 ``goal/changed``。我们这边
    idle 事件由 ``routes._run_message`` 的 finally 报；这里补 goal/changed 监听。
    activation 永不落盘 ⇒ 进程重启后全部 disarmed，必须人显式 /goal resume。
    """
    try:
        from src.goal import driver as goal_driver
        goal_driver.install()
    except Exception as exc:  # noqa: BLE001 装载失败不阻断主服务
        print(f"[startup] goal 驱动装载失败（不阻断）: {exc}", flush=True)

from src.api.routes import router as api_router  # noqa: E402  (需在 .env 加载后导入)
# [模型设置第二步 2026-10-12] 设置接口（供应商/模型/角色绑定 + 密钥 + 备份还原）
from src.api.settings_api import router as settings_router  # noqa: E402
from src.api.asset_cache import install_asset_cache  # noqa: E402

app.include_router(api_router)
app.include_router(settings_router)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# [开发期缓存 2026-10-21] 页面与静态资源一律 no-cache（浏览器每次带 ETag 验证）：
# 文件没变走 304（几乎零成本），变了立刻拿到新的。
# 为什么要这条：`?v=NN` 手工/脚本升号是**唯一**的失效手段，只要漏一次，
# 用户就会看到「改了不生效」——今天为此浪费了三轮。本机单用户场景下
# 关掉强缓存没有任何代价。
@app.middleware("http")
async def _no_cache_assets(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# [画布验收 2026-10-19] 静态资源"可缓存但必须回源校验"，详见 src/api/asset_cache.py。
# 病根：原来全靠模板里手写 `?v=NN` 升号破缓存，忘了升号就改了不生效（白验收），
# 发版后用户旧缓存还会把新结构撑坏。
install_asset_cache(app)


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


def _render_work(work_id: str) -> str:
    """画布页模板渲染（templates/work.html）。

    [阶段3 A3.2 2026-10-17] 模板已拆分：画布页只注入 WORK_ID，CARD_ID 恒为空串。
    """
    html = (BASE_DIR / "templates" / "work.html").read_text(encoding="utf-8")
    return html.replace("{{WORK_ID}}", work_id).replace("{{CARD_ID}}", "")


def _render_card(work_id: str, card_id: str) -> str:
    """对话页模板渲染（templates/card.html，[阶段3 A3.2] 拆出的纯对话页）。"""
    html = (BASE_DIR / "templates" / "card.html").read_text(encoding="utf-8")
    return html.replace("{{WORK_ID}}", work_id).replace("{{CARD_ID}}", card_id)


@app.get("/work/{work_id}", response_class=HTMLResponse)
async def work_page(work_id: str) -> HTMLResponse:
    """Phase 8：剧本画布页。work_id 注入到 HTML 占位符。

    [建项目流程 2026-10-17] 唯一例外：``work_id == "draft"`` 是**项目草稿对话页**，
    不是画布页。用户点「＋新建项目」要的是开始对话；落到画布＝一块空地（病根①）。
    这里直接渲染对话模板并注入双 draft（项目 id 与卡 id 同为 "draft"），
    前端据此进草稿项目态：零网络、零 POST，首条消息落库后才换成真地址。
    """
    if work_id == "draft":
        return HTMLResponse(content=_render_card("draft", "draft"))
    return HTMLResponse(content=_render_work(work_id))


@app.get("/work/{work_id}/card/{card_id}", response_class=HTMLResponse)
async def work_card_page(work_id: str, card_id: str) -> HTMLResponse:
    """[阶段2 A2.1 / 阶段3 A3.2] 对话独立页：URL 即页面身份（刷新各回各家）。"""
    return HTMLResponse(content=_render_card(work_id, card_id))


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

    # [keep-alive 断连 2026-10-25] timeout_keep_alive 5 → 30：
    # uvicorn 默认**空闲 5 秒就关掉** keep-alive 连接。浏览器把连接池里的连接留着复用，
    # 空闲超过 5 秒的那条已被服务端关掉 ⇒ 下一次请求落在它身上 ⇒
    # Chrome 抛 `TypeError: Failed to fetch` ⇒ 前端弹「网络错误」。
    # 实测（裸 socket，同一条连接隔 N 秒再发一次）：
    #   等 1s / 3s ⇒ 正常 200；等 6s / 8s ⇒ **WinError 10053 连接中止**。
    # 浏览器自身通常会重试，但我们的 fetch 封装**故意不重试非超时错误**
    # （见 static/esm/api.js）⇒ 这类瞬时竞态直接变成用户可见的「网络错误」。
    # 两头一起治：服务端别在浏览器脚下断连接 ＋ 前端对连接层失败重试一次。
    uvicorn.run(app, host="127.0.0.1", port=5700, timeout_keep_alive=30)
