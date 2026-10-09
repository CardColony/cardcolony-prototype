# -*- coding: utf-8 -*-
"""统一权限表与检查关口（THINK-ECO-permgmt · 方案终稿 §2/§4.1 · 任务书 T1）。

忠实对象：
- docs/exploration/THINK-ECO-permgmt-方案终稿-20261008.md（SHA256 C5C8AE96…）
- docs/exploration/THINK-ECO-permgmt-任务书-终稿-20261008.md（T1/T3-T7）
- 交办单《权限管理》T3-T8 续做（2026-10-09，定夺 1/定夺 2）

落点按 p1 实证修正：card_store 形态＝{card_id: meta dict}（与工具层 cards
同形），meta 字段 card_type / parent / project_class（仅根卡）。

[定夺 1 · 权限表值] POLICY 五键＝原方案 trading-workspace/docs/权限管理_落地方案.md
L48-78 **逐字**；唯一偏离＝五键各加 SHADOW_WRITE——原方案五行均无 SHADOW_WRITE，
而任务书 T5 给影子写通道加关口判定，两者相加＝全部已注册分类写不了影子、影子
机制（10-03 上线，72 张影子在盘）整体停摆；判原方案自相矛盾（本方案治权限散落，
不废影子卡），按**保持现状**处理：给位，影子照写。偏离为交办单定夺 1 所定。

[定夺 2 · 查表取键] 查表键＝先取 project_class（仅根卡有，registry 闸 1）、
无则取 card_type。依据：card_type_registry.json L87 逐字「根卡身份由
project_class 承载，project 只是类型名」；实盘 4 张根卡卡面双字段并存；
不改则根卡 card_type="project" 查表得 None → 默认关门连读拒死、整树打不开。

**R-C**：本模块只判权限，不设任何时间/长度/轮次闸。T7 打点在 check /
check_with_scope 唯一判定点埋一处、两态同记（granted true/false），不做
二次判定、不分叉。
"""
from __future__ import annotations

import enum
from enum import Flag
from typing import Any, Mapping

from ..api import prompt_log

__all__ = [
    "Perm",
    "POLICY",
    "DEFAULT_POLICY",
    "check",
    "check_with_scope",
]


class Perm(Flag):
    """权限位（方案终稿 §2/任务书 T1 逐字：三组十三位）。

    卡片操作位七、工具组位四、通道位二；NONE＝空集。
    """

    # A. 卡片操作位
    READ = enum.auto()
    EDIT = enum.auto()
    DELETE = enum.auto()
    CREATE_CHILD = enum.auto()
    LINK = enum.auto()
    UNLINK = enum.auto()
    TREE_SCOPE = enum.auto()  # 树范围：树类操作可伸入子树（见 check_with_scope）

    # B. 工具组位
    SEARCH = enum.auto()
    TOOLS_FILE = enum.auto()
    TOOLS_WEB = enum.auto()
    TOOLS_CARD = enum.auto()

    # C. 通道位
    CARD_PROPOSAL = enum.auto()
    SHADOW_WRITE = enum.auto()

    NONE = 0


# 默认关门：未注册分类 → 全拒（方案 PM-01「默认关门」逐字）。
DEFAULT_POLICY = Perm.NONE

# 按卡片分类的权限表。
# [定夺 1 · 2026-10-09 交办单] 五键值＝原方案 §2.2 L48-78 逐字照填；唯一偏离＝
# 各键加 SHADOW_WRITE（偏离理由见模块 docstring：原方案自相矛盾，按保持现状给位，
# 影子机制不废）。SEARCH/TOOLS_CARD/CARD_PROPOSAL 三位本批无接线点（任务书 T4
# 只明说 FILE×4/WEB×2 过滤），位照填、暂无行为效果。
POLICY: dict[str, Perm] = {
    "rd_project": (
        Perm.READ | Perm.EDIT | Perm.CREATE_CHILD | Perm.LINK | Perm.UNLINK
        | Perm.TREE_SCOPE
        | Perm.SEARCH | Perm.TOOLS_FILE | Perm.TOOLS_WEB | Perm.TOOLS_CARD
        | Perm.CARD_PROPOSAL
        | Perm.SHADOW_WRITE  # 偏离位（定夺 1）：原方案此行无
    ),
    "research_project": (
        Perm.READ | Perm.EDIT | Perm.CREATE_CHILD | Perm.LINK | Perm.UNLINK
        | Perm.TREE_SCOPE
        | Perm.SEARCH | Perm.TOOLS_FILE | Perm.TOOLS_WEB | Perm.TOOLS_CARD
        | Perm.CARD_PROPOSAL
        | Perm.SHADOW_WRITE  # 偏离位（定夺 1）：原方案此行无
    ),
    "sub_project": (
        Perm.READ | Perm.EDIT | Perm.CREATE_CHILD | Perm.DELETE
        | Perm.LINK | Perm.UNLINK | Perm.TREE_SCOPE
        | Perm.SEARCH | Perm.TOOLS_FILE | Perm.TOOLS_WEB | Perm.TOOLS_CARD
        | Perm.SHADOW_WRITE  # 偏离位（定夺 1）：原方案此行无
    ),
    "topic": (
        Perm.READ | Perm.EDIT | Perm.CREATE_CHILD | Perm.DELETE
        | Perm.LINK | Perm.UNLINK | Perm.TREE_SCOPE
        | Perm.SEARCH | Perm.TOOLS_FILE | Perm.TOOLS_WEB | Perm.TOOLS_CARD
        | Perm.SHADOW_WRITE  # 偏离位（定夺 1）：原方案此行无
    ),
    "tool": (
        Perm.READ | Perm.EDIT | Perm.DELETE
        | Perm.SEARCH | Perm.TOOLS_CARD
        | Perm.SHADOW_WRITE  # 偏离位（定夺 1）：原方案此行无
        # 无 CREATE_CHILD（工具卡不可有子卡）、无 TREE_SCOPE、无 TOOLS_FILE/WEB
    ),
}

