# -*- coding: utf-8 -*-
"""**CLI 进程池执行体**：自己起 N 个完整版 WorkBuddy CLI ⇒ 真并行。

**为什么要有这个**（2026-10-23 20:20 用户令："走 CLI 并发，不并发我要它有什么用"）：

- 桌面端托管的那**一个**网关：实测 ACP 入口**一次只放行一个在途请求**
  ⇒ 同网关并发只会 1 成功、其余堵死 ⇒ 并发度 1。
- 而**独立 CLI 进程**：一个进程 = 一个 agent 槽 ⇒ 起 N 个就是 N 路真并行，
  且每个进程的 **cwd 由我们定**（= 那个 work 目录 ⇒ 真"一卡一目录"），
  桌面端关不关都不影响。

**前置条件（实测得来的硬约束）**：独立 CLI 必须**自己登录过**——
- 桌面端进程那份 env 复制过来**没用**（实测 14 个桌面端进程的 env 全试过，
  全部 `Authentication required`）⇒ 凭据不是靠 env 传的；
- 完整版 CLI（`codebuddy install` 装的原生版）带 TUI 与 `/login` ⇒ 登录一次即可。

**"能干活"的判据**：**必须真发一次 prompt**（老教训：起得来 ≠ 能调模型；
桌面端派生的子进程就是"起得来但调不动"）。探针用**表内免费模型**。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .acp_client import AcpClient

logger = logging.getLogger("api.wb_exec.cli_pool")

#: 起一个 CLI 网关的等待上限（秒）。
BOOT_TIMEOUT_SEC = 90.0
#: 探针轮询间隔（秒）。
POLL_SEC = 1.0
#: 探针 prompt 的超时（秒）——真调一次模型，短。
PROBE_TIMEOUT_SEC = 90.0

#: **最近一次 health 探测失败的原因**（排障用；空串＝没失败过）。
_LAST_HEALTH_ERROR = ""


def free_port() -> int:
    """拿一个空闲端口（bind 后立刻释放——有极小竞态，失败会重试）。"""
    with socket.socket(socket.Afamily_INET if hasattr(socket, "afamily") else socket.AF_INET,
                        socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class CliWorker:
    """一个 CLI 进程（= 一个 agent 槽）。

    **两种跑法**：
    - **本机**（Windows 原生 CLI）：``exe`` 直接跑，env 里给配置目录。
    - **WSL 内**（``wsl`` 有值）：``wsl.exe -d <发行版> bash -lc "<命令>"``，
      命令在 **WSL 的 HOME** 下跑 ⇒ **凭据目录天然隔离**（Linux 路径
      ``~/.local/share/CodeBuddyExtension/Data/Public/auth``）⇒
      **两套 CLI 服务可以各登各的账号，互不覆盖**（2026-10-23 用户要求）。
    """

    exe: str
    config_dir: str
    work_dir: str
    port: int
    proc: Optional[subprocess.Popen] = None
    extra_env: dict[str, str] = field(default_factory=dict)
    #: WSL 模式：``{distro, home, cmd}``
    wsl: Optional[dict[str, str]] = None
    last_error: str = ""
    #: **WSL 模式：Windows 侧访问它的地址**（WSL 的 IP）。
    #: **★ 为什么不能用 127.0.0.1**（2026-10-24 实测）：WSL2 的
    #: ``localhostForwarding`` 只把 WSL 内绑 **127.0.0.1** 的服务转出来；
    #: 而 WSL 里的 CLI 我们绑的是 ``0.0.0.0``（否则转不出来）⇒
    #: 只能走 **WSL 的 IP**（NAT 网络下宿主能直连 guest 的 IP）。
    #: ⇒ 前提：Hyper-V 防火墙要放行（``New-NetFirewallHyperVRule``，管理员）。
    host: str = "127.0.0.1"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def build_env(config_dir: str, extra: Optional[dict[str, str]] = None,
              home: str = "") -> dict:
    """CLI 进程的环境：配置目录指向该版本的数据根（凭据就落在这儿）。

    **``home``** ＝ 隔离的 ``USERPROFILE`` ⇒ **凭据目录跟着走**
    （桌面端的凭据目录 ＝ ``%USERPROFILE%\\AppData\\Local\\CodeBuddyExtension\\
    Data\\Public\\auth``）⇒ 两套 CLI 服务**各自一个 home ⇒ 各自一个账号文件**
    ⇒ 互不覆盖（2026-10-23 用户要求"两套 CLI 服务，各自登录账号"）。
    """
    env = dict(os.environ)
    env["CODEBUDDY_CONFIG_DIR"] = config_dir
    env["WORKBUDDY_CONFIG_DIR"] = config_dir
    if home:
        env["USERPROFILE"] = home
        env["HOME"] = home
    for k, v in (extra or {}).items():
        if v is None:
            continue
        env[str(k)] = str(v)
    return env


def to_wsl_path(path: str) -> str:
    """Windows 路径 ⇒ WSL 路径（``D:\\a\\b`` ⇒ ``/mnt/d/a/b``）。"""
    p = str(path).replace("\\", "/")
    if len(p) > 2 and p[1] == ":":
        return f"/mnt/{p[0].lower()}" + p[2:]
    return p


async def _wsl_ip(distro: str = "Ubuntu-22.04", tries: int = 20,
                  wait_sec: float = 1.5) -> str:
    """取 WSL 的 IP（**Windows 侧访问 WSL 里服务的地址**）。

    **★ 为什么必须重试**（2026-10-24 实测踩到的坑）：
    ``wsl.exe`` 刚把 VM 拉起来时，**eth0 的 IP 还没分配好**
    ⇒ 第一次 ``hostname -I`` 可能返回空 ⇒ 拿空 IP 去连 ⇒ 必然连不上
    ⇒ 表现就是"CLI 网关起不来：90s 内没就绪"（其实进程好好地在跑）。
    ⇒ 这里**轮询等 IP 出现**（最多 ~30 秒）。

    WSL 每次重启 IP 都可能变 ⇒ **每次起 worker 现取**，不做缓存。
    取不到 ⇒ 返回空串（调用方会因连不上而报错，**不猜**）。
    """
    for i in range(max(1, tries)):
        try:
            r = subprocess.run(
                ["wsl.exe", "-d", distro, "-e", "sh", "-c",
                 "hostname -I | awk '{print $1}'"],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace")
            ip = (r.stdout or "").strip().splitlines()
            ip = ip[0].strip() if ip else ""
            if ip:
                return ip
        except Exception as exc:  # noqa: BLE001
            logger.debug("[wb_exec] 取 WSL IP 失败（第 %d 次）: %s", i + 1, exc)
        if i + 1 < tries:
            await asyncio.sleep(wait_sec)
    logger.warning("[wb_exec] 取不到 WSL IP（%s）⇒ 访问不了 WSL 里的 CLI", distro)
    return ""


def _wsl_command(wsl: dict[str, str], port: int, cwd_wsl: str,
                 permission_mode: str = "bypassPermissions") -> str:
    """在 WSL 里跑 CLI 的完整命令（HOME/配置目录都指到**隔离的** WSL home）。"""
    home = wsl.get("home") or "/root"
    cmd = wsl.get("cmd") or "codebuddy"
    return (
        f"export HOME={home}; "
        f"export CODEBUDDY_CONFIG_DIR={home}/.codebuddy; "
        f"export WORKBUDDY_CONFIG_DIR={home}/.codebuddy; "
        f"cd {cwd_wsl} || exit 1; "
        # **★ 必须绑 0.0.0.0**（2026-10-24 实测踩到的坑）：
        #   绑 127.0.0.1 ⇒ WSL2 的 localhost 转发不转它（只转 WSL 内自己访问
        #   127.0.0.1 的那种），Windows 侧用 WSL 的 IP 连 ⇒ **连不上** ⇒
        #   表现是「CLI 网关起不来：90s 内没就绪」，其实进程好好地在跑。
        f"exec {cmd} --serve --port {port} --host 0.0.0.0 --auth none "
        f"--permission-mode {permission_mode}"
    )


async def start_cli_worker(exe: str, config_dir: str, work_dir: str,
                           extra_env: Optional[dict[str, str]] = None,
                           port: int = 0,
                           wsl: Optional[dict[str, str]] = None,
                           home: str = "",
                           permission_mode: str = "bypassPermissions"
                           ) -> CliWorker:
    """起一个 CLI 网关（``--serve --auth none``），**等它 health 通了才返回**。

    **cwd = work 目录** ⇒ 它的会话 cwd 就是这个 work 目录 ⇒ 真"一卡一目录"。

    ``wsl`` 有值 ⇒ **进程跑在 WSL 里**（``wsl.exe -d <发行版> bash -lc ...``）：
    凭据目录在 WSL 的 HOME 下 ⇒ 与桌面端、与另一套 CLI **物理隔离**。

    **★ 权限模式（2026-10-24 实测更正）**：早先固定 ``acceptEdits`` ⇒
    只自动放行"文件编辑"，**Bash / PowerShell 仍要人工审批**
    ⇒ agent 一跑命令就进 ``runStatus=waiting_for_permission`` **干等**
    ⇒ 那个 agent 槽被永久占住 ⇒ 后面的卡全卡死（现场：card_012 / card_014
    就是这么挂住的）。⇒ 默认改 ``bypassPermissions``：命令直接放行。
    """
    if not exe or (not Path(exe).exists() and not wsl):
        raise RuntimeError(f"CLI 不存在: {exe}")
    port = port or free_port()
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wb_exec] 建 work 目录失败 %s: %s", work_dir, exc)
    worker = CliWorker(exe=exe, config_dir=config_dir, work_dir=work_dir,
                       port=port, extra_env=dict(extra_env or {}), wsl=wsl)
    if wsl:
        cmd = _wsl_command(wsl, port, to_wsl_path(work_dir), permission_mode)
        argv = ["wsl.exe", "-d", str(wsl.get("distro") or "Ubuntu-22.04"),
                "-e", "bash", "-lc", cmd]
    else:
        argv = [exe, "--serve", "--port", str(port), "--host", "127.0.0.1",
                "--auth", "none", "--permission-mode", permission_mode]
    worker.proc = subprocess.Popen(
        argv, cwd=work_dir,
        env=(build_env(config_dir, extra_env, home) if not wsl
             else dict(os.environ)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wsl:
        # **★ 必须在进程起来之后取 IP**（2026-10-24 实测踩到的坑）：
        #   WSL2 在**没有活动进程时会整体停掉**（/tmp 清空、IP 变化）。
        #   ⇒ 启动前取到的 IP 可能是上一轮 VM 的 ⇒ 连不上。
        #   ⇒ 进程起来后 WSL 一定活着，这时取的 IP 才准。
        #   （`wsl.exe -e bash -lc` 是**前台**挂着 ⇒ 进程不死 ⇒ WSL 不停。）
        worker.host = await _wsl_ip(str(wsl.get("distro") or "Ubuntu-22.04"))
    deadline = asyncio.get_running_loop().time() + BOOT_TIMEOUT_SEC
    while asyncio.get_running_loop().time() < deadline:
        if not worker.alive():
            worker.last_error = f"进程退出（码 {worker.proc.returncode}）"
            break
        if await _healthy(worker.base_url):
            logger.info("[wb_exec] CLI 进程就绪 %s 端口 %s（cwd=%s）",
                        Path(exe).name, port, work_dir)
            return worker
        await asyncio.sleep(POLL_SEC)
    if not worker.last_error:
        worker.last_error = (f"{BOOT_TIMEOUT_SEC}s 内没就绪"
                             f"（host={worker.host} port={port} "
                             f"health={_LAST_HEALTH_ERROR}）")
    worker.stop()
    raise RuntimeError(f"CLI 网关起不来：{worker.last_error}")


async def _healthy(base_url: str) -> bool:
    """探这个地址的 health（**失败原因记进 :data:`_LAST_HEALTH_ERROR`，便于排障**）。"""
    global _LAST_HEALTH_ERROR
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{base_url}/api/v1/health",
                            headers={"x-codebuddy-request": "1"})
        if r.status_code in (200, 401):
            return True
        _LAST_HEALTH_ERROR = f"HTTP {r.status_code}"
        return False
    except Exception as exc:  # noqa: BLE001
        _LAST_HEALTH_ERROR = f"{type(exc).__name__}: {exc}"
        return False


async def probe_can_work(base_url: str, model: str,
                         timeout: float = PROBE_TIMEOUT_SEC
                         ) -> tuple[bool, list[dict]]:
    """**真发一次 prompt** 验这个 CLI 能不能干活（老教训：起得来 ≠ 能调模型）。

    返回 ``(能不能干活, 模型目录)``。目录里带 ``_meta.credits``（如
    ``"x0.00 credits"`` / ``"x0.11 credits"``）⇒ **上层据此判断"这个模型在
    这个产品里是不是免费"**（用户规矩：绝不把任务换到付费端点上）。

    返回 True ＝ 能出正文（``stopReason == end_turn``）。
    """
    client = AcpClient(base_url, timeout=timeout, connect_timeout=15.0)
    catalog: list[dict] = []
    try:
        try:
            await client.connect()
            await client.initialize()
        except Exception as exc:  # noqa: BLE001
            logger.info("[wb_exec] CLI 探针握手失败 %s: %s", base_url, exc)
            return False, catalog
        sid = f"probe-{os.getpid()}-{int(asyncio.get_running_loop().time()*1000)}"
        try:
            res = await client.session_load(sid, os.path.dirname(base_url) or ".",  # noqa: ARG005
                                           take_writer=True)
            models = ((res or {}).get("models") or {}).get("availableModels") or []
            catalog = [m for m in models if isinstance(m, dict)]
        except Exception as exc:  # noqa: BLE001
            logger.info("[wb_exec] CLI 探针建会话失败 %s: %s", base_url, exc)
            return False, catalog
        try:
            await client.set_config_option(sid, "model", model)
        except Exception as exc:  # noqa: BLE001
            logger.info("[wb_exec] CLI 探针切模型失败 %s: %s", base_url, exc)
            return False, catalog
        try:
            res = await client.prompt(sid, "只回复两个字：收到", timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.info("[wb_exec] CLI 探针发 prompt 失败 %s: %s", base_url, exc)
            return False, catalog
        stop = str((res or {}).get("stopReason") or "")
        meta = (res or {}).get("_meta") or {}
        err = str(meta.get("codebuddy.ai/errorMessage") or "")
        ok = stop == "end_turn"
        logger.info("[wb_exec] CLI 探针 %s：stop=%s %s", base_url, stop, err)
        return ok, catalog
    finally:
        await client.close()


def model_credits(catalog: list[dict], model: str) -> str:
    """这个模型在**这个产品**里的计价（``"x0.00 credits"`` 之类；查不到 ⇒ 空）。"""
    for m in catalog:
        if str(m.get("modelId") or "") != str(model):
            continue
        meta = m.get("_meta") or {}
        return str(meta.get("credits") or m.get("description") or "")
    return ""


def is_free_model(catalog: list[dict], model: str) -> bool:
    """这个模型在**这个产品**里免费吗？（``x0.00`` ⇒ 免费）

    **用户规矩**：绝不把任务换到付费端点上（"x0.11" 就是别的产品的价）。
    ⇒ CLI 池**只服务免费模型**；要花钱的模型仍旧走它自己的桌面端网关。
    """
    credits = model_credits(catalog, model).strip().lower()
    return credits.startswith("x0.00") or credits.startswith("x0.0 ") \
        or credits == "free" or credits.startswith("free ")
