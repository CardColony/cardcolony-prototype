# -*- coding: utf-8 -*-
"""以 detached 进程启动 5713 隔离实例（避开 cmd 的 GBK 中文路径坑）。

为什么不用 .bat：路径里有中文（推演agent），cmd 在 GBK 代码页下把它解成乱码，
`cd /d` 直接失败（实测两次）。Python 这边路径与环境变量都是 Unicode，一步到位；
DETACHED_PROCESS 保证父进程（本次调用）退出后实例继续跑。
"""
import os
import subprocess
from pathlib import Path

REPO = Path(r"D:\AI_station\推演agent\.worktrees\5713")
PY = r"C:\Program Files\Python311\python.exe"
LOG = REPO / "verify" / "5713" / "_instance_run3.log"

env = dict(os.environ)
env["ECO_TEST_SANDBOX"] = "1"
env["ECO_SANDBOX_ROOT"] = str(REPO.parent / "5713_sandbox")

LOG.parent.mkdir(parents=True, exist_ok=True)
log = open(LOG, "ab", buffering=0)
flags = 0
flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
p = subprocess.Popen([PY, "scripts/run_test_instance_5701.py", "5713"],
                     cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, creationflags=flags)
print("launched pid", p.pid, "sandbox_root", env["ECO_SANDBOX_ROOT"], flush=True)