# 树类动作（原方案 §3.2 代码逐字）：动作属这五者、且 target 分类授权含
# TREE_SCOPE 时，check_with_scope 才追加 _is_in_subtree 作用域限定。
_TREE_ACTIONS = frozenset(
    {Perm.EDIT, Perm.DELETE, Perm.LINK, Perm.UNLINK, Perm.CREATE_CHILD}
)


def _type_key(card: Mapping[str, Any] | None) -> str:
    """查表键（定夺 2）：先取 project_class（仅根卡有），无则取 card_type。"""
    card = card or {}
    return card.get("project_class") or card.get("card_type", "")


def grants_for(type_key: str) -> Perm:
    """查分类授权：未注册分类走 DEFAULT_POLICY（默认关门）。"""
    return POLICY.get(type_key, DEFAULT_POLICY)


def _bit_names(perms: Perm) -> str:
    """授权位的机械投影（仅用于拒绝文案，不做任何二次判定）。"""
    return "|".join(
        name for name, bit in Perm._member_map_.items() if bit and perms & bit
    ) or "NONE"


def check(card_id: str, action: Perm, card_store: Mapping[str, dict[str, Any]] | None) -> bool:
    """统一权限关口（方案终稿 §2 接口：返回 bool、拒绝抛 PermissionError）。

    取卡 → 查表键（定夺 2）→ POLICY 授权 → 判 action 在位。
    拒绝情形（分类未注册 / 位不在授权内 / 卡不存在）统一走唯一抛出路径；
    放行返回 True。T7 打点在唯一判定点，两态同记。
    """
    card = (card_store or {}).get(card_id)
    type_key = _type_key(card)
    granted = grants_for(type_key)
    granted_flag = (granted & action) == action
    prompt_log.append_perm_log({
        "fn": "check",
        "card_id": card_id,
        "action": action.name,
        "type_key": type_key,
        "granted": granted_flag,
    })
    if not granted_flag:
        raise PermissionError(
            f"[perm] denied card={card_id} action={action.name} "
            f"type_key={type_key!r} granted={_bit_names(granted)}"
        )
    return True


def check_with_scope(
    actor_id: str,
    target_id: str,
    action: Perm,
    card_store: Mapping[str, dict[str, Any]] | None,
) -> bool:
    """带作用域的统一关口：先过 check，再按原方案 §3.2 叠加树作用域。

    树类动作（EDIT/DELETE/LINK/UNLINK/CREATE_CHILD）且 target 分类授权含
    TREE_SCOPE 时，追加 _is_in_subtree(actor, target) 限定——actor 不在
    target 的血统上游即拒。拒绝抛 PermissionError；放行返回 True。
    T7 打点：基础判定由 check 记一条，本函数作用域阶段另记一条。
    """
    check(target_id, action, card_store)
    scope_applied = False
    scope_ok = True
    if action in _TREE_ACTIONS:
        target = (card_store or {}).get(target_id) or {}
        if Perm.TREE_SCOPE in grants_for(_type_key(target)):
            scope_applied = True
            scope_ok = _is_in_subtree(actor_id, target_id, card_store)
    prompt_log.append_perm_log({
        "fn": "check_with_scope",
        "actor_id": actor_id,
        "target_id": target_id,
        "action": action.name,
        "scope_applied": scope_applied,
        "granted": scope_ok,
    })
    if not scope_ok:
        raise PermissionError(
            f"[perm] denied card={target_id} action={action.name} "
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
