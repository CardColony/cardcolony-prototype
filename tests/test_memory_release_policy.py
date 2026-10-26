# -*- coding: utf-8 -*-
"""内存释放策略网（[P25 · 2026-10-26]）

## 用户的问题与裁定

用户问：「**你现在内存释放是多久？**」——这是在确认我加的缓存**会不会按时间被扔掉**。

用户的裁定（2026-10-25，原话）：「**我他妈这么缺内存吗？需要他妈的十分钟就他妈的释放？**」
⇒ 释放**只按容量封顶**，**绝不按时间**。

## 本网钉住的不变量

| 编号 | 不变量 |
|---|---|
| M1 | 前端画板：`idleReleaseMs == 0`（**时间闸关闭**——不按时间释放任何画板） |
| M2 | 前端画板：`residentMax` 是**正数**（唯一的释放理由＝容量封顶，还在） |
| M3 | 服务端每个进程内缓存都**有上限**（内存封顶） |
| M4 | 服务端缓存**不按时间淘汰**（只按容量/失效判据）——不许偷偷加时间闸 |

铁律一：只用 conftest 的沙箱，不碰真实 works。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM_JS = (ROOT / "static" / "stream.js").read_text(encoding="utf-8")


def _board_cfg(src: str) -> dict[str, str]:
    """取 BOARD_CFG 里的几个值（源码级，避开注释）。"""
    body = re.sub(r"/\*[\s\S]*?\*/", "", src)
    out: dict[str, str] = {}
    for key in ("idleReleaseMs", "residentMax", "sweepMs", "pageNodes"):
        m = re.search(rf"{key}\s*:\s*([^,}}]+)", body)
        if m:
            out[key] = m.group(1).strip()
    return out


def test_m1_frontend_no_time_based_release():
    """M1：前端画板的时间闸必须是 0（不按时间释放）。"""
    cfg = _board_cfg(STREAM_JS)
    assert "idleReleaseMs" in cfg, f"stream.js 里应能读到 idleReleaseMs，实得 {cfg}"
    assert cfg["idleReleaseMs"] == "0", \
        f"idleReleaseMs 必须是 0（关闭时间释放），实为 {cfg['idleReleaseMs']}"


def test_m2_frontend_capacity_cap_still_there():
    """M2：容量封顶还在（唯一的释放理由）。"""
    cfg = _board_cfg(STREAM_JS)
    n = int(cfg["residentMax"])
    assert n > 0, cfg
    assert n >= 40, f"常驻上限不应小于 40（原来 40，本轮提到 {n}）"


def test_m3_server_caches_are_all_bounded():
    """M3：服务端每个进程内缓存都有上限（内存封顶）。"""
    from src.api import access_log
    from src.storage import event_log, session_facade

    assert session_facade._PARSE_CACHE_MAX > 0
    assert event_log._REVOKE_CACHE_MAX > 0, "撤区间缓存必须有上限（P25 补：原来漏了）"
    assert access_log.MAX_BYTES > 0, "落盘日志要有轮转上限"


def test_m4_no_time_based_cache_eviction():
    """M4：服务端缓存**不按时间淘汰**（只按容量 / 文件变化失效）。"""
    from src.storage import event_log, session_facade

    for mod, name in ((session_facade, "_PARSE_CACHE"), (event_log, "_REVOKE_CACHE")):
        src = inspect.getsource(mod)
        # 找缓存写入/淘汰那几行的上下文，不许出现时间判据
        for m in re.finditer(rf"{name}\[", src):
            seg = src[max(0, m.start() - 400): m.start() + 400]
            assert "time.time()" not in seg and "time.monotonic()" not in seg, \
                f"{mod.__name__}.{name} 出现了按时间淘汰的判据（用户明令：只按容量封顶）"
