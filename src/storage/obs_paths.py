# -*- coding: utf-8 -*-
"""观测产物路径（照抄 dsh `gen3/context/obs-paths.ts` 的核心：**按实例派生**）。

## 为什么有这个模块（dsh 原注的事故教训，逐字保留其意）

> 此前所有观测产物都用**硬编码绝对路径**，导致：**所有实例（生产 / 测试 / 任意 worktree
> 实例）的日志全部写进同一个文件**。后果：排查时日志里混着多个实例的报错，**无法分辨
> "谁写的"**——确认写入者本身就要花掉大量时间。

## 修法

根目录**按实例派生**：`<LOGS_DIR>/obs/<实例标签>/`。每个实例天然写自己目录下的文件。

**实例标签**（优先级从高到低）：
1. 环境变量 `ECO_INSTANCE_TAG`（显式指定，测试/多实例跑用）；
2. 环境变量 `PORT` / `ECO_PORT`（uvicorn 端口，如 5700 / 5717）；
3. 进程 id（兜底）。

## 本模块做什么

- `obs_root()` 及各产物路径（monitor / dump / board / stage log / evidence index）；
- `append_stage_line()`：**阶段打点**（dsh `StageRecord` 的最小形态）——一行 JSON，
  失败只记日志、绝不阻断业务（观测不得拖垮主链路）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("storage.obs")

#: 观测根（相对 logs/）；与 dsh `obsRoot()` 同义
_OBS_DIRNAME = "obs"

#: 阶段打点的目录名
_STAGE_DIRNAME = "stage"


def _instance_tag() -> str:
    """实例标签（env 显式 → 端口 → pid）。"""
    for key in ("ECO_INSTANCE_TAG",):
        val = os.environ.get(key, "").strip()
        if val:
            return _safe(val)
    for key in ("ECO_PORT", "PORT"):
        val = os.environ.get(key, "").strip()
        if val:
            return f"port{_safe(val)}"
    return f"pid{os.getpid()}"


def _safe(text: str) -> str:
    """标签只留字母数字与下划线（防路径注入）。"""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(text))[:32]


def _logs_dir() -> Path:
    """日志根——**调用时解引用**（沙箱/测试重定向自动跟随，同项目既有手法）。

    首选 `src.api.prompt_log.LOGS_DIR`（conftest 的沙箱就是 patch 它）；取不到时回落
    `session_store.BASE_DIR/logs`。延迟 import：`src.api` 包会拉起 routes，模块级 import 必成环。
    """
    try:
        from ..api import prompt_log
        return Path(prompt_log.LOGS_DIR)
    except Exception:  # noqa: BLE001 回落（例如 api 包不可用）
        from . import session_store
        return Path(session_store.BASE_DIR) / "logs"


def obs_root() -> Path:
    """观测根：`<LOGS_DIR>/obs/<实例标签>/`（目录自愈）。"""
    p = _logs_dir() / _OBS_DIRNAME / _instance_tag()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001 目录建不出来也不阻断
        logger.warning("[obs] 观测目录创建失败 %s: %s", p, exc)
    return p


def obs_instance_tag() -> str:
    return _instance_tag()


def obs_monitor_path(name: str) -> Path:
    """monitor 产物（usage / shadow / condense batch / depth / gate …）。"""
    d = obs_root() / "monitor"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(name)}.jsonl"


def obs_dump_path(name: str = "dump") -> Path:
    d = obs_root() / "dump"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(name)}.jsonl"


def obs_board_root() -> Path:
    d = obs_root() / "board"
    d.mkdir(parents=True, exist_ok=True)
    return d


def obs_present_memory_path(session_id: str) -> Path:
    d = obs_root() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(session_id)}.json"


def obs_evidence_index_path(session_id: str) -> Path:
    d = obs_root() / "evidence-index"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(session_id)}.json"


def obs_stage_log_path(session_id: str) -> Path:
    """阶段打点日志（按会话分文件；空 id → `_global`）。"""
    d = obs_root() / _STAGE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(session_id) or '_global'}.jsonl"


def append_stage_line(session_id: str, stage: str, direction: str,
                      payload: Optional[dict[str, Any]] = None) -> bool:
    """写一行阶段打点（dsh `StageRecord` 最小形态）。

    形如 `{"ts":"…","sid":"…","stage":"tool.memory_correct","dir":"in","…":…}`。
    **失败只记日志、绝不抛**——观测不得拖垮主链路。
    """
    rec = {
        "ts": datetime.now().astimezone().isoformat(),
        "sid": str(session_id or ""),
        "stage": str(stage or ""),
        "dir": str(direction or ""),
    }
    if isinstance(payload, dict):
        rec.update(payload)
    try:
        path = obs_stage_log_path(session_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:  # noqa: BLE001
        logger.warning("[obs] 阶段打点写入失败 stage=%s: %s", stage, exc)
        return False
