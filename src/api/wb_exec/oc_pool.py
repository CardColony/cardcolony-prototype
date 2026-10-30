# -*- coding: utf-8 -*-
"""**opencode server 进程池**（一个进程 = 一个 headless HTTP 引擎，可承载多个 session）。

**为什么是"进程池"而不是"每卡一个进程"**（2026-10-25 实测）：
- ``opencode serve`` 一个进程里能并发跑多个 session（实测：串行 8.2s vs 并行 4.3s）
  ⇒ **N 个进程 × K 个 slot** 比"每卡一进程"省进程；
- 但**一个 session 卡死不该拖垮别人** ⇒ 进程级隔离（与 WorkBuddy 的 CLI 池同构）；
- ``/event`` 是**每进程一条**的全局 SSE ⇒ 每个 slot 自己订阅也行（按 sessionID 过滤），
  连接数 = slot 数（可接受）。

**不用 ACP 的理由**：``opencode acp`` 只走 stdio ndJSON ⇒ 得新写一套管道传输；
HTTP 有 162 个端点 + OpenAPI + SSE ⇒ httpx 直接复用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("api.wb_exec.oc_pool")

#: 启动后等健康检查通过的上限（秒）
BOOT_TIMEOUT_SEC = 90.0
#: 健康检查轮询间隔（秒）
POLL_SEC = 0.5
#: 进程启动后额外缓冲（秒）—— opencode 起来后还要加载 provider 目录
EXTRA_BOOT_SEC = 1.5

#: **浏览器 UA**（2026-10-25 实测的关键）：
#: opencode 自己调 ``opencode.ai/zen/go/v1`` 时 Bun 的 fetch 不带 UA
#: ⇒ 被 Cloudflare 的 Browser Integrity Check 拦 ⇒ 403「error code: 1010」
#: ⇒ **连用户已付费的 Go 订阅内的模型也一起挂**。
#: ⇒ 用**自定义 provider** 直连该端点，并在 ``options.headers`` 里塞这个 UA。
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def write_provider_config(config_dir: str, *, provider_id: str,
                          base_url: str, api_key: str,
                          models: list[str],
                          user_agent: str = BROWSER_UA,
                          name: str = "opencode-direct") -> str:
    """写一份 opencode 配置：**自定义 provider 直连 + 浏览器 UA**。

    **为什么必须自己写**（而不是用 opencode 内置的 ``opencode-go``）：
    内置 provider 走 opencode 自己的 fetch ⇒ **不带浏览器 UA** ⇒ 被 Cloudflare 拦。

    ⚠ **apiKey 会落在这个文件里** ⇒ 所以这个文件必须放在**临时目录**、
    **不能进仓库**（由调用方负责用完清理）。
    """
    base = Path(config_dir)
    base.mkdir(parents=True, exist_ok=True)
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": name,
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key,
                    "headers": {"User-Agent": user_agent},
                },
                "models": {m: {"name": m} for m in models},
            },
        },
        "model": f"{provider_id}/{models[0]}" if models else "",
    }
    path = base / "opencode.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[wb_exec] opencode 配置已写 %s（provider=%s）", path, provider_id)
    return str(path)


def free_port() -> int:
    """拿一个空闲端口（bind 后立刻释放——有极小竞态，失败会重试）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class OcServer:
    """一个 ``opencode serve`` 进程。"""

    exe: str
    port: int
    cwd: str = ""
    proc: Optional[subprocess.Popen] = None
    last_error: str = ""
    #: 额外环境变量（例如 ``OPENCODE_CONFIG`` 指向自定义 provider 配置）
    env_extra: dict[str, str] = field(default_factory=dict)
    #: **当前占着这个进程的 session id**（空 ⇒ 空闲 ⇒ 可派活）。
    #: **一进程一槽**（与 WorkBuddy 的 CLI 池同构 ⇒ 进程级隔离）。
    busy_session: str = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def as_dict(self) -> dict:
        return {"port": self.port, "base_url": self.base_url,
                "alive": self.alive(), "pid": self.proc.pid if self.proc else 0,
                "last_error": self.last_error}


def _health(base_url: str) -> bool:
    """探 ``/global/health``（同步，供启动等待用）。"""
    import httpx
    try:
        r = httpx.get(f"{base_url}/global/health", timeout=3.0)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def start_oc_server(exe: str, cwd: str = "", port: int = 0,
                          config_path: str = "",
                          env_extra: Optional[dict[str, str]] = None
                          ) -> OcServer:
    """起一个 ``opencode serve``，**等它健康了才返回**。

    ``config_path``：opencode 的自定义 provider 配置文件（``OPENCODE_CONFIG``）。
    """
    if not exe or not Path(exe).exists():
        raise RuntimeError(f"opencode 可执行文件不存在: {exe}")
    port = port or free_port()
    if cwd:
        Path(cwd).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(env_extra or {})
    if config_path:
        env["OPENCODE_CONFIG"] = config_path
    argv = [exe, "serve", "--port", str(port), "--hostname", "127.0.0.1"]
    logger.info("[wb_exec] 起 opencode serve 端口=%s cwd=%s", port, cwd)
    proc = subprocess.Popen(argv, cwd=cwd or None, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server = OcServer(exe=exe, port=port, cwd=cwd, proc=proc,
                      env_extra=dict(env_extra or {}))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BOOT_TIMEOUT_SEC
    while loop.time() < deadline:
        if not server.alive():
            server.last_error = f"进程退出（码 {proc.returncode}）"
            break
        if _health(server.base_url):
            await asyncio.sleep(EXTRA_BOOT_SEC)
            server.last_error = ""
            logger.info("[wb_exec] opencode serve 就绪 %s", server.base_url)
            return server
        await asyncio.sleep(POLL_SEC)
    if not server.last_error:
        server.last_error = f"{BOOT_TIMEOUT_SEC}s 内没就绪"
    server.stop()
    raise RuntimeError(f"opencode server 起不来：{server.last_error}")
