# -*- coding: utf-8 -*-
"""探针：窄读 / 整卡 / 时序尾窗 三条读口的**服务端**耗时与字节。

用法：python scripts/probe_read_ports.py <base_url> [work_id] [card_id]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request


def timed_get(url: str, timeout: float = 600.0) -> tuple[int, float, bytes]:
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    dt = (time.perf_counter() - t0) * 1000
    return resp.status, dt, body


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5707"
    wid = sys.argv[2] if len(sys.argv) > 2 else "work_073"
    cid = sys.argv[3] if len(sys.argv) > 3 else "card_007"
    urls = [
        ("窄读 projection=meta", f"{base}/api/works/{wid}/cards/{cid}?projection=meta"),
        ("整卡（默认契约）", f"{base}/api/works/{wid}/cards/{cid}"),
        ("时序尾窗 tail=400", f"{base}/api/works/{wid}/cards/{cid}/timeline?tail=400"),
        ("窄读（热）", f"{base}/api/works/{wid}/cards/{cid}?projection=meta"),
        ("整卡（热）", f"{base}/api/works/{wid}/cards/{cid}"),
        ("时序尾窗（热）", f"{base}/api/works/{wid}/cards/{cid}/timeline?tail=400"),
        ("时序尾窗（热2）", f"{base}/api/works/{wid}/cards/{cid}/timeline?tail=400"),
        ("exec-backend", f"{base}/api/works/{wid}/cards/{cid}/exec-backend"),
        ("模型覆盖表（全项目）", f"{base}/api/settings/model-overrides/{wid}?card_id={cid}"),
        ("模型生效（窄）", f"{base}/api/settings/model-overrides/{wid}/effective?card_id={cid}"),
        ("规则 /rule", f"{base}/api/works/{wid}/rule"),
        ("导航 /api/nav", f"{base}/api/nav"),
    ]
    for label, url in urls:
        try:
            status, ms, body = timed_get(url)
            print(f"{label:<24} {status}  {ms:8.1f} ms  {len(body)/1024:9.1f} KB")
        except Exception as exc:  # noqa: BLE001
            print(f"{label:<24} ERR  {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
