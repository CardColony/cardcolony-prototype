# -*- coding: utf-8 -*-
"""重启后自检：SIWC 两把座椅是否真的进了生产进程。

用法（重启完之后跑）：
    ./.venv/Scripts/python.exe scripts/verify_siwc.py
"""
from __future__ import annotations

import json
import sys
import time

import httpx

WEB = "http://127.0.0.1:5700"
SEATS = (
    "gpt-62-sol-siwc",
    "gpt-61-terra-siwc",
    "gpt-62-astra-siwc",
    "gpt-6-astra-siwc",
    "gpt-61-sol-siwc",
    "gpt-6-sol-siwc",
    "gpt-6-luna-siwc",
)
PORTS = (5700, 5701, 5702, 5703, 5704, 5710)


def _get(url: str, timeout: float = 6.0) -> httpx.Response | None:
    try:
        return httpx.get(url, timeout=timeout, trust_env=False)
    except Exception:
        return None


def main() -> int:
    print("=" * 60)
    print("1) 进程存活")
    bad = []
    for port in PORTS:
        r = _get(f"http://127.0.0.1:{port}/api/settings/models")
        ok = r is not None and r.status_code == 200
        print(f"   {port}: {'OK' if ok else 'FAIL'}")
        if not ok:
            bad.append(port)
    if bad:
        print(f"   × 端口未就绪: {bad}（等几秒再跑，或看 logs\\service_restart.log）")

    print("2) 座椅是否在目录里")
    r = _get(f"{WEB}/api/settings/models")
    if r is not None and r.status_code == 200:
        body = r.text
        for seat in SEATS:
            print(f"   {seat}: {'在' if seat in body else '不在（models.yaml 没刷进来）'}")

    print("3) 真实调用（走生产进程，ChatGPT 套餐额度）")
    failed = 0
    for seat in SEATS:
        t0 = time.time()
        try:
            resp = httpx.post(
                f"{WEB}/api/settings/models/test",
                json={"kind": "model", "model_name": seat},
                timeout=180, trust_env=False)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            ok = bool(data.get("ok"))
            detail = str(data.get("reply") or data.get("error") or resp.text)[:90]
            print(f"   {seat}: {'OK' if ok else 'FAIL'} {time.time()-t0:.1f}s {detail!r}")
            if not ok:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"   {seat}: EXC {type(exc).__name__}: {str(exc)[:100]}")
            failed += 1

    print("=" * 60)
    if failed == 0 and not bad:
        print("结论：重启生效，SIWC 两把座椅可用。")
        return 0
    print("结论：未全通。看上面失败项；配置问题看 logs\\service_restart.log / server.log。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
