# -*- coding: utf-8 -*-
"""安全重启规程（唯一许可的重启方式）。

背景：10-02 当天两次误杀在跑调用——16:49:44 用户消息发出 2 秒后服务被重启、
18:06 消息同样死于 18:10 重启。两次都是"没查清有没有在跑调用就动了服务"。

用法：
    python restart_safe.py            # 检查并安全重启
    python restart_safe.py --check    # 只检查不动手

判据（机械，不猜）：
    服务进程对远端 443/80 存在 ESTABLISHED 连接 → 视为有调用在跑 → 拒绝重启。
    （三席并发/统合/工具循环期间，服务进程必然持有到模型端点的连接；
     空闲时的 keep-alive 残留会在数秒内自行消散，命中时打印远端地址供人工复核。）
"""
import os, subprocess, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8")
CHECK_ONLY = "--check" in sys.argv
PORT = 5700
BASE = f"http://127.0.0.1:{PORT}"


def get_pid_listeners() -> list[int]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue)"
         ".OwningProcess | Select-Object -Unique"],
        capture_output=True, text=True).stdout
    return [int(p) for p in out.split() if p.isdigit()]


def get_outbound(pid: int) -> list[str]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-NetTCPConnection -OwningProcess {pid} -State Established -ErrorAction SilentlyContinue"
         " | Where-Object { $_.RemotePort -in 443,80 }"
         " | ForEach-Object { \"$($_.RemoteAddress):$($_.RemotePort)\" } | Select-Object -Unique"],
        capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    pids = get_pid_listeners()
    if not pids:
        print(f"[1] 端口 {PORT} 无监听——服务未运行，可直接启动。")
        subprocess.run(["cmd", "/c", "start", "", "/min", "start.bat"])
        print("start.bat 已拉起。")
        return 0

    print(f"[1] 服务运行中 PID={pids}")
    # 服务健康检查失败不算"可重启"，只作提示
    try:
        urllib.request.urlopen(f"{BASE}/api/works", timeout=5)
        print("[2] /api/works 200")
    except Exception as exc:
        print(f"[2] 健康检查异常（{exc}）——仍按连接判据决定。")

    for pid in pids:
        remote = get_outbound(pid)
        if remote:
            print(f"[3] PID {pid} 有到模型端点的活连接：{remote}")
            print("    判定：调用进行中——拒绝重启。等它跑完（前端有实时进度）再来。")
            return 1

    print("[3] 无对外活连接——无调用在跑，可以重启。")
    if CHECK_ONLY:
        print("    （--check 模式，只报告不动手）")
        return 0

    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"[4] killed {pid}")
    time.sleep(2)
    if os.path.exists(".server.pid"):
        os.remove(".server.pid")
    subprocess.run(["cmd", "/c", "start", "", "/min", "start.bat"])
    for i in range(30):
        time.sleep(2)
        try:
            urllib.request.urlopen(f"{BASE}/api/works", timeout=4)
            print(f"[5] 服务就绪（{(i + 1) * 2}s）")
            return 0
        except Exception:
            pass
    print("[5] !! 60 秒未就绪，人工检查。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
