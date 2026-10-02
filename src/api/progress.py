# -*- coding: utf-8 -*-
"""调用进度跟踪（实时反馈用，纯展示层）。

单进程 uvicorn 内存态：key=(work_id, card_id)。
只上报状态，不拦截、不限制、不进任何模型输入——不是闸。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

_store: Dict[Tuple[str, str], Dict[str, Any]] = {}


def set_stage(work_id: str, card_id: str, stage: str, detail: str = "") -> None:
    _store[(work_id, card_id)] = {
        "stage": stage,
        "detail": detail,
        "ts": time.time(),
    }


def get_stage(work_id: str, card_id: str) -> Optional[Dict[str, Any]]:
    return _store.get((work_id, card_id))
