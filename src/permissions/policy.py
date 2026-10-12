# -*- coding: utf-8 -*-
"""卡片权限底座（重做 2026-10-12 · 任务书《权限底座重做》v2）。

**两级定位**：``project_class``（工牌，仅根卡承载、同树子卡继承）＋ ``card_type``
（卡类型）→ 一组权限开关。**权限表不在代码里**，在 ``config/permissions.json``。

[用户裁定 1-7 · 2026-10-12 逐条落地]

    1. 权限＝按卡片类型分配，两级定位（project_class ＋ card_type）。
    2. **类型不可变**：建卡定死；改 card_type / project_class 由 card_store
       存储层拒（本模块不参与——它只在**算怎么发权**时才被调到）。
    3. **可配置**：加类型＝改 permissions.json，本文件零改动。
    4. **固化 ＋ 版本刷新**：建卡时算一次写进卡的 ``perms``，同时记
       ``perms_version``（当时配置的 version）；**运行时只读卡上的 perms，
       不读配置文件**。改配置＝version+1 ＋ 跑 scripts/refresh_perms.py。
    5. **默认关门**：配置里没配到的组合＝空列表＝全拒。
    6. **开关可扩展**：开关词汇表＝本文件的常量集合 ``SWITCHES``；加开关＝
       代码加一个常量 ＋ 配置里用起来。配置里出现不在词汇表内的开关＝无效
       （丢弃并留 warning，不静默生效）。
    7. edit 本次保留（未来会收走）。

**树作用域（防越界）独立于类型分权**（方案 §4）：``check`` 先过类型分权，
再对 ``TREE_SWITCHES``（edit/delete/link/unlink/create_child）判「操作者是否在
目标上游」——独立函数 ``_is_in_subtree``，不含任何权限语义。

**R-C**：本模块只判权限，不设任何时间/长度/轮次闸。打点在唯一判定点，
两态同记（granted true/false），不做二次判定、不分叉。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from ..api import prompt_log

__all__ = [
    "SWITCHES",
    "TREE_SWITCHES",
    "PERMISSIONS_PATH",
    "PERM_KEY",
    "PERM_VERSION_KEY",
    "perms_for",
    "config_version",
    "compute_perms",
    "has_perm",
    "check",
    "_is_in_subtree",
]

logger = logging.getLogger("permissions.policy")


# ============================================================
# 开关词汇表（代码里定义的常量集合；加开关＝在此加一个常量）
# ============================================================

SWITCH_READ = "read"                    # 能读
SWITCH_EDIT = "edit"                    # 能改（未来会收走）
SWITCH_DELETE = "delete"                # 能删
SWITCH_CREATE_CHILD = "create_child"    # 能在它下面建子卡
SWITCH_LINK = "link"                    # 能连线
SWITCH_UNLINK = "unlink"                # 能解线
SWITCH_TOOLS_FILE = "tools_file"        # 能读写文件
SWITCH_TOOLS_WEB = "tools_web"          # 能联网
SWITCH_SHADOW_WRITE = "shadow_write"    # 能写自己的影子卡
SWITCH_MANUSCRIPT = "manuscript"        # 能查阅本项目存档的原始资料（manuscript 三件）
SWITCH_INVOKE_TOOL = "invoke_tool"      # 能调用工具卡（run_tool_card）
SWITCH_PLAN = "plan"                    # 能建计划、改计划、推进步骤（思维链六件）

# [工具面补齐 2026-10-12] 删掉了原 SWITCH_SEARCH="search"：
#   全系统**没有独立的 search 工具**——搜索能力由 web_search/fetch_web 承担，
#   它们归 SWITCH_TOOLS_WEB。那是个没有任何工具消费的**死开关**，留在配置里
#   只会让人误以为「配了 search 就能用搜索」。删除后配置里的 search 值会被
#   perms_for 当未知开关丢弃并留 warning（存量卡刷新后自动消掉）。

#: 全部合法开关（配置里的值必须是它的子集，否则该开关无效）
SWITCHES: frozenset[str] = frozenset({
    SWITCH_READ,
    SWITCH_EDIT,
    SWITCH_DELETE,
    SWITCH_CREATE_CHILD,
    SWITCH_LINK,
    SWITCH_UNLINK,
    SWITCH_TOOLS_FILE,
    SWITCH_TOOLS_WEB,
    SWITCH_SHADOW_WRITE,
    SWITCH_MANUSCRIPT,
    SWITCH_INVOKE_TOOL,
    SWITCH_PLAN,
})

#: 树类开关：类型分权通过后追加「操作者是否在目标上游」限定（方案 §4）
TREE_SWITCHES: frozenset[str] = frozenset({
    SWITCH_EDIT,
    SWITCH_DELETE,
    SWITCH_LINK,
    SWITCH_UNLINK,
    SWITCH_CREATE_CHILD,
})


# ============================================================
# 配置读取
# ============================================================

#: 权限表路径（模块级常量 ⇒ 测试可 monkeypatch 隔离，与 card_store
#: TYPE_REGISTRY_PATH 同一套手法）
PERMISSIONS_PATH = Path(__file__).resolve().parents[2] / "config" / "permissions.json"

#: 卡面上的固化字段名（card_store 落盘/渲染与本模块读 perms 共用同一对名字，
#: 两处不得各写一套字符串字面量）
PERM_KEY = "perms"
PERM_VERSION_KEY = "perms_version"


def _load_config() -> dict[str, Any]:
    """读权限配置（每次现读，只被「算 perms」的路径调用）。

    读不到/坏 JSON ⇒ 空表 ⇒ 默认关门（不发任何权限）。
    """
    try:
        data = json.loads(PERMISSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("权限配置不可用（默认关门）: %s: %s", PERMISSIONS_PATH, exc)
        return {}
    return data if isinstance(data, dict) else {}


def config_version() -> int:
    """当前权限配置的 version（写进卡的 perms_version）。配置不可用＝0。"""
    raw = _load_config().get("version", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def perms_for(project_class: str, card_type: str) -> list[str]:
    """按两级定位查权限开关列表。**默认关门：查不到＝空列表**。

    根卡查 ``permissions[project_class]["project"]``；子卡查
    ``permissions[project_class][card_type]``。

    返回值为 ``SWITCHES`` 的子集：不在词汇表内的配置值会被丢弃并留 warning
    （加开关＝代码加常量＋配置用起来；只改配置不生效是**有意**的）。
    """
    table = _load_config().get("permissions") or {}
    row = table.get(str(project_class or ""))
    if not isinstance(row, Mapping):
        return []
    raw = row.get(str(card_type or ""))
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item)
        if s in SWITCHES:
            out.append(s)
        else:
            logger.warning(
                "权限配置含未知开关（已丢弃）: %s/%s → %r",
                project_class, card_type, item,
            )
    return out


def compute_perms(project_class: str, card_type: str) -> tuple[list[str], int]:
    """算出这张卡应有的 ``(perms, perms_version)``——**只在建卡/刷新时调用**。"""
    return perms_for(project_class, card_type), config_version()


# ============================================================
# 运行时判定（读卡上固化的 perms，不读配置）
# ============================================================

def has_perm(card_meta: Mapping[str, Any] | None, switch: str) -> bool:
    """这张卡是否被授予 ``switch``？

    读**卡上固化的** ``perms`` 列表（[裁定 4] 运行时照读、不现查配置）。

    False 的四种情形（全部＝默认关门，不区分、不回落）：
      - 卡不存在 / meta 为 None
      - 卡上没有 perms（从未固化过）
      - perms 里不含该开关
      - switch 不在 ``SWITCHES`` 词汇表内（不存在的开关谈不上授权）
    """
    if str(switch) not in SWITCHES:
        return False
    meta = card_meta or {}
    perms = meta.get(PERM_KEY)
    if not isinstance(perms, (list, tuple, set, frozenset)):
        return False
    return str(switch) in {str(s) for s in perms}


def _deny_message(card_id: str, switch: str,
                  card_meta: Mapping[str, Any] | None) -> str:
    meta = card_meta or {}
    granted = meta.get(PERM_KEY)
    shown = ("|".join(sorted(str(s) for s in granted))
             if isinstance(granted, (list, tuple, set, frozenset)) else "NONE")
    return (
        f"[perm] denied card={card_id} action={switch} "
        f"card_type={meta.get('card_type', '')!r} perms={shown}"
    )


def check(actor_id: str, target_id: str, switch: str,
          cards: Mapping[str, dict[str, Any]] | None) -> bool:
    """统一权限关口：类型分权 →（树类开关）树作用域。通过 True，拒绝抛 PermissionError。

    ``cards`` 为卡面 meta 表（``{card_id: meta}``）。两级失败的文案各自独立
    （「分类不授权」/「越出子树」），便于模型分辨。

    打点：类型分权一条（fn="check"）、树作用域命中时再一条（fn="tree_scope"），
    两态同记（granted true/false）。
    """
    cards = cards or {}
    target = cards.get(target_id)
    allowed = has_perm(target, switch)
    prompt_log.append_perm_log({
        "fn": "check",
        "actor_id": actor_id,
        "target_id": target_id,
        "switch": switch,
        "granted": allowed,
    })
    if not allowed:
        raise PermissionError(_deny_message(target_id, switch, target))

    if switch in TREE_SWITCHES:
        scope_ok = _is_in_subtree(actor_id, target_id, cards)
        prompt_log.append_perm_log({
            "fn": "tree_scope",
            "actor_id": actor_id,
            "target_id": target_id,
            "switch": switch,
            "granted": scope_ok,
        })
        if not scope_ok:
            raise PermissionError(
                f"[perm] denied card={target_id} action={switch} "
                f"scope: actor={actor_id} 不在 target={target_id} 的子树上游"
            )
    return True


def _is_in_subtree(
    actor_id: str,
    target_id: str,
    cards: Mapping[str, dict[str, Any]] | None,
) -> bool:
    """target 是否落在 actor 的子树内（含 actor 自身）。

    与收口前 card_tools 写侧子树判据（原 L877，已删）逐字同口径——R3 等价
    验证 27,605 对 0 破例（docs/exploration/THINK-ECO-permgmt-R3-等价验证-
    20261009_0010.md）：自指直真；沿 target 的 parent 链上溯找 actor；
    visited 防环、悬空 parent 断链、guard<1000，全部同源。

    [重做 2026-10-12] 从权限开关里**拆出**，成为独立的树作用域函数——
    它不含任何类型分权语义；未来 edit 收走后，这一层可整层删除。
    """
    if target_id == actor_id:
        return True
    cards = cards or {}
    tgt = cards.get(target_id)
    if tgt is None:
        return False
    visited: set[str] = set()
    node = tgt.get("parent", "")
    guard = 0
    while node and guard < 1000:
        guard += 1
        if node in visited:
            break
        visited.add(node)
        if node == actor_id:
            return True
        nm = cards.get(node)
        if nm is None:
            break
        node = nm.get("parent", "")
    return False
