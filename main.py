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

# [双循环隔离 2026-11-05] 服务循环 B（独立线程）上的一切写/emit/孵化动作
# 经 on_main 委托回主循环 A——startup 钩子与端点共用同一套边界。
from src.infra.loops import install_main_dispatch, on_main
from src.infra import procs as _procs


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

# [多进程拆分 2026-11-06 · 用户令] web 角色＝纯服务进程：白名单只读 GET
# 本地答，其余一切透明反代 runner（ECO_RUNNER_URL）。
_procs.install_web_proxy(app)

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
@on_main
async def _start_goal_driver() -> None:
    """[goal 重启恢复 2026-10-28] 装载续轮驱动 + 把还在跑的目标重新授上权。

    两件事一起做，缺一都白搭：
      ① ``install()``：挂 ``goal/changed`` 监听（改完目标要能立刻点火）；
         改前这里**从来没被调用过**，所以 `/goal resume` 只置 armed、没人点火。
      ② ``rearm_active_goals()``：``activation`` 永不落盘，重启后全部 disarmed——
         不恢复的话，数据库里 goal 还 active、界面上看着在跑，驱动却再也不认它
         （用户实测「goal 明明没完成却停下了」就是这个）。

    失败不阻断启动（起不来只是不自动续轮，服务本身该能用）。
    """
    try:
        from src.goal import driver as goal_driver
        goal_driver.install()
        await goal_driver.rearm_active_goals()
    except Exception as _exc:  # noqa: BLE001
        print(f"[startup] goal 驱动装载失败（不阻断启动）: {_exc}", flush=True)


@app.on_event("startup")
@on_main
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
    # [2026-10-29 删] 这里原来照角色版抄了一行
    #   `_function_room.startup_sweep_all()`
    # ——但 `function_room` 里**从来没有这个函数**（也永远不该有）：
    # 职能只在评审里被叫醒，不许有自动唤醒入口（见 `src/api/system_claims.py`
    # 的机械判据 banned = notify_event / schedule_event / spawn_opinion /
    # _profiles_watching / startup_sweep_all）。这行是死调用，每次启动必报错，
    # 已删除。别再照角色版往这里抄。



@app.on_event("startup")
@on_main
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

