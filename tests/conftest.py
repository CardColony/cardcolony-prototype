# -*- coding: utf-8 -*-
"""pytest 全局隔离层（步骤一 · 回归网，card_012）。

两条铁律，所有用例共享：

**铁律一 · 不得触碰真实数据目录。**
`works/`、快照根、`logs/`、`file_tools` 的允许范围与备份目录，全部在
autouse fixture 里重定向进 pytest 的 tmp_path。手法与既有离线预检先例
`scripts/preflight_shadow_offline.py:30-40` 同族（直接改写模块级路径常量）。
之所以能这么改写：这些根在源码里都是**模块级常量**（`BASE_DIR` / `WORKS_DIR`
/ `SNAPSHOT_ROOT` / `LOGS_DIR` / `ALLOWED_ROOTS` / `BACKUP_DIR`），函数体通过
模块全局名访问它们——这正是步骤二「单一 paths 注入点」要收敛掉的东西；
本回归网在旧形态上先立住行为，改造后这些 monkeypatch 会被换成注入，
测试断言（行为）不动。

**铁律二 · 不得 import main。**
`main.py:83-85` 的 `startup_init()` 在**模块导入期**执行：建 `logs/`、`works/`，
跑 `init_git()`（`git add -A` + commit，要求机器有 git 二进制），写 `.server.pid`。
任何 `import main` 的测试都会污染工作目录。端到端用例改为自行用
`src.api.routes.router` 装配一个 FastAPI app（不挂 `/static`），经
`httpx.ASGITransport` 走真实 HTTP 链路——测的是同一批路由函数。
"""
from __future__ import annotations

import os

# [取消 inbox 状态机 2026-10-22 · D25] 测试环境关闭「发信即投递」。
# 既有测试大量是同步时序写的（发信 → 断言 pending → 手动 schedule → 断言回话），
# 自动投递会在发信瞬间起异步回合与它们冲突，并放大 Windows 会话文件锁抖动。
# 真实链路不受影响（见 message_runner.deliver_now 里的同名开关）。
os.environ.setdefault("ECO_NO_AUTO_DELIVER", "1")

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """把全部运行时根搬进 tmp_path，逐个用例一份干净沙箱。"""
    from src.api import file_tools, model_service, prompt_log
    from src.storage import (
        canvas_store, card_db, card_store, data_snapshot,
        manuscript_store, session_store, work_store,
    )

    base = tmp_path / "app_home"
    works = base / "works"
    works.mkdir(parents=True)
    (base / "trading-workspace").mkdir()
    (base / "推演agent").mkdir()
    logs = base / "logs"
    logs.mkdir()

    # 运行数据根：storage 五件套（shadow_store 经 card_store.WORKS_DIR 跟随）
    for mod in (card_store, work_store, canvas_store, session_store, manuscript_store):
        monkeypatch.setattr(mod, "WORKS_DIR", works)

    # [卡片存储迁移 SQLite] card_db **不设模块级路径常量**——库路径调用时从
    # card_store.WORKS_DIR 派生，故上面一行 patch 已自动覆盖它（新模块沙箱跟随）。
    # 这里显式清掉它的连接/锁缓存：用例之间 work_id 相同（work_001）但库在不同
    # tmp 目录，缓存按库绝对路径索引，跨用例残留会连到上一个沙箱的库。
    card_db.reset_caches()

    # 快照根：BASE_DIR 用于 _iter_protected_files 与相对路径计算
    monkeypatch.setattr(data_snapshot, "BASE_DIR", base)
    monkeypatch.setattr(data_snapshot, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    # [T-0001] 指纹文件是模块级常量，漏改会让用例写进真实仓库（铁律一）
    monkeypatch.setattr(
        data_snapshot, "_FINGERPRINT_FILE", base / ".snapshot_fingerprints.json"
    )

    # prompt 日志（审计双通道之一）
    monkeypatch.setattr(prompt_log, "LOGS_DIR", logs)

    # 文件工具边界：源码里 BASE_DIR 是硬编码绝对路径 D:\\AI_station
    monkeypatch.setattr(file_tools, "BASE_DIR", base)
    monkeypatch.setattr(
        file_tools, "ALLOWED_ROOTS",
        ((base / "trading-workspace").resolve(), (base / "推演agent").resolve()),
    )
    monkeypatch.setattr(file_tools, "BACKUP_DIR", base / "推演agent" / "_backups")

    # 模型层：清掉全局单例，除非用例自己设；MOCK_LLM 默认不设
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setattr(model_service, "_gateway", None)

    # [消息底座能力对齐 2026-10-13] 卡级事件流的进程内 seq 游标：每个用例一份
    # 沙箱文件树 ⇒ 游标必须随之清零（否则用例 B 会接着用例 A 的号往下取，
    # 而它面对的已是一个全新的空文件）。
    from src.storage import event_log
    monkeypatch.setattr(event_log, "_seq_cursor", {})
    event_log._pending.clear()

    yield SimpleNamespace(base=base, works=works, logs=logs, tmp=tmp_path)

    # [卡片存储迁移 SQLite] 用例一结束就关掉 card_db 的长活连接——否则打开的
    # cards.db / -wal / -shm 句柄会让 pytest 的 tmp 目录清理在 Windows 上卡死
    # （实测：测试跑完到 100% 却挂十几分钟不退出）。
    card_db.reset_caches()


def prime_card_session(work_id: str, card_id: str) -> None:
    """把卡片的会话文件**就地升成 V2 空会话**（只写 V2 头、零会话块）。

    [卡片底座第一步 2026-10-14] 为什么需要：``card_store.create_card`` 会给每张卡
    铺一个 **V1 占位**会话文件（``<!-- session: card_NNN, created: ... -->``，
    card_store.py:879-886），于是**首次落块**要走 ``session_facade.append`` 的
    「V1→V2 现场升格」分支——那条分支用 ``tmp→os.replace``。

    而 Windows 下 ``os.replace`` 与**并发读句柄**互斥（POSIX 允许 rename 覆盖被读
    的文件，Windows 不允许）：用例的「每 50ms 读一次会话」轮询会与收件方首次落块
    的 replace 撞车，实测偶发 ``WinError 5 拒绝访问``（重试也压不住——读句柄在
    加载下持续占着）。那是**既有隐患**（与本批改造无关），用例按「会话已升 V2」
    铺好即绕开——**不改产品行为**，只让用例不踩那条路。

    安全闸：已有会话块（``---MSG---``）或已是 V2 的文件**一律不动**。
    """
    from src.storage import session_facade, session_store

    p = session_facade._session_path(work_id, card_id)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if session_facade._detect_format(text) == 2 or session_store.MARKER in text:
        return
    session_facade._atomic_write_text(
        p, session_facade._render_v2_header(
            session_facade._default_header(card_id)))


@pytest.fixture
async def work(sandbox):
    """一个空剧本（含剧本根卡 card_001），带干净沙箱。"""
    from src.storage import work_store

    wdata = await work_store.create_work("回归测试剧本")
    assert "error" not in wdata, wdata
    return wdata

@pytest.fixture
def app():
    """一个只挂路由的 FastAPI app（**不挂** ``/static``、**不 import main**）。

    与真服务同一条路由链路（``src.api.routes.router``），差别只在外壳：
    真服务外面还套了 ``/static`` 与首页模板，见 conftest 顶部「铁律二」。

    [5715] 提到 conftest：对话模块的两张网（``test_conversation_flow`` /
    ``test_conversation_edges``）都要这套夹具，留在各自文件里会各写一份。
    """
    from fastapi import FastAPI

    from src.api import routes

    a = FastAPI()
    a.include_router(routes.router)
    return a


@pytest.fixture
async def client(app):
    """经 ``httpx.ASGITransport`` 走真实 HTTP 链路（不监听端口）。"""
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def typed_registry(monkeypatch):
    """[permgmt T8 · 2026-10-09] 注册分类沙箱：TYPE_REGISTRY_PATH 指向 tests/
    _fixtures 下的 registry 副本。

    权限收口后 POLICY 按真实分类发权，需要卡面是注册分类（根卡 rd_project／
    子卡 sub_project——create_card 缺省按本树词表映射）的用例显式挂本夹具。
    只影响挂用例；autouse 沙箱与其余套件保持「无注册表降级」现状不变。
    """
    from src.storage import card_store
    monkeypatch.setattr(
        card_store, "TYPE_REGISTRY_PATH",
        ROOT / "tests" / "_fixtures" / "card_type_registry.json",
    )
