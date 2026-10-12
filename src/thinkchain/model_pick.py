# -*- coding: utf-8 -*-
"""模型选择（薄包装 · 模型设置第一步收拢）。

原实现读 config/model_pick.json（角色→席位映射）。收拢后：角色→模型绑定
已并入 config/models.yaml 的 bindings 段，本件降级为 model_config 的薄包装，
函数签名不变（消费点 message_runner / simple_service 零改动）。

三级回落语义不变：role → default_model → 常量 DEFAULT_MODEL。
"""
from __future__ import annotations

import logging
from typing import Any

from ..api import model_config

logger = logging.getLogger("thinkchain.model_pick")


def pick_model(role: str = "default") -> str:
    """按角色取模型名。role="default"（无活板）静默落 default_model；
    真实角色命中 bindings 直接返回，未命中留痕落 default_model。
    返回语义与回落顺序不变（原：role 行 → default 行 → 常量 DEFAULT_MODEL）。"""
    if role == "default":
        return model_config.default_model()
    hit = model_config.resolve(role)
    if hit is not None:
        return str(hit)
    logger.warning("模型选择：类型 %s 未配置，落默认模型 %s",
                   role, model_config.default_model())
    return model_config.default_model()


def resolve_model(role: str) -> str:
    """统一入口：角色 → 模型名。未命中/非法（不在 models 键集）回落 default_model。

    单角色返 str；多席（viewpoint）防御性取首个。与 pick_model 不同：本函数
    校验值合法性（非法回落 + 留痕），pick_model 保留原始值让消费点显式报错。
    """
    hit = model_config.resolve(role)
    if hit is None:
        return model_config.default_model()
    if isinstance(hit, list):
        return str(hit[0]) if hit else model_config.default_model()
    name = str(hit)
    if name not in model_config.model_names():
        logger.warning("模型选择：类型 %s 指向非法模型 %s，回落默认 %s",
                       role, name, model_config.default_model())
        return model_config.default_model()
    return name