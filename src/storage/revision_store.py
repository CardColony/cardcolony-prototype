# -*- coding: utf-8 -*-
"""修订留痕通用接口（迭代一·会话底座 S7-R6 / 4.7，T111；T114 首笔消费）。

`record_revision(entity, change)` ＝ 卡片修改历史先例的通用化——修订事实
追加式留痕、永不改写、永不删除（档案纪律）。

落点：`works/{work}/_ops/revisions.jsonl`（每行 ts/entity/change/detail）。
迭代一的首笔真实消费＝现场升格留痕（T114：entity=session, change=v1_to_v2）。
迭代二消费方：板子修订（R2）、类型注册表变更（R1）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import session_store

logger = logging.getLogger("storage.revision_store")

REVISIONS_FILE = "_ops/revisions.jsonl"


def revisions_path(work_id: str) -> Path:
    return session_store.WORKS_DIR / work_id / REVISIONS_FILE


def record_revision(work_id: str, entity: str, change: str,
                    detail: Any = None) -> bool:
    """追加一条修订留痕。返回是否落盘成功（IO 事实，不抛出）。"""
    try:
        line = json.dumps({
            "ts": session_store._now_iso(),
            "entity": entity,
            "change": change,
            "detail": detail if detail is not None else {},
        }, ensure_ascii=False)
        p = revisions_path(work_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            import os
            os.fsync(f.fileno())
        return True
    except Exception as exc:  # noqa: BLE001 留痕失败不阻断（IO 事实留痕）
        logger.warning("[revision] 留痕失败 entity=%s change=%s work=%s: %s",
                       entity, change, work_id, exc)
        return False
