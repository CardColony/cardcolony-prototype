# -*- coding: utf-8 -*-
"""WorkBuddy 执行体配置（读 config/wb_exec.json）。

**fail-open 纪律**：配置读不到 / 解析失败 ⇒ 全链路旁路（coder 卡仍走系统自己的模型），
绝不因为配置问题把 coder 卡搞挂。

配置项语义见 config/wb_exec.json 的 ``_meta`` 与各段 ``_doc``。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("api.wb_exec.config")

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config" / "wb_exec.json"

#: 环境变量覆盖配置路径（测试与多实例用）。
ENV_CONFIG_PATH = "WB_EXEC_CONFIG"


@dataclass(frozen=True)
class Executor:
    """一个 WorkBuddy 实例（国内版 / 国际版）＝ 一个 ACP 网关。"""

    id: str
    label: str
    install_root: str
    product_config: str
    cli_bin: str
    node_bin: str = ""
    port: int = 0
    #: **该版本自己的数据根**（账号 authSession 落在这里）——
    #: 国内版 `C:\Users\<u>\.workbuddy`、国际版 `C:\Users\<u>\.workbuddy-ai`。
    #: **不给 ⇒ CLI 读默认空目录 ⇒ 没有 authSession ⇒ 一律 "Authentication required"**
    #: （2026-10-23 实测踩到的坑）。
    config_dir: str = ""
    #: **桌面端登记文件目录**（``sessions/<pid>.json``）——
    #: 里面写着跑起来的网关 ``url`` / ``sessionId`` / ``cwd``。
    #: 留空 ⇒ 默认 ``<config_dir>/sessions``。
    sessions_dir: str = ""
    #: **显式指定网关地址**（测试/特例）。留空 ⇒ 自动发现桌面端的活网关。
    base_url: str = ""
    #: **只认桌面端起的网关**（父进程链里有 ``WorkBuddy.exe``/``WorkBuddyAI.exe``）。
    #: 默认开 —— 自己起的 CLI 也会在同一个 ``sessions/`` 目录留登记文件，
    #: 会被优先选中然后必然鉴权失败（实测踩到）。测试可关掉。
    require_desktop_owner: bool = True
    #: **这个版本的桌面端主进程名**（例如国内版 ``WorkBuddy.exe``、
    #: 国际版 ``WorkBuddyAI.exe``）。
    #:
    #: **为什么必须有它**（2026-10-23 18:10 实测打出来的能力矩阵）：
    #: 只有**监听进程本身就是桌面端主进程**的网关才带登录态、能真调模型——
    #: 桌面端自己起的 CLI 子进程（prewarm 热池 / 由网关 ``process/start`` 派生的
    #: 租约 worker）**全都拿不到登录态**：
    #: 有的在 ``session/load`` 就报 ``Authentication required``，
    #: 有的能建会话但**发 prompt 时** ``stopReason=refusal`` ＋
    #: ``errorMessage={"category":"auth"}``。
    #: ⇒ 发现阶段就按"监听进程名"过滤，别等派发时才发现。
    desktop_procs: tuple[str, ...] = ("WorkBuddy.exe", "WorkBuddyAI.exe")
    #: **完整版 CLI 可执行文件**（`codebuddy install` 装的原生版，**带 TUI 与
    #: `/login`**）。桌面端内嵌的那份是"精简版"（没有 TUI、没 Web UI、命令表里
    #: 也没有 login）⇒ **没法自己登录** ⇒ 只能当"桌面端的运行时"用。
    #: 填了它 ⇒ 执行体走**自己起 CLI 进程池** ⇒ **真并行**（一进程一槽）。
    cli_exe: str = ""
    #: **CLI 用的配置目录**（登录凭据落在这儿；通常＝该版本数据根）。
    cli_config_dir: str = ""
    #: **CLI 进程池大小**（每个进程一个 agent 槽 ⇒ 并发度）。
    cli_pool_size: int = 0
    #: **探针模型**（验"这个 CLI 能不能真干活"时用哪个模型；
    #: 必须是**表内免费模型**，不许拿付费端点做探针）。
    cli_probe_model: str = "hy3"
    #: **WSL 模式**：``{distro, home, cmd}`` —— 进程跑在 WSL 里，
    #: **凭据目录在 WSL 的 HOME 下 ⇒ 与桌面端、与另一套 CLI 物理隔离**
    #: （2026-10-23 用户要求"两套 CLI 服务，各自登录账号"）。
    #: 例：``{"distro": "Ubuntu-22.04", "home": "/home/user/intl-home",
    #: "cmd": "/home/user/.local/bin/codebuddy"}``
    cli_wsl: Optional[dict[str, str]] = None
    #: **隔离的 USERPROFILE**（两套 CLI 服务各自的凭据目录靠它分开：
    #: 凭据目录 ＝ ``%USERPROFILE%\AppData\Local\CodeBuddyExtension\Data\Public\auth``）
    cli_home: str = ""
    # ------------------------------------------------------------ opencode
    #: **opencode serve 可执行文件**（``opencode serve`` = headless HTTP 引擎）。
    #: 填了 ⇒ 这个执行体走 **opencode**（原生 CLI 池那套是 ACP）。
    oc_serve_exe: str = ""
    #: **opencode 进程数** ＝ **槽位数**（一进程一槽，与 WorkBuddy 的 CLI 池同构）。
    oc_pool_size: int = 0
    #: **opencode 直连的上游端点**（OpenAI 兼容）—— 通常是用户的 **Go 订阅**：
    #: ``https://opencode.ai/zen/go/v1``。
    oc_base_url: str = ""
    #: 上游 key 的环境变量名（**不写明文**）。
    oc_api_key_env: str = ""
    #: ★ **多个 key 的环境变量名**（2026-11-03 加）。
    #: ⇒ **一个 key 一个工位** ⇒ 免费额度 ×N（每个 worker 用自己的 key）。
    oc_api_key_envs: tuple[str, ...] = ()
    #: **自定义 provider 的 id**（写进 opencode.json；用它调模型）。
    oc_provider_id: str = "wb-direct"
    #: ★ **用 opencode 的「内置 provider」**（2026-11-01 加）。
    #: ⇒ **不写**自定义 provider 配置（写了就会把内置的同名 provider 顶掉）
    #: ⇒ 直接用 opencode 自带的 provider id（如 ``opencode``）＋
    #:    serve 进程环境里的 key（如 ``OPENCODE_API_KEY``）。
    #: **为什么需要它**：Zen 的**免费档**（``deepseek-v4.1-flash-free`` 等）
    #: **只允许从 opencode 内部调** —— 裸 HTTP 直连按量端点会被拒
    #: （实测 429「Free usage limit reached. Free models work in OpenCode」）
    #: ⇒ 走自定义 provider（直连）**拿不到免费档** ⇒ 只能走内置 provider。
    oc_use_builtin_provider: bool = False
    #: 可用模型清单（写进 opencode.json 的 models）。
    oc_models: tuple[str, ...] = ()
    #: **必须带浏览器 UA**（2026-10-25 实测：opencode 自己的 fetch 被 Cloudflare
    #: 拦 ⇒ 403「error code: 1010」⇒ 连已付费订阅内的模型也一起挂）。
    oc_user_agent: str = ""
    # ---------------------------------------------------------- Devin（2026-11-03）
    #: **Devin CLI 的可执行文件**（``%LOCALAPPDATA%\\devin\\cli\\bin\\devin.exe``）。
    devin_cli_exe: str = ""
    #: **模型**（模糊短名：``swe-2``/``swe``/``opus``/``sonnet``/``codex``/``gemini``）。
    #: ★ **SWE-2 在用户套餐里是免费的**（用户 2026-11-03）⇒ 默认用它。
    devin_model: str = "swe-2"
    #: **权限模式**：``bypass``（=dangerous/yolo）——不给的话 agent 会停下来等审批 ⇒ 卡死。
    devin_permission_mode: str = "bypass"
    #: **走云端会话**（用账号额度；False = 本地 agent 跑）。
    devin_cloud: bool = True
    #: **同时跑几个 Devin 会话**（远端 ⇒ 上限取决于 org 额度池）。
    devin_max_concurrent: int = 2
    #: 单轮任务的墙钟上限（秒）——**兜底**（正常由 CLI 自己收口）。
    devin_timeout_sec: float = 3600.0
    #: Devin 的组织 ID 的环境变量名（设置 → Devin API 页面顶部）。
    devin_org_id_env: str = ""
    #: Devin 的 **服务用户 API key** 的环境变量名（``cog_`` 开头）。
    #: ★ **不是** webapp 会话令牌（``apk_user_…``）——那个打 /v3 全 403。
    devin_api_key_env: str = ""
    #: Devin API 基址（走 CLI 时用不上，留着做诊断）。
    devin_base_url: str = "https://api.devin.ai/v3"
    # -------------------------------------------------- Command Code（第五路）
    #: **Command Code 的入口脚本**（npm 全局装的 ``dist/index.mjs``）。
    #: 填了 ⇒ 这个执行体走 command-code（2026-11-04 加）。
    cc_cli_entry: str = ""
    #: **node 可执行文件**（默认 ``node``）。
    cc_node_exe: str = "node"
    #: **key 的环境变量名**（``COMMAND_CODE_API_KEY``；**只写名字，不写明文**）。
    cc_api_key_env: str = "COMMAND_CODE_API_KEY"
    #: **可用模型**（候选表里指定具体档位；这里是兜底默认）。
    cc_models: tuple[str, ...] = ()
    #: **同时跑几个**（一个进程 = 一个槽）。
    cc_max_concurrent: int = 2
    #: **单轮墙钟上限**（秒）。
    cc_timeout_sec: float = 3600.0
    #: **一轮最多几个 turn**（agent 自己停；超了 CLI 会 exit 8）。
    cc_max_turns: int = 8
    # ------------------------------------------------------------ 会话清理
    #: ★ **用完在"这个客户端"里清掉我们自己那条会话**（2026-11-04 用户令：
    #:   「各自做完的任务，在各自客户端内做一个清理机制」）。
    #: ★★ **红线**：「千万别删除对话」「千万别删错对话」
    #:   ⇒ **默认关**；只有我们自己起的 CLI 执行体（wb-devin / wb-oc）显式开；
    #:   **桌面端会话库（用户的对话）永远不碰**（见 cleanup.py 的六条纪律）。
    cleanup_after_run: bool = False

    def node_path(self) -> str:
        """node 可执行文件：配置显式给 > 该版本自带的 binaries > 系统 node。"""
        if self.node_bin:
            return self.node_bin
        local = (Path(self.install_root)
                 / "binaries" / "node")
        # <root>/binaries/node/versions/<ver>/node.exe
        if local.is_dir():
            for ver in sorted(local.glob("versions/*"), reverse=True):
                cand = ver / ("node.exe" if os.name == "nt" else "node")
                if cand.exists():
                    return str(cand)
        return "node"


@dataclass(frozen=True)
class Candidate:
    """候选表的一行：(执行器, 模型, 权重, 优先级)。

    ★ **2026-11-04 用户令：不再是"配比"，改成"优先级瀑布"**——
    原话：「先全部排 devin，你得测它并发，它排满了或额度用完了，就落 opencode，
    opencode 不行，再落 workbuddy」。
    ⇒ **``priority`` 越小越优先**；表里没写 ``priority`` 时**按行序**（0,1,2…），
    也就是**表的第一行就是最高优先级**。
    ⇒ ``weight`` 保留：老的"平滑加权轮询"模式仍可用（``Picker``），
      但**优先级模式下不再用它分流**（它只用来标记"备用端点"的老语义）。
    """

    executor: str
    model: str
    weight: int
    priority: int = 0


@dataclass(frozen=True)
class Guards:
    permission_mode: str = "dontAsk"
    #: **CLI 进程池用的权限模式**（2026-10-23 21:05 实测）：
    #: ``dontAsk`` 下 CLI 的 **Write / Bash / PowerShell 全被拒**（"需要审批"）
    #: ⇒ agent 写不了文件 ⇒ 任务做不成。
    #:
    #: **★ 2026-10-24 实测再更正**：``acceptEdits`` 也**只自动放行"文件编辑"**，
    #: **Bash / PowerShell 仍然要人工审批** ⇒ agent 一跑命令就进
    #: ``runStatus=waiting_for_permission`` **干等** ⇒ 那个 agent 槽被永久占住
    #: ⇒ 后面的卡全卡死（现场：card_012 / card_014 就是这么挂住的）。
    #: ⇒ CLI 侧默认改 ``bypassPermissions``（命令直接放行）。
    #: **只影响我们自己起的 CLI 进程**（cwd＝给这张卡的工作目录）。
    cli_permission_mode: str = "bypassPermissions"
    sandbox: bool = True
    context_window: int = 300000


@dataclass(frozen=True)
class Watchdog:
    """委派回合的**传输层兜底**（**不含任何业务时间闸**）。

    2026-10-23 重写：早先这里是三套时间阈值（网关心跳 90s / prompt 心跳 150s /
    业务事件 240s）＋ rpc_timeout_sec=300 —— 那是**拿时间猜死活**，
    猜错就误杀正在干活的长任务（实测 615.9 秒的正常任务被判"卡住"），
    更糟的是猜错"未登录"还触发全局熔断，把整条通道打瘫。

    ⇒ 现在**只按状态判断**（见 ``watchdog.py`` 的状态表）；
    这里只留两个**纯传输层**的超时：
    - ``connect_timeout_sec``：建连（含首次 ``initialize``）；
    - ``read_timeout_sec``：**必须显著大于 WorkBuddy 的 60 秒 keepalive 间隔**，
      否则"正在思考/跑大脚本"会被误判成"断了"。它是**兜底**，不是判据。
    """

    connect_timeout_sec: float = 30.0
    #: **普通 RPC 的超时**（session/new、session/load、set_config_option 这些快操作）。
    #: ★ **prompt 那条长任务不再有读超时**（2026-11-03 改）⇒ 判死活只靠
    #: **状态与连接事件**（agentPhase / 在途工具 / 吐字 / SSE 结束 / httpx 异常）。
    #: **为什么**：ACP 的 ``session/prompt`` 响应本身就是 **SSE 流**，httpx 的
    #: read timeout 判的是「**两帧之间**多久没数据」——而 agent 跑一个大 bash 时
    #: **本来就不发帧**（连 keepalive 都没有）⇒ 旧的 600s 超时把**正在干活的长任务**
    #: 误判成「连接断了」⇒ 续跑（重发整段 prompt）⇒ 又超时 ⇒ 最多 max_resume 次
    #: ⇒ **30 分钟没动静**（观感＝卡死）。实测（work_027 全量轮次）：wb-cn/hy3
    #: 总耗时**中位 373.8s、最大 3351s**、wb-intl/hy3 中位 901.4s ⇒ hy3 的活儿
    #: 天然超过 600s（换 hy3 之前主流 deepseek、中位 429s ⇒ 触发率低
    #: ⇒ 换完立刻「卡死变多」）。⇒ **process 死了连接一定断** ⇒ httpx 抛异常
    #: ⇒ 那才是真死。
    read_timeout_sec: float = 600.0
    max_resume: int = 3
    #: 一个网关**忙**时的等待上限（秒）——**按 busy 状态排队等**，
    #: 这是"排队"的上限，不是"猜死活"。
    busy_wait_sec: float = 900.0


@dataclass(frozen=True)
class TaskSpec:
    segment_marker: str = "【推演agent 任务卡 {card_id} · 段 {seg}/{total} · run={run_id}】"
    auto_segment: bool = False


@dataclass(frozen=True)
class ScopeSpec:
    """开闸范围（爆炸半径控制）。

    ``all``     —— 所有 coder 卡走 WorkBuddy（缺省即委派）
    ``opt_in``  —— **只有显式把该卡 ``exec_backend`` 设成 ``auto`` 的卡**才走 WorkBuddy；
                    存量卡默认留在系统自己的模型上，**逐张接管**（开闸零冲击）
    """

    mode: str = "all"


@dataclass(frozen=True)
class WbExecConfig:
    enabled: bool = False
    executors: tuple[Executor, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    fallback_models: tuple[str, ...] = ("hy3",)
    prefer_same_executor: bool = True
    #: ★★ **哪些卡型走这套路由**（2026-11-04 用户令：「验收卡、pm卡，本质上应该和
    #: coder卡用一套路由机制，而不是分开的」）。
    #: ⇒ **白名单进配置**（``wb_exec.json`` 的 ``delegable_card_types``）⇒
    #:   后端判定、接口返回、**前端渲染门控**共用**同一份** ⇒ 不再各写一套卡型。
    #: 默认 = coder（编程卡）＋ qa_card（验收卡）＋ pm_card（执笔卡）。
    delegable_card_types: tuple[str, ...] = ("coder", "qa_card", "pm_card")
    #: **备用端点**（候选表里 ``weight == 0`` 的那些）——
    #: ★ 2026-11-03 用户令：「hy3 free 额度用完 ⇒ 全部换收费的两端 glm-5.3-flash」。
    #: ⇒ 语义：**平时不吃流量**（权重 0）；等原候选的额度用完时，
    #:   **份额转移**给它（`picker.set_weight`）⇒ 后续所有卡自动走它。
    guards: Guards = field(default_factory=Guards)
    watchdog: Watchdog = field(default_factory=Watchdog)
    on_unavailable: str = "fallback_system"
    task: TaskSpec = field(default_factory=TaskSpec)
    scope: ScopeSpec = field(default_factory=ScopeSpec)
    #: **每个执行体同时跑几个任务**（默认 1＝严格串行）。
    #: 桌面端网关是**单 agent 进程**（实测：同版本并发发两个请求，
    #: 后到的那个会被堵到超时 ⇒ 同一版本必须排队）；**跨版本是两个独立网关
    #: 进程 ⇒ 可以真并行**。调大它只有在网关能同时跑多个 agent 时才有意义。
    per_executor_concurrency: int = 1

    def executor(self, executor_id: str) -> Optional[Executor]:
        for ex in self.executors:
            if ex.id == executor_id:
                return ex
        return None

    def candidate(self, executor_id: str, model: str) -> Optional[Candidate]:
        for c in self.candidates:
            if c.executor == executor_id and c.model == model:
                return c
        return None


_EMPTY = WbExecConfig()

_cache: Optional[WbExecConfig] = None


def config_path() -> Path:
    override = os.environ.get(ENV_CONFIG_PATH, "").strip()
    if override:
        return Path(override)
    return CONFIG_PATH


def load(force: bool = False) -> WbExecConfig:
    """读配置（带进程内缓存）。**任何异常都返回"关闭态"，不抛。**"""
    global _cache
    if _cache is not None and not force:
        return _cache
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("[wb_exec] 配置不存在（%s）⇒ 执行体旁路", path)
        _cache = _EMPTY
        return _cache
    except Exception as exc:  # noqa: BLE001 配置坏了不拖垮主链路
        logger.warning("[wb_exec] 配置读取失败（%s）⇒ 执行体旁路: %s", path, exc)
        _cache = _EMPTY
        return _cache
    try:
        _cache = _parse(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 配置解析失败 ⇒ 执行体旁路: %s", exc)
        _cache = _EMPTY
    return _cache


def reset_cache() -> None:
    """测试用：清缓存。"""
    global _cache
    _cache = None


def _parse(raw: dict[str, Any]) -> WbExecConfig:
    executors = tuple(
        Executor(
            id=str(e.get("id") or ""),
            label=str(e.get("label") or e.get("id") or ""),
            install_root=str(e.get("install_root") or ""),
            product_config=str(e.get("product_config") or ""),
            cli_bin=str(e.get("cli_bin") or ""),
            node_bin=str(e.get("node_bin") or ""),
            port=int(e.get("port") or 0),
            config_dir=str(e.get("config_dir") or ""),
            sessions_dir=str(e.get("sessions_dir") or ""),
            base_url=str(e.get("base_url") or ""),
            require_desktop_owner=bool(e.get("require_desktop_owner", True)),
            desktop_procs=tuple(
                str(p) for p in (e.get("desktop_procs")
                                 or ("WorkBuddy.exe", "WorkBuddyAI.exe"))
                if str(p)),
            cli_exe=str(e.get("cli_exe") or ""),
            cli_config_dir=str(e.get("cli_config_dir") or e.get("config_dir") or ""),
            cli_pool_size=max(0, int(e.get("cli_pool_size") or 0)),
            cli_probe_model=str(e.get("cli_probe_model") or "hy3"),
            cli_wsl=(dict(e["cli_wsl"]) if isinstance(e.get("cli_wsl"), dict)
                     and e.get("cli_wsl") else None),
            cli_home=str(e.get("cli_home") or ""),
            oc_serve_exe=str(e.get("oc_serve_exe") or ""),
            oc_pool_size=max(0, int(e.get("oc_pool_size") or 0)),
            oc_base_url=str(e.get("oc_base_url") or ""),
            oc_api_key_env=str(e.get("oc_api_key_env") or ""),
            oc_api_key_envs=tuple(
                str(x) for x in (e.get("oc_api_key_envs") or ()) if str(x)),
            oc_provider_id=str(e.get("oc_provider_id") or "wb-direct"),
            oc_use_builtin_provider=bool(e.get("oc_use_builtin_provider") or False),
            oc_models=tuple(str(m) for m in (e.get("oc_models") or ()) if str(m)),
            oc_user_agent=str(e.get("oc_user_agent") or ""),
            # -------------------------------------------------------- Devin
            devin_cli_exe=str(e.get("devin_cli_exe") or ""),
            devin_model=str(e.get("devin_model") or "swe-2"),
            devin_permission_mode=str(e.get("devin_permission_mode")
                                     or "bypass"),
            devin_cloud=bool(e.get("devin_cloud", True)),
            devin_max_concurrent=max(0, int(e.get("devin_max_concurrent") or 0)),
            devin_timeout_sec=float(e.get("devin_timeout_sec") or 3600.0),
            devin_org_id_env=str(e.get("devin_org_id_env") or ""),
            devin_api_key_env=str(e.get("devin_api_key_env") or ""),
            devin_base_url=str(e.get("devin_base_url")
                              or "https://api.devin.ai/v3"),
            # ★ 会话清理：默认关 ⇒ **只有显式开的执行体才清**（红线）。
            cleanup_after_run=bool(e.get("cleanup_after_run") or False),
            # ------------------------------------------- Command Code（第五路）
            cc_cli_entry=str(e.get("cc_cli_entry") or ""),
            cc_node_exe=str(e.get("cc_node_exe") or "node"),
            cc_api_key_env=str(e.get("cc_api_key_env")
                               or "COMMAND_CODE_API_KEY"),
            cc_models=tuple(str(m) for m in (e.get("cc_models") or ()) if str(m)),
            cc_max_concurrent=max(0, int(e.get("cc_max_concurrent") or 0)),
            cc_timeout_sec=float(e.get("cc_timeout_sec") or 3600.0),
            cc_max_turns=max(1, int(e.get("cc_max_turns") or 8)),
        )
        for e in (raw.get("executors") or [])
        if isinstance(e, dict) and str(e.get("id") or "")
    )
    candidates = tuple(
        Candidate(
            executor=str(c.get("executor") or ""),
            model=str(c.get("model") or ""),
            weight=int(c.get("weight") or 0),
            # ★ **优先级**：写了就用写的；没写就按**表里的行序**（行序＝优先级序）。
            priority=int(c["priority"]) if str(c.get("priority") or "") != ""
            else _idx,
        )
        for _idx, c in enumerate(raw.get("candidates") or [])
        if isinstance(c, dict) and str(c.get("executor") or "")
        and str(c.get("model") or "") and int(c.get("weight") or 0) >= 0
    )
    fb = raw.get("fallback") or {}
    guards = raw.get("guards") or {}
    wd = raw.get("watchdog") or {}
    task = raw.get("task") or {}
    on_unavailable = raw.get("on_unavailable") or {}
    scope = raw.get("scope") or {}
    return WbExecConfig(
        enabled=bool(raw.get("enabled")),
        executors=executors,
        candidates=candidates,
        fallback_models=tuple(str(m) for m in (fb.get("models") or ["hy3"]) if str(m)),
        prefer_same_executor=bool(fb.get("prefer_same_executor", True)),
        # ★ 可委派卡型白名单（配置化 ⇒ 前后端同一份事实源）。
        delegable_card_types=tuple(
            str(t) for t in (raw.get("delegable_card_types")
                             or ("coder", "qa_card", "pm_card")) if str(t)),
        guards=Guards(
            permission_mode=str(guards.get("permission_mode") or "dontAsk"),
            cli_permission_mode=str(guards.get("cli_permission_mode")
                                    or "bypassPermissions"),
            sandbox=bool(guards.get("sandbox", True)),
            context_window=int(guards.get("context_window") or 300000),
        ),
        watchdog=Watchdog(
            connect_timeout_sec=float(wd.get("connect_timeout_sec") or 30.0),
            read_timeout_sec=float(wd.get("read_timeout_sec") or 600.0),
            max_resume=int(wd.get("max_resume") or 3),
            busy_wait_sec=float(wd.get("busy_wait_sec") or 900.0),
        ),
        on_unavailable=str(on_unavailable.get("mode") or "fallback_system"),
        task=TaskSpec(
            segment_marker=str(task.get("segment_marker")
                               or TaskSpec.segment_marker),
            auto_segment=bool(task.get("auto_segment", False)),
        ),
        scope=ScopeSpec(mode=str(scope.get("mode") or "all"),
                        ),
        per_executor_concurrency=max(
            1, int((raw.get("concurrency") or {}).get("per_executor") or 1)),
    )
