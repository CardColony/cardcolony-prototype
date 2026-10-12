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
import datetime
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
CHECK_ONLY = "--check" in sys.argv
PORT = 5700
BASE = f"http://127.0.0.1:{PORT}"
# [2026-10-12 幽灵态修复] 崩掉的请求（未捕获异常穿透 → handle_error 没跑）会把
# progress 的 stage 永久停在活跃档，主判据从此永远拒绝重启（实测挡死 20+ 分钟）。
# 判据加一层时效：活跃态带 ts，超过本秒数无任何活动即视为崩溃残留；残留本身
# 不再锁死重启，但需旁证（磁盘 25 秒窗口无新写 ＋ 无对外连接）才放行——真在跑
# 的调用总会留下旁证（工具轮换/日志写/模型端点连接三者至少居其一）。
STALE_ACTIVE_SECONDS = 1800


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


def progress_active() -> list[tuple[str, float]]:
    """主判据：任一卡片 progress 活跃（非 done/error/空）→ 在跑。
    端点：GET /api/works/{wid}/cards/{cid}/progress（routes.py L215）。
    canvas 端点拿全部卡：GET /api/works/{wid}/canvas（L106）。"""
    active: list[tuple[str, float]] = []
    try:
        works = json.load(urllib.request.urlopen(f"{BASE}/api/works", timeout=5))
        wl = works.get("works") if isinstance(works, dict) else works
        for w in wl or []:
            wid = w.get("id") or w.get("work_id")
            try:
                cv = json.load(urllib.request.urlopen(
                    f"{BASE}/api/works/{wid}/canvas", timeout=5))
            except Exception:  # noqa: BLE001
                continue
            for c in cv.get("cards", []) if isinstance(cv, dict) else []:
                cid = c.get("id") or c.get("card_id")
                if not cid:
                    continue
                try:
                    pr = json.load(urllib.request.urlopen(
                        f"{BASE}/api/works/{wid}/cards/{cid}/progress", timeout=4))
                except Exception:  # noqa: BLE001
                    continue
                stage = pr.get("stage", "")
                # 静态档白名单（routes.py L220 权威：从未对话的卡默认 idle；
                # 完成态 done / 失败态 error / 空）。白名单外的任何档＝活跃。
                if stage and stage not in ("done", "error", "idle"):
                    try:
                        _ts = float(pr.get("ts", 0) or 0)
                    except (TypeError, ValueError):
                        _ts = 0.0
                    active.append((f"{wid}/{cid}:{stage}", _ts))
    except Exception:  # noqa: BLE001
        pass
    return active


def disk_activity(window_s: int = 25) -> bool:
    """复核判据：session/logs 在窗口内有新写入 → 调用在跑。"""
    cutoff = time.time() - window_s
    for pat in ("works/*/sessions/*", "logs/prompt_*.json"):
        for p in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), pat)):
            if os.path.getmtime(p) > cutoff:
                return True
    return False


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

    # [主判据] progress 活跃态——新鲜的无条件拒绝；陈旧的按崩溃残留复核旁证
    # （[2026-10-12] 见 STALE_ACTIVE_SECONDS：崩掉的请求会留下永久活跃态）
    active = progress_active()
    now = time.time()
    fresh = [e for e in active if e[1] and now - e[1] < STALE_ACTIVE_SECONDS]
    stale = [e for e in active if e not in fresh]
    if fresh:
        print(f"[3] progress 活跃调用：{[e[0] for e in fresh]}")
        print("    判定：调用进行中——拒绝重启。等它跑完（前端有实时进度）再来。")
        return 1
    if stale:
        print(f"[3] progress 残留活跃态（已超 "
              f"{STALE_ACTIVE_SECONDS // 60} 分钟无活动，判为崩溃请求的残留）："
              f"{[e[0] for e in stale]}")
        if disk_activity(25):
            print("    旁证：25 秒窗口内有 session/logs 新写入——有活动，拒绝重启。")
            return 1
        if any(get_outbound(pid) for pid in pids):
            print("    旁证：服务有到模型端点的连接——有活动，拒绝重启。")
            return 1
        print("    旁证：无磁盘新写入、无对外连接——按无调用在跑处理，继续。")
    else:
        print("[3] progress 全静（done/error/空）")

    # [复核判据] 对外活连接——命中时延迟复核，防 keep-alive 残留假阳性
    for pid in pids:
        remote = get_outbound(pid)
        if remote:
            print(f"[4] PID {pid} 有到模型端点的连接：{remote}——8 秒后复核")
            time.sleep(8)
            active2 = progress_active()
            disk2 = disk_activity(25)
            now2 = time.time()
            fresh2 = [e for e in active2
                      if e[1] and now2 - e[1] < STALE_ACTIVE_SECONDS]
            if fresh2:
                print(f"    复核：progress 活跃 {[e[0] for e in fresh2]}"
                      "——调用进行中，拒绝重启。")
                return 1
            if disk2:
                print("    复核：session/logs 25 秒窗口有新写入——调用进行中，拒绝重启。")
                return 1
            print("    复核：progress 全静且磁盘无新写入——连接为 keep-alive 残留，放行。")

    print("[5] 判定：无调用在跑，可以重启。")
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
