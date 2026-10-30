# -*- coding: utf-8 -*-
"""条件集的配置读取（``config/harness_policies.json``）。

**每次现读**，改文件即生效，不重启——和原来 ``harness_rules`` 一样。
坏条目由 ``PolicySet.load`` 标错并跳过，不拖垮整表。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("infra.policy_config")

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "harness_policies.json"


def config_path() -> Path:
    """配置路径（``ECO_POLICIES_PATH`` 可覆盖，测试用）。"""
    env = os.environ.get("ECO_POLICIES_PATH")
    return Path(env) if env else DEFAULT_PATH


def load_doc() -> dict[str, Any]:
    """整份配置（含 ``_meta``）。读不到 ⇒ 空表。"""
    p = config_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("[policy_config] 配置不存在: %s", p)
        return {"policies": []}
    except (ValueError, OSError) as exc:
        logger.warning("[policy_config] 配置读不了 %s: %s", p, exc)
        return {"policies": []}
    if isinstance(raw, list):
        return {"policies": raw}
    return raw if isinstance(raw, dict) else {"policies": []}


def load_raw() -> list[dict[str, Any]]:
    """策略列表（``PolicySet.load`` 吃这个）。"""
    doc = load_doc()
    items = doc.get("policies")
    return [x for x in (items or []) if isinstance(x, dict)]
