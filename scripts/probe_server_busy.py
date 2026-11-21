# -*- coding: utf-8 -*-
"""探针：页面加载期间，服务端读口到底是被**谁**压住了。

做法：连续打同一个读口，逐次记耗时；另起一个浏览器做冷开。
⇒ 若「浏览器加载期间」的读口耗时暴涨 ⇒ 是**服务端忙**（事件循环被占）；
   若浏览器加载期间读口依旧很快 ⇒ 是**浏览器侧**（连接池/解析/渲染）。

用法：python scripts/probe_server_busy.py <baseUrl> <workId> <cardId> [秒数]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5707"
WID = sys.argv[2] if len(sys.argv) > 2 else "work_073"
CID = sys.argv[3] if len(sys.argv) > 3 else "card_007"
SECS = float(sys.argv[4]) if len(sys.argv) > 4 else 12.0


def timed_get(url: str) -> tuple[float, int]:
    t0 = time.perf_counter()
    with urllib.request.urlopen(urllib.request.Request(url), timeout=120) as r:
        body = r.read()
    return (time.perf_counter() - t0) * 1000, len(body)


def main() -> int:
    tl = f"{BASE}/api/works/{WID}/cards/{CID}/timeline?tail=400&session={CID}"
    print("== 空闲时（连打三次）==")
    for i in range(3):
        ms, n = timed_get(tl)
        print(f"  {ms:8.1f} ms  {n/1024:8.1f} KB")

    print("== 浏览器冷开期间 ==")
    proc = subprocess.Popen(
        ["node", "tests/chat_perf.mjs", BASE, WID, CID, "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t_end = time.time() + SECS
    while time.time() < t_end:
        ms, n = timed_get(tl)
        print(f"  {ms:8.1f} ms  {n/1024:8.1f} KB   @{time.strftime('%H:%M:%S')}")
        time.sleep(0.5)
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("== 收尾（再打两次）==")
    for i in range(2):
        ms, n = timed_get(tl)
        print(f"  {ms:8.1f} ms  {n/1024:8.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
