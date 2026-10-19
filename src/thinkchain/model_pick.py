# -*- coding: utf-8 -*-
"""模型选择（薄包装 · 模型设置第一步收拢）。

原实现读 config/model_pick.json（角色→席位映射）。收拢后：角色→模型绑定
已并入 config/models.yaml 的 bindings 段，本件降级为 model_config 的薄包装，
函数签名不变（消费点 message_runner / simple_service 零改动）。

三级回落语义不变：role → default_model → 常量 DEFAULT_MODEL。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..api import model_config

logger = logging.getLogger("thinkchain.model_pick")


def pick_model(role: str = "default", *, work_id: Optional[str] = None,
               card_id: Optional[str] = None) -> str:
    """按角色取模型名。role="default"（无活板）静默落 default_model；
    真实角色命中 bindings 直接返回，未命中留痕落 default_model。
    返回语义与回落顺序不变（原：role 行 → default 行 → 常量 DEFAULT_MODEL）。

    [模型设置按项目/卡片覆盖 2026-10-19] work_id / card_id 可选：传了就按
    card > work > global 的优先序解析（model_config.resolve 内完成）；不传 =
    全局（既有行为逐字不变）。
    """
    if role == "default":
        return model_config.default_model(work_id=work_id, card_id=card_id)
    hit = model_config.resolve(role, work_id=work_id, card_id=card_id)
    if hit is not None:
        return str(hit)
    logger.warning("模型选择：类型 %s 未配置，落默认模型 %s",
                   role, model_config.default_model(work_id=work_id, card_id=card_id))
    return model_config.default_model(work_id=work_id, card_id=card_id)


def resolve_model(role: str, *, work_id: Optional[str] = None,
                  card_id: Optional[str] = None) -> str:
    """统一入口：角色 → 模型名。未命中/非法（不在 models 键集）回落 default_model。

    单角色返 str；多席（viewpoint）防御性取首个。与 pick_model 不同：本函数
    校验值合法性（非法回落 + 留痕），pick_model 保留原始值让消费点显式报错。
    work_id / card_id 可选：传了走 card>work>global 优先序覆盖。
    """
    hit = model_config.resolve(role, work_id=work_id, card_id=card_id)
    if hit is None:
        return model_config.default_model(work_id=work_id, card_id=card_id)
    if isinstance(hit, list):
        return str(hit[0]) if hit else model_config.default_model(work_id=work_id, card_id=card_id)
    name = str(hit)
    if name not in model_config.model_names():
        logger.warning("模型选择：类型 %s 指向非法模型 %s，回落默认 %s",
                       role, name, model_config.default_model(work_id=work_id, card_id=card_id))
        return model_config.default_model(work_id=work_id, card_id=card_id)
    return name