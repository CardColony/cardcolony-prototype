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
#
# [多进程拓扑 2026-11-07] 服务＝web(5700) + runner x ECO_RUNNER_COUNT(5701+i)。
# 看门狗逐进程探活：web 死单独拉 web，runner-i 死单独拉 runner-i——
# 不连坐（健康进程上的在途回合不被陪葬）。runner-i 只认「应有pid文件」：
# .server-runner{i}.pid 在才探才拉，单进程旧模式（无 runner pid 文件）
# 不碰 runner——否则会给 all 模式的进程旁边生二胎，双驱动器抢卡。
# 「手动停止闸」：stop.bat 立 logs/.manual_stop，看门狗见此标记不拉起；
# start.bat 撤标记。服务在跑时陈旧标记自清。

import glob
import json
import os
import shutil
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

RUNNER_COUNT = int(os.environ.get("ECO_RUNNER_COUNT") or 4)   # 与 start.bat 同口径
BASE_PORT = int(os.environ.get("ECO_RUNNER_BASE_PORT") or 5701)
# [凝练独立进程 2026-11-10] condense 进程裸探针口（condense_main.py 供）。
CONDENSE_PORT = int(os.environ.get("ECO_CONDENSE_PORT") or 5710)
STOP_FLAG = os.path.join(ROOT, "logs", ".manual_stop")
WD_PID_FILE = os.path.join(ROOT, "logs", ".watchdog.pid")

# [定期清理 2026-11-07] Temp 清扫：卡片测试产物、无头 Chrome 档案、Go 构建
# 残骸在 TEMP 根下越积越多（被杀的进程不会自清）。判据＝白名单图样＋静默期
# 超龄：活着的测试/chrome 持续写 ⇒ mtime 新鲜 ⇒ 不碰；孤儿几小时不写 ⇒ 删。
# 间隔/龄期/目录均可经 ECO_* 覆盖；ECO_TEMP_SWEEP_S=0 关停。
SWEEP_DIR = os.environ.get("ECO_TEMP_SWEEP_DIR") or r"D:\Temp"
SWEEP_EVERY_S = float(os.environ.get("ECO_TEMP_SWEEP_S") or 3600)
SWEEP_AGE_S = float(os.environ.get("ECO_TEMP_AGE_S") or 4 * 3600)
SWEEP_GLOBS = (
    "HeadlessChrome*", "go-build*", "workbuddy-product-spill-*",
    "cardlo-u8-markers-*", "codebuddy-*", "dbwatch_*",
    "chrome-probe-*", "devin_cdp_*", "pytest-*", "TestLease*",
)
# 只清内部老龄条目、不整删的目录（容器本身是活的）。
SWEEP_INSIDE = ("devin.exe-overflows",)

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


