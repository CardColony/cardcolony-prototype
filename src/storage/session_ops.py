# -*- coding: utf-8 -*-
"""底座全日志打点通道（迭代一·会话底座 S3 / 4.12，T152-T156）。

落点：`works/_ops/session_ops.jsonl`（追加式、永不改写——与档案意图同纪律）。
与既有 prompt 日志／tool_events 通道**零交集**（T153，D-06 保留 9）。
格式：每行 ts/action/work/card/detail 五键 jsonl（T154）。
打点不设过滤、不设级别开关（T156）——底座动作全量留痕。

九个点位（T152）：append／health_check／损坏处置（damage_repair）／升格（upgrade）／
迁移（migrate）／互斥拒绝（mutex_reject）／人停（human_stop）／投影 rebuild
（projection_rebuild）／迁移窗口起止（migration_window_open/close）。

打点失败不阻断主流程（fail-open + logger.warning）——留痕是档案义务，
不是主流程的闸。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import session_store

logger = logging.getLogger("storage.session_ops")

OPS_FILE = "_ops/session_ops.jsonl"


def ops_path() -> Path:
    """打点文件路径（动态跟随 WORKS_DIR——conftest 沙箱重定向必须生效）。"""
    return session_store.WORKS_DIR / OPS_FILE


def ops_log(action: str, work_id: str, card_id: str,
            detail: Any = None) -> None:
    """追加一条五键打点。任何异常只警告、不抛出、不阻断调用方。"""
    try:
        line = json.dumps({
            "ts": session_store._now_iso(),
            "action": action,
            "work": work_id,
            "card": card_id,
            "detail": detail if detail is not None else {},
        }, ensure_ascii=False)
        p = ops_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            import os
            os.fsync(f.fileno())
    except Exception as exc:  # noqa: BLE001 打点失败不阻断
        logger.warning("[ops] 打点失败 action=%s work=%s card=%s: %s",
                       action, work_id, card_id, exc)
