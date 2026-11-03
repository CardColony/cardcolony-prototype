# -*- coding: utf-8 -*-
"""Chrome 发现与拉起（纯标准库，Windows / Linux / WSL / macOS）。

路径优先级：环境变量 CARDLO_CHROME_PATH → CHROME_PATH → 常见安装位
（Windows Program Files、WSL /mnt/c、Linux which、macOS /Applications）。
无 Chrome 时退 Edge（同为 CDP）。

拉起方式照 minimax-code 的 headless provider：--remote-debugging-port=0
让 Chrome 自选端口，读 profile 里的 DevToolsActivePort 文件拿 ws 端点；
每会话一个独立 --user-data-dir（隔离 cookie/缓存，可整目录清掉）。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


class ChromeNotFoundError(Exception):
    pass


class ChromeLaunchError(Exception):
    pass


_WIN_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
]
_WSL_CANDIDATES = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
]
_POSIX_NAMES = ("google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser", "brave-browser", "microsoft-edge")

#: 官方 headless 壳（Chrome for Testing 的 chrome-headless-shell）——
#: Google 为 CDP headless 出的独立二进制，WSL/CI 场景的最优解：
#: 原生跑 ⇒ 无 Windows 防火墙、无互操作、/mnt 路径即 Linux 路径。
HEADLESS_SHELL_VERSION = "141.0.7390.65"
HEADLESS_SHELL_ZIP_URL = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    f"{HEADLESS_SHELL_VERSION}/linux64/chrome-headless-shell-linux64.zip")
_RUNTIME_BIN_REL = "chrome-headless-shell-linux64/chrome-headless-shell"


def _runtime_candidates() -> list[str]:
    """已落地的 headless-shell 二进制候选位（环境变量 → 用户缓存 → 仓内）。"""
    out: list[str] = []
    env = os.environ.get("CARDLO_HEADLESS_SHELL", "").strip()
    if env:
        out.append(env)
    try:
        home = Path.home()
        out.append(str(home / ".cache" / "cardlo-browser" / _RUNTIME_BIN_REL))
    except Exception:  # noqa: BLE001
        pass
    out.append(str(Path.cwd() / "_browser_runtime" / _RUNTIME_BIN_REL))
    return out


def install_headless_shell(dest_root: Path | None = None,
                           quiet: bool = False) -> Path:
    """下载并解出官方 chrome-headless-shell（~120MB，一次性）。

    dest_root 默认 ``~/.cache/cardlo-browser/``（WSL 原生家目录，
    无中文路径/防火墙问题）。已存在可用二进制 ⇒ 直接返回不重下。
    """
    import io
    import stat
    import urllib.request
    import zipfile
    root = dest_root or (Path.home() / ".cache" / "cardlo-browser")
    binary = root / _RUNTIME_BIN_REL
    if binary.exists() and os.access(binary, os.X_OK):
        return binary
    if not quiet:
        print(f"下载 chrome-headless-shell {HEADLESS_SHELL_VERSION} → {root}",
              file=sys.stderr)
    with urllib.request.urlopen(HEADLESS_SHELL_ZIP_URL, timeout=300) as r:
        blob = r.read()
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(root)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return binary
_MAC_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def _is_wsl() -> bool:
    if sys.platform.startswith("linux"):
        try:
            return "microsoft" in Path("/proc/version").read_text().lower()
        except OSError:
            return False
    return False


def find_chrome() -> str:
    """找到可用的 Chrome/Chromium/Edge 可执行文件路径，找不到抛异常。"""
    for env in ("CARDLO_CHROME_PATH", "CHROME_PATH"):
        p = os.environ.get(env, "").strip()
        if p and Path(p).exists():
            return p
    if sys.platform.startswith("win"):
        for p in _WIN_CANDIDATES:
            if Path(p).exists():
                return p
        for name in ("chrome.exe", "msedge.exe", "chromium.exe", "brave.exe"):
            found = shutil.which(name)
            if found:
                return found
    elif sys.platform == "darwin":
        for p in _MAC_CANDIDATES:
            if Path(p).exists():
                return p
        for name in _POSIX_NAMES:
            found = shutil.which(name)
            if found:
                return found
    else:
        # 原生优先：已装的 headless-shell / 系统 chromium 都在前；
        # /mnt/c Windows 二进制殿后（WSL→Windows 有防火墙拦 DevTools）。
        for p in _runtime_candidates():
            if Path(p).exists():
                return p
        for name in _POSIX_NAMES:
            found = shutil.which(name)
            if found:
                return found
        for p in _WSL_CANDIDATES:
            if Path(p).exists():
                return p
    raise ChromeNotFoundError(
        "找不到 Chrome/Chromium/Edge。可设环境变量 CARDLO_CHROME_PATH 指定。")


def _to_win_path(p: str) -> str:
    """WSL → Windows 路径：/mnt/d/x/y → D:\\x\\y。非 /mnt 路径原样返回。"""
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", p)
    if m:
        return m.group(1).upper() + ":\\" + m.group(2).replace("/", "\\")
    return p


def _to_wsl_path(p: str) -> str:
    """Windows → WSL 路径：D:\\x\\y → /mnt/d/x/y。非盘符路径原样返回。"""
    m = re.match(r"^([a-zA-Z]):[\\/](.*)$", p)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return p


def _ascii_safe(p: str) -> bool:
    try:
        p.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def wsl_host_ip() -> str:
    """WSL2 里 Windows 宿主机的地址（default route 网关）。

    为什么是它不是 127.0.0.1：WSL2 的 localhost 转发只解决
    Windows→WSL 方向；**WSL 连不到 Windows 回环**。DevTools 要绑在
    Hyper-V 虚拟交换机的宿主机侧地址上，WSL 才能连——该网段是私有
    NAT，不出本机。
    """
    try:
        out = subprocess.run(["ip", "route"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("default"):
                return line.split()[2]
    except Exception:  # noqa: BLE001
        pass
    return ""


def launch(chrome_path: str, profile_dir: Path,
           headless: bool = True, viewport: tuple[int, int] = (1280, 800),
           extra_args: list[str] | None = None,
           start_url: str = "about:blank"
           ) -> tuple[subprocess.Popen, Path, str]:
    """拉起一个 Chrome 进程。返回 (进程, **实际生效**的 profile 目录)。

    返回值带 profile 是因为 WSL 模式可能挪位置：起 Windows 二进制时
    命令行参数经 GBK/UTF-8 编码交界，**含非 ASCII 的 profile 路径会
    搅乱**（实测：D:\\AI_station\\推演agent\\… 下 Chrome 起不来）。
    此时把 chrome 的 user-data-dir 挪到 ASCII 安全位
    ``<盘>/Temp/cardlo_browser/<名字哈希>``——profile 本就是可丢状态，
    session.json/截图仍落调用方目录（我们 python 读写，无编码问题）。
    file:// URL 同病：做 UTF-8 百分号编码。
    """
    import hashlib
    profile_dir = profile_dir.resolve()   # 相对路径在 Windows 侧无意义
    profile_dir.mkdir(parents=True, exist_ok=True)
    win_binary = _is_wsl() and chrome_path.startswith("/mnt/")
    prof_arg = str(profile_dir)
    prof_local = profile_dir
    url = start_url
    if win_binary:
        prof_arg = _to_win_path(prof_arg)
        if not _ascii_safe(prof_arg):
            digest = hashlib.sha1(prof_arg.encode("utf-8")).hexdigest()[:10]
            prof_arg = f"C:\\Temp\\cardlo_browser\\{profile_dir.name}-{digest}"
            prof_local = Path(_to_wsl_path(prof_arg))
        if url.startswith("file://"):
            from urllib.parse import quote
            url = ("file:///" + quote(
                _to_win_path(url[7:].lstrip("/")).replace("\\", "/"),
                safe="/:._-~"))
    else:
        prof_local.mkdir(parents=True, exist_ok=True)
    dbg_host = "127.0.0.1"
    if win_binary:
        dbg_host = wsl_host_ip() or "127.0.0.1"
    args = [
        chrome_path,
        "--remote-debugging-port=0",
        f"--remote-debugging-address={dbg_host}",
        f"--user-data-dir={prof_arg}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-features=Translate,OptimizationHints",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--mute-audio",
        "--no-service-autorun",
        "--password-store=basic",
        f"--window-size={viewport[0]},{viewport[1]}",
        "--window-position=0,0",
    ]
    if headless:
        args.append("--headless=new")
    if extra_args:
        args.extend(extra_args)
    args.append(url)
    kwargs: dict = {"stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)
    except OSError as exc:
        raise ChromeLaunchError(f"启动失败 {chrome_path}: {exc}") from exc
    return proc, prof_local, dbg_host


def read_devtools_ws(profile_dir: Path, timeout: float = 15.0,
                     host: str = "127.0.0.1") -> tuple[int, str]:
    """轮询 profile 里的 DevToolsActivePort → (port, ws_url)。

    host 默认回环；WSL 模式传宿主机地址（见 launch 的 dbg_host）——
    文件里写的 ws path 与 host 无关，换宿主即可。
    """
    marker = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = marker.read_text(encoding="ascii", errors="replace")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if len(lines) >= 2:
                port = int(lines[0])
                return port, f"ws://{host}:{port}{lines[1]}"
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise ChromeLaunchError(
        f"DevToolsActivePort 未出现（{timeout}s）：{marker}")


def port_alive(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def kill_process(pid: int) -> None:
    """终止一个 Chrome 进程（尽力，不抛）。"""
    if pid <= 0:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass
