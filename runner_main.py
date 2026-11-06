# -*- coding: utf-8 -*-
"""[多进程拆分 2026-11-06] 执行进程入口。

``python runner_main.py [INDEX]`` ⇒ 同一 main.py、ECO_ROLE=runner、
绑回环 ``ECO_RUNNER_BASE_PORT+INDEX``（缺省 5701+INDEX）：卡按子树根
哈希分片，本片回合全在这进程跑，跨片调用经 /ctl/* 透明转发。

完整拓扑（ECO_RUNNER_COUNT=K）：
  web      ECO_ROLE=web ECO_RUNNER_COUNT=K python main.py   ← 5700 对外
  runner0  ECO_RUNNER_COUNT=K python runner_main.py 0       ← 5701（驱动器/续跑）
  runner1  ECO_RUNNER_COUNT=K python runner_main.py 1       ← 5702
  ...
不设 ECO_ROLE 直接 ``python main.py``＝旧单进程行为（all）。
"""
import os
import runpy
import sys

os.environ.setdefault("ECO_ROLE", "runner")
if len(sys.argv) > 1:
    os.environ.setdefault("ECO_RUNNER_INDEX", sys.argv[1])

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "main.py"), run_name="__main__")
