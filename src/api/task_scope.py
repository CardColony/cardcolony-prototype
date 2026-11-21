# -*- coding: utf-8 -*-
"""任务范围判定（模型调用成本优化任务书 §三·第二步「按可信任务范围减少正文」）。

**只用宿主已有的结构化字段**判本轮的任务范围：回合来源（source/source_kind）、
身份锚（act_as/ctx_card）、卡型、直接子卡的卡型、差事账对象、活板当前步判据。
**不解析正文**——任务书明令「正文里的卡号只供查漏」，本模块一个正文卡号都不认。

三档（任务书 §三·第二步的表）：

- ``single``  单卡委派／专项验收：对象卡可指名 ⇒ 清单与子卡材料按对象卡收窄；
              依赖正稿按「能定位就给相关条款＋定位，不能定位才给完整正稿」。
- ``global``  全局统筹：本卡是管辖者 ⇒ 全部直接子卡给简表（卡号·标题·活性）；
              依赖正稿只给关系坐标＋版本/可回读定位（不再整份内联）。
- ``unknown`` 任务范围、版本或回读条件不明确 ⇒ 沿用完整材料（安全回退）。

**判不出来一律回退 unknown**：缩量省的是钱，判错丢的是事实——宁多不少。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "MODE_SINGLE",
    "MODE_GLOBAL",
    "MODE_UNKNOWN",
    "TaskScope",
    "resolve_scope",
    "EXEC_CARD_TYPES",
    "DOMAIN_CARD_TYPES",
    "BINDING_CARD",
    "BINDING_JURISDICTION",
]

MODE_SINGLE = "single"
MODE_GLOBAL = "global"
MODE_UNKNOWN = "unknown"

#: 执行型卡（自己做一件事；子卡是它派出去的交付件）——单卡专项的典型对象。
EXEC_CARD_TYPES = frozenset({"worker", "ui", "qa_card", "pm_card", "tool"})

#: 领域型卡（自己是一片领域；子卡是子领域，各有各的判据）——统筹者的典型形态。
DOMAIN_CARD_TYPES = frozenset({"project", "sub_project", "topic"})

#: 既非领域也非执行（职能/角色/评审/规则/对齐）——不计入管辖范围统计。
OTHER_CARD_TYPES = frozenset({"function", "role", "review", "rule", "align"})

#: **本轮可信任务绑定**的两种形态（谁在定范围）：
#: - ``card``：范围＝一张对象卡（单卡专项/委派/验收）；
#: - ``jurisdiction``：范围＝某张卡的管辖范围（全局统筹）。
BINDING_CARD = "card"
BINDING_JURISDICTION = "jurisdiction"

#: 活板判据里的条款号（A2／R10／S9-R01…）——只在本模块内当锚抽出来用。
_ANCHOR_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Z]{1,4}\d{1,3}"
    r"(?:[-–][A-Z]{0,4}\d{1,3})*"
    r"(?![0-9])")

#: 其他来源的回合**不在本批缩量范围**内（后续批次各自处理）：评审席（批三）、
#: 场授回合、职能/角色席位。判据＝回合来源标记，不是猜的。
_OUT_OF_SCOPE_SOURCE_KINDS = frozenset({"review_step", "align_turn"})


@dataclass(frozen=True)
class TaskScope:
    """本轮的任务范围判定结果（纯数据，渲染器只读）。"""

    mode: str = MODE_UNKNOWN
    #: single 档的对象卡号（空串＝对象就是本卡）。
    target_card: str = ""
    #: 判据来源标签（可复查：这个档位凭什么判出来的）。
    basis: str = ""
    #: 人读说明（装配日志/回报里能对上号的原话）。
    reason: str = ""
    #: 本轮能否按行段回读原文（本轮工具面里有没有 read_card）。
    readback_available: bool = True
    #: 判据锚（条款号）——由关系说明/活板判据抽出，交给定位器用。
    anchors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def narrowed(self) -> bool:
        """是否走缩量路径（false ⇒ 完整材料，逐字同旧）。"""
        return self.mode in (MODE_SINGLE, MODE_GLOBAL)

    @property
    def is_single(self) -> bool:
        return self.mode == MODE_SINGLE

    @property
    def is_global(self) -> bool:
        return self.mode == MODE_GLOBAL


def _child_types(children: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """直接子卡按卡型分两堆：领域子卡（统筹的证据）��执行件（交付件）。"""
    domain: list[str] = []
    execs: list[str] = []
    for ch in children or []:
        cid = str(ch.get("card_id") or "")
        if not cid:
            continue
        ctype = str(ch.get("card_type") or "")
        if ctype in DOMAIN_CARD_TYPES:
            domain.append(cid)
        elif ctype in EXEC_CARD_TYPES:
            execs.append(cid)
    return domain, execs


def resolve_scope(*, card_id: str, card_type: str,
                  children: Optional[list[dict[str, Any]]] = None,
                  source: Optional[dict[str, Any]] = None,
                  source_kind: str = "",
                  act_as: str = "",
                  ctx_card: str = "",
                  dispatch_targets: Optional[list[str]] = None,
                  board_criteria: str = "",
                  readback_available: bool = True,
                  task_binding: Optional[dict[str, Any]] = None,
                  own_task: bool = False) -> TaskScope:
    """判定本轮任务范围。**纯判定**：吃装配层已取好的数据，不再查库。

    [返工 2026-11-20 · 复核算二] **范围只能由「本轮可信任务绑定」判出来**：
    卡型与子树**不再**用于推断档位（那会把「有领域子卡的管理卡的普通聊天」
    错判成全局统筹），它们只用于确定**已证实范围内有哪些卡**。

    绑定三来源（按优先级，先显式后派生）：

    1. **显式绑定**：正式提交的 `source.task` 或回合入口给的 `task_binding`。
       ``{"kind": "card", "card_id": X}``（对象卡）或
       ``{"kind": "jurisdiction", "card_id": Y}``（管辖范围）。
    2. **差事账绑定**（`dispatch_targets`）：本轮在派差事且绑定唯一
       ``target_card`` ⇒ 对象＝该卡（**任意卡型都算**——不再按 qa/pm 过滤）。
    3. **本卡正在推进目标**（`own_task`：有目标标识的目标续轮）
       ⇒ 范围＝本卡的管辖范围。

    **没有绑定 ⇒ unknown（完整材料，逐字同旧）**。
    """
    src = dict(source or {})
    # ① 非普通主对话 ⇒ 不缩量
    if act_as and act_as != card_id:
        return TaskScope(MODE_UNKNOWN, basis="venue_seat",
                         reason="席位/场授回合：身份锚与执行卡不同，本批不缩量")
    if str(src.get("kind") or "") in _OUT_OF_SCOPE_SOURCE_KINDS:
        return TaskScope(MODE_UNKNOWN, basis=str(src.get("kind") or ""),
                         reason=f"{src.get('kind')} 回合：另有批次处理，本批不缩量")
    if ctx_card and ctx_card != card_id:
        return TaskScope(MODE_UNKNOWN, basis="ctx_card",
                         reason="上下文锚≠执行卡（场务/评审语境），本批不缩量")

    b = dict(task_binding or {})
    task = src.get("task")
    criteria = board_criteria
    if isinstance(task, dict) and (task.get("task_id") or task.get("id")):
        target = str(task.get("target") or "").strip()
        criteria = str(task.get("accept") or "")
        if not re.fullmatch(r"card_\d+", target):
            return TaskScope(MODE_UNKNOWN, basis="unbound_task_target",
                             reason="已提交任务缺少可定位的目标卡，保留完整材料")
        kind = (BINDING_JURISDICTION
                if task.get("scope") == BINDING_JURISDICTION and target == card_id
                else BINDING_CARD)
        b = {"kind": kind, "card_id": target}
    anchors = tuple(_ANCHOR_RE.findall(criteria or ""))[:24]

    # ② 显式任务绑定
    bkind = str(b.get("kind") or "")
    bcard = str(b.get("card_id") or "")
    if bcard and (bkind == BINDING_CARD
                  or (bkind == BINDING_JURISDICTION and bcard == card_id)):
        if bkind == BINDING_CARD:
            return TaskScope(MODE_SINGLE, target_card=bcard,
                             basis="explicit_binding_card",
                             reason=f"回合显式绑定对象卡 {bcard}",
                             readback_available=readback_available,
                             anchors=anchors)
        return TaskScope(MODE_GLOBAL, target_card=bcard,
                         basis="explicit_binding_jurisdiction",
                         reason=f"回合显式绑定管辖范围 {bcard}",
                         readback_available=readback_available,
                         anchors=anchors)

    # ③ 差事账绑定：唯一绑定到**验收对象**的结构化字段（任意卡型）
    targets = [str(t) for t in (dispatch_targets or []) if str(t or "")]
    uniq = sorted(set(targets))
    if len(uniq) == 1:
        return TaskScope(MODE_SINGLE, target_card=uniq[0],
                         basis="dispatch_target",
                         reason=f"差事账绑定唯一验收对象 {uniq[0]}",
                         readback_available=readback_available,
                         anchors=anchors)
    if len(uniq) > 1:
        return TaskScope(MODE_UNKNOWN, basis="dispatch_ambiguous",
                         reason=f"差事账绑定了多个对象（{len(uniq)} 个），范围不可证明")

    # ④ 目标续轮 ⇒ 本卡管辖范围
    if own_task:
        return TaskScope(MODE_GLOBAL, target_card=card_id,
                         basis="own_task",
                         reason="本轮从本卡的目标续接，按本卡管辖范围选材",
                         readback_available=readback_available,
                         anchors=anchors)

    # ⑤ 没有可信绑定 ⇒ 完整材料（普通聊天等）
    return TaskScope(MODE_UNKNOWN, basis="no_task_binding",
                     reason="本轮没有可信任务绑定（普通聊天等）⇒ 保留完整材料")