def _probe(url):
    """True=活着；返回 (ok, 原因)"""
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as r:
            return (r.status == 200, f"http {r.status}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def probe():
    return _probe(PROBE_URL)


def probe_runner(i):
    """runner-i 守 ECO_RUNNER_BASE_PORT+i 回环，白名单 GET 与 web 同面。"""
    return _probe(f"http://127.0.0.1:{BASE_PORT + i}/api/nav/version")


def probe_condense():
    """condense 进程守 5710 回环裸探针（condense_main.py 的 _health_server）。"""
    return _probe(f"http://127.0.0.1:{CONDENSE_PORT}/health")


def manual_stop_flag():
    """手动停止标记：在 ⇒ stop.bat 干的，不拉起。陈旧（>60s 且服务在跑）自清。"""
    if not os.path.exists(STOP_FLAG):
        return False
    return True


def clear_flag_if_stale(service_up):
    """服务明明在跑而标记还在 ⇒ 有人绕过 start.bat 拉起的，标记作废。"""
    if not service_up or not os.path.exists(STOP_FLAG):
        return
    try:
        if time.time() - os.path.getmtime(STOP_FLAG) < 60:
            return  # 刚立的：stop 正在执行中，别抢
        os.remove(STOP_FLAG)
        log("服务在跑，陈旧手动停止标记已清")
    except OSError:
        pass


def breaker_ok(restarts):
    """熔断：窗口内已达上限 ⇒ False（停手），否则 True。"""
    now = time.time()
    restarts[:] = [t for t in restarts if now - t < RESTART_WINDOW]
    if len(restarts) >= RESTART_LIMIT:
        log("熔断：10 分钟内已重启 %d 次，停手（人来看看）" % len(restarts))
        return False
    return True


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
         "Where-Object {$_.CommandLine -like '*main.py*' "
         "-and $_.CommandLine -notlike '*runner_main*'} | "
         "Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True, timeout=15)
    for tok in (r.stdout or "").split():
        if tok.isdigit():
            return int(tok)
    return 0


def proc_age(pid):
    """进程已活多少秒；查不到返回 None（不把岁数不明当年轻）。"""
    if not pid:
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(New-TimeSpan -Start (Get-CimInstance Win32_Process -Filter "
             f"'ProcessId={pid}').CreationDate -End (Get-Date)).TotalSeconds"],
            capture_output=True, text=True, timeout=15)
        return float((r.stdout or "").strip())
    except (ValueError, subprocess.TimeoutExpired):
        return None


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
                txt = "\n".join(l for l in txt.splitlines() if ":570" in l) or "(570x 无任何行)"
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


def _cluster_env(role, index=None):
    """与 start.bat 同口径的子进程环境。"""
    env = os.environ.copy()
    env["ECO_ROLE"] = role
    env["ECO_RUNNER_COUNT"] = str(RUNNER_COUNT)
    env["ECO_RUNNER_BASE_PORT"] = str(BASE_PORT)
    for k in ("ECO_RUNNER_URL", "ECO_RUNNER_URLS", "ECO_PORT",
              "ECO_RUNNER_INDEX"):
        env.pop(k, None)
    if index is not None:
        env["ECO_RUNNER_INDEX"] = str(index)
    return env


def _venv_py():
    p = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else "python"


def spawn_web():
    """拉起 web（5700 对外）。独立控制台、不挂在本进程上。"""
    log("拉起 web（main.py, ECO_ROLE=web）")
    subprocess.Popen([_venv_py(), "main.py"], cwd=ROOT,
                     env=_cluster_env("web"),
                     creationflags=(subprocess.CREATE_NEW_CONSOLE
                                    | subprocess.CREATE_NEW_PROCESS_GROUP))


def spawn_runner(i):
    """拉起 runner-i（5701+i 回环）。无窗，stdout 落 logs\\runner{i}_stdout.log。"""
    log(f"拉起 runner{i}（runner_main.py {i}）")
    out = open(os.path.join(ROOT, "logs", f"runner{i}_stdout.log"), "ab")
    try:
        subprocess.Popen([_venv_py(), "runner_main.py", str(i)], cwd=ROOT,
                         env=_cluster_env("runner", i), stdout=out,
                         stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    finally:
        out.close()


def spawn_condense():
    """拉起凝练独立进程（5710 探针）。无窗，stdout 落 logs\\condense_stdout.log。"""
    log("拉起 condense（condense_main.py）")
    out = open(os.path.join(ROOT, "logs", "condense_stdout.log"), "ab")
    try:
        subprocess.Popen([_venv_py(), "condense_main.py"], cwd=ROOT,
                         env=_cluster_env("condense"), stdout=out,
                         stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    finally:
        out.close()


def kill_by_pidfile(name, tag):
    """按 pid 文件杀进程并清文件。返回是否有进程被杀。"""
    pf = os.path.join(ROOT, name)
    pid = 0
    try:
        pid = int(open(pf).read().strip())
    except (ValueError, OSError):
        pass
    if pid:
        log(f"taskkill /F /T /PID {pid}（{tag}）")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30)
        time.sleep(2)
    try:
        os.remove(pf)
    except OSError:
        pass
    return bool(pid)


def kill_web():
    """只杀 web 进程本身（pid 文件优先，命令行兜底），不碰 runner。"""
    if kill_by_pidfile(".server.pid", "web"):
        return
    pid = find_pid()   # 兜底：pid 文件丢了时按命令行找 main.py（不含 runner_main）
    if pid:
        log(f"taskkill /F /T /PID {pid}（web 兜底）")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30)
        time.sleep(2)


def kill_runner(i):
    """只杀 runner-i。pid 文件缺失时按端口扫回环监听者兜底。"""
    if kill_by_pidfile(f".server-runner{i}.pid", f"runner{i}"):
        return
    try:
        r = subprocess.run(
            ["cmd", "/c",
             f"netstat -ano | findstr LISTENING | findstr /C:\":{BASE_PORT + i} \""],
            capture_output=True, text=True, timeout=15)
        for line in (r.stdout or "").splitlines():
            tok = line.split()
            if tok and tok[-1].isdigit() and int(tok[-1]) > 0:
                log(f"taskkill /F /T /PID {tok[-1]}（runner{i} 端口兜底）")
                subprocess.run(["taskkill", "/F", "/T", "/PID", tok[-1]],
                               capture_output=True, timeout=30)
                time.sleep(2)
                break
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"runner{i} 端口兜底失败: {e}")


