# -*- coding: utf-8 -*-
"""调用进度跟踪（实时反馈用，纯展示层）。

单进程 uvicorn 内存态：key=(work_id, card_id)。
只上报状态，不拦截、不限制、不进任何模型输入——不是闸。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

_store: Dict[Tuple[str, str], Dict[str, Any]] = {}

# 终止态：进程侧已收尾（红线的「活跃判定加固」要区分「还在跑」与「早就跑完了」）
_TERMINAL_STAGES = frozenset({"done", "error", "idle"})


def set_stage(work_id: str, card_id: str, stage: str, detail: str = "") -> None:
    _store[(work_id, card_id)] = {
        "stage": stage,
        "detail": detail,
        "ts": time.time(),
        # [信息流重出 Task4] 显式活跃位：此前前端只能靠阶段白名单反推「还在跑」，
        # 于是超长请求若阶段不变/进程已死，两种情况长得一样 → 刷新后要么误报进行中、
        # 要么误判收尾。现在由写点自己声明。
        "active": stage not in _TERMINAL_STAGES,
    }


def heartbeat(work_id: str, card_id: str, stage: str = "streaming") -> None:
    """只刷新活跃时间戳，不改语义阶段。

    逐字流每帧调用：仍在吐字的超长请求因此持续被判定为「仍在推进」，
    刷新后 /progress 仍能补出「进行中」（V-T4-1）。终止态不被心跳复活。
    """
    key = (work_id, card_id)
    cur = _store.get(key)
    if cur is None:
        _store[key] = {"stage": stage, "detail": "", "ts": time.time(), "active": True}
        return
    if not cur.get("active"):
        return          # done/error 之后不再复活，避免收尾后又被拉回进行中
    cur["ts"] = time.time()


def get_stage(work_id: str, card_id: str) -> Optional[Dict[str, Any]]:
    return _store.get((work_id, card_id))
