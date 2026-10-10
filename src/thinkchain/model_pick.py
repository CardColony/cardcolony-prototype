# -*- coding: utf-8 -*-
"""模型选择查表（迭代二 S3 / 方案 §4.5·模型选择）。

「恒返 main 的模型选择」（任务书 S3 目标）：查表函数＋
config/model_pick.json（形态参照 models.yaml 同层同构；初始一行
「默认→main」）。本期模型选择是**预留口**：路由挂点位立住、表内只有
一行 default→main——后续扩展（按 role 分席）只改表不改代码。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("thinkchain.model_pick")

BASE_DIR = Path(__file__).resolve().parents[2]
PICK_PATH = BASE_DIR / "config" / "model_pick.json"

_FALLBACK = "main"


def _load_table() -> dict[str, Any]:
    """读表（模块级常量路径——conftest 沙箱不重定向 config/，表是静态配置
    非 IO 产物）。缺表/坏表→空表兜底（pick_model 落 _FALLBACK）。"""
    try:
        data = json.loads(PICK_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_pick 表读取失败 %s: %s", PICK_PATH, exc)
        return {}


def pick_model(role: str = "default") -> str:
    """按角色查表取模型席位名。三级回落：role 行 → default 行 → _FALLBACK；
    返回语义与回落顺序不变（迭代三 S2 只加缺行留痕日志）。"""
    table = _load_table()
    hit = table.get(role)
    if hit:
        return str(hit)
    # [迭代三 S2] 缺行留痕：类型未配置（表内无 role 键）→ 落 default。
    # 只记一行 warning，不改回落顺序与返回值。
    logger.warning("模型选择：类型 %s 未配置落 default（表现有键=%s）",
                   role, sorted(table.keys()))
    name = table.get("default") or _FALLBACK
    return str(name)
