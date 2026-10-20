# -*- coding: utf-8 -*-
"""5701 测试实例启动器（信息流重出 · 真请求取证用，开工令后执行验收的预演）。

**为什么不用 main.py**：main.py 在**模块导入期**执行 `startup_init()`，内含
`init_git()`（`git add -A` + commit）与 `write_pid()`（覆盖 `.server.pid`）。
本仓库对 git 操作与在途服务进程身份有硬约束（2026-10-03 事故禁令、Task5「三验：
进程身份」），因此测试实例**不 import main**，而是照抄它的 app 装配（router +
/static + index + work 页），只跳过 startup_init。

与真服务的差异（取证时须知）：
  - 不跑 init_directories / init_methodology / init_canvas / init_git / write_pid；
  - 其余装配与 main.py 逐行一致，跑的是同一批路由函数与同一份静态资源。

用法：
  .venv\\Scripts\\python.exe scripts\\run_test_instance_5701.py
  （前台运行；Ctrl+C 停。停后请自行确认 5700 生产进程未受影响。）
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")   # 与 main.py 同款：先加载 .env 再导入路由

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from src.api.routes import router as api_router  # noqa: E402
# [画布 5711] 补上设置路由（与 main.py 装配一致）。
# 原来只 include 了 api_router ⇒ 画布页的模型设置请求（/api/settings/models、
# /api/settings/model-overrides/{work}）在测试实例上**全部 404**——取证时看到的
# 网络失败其实是**实例装配缺件**，不是产品缺陷。补齐后实例与生产同构。
from src.api.settings_api import router as settings_router  # noqa: E402


def redirect_roots() -> str | None:
    """把全部运行时根搬进沙箱，使真请求**零触碰**生产 works/（红线6）。

    真发一条消息必然要往卡会话里写用户消息与助手回复——不隔离就直接改到生产数据面。
    手法与 tests/conftest.py 的 sandbox fixture 同族（改写模块级路径常量），
    只在 ECO_TEST_SANDBOX=1 时启用；返回沙箱根目录。

    代价（取证时须知）：沙箱 works/ 是空的，会重新初始化，故首轮比生产慢一点；
    但**路由函数、模型调用、静态资源与生产完全同源**，逐字流取证成立。
    """
    import os
    import shutil
    import tempfile

    if os.environ.get("ECO_TEST_SANDBOX") != "1":
        return None

    # [2026-10-19] 支持复用已有沙箱根：``ECO_SANDBOX_ROOT=<路径>`` 时直接用它，
    # 不再 mkdtemp——否则每次重启都换目录，正在跑的实验数据全丢。
    fixed_root = os.environ.get("ECO_SANDBOX_ROOT", "").strip()
    if fixed_root:
        base = Path(fixed_root)
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(tempfile.mkdtemp(prefix="eco_5701_"))
    works = base / "works"
    works.mkdir(parents=True, exist_ok=True)
    (base / "trading-workspace").mkdir(exist_ok=True)
    (base / "推演agent").mkdir(exist_ok=True)
    logs = base / "logs"
    logs.mkdir(exist_ok=True)

    from src.api import file_tools, prompt_log
    from src.storage import (
        canvas_store, card_store, data_snapshot,
        manuscript_store, session_store, work_store,
    )

    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        mod.WORKS_DIR = works
    data_snapshot.BASE_DIR = base
    data_snapshot.SNAPSHOT_ROOT = base / "snapshots"
    data_snapshot._FINGERPRINT_FILE = base / ".snapshot_fingerprints.json"
    prompt_log.LOGS_DIR = logs
    file_tools.BASE_DIR = base
    file_tools.ALLOWED_ROOTS = (
        (base / "trading-workspace").resolve(), (base / "推演agent").resolve(),
    )
    file_tools.BACKUP_DIR = base / "推演agent" / "_backups"
    os.environ["_ECO_TEST_SANDBOX_ROOT"] = str(base)
    return str(base)


_SANDBOX_ROOT = redirect_roots()

app = FastAPI(title="推演agent 5701 测试实例")

app.include_router(api_router)
app.include_router(settings_router)   # [画布 5711] 与 main.py 装配一致（见上方注释）
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # [首页两态 2026-10-15] 与 main.py 同步：根地址渲染 home.html（原 index.html 已随旧列表页退役删除）。
    html = (BASE_DIR / "templates" / "home.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


def _render_work(work_id: str) -> str:
    """与 main.py 逐行一致（[阶段3 A3.2] 画布页只注入 WORK_ID）。"""
    html = (BASE_DIR / "templates" / "work.html").read_text(encoding="utf-8")
    return html.replace("{{WORK_ID}}", work_id).replace("{{CARD_ID}}", "")


def _render_card(work_id: str, card_id: str) -> str:
    """与 main.py 逐行一致（[阶段3 A3.2] 对话页渲染 card.html）。"""
    html = (BASE_DIR / "templates" / "card.html").read_text(encoding="utf-8")
    return html.replace("{{WORK_ID}}", work_id).replace("{{CARD_ID}}", card_id)


@app.get("/work/{work_id}", response_class=HTMLResponse)
async def work_page(work_id: str) -> HTMLResponse:
    """与 main.py 逐行一致（[建项目流程 2026-10-17] /work/draft 特判渲染对话模板）。"""
    if work_id == "draft":
        return HTMLResponse(content=_render_card("draft", "draft"))
    return HTMLResponse(content=_render_work(work_id))


@app.get("/work/{work_id}/card/{card_id}", response_class=HTMLResponse)
async def work_card_page(work_id: str, card_id: str) -> HTMLResponse:
    """[阶段2 A2.1 / 阶段3 A3.2] 对话独立地址（与 main.py 逐行一致）。"""
    return HTMLResponse(content=_render_card(work_id, card_id))


@app.get("/__instance__")
async def instance_marker() -> dict:
    """进程身份探针：确认请求确实落在测试实例上，而不是 5700 生产。"""
    import os

    return {
        "instance": "test-instance",
        "pid": os.getpid(),
        "base_dir": str(BASE_DIR),
        "sandbox_root": _SANDBOX_ROOT,
        "sandbox_enabled": _SANDBOX_ROOT is not None,
    }


if __name__ == "__main__":
    import uvicorn

    # 端口可传参（默认 5702）。原定 5701，实测发现 5701 已被一个**既存** main.py
    # 实例（PID 30548）占用——非本任务启动，故不抢占、不终止，改用相邻空闲端口。
    # 换端口不影响取证：跑的是同一批路由函数与同一份静态资源。
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5702
    # [goal 域 2026-10-20] 本脚本**不 import main**（见文件头说明），所以 main.py 里
    # 的 startup 钩子在这条路径上不会跑——续轮驱动必须在这里显式装载。
    from src.goal import driver as _goal_driver
    _goal_driver.install()
    uvicorn.run(app, host="127.0.0.1", port=port)