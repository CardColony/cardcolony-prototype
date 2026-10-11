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


# ---------------------------------------------------------------------------
# [迭代五 S2] 席位级模型配置（E9/E10）。与 pick_model 的分工：
# - pick_model：执行/思考链路既有路由（缺键落 default→main），实现不动；
# - get_override/resolve_model：三席/统合官/自判的**席位级**取模——键不存在
#   与键在值空串是两种形态（R-I9 分级，防默认态每回合刷 warning）；
#   resolve_model 统一消费入口，带 models.yaml 键集校验（R-I5：照
#   simple_service._call_viewpoint 同款 `gw.config.get("models")` 路径），
#   非法值回落 main 不 KeyError。禁直接调 pick_model（缺键落 default→main
#   会把三席全换到 main）。
# ---------------------------------------------------------------------------
def get_override(role: str) -> str | None:
    """查席位级覆盖值。

    - 键不存在（表内无 role）→ `logger.warning` 留痕＋返 `None`（未配置）；
    - 键在但值空串（钦定默认态）→ 返 `None`、不警告（默认态不刷日志）；
    - 键在且值非空 → 返 `str(值)`。
    """
    table = _load_table()
    if role not in table:
        logger.warning("模型选择：席位 %s 未配置（表现有键=%s）",
                       role, sorted(table.keys()))
        return None
    hit = table.get(role)
    return str(hit) if hit else None


def resolve_model(role: str) -> str:
    """统一入口：席位级覆盖 → models.yaml 键集校验 → 缺省/非法回落 `"main"`。

    键集经 `model_service.get_gateway().config.get("models")` 现取（热加载，
    照 simple_service 同款路径）；获取失败/非法值一律 `logger.warning` 留痕
    后回落 main，绝不抛出（消费点三处：triage 自判 / 统合官 / 三席）。
    """
    hit = get_override(role)
    if not hit:
        return "main"
    try:
        from ..api.model_service import get_gateway
        models_cfg = get_gateway().config.get("models") or {}
    except Exception as exc:  # noqa: BLE001 键集获取失败不阻断调用
        logger.warning("模型选择：%s 覆盖值 %s 无法校验键集（%s），回落 main",
                       role, hit, exc)
        return "main"
    if hit not in models_cfg:
        logger.warning("模型选择：%s 配置了非法模型 %s，回落 main", role, hit)
        return "main"
    return hit
