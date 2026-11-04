# watchdog.py —— 服务看门狗 [2026-11-04]
#
# 背景：main.py 反复出现「TCP 监听插座悄悄死掉」——进程活着、事件循环照跑、
# 卡片照干、日志照写，唯独所有 HTTP 连接被拒绝，页面全挂且无人发现。
# 本脚本从进程外探活：死了就拉起，死因是什么都兜得住。
#
# 判定：连续 4 次探活失败（≈80s 持续无响应）→ 重启。
#   失败＝连接拒绝 / 超时(>10s) / 非 200。单次抖动不触发。
# 熔断：10 分钟内重启 ≥4 次 → 停手写日志（崩溃循环比死着更糟）。
# 用法：python watchdog.py   （建议用 start "看门狗" /min 后台跑）

import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

PROBE_URL = "http://127.0.0.1:5700/api/nav/version"
PROBE_TIMEOUT = 10          # 秒；该端点正常 <100ms，10s 已极宽松
INTERVAL = 20               # 探活间隔
MAX_FAILS = 4               # 连续失败阈值 → 重启
RESTART_WINDOW = 600        # 熔断窗口（秒）
RESTART_LIMIT = 4           # 窗口内最多重启次数
BOOT_WAIT = 25              # 重启后等它爬起来

LOG = os.path.join(ROOT, "logs", "watchdog.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def probe():
    """True=活着；返回 (ok, 原因)"""
    try:
        with urllib.request.urlopen(PROBE_URL, timeout=PROBE_TIMEOUT) as r:
            return (r.status == 200, f"http {r.status}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def find_pid():
    """先信 .server.pid（校验它还活着且是 main.py），落空则按命令行找"""
    pf = os.path.join(ROOT, ".server.pid")
    if os.path.exists(pf):
        try:
            pid = int(open(pf).read().strip())
        except (ValueError, OSError):
            pid = 0
        if pid:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=15)
            if "main.py" in (r.stdout or ""):
                return pid
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
         "Where-Object {$_.CommandLine -like '*main.py*'} | "
         "Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True, timeout=15)
    for tok in (r.stdout or "").split():
        if tok.isdigit():
            return int(tok)
    return 0


def kill(pid):
    if not pid:
        return
    log(f"taskkill /F /T /PID {pid}")
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=30)
    time.sleep(3)


def autopsy(fail_why):
    """重启毁现场——动刀前先把尸体拍下来：netstat 5700 状态、python 进程
    全家福、server.log / listener_autopsy.log 尾巴。"""
    out = [f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 判定死亡: {fail_why} ====="]
    for label, cmd in [
        ("netstat 5700", ["netstat", "-ano"]),
        ("python procs", ["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
                          "Select ProcessId,CommandLine | Format-List"]),
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            txt = r.stdout or ""
            if label.startswith("netstat"):
                txt = "\n".join(l for l in txt.splitlines() if ":5700" in l) or "(5700 无任何行)"
            out.append(f"--- {label} ---\n{txt}")
        except Exception as e:
            out.append(f"--- {label} 失败: {e} ---")
    for lf in ["logs/server.log", "logs/listener_autopsy.log"]:
        try:
            with open(lf, "rb") as f:
                f.seek(-60000, 2)
                tail = f.read().decode("utf-8", "replace")
            out.append(f"--- {lf} 尾 ---\n{tail}")
        except Exception as e:
            out.append(f"--- {lf} 读取失败: {e} ---")
    path = os.path.join(ROOT, "logs", f"autopsy_{time.strftime('%m%d_%H%M%S')}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        log(f"尸检落盘 {path}")
    except OSError as e:
        log(f"尸检写盘失败: {e}")


def start_service():
    """按 start.bat 同款方式拉起：venv python main.py，独立控制台、
    不挂在本进程上（看门狗死了服务照跑）。PID 由 main.py 自己写 .server.pid"""
    venv_py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        venv_py = "python"
    cmd = f'start "推演agent画布" "{venv_py}" main.py'
    log(f"拉起: {cmd}")
    subprocess.Popen(cmd, shell=True, cwd=ROOT,
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


def main():
    log(f"看门狗上线：探 {PROBE_URL} 每 {INTERVAL}s，{MAX_FAILS} 连败即重启")
    fails = 0
    restarts = []  # 重启时刻列表（熔断用）
    while True:
        ok, why = probe()
        if ok:
            if fails:
                log(f"恢复（此前连败 {fails} 次）")
            fails = 0
        else:
            fails += 1
            log(f"探活失败 {fails}/{MAX_FAILS}: {why}")
            if fails >= MAX_FAILS:
                now = time.time()
                restarts = [t for t in restarts if now - t < RESTART_WINDOW]
                if len(restarts) >= RESTART_LIMIT:
                    log("熔断：10 分钟内已重启 %d 次，停手（人来看看）" % len(restarts))
                    fails = 0
                    time.sleep(INTERVAL)
                    continue
                pid = find_pid()
                log(f"判定死亡，重启（存活进程 pid={pid or '无'}）")
                autopsy(why)
                kill(pid)
                start_service()
                restarts.append(time.time())
                fails = 0
                time.sleep(BOOT_WAIT)
                continue
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