def kill_condense():
    """只杀凝练进程。pid 文件缺失时按 5710 端口扫回环监听者兜底。"""
    if kill_by_pidfile(".server-condense.pid", "condense"):
        return
    try:
        r = subprocess.run(
            ["cmd", "/c",
             f"netstat -ano | findstr LISTENING | findstr /C:\":{CONDENSE_PORT} \""],
            capture_output=True, text=True, timeout=15)
        for line in (r.stdout or "").splitlines():
            tok = line.split()
            if tok and tok[-1].isdigit() and int(tok[-1]) > 0:
                log(f"taskkill /F /T /PID {tok[-1]}（condense 端口兜底）")
                subprocess.run(["taskkill", "/F", "/T", "/PID", tok[-1]],
                               capture_output=True, timeout=30)
                time.sleep(2)
                break
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"condense 端口兜底失败: {e}")


def _entry_newest(path):
    """条目自身 + 直接子项的最晚 mtime——顶层 mtime 不随深层写入更新，
    多采一层防把「还在往里写」的目录误判成孤儿。"""
    newest = 0.0
    try:
        newest = os.path.getmtime(path)
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    if st.st_mtime > newest:
                        newest = st.st_mtime
                except OSError:
                    pass
    except OSError:
        pass
    return newest


def _rm_entry(path):
    """删一个文件或目录，返回释放字节（量不出按 0）。锁着的条目抛错由调用方计数。"""
    freed = 0
    if os.path.isdir(path) and not os.path.islink(path):
        for dp, dn, fn in os.walk(path):
            for name in fn:
                try:
                    freed += os.path.getsize(os.path.join(dp, name))
                except OSError:
                    pass
        shutil.rmtree(path)          # 部分锁文件会抛 Error——调用方按「在写」计
    else:
        try:
            freed = os.path.getsize(path)
        except OSError:
            pass
        os.remove(path)
    return freed


def janitor():
    """Temp 定期清理一轮。白名单图样 + 静默超龄 ⇒ 删；其余一律不碰。"""
    if not SWEEP_DIR or not os.path.isdir(SWEEP_DIR):
        return
    now = time.time()
    gone = freed = locked = 0
    for pat in SWEEP_GLOBS:
        for p in glob.glob(os.path.join(SWEEP_DIR, pat)):
            try:
                if now - _entry_newest(p) < SWEEP_AGE_S:
                    continue
                freed += _rm_entry(p)
                gone += 1
            except (OSError, shutil.Error):
                locked += 1
    for dname in SWEEP_INSIDE:
        d = os.path.join(SWEEP_DIR, dname)
        if not os.path.isdir(d):
            continue
        try:
            for e in os.scandir(d):
                try:
                    if now - _entry_newest(e.path) < SWEEP_AGE_S:
                        continue
                    freed += _rm_entry(e.path)
                    gone += 1
                except (OSError, shutil.Error):
                    locked += 1
        except OSError:
            pass
    if gone or locked:
        log(f"temp 清扫: 删 {gone} 项（释 ~{freed // (1 << 20)}MB），"
            f"跳过在写/锁住 {locked} 项")


def wait_boot(budget=180):
    """拉起后等它监听就绪：每 5s 探一次，活了 True；超时 False。
    大库冷启动（迁移/检查点）远超 BOOT_WAIT，期间绝不能再次拉起。"""
    deadline = time.time() + budget
    while time.time() < deadline:
        ok, _ = probe()
        if ok:
            return True
        time.sleep(5)
    return False


def main():
    try:
        with open(WD_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    log(f"看门狗上线：探 web {PROBE_URL} + runner x{RUNNER_COUNT}"
        f"（{BASE_PORT}+i）+ condense（{CONDENSE_PORT}）"
        f"每 {INTERVAL}s，{MAX_FAILS} 连败即重启")
    fails = 0                      # web 连败
    runner_fails = {}              # runner-i 连败
    runner_seen = set()            # 见过 pid 文件的 runner——在册才探才拉
    condense_fails = 0             # condense 连败
    condense_seen = False          # 见过 .server-condense.pid 才探才拉（旧模式没有它）
    restarts = []                  # 重启时刻列表（熔断用）
    last_sweep = 0.0               # temp 清扫上次时刻（0＝上线即扫一轮）
    while True:
        ok, why = probe()
        clear_flag_if_stale(ok)

        # --- temp 定期清理：与服务生死无关，手动停止期间也照扫 ---
        if SWEEP_EVERY_S > 0 and time.time() - last_sweep >= SWEEP_EVERY_S:
            last_sweep = time.time()
            try:
                janitor()
            except Exception as e:  # noqa: BLE001 清扫失败绝不能拖死看门狗
                log(f"temp 清扫异常（下轮照跑）: {e}")

        if manual_stop_flag():
            if fails or any(runner_fails.values()):
                log("手动停止标记在——停摆是人为的，不拉起")
            fails = 0
            runner_fails.clear()
            time.sleep(INTERVAL)
            continue

        # --- web ---
        if ok:
            if fails:
                log(f"web 恢复（此前连败 {fails} 次）")
            fails = 0
        else:
            fails += 1
            log(f"web 探活失败 {fails}/{MAX_FAILS}: {why}")
            if fails >= MAX_FAILS:
                if not breaker_ok(restarts):
                    fails = 0
                    time.sleep(INTERVAL)
                    continue
                pid = find_pid()
                age = proc_age(pid)
                if pid and age is not None and age < 180:
                    log(f"探活连败但 web pid={pid} 才起 {age:.0f}s（启动期宽限），本轮不杀")
                    fails = 0
                    time.sleep(INTERVAL)
                    continue
                log(f"web 判定死亡，单独重启（pid={pid or '无'}）")
                autopsy(why)
                kill_web()
                ok2, _ = probe()
                pid2 = find_pid()
                if ok2 or pid2:
                    log(f"杀后复核：probe={'ok' if ok2 else 'fail'} pid={pid2 or '无'}，已有实例，跳过拉起")
                else:
                    spawn_web()
                restarts.append(time.time())
                fails = 0
                if not wait_boot():
                    log("BOOT 等待超时仍未就绪（下轮继续探，不重复拉起）")

        # --- runners：只认「在册」的（见过 pid 文件或本狗拉起的）——
        # 单进程 all 模式永远没有任何 runner pid 文件 ⇒ 一个都不碰，
        # 否则会给 all 进程旁生 runner 双驱动抢卡。在册 runner 即使 pid
        # 文件被清理/未写出也照探——拉起失败也能被熔断兜住。 ---
        for i in range(RUNNER_COUNT):
            if os.path.exists(f".server-runner{i}.pid"):
                runner_seen.add(i)
            if i not in runner_seen:
                continue
            rok, rwhy = probe_runner(i)
            if rok:
                runner_fails[i] = 0
                continue
            runner_fails[i] = runner_fails.get(i, 0) + 1
            log(f"runner{i} 探活失败 {runner_fails[i]}/{MAX_FAILS}: {rwhy}")
            if runner_fails[i] < MAX_FAILS:
                continue
            if not breaker_ok(restarts):
                runner_fails[i] = 0
                continue
            log(f"runner{i} 判定死亡，单独重启")
            kill_runner(i)
            rok2, _ = probe_runner(i)
            if rok2:
                log(f"runner{i} 杀后复核：已活，跳过拉起")
            else:
                runner_seen.add(i)
                spawn_runner(i)
            restarts.append(time.time())
            runner_fails[i] = 0

        # --- condense：与 runner 同则——在册（见过 pid 文件）才探才拉 ---
        if os.path.exists(".server-condense.pid"):
            condense_seen = True
        if condense_seen:
            cok, cwhy = probe_condense()
            if cok:
                condense_fails = 0
            else:
                condense_fails += 1
                log(f"condense 探活失败 {condense_fails}/{MAX_FAILS}: {cwhy}")
                if condense_fails >= MAX_FAILS:
                    if not breaker_ok(restarts):
                        condense_fails = 0
                    else:
                        log("condense 判定死亡，单独重启")
                        kill_condense()
                        cok2, _ = probe_condense()
                        if cok2:
                            log("condense 杀后复核：已活，跳过拉起")
                        else:
                            spawn_condense()
                        restarts.append(time.time())
                        condense_fails = 0

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
