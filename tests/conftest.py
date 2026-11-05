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
# [伴生评审卡 2026-10-27] 测试里不起评审回合（会真调模型）——照上面同一先例。
os.environ.setdefault("ECO_NO_AUTO_REVIEW", "1")

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
    # [2026-10-27] 门面的解析缓存也按 work_id 索引，跨用例会串味 ⇒ 一并清。
    from src.storage import session_facade as _sf_clear
    _sf_clear.reset_cache()
    # [干预台账 2026-10-31] harness.db 连接按 (works_dir, work_id) 索引：
    # 单例还指着上个沙箱目录时，同样的 work_id/card_id/seq 会让动作去重
    # 键撞上旧库已投递的记录 ⇒ 新用例的投递被吞（批量红、单跑绿）。
    # 与上面 card_db 同一先例：沙箱建好后就把单例绑到本沙箱的 works。
    from src.infra import intervention_store as _ist_clear
    _ist_clear.reset(_ist_clear.InterventionStore(works_dir=works))

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

    # [wb_exec 2026-10-23] **执行体（WorkBuddy）隔离**：
    # 仓库里的 config/wb_exec.json 是**开闸态**（enabled=true）——
    # 若测试直接用它，任何跑 worker 卡回合的用例会**真去起 WorkBuddy 网关**
    # （spawn cbc --serve、等健康 90 秒、可能联网）⇒ 慢且不确定。
    # ⇒ 全部用例强制指向一份**关着**的临时配置。
    # （要测开闸态的行为，用例自己 monkeypatch WB_EXEC_CONFIG_PATH 指向真配置。）
    import json as _json
    from src.api.wb_exec import config as _wb_config
    from src.api.wb_exec import picker as _wb_picker
    from src.api.wb_exec import registry as _wb_registry
    from src.api.wb_exec import switch as _wb_switch
    _wb_off = base / "wb_exec_off.json"
    _wb_off.write_text(_json.dumps({"enabled": False}, ensure_ascii=False),
                       encoding="utf-8")
    monkeypatch.setenv(_wb_config.ENV_CONFIG_PATH, str(_wb_off))
    # **★ 运行时总开关也要关掉**（2026-10-23 23:55 修）：
    #   ``config/wb_exec_switch.json``（用户手动总开关，enabled=true）**优先级高于**
    #   上面那份"关着"的配置 ⇒ 只关配置**不够** ⇒ 单元测试里委派照样被打开
    #   ⇒ 测试会**真去连本机正在跑的 WorkBuddy**（还起 CLI 池、真调模型）
    #   ⇒ 把 ``test_k3_busy_receiver_keeps_letter`` 这类用例跑挂。
    #   ⇒ 测试期间把开关指到一份"关着"的临时文件。
    _wb_sw_off = base / "wb_exec_switch_off.json"
    _wb_sw_off.write_text(_json.dumps({"enabled": False}, ensure_ascii=False),
                          encoding="utf-8")
    monkeypatch.setattr(_wb_switch, "SWITCH_PATH", _wb_sw_off)
    _wb_config.reset_cache()
    _wb_picker.reset_pickers()
    _wb_registry.reset_registry()

    # [差事策略 2026-11-01] **harness 策略集隔离**：仓库
    # config/harness_policies.json 里 staff_* deploy 策略是真点火——
    # 建差事＋起模型回合（`_run_message`）。任何发 card_created /
    # card_goal_completed 事件的用例都会经 event_log→feed 真去调模型。
    # ⇒ 全部用例指向空策略表（feed 机制照跑、零命中），并清 _SET 单例
    #   防用例间串味；要测真策略的用例自己把 ECO_POLICIES_PATH 指向目标
    #   文件并 ``policy_set.reset_set(自建集)``（照 wb_exec 同一先例）。
    _pol_off = base / "policies_off.json"
    _pol_off.write_text(_json.dumps({"policies": []}, ensure_ascii=False),
                        encoding="utf-8")
    monkeypatch.setenv("ECO_POLICIES_PATH", str(_pol_off))
    from src.infra import policy_set as _ps
    _ps.reset_set()

    # [预设职能 2026-10-24] **职能目录隔离**：仓库里的 config/function_catalog.json
    # 是开闸态(seed_on_create=true)——若测试直接用它,任何建 work 的用例会真播种
    # 五个职能(总裁+四职能卡),改变卡数/收口闸/上下文断言 ⇒ 基线漂移。
    # ⇒ 全部用例强制指向一份「关着」的临时目录(要测播种的用例自己把
    #   FUNCTION_CATALOG_PATH 指向真目录或临时开闸文件)。
    from src.api import function_room as _fr
    _fc_off = base / "function_catalog_off.json"
    _fc_off.write_text(_json.dumps({"seed_on_create": False, "functions": []},
                                   ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(_fr.ENV_CATALOG_PATH, str(_fc_off))
    _fr.reset_cache()

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
    # [干预台账] harness.db 同型问题：关连接再还单例，tmp 目录不被句柄拖住。
    _ist_clear.reset()

    # [自动停止权威性 2026-10-24] goal 服务是**进程单例**，缓存里存的是按事件
    # fold 出来的目标状态。`control.stop_auto` 现在走 `service.pause()`（权威命令），
    # 会顺带为那张卡建一份缓存——不清就会跨用例串：下一个用例直接改 DB 的
    # `goal_state` 列，读到的却还是上个用例的缓存 ⇒ 批量跑红、单跑绿。
    # 原来只有 test_goal_reactivate 自己清，覆盖面不够；这里统一清。
    try:
        from src.goal.service import get_service as _get_goal_service
        _get_goal_service()._caches.clear()
    except Exception:  # noqa: BLE001 goal 包未装/未初始化 ⇒ 无需清
        pass

    # [差事策略 2026-11-01] 策略单例也清：用例自建的集不能漏给下一个用例
    # （下一个用例的 dispatcher() 会懒加载——env 已回到空表）。
    _ps.reset_set()

    # [wb_exec 2026-10-23] 执行体的进程级状态也要清（配比器／未登录记忆／
    # 网关注册表），否则会跨用例串。
    from src.api.wb_exec import config as _wb_config
    from src.api.wb_exec import picker as _wb_picker
    from src.api.wb_exec import registry as _wb_registry
    from src.api.wb_exec import switch as _wb_switch
    _wb_config.reset_cache()
    _wb_picker.reset_pickers()
    _wb_registry.reset_registry()
    # [总开关 2026-10-23 16:20] 执行体总开关的**运行时文件**也要隔离到沙箱
    # （否则用例里切开关会写仓库的 config/wb_exec_switch.json ⇒ 影响真跑）。
    _wb_switch.SWITCH_PATH = base / "wb_exec_switch.json"


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


@pytest.fixture(autouse=True)
def _wb_exec_quota_state_to_tmp(tmp_path_factory, monkeypatch):
    """wb_exec 的**额度状态文件不许写进仓库**（2026-11-04）。

    背景：``registry.mark_exhausted`` 现在会**同时落盘**
    （跨重启生效 ⇒ 生产重启后仍记得某个端点额度用完）。
    ⇒ 用例里触发 quota ⇒ 会把 ``config/wb_exec_quota.json`` 写脏
    ⇒ **污染真实配置**（实测：一次测试跑完，wb-cn 的两个档被标成"额度耗尽"）。

    ⇒ 这里把状态文件重定向到临时目录（**只影响测试**）。
    """
    from src.api.wb_exec import quota_state
    path = tmp_path_factory.mktemp("wb_exec_quota") / "quota.json"
    monkeypatch.setattr(quota_state, "STATE_PATH", path)
    yield
