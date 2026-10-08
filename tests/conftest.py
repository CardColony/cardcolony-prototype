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
        canvas_store, card_store, data_snapshot,
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

    # [研究项目词表与迁移 T4 · 2026-10-08] 类型注册表同样属于"真实环境"，
    # 沙箱内指向 tmp_path 下不存在的文件 ⇒ load_type_registry() 返回 {} ⇒
    # 按 card_store 既有设计"无注册表降级放行"（不阻断写路径）。
    # 依据：load_type_registry docstring 明说"路径由模块级常量派生，测试可
    # monkeypatch 改写（与 WORKS_DIR 同一套隔离手法）"——基座 T2 预告了手法
    # 但未落地；「线索」write_blocked 收紧后测试加载生产真实注册表会大面积
    # 误红（32 条用默认 card_type='线索' 建卡），故补齐本隔离点。
    # 代价（如实记）：注册表闸逻辑在 pytest 内从此零覆盖（原本也是零——
    # tests/ 无任何基座用例）；闸的真实验证在沙箱外以自检脚本执行（见
    # 执行回报 §T3），基座测试建设属后续任务。
    monkeypatch.setattr(
        card_store, "TYPE_REGISTRY_PATH",
        base / "config" / "card_type_registry.json",
    )

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

    yield SimpleNamespace(base=base, works=works, logs=logs, tmp=tmp_path)


@pytest.fixture
async def work(sandbox):
    """一个空剧本（含剧本根卡 card_001），带干净沙箱。"""
    from src.storage import work_store

    wdata = await work_store.create_work("回归测试剧本")
    assert "error" not in wdata, wdata
    return wdata