# -*- coding: utf-8 -*-
"""角色注册表 · 只读读取层。

`config/roles.json` 是「系统有哪些角色、每个角色干什么」的唯一事实源，
本模块是它的唯一读取口：三个纯函数，只读只解析零判断。

- **每次调用现读**（热加载，照 `src/api/card_msg.py` `load_registry` 先例）：
  「加角色＝加一条 JSON、代码零改动」靠这一点成立。
- 坏表/缺文件/形态非法 → `logger.warning` ＋ fail-open 空值
  （`{}` / `None` / `False`），**绝不抛出**——与 card_msg 先例同族。

只管角色；模型相关（角色→模型绑定、模型定义、resolve 解析）归设置侧，不碰。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("roles")

# src/roles.py → 项目根（parents[1]）；表在根下 config/roles.json。
BASE_DIR = Path(__file__).resolve().parents[1]
ROLES_PATH = BASE_DIR / "config" / "roles.json"


def _load_table() -> dict:
    """读角色注册表并返回 `roles` 子表。**每次调用现读**（热加载先例）。

    坏表/缺文件 → `logger.warning` ＋ 返 `{}`（fail-open，不抛）。
    """
    try:
        data = json.loads(ROLES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("角色注册表不存在: %s", ROLES_PATH)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("角色注册表读取失败 %s: %s", ROLES_PATH, exc)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
        logger.warning("角色注册表形态非法（缺 roles 或非对象）: %s", ROLES_PATH)
        return {}
    return data["roles"]


def list_roles() -> dict:
    """全部角色定义 `{role_id: {display, note, multi}}`（保持文件内声明序）。

    坏表/缺文件 → `{}`（fail-open）。
    """
    return _load_table()


def get_role(role_id: str):
    """单角色定义 dict；未命中/形态非对象 → `None`。"""
    entry = _load_table().get(role_id)
    return entry if isinstance(entry, dict) else None


def is_multi(role_id: str) -> bool:
    """该角色 `multi` 是否 true；未命中/缺键/非 true → `False`。"""
    entry = get_role(role_id)
    return bool((entry or {}).get("multi", False))
