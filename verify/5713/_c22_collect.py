# -*- coding: utf-8 -*-
"""card_022 · 用**项目自己的 venv**把整套测试连跑 3 轮，产出 r1/r2/r3.xml。

为什么是脚本而不是 .bat：本仓路径含中文，.bat 以 UTF-8 落盘后 cmd(cp936) 读不出
那条路径（实测 exit=9009 command not found）。python 脚本按 UTF-8 解析，且解释器
由启动参数给出，绕开这个坑。

启动方式（由卡执行）：start /min "<项目venv python>" verify\5713\_c22_collect.py
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent      # <worktree>/verify/5713
REPO = HERE.parents[1]                      # <worktree> 根
PY = Path(sys.executable)
OUT = HERE / "_c22_rounds"
OUT.mkdir(exist_ok=True)
LOG = OUT / "run.log"


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    project_venv = ".venv" in PY.parts
    log("START %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("PY=%s  (is_project_venv=%s)" % (PY, project_venv))
    log("REPO=%s" % REPO)
    if not project_venv:
        log("ABORT: 启动解释器不是项目 venv，先修启动命令")
        return 2
    for i in (1, 2, 3):
        xml = OUT / ("r%d.xml" % i)
        out = OUT / ("r%d.out" % i)
        if xml.exists():
            xml.unlink()          # 不让上一轮的 xml 冒充这一轮
        t0 = time.time()
        with out.open("wb") as f:
            p = subprocess.run(
                [str(PY), "-m", "pytest", "tests", "-q", "--junitxml=%s" % xml],
                cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT,
            )
        log("ROUND%d exit=%d %.1fs at %s xml=%s"
            % (i, p.returncode, time.time() - t0, time.strftime("%H:%M:%S"), xml.exists()))
    log("DONE %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    return 0


if __name__ == "__main__":
    sys.exit(main())