# -*- coding: utf-8 -*-
"""网关管理：**接管桌面端已经跑起来的 WorkBuddy 网关**（不再自己起）。

**为什么改成"接管"（2026-10-23 实测推翻的旧结论）**：
旧实现是自己 spawn 一个 ``codebuddy --serve`` 网关。实测（两版都试过）：

- 带桌面端**原样 env**（含 ``CODEBUDDY_CONFIG_DIR``）自建网关 ⇒ 网关能起来、
  ``session/new`` 能返回**完整模型目录**，但真正发 prompt 时报
  ``Authentication required. Please use /login command`` ⇒
  CLI 日志里写得很直白::

      [FileAuthenticationStorage] [AtRestEncryption] unavailable
        adapter=credential-fields resource=auth/workbuddy-desktop.info
        operation=read category=missing-key role=cbc; continuing
      [AuthenticationManager] [AuthDoInitProbe] hasSession=true hasAccessToken=false

  ⇒ **登录态的解密密钥（protectorKey）只由桌面端进程持有**，不在磁盘上、
  也不在 env 里 ⇒ **自建网关拿不到登录态**，必然 auth 失败。
  （自建网关的 prompt 会以 ``stopReason: "refusal"`` 返回，
  ``category=auth`` / ``rpcCode=-32000`` ⇒ 表面像"模型拒绝"，其实是鉴权失败。）

- 而**桌面端自己起的网关是登录态完好的**（国内版 sidecar / 国际版 prewarm），
  实测能真出活：``session/new`` → 切模型 → ``dontAsk`` → ``session/prompt``
  ⇒ ``stopReason: "end_turn"`` + 正文。

⇒ 结论：**执行体 = 桌面端在跑的网关**。推演agent 只负责发现它、连上去、
建自己的会话、下任务。

**怎么发现**（端口每次桌面端重启都会变 ⇒ 不能写死）：
1. 配置里显式给 ``base_url``（测试/特例）。
2. 读该版本数据根下的 ``sessions/<pid>.json``——桌面端给每个 codebuddy
   子进程写的登记文件，里面有 ``pid`` / ``url`` / ``sessionId`` / ``cwd``。
3. 没有 ``url`` 的条目（国际版 prewarm 就没有）⇒ 按 ``pid`` 反查它监听的端口。
4. 兜底：扫本机监听端口，逐个探 ``/api/v1/health``（**401 也算"这是网关"**——
   桌面端网关的 REST 端点要口令，而 ACP 端点不要）。

**连接要复用**：ACP 的 ``acp/connect`` 建出来的连接**没有 disconnect 端点**
（只有 ``/api/v1/acp`` 与 ``/api/v1/acp/connect``），且网关侧
``maxConnections=32`` / ``tokenTtlMs=24h`` ⇒ 每张卡新建连接迟早撞上限。
⇒ 连接（connectionId + sessionToken）存在注册表里**跨委派复用**，
每次委派只新建一个轻量的 :class:`AcpClient`（避免并发时
``on_notification`` 回调互相覆盖）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import acp_client
from . import cli_pool
from . import oc_client
from . import oc_pool
from .acp_client import AcpClient
from .config import Executor, load as load_config
from .winenv import gateway_password as _proc_gateway_password

logger = logging.getLogger("api.wb_exec.registry")

#: 发现网关时的 HTTP 探测超时（秒）。
PROBE_TIMEOUT_SEC = 3.0
#: 扫端口兜底时，最多探多少个端口（防呆）。
MAX_SCAN_PORTS = 200
#: **凭据探针用的固定会话 id**（2026-10-24）——
#: 固定 ⇒ 每个端口最多留一个探针会话（早先随机 uuid ⇒ 越探越多）。
PROBE_SESSION_ID = "probe-wb-exec"


# ===================================================================== 网关句柄
@dataclass
class GatewayProcess:
    """一个可用的 WorkBuddy 网关（**进程外**的，桌面端托管）。"""

    executor: Executor
    port: int = 0
    password: str = ""
    #: 显式 URL（发现到的）。为空则按 port 拼。
    base_url_override: str = ""
    #: ACP 连接凭据（**跨委派复用** ⇒ 不撞 maxConnections=32）。
    connection_id: str = ""
    session_token: str = ""
    #: 这条连接是否已经 initialize 过。
    initialized: bool = False
    #: 发现方式（诊断用）：config / session-file / pid-port / scan
    source: str = ""
    last_error: str = ""
    proc: Optional[subprocess.Popen] = None   # 恒为 None（桌面端托管）
    #: 这个网关**由哪个进程**监听（发现时拿到的 pid）。
    owner_pid: int = 0
    #: **探针实测：有凭据吗**？（用 session/load 探一次，不调模型、不花钱）
    #: ——**不能靠"登记文件里有没有 url"判断**（实测：9 个候选里 8 个没凭据，
    #: 唯一有凭据的那个反而是没有 url 的）。凭据是"某个进程启动那一刻加载到、
    #: 自己内存里持有"的 ⇒ 重建后的新进程加载不到。
    credentialed: bool = False
    #: **CLI 进程**（我们自己起的完整版 CLI ⇒ 一进程一 agent 槽 ⇒ 真并行）。
    cli_worker: Optional[object] = None
    #: **opencode 模式**：会话 id（**一卡一目录**：``?directory=该卡工作目录``）。
    session_id: str = ""
    #: **opencode 模式**：该 server 的 HTTP 客户端。
    client: Optional[Any] = None

    @property
    def base_url(self) -> str:
        if self.base_url_override:
            return self.base_url_override.rstrip("/")
        return f"http://127.0.0.1:{self.port}"

    def headers(self) -> dict[str, str]:
        """ACP 端点**不需要**口令（桌面端的 REST 端点才要）⇒ 这里只带必带头。"""
        return {acp_client.HEADER_REQUEST: acp_client.HEADER_VALUE}


# ===================================================================== 端口与进程（诊断用）
def listening() -> list[tuple[int, int]]:
    """本机所有监听端口 ＋ 占用它的 pid（``[(port, pid), …]``）。"""
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[int, int]] = []
    for line in (r.stdout or "").splitlines():
        m = _NETSTAT_RE.match(line)
        if m:
            out.append((int(m.group(2)), int(m.group(3))))
    return out


def listening_ports_by_pid(pid: int) -> list[int]:
    """某进程监听的端口。"""
    return [p for p, own in listening() if own == pid]


# ===================================================================== 进程归属
_proc_table_cache: tuple[float, dict[int, tuple[int, str]]] = (0.0, {})


def process_table(max_age_sec: float = 20.0) -> dict[int, tuple[int, str]]:
    """``{pid: (父pid, 进程名)}``（Windows；读不到就返回空表）。

    **为什么需要它**：``<config_dir>/sessions/*.json`` 里**谁**都可能留下条目
    （自己起的 CLI 也会写同名文件）⇒ 必须确认那个 pid **确实是桌面端起的**，
    否则会连上一个没登录的野网关（实测踩到：残留的自建网关抢在桌面端前面被选中
    ⇒ ``Authentication required``）。
    """
    global _proc_table_cache
    now = time.time()
    ts, table = _proc_table_cache
    if now - ts < max_age_sec:
        return table
    out: dict[int, tuple[int, str]] = {}
    try:
        ps = (
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.Name)\" }"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            out[pid] = (ppid, parts[2].strip())
    except Exception as exc:  # noqa: BLE001 读不到 ⇒ 不过滤（宁可用错也别不用）
        logger.debug("[wb_exec] 读进程表失败（不过滤归属）: %s", exc)
        return {}
    _proc_table_cache = (now, out)
    return out


def pid_alive(pid: int, table: Optional[dict] = None) -> bool:
    tbl = process_table() if table is None else table
    if not tbl:
        return True
    return pid in tbl


#: 探针判"**没凭据**"的文本关键词（与 acp_client 的鉴权判定同源）。
AUTH_TEXT_HINTS = ("authentication required", "authsession", "auth_required",
                   "not logged in", "please use /login",
                   "sign in to your account")


async def probe_gateway(base_url: str,
                        timeout: float = PROBE_TIMEOUT_SEC) -> bool:
    """探"这个端口是不是 WorkBuddy 网关"。

    **401 也算"是"**：桌面端网关的 REST 端点要口令（没口令返 401），
    而 ACP 端点不要口令。⇒ 判据是"这个端口有个 WorkBuddy 服务在应答"，
    不是"健康检查通过"。
    """
    import httpx
    url = f"{base_url.rstrip('/')}/api/v1/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url, headers={acp_client.HEADER_REQUEST: acp_client.HEADER_VALUE})
        return resp.status_code in (200, 401)
    except Exception:  # noqa: BLE001 连不上 ⇒ 不是网关
        return False


# ===================================================================== 注册表
class ExecutorRegistry:
    """执行器 ↔ 网关的注册表（进程内单例）。

    **不跨 work 隔离**：网关是机器级资源（一个版本一个），
    多个 work 的 coder 卡共享同一批网关（这正是"充分用额度"想要的）。
    """

    def __init__(self) -> None:
        self._gateways: dict[str, GatewayProcess] = {}
        #: **账号未登录**的执行体（进程内记忆）。
        #: 桌面端登录态失效时，重连多少次都没用 ⇒ 第一次发现就记下来，
        #: 之后该执行体的卡**直接秒级回落**，不再发 prompt。
        self._unauthenticated: set[str] = set()
        #: 鉴权失败计数（**只做诊断**；不做全局熔断——故障要局部化）。
        self._auth_failures: dict[str, int] = {}
        #: **建连接的锁**（每执行体一把）——
        #: 并发派发时两个任务同时进来会各建一条连接（ACP 连接上限 32，
        #: 且两个连接会互相覆盖 connection_id）⇒ 必须串行化"首次建连"。
        self._conn_locks: dict[str, tuple[Any, asyncio.Lock]] = {}
        #: **每执行体的并发闸**（默认 1）——
        #: 桌面端网关是**单 agent 进程**（实测：同一版本并发发两个请求，
        #: 后到的那个会被堵到超时，见 §十五 的并发实测）⇒ 同一版本的任务必须**排队**，
        #: 否则第二个会挂死。跨版本是两个独立网关进程 ⇒ **可以真并行**。
        self._exec_sems: dict[str, tuple[Any, asyncio.Semaphore]] = {}
        #: **按网关实例**的并发闸（每个网关 1 路；跨网关才并行）。
        self._gw_sems: dict[str, tuple[Any, asyncio.Semaphore]] = {}
        #: **槽位占用表**（``{base_url: holder}``）——"这个网关/CLI 进程这会儿有人用"。
        #:
        #: **★ 为什么不用信号量排队**（2026-10-24 实跑抓到的死锁）：
        #: 早先 ``semaphore_for(base_url)`` 是 ``asyncio.Semaphore(1)``，
        #: 后来的任务**无限等在信号量上** ⇒ 一张卡可以**永久挂死**
        #: （现场取证：``card_019.jsonl`` 只有 ``wb_exec_start``，后续事件永不到）。
        #: ⇒ 现在改成**占/不占两种状态**：占不到 ⇒ 立刻换候选，**不排队**。
        self._slots: dict[str, str] = {}
        #: **opencode server 进程池**（``{pool_key: [OcServer, …]}``）——
        #: **一进程一槽**（与 WorkBuddy 的 CLI 池同构 ⇒ 进程级隔离：一个卡卡死
        #: 不影响别的卡）。
        self._oc_pool: dict[tuple, list] = {}
        self._oc_cursor: dict[tuple, int] = {}
        self._oc_locks: dict[tuple, tuple[Any, asyncio.Lock]] = {}
        #: **opencode 配置文件的路径缓存**（``{(base_url, key_env): path}``）——
        #: 里面含 apiKey ⇒ 只放临时目录、不进仓库。
        self._oc_cfg_paths: dict[tuple, str] = {}
        #: **每个执行体的模型目录缓存**（``{executor_id: {modelId, …}}``）——
        #: **只是 ``session/load`` 返回的本地列表（被过滤过）**，只作诊断展示，
        #: **不能用来判断"这个模型有没有"**（见 :meth:`model_unavailable`）。
        self._catalog: dict[str, set[str]] = {}
        #: **实测不可用的模型**（``{executor_id: {modelId, …}}``）——
        #: 真调失败报 "Model unavailable" 才记 ⇒ **这是唯一可信的判据**。
        self._model_unavailable: dict[str, set[str]] = {}
        #: **CLI 池的模型目录**（``{pool_key: [model…]}``）——
        #: 用来判断"这个模型在 CLI 产品里**免费吗**"（看 credits 字段）。
        self._cli_catalog: dict[tuple, list[dict]] = {}
        #: **CLI 进程池**（``{pool_key: [GatewayProcess, …]}``）——
        #: 我们自己起的完整版 CLI ⇒ 一个进程一个 agent 槽 ⇒ **真并行**。
        self._cli_pool: dict[tuple, list[GatewayProcess]] = {}
        self._cli_cursor: dict[tuple, int] = {}
        self._cli_locks: dict[tuple, tuple[Any, asyncio.Lock]] = {}
        #: **"这个执行体的 CLI 用不了"**（没登录/起不来）⇒ 不再反复起进程探。
        #: **按状态失效**（用户登录后调 :meth:`note_cli_state_changed`），不用时间闸。
        self._cli_unusable: set[str] = set()

    def remember_models(self, executor_id: str, models: Any) -> None:
        """记下某执行体的模型目录（``models`` 是 modelId 列表）。

        **⚠ 这个目录是"被过滤过的本地列表"，不是该版本的全部模型**（实测 2026-10-23
        20:45：国版只返回 5 个，可 ``hy3`` / ``step-5-preview`` 实测都能调）⇒
        **不能拿它判断"这个模型有没有"**。只作诊断展示用。
        """
        ids = {str(m) for m in (models or []) if str(m)}
        if ids:
            self._catalog[executor_id] = ids

    def note_model_unavailable(self, executor_id: str, model: str) -> None:
        """**实测**这个执行体没有这个模型 ⇒ 记下来（之后不再选它）。

        **按状态判定**（不是猜、不是时间闸）：只有真调失败并回报
        "Model unavailable" 才记。
        """
        self._model_unavailable.setdefault(executor_id, set()).add(str(model))
        logger.info("[wb_exec] 执行体 %s 实测没有模型 %s ⇒ 记下（不再选它）",
                    executor_id, model)

    def model_unavailable(self, executor_id: str, model: str) -> bool:
        """这个执行体**实测**没有这个模型吗？（**没实测过 ⇒ False**，不猜）

        **⚠ 早先这里拿 ``session/load`` 的 ``availableModels`` 当目录** ⇒
        那是**被过滤过的本地列表** ⇒ 国版的 hy3 / step-5-preview 被误判成"没有"
        ⇒ 国版那 30% 权重全被跳过。**现在只认实测证据。**
        """
        return str(model) in self._model_unavailable.get(executor_id, set())

    # ---------------------------------------------------------------- 槽位
    def semaphore_for(self, base_url: str) -> asyncio.Semaphore:
        """这个网关（= CLI 池进程）的**并发闸**：一个 agent 槽一次只放行一个。

        **★ 槽满 ⇒ 排队等**（用户令 2026-10-24：「槽满就他妈排队」）。
        早先这里是"占不到就抛异常 ⇒ 换候选 ⇒ 回落系统模型" ⇒ **等于花钱**，
        用户不接受 ⇒ 改回**排队**（在 WorkBuddy 侧等，不花钱）。
        排队是**可见的**：bridge 在等之前 emit「排队中」。
        """
        key = self._slot_key(base_url)
        loop = asyncio.get_running_loop()
        ent = self._gw_sems.get(key)
        if ent is None or ent[0] is not loop:
            sem = asyncio.Semaphore(1)
            self._gw_sems[key] = (loop, sem)
            return sem
        return ent[1]
    # ---------------------------------------------------------------- 槽位
    @staticmethod
    def _slot_key(base_url: str) -> str:
        return (base_url or "").rstrip("/")

    def claim_slot(self, base_url: str, holder: str) -> bool:
        """占这个网关的 agent 槽。**占不到就 False —— 不等待、不排队。**

        **这是"能不能干活"的唯一判据**：槽位的占用状态是我们**自己持有的状态**，
        不是拿时间猜（用户纪律：判定一律用状态）。

        同一个 ``holder`` 重复占同一个槽 ⇒ 幂等成功（重入不算抢）。
        """
        key = self._slot_key(base_url)
        if not key:
            return False
        cur = self._slots.get(key)
        if cur is not None and cur != holder:
            return False
        self._slots[key] = holder
        return True

    def mark_slot(self, base_url: str, holder: str) -> None:
        """登记"这个槽正被 holder 占用"（**排队拿到槽之后**调用）。

        与 :meth:`claim_slot` 的区别：``claim_slot`` 是"占不到就 False"
        （用于不排队的场景）；``mark_slot`` 是"**已经排到了**，登记一下"
        （让"这个槽有没有被占"的判断保持准）。
        """
        key = self._slot_key(base_url)
        if key:
            self._slots[key] = holder

    def release_slot(self, base_url: str, holder: str = "") -> None:
        """释放槽（**幂等**；``holder`` 不匹配时不动别人的槽）。"""
        key = self._slot_key(base_url)
        cur = self._slots.get(key)
        if cur is None:
            return
        if holder and cur != holder:
            return
        self._slots.pop(key, None)

    def slot_holder(self, base_url: str) -> str:
        """这个槽现在被谁占着（空 ⇒ ""）。"""
        return self._slots.get(self._slot_key(base_url), "")

    def slots_snapshot(self) -> dict[str, str]:
        """当前所有槽位的占用表（诊断用）。"""
        return dict(self._slots)

    def _conn_lock(self, executor_id: str) -> asyncio.Lock:
        """该执行体的建连锁（**按事件循环分代**：换 loop 就换一把）。"""
        loop = asyncio.get_running_loop()
        ent = self._conn_locks.get(executor_id)
        if ent is None or ent[0] is not loop:
            lock = asyncio.Lock()
            self._conn_locks[executor_id] = (loop, lock)
            return lock
        return ent[1]

    # ---------------------------------------------------------------- 账号态
    def mark_unauthenticated(self, executor_id: str) -> None:
        """记录一次鉴权失败（**只做诊断，不熔断**）。

        **为什么没有熔断**（2026-10-23 实跑的教训）：
        早先这里是"**整个执行体永久标死**"⇒
        ① 国际版 prewarm 进程被桌面端**回收重建**的窗口期发了一次 prompt ⇒
        一次真鉴权失败 ⇒ **之后所有卡全回落** ⇒ 整条 WorkBuddy 通道瘫痪，
        用户看着像"突然全都不干活了"；
        ② 再叠上"``-32000`` 就当鉴权失败"的误判 ⇒ 放大成整条通道死。

        ⇒ 正确的粒度是**故障局部化**：**这一张卡**回落，**别的卡照常**。
        这里只留一个计数，供诊断。
        """
        n = self._auth_failures.get(executor_id, 0) + 1
        self._auth_failures[executor_id] = n
        logger.warning("[wb_exec] 执行体 %s 鉴权失败（累计 %d 次）"
                       "——只影响这一张卡，不影响其它卡", executor_id, n)

    def is_unauthenticated(self, executor_id: str) -> bool:
        """**永远 False** —— 不做全局熔断（理由见 :meth:`mark_unauthenticated`）。"""
        return False

    def auth_failures(self, executor_id: str) -> int:
        """某执行体累计的鉴权失败次数（诊断用）。"""
        return int(self._auth_failures.get(executor_id, 0))

    def clear_unauthenticated(self, executor_id: str = "") -> None:
        """清掉标记（用户重新登录桌面端后可用这里手动清）。"""
        if executor_id:
            self._unauthenticated.discard(executor_id)
        else:
            self._unauthenticated.clear()

    # ---------------------------------------------------------------- 访问
    def gateway(self, executor_id: str) -> Optional[GatewayProcess]:
        return self._gateways.get(executor_id)

    def base_url(self, executor_id: str) -> Optional[str]:
        gw = self._gateways.get(executor_id)
        return gw.base_url if gw else None

    def headers(self, executor_id: str) -> dict[str, str]:
        gw = self._gateways.get(executor_id)
        if gw is None:
            return {acp_client.HEADER_REQUEST: acp_client.HEADER_VALUE}
        return gw.headers()

    def is_running(self, executor_id: str) -> bool:
        """该执行体是否有可用网关（**已发现**就算"在跑"）。"""
        return executor_id in self._gateways

    # ---------------------------------------------------------------- 发现
    # ---------------------------------------------------------------- 连接
    async def ensure_connection(self, gw: GatewayProcess) -> GatewayProcess:
        """确保这条网关连接可用（**只建一次**，之后跨委派复用）。

        **并发安全**：第一次建连走该执行体的锁 ⇒ 并发派发时只会建**一条**连接
        （ACP 端点没有 disconnect、网关侧 maxConnections=32 ⇒ 不能浪费）。
        """
        async with self._conn_lock(gw.executor.id):
            if gw.connection_id and gw.session_token:
                if await probe_gateway(gw.base_url):
                    return gw
                # 连接可能已过期 ⇒ 重连
                gw.connection_id = ""
                gw.session_token = ""
                gw.initialized = False
            client = AcpClient(gw.base_url, timeout=30.0,
                               extra_headers=gw.headers())
            try:
                await client.connect()
                await client.initialize()
            finally:
                await client.close()
            gw.connection_id = client.connection_id
            gw.session_token = client.session_token
            gw.initialized = True
            logger.info("[wb_exec] ACP 连接就绪 %s conn=%s",
                        gw.executor.id, gw.connection_id)
            return gw

    # ---------------------------------------------------------------- 取网关
    async def acquire(self, executor_id: str, work_dir: str = "",
                      model: str = "") -> GatewayProcess:
        """取这个执行体**原生 CLI 进程池**里一个**空闲**的 agent 槽。

        **★★ 2026-10-24 用户令：「确保它一定跑在原生 CLI 上」**
        ⇒ **桌面端网关这条路整条停用**：不回落、不发现、不探凭据、不碰客户端。

        早先为什么会跑进客户端（实测取证）：
        - "这个模型在 CLI 里要花钱（deepseek 是 x0.11）⇒ 按规矩走它自己的
          桌面端网关" ⇒ 任务**跑进用户的 WorkBuddy 客户端对话**；
        - 而且桌面端**派生的 cli 子进程进程名也是 WorkBuddy*.exe**
          ⇒ 只按进程名筛根本分不出来 ⇒ 探针还在它上面建出 ``probe-*``
          会话 ⇒ **污染用户自己的客户端**。

        ⇒ 现在：只有 CLI 池这一条路。拿不到空闲槽 ⇒ 抛错
          （上层**换候选 / 回落系统模型**）⇒ **不排队、不空等**。

        **★ opencode 执行体**（2026-10-25 加）：``oc_serve_exe`` 配了 ⇒ 走
        **opencode serve**（headless HTTP）那条路 ⇒ 见 :meth:`_acquire_oc`。
        """
        cfg = load_config()
        ex = cfg.executor(executor_id)
        if ex is not None and ex.oc_serve_exe and ex.oc_pool_size > 0:
            gw = await self._acquire_oc(executor_id, work_dir, model)
            if gw is not None:
                return gw
        gw = await self._acquire_cli(executor_id, work_dir, model)
        if gw is None:
            raise RuntimeError(
                f"执行体 {executor_id} 的原生 CLI 池此刻没有空闲进程"
                f"（模型={model or '未指定'}；池大小见 cli_pool_size）")
        return gw

    # ---------------------------------------------------------------- opencode 池
    def _oc_lock(self, key: tuple) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        ent = self._oc_locks.get(key)
        if ent is None or ent[0] is not loop:
            lock = asyncio.Lock()
            self._oc_locks[key] = (loop, lock)
            return lock
        return ent[1]

    def _oc_config_path(self, ex: Executor) -> str:
        """opencode 的自定义 provider 配置（**含 apiKey ⇒ 临时目录，不进仓库**）。"""
        key = (ex.oc_base_url, ex.oc_api_key_env)
        path = self._oc_cfg_paths.get(key)
        if path and os.path.exists(path):
            return path
        api_key = ""
        if ex.oc_api_key_env:
            api_key = os.environ.get(ex.oc_api_key_env, "")
        import hashlib

        base = (Path(tempfile.gettempdir()) / "wb_exec_oc_cfg"
                / hashlib.md5(str(key).encode()).hexdigest()[:12])
        path = oc_pool.write_provider_config(
            str(base), provider_id=ex.oc_provider_id,
            base_url=ex.oc_base_url, api_key=api_key,
            models=list(ex.oc_models) or ["deepseek-v4.1-flash"],
            user_agent=ex.oc_user_agent or oc_pool.BROWSER_UA)
        self._oc_cfg_paths[key] = path
        return path

    async def _oc_servers(self, ex: Executor,
                           work_dir: str) -> list:
        """起/复用 **opencode serve 进程池**（进程数 = ``oc_pool_size`` = 槽数）。"""
        key = (ex.oc_serve_exe.lower(), ex.oc_provider_id, ex.oc_base_url)
        lock = self._oc_lock(key)
        async with lock:
            pool = self._oc_pool.get(key) or []
            alive = [s for s in pool if s.alive()]
            self._oc_pool[key] = alive
            if len(alive) < ex.oc_pool_size:
                cfg_path = self._oc_config_path(ex)
                while len(alive) < ex.oc_pool_size:
                    try:
                        srv = await oc_pool.start_oc_server(
                            ex.oc_serve_exe, cwd=work_dir, config_path=cfg_path)
                    except Exception as exc:  # noqa: BLE001 起不来就少几个槽
                        logger.warning("[wb_exec] 起 opencode server 失败: %s", exc)
                        break
                    alive.append(srv)
                self._oc_pool[key] = alive
            return alive

    async def _acquire_oc(self, executor_id: str, work_dir: str,
                          model: str = "", on_wait: Any = None
                          ) -> Optional[GatewayProcess]:
        """取一个 **opencode 槽**（一进程一槽；session 每次委派新建 ⇒ 一卡一目录）。

        **★ 槽满 ⇒ 排队等**（不是回落、不是换候选）—— 与 WorkBuddy 那套同语义。
        ``on_wait`` 回调用来 emit「排队中」事件（**让人看得见它在等**）。
        """
        cfg = load_config()
        ex = cfg.executor(executor_id)
        if ex is None or not ex.oc_serve_exe or ex.oc_pool_size <= 0:
            return None
        key = (ex.oc_serve_exe.lower(), ex.oc_provider_id, ex.oc_base_url)
        waited = False
        while True:
            servers = await self._oc_servers(ex, work_dir)
            if not servers:
                return None
            target = None
            for s in servers:
                if not getattr(s, "busy_session", ""):
                    target = s
                    break
            if target is None:
                if not waited and on_wait is not None:
                    try:
                        on_wait()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[wb_exec] 排队回调失败: %s", exc)
                    waited = True
                await asyncio.sleep(0.5)
                continue
            client = oc_client.OcClient(target.base_url)
            try:
                sid = await client.create_session(
                    work_dir, permission=oc_client.FULL_ALLOW_PERMISSION)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[wb_exec] 建 opencode session 失败: %s", exc)
                await client.close()
                await asyncio.sleep(0.5)
                continue
            target.busy_session = sid
            gw = GatewayProcess(
                executor=ex, port=0, base_url_override=target.base_url,
                source="opencode", credentialed=True)
            gw.session_id = sid
            gw.cli_worker = target
            gw.client = client
            logger.info("[wb_exec] opencode 槽就绪 session=%s server=%s",
                        sid, target.base_url)
            return gw

    # ---------------------------------------------------------------- CLI 池
    async def _acquire_cli(self, executor_id: str, work_dir: str,
                            model: str = "") -> Optional[GatewayProcess]:
        """从 **CLI 进程池**里取一个（没有 ⇒ None ⇒ 上层回落系统模型）。

        **★ 池满怎么办**：**照给一个**（上层在它的槽上**排队等**）。
        早先这里是"池满就返回 None ⇒ 换候选 ⇒ 回落系统模型" ⇒ **等于花钱**，
        用户不接受（2026-10-24：「槽满就他妈排队」）。
        """
        cfg = load_config()
        ex = cfg.executor(executor_id)
        if ex is None or not ex.cli_exe or ex.cli_pool_size <= 0:
            return None
        key = _pool_key(ex.cli_exe, ex.cli_config_dir or ex.config_dir, work_dir)
        pool = self._cli_pool.get(key) or []
        alive = [g for g in pool
                 if g.cli_worker is not None and g.cli_worker.alive()]
        if alive:
            self._cli_pool[key] = alive
            # **★ 不再看"这个模型在 CLI 里免费吗"**（2026-10-24 用户令：
            #   「确保它一定跑在原生 CLI 上」）。
            #   早先的规则是"CLI 里要花钱（deepseek 是 x0.11）⇒ 按规矩走它自己的
            #   桌面端网关" ⇒ **任务跑进了用户的 WorkBuddy 客户端对话**
            #   （实测 5700 的连接直指国际版桌面端派生进程，客户端里还多出
            #   `probe-*` 探针会话）⇒ 现在这条分流**整条删除**：
            #   **所有表内模型一律走原生 CLI 池**。
            #   （代价：deepseek 在国内版 CLI 里按 x0.11 计费 —— 用户已知悉并选择。）
            # **优先给"没被占"的**（少排一会儿队）；全被占 ⇒ **照给一个去排队**。
            idle = [g for g in alive if not self.slot_holder(g.base_url)]
            if idle:
                idx = self._cli_cursor.get(key, 0) % len(idle)
                self._cli_cursor[key] = idx + 1
                return idle[idx]
            idx = self._cli_cursor.get(key, 0) % len(alive)
            self._cli_cursor[key] = idx + 1
            logger.info("[wb_exec] %s CLI 池 %d 个进程全被占用 ⇒ 给一个去排队"
                        "（用户令：槽满就排队，不让路、不回落）",
                        executor_id, len(alive))
            return alive[idx]
        # **负面结论缓存**（2026-10-23 20:25）：CLI 没登录 ⇒ 探针必然不过 ⇒
        #   每次派发都去起进程＋探针是纯浪费。⇒ 记住"这个执行体的 CLI 不可用"，
        #   **按状态**失效（见 :meth:`note_cli_state_changed`），不用时间闸。
        if executor_id in self._cli_unusable:
            return None
        built = await self._grow_cli_pool(cfg, ex, key, work_dir)
        if not built:
            self._cli_unusable.add(executor_id)
            return None
        idle = [g for g in built if not self.slot_holder(g.base_url)]
        pick = idle or built          # 全被占 ⇒ 照给一个去排队
        idx = self._cli_cursor.get(key, 0) % len(pick)
        self._cli_cursor[key] = idx + 1
        return pick[idx]

    def has_free_slot(self, executor_id: str, model: str = "",
                      work_dir: str = "") -> bool:
        """这个执行体现在**还有没有空闲的 agent 槽**？

        **判定用状态**：槽位占用表（:meth:`claim_slot`）＋ CLI 池里活着的进程。
        * 不是"看有没有在跑"（那是表象），* 是"**这个槽有没有被占**"。

        **★ 只看原生 CLI 池**（2026-10-24 用户令：一定跑在原生 CLI 上
        ⇒ 桌面端网关这条路不再参与调度）。
        """
        cfg = load_config()
        ex = cfg.executor(executor_id)
        if ex is None:
            return False
        # ① CLI 池：有**没被占**的活进程（**不再看模型贵不贵** ⇒ 一律走池）
        if ex.cli_exe and ex.cli_pool_size > 0:
            key = _pool_key(ex.cli_exe, ex.cli_config_dir or ex.config_dir, work_dir)
            pool = self._cli_pool.get(key) or []
            alive = [g for g in pool
                     if g.cli_worker is not None and g.cli_worker.alive()]
            for g in alive:
                if not self.slot_holder(g.base_url):
                    return True
        return False

    def pick_free_candidate(self, candidates: Any, tried: set[tuple[str, str]],
                             work_dir: str = "") -> Optional[Any]:
        """挑一个**现在就有空闲槽**的候选（配比表内；**不换表外付费模型**）。

        没有 ⇒ 返回 ``None``（⇒ 上层回落系统自己的模型 ＋ 如实记账）。
        """
        for c in candidates or ():
            key = (getattr(c, "executor", ""), getattr(c, "model", ""))
            if key in tried:
                continue
            if self.has_free_slot(key[0], key[1], work_dir):
                return c
        return None

    def note_cli_state_changed(self, executor_id: str = "") -> None:
        """**CLI 状态变了**（比如用户刚登录）⇒ 清掉负面结论，下次派发重新探。"""
        if executor_id:
            self._cli_unusable.discard(executor_id)
        else:
            self._cli_unusable.clear()

    async def _grow_cli_pool(self, cfg: WbExecConfig, ex: Executor,
                             key: tuple, work_dir: str) -> list[GatewayProcess]:
        """起 CLI 进程池（**补到满编**），并**真发一次 prompt** 验它们能干活（老教训）。"""
        lock = self._cli_lock(key)
        async with lock:
            pool = self._cli_pool.get(key) or []
            alive = [g for g in pool
                     if g.cli_worker is not None and g.cli_worker.alive()]
            size = max(1, int(ex.cli_pool_size))
            need = size - len(alive)
            if need <= 0:
                self._cli_pool[key] = alive
                return alive
            out: list[GatewayProcess] = list(alive)
            for _i in range(need):
                try:
                    worker = await cli_pool.start_cli_worker(
                        ex.cli_exe, ex.cli_config_dir or ex.config_dir, work_dir,
                        wsl=ex.cli_wsl, home=ex.cli_home,
                        permission_mode=cfg.guards.cli_permission_mode)
                except Exception as exc:  # noqa: BLE001 起不来就停
                    logger.warning("[wb_exec] 起 CLI 进程失败（%s）：%s", ex.id, exc)
                    break
                gw = GatewayProcess(
                    executor=ex, port=worker.port,
                    base_url_override=worker.base_url,
                    source="cli",
                    owner_pid=worker.proc.pid if worker.proc else 0,
                    credentialed=True)
                gw.cli_worker = worker
                # **★ 验"能干活"**（2026-10-23 20:20）：起得来 ≠ 能调模型。
                #   探针用**表内免费模型**；探不过就把这个进程扔掉。
                ok, catalog = await cli_pool.probe_can_work(
                    worker.base_url, ex.cli_probe_model or "hy3")
                if not ok:
                    logger.warning(
                        "[wb_exec] CLI 进程 %s 探针不过（不能真调模型）⇒ 丢弃",
                        worker.base_url)
                    worker.stop()
                    break
                if catalog:
                    self._cli_catalog.setdefault(key, catalog)
                out.append(gw)
            self._cli_pool[key] = out
            logger.info("[wb_exec] %s CLI 池[%s]：可用进程 %d 个（cwd=%s）",
                        ex.id, Path(work_dir).name, len(out), work_dir)
            return out

    def _cli_lock(self, key: tuple) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        ent = self._cli_locks.get(key)
        if ent is None or ent[0] is not loop:
            lock = asyncio.Lock()
            self._cli_locks[key] = (loop, lock)
            return lock
        return ent[1]

    async def _status_payload(self, gw: GatewayProcess) -> dict:
        """读这个网关的 ``GET /api/v1/status``（读不到 ⇒ 空 dict ＝"不知道"）。"""
        import httpx
        headers = {acp_client.HEADER_REQUEST: acp_client.HEADER_VALUE}
        pwd = _proc_gateway_password(gw.owner_pid)
        if pwd:
            headers["Authorization"] = f"Bearer {pwd}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{gw.base_url}/api/v1/status",
                                        headers=headers)
            if resp.status_code != 200:
                return {}
            data = resp.json()
            data = data.get("data") if isinstance(data, dict) else None
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 读不到就是"不知道"
            return {}

    async def gateway_busy(self, gw: GatewayProcess) -> Optional[bool]:
        """这个网关**现在忙不忙**？（**问它自己的状态接口**，不猜）

        ``GET /api/v1/status`` ⇒ ``{busy, runStatus, activeSessionId, …}``
        （桌面端网关要口令 ⇒ 从它的进程 env 读）

        读不到 ⇒ 返回 ``None``（**不知道**，不是"不忙"）。
        """
        data = await self._status_payload(gw)
        if not data:
            return None
        if "busy" in data:
            return bool(data.get("busy"))
        rs = str(data.get("runStatus") or "")
        if rs:
            return rs != "idle"
        return None

    async def gateway_run_status(self, gw: GatewayProcess) -> str:
        """这个网关现在的**运行状态**（``idle`` / ``running`` /
        ``waiting_for_permission`` / …）；**读不到 ⇒ ""（不知道）**。

        **★ 这是"卡死"最关键的状态**（2026-10-24 实测）：
        ``runStatus == "waiting_for_permission"`` ⇒ **WorkBuddy 在等人工批准**
        ⇒ 没人会批 ⇒ 它**永远**不会自己继续 ⇒ 那个 agent 槽被**永久占住**
        ⇒ 后面的卡全卡死（现场：card_012 / card_014 的 CLI 进程就停在这个状态）。

        **判定完全用状态**，不拿时间猜。读不到 ⇒ 当作"不知道"⇒ 不处置。
        """
        data = await self._status_payload(gw)
        return str(data.get("runStatus") or "")

    # ---------------------------------------------------------------- 维护
    def stop_all(self) -> None:
        """清空注册表（**不杀桌面端的网关**——我们只是断开引用）。"""
        self._gateways.clear()

    def status(self) -> list[dict]:
        out = []
        for ex_id, gw in self._gateways.items():
            out.append({
                "executor": ex_id, "base_url": gw.base_url,
                "source": gw.source, "conn": gw.connection_id,
                "credentialed": gw.credentialed,
                "last_error": gw.last_error,
            })
        return out


def cfg_path_logs() -> str:
    """日志目录（WorkBuddy 执行体的排障日志放这儿）。"""
    from .config import REPO_ROOT
    return str(REPO_ROOT / "logs" / "wb_exec")


def _pool_key(cli_exe: str, cli_config_dir: str, work_dir: str) -> tuple:
    """池的键 ＝ (CLI 可执行文件, **CLI 配置目录**, work 目录)。

    **为什么要带配置目录**（2026-10-23 21:45）：两套 CLI 服务**各自登录各自的
    账号**（国内版 / 国际版）⇒ 凭据文件、模型目录、计价都不同 ⇒ **必须是两套池**，
    不能共用（共用就串账号了）。
    work 目录不同 ⇒ cwd 隔离 ⇒ 一卡一目录。
    """
    try:
        wd = os.path.normcase(os.path.abspath(work_dir))
    except Exception:  # noqa: BLE001
        wd = str(work_dir)
    return (str(cli_exe).lower(), str(cli_config_dir).lower(), wd)


def _port_of(url: str) -> int:
    try:
        return int(url.rsplit(":", 1)[1])
    except Exception:  # noqa: BLE001
        return 0


_registry: Optional[ExecutorRegistry] = None


def get_registry() -> ExecutorRegistry:
    global _registry
    if _registry is None:
        _registry = ExecutorRegistry()
    return _registry


def reset_registry() -> None:
    """测试用：清注册表。"""
    global _registry
    if _registry is not None:
        _registry.stop_all()
    _registry = None