@app.on_event("startup")
@on_main
async def _resume_reviews() -> None:
    """[2026-10-29 用户裁定「用 harness 接管评审卡的流程存续」] 重启续跑。

    评审的流程存续现在靠事件驱动（一步一返回 + 策略 continue），进程内没有
    常驻循环。重启后不会有人自己接着跑 ⇒ 启动时扫一遍「还有批次的评审卡」，
    每张跑一步，之后交给事件链自己接下去。

    fail-open：任何一张失败只打印，不影响启动和其他卡。
    """
    try:
        import asyncio as _asyncio
        from src.storage import work_store as _ws
        from src.storage import card_db as _cdb
        from src.api import review_runner as _rr

        def _scan(wid: str) -> list[str]:
            def _fn(conn):
                out = []
                for r in conn.execute(
                        "SELECT card_id, review_state FROM cards"
                        " WHERE work_id=? AND card_type='review'", (wid,)):
                    import json as _json
                    try:
                        st = _json.loads(r["review_state"] or "{}")
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(st, dict) and str(st.get("batch") or ""):
                        out.append(str(r["card_id"]))
                return out
            return _cdb.read_sync(wid, _fn, []) or []

        async def _run() -> None:
            for w in _ws.list_works():
                wid = str(w.get("work_id") or "")
                if not wid:
                    continue
                for rid in _scan(wid):
                    try:
                        r = await _rr.run_once(wid, rid)
                        print(f"[startup] 评审续跑 {wid}/{rid}: {r.get('reason')}",
                              flush=True)
                    except Exception as exc:  # noqa: BLE001 单张失败不影响别的
                        print(f"[startup] 评审续跑失败 {wid}/{rid}: {exc}", flush=True)

        _asyncio.create_task(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 评审续跑装载失败（不阻断）: {exc}", flush=True)


@app.on_event("startup")
@on_main
async def _resume_interventions() -> None:
    """[干预台账 2026-10-31] 重启接责：复核开着的干预事项，重投没送成的动作。

    与上面的 ``_resume_reviews`` 互补：它兜"评审批还在、但没留下动作账"的
    老状态；本函数接管新链路——凡经台账登记的 pending/failed 动作，重启后
    按原文重投（投前查重 + 事项复核，不复活已解决的问题）。fail-open。
    """
    try:
        import asyncio as _asyncio
        from src.storage import work_store as _ws
        from src.infra import intervention_runner as _ir

        async def _run() -> None:
            for w in _ws.list_works():
                wid = str(w.get("work_id") or "")
                if not wid:
                    continue
                try:
                    stats = await _ir.resume(wid)
                    if stats.get("matters") or stats.get("actions"):
                        print(f"[startup] 干预接责 {wid}: {stats}", flush=True)
                except Exception as exc:  # noqa: BLE001 单项目失败不影响别的
                    print(f"[startup] 干预接责失败 {wid}: {exc}", flush=True)

        _asyncio.create_task(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 干预接责装载失败（不阻断）: {exc}", flush=True)


@app.on_event("startup")
@on_main
async def _scheduled_snapshots() -> None:
    """[2026-11-04 用户裁定] 快照只留**每日两次定时全备**。

    裁定背景：写路径逐写快照（schedule 30s 合并窗）＋错误路径 checkpoint
    已被撤（data_snapshot.SNAPSHOT_ON_WRITE 默认关、handle_error 不再拍）——
    「正常情况数据根本不会丢，唯一防的是模型瞎改」。⇒ 唯一的点火源是
    这里：每 ``ECO_SNAPSHOT_PERIOD_HOURS``（默认 12h）全量拍一轮
    （``work_id=None``＝全部 work）；开机后 ``ECO_SNAPSHOT_FIRST_DELAY_S``
    （默认 600s）先补一份基线（重启即丢全部留痕窗口）。fail-open。
    """
    try:
        import asyncio as _asyncio
        import os as _os
        from src.storage import data_snapshot as _ds

        period_h = float(_os.environ.get("ECO_SNAPSHOT_PERIOD_HOURS") or 12)
        first_delay = float(_os.environ.get("ECO_SNAPSHOT_FIRST_DELAY_S") or 600)

        async def _loop() -> None:
            await _asyncio.sleep(first_delay)
            while True:
                try:
                    ok = await _ds.snapshot(reason="scheduled:daily", work_id=None)
                    print(f"[snapshot] 定时全备 ok={ok}", flush=True)
                except Exception as exc:  # noqa: BLE001 尽力而为
                    print(f"[snapshot] 定时全备失败（下轮照跑）: {exc}", flush=True)
                await _asyncio.sleep(period_h * 3600)

        _asyncio.create_task(_loop())
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 定时快照装载失败（不阻断）: {exc}", flush=True)


@app.on_event("startup")
@on_main
async def _start_db_watchdog() -> None:
    """[库看门狗 2026-11-07 · work_027 cards.db 原地清零事故] stat 哨兵＋异常隔离。

    每 ECO_DB_WATCH_S（默认 5s）盯各库尺寸突变（归零/腰斩/连 WAL 消失），
    命中即把三件套拷进 db_quarantine/ 并 critical 告警；每 ECO_DB_CHECK_S
    （默认 3600s）抽副本跑 quick_check。fail-open：起不来不阻断主服务。
    """
    try:
        from src.storage import db_watchdog as _dw
        _dw.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 库看门狗启动失败（不阻断）: {exc}", flush=True)


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


# [双循环隔离 2026-11-05] 路由分发：白名单（无锁读/SSE 流）留在服务循环 B
# 原生跑；其余端点（写/emit/孵化/取锁）一律委托回主循环 A——跑卡域单写者
# 语义与单循环时代逐字一致。_MAIN_LOOP 未注册（测试）时原地执行、零差异。
install_main_dispatch(app)


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


def _install_listener_autopsy():
    """[死因取证 2026-11-04] 5700 监听插座已两次「凭空消失」（netstat 无
    LISTENING、进程与事件循环俱在）——插座是内核对象不会自己死，必有代码
    主动 close。给 socket.close 挂探针：SO_ACCEPTCONN 只在 LISTENING 插座
    上为真，谁关它（Server.close／传输拆解／GC 收尸）都落 traceback 到
    logs/listener_autopsy.log——下次死亡即现场。"""
    import socket as _s
    import time as _t
    import traceback as _tb
    _real_close = _s.socket.close

    def _traced_close(self):
        try:
            if self.getsockopt(_s.SOL_SOCKET, _s.SO_ACCEPTCONN):
                try:
                    laddr = self.getsockname()   # 5700＝真杀；临时口＝socketpair 噪音
                except OSError:
                    laddr = "?"
                with open(BASE_DIR / "logs" / "listener_autopsy.log",
                          "a", encoding="utf-8") as f:
                    f.write(f"\n===== {_t.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"LISTEN 插座被 close laddr={laddr} =====\n")
                    _tb.print_stack(file=f)
        except OSError:
            pass
        return _real_close(self)

    _s.socket.close = _traced_close


def _autopsy_note(text: str) -> None:
    """给尸检日志写一行（容错，绝不抛）。"""
    try:
        import time as _t
        p = BASE_DIR / "logs" / "listener_autopsy.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:  # noqa: BLE001
        pass


async def _persistent_accept(real_accept, proactor, sock):
    """accept 瞬发 OSError 的内部重试体（模块级便于单测）。

    ``real_accept(proactor, sock)`` 每次调用返回一个 awaitable〔完成值
    ``(conn, addr)``〕。瞬发 OSError 且插座还活着 ⇒ 退避重发；
    CancelledError（关停）与插座真死（fileno==-1）原样上抛。
    """
    import asyncio as _a
    attempt = 0
    inner = None
    while True:
        try:
            inner = real_accept(proactor, sock)
            return await inner
        except _a.CancelledError:
            try:
                if inner is not None:
                    inner.cancel()              # 清掉没人消费的内层 accept
            except Exception:  # noqa: BLE001
                pass
            raise
        except OSError as exc:
            if sock.fileno() == -1:
                raise                           # 插座真没了：交回原逻辑
            attempt += 1
            _autopsy_note(
                f"accept 瞬发失败 #{attempt} "
                f"winerror={getattr(exc, 'winerror', None)}：未关插座，退避重发")
            await _a.sleep(min(1.0, 0.05 * attempt))


def _install_accept_resilience() -> None:
    """[死因实锤 2026-11-04] 监听插座「凭空消失」的真凶：CPython 自身。

    ``proactor_events.py`` 的 accept 循环（``BaseProactorEventLoop._accept2``
    内的 ``loop`` 闭包）：只要 AcceptEx 的完成值是一次 **OSError**——
    例如 WinError 64（NETNAME_DELETED）、995（OPERATION_ABORTED）这类
    **瞬发**网络异常（本机有虚拟显示/网络过滤驱动时会偶发）——它的处理是
    记一条 "Accept failed" 然后 ``sock.close()`` **直接关掉监听插座**：
    零重试、零自愈，进程照跑、LISTENING 消失、只能重启。
    （尸检日志 15:50:13 抓到的关闭栈即此。）

    补丁：包住 ``IocpProactor.accept``——完成值为 OSError 且**插座还活着**
    时，不把异常交回 loop（那必走 close 分支），而是内部重新发出一次
    accept（退避递增、封顶 1s）；插座真关或 CancelledError 才原样上抛
    （关停路径语义不变）。返回契约仍是 ``(conn, addr)`` 的 awaitable，
    ``loop`` 闭包零感知。
    """
    import asyncio as _a  # noqa: F401
    try:
        from asyncio import windows_events as _we
    except ImportError:  # 非 Windows 无此路径
        return
    _real_accept = _we.IocpProactor.accept

    def _accept(self, sock):
        return self._loop.create_task(
            _persistent_accept(_real_accept, self, sock))

    _we.IocpProactor.accept = _accept
    _autopsy_note("accept 韧性补丁已装载（IocpProactor.accept 瞬发错误→重试，不再关插座）")


if __name__ == "__main__":
    import uvicorn

    _install_listener_autopsy()
    _install_accept_resilience()

    # [keep-alive 断连 2026-10-25] timeout_keep_alive 5 → 30：
    # uvicorn 默认**空闲 5 秒就关掉** keep-alive 连接。浏览器把连接池里的连接留着复用，
    # 空闲超过 5 秒的那条已被服务端关掉 ⇒ 下一次请求落在它身上 ⇒
    # Chrome 抛 `TypeError: Failed to fetch` ⇒ 前端弹「网络错误」。
    # 实测（裸 socket，同一条连接隔 N 秒再发一次）：
    #   等 1s / 3s ⇒ 正常 200；等 6s / 8s ⇒ **WinError 10053 连接中止**。
    # 浏览器自身通常会重试，但我们的 fetch 封装**故意不重试非超时错误**
    # （见 static/esm/api.js）⇒ 这类瞬时竞态直接变成用户可见的「网络错误」。
    # 两头一起治：服务端别在浏览器脚下断连接 ＋ 前端对连接层失败重试一次。
    # [双循环隔离 2026-11-05] 实测满载时 /api/nav/version 9.34s——不是插座死，
    # 是 runner 的同步 CPU 段把协作式循环饿死、所有请求排队。
    #   主循环 A（本线程）：runner/scheduler/harness + 一切写与 emit；
    #   服务循环 B（serving-loop 线程）：uvicorn + 静态 + 白名单读 + SSE。
    # 线程间由 OS 抢占调度 ⇒ HTTP 永不被跑卡域的同步段饿死。
    import asyncio as _asyncio
    import threading as _threading

    from src.infra import loops as _loops

    _main_loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(_main_loop)
    _loops.set_main_loop(_main_loop)
    _loops.start_lag_monitor(_main_loop)
    # [runner 分槽环 2026-11-05] 卡按子树根亲和散到 K 条环（lane0＝A）。
    # 默认 1＝行为与分槽前逐字一致；ECO_RUNNER_LOOPS=2+ 生效。
    # [多进程拆分 2026-11-06] web 角色没有跑卡域——不起槽环；
    # 驱动器/调度器/续跑全部 startup 钩子是跑卡域的活：web 一个都不跑，
    # 非 0 号 runner 只留 goal install（goal/changed 是本进程内监听，
    # 每张卡在自己的属主进程里改 goal ⇒ 监听必须每个 runner 都有）；
    # 全库一份的续跑/调度只跑在 0 号，恢复出来的回合经 _run_message
    # 分片自查自动转去属主。
    if _procs.is_web():
        app.router.on_startup.clear()
    elif _procs.is_runner() and not _procs.is_primary_runner():
        async def _runner_local_startup() -> None:
            try:
                from src.goal import driver as goal_driver
                goal_driver.install()
            except Exception:  # noqa: BLE001
                pass
        app.router.on_startup[:] = [_runner_local_startup]
    if _procs.runner_side():
        _loops.start_runner_lanes(
            int(os.environ.get("ECO_RUNNER_LOOPS", "1") or "1"))

    _holder: dict = {}

    def _serve() -> None:
        loop_b = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop_b)
        _loops.set_serving_loop(loop_b)
        # [多进程拆分 2026-11-06] web 守 5700（对外）；runner-i 守
        # ECO_RUNNER_BASE_PORT+i（缺省 5701+i，回环只接 web 反代与
        # runner 间转发）；all＝旧行为单进程 5700。
        _base = int(os.environ.get("ECO_RUNNER_BASE_PORT") or 5701)
        _port = int(os.environ.get("ECO_PORT")
                    or (_base + _procs.runner_index()
                        if _procs.is_runner() else 5700))
        config = uvicorn.Config(
            app, host="127.0.0.1", port=_port, timeout_keep_alive=30,
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        _holder["server"] = server
        try:
            loop_b.run_until_complete(server.serve())
        finally:
            loop_b.close()

    _serve_thread = _threading.Thread(
        target=_serve, name="serving-loop", daemon=True)
    _serve_thread.start()

    try:
        _main_loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _srv = _holder.get("server")
        if _srv is not None:
            _srv.should_exit = True
            _serve_thread.join(timeout=5)
        _loops.stop_runner_lanes()
        _main_loop.close()
