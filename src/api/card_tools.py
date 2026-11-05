# -*- coding: utf-8 -*-
"""卡片工具与权限（白板版 2026-10-02）。

工具接口只含机械事实：
- read_card：返回标题、父卡、连线、三个内容块——不暴露内部四区语义
- edit_card(card_id, content)：覆盖写指定卡三个内容块（内部落存储层三个块列）
- link_cards：通信线（卡上 links），模型可用类型限 先后/关联（锚定为系统内部关系，不对模型开放）
- link_rel：依赖关联线（卡上 relations，第三种线），kind 限 依赖/关联；**不参与通信**

[权限收口 2026-10-09 → 权限底座重做 2026-10-12] 权限统一走 src/permissions/policy：
PERMS 来自**卡上固化的 perms 字段**（两级定位 project_class＋card_type 在
config/permissions.json 里的配置，建卡时算一次写死），统一关口 policy.check
（类型分权 → 树类开关再叠树作用域）；读/写/建/删/连线/解线/影子各通道全部经
关口判定。越权不抛给模型，返回 {"error": ...}。原「读＝全盘可读、写＝自己的
子树」两道散置判据（2026-10-07/10-08 口径）已收口退役。

[建卡工具化 2026-10-08] create_card 入标准工具通道（此前只走文本协议块
card-proposal，见 card_proposal.py）。两条通道并存：工具通道优先、文本通道
兜底（模型过渡期仍写文本块），跨通道同轮去重防重复建卡。
**建卡权限口径（代码现状）＝关口 CREATE_CHILD 判定**：只能建自己的子卡片
（parent 缺省＝当前卡自身，即建自己的下一层；显式 parent 须经关口放行）。

[B-09 工具注册表 2026-10-08] 本文件改为「单点注册表派生」形态：每个工具一处
注册（schema 字面量 ＋ handler ＋ readonly 标志住在一起）。三处下游全部从
TOOL_REGISTRY 派生，不再手工维护：build_tool_schemas（工具面）、execute_tool
（查表调用）、READONLY_TOOLS（只读白名单，原漂移源）。**工具面逐字零变化**
（name/description/parameters 一字未改，TOOL_TABLE_VERSION 不动——工具面零变化
⇒ 版本不动，这是纪律不是遗漏）。改的只是内部组织形态。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Union

from ..permissions import policy
from ..storage import (card_db, card_store, manuscript_store, memory_store,
                       function_store, role_store, shadow_store, work_store)
from ..storage import event_log, session_facade, session_ops
from ..storage.hash_utils import parse_front_matter, split_body_sections

from ..goal import tools as goal_tools
from ..infra import policy_tools as _policy_tools
from . import card_msg, context_regions, credential_guard, event_stream, role_room
from ..toolcard import driver as toolcard_driver
from ..toolcard import script_loader as toolcard_script_loader
from ..toolcard import step_prompt as toolcard_step_prompt
from ..thinkchain import plan_tools as thinkchain_plan_tools
from . import file_tools, prompt_log
from .tool_doc import ToolDoc, ToolEffect

logger = logging.getLogger("api.card_tools")

# [白板版 2026-10-02] 模型可建的**通信线**类型收窄为两个中性关系；
# "锚定"（系统派生目标卡专用）与"兑现"（写作agent残留语义）不再对模型开放
MODEL_REL_TYPES = ("先后", "关联")

# [依赖关联线 2026-10-16] 模型可建的**依赖关联线** kind（第三种线 relations）。
# 与通信线的 type 是两个字段上的概念，重名不影响（任务书 §二.12）。
MODEL_DEP_KINDS = ("依赖", "影响")

# [更新现状按钮改造 2026-10-16] 「更新现状」专用操作回合的操作标记
# （POST …/messages 请求体的 op 字段）。它不是权限开关，是**本轮操作类型**：
#   message_runner 认它 → 轮首注入专用任务（current_turn.build_status_task_event）；
#   build_tool_schemas 认它 → 把 OP_ONLY_TOOLS 临时放进本轮工具面。
OP_UPDATE_STATUS = "update_status"

# [需求确定按钮改造 2026-10-29 · 用户令] 「需求确定」按钮＝一次带 op 的专用回合，
# 按卡类型分两种，注入的提示词与流程都不同：
#   OP_REQUIREMENTS_FINAL（根卡）—— 这一轮给根卡写需求文档**正稿**的口子。
#     根卡开不出评审（评审只对 sub_project/topic 开），也不需要评审——它的评审人就是用户。
#   OP_REQUIREMENTS_DRAFT（其他卡）—— 这一轮拟一版草稿，拟完开评审（草稿转正走评审）。
OP_REQUIREMENTS_FINAL = "requirements_final"
OP_REQUIREMENTS_DRAFT = "requirements_draft"
REQUIREMENTS_OPS: frozenset[str] = frozenset(
    {OP_REQUIREMENTS_FINAL, OP_REQUIREMENTS_DRAFT})

# [更新现状按钮改造 2026-10-16] **非卡片内挂**的专用工具集。
# 与 WORKER_ONLY_TOOLS（按卡类型收敛）/ ROOT_ONLY_TOOLS（按卡 id 收敛）不同——那两类
# 仍是「卡片内挂、按条件收窄」；本类**压根不进常规工具面**（不进四段过滤），只在
# 对应 op 回合由 build_tool_schemas 在过滤之后追加。用户 2026-10-16 口径：
# 「这个工具不是卡片内挂的，我不点按钮，模型是无法看到这个工具并调用这个工具的」。
# 同时为「未来在特定模式下开放给模型」留口——加一种 op 即多一组专用工具。
OP_ONLY_TOOLS: frozenset[str] = frozenset(
    {"propose_status", "write_requirements_final"})

# ============================================================
# [自动模式 2026-10-17 · 回灌 5700 块0.4] 末段追加 · 自动模式专用工具（非卡片内挂）
#
# 与 WORKER_ONLY_TOOLS（按卡类型收敛）/ ROOT_ONLY_TOOLS（按卡 id 收敛）/ 
# OP_ONLY_TOOLS（按 op 收敛）不同——本类**压根不进常规工具面**（不进四段过滤），
# 只在 ``auto_mode=1`` 的卡上由 build_tool_schemas 在四段之后追加。
# ⇒ 常规卡片工具面**逐字不变**（红线 2 / 块0.4 硬判据 1）。
# ============================================================
#: 块 0.4：update_goal（声明目标态——「停」的唯一权威在模型）。
#: G2 起扩为三件：＋send_card_message/read_inbox（把卡间通信交回模型）。
#:
#: [2026-10-29 · 用户令「我有任意一句话说过这是自动模式的工具吗」]
#: **`edit_requirements` 从本集合移除**：它是普通的写卡面工具（挂 SWITCH_EDIT），
#: 按权限走常规面即可。此前挂在自动模式专用集里 ⇒ 手动卡压根看不见它，
#: 而「草稿 → 评审 → 正稿」这条流程对**所有**子卡都成立（2026-10-28 用户裁定
#: 「需求文档它可以自己改，这个权限交还给它」），不是自动模式的特权。
AUTO_ONLY_TOOLS: frozenset[str] = frozenset(
    # [v12 2026-10-22 · 用户裁定「worker 至少能给自己的父卡发信」] send_card_message
    # **移出本集**：它原来只随 auto_mode=1 下发，于是非自动卡（含 worker）压根看不见
    # 发信工具——即使 perms 里给了开关。现在改由 TOOL_SWITCH 常规过滤
    # （有 send_card_message 开关就可见），与其它工具同一条规则。
    {"update_goal", "get_goal", "create_goal", "resume_child"})

# ============================================================
# [角色机制 5710] 角色卡专用工具（非卡片内挂）
#
# 与 AUTO_ONLY_TOOLS 同手法：不进常规工具面、不随 perms 过滤；只在
# ``card_type=="role"`` 的当前卡上由 build_tool_schemas 末段追加。
# 角色卡不是作业卡：它只判断、记录判断、必要时发言/警报。
# ============================================================
FUNCTION_ONLY_TOOLS: frozenset[str] = frozenset({
    # [2026-10-31 用户令「判断、警报我明确不需要，拆掉」] record_verdict /
    # raise_alarm / correct_alarm 已从注册表删除，名字同步从本集移除，
    # 不留「集合里有、注册表没有」的悬空项。
    # [通信收拢 2026-10-28] issue_directive / verify_directive 退役（并进 direct），
    # 不再进任何集合；名字从本表删掉，避免「集合里有、注册表没有」的悬空项。
    # [总裁制 P4/P5 2026-10-24] 四件新工具不进基础池(report_up 由类型闸按
    # 卡类型追加;总裁三件由末段④按目录追加)。放基础池会经二段 perms 闸
    # (read 开关)漏给所有有 read 的卡(rule 卡实测漏)。
    # ★ 只用于基础池排除:_apply_function_toolface 的追加清单是独立元组,
    #   职能回合不会因此拿到这四件。
    "report_up", "curate_face", "confirm_with_user", "report_to_user",
    "stop_card", "silence_card", "continue_task",
    # [四场景 2026-10-25] 评审/汇报/同步/指令五件不进基础池:
    # sync 由类型闸追加、direct 由类型闸与总裁目录追加。
    "sync", "direct",
    # [伴生评审卡 2026-10-27] approve 只给**职能**（评审成员）：放基础池会经
    # 二段 perms 闸（read 开关）漏给所有有 read 的卡——子卡也会看得见"通过"，
    # 而它根本不是评审成员。由 _apply_function_toolface 追加。
    "approve",
    # [伴生评审卡 2026-10-27] 撤销／裁决同样不进基础池：它们挂的是
    # read/edit 开关，而 `tool` 卡也有 edit ⇒ 实测漏给了 tool 卡（
    # test_cardperm_readopen 抓到）。总裁三件走目录 allow。
    "revoke_approve", "decide",
})

#: [card_031] 旧名保留别名——旧 role 路径的代码/用例仍按 ROLE_ONLY_TOOLS 取值，
#: 指向同一份集合（**不是第二份定义**，改一处即两处同步）。
ROLE_ONLY_TOOLS: frozenset[str] = FUNCTION_ONLY_TOOLS

# [权限底座重做 2026-10-12] 工具组→工具名映射（build_tool_schemas 过滤面）。
# 由卡上的 tools_file / tools_web 两个开关决定下发哪几组：缺哪个开关就不下发
# 哪组。SEARCH 开关本次仍无接线点（无对应工具组），保留不发。
_FILE_GROUP_TOOLS = ("list_files", "read_file", "search_files", "write_file")
_WEB_GROUP_TOOLS = ("web_search", "fetch_web")

# [工具面补齐 2026-10-12] **工具 → 开关** 映射：每件工具由哪个权限开关决定可见性。
#
# 补的是什么：此前 _filter_by_category 只按 tools_file / tools_web 两组过滤，
# 卡片那 6 个开关（read/edit/delete/create_child/link/unlink）与 shadow_write
# **只在执行层有关口、没接工具面**——配置里删掉 edit，模型照样看得见 edit_card，
# 调下去才被拒。用户裁定「没有权限的卡片应该压根看不见对应的工具」，
# 故建本表让工具面与执行层同源：同一张卡的 perms，既决定它调不调得动，
# 也决定它看不看得见。
#
# 本表与执行层关口的关系（**必须同源，不得各写一套**）：
#   工具面 —— 查这张卡**自己**的 perms，决定这件工具**看不看得见**；
#   执行层 —— policy.check(actor, target, switch) 查**目标卡**的 perms
#             （读侧全盘可读、写侧叠树作用域），决定这次调用**允不允许**。
# 两者查的都是 policy.has_perm，卡上没这个开关 ⇒ 工具面不下发 ∧ 执行层必拒。
#
# 映射依据（逐件对过执行层的 policy.check 调用点，不是拍脑袋）：
#   read_card / read_memory / read_shadow  → read（L169/L194/L379 执行层关口）
#   edit_card                              → edit（L272）
#   delete_card                            → delete（L337）
#   create_card                            → create_child（L227）
#   link_cards / unlink_cards              → link / unlink（L430/L474）
#   link_rel / unlink_rel                  → link_rel / unlink_rel（依赖线独立开关，10-27 裁定）
#   update_shadow                          → shadow_write（L361）
#   list_manuscript/read_manuscript/search_manuscript → manuscript（新增开关，原无）
#   run_tool_card                          → invoke_tool（新增开关，原无）
#   create_plan/plan_reorder/plan_insert/plan_remove/plan_modify/step_complete
#                                           → plan（新增开关，原无；这六件由
#                                             src/thinkchain/plan_tools 注册进同一个
#                                             TOOL_REGISTRY，同样会被本过滤扫到）
#   web_search / fetch_web                 → tools_web
#   list_files/read_file/search_files/write_file → tools_file
#   draft_rule_edit                        → read（规则卡 2026-10-15·块3：起草是
#                                             读性质，零新开关；第四段根卡闸另收）
TOOL_SWITCH: dict[str, str] = {
    "read_card": policy.SWITCH_READ,
    "read_memory": policy.SWITCH_READ,
    # [memory_expand 接线 2026-11-01 · 用户令「给所有卡挂上」] 与 read_memory
    # 同开关：它是记忆条目沿锚点展开到逐字原文的下探件，读性质——read 门槛
    # 一致的卡都该有（此前注册了但漏配开关 ⇒ 全卡不可见，死件）。
    "memory_expand": policy.SWITCH_READ,
    "read_shadow": policy.SWITCH_READ,
    # [卡片底座第一步 2026-10-14] 沿通信线读——独立开关（沿连线读比直接读更敏感）
    "read_linked": policy.SWITCH_READ_LINKED,
    # [规则卡 2026-10-15 · 块3] draft_rule_edit 挂 read（起草是读性质，零新开关；
    # 只起草不落盘，落盘走用户审批 /rule/commit）。可见性再由 build_tool_schemas
    # 第四段 ROOT_ONLY_TOOLS 收敛到剧本根卡，执行层 handler 再校验一次（双保险）。
    "draft_rule_edit": policy.SWITCH_READ,
    "edit_card": policy.SWITCH_EDIT,
    # [分块写入 2026-10-19 · permexp] 与 edit_card 同开关：写正文就是写正文。
    "edit_requirements": policy.SWITCH_EDIT,
    # [worker 卡 2026-10-15 · §六块2 · **v1.1 修订**] report_done 挂 tools_file——
    # 原挂 SWITCH_EDIT 系派发方笔误：tool 卡有 edit 无 tools_file，挂 edit 会让
    # readopen:287 与 :252 在「零翻新」下数学上不可能同时成立。挂 tools_file 后：
    # tool 卡第二段即滤（:287 绿）、sub_project 绕第三段闸可见恰为全量（:252 绿）、
    # worker 有 tools_file 正常可见（判据1 绿）。判定权在第三段闸＋执行层
    # card_type 校验双保险，开关只决定「有没有资格进闸」。
    "report_done": policy.SWITCH_TOOLS_FILE,
    "delete_card": policy.SWITCH_DELETE,
    "create_card": policy.SWITCH_CREATE_CHILD,
    # [父卡设子卡使命 2026-10-21] 立使命与建子卡同一把关口：能建就能立。
    "set_child_mission": policy.SWITCH_CREATE_CHILD,
    # [自动模式 2026-10-17 · 回灌 5700 块0.4] update_goal 挂 plan（「建计划/改计划/
    # 推进步骤」语义最近——【起草】可推翻，Open Item TC-6.4）。★ 它**不进常规
    # 工具面**：被 AUTO_ONLY_TOOLS 从工具池里排除，只在 auto_mode=1 的卡上于
    # 四段过滤之后追加（同「非卡片内挂」手法）。
    "update_goal": policy.SWITCH_PLAN,
    # [goal 域 2026-10-20 · 照抄 dsh-tool-goal] 与 update_goal 同一组开关：目标
    # 三件套是一个整体，拆开映射会让 get_goal 被工具面静默拦掉（注册表不变量
    # 要求每件工具都有开关映射，缺了就等于「有工具但永远看不见」）。
    "get_goal": policy.SWITCH_PLAN,
    "create_goal": policy.SWITCH_PLAN,
    # [恢复子卡 2026-10-20] 与目标三件套同组：它是「替子卡 resume」的那一步，
    # 没有它，父卡只能把子卡叫醒一次、续不上目标。
    "resume_child": policy.SWITCH_PLAN,
    # [harness 给根卡 2026-10-28] 设计盯梢策略挂 plan（「定计划/定规矩」语义最近）。
    # ★ 这条映射是**必需**的：注册表不变量要求每件工具都有开关映射，缺了就被
    # `_filter_by_category` 静默拦下——模型压根看不见这个工具（「有工具但永远
    # 看不见」）。实际可见范围再由 ROOT_ONLY_TOOLS 收到根卡。
    "design_policy": policy.SWITCH_PLAN,
    # [worker 卡 2026-10-15 · §六块2] create_worker_card 挂既有 create_child（建
    # worker 子卡走同一建卡关口）：无 create_child 的类型（tool/worker）压根看不见
    # 它，有 create_child 的（project/sub_project）按下发面规则可见。
    "create_worker_card": policy.SWITCH_CREATE_CHILD,
    # [差事卡 2026-11-01] 建差事卡＝召唤职能：建的是子卡 ⇒ create_child 同关口。
    "create_deploy_card": policy.SWITCH_CREATE_CHILD,
    # [UI 卡 2026-11-02] 建 UI 卡＝派 UI 执行卡：建的是子卡 ⇒ create_child 同关口。
    "create_ui_card": policy.SWITCH_CREATE_CHILD,
    # 差事回执：只写派遣方会话＋唤醒，无面写 ⇒ read 开关（发信类同口径）。
    "report": policy.SWITCH_READ,
    "link_cards": policy.SWITCH_LINK,
    "unlink_cards": policy.SWITCH_UNLINK,
    # [依赖关联线 2026-10-16] 第三种线（依赖关联线）的建/解线工具——复用 link/unlink
    # 开关（不新开）。写的是卡上另一个字段（relations，非 links），权限同款，
    # 工具面按现有四级过滤机制自动生效。
    # [三线拆开关 2026-10-27 · 用户裁定「区分」] 依赖线用自己的开关，
    # 不再与通信线共用 link/unlink。
    "link_rel": policy.SWITCH_LINK_REL,
    "unlink_rel": policy.SWITCH_UNLINK_REL,
    # [更新现状按钮改造 2026-10-16] propose_status 挂 SWITCH_READ——**只为满足
    # 「注册表每件工具都要有开关映射」的完备性不变量**（tests/test_cardperm_readopen.py
    # ::test_tool_switch_covers_every_registered_tool），语义上「只提议不落盘」也是读性质
    # （同 draft_rule_edit 的口径）。**实际下发不走这个开关**：本工具在 OP_ONLY_TOOLS 里，
    # 被 build_tool_schemas 从常规工具面摘除，只在 op=update_status 回合追加（见该函数）。
    "propose_status": policy.SWITCH_READ,
    # [需求确定按钮改造 2026-10-29] write_requirements_final 挂 edit——**只为满足
    # 「注册表每件工具都要有开关映射」的完备性不变量**。实际下发不走这个开关：
    # 本工具在 OP_ONLY_TOOLS 里，被 build_tool_schemas 从常规工具面摘除，
    # 只在 op=requirements_final 且本卡是根卡时追加（见该函数）。
    "write_requirements_final": policy.SWITCH_EDIT,
    # [通信收拢 2026-10-28] issue_directive / verify_directive 已退役并进 direct，
    # 开关映射一并删除（映射表与注册表必须一一对应，悬空项会被 test_no_new_ghost_tool 抓）。
    # 组阁权挂 create_child（根卡才有），关注管理挂 link；两件都只对根卡下发。
    "create_function": policy.SWITCH_CREATE_CHILD,
    "function_watch_cards": policy.SWITCH_LINK,
    "update_function_profile": policy.SWITCH_CREATE_CHILD,
    "set_function_duty": policy.SWITCH_CREATE_CHILD,
    "list_functions": policy.SWITCH_READ,
    # [卡片笔记 2026-10-27] 笔记同挂 shadow_write：能写影子就能写笔记。
    "edit_note": policy.SWITCH_SHADOW_WRITE,
    "edit_shadow": policy.SWITCH_SHADOW_WRITE,
    "list_manuscript": policy.SWITCH_MANUSCRIPT,
    "read_manuscript": policy.SWITCH_MANUSCRIPT,
    "search_manuscript": policy.SWITCH_MANUSCRIPT,
    "web_search": policy.SWITCH_TOOLS_WEB,
    "fetch_web": policy.SWITCH_TOOLS_WEB,
    # [浏览器能力 2026-11-02] browser 单件挂 tools_web——联网类动作同类别
    # （开页面、读页面、操作页面）。实际下发再受能力挂载闸
    # （_filter_by_capability：卡上 abilities 须有 browser）。
    "browser": policy.SWITCH_TOOLS_WEB,
    # [基础工具找回 2026-10-18 · Cardlo 搬运] 七件全挂 tools_file
    "bash": policy.SWITCH_TOOLS_FILE,
    "python": policy.SWITCH_TOOLS_FILE,
    "glob_files": policy.SWITCH_TOOLS_READ_FILE,
    "delete_file": policy.SWITCH_TOOLS_FILE,
    "move_file": policy.SWITCH_TOOLS_FILE,
    "rename_file": policy.SWITCH_TOOLS_FILE,
    "edit_file": policy.SWITCH_TOOLS_FILE,
    "list_files": policy.SWITCH_TOOLS_READ_FILE,
    "read_file": policy.SWITCH_TOOLS_READ_FILE,
    "search_files": policy.SWITCH_TOOLS_READ_FILE,
    "write_file": policy.SWITCH_TOOLS_FILE,
    # 思维链六件（plan_tools 注册进本注册表）
    "create_plan": policy.SWITCH_PLAN,
    "plan_reorder": policy.SWITCH_PLAN,
    "plan_insert": policy.SWITCH_PLAN,
    "plan_remove": policy.SWITCH_PLAN,
    "plan_modify": policy.SWITCH_PLAN,
    "step_complete": policy.SWITCH_PLAN,
}

# [T12-R3] 工具表基准版本化固化：工具集/schema 变更须递增版本号并对账基准指纹。
# tools-t12-1：T12 增补 read_memory（其余 16 件与 T11 基线逐字一致）。
# tools-tc-1：[建卡工具化 2026-10-08] 增补 create_card（共 18 件）。
# tools-tc-2：[卡片信息结构改造 S3-7/S3-8 · 2026-10-08] edit_card schema 描述随闸语义
# 更新（三块语法＋条目语法＋三条写入规则）；工具件数不变。
# [B-09 2026-10-08] 注册表落地**不改工具面** ⇒ 版本号保持 tools-tc-2 不递增。
# tools-tc-3：[工具卡底座 T9/T7 · 2026-10-09] **工具集变更 ⇒ 递增**（承 T12-R3
# 纪律：工具集/schema 变更须递增版本号并对账基准指纹）。变更两处：
#   ① 新增第 19 件工具 run_tool_card（T7）；
#   ② 主卡侧收回 7 件写工具（T9）——工具面**按角色分化**：工具卡 19 件、
#      主卡 12 件（读＋查＋调用 run_tool_card）。
# B-09 的「版本不动」只成立于「工具面零变化」；本次工具面确有变化，故递增。
# tools-tc-4：[卡片底座第一步 2026-10-14] 新增第 20 件工具 read_linked（沿通信线
#   读对方卡的正文／影子／对话）。工具集变更 ⇒ 递增（承 T12-R3 纪律）。
# tools-tc-5：[worker 卡 2026-10-15] **工具集变更 ⇒ 递增**：新增第 17 件 report_done
#   （worker 卡完工回报）与第 18 件 create_worker_card（主卡派生worker 卡并首跑）；
#   build_tool_schemas 增第三段 card_type 闸（WORKER_ONLY_TOOLS，report_done 只对
#   worker 下发）。TOOL_REGISTRY 字面量条目数 由 16 → 18（验收 grep 口径见任务书
#   §六块2 判据 5）。
# tools-tc-6：[规则卡 2026-10-15 · 块3] **工具集变更 ⇒ 递增**：新增第 19 件
#   draft_rule_edit（根卡起草规则区修改，只起草不落盘）；build_tool_schemas 增
#   第四段根卡闸（ROOT_ONLY_TOOLS，只对剧本根卡下发）。TOOL_REGISTRY 字面量
#   条目数 由 18 → 19（验收 grep 口径见任务书 §六块3 硬判据 1）。
# tools-tc-7：[依赖关联线 2026-10-16] **工具集变更 ⇒ 递增**：新增第 20/21 件
#   link_rel / unlink_rel（第三种线「依赖关联线」的建/解线，写卡上 relations
#   字段，不参与通信；权限复用 link/unlink 开关）。TOOL_REGISTRY 字面量条目数
#   由 19 → 21（验收 grep 口径：注册表内「双引号工具名 + 冒号 + ToolDef(」的行数 == 21）。
# tools-tc-8：[更新现状按钮改造 2026-10-16] **工具集变更 ⇒ 递增**：新增第 22 件
#   propose_status（「更新现状」专用回合里只提议、不落盘地交出「新现状」正文）。
#   **它不进常规工具面**（列 OP_ONLY_TOOLS），由 build_tool_schemas 在四段过滤之后、
#   仅当 op==OP_UPDATE_STATUS 时追加 ⇒ 常规卡片工具面逐字不变，本号递增只因
#   「注册表工具集/schema 变更」（T12-R3 纪律）。
#   TOOL_REGISTRY 字面量条目数 由 21 → 22（验收 grep 口径同 tools-tc-7）。
# tools-tc-17：[需求确定按钮改造 2026-10-29] **工具集变更 ⇒ 递增**：新增
#   write_requirements_final（根卡【需求确定】那一轮写需求文档**正稿**的口子）。
#   同样不进常规工具面（OP_ONLY_TOOLS），只在 op==OP_REQUIREMENTS_FINAL 且本卡
#   是根卡（project）时追加 ⇒ 常规卡片工具面逐字不变。
# tools-tc-18：见上行（tc-18 现行值）。
# tools-tc-19：[事实分层 2026-11-01] **schema 文案变更 ⇒ 递增**：sync 的
#   content 参数描述「要同步的事实」→「要同步的事(常是你卡面的变化)」——
#   消息是送达信息，不把消息教成第二条事实源；read_shadow 跨卡剥笔记随批修。
# tools-tc-22：[UI 卡不编程 2026-11-05 · 用户令] create_ui_card/create_worker_card
#   描述改写：判据＝要不要跑命令；UI 卡写页面但没有 bash/python。
# tools-tc-25：[coder→worker 改名 2026-11-05 · 用户令] 工具集变更 ⇒ 递增：
#   create_coder_card → create_worker_card（除 UI 外一切执行：编程/文档/命令/
#   构建测试/查档）；完工消息类型 coder_done → worker_done；卡型值 worker。
TOOL_TABLE_VERSION = "tools-tc-25"


# ============================================================
# [T-0010 §1] 工具结果信封
#
# 契约：结果一律带信封，成败读 ok 字段，错误可分类。
#   成功 {"ok": True,  "error": None,                        "data": {...原返回dict}}
#   失败 {"ok": False, "error": {"code": <码>, "message": 文案}, "data": None}
# **错误文案一字不改**，只补 code——code 是给模型分类决策用的新维度。
# ============================================================

TOOL_ERROR_CODES: tuple[str, ...] = (
    "denied",      # 权限/越权拒绝（含结构性护栏：根卡不可删、影子卡不可直改）
    "not_found",   # 目标不存在（卡/影子/work/章节/关系/工具名）
    "locked",      # 423 锁超时
    "validation",  # 参数缺失/类型错/取值非法/内容不合闸
    "internal",    # 其余一切（含 storage 直传错误与未捕获异常）
)


def err(code: str, message: str) -> dict[str, Any]:
    """失败信封。``message`` 为回灌模型的原文案。"""
    return {"ok": False, "error": {"code": code, "message": message}, "data": None}


def ok_(data: dict[str, Any]) -> dict[str, Any]:
    """成功信封：既有成功返回 dict（含 card_id/title 等键）**原样**进 data。"""
    return {"ok": True, "error": None, "data": data}


def _is_envelope(x: Any) -> bool:
    """是否是信封（三个键齐全）。handler 自建的 {'ok': True, ...} 不算信封。"""
    return (isinstance(x, dict) and "ok" in x and "error" in x and "data" in x)


def render_tool_result(result: dict[str, Any]) -> str:
    """回灌模型的文本投影——**单一来源**。

    message_runner 的两个调用点（slot 入槽 / tool_full_texts 累积）都必须走它，
    禁止各自拼装：两处曾各自 ``json.dumps(tool_result)``，信封化后错误形态会被
    误当数据喂给模型。
    """
    if result.get("ok") is True:
        return json.dumps(result.get("data"), ensure_ascii=False)
    e = result.get("error")
    if isinstance(e, str):          # 未信封化的直传（旧路径兜底）
        return e
    e = e or {}
    return f"[tool_error code={e.get('code')}] {e.get('message')}"


# ============================================================
# 工具注册表（B-09）
# ============================================================

@dataclass(frozen=True)
class ToolDef:
    """单件工具的注册项：schema ＋ handler ＋ 只读标志住在一起。

    schema 允许两种形态（两者都从同一处派生下游，语义无差）：
    - dict：字面量 schema（14 件内置工具，段内注释随段迁移保留）。
    - Callable[[], dict]：**延迟求值**的 schema。仅 file_tools 四件用此形态——
      它们的 description 是 f-string over `ALLOWED_ROOTS`，而 conftest 会
      monkeypatch 该常量；若在模块加载期取件填入，会把真实仓库路径
      （D:\\AI_station）冻结进 schema（实测：该描述对 ALLOWED_ROOTS
      call-time 敏感），既是工具面变化，也让测试期与生产期描述分叉。
      延迟求值保持与改前逐字一致（每次派生现取）。
    """
    schema: Union[dict[str, Any], Callable[[], dict[str, Any]]]
    handler: Callable[..., Any]
    readonly: bool
    #: [工具说明唯一源 2026-10-27] 场景／用法／效果事实（`tool_doc.ToolDoc`）。
    #: 填了它 ⇒ `_schema_of` 用 `render_description` 覆盖 schema 里的
    #: description（tools 参数与提示词工具节同源）；没填 ⇒ 回退原 description
    #: （过渡期，逐件填完后本字段改必填）。
    doc: Optional[Any] = None


def _schema_of(def_: ToolDef) -> dict[str, Any]:
    """取 schema 字面量（延迟形态在此现取）。

    [工具说明唯一源 2026-10-27] 若注册项带 `doc`，用渲染结果覆盖 description
    ——**这是唯一渲染点**，tools 参数与提示词工具节都经这里取，不可能不一致。
    """
    s = def_.schema() if callable(def_.schema) else def_.schema
    _doc = getattr(def_, "doc", None)
    if _doc is not None and isinstance(s, dict):
        from .tool_doc import render_description  # noqa: PLC0415 延迟破环
        s = dict(s)
        s["description"] = render_description(str(s.get("name") or ""), _doc)
    return s


# ============================================================
# handler（现 execute_tool 各 if 分支的分支体，原样搬运为独立函数）
# 签名统一：_h_<tool>(work_id, current_id, arguments, cards) -> dict
# ============================================================

def _last_event_line(p: Path) -> str:
    """只读文件末尾 8KB 取最后一行——事件文件可能十几万行（card_009 实测 11 万行），
    读全文会把热路径拖死。"""
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            back = min(size, 8192)
            f.seek(size - back)
            data = f.read().decode("utf-8", "replace")
        lines = [ln for ln in data.splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


#: 事件类型 → 上级看得懂的活说法（**"最后"而非"正在"**：距今可能很久，
#: 说"正在推理（60 分钟前）"是自相矛盾）
_EVENT_LABEL = {
    "reasoning": "最后在推理",
    "tool_call": "最后调了工具",
    "tool_result": "最后收到工具结果",
    "round_start": "最后开了一轮",
    "card_message": "最后收到卡间消息",
    "done": "已收回合",
    "error": "最后出错",
    "card_block_edited": "最后卡面被改",
    "card_created": "刚被建出来",
}

#: 回合边界事件——「最后一条信息的状态」只认这些，过程事件（tool_call 等）
#: 不回答"它停在哪"。turn_end 是权威终止帧（finally 无条件发）；done/error
#: 是内容层收口；card_stopped 是被停。
_TURN_BOUNDARY_TYPES = {"turn_end", "done", "error",
                        "card_stopped", "card_stopped_self"}

#: turn_end.outcome / done.end_reason → 上级看得懂的落态
_TURN_END_OUTCOME = {
    "ok": "回合正常收口",
    "error": "回合出错",
    "aborted": "回合被中止",
}
_DONE_END_REASON = {
    "model_done": "回合正常收口",
    "human_stop": "被人为停止",
    "policy_stop": "被策略停止",
}


def _activity_ago(ts_text: str) -> str:
    """ISO 时间戳 → 距今说法；坏了就回空。"""
    try:
        ts = datetime.fromisoformat(str(ts_text or ""))
        secs = (datetime.now().astimezone() - ts).total_seconds()
        if secs < 60:
            return "刚刚"
        if secs < 3600:
            return f"{int(secs // 60)} 分钟前"
        if secs < 86400:
            return f"{int(secs // 3600)} 小时前"
        return f"{int(secs // 86400)} 天前"
    except Exception:  # noqa: BLE001 时间戳坏了就不报距今
        return ""


def _card_activity(work_id: str, card_id: str, meta: dict[str, Any]) -> str:
    """给上级看的一行活性：**在不在跑 · 最后一条信息的状态 · 已跑轮次**。

    [上级可见性 2026-10-19 · permexp] 用户裁定：上级看不见下属在干什么 ⇒ 只能看
    文件大小 ⇒ 误判"零产出" ⇒ 换人。

    [活性口径 2026-11-01] 用户裁定「活性＝此刻在不在运行＋最后一条信息的状态」：
    · 在不在跑只看 ``turn_holder``（在途回合唯一登记处，auto_driver 判忙同用它），
      progress stage 作旁证（带阶段名）——不用内存 stage 单判（崩了没清会谎报）；
    · 停了看**回合边界事件**的落态（turn_end.outcome / done.end_reason /
      error / card_stopped），run_start 后无边界＝上一回合没收口（崩/重启）；
    · goal_state 降为**目标位标签**（已收口/卡住了/已暂停/已沉默），
      「active」不再反推"待推进"——目标开着不等于有人在跑。
    """
    parts: list[str] = []

    # ① 在不在跑：turn_holder 为准，progress.active 旁证
    holder = ""
    try:
        holder = session_facade.turn_holder(work_id, card_id)
    except Exception:  # noqa: BLE001 读不到＝不占坑
        holder = ""
    stage_name = ""
    try:
        from . import progress
        rec = progress.get_stage(work_id, card_id) or {}
        if rec.get("active"):
            stage_name = str(rec.get("stage") or "")
    except Exception:  # noqa: BLE001 旁证坏了不挡
        stage_name = ""
    running = bool(holder) or bool(stage_name)

    if running:
        parts.append(f"运行中（{stage_name}）" if stage_name else "运行中")
    else:
        # 不在跑：先问有没有被沉默，再报目标位
        silenced = False
        try:
            from ..storage import governance_store
            silenced = governance_store.is_silenced(work_id, card_id)
        except Exception:  # noqa: BLE001 治理表读不到＝没沉默
            silenced = False
        if silenced:
            parts.append("已沉默")
        else:
            parts.append({
                "complete": "已收口",
                "blocked": "卡住了",
                "paused": "已暂停",
            }.get(str((meta or {}).get("goal_state") or ""), "已停止"))

    # ② 最后一条信息的状态：尾部窗口找最近的回合边界事件
    try:
        from ..storage import event_log
        tail = event_log.timeline(work_id, card_id, tail=40)
        last_run_start = None
        boundary = None
        for rec in tail:
            t = str(rec.get("type") or "")
            if t == "run_start":
                last_run_start = rec
            elif t in _TURN_BOUNDARY_TYPES:
                boundary = rec
        label = ""
        ts_text = ""
        if boundary is not None:
            t = str(boundary.get("type") or "")
            data = boundary.get("data") or {}
            if t == "turn_end":
                label = _TURN_END_OUTCOME.get(str(data.get("outcome") or ""),
                                             "回合结束")
            elif t == "done":
                label = _DONE_END_REASON.get(str(data.get("end_reason") or ""),
                                           "回合正常收口")
            elif t == "error":
                label = f"回合出错（{data.get('error_type') or '?'}）" \
                    if data.get("error_type") else "回合出错"
            else:
                label = "被停止" if t == "card_stopped" else "自行停止"
            ts_text = str(boundary.get("ts") or "")
        elif last_run_start is not None and not running:
            # run_start 之后没有任何收口帧，且此刻不占坑 ⇒ 中途停了（崩/重启）
            label = "上一回合没收口"
            ts_text = str(last_run_start.get("ts") or "")
        elif tail:
            # 无回合史但有事件（建卡/写卡面留痕）——报最新一条活动兜底
            rec = tail[-1]
            label = _EVENT_LABEL.get(str(rec.get("type") or ""),
                                     str(rec.get("type") or ""))
            ts_text = str(rec.get("ts") or "")
        else:
            label = "未跑过回合" if not running else ""
        if label:
            ago = _activity_ago(ts_text)
            parts.append(f"{label}（{ago}）" if ago else label)
    except Exception:  # noqa: BLE001 活性是增益信息，读不到不阻断 read_card
        pass

    # ③ 已跑轮次
    rounds = (meta or {}).get("goal_rounds")
    if rounds:
        parts.append(f"已跑 {rounds} 轮")

    return " · ".join(parts)


def _cap_capabilities() -> list[dict[str, Any]]:
    """能力清单全表（读不到 ⇒ 空表）。只给 switch→能力 反查用。"""
    try:
        from ..infra import capability_registry as _cap
        return _cap.capabilities()
    except Exception:  # noqa: BLE001
        return []


def _abilities_of(card: dict[str, Any] | None,
                  cards: dict[str, dict[str, Any]] | None = None) -> set[str] | None:
    """这张卡挂的能力集合。**库里有就用库里的；没有就按卡型现算（不落库）**。

    [perms 退役 2026-10-30] perms 列删掉后，"缺挂载"不能再回退到 perms 判据。
    但"缺"有两种：真卡没落（异常）与手工构造的卡面（测试/合成卡）。两者都不该
    直接关门——按卡型＋模式现算一次，结果与建卡时算的完全同源（同一个
    ``abilities_for``），所以不会放宽。

    算不出来（卡型不认识 / 清单不可用）⇒ None，调用方按"没有任何能力"处理。
    """
    if not isinstance(card, dict):
        return None
    ab = card.get("abilities")
    if isinstance(ab, list):
        return {str(x) for x in ab}
    # [兼容层 2026-10-30] 卡上没有挂载、但**声明了开关**（老数据 / 手工构造的卡面）
    # ⇒ 按它给的开关反推能力：开关→能力是 1:1（拆包后每项能力只对应一个开关），
    # 所以这层不会放宽也不会收紧。真卡都有 abilities，走不到这里。
    perms = card.get("perms")
    if isinstance(perms, list):
        granted = set(str(x) for x in perms)
        try:
            from ..permissions import policy as _pol

            granted |= {s2 for s2 in granted
                        for s2 in _pol.SWITCH_IMPLIES.get(s2, ())}
        except Exception:  # noqa: BLE001
            pass
        return {str(c.get("code")) for c in _cap_capabilities()
                if str((c.get("impl") or {}).get("switch") or "") in granted}
    ctype = str(card.get("card_type") or "")
    if not ctype:
        return None
    mode = "auto" if bool(card.get("auto_mode")) else str(card.get("mode") or "manual")
    project_class = str(card.get("project_class") or "")
    if not project_class:
        for m in (cards or {}).values():
            if str((m or {}).get("card_type") or "") == "project":
                project_class = str((m or {}).get("project_class") or "")
                break
    try:
        from ..infra import capability_registry as _cap
        from ..permissions import policy

        perms, _ = policy.compute_perms(project_class, ctype)
        return set(_cap.abilities_for(perms, mode, ctype))
    except Exception as exc:  # noqa: BLE001 算不出来 ⇒ 当没有能力
        logger.warning("[capability] 现算挂载失败 card_type=%s: %s", ctype, exc)
        return None


def _actor_can_switch(card: dict[str, Any] | None, switch: str) -> bool:
    """操作者能不能做这个 switch：**已迁移看能力，未迁移看 perms**。

    与 ``_cap_gate`` 的分工：这里只回答"岗位能不能做这类事"，不管作用域；
    作用域另由 ``policy.can`` 判。``switch`` 到能力的对应关系直接读清单里的
    ``impl.switch``（拆包后一项能力只对应一个权限类别）。
    """
    if not isinstance(card, dict):
        return False
    mounted = _abilities_of(card, None)
    if mounted is None:
        return policy.has_perm(card, switch)       # 算不出来 ⇒ 老路
    codes = {str(c.get("code")) for c in _cap_capabilities()
             if str((c.get("impl") or {}).get("switch") or "") == switch}
    if not codes:
        return policy.has_perm(card, switch)
    return bool(mounted & codes)


def _function_extra_mounts(work_id: str, actor_id: str) -> list[str]:
    """actor 是职能档案 ⇒ 目录条目 ``extra_abilities``（岗位挂载，如 PM 的
    edit_other_face）；非职能/查不到 ⇒ 空。"""
    try:
        from ..storage import function_store as _fs
        from . import function_room
        prof = _fs.get_profile(work_id, actor_id) or {}
        if not prof:
            return []
        e = function_room.entry(function_room.catalog_code_of(prof)) or {}
        return [str(a) for a in (e.get("extra_abilities") or []) if str(a)]
    except Exception:  # noqa: BLE001
        return []


def _deployment_scope(work_id: str, actor_id: str, target_id: str,
                      cap_code: str) -> bool:
    """差事即作用域：actor 有覆盖 ``target_id`` 的**在派差事**，且该差事
    实发面（dep.tools）含 ``cap_code`` 展开的工具 ⇒ 视同「在范围内」。

    职能不进卡树，子树判据对它恒假——但差事授面本来就是「这次派活发的
    钥匙」，作用域与授面同源：差事覆盖哪张卡、发了什么工具，都在
    ``function_deployments`` 账上。只有发过的工具才放行——差事不会
    让 actor 拿到账外能力（dispose 之类没发就是没发）。
    """
    try:
        from ..storage import function_store as _fs
        from ..infra import capability_registry as _cap
        want = set(_cap.tools_of([cap_code]) or [])
        if not want:
            return False
        for dep in _fs.active_for_function(work_id, actor_id):
            if str(target_id) not in (str(dep.get("venue_ref") or ""),
                                      str(dep.get("target_card") or "")):
                continue
            if want & set(dep.get("tools") or []):
                return True
    except Exception:  # noqa: BLE001 差事账查不了不放宽
        pass
    return False


def _cap_gate(actor_id: str, cap_code: str, switch: str, target_id: str,
              cards: dict[str, dict[str, Any]] | None, what: str,
              *, work_id: str = "") -> dict[str, Any] | None:
    """写侧判据：**能力（岗位）＋ 作用域（按线）**。返回 None＝放行，否则 denied 信封。

    [能力底座 2026-10-30] 把 handler 里"查目标卡 perms"换成两件该查的事：

    - **能力**：操作者挂没挂这项能力（岗位能不能做这件事）；缺挂载时按卡型、模式同源现算。
    - **作用域**：树类开关（edit/delete/link/unlink/create_child）仍判操作者能不能对目标做。
    为什么不再查目标卡 perms：目标卡的 perms 是"这张卡能不能被这样动"，但同一张卡
    对不同的人本来就该不同答案——权限是关系，不是对象属性。
    """
    card = (cards or {}).get(actor_id)
    mounted = _abilities_of(card, cards)
    if mounted is None:
        return err("denied", f"本卡能力挂载不可用，不能{what}。")
    if cap_code not in mounted and work_id:
        # [执笔挂载 2026-11-01] 职能席位的卡面是合成面，目录 ``extra_abilities``
        #（PM 的 edit_other_face 这类岗位挂载）合成路径看不见——并进来再判。
        mounted = set(mounted) | set(
            _function_extra_mounts(work_id, actor_id))
    if cap_code not in mounted:
        return err("denied", f"本卡没挂「{cap_code}」这项能力，不能{what}。")
    if switch in policy.TREE_SWITCHES and not policy.can(actor_id, target_id, switch, cards):
        # 树闸不过 ⇒ 最后查差事：在派差事覆盖目标且实发面含此能力的
        # 工具，差事授权即临时作用域（职能无子树，这是它执笔的唯一通道）。
        if not _deployment_scope(work_id, actor_id, target_id, cap_code):
            return err("denied", f"越权：{target_id} 不在你的范围内，不能{what}")
    return None


def _line_between(work_id: str, current_id: str, target_id: str,
                  cards: dict[str, dict[str, Any]]) -> str:
    """两张卡之间是什么线：``parent``（父子线）/ ``relation``（关系线）/ ``""``（没有线）。

    [关系治理 2026-10-30 · 用户令「没关系的卡不该出现在清单里，这是污染」＋
     「权限应该按三种线来判断」] 读卡的门槛从"目标卡有没有 read 开关"改成
    **两卡之间有没有线**——权限是关系，不是对象属性。

    - 父子线：祖先链 ∪ 子树（双向），读**全部**卡面
    - 关系线：依赖/影响（双向），只读**需求文档**那类对外信息
    - 通信线：不算（它只管发消息）

    关系线双向查边表（`card_relations` 出边＋入边）；查不到 ⇒ 当作没有线。
    """
    cid = str(current_id or "")
    tid = str(target_id or "")
    if not cid or not tid:
        return ""
    if cid == tid:
        return "parent"
    # 两类**离树例外**（与 policy.can() 的规则④⑤同源，不另造一套）：
    #   · 根卡＝项目本体，对任何卡通过；
    #   · 职能/角色＝离树观察者，被裁定过「能读全树」。
    # 它们没有父子线，若不加这两条，连自己组建的职能卡都读不到。
    actor_meta = (cards or {}).get(cid) or {}
    actor_type = str(actor_meta.get("card_type") or "")
    if actor_type == "project" and not str(actor_meta.get("parent") or ""):
        return "parent"
    if actor_type in ("function", "role"):
        return "parent"
    # 父子线：沿 parent 上溯（祖先）与下溯（子树）
    parent = {str(k): str((v or {}).get("parent") or "") for k, v in (cards or {}).items()}
    cur = cid
    while parent.get(cur):
        cur = parent[cur]
        if cur == tid:
            return "parent"
    cur = tid                                  # 反向：target 是不是我的祖先
    while parent.get(cur):
        cur = parent[cur]
        if cur == cid:
            return "parent"
    stack, seen = [cid], {cid}
    while stack:
        x = stack.pop()
        for k, p in parent.items():
            if p == x and k not in seen:
                seen.add(k)
                if k == tid:
                    return "parent"
                stack.append(k)
    # 关系线：出边 + 入边
    try:
        from ..storage import card_db

        def _fn(conn):  # type: ignore[no-untyped-def]
            row = conn.execute(
                "SELECT 1 FROM card_relations WHERE (from_card=? AND to_card=?)"
                " OR (from_card=? AND to_card=?) LIMIT 1",
                (cid, tid, tid, cid)).fetchone()
            return row is not None

        if card_db.read_sync(work_id, _fn, False):
            return "relation"
    except Exception as exc:  # noqa: BLE001 查不到线表 ⇒ 当作没有线（fail-closed）
        logger.warning("[read_card] 关系线查询失败 %s→%s: %s", cid, tid, exc)
    return ""


# [2026-11-02 · card_021 实案续] 分段读：块名 → 存储字段。模型侧中文名为主，
# 英文别名照收（模型常拿存储字段名直接填）。
_BLOCK_READ_ALIASES = {
    "使命": "mission", "mission": "mission",
    "现状": "status_block", "status": "status_block", "body": "status_block",
    "需求文档": "requirements", "requirements": "requirements",
    "需求文档·草稿": "requirements_draft", "草稿": "requirements_draft",
    "requirements_draft": "requirements_draft",
}

_BLOCK_READ_LABELS = {
    "mission": "使命", "status_block": "现状",
    "requirements": "需求文档", "requirements_draft": "需求文档·草稿",
}


def _read_card_cap() -> int:
    """单块回显上限（字符）：READ_CARD_BLOCK_CAP 覆盖，缺省 24000。

    8 万 token 的需求文档（card_021 实案）整块回读一次就上 8 万 token 的
    线；封顶＋续读指引让「读卡」自身不再能一次吞掉上下文。惰性读 env
    （先例：memory_store 的 CONDENSE_TRIGGER_TOKENS）。
    """
    raw = os.environ.get("READ_CARD_BLOCK_CAP", "").strip()
    if not raw:
        return 24000
    try:
        v = int(raw)
        return v if v > 0 else 24000
    except ValueError:
        return 24000


def _cap_block_text(text: str, label: str, target_id: str) -> str:
    """块内容封顶：超限截到行边界＋尾部续读指引（非静默——明示行数与怎么续）。"""
    t = str(text or "")
    cap = _read_card_cap()
    if len(t) <= cap:
        return t
    head = t[:cap]
    cut = head.rfind("\n")
    if cut > cap // 2:
        head = head[:cut]
    shown = head.count("\n") + (1 if head else 0)
    total_lines = t.count("\n") + (1 if t else 0)
    return (head + f"\n…（本块共 {total_lines} 行/{len(t)} 字，以上为前 "
            f"{shown} 行——续读：read_card 带 block=\"{label}\" "
            f"para_start={shown + 1}）")


def _ranged_block_reply(target_id: str, title: str, label: str,
                        text: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """单块按行区间回读：``block``＋``para_start``/``para_end``（与 read_file
    的 para_* 同口径：1 起、闭区间、越端收敛）。区间本身超 cap 也封顶。"""
    rows = str(text or "").split("\n") if text else []
    total = len(rows)
    try:
        start = max(1, int(arguments.get("para_start") or 1))
        end = int(arguments.get("para_end") or 0) or total
    except (TypeError, ValueError):
        return err("validation", "para_start/para_end 必须是整数行号")
    if total and start > total:
        return err("validation", f"para_start={start} 超出范围：该块共 {total} 行")
    end = max(start, min(end, total)) if total else 0
    content = "\n".join(rows[start - 1:end]) if total else ""
    shown_end = end
    cap = _read_card_cap()
    if len(content) > cap:
        head = content[:cap]
        cut = head.rfind("\n")
        if cut > cap // 2:
            head = head[:cut]
        shown_end = start + head.count("\n")
        content = (head + f"\n…（区间 {start}-{end} 共 {len(content)} 字，"
                   f"仅返回前 {shown_end - start + 1} 行——"
                   "用更小的 para 区间续读）")
    return {"card_id": target_id, "title": title, "block": label,
            "total_lines": total,
            "read_lines": f"{start}-{shown_end}" if total else "0-0",
            "content": content}


async def _h_read_card(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """读另一张卡。**跨卡可见性开口**（卡片区域语义 2026-10-14 · v3）。

    此前只回 ``body``＝现状块一项，使命／第三块跨卡读不到（方案 §二.3 缺口）。
    现在一次给全三个正文块：**使命 / 现状 / 第三块**（v3：三块全是对外，
    「职责与边界」已按用户 17:56 令整体取消——边界靠权限卡，不靠文字描述）。

    - 第三块的**对外名由本树 project_class 决定**（rd_project → requirements；
      其余 → requirements），与 routes._apply_block_set 同一判据、同一口径。
    - **修改历史不给**（裁定 3：机械留痕，非事实源，读了是污染；v3 起它移进影子）。
    - 权限口径、返回信封、错误码**一律不变**：仍走 policy.SWITCH_READ 关口，
      越权仍回既有 denied 信封。
    """
    target_id = arguments.get("card_id", "")
    # [语义修正 2026-10-02] 先判存在再判权限：已删除的卡不再误报"越权"
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    # [权限收口 2026-10-09] 统一关口（读）：拒绝转既有 denied 信封；
    # 不存在已在上面走 not_found，关口不再见到缺卡
    # [关系治理 2026-10-30] 读的门槛＝**两卡之间有没有线**（父子线/关系线），
    # 不再看目标卡的 perms：权限是关系，不是对象属性。
    line = _line_between(work_id, current_id, target_id, cards)
    if not line:
        # [差事卡 2026-11-01] 无线最后查差事：在派差事覆盖 target_card 且实发
        # 面含 read_card ⇒ 差事授权即临时读权（职能不进树，对线恒无线；QA 在
        # 验收卡上读 sibling 被验卡的唯一通道）。
        if not _deployment_scope(work_id, current_id, target_id, "read_card"):
            return err(
                "denied",
                f"越权：{target_id} 与你之间没有父子线也没有关系线，读不到。")
        line = "deployment"
    if line == "deployment":
        # 差事读权给全卡面（验收要拿需求文档＋现状当判据），但不算父子线——
        # 只放 reading，不放组织权。
        pass
    result = await card_store.read_card(work_id, target_id)
    body = result["body"] or {}
    # [角色机制 5710 v2] 角色卡返回**独立结构**（管辖/关注/发言契约/升级线/
    # 值班/职责书），不再拿普通卡三块冒充。
    if str(result["meta"].get("card_type") or "") == role_room.ROLE_TYPE:
        summary = await role_room.role_summary(work_id, target_id)
        return {
            "card_id": target_id,
            "title": result["meta"].get("title", ""),
            "parent": "",
            "card_type": role_room.ROLE_TYPE,
            "role": summary,
            "text": summary.get("text", ""),
        }
    # [预设职能 2026-10-24] 职能卡同样返回**独立结构**:职责书/管辖/观点簿/
    # 指令簿与专业台账从 function_room.summary 动态拼装,不用通用三块冒充。
    if str(result["meta"].get("card_type") or "") == "function":
        from . import function_room as _function_room
        try:
            fsum = _function_room.summary(work_id, target_id)
        except Exception as exc:  # noqa: BLE001 台账拼不出 ⇒ 退回三块,不让读失败
            logger.warning("[read_card] 职能卡面拼装失败 work=%s fn=%s: %s",
                           work_id, target_id, exc)
            fsum = None
        if fsum is not None:
            return {
                "card_id": target_id,
                "title": result["meta"].get("title", ""),
                "parent": "",
                "card_type": "function",
                "function": fsum,
                "text": fsum.get("text", ""),
            }
    # [2026-11-02 · card_021 实案续] 分段读：block=块名 ＋ para_start/para_end
    # 按行区间只回一段。整块回在 8 万 token 文档上是「一次调用上 8 万 token
    # 的线」——编辑器报错已给行号，这里给按行取文的通道。
    want_block = str(arguments.get("block") or "").strip()
    if want_block:
        field = _BLOCK_READ_ALIASES.get(want_block)
        if field is None:
            return err("validation",
                       "block 只认：" + "、".join(dict.fromkeys(
                           _BLOCK_READ_LABELS.values()))
                       + f"（收到 {want_block!r}）")
        # 角色/职能卡是结构化卡面（上面已早返），走到这里必是普通三区块卡。
        if line == "relation" and field != "requirements":
            return err("denied", "关系线只读对外信息：block 只可取「需求文档」。")
        if field == "requirements_draft":
            if target_id != current_id:
                return err("denied", "「需求文档·草稿」只能读自己的卡。")
            text = str(result["meta"].get("requirements_draft") or "")
        else:
            text = str(body.get(field) or "")
        return _ranged_block_reply(
            target_id, str(result["meta"].get("title", "")),
            _BLOCK_READ_LABELS[field], text, arguments)
    # 第三块在库内恒为 requirements 一列，**模型侧统一叫「需求文档」**。
    # [需求文档正名 2026-10-28] 改前按 project_class 分叉（rd_project→requirements／
    # 其他树→rulings），而 workspace.js 早在 2026-10-22 就统一成「需求文档、不分树」
    # ⇒ 分叉是死代码，删掉。另外键名原来给英文（使命/现状却是中文），
    # 模型读卡看到 `requirements` 会与「需求文档」对不上，故统一成中文键。
    third_key = "需求文档"
    # [关系治理 2026-10-30] 关系线只给**对外信息**（需求文档；接口/文档信息将来并入这里）。
    # 使命＝"它为什么存在"、现状＝"它现在什么样"，那是父子线才给的——
    # 关系线只说明"我消费它的什么 / 我动会波及它什么"。
    if line == "relation":
        return {
            "card_id": target_id,
            "title": result["meta"].get("title", ""),
            "关系": "关系线（依赖/影响）· 只读对外信息，使命与现状不给",
            "links": result["meta"].get("links") or [],
            "relations": result["meta"].get("relations") or [],
            # [relations_in 2026-11-01 · 用户令「谁依赖我应该在对方卡面上，
            # 需要知道的不只是它自己」] 入边也是对外信息的一部分。
            "relations_in": result["meta"].get("relations_in") or [],
            third_key: _cap_block_text(body.get("requirements", ""),
                                       "需求文档", target_id),
        }
    _status_capped = _cap_block_text(body.get("status_block", ""),
                                     "现状", target_id)
    return {
        "card_id": target_id,
        "title": result["meta"].get("title", ""),
        "parent": result["meta"].get("parent", ""),
        # [上级可见性 2026-10-19] 一行活性：运行态 · 最近活动（多久前）· 已跑轮次。
        # 上级读子卡时自然看到，不必额外查；读自己时也成立（显示"空闲"）。
        "活性": _card_activity(work_id, target_id, result["meta"]),
        # [依赖关联线 2026-10-16] 两线并列返回：
        #   links     ＝通信线（原 relations 改名，read_linked 沿它寻址）
        #   relations ＝依赖关联线（新线，不参与通信）
        # [relations_in 2026-11-01] ＋入边投影「谁依赖我」（属卡面事实）。
        "links": result["meta"].get("links") or [],
        "relations": result["meta"].get("relations") or [],
        "relations_in": result["meta"].get("relations_in") or [],
        "body": _status_capped,
        "使命": _cap_block_text(body.get("mission", ""), "使命", target_id),
        "现状": _status_capped,
        third_key: _cap_block_text(body.get("requirements", ""),
                                   "需求文档", target_id),
        # [2026-11-01 · 用户令「草稿不进上下文，只有正稿常驻」] 草稿仍可
        # 按需自读（改它的入口是 edit_requirements，评审材料另走评审通道）：
        # 仅当自己读自己且有草稿时附，读别人的卡不给。
        **({"需求文档·草稿": _cap_block_text(
            result["meta"].get("requirements_draft") or "",
            "需求文档·草稿", target_id)}
           if target_id == current_id
           and str(result["meta"].get("requirements_draft") or "").strip()
           else {}),
    }


async def _h_read_memory(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [T12-R3] 记忆锚点下探（condense_fold 展开工具）：权限同 read_card
    # （读＝子树∪血统链∪连线∪锚卡全局可读）；entries 原样返回，块序号区间
    # 是机械过滤非加工；越界/空记忆如实返回空（不构造内容）。
    target_id = arguments.get("card_id") or current_id
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    # [关系治理 2026-10-30] 读按线判，与 read_card 同一条规则（父子线/关系线）。
    # 关系线只读对外信息，记忆是过程记录 ⇒ 只有父子线才给。
    _line = _line_between(work_id, current_id, target_id, cards)
    if not _line:
        return err("denied",
                   f"越权：{target_id} 与你之间没有父子线也没有关系线，读不到。")
    if _line == "relation":
        return err("denied",
                   f"{target_id} 与你是关系线：只读对外信息（需求文档），记忆不给。")
    state = memory_store.load_memory(work_id, target_id)
    entries = [e for e in (state.get("entries") or []) if isinstance(e, dict)]

    def _seq_of(e: dict[str, Any]) -> int | None:
        """块号取自 `anchor.seq`（CausalNode 结构）；旧格式的顶层 `seq` 兜底。

        [记忆编号体系 2026-10-29] 原实现只读 `e["seq"]`，而库里条目早已是
        `anchor.seq` ⇒ **区间过滤在生产上一直返回空**（与记忆区渲染同源错位）。
        """
        a = e.get("anchor")
        if isinstance(a, dict):
            v = a.get("seq")
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        v = e.get("seq")
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    lo = arguments.get("from_seq")
    hi = arguments.get("to_seq")
    if isinstance(lo, int) and not isinstance(lo, bool):
        entries = [e for e in entries
                   if (s := _seq_of(e)) is not None and s >= lo]
    if isinstance(hi, int) and not isinstance(hi, bool):
        entries = [e for e in entries
                   if (s := _seq_of(e)) is not None and s <= hi]
    return {"card_id": target_id, "mark": state.get("mark", 0), "entries": entries}


async def _h_memory_expand(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [F19 · 2026-10-31] memory_expand（dsh applyMemoryExpandTool 的移植注册）：
    # 沿锚点展开整组——结论 → 细节层 → 逐字原文；sunk 命中复活（revive_update）。
    # 权限同 read_memory（父子线才给，关系线不给——记忆是过程记录）。
    target_id = arguments.get("card_id") or current_id
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    _line = _line_between(work_id, current_id, target_id, cards)
    if not _line:
        return err("denied",
                   f"越权：{target_id} 与你之间没有父子线也没有关系线，读不到。")
    if _line == "relation":
        return err("denied",
                   f"{target_id} 与你是关系线：只读对外信息（需求文档），记忆不给。")
    seq = arguments.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return err("validation",
                   "seq 必须是整数——取记忆区锚点行 @锚点{card,seq} 里的 seq")
    page = arguments.get("page")
    from ..storage import memory_expand as _mx
    result = _mx.expand(work_id, target_id, int(seq),
                        page=int(page) if isinstance(page, int) and not isinstance(page, bool) else 0)
    return ok_({"card_id": target_id, "seq": int(seq),
                "found": bool(result.get("found")),
                "revived": bool(result.get("revived")),
                "text": _mx.render(result)})


async def _h_create_card(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # 判定（代码现状口径：只能建自己的子卡片）。parent 缺省＝当前卡自身（建自己
    # 下一层）；给了 parent 则经 policy.check 判定（parent 卡须授 CREATE_CHILD
    # ＋ actor 在 target 子树上游），与 edit_card/delete_card 同一关口，
    # 无第二套规则。结构性护栏（剧本根卡/带子卡者不可删）不在此——建卡不涉删。
    title = str(arguments.get("title", "") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    # [建卡工具化 2026-10-08][权限底座重做 2026-10-12] 权限＝关口 CREATE_CHILD
    # [拆分流程 2026-10-21 · 用户裁定「按职责拆卡，然后填使命」]
    # 使命必填且必须是真的职责：空串 / 模板占位一律拒。
    # 这不是格式闸门（不判文字好坏），只判「有没有写」——没写就是没写。
    _mission = str(arguments.get("mission", "") or "").strip()
    if not _mission:
        return err(
            "validation",
            "mission 必填：先说清这张卡负责什么（哪一块、为什么需要它、边界在哪）再建卡",
        )
    if _MISSION_PLACEHOLDER in _mission or len(_mission) < 8:
        return err(
            "validation",
            "mission 不能是模板占位或过短：写清这张卡负责哪一块、负责到什么程度",
        )
    # parent 缺省＝当前卡自身；显式给空串也按缺省处理（模型常传空串）
    target_parent = str(arguments.get("parent", "") or "").strip() or current_id
    # 存在性先判：已删/不存在的 parent 不误报越权（与 read_card 同序）
    if target_parent not in cards:
        return err("not_found", f"父卡不存在: {target_parent}")
    # [权限底座重做 2026-10-12] 关口 CREATE_CHILD：parent 卡上的固化 perms 须授
    # create_child；树类开关再收紧为「只能建自己子树内的卡」
    # [离开树的角色卡 2026-10-22 · 用户裁定] 组织权作用域由发起方位置决定：
    #   · 在树内的卡   → 只能建在自己子树内（原语义，逐字不变）
    #   · 离树的角色卡 → 可建在本项目树内**任意位置**（不含根卡）
    # 角色卡不在树里，"在自己子树内"对它恒假 ⇒ 不换这个口径它什么也建不出来。
    if not _org_scope_allows(current_id, target_parent,
                             policy.SWITCH_CREATE_CHILD, cards):
        return err(
            "denied",
            (
                f"越权：不能把卡建到 {target_parent} 之下"
                f"（当前卡 {current_id}；树内的卡只能用自己的子树，"
                f"离树角色卡可挂全树但不得动根卡）"
            ),
        )
    result = await card_store.create_card(
        work_id, title, "模型生成",
        parent=target_parent,
        conclusion=str(arguments.get("content", "") or ""),
        # [自动模式 2026-10-17 · 回灌 5700 块0.4/D2] 派活：把子卡的目标一并给出
        # （建卡即起跑，子卡继承本卡 auto 后由 auto_driver 点火）。缺省空串 ⇒
        # 行为与改造前一致。
        goal=str(arguments.get("goal", "") or ""),
        # [拆分流程 2026-10-21] 使命与目标分开落库：使命是这张卡的身份，
        # 目标只是这一轮要达成的东西——两者混在一个字段里，目标一漂使命就没了。
        mission=_mission,
        # [2026-10-26 · 记录补全] 记下**谁建的这张卡**（父卡建卡派活 = 派发证据）
        created_by=current_id,
    )
    if "error" in result:
        return result
    # 建卡同步刷新 meta 缓存：同轮后续工具（如 link_cards→新卡）能看到新卡
    cards[result["meta"]["card_id"]] = {
        **result["meta"], "card_id": result["meta"]["card_id"],
    }
    # [头尾介入 2026-10-25 · 用户令「职能只在阶段任务头尾介入」] 建卡本身
    # 不再发事件(过程零唤醒);头事件在总裁首次策展该卡需求时发(stage_head)。
    # 回执给模型：卡号真身在此——模型据此确证建成、不必再猜（建卡工具化的核心收益）
    return {
        "ok": True,
        "card_id": result["meta"]["card_id"],
        "title": result["meta"]["title"],
        "parent": result["meta"]["parent"],
        "shadow": "已派生影子卡（过程记录，你不用管）",
    }


# ============================================================
# [自动模式 2026-10-17 · 回灌 5700 块0.4] update_goal（自动模式卡专用）
#
# 「停」的**唯一权威在模型**（方案 §2.2）：模型必须显式声明 完成/暂停/卡住，
# 否则驱动器一直起下一轮。本工具是那个显式声明的出口。
# 下发＝**非卡片内挂**的专用工具（AUTO_ONLY_TOOLS）：不进四段过滤，只在
# auto_mode=1 的卡上于四段之后追加 ⇒ 常规卡片工具面逐字不变。
# 执行层双保险：①卡必须 auto_mode=1 ②policy.check（挂 plan 开关）。
# ============================================================

#: action → goal_state 的映射（三态；``active`` 不由本工具设置）。
_GOAL_ACTION_TO_STATE = {
    "complete": "complete",
    "pause": "paused",
    "blocked": "blocked",
}

#: [6702 移植 2026-10-17] 宽裕度：未传参的纯推断 pause 在「子卡恰好刚完工」
#: 的采样边界上也可能是一次活锁重挂；对这类 pause 宽裕度地清零（把 1 次清掉）。
#: 取 1＝只抹平采样边界的一次歧义，不改变「连续 ≥3 次才死锁」的判定实效。
#: 它**不是闸**：不参与任何轮次/进度/时间/token 判定。
_AWAIT_RESUME_GIVEUP_SLACK = 1


def _as_bool(raw: Any) -> bool:
    """宽松读布尔（模型可能送 ``true``/``"true"``/``1``/``"是"``）。

    缺省/空/``None`` ⇒ ``False``（**向后兼容的关键**：不传该参数时逐字保持旧行为）。
    刻意只把**明确真值**当真——宁可漏置位（退回旧推断分支）也不误置位。
    """
    if raw is None or raw == "":
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "y", "是")
    return bool(raw)


async def _h_update_goal(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """声明目标态：complete（完成）/ pause（暂停）/ blocked（卡住）。"""
    action = str(arguments.get("action", "") or "").strip().lower()
    reason = str(arguments.get("reason", "") or "").strip()
    if action not in _GOAL_ACTION_TO_STATE:
        return err("validation",
                   f"action 非法: {action or '(空)'}（只允许 complete/pause/blocked）")
    # ① 卡必须 auto_mode=1（双保险之一）
    meta = cards.get(current_id) or {}
    if not bool(meta.get("auto_mode")):
        return err("denied", "本卡不是自动模式卡，update_goal 不可用")
    # ② 执行层关口（同 create_card 等：policy.check，挂 plan 开关）
    try:
        policy.check(current_id, current_id, policy.SWITCH_PLAN, cards)
    except PermissionError as exc:
        return err("denied", str(exc))
    state = _GOAL_ACTION_TO_STATE[action]

    # [通信收拢 2026-10-28] 原「未回应询问挡收口」闸**删除**：
    # 它判的是收件箱账本里 `reply_expected` 的 msg_ask，而 ask/answer 这对类型
    # 已随 send_card_message 退役（现在没有任何工具能发 msg_ask）⇒ 判据永远为空，
    # 留着就是一道「看起来在管、实际不可能触发」的假闸。

    if action == "complete":
        # [角色机制 5710 v3] **指令闸**：目标卡上有未验收的角色指令
        # （issued/rejected/overdue）时不许收口；出路由角色复核后清账，
        # 或先把卡退回 blocked 说明卡在哪。
        # [通信收拢 2026-10-28] 角色验收不再有单独工具（verify_directive 已退役）：
        # 通过就不说话，不通过由角色再发一条 direct 要求改。
        try:
            _blockers = role_room.open_directives_for(work_id, current_id)
        except Exception:  # noqa: BLE001 读不到闸表不阻断主链（fail-open）
            _blockers = []
        if _blockers:
            _desc = "；".join(
                f"{d.get('id')}→{d.get('target_card_id')}: "
                f"{str(d.get('instruction') or '')[:60]}"
                for d in _blockers[:3])
            return err("validation",
                       f"收口被拒：本卡还有 {len(_blockers)} 条未验收的角色指令"
                       f"（{_desc}）。先按指令做完、把结果写进现状；"
                       "角色复核通过即清账（不通过它会再下指令），"
                       "或调用 update_goal(blocked) 说明卡在哪。")
        # [画布 2026-10-19] **需求文档闸**：收口前，本卡正文必须至少有一条需求文档。
        #
        # 为什么卡在这里：需求文档是这张卡对外的**唯一结论形态**——画布上显示的是它，
        # 别的卡 read_card 读到的也是它。没有需求文档的卡，在画布上就只剩一个标题，
        # 用户扫完整棵树也不知道这条线决定了什么（实测 39 张卡只有 1 张写过需求文档）。
        # 这和上面那条"未回应询问不许收工"是同一类闸：收口是唯一宣称"做完了"的动作，
        # 那它必须留下"做完了什么"的凭据。
        #
        # 判据纯机械：正文里有没有 `D{nn}【…】`。正文为空（还没写任何内容）不拦——
        # 那种卡在画布上本来也无内容可显示，拦它只会挡住建卡流程。
        try:
            _cur = await card_store.read_card(work_id, current_id)
            _body = str(((_cur or {}).get("body") or {}).get("status_block") or "")
            _body += "\n" + str(((_cur or {}).get("body") or {}).get("requirements") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[自动模式] 读需求文档失败（放行）card=%s: %s", current_id, exc)
            _body = ""
        # 两个必须排除的"假需求文档/假正文"：
        #  ① 骨架示例行 `D01【裁定】……（日期·依据）`——模板占位不是决定，
        #     不排除的话每张新建卡一出生就"有需求文档"，闸等于不存在。
        #  ② 正文仍是骨架（现状块还留着"（当前状况快照：…"这段模板说明）——
        #     那说明这张卡还没写过任何内容，拦它只会挡住建卡流程。
        #     `create_card` 会把 NEW_CARD_SKELETON 写进正文，所以"空正文"在真实
        #     流程里几乎不存在，只能按"是否还是模板"来判。
        # [2026-10-22 用户裁定] 需求文档**不是只追加**——它是覆盖写·单一最新的需求确定源。
        # 闸的判据随之改为「需求文档块有实质内容」（非空、非骨架占位），不再要求 D{nn} 条目。
        _req = str(((_cur or {}).get("body") or {}).get("requirements") or "")
        # [2026-10-31 修] 判据改成**按块**，并与上下文层共用同一个 `_is_placeholder`
        # （单一口径）。改前是「现状块＋需求块拼一起，含标记子串就算没写」⇒ 两类误拒：
        #   ① 现状块还是骨架、需求块已写真内容 ⇒ 被拒「需求文档还停在骨架占位行」；
        #   ② 需求块沿用模板句起头再写真内容（card_001 正稿正是这么写的）⇒ 子串命中
        #      ⇒ 同样被误拒，且提示语指向一个不存在的状态。
        from .card_draft import _is_placeholder as _is_ph
        _unwritten = _is_ph(_req)
        if (_req.strip() and not _unwritten):
            pass   # 有实质内容 ⇒ 放行
        elif _req.strip() and _unwritten:
            return err("validation",
                       "收口被拒：本卡的需求文档还停在骨架占位行。"
                       "出路：用 edit_card 把 ## 需求文档 段整块写好——"
                       "按需求逐条写清「要什么、验收标准是什么」，再调 update_goal(complete)。")
        elif _body.strip():
            return err("validation",
                       "收口被拒：本卡还没有写需求文档。需求文档是这张卡对外的唯一结论形态——"
                       "画布上显示的是它，别的卡读到的也是它；没有它，这条线在画布上"
                       "就只剩一个标题。"
                       "出路：用 edit_card 把 ## 需求文档 段整块写好——"
                       "按需求逐条写清「要什么、验收标准是什么」，再调 update_goal(complete)。")
        # 全空 ⇒ 不拦（建卡流程与空卡本来无内容可显示）

        # [5713 收口闸 · card_023] **零角色收口闸**：项目根卡收口时，若 work 下
        # 有多张工作卡、却零个在盯的职能角色，收口被拒——「强制它做一步拆分团队」。
        # 判据与生产路径 goal_tools.h_update_goal 同源（单一 helper），插在需求文档闸
        # 之后、未回应询问闸之前（同属"收口必须留凭据"这一族）。
        # 方向变更（card_004-799a7954）：删掉「【无人盯】写理由即可放行」出口——
        # 唯一出路＝真的调 create_role 建成非空壳 active 角色；闸不规定拆法（守 D02）。
        _role_gate = await goal_tools._zero_role_close_gate_block(
            work_id, current_id, cards)
        if _role_gate is not None:
            return _role_gate

    # [6702 移植 2026-10-17 · P4 · 等子卡恢复位] **仅 pause** 且**当时仍有未终止子卡**
    # ⇒ 置位 ``goal_await_children=1``，把「模型主动 pause 等子卡」与「用户按停」区分开
    # （两者都只写 goal_state=paused，光看 goal_state 不可区分）。
    # 判据取「声明这一刻」的子卡状态（与 after_turn 的唤醒判据同源），落库即固定。
    # 无未终止子卡的 pause ⇒ 显式落 0（「不做了」，不是「等子卡」）；其余动作
    # （complete/blocked）**一个字都不改**（既有收口闸逐字不变）。
    # 显式 ``await_children`` 参数与 ``goal_await_wakes`` 计数见后续批次（P5/P6）。
    # [6702 移植 2026-10-17 · await_children 显式声明] 「是否在等子卡」此前是
    # **推断**出来的——判据取「声明这一刻」是否有未终止子卡。实测 work_015 证伪：
    # 模型 reason 写着「等两张子卡跑完再整合」，却可能在子卡**已全部 complete 之后**
    # 才调 pause ⇒ 采样点落在竞态另一侧 ⇒ 标志位 0 ⇒ 父卡永久挂死。
    # 根因＝**用瞬时采样代替模型意图**。故新增可选参数 ``await_children``：
    #   · 显式给了 True ⇒ 直接置 1，**不看** has_active_child（意图由模型声明）
    #   · 未给（False）⇒ **保留原推断分支**（有未终止子卡仍置 1），旧路径不退化
    #   · 两条都不放宽到「有子卡就置位」：那会把「子卡完工后再 pause（等人类
    #     外部条件）」也置位 ⇒ 恢复后再 pause 又置位 ⇒ 无限唤醒-挂起打转
    # complete/blocked **一个字都不改**（永不在此处置位）。
    # ★ 计数逻辑（goal_await_wakes）由 P6 在本分支内追加，本批只做标志位。
    fields: dict[str, Any] = {"goal_state": state}
    if action == "pause":
        from . import auto_driver      # 局部 import（同本模块既有延迟引用手法）
        if _as_bool(arguments.get("await_children")):
            fields["goal_await_children"] = 1
        else:
            fields["goal_await_children"] = (
                1 if auto_driver.has_active_child(work_id, current_id) else 0)
        # [等子卡活锁兜底 2026-10-17 · 口径与任务书的偏差（已裁定，非默默偏离）]
        # 计数列 ``goal_await_wakes`` 的语义＝「**连续空等重挂**次数」，判定与清零：
        #   · **空等重挂**（声明 ``await_children=true`` 而**此刻并无未终止子卡**——
        #     等待条件早已兑现却仍把自己挂起，正是实测 work_017 的活锁形态）⇒ **+1**；
        #   · **真等待**（声明时确有子卡在跑）⇒ **清零**（连击断裂：正常的
        #     「等→醒→干活→再等」循环每次都是真等待，计数永远攒不起来，永不误伤）；
        #   · 带宽裕度（``_AWAIT_RESUME_GIVEUP_SLACK``）地清零：见常量处说明——
        #     纯推断 pause（未传参）在「子卡恰好刚完工」的采样边界上也可能是活锁，
        #     但把 1 次清零不改变死锁判定的实效（死锁＝连续 ≥3）。
        #   · 换新目标生命周期 ⇒ 归零（``ensure_goal_from_message``）；
        #   · **不传参**的纯推断 pause ⇒ 宽裕度地清掉 1 次（实现见下：``max(0, wakes-1)``）。
        # ★ 为什么计数点在 pause 声明处而不是任务书原拟的 ``_resume_awaiting``：
        #   见 ``auto_driver._AWAIT_RESUME_DEADLOCK_GIVEUP`` 的口径段（①对齐「第 3 次
        #   起不再恢复」的实测序列；②恢复时 +1 会卡死合法的第 4 次真等待）。
        # ★ 本分支**只**动 pause 的 fields；上方 complete 的收口闸（未回应询问拒绝）
        #   与 blocked 通知路径**一字未动**。
        try:
            _wakes = int(auto_driver._await_wakes(
                auto_driver._meta(work_id, current_id)) or 0)
        except Exception:  # noqa: BLE001 读不出按 0（兜底计数，绝不阻断 pause 本身）
            _wakes = 0
        if _as_bool(arguments.get("await_children")):
            if auto_driver.has_active_child(work_id, current_id):
                fields["goal_await_wakes"] = 0        # 真等待 ⇒ 连击断裂
            else:
                fields["goal_await_wakes"] = _wakes + 1   # 空等重挂 ⇒ 计数 +1
        else:
            fields["goal_await_wakes"] = max(0, _wakes - _AWAIT_RESUME_GIVEUP_SLACK)
    note = f"[自动模式] 模型声明 {action}" + (f"：{reason}" if reason else "")
    res = await card_store.update_card(
        work_id, current_id, fields, note)
    if isinstance(res, dict) and res.get("error"):
        return err("internal", str(res["error"]))

    # [G4 · b6e339a 补记④] blocked ⇒ 系统自动把理由**通知发问方**。
    # work_028 实测：专家卡被闸逼到 blocked（合法出口），但发问方无从得知对方
    # 弃答——这次靠根卡机灵去读影子才发现，换个模型就会一直 pause 挂死。
    # 通知走流程专用薄壳 send_card_message（msg_notify：reply_expected=false，
    # 不会再造义务 ⇒ 无循环）；收件方＝每条未回应询问的发件卡；发不出只留痕
    # 不阻断 blocked 本身。
    # 目标终止 ⇒ **释放执行锁**（串行模式下队列前进；并行模式为 no-op）
    from . import serial_gate
    serial_gate.release(work_id, current_id)
    return {"ok": True, "action": action, "goal_state": state, "reason": reason}


# ============================================================
# [worker 卡 2026-10-15 · §六块2] worker 卡两件工具的 handler
#
# report_done（第 17 件·完工收口）与 create_worker_card（第 18 件·派生即调起）。
# report_done 照单执行：不验产物、不判真假、不查轮数；只做机械动作——判态、
# 落 state/coder_final、影子留痕、系统发回执（模型无发信工具，coder perms 不含）。
# ============================================================

def _count_write_file_calls(work_id: str, card_id: str) -> int:
    """机械统计本卡 tool_call 事件里 write_file 的调用次数（影子完工行用）。

    数据源＝works/{work}/events/{card}.jsonl（event_stream.emit 的落盘真相，
    本文件只读不改）。落盘是后台任务，调用方先 await event_log.drain() 等在途
    写完成再读；任何读取失败一律按 0 计（留痕不因统计失败而中断）。
    """
    try:
        from ..storage import event_log
        recs = event_log.read(work_id, card_id)
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("type") or "") != "tool_call":
            continue
        data = rec.get("data") or {}
        if isinstance(data, dict) and str(data.get("tool") or "") == "write_file":
            n += 1
    return n


#: [完工必达 2026-11-03] 最近一次 worker_done 回执的发送时刻（card_id → ts）。
#: 兜底网（card_store._worker_done_net）按「回执时刻 ≥ 完工时刻」判重——
#: report_done 链是先标完工再发信，它的回执必然晚于完工写入。
_CODER_DONE_SENT_AT: dict[str, float] = {}


def _collect_deliverables(work_id: str, card_id: str,
                          limit: int = 8) -> tuple[list[str], str]:
    """[验收指引 2026-11-03] 收 work 目录顶层与本卡同名的产物落点。

    命名约定实测多形：``receipt_card_187.md``／``card_181_receipt.md``／
    ``evidence_card_181``／``card_170/``（含回执＋证据）／``*_rework`` 改版
    后缀——统一用「文件名含 card_id（数字边界）」捞。返回 (落点行列表,
    回执摘录)：行按修改时间倒序、带类型与体积；摘录取最像回执的 .md 的
    开头一段（父卡不先找文件也有内容可看）。产物本体可能已被清理或本就
    没有——找不到返回空，不报错。
    """
    root = work_store._work_dir(work_id)
    pat = re.compile(re.escape(card_id) + r"(?!\d)")
    try:
        entries = []
        for e in os.scandir(root):
            if not pat.search(e.name):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, e.name,
                            e.is_dir(follow_symlinks=False), st.st_size))
    except OSError:
        return [], ""
    entries.sort(reverse=True)
    lines = []
    for _mt, name, is_dir, size in entries[:limit]:
        kind = "目录" if is_dir else f"{size / 1024:.1f}KB"
        lines.append(f"· works/{work_id}/{name}（{kind}）")
    receipt_md = ""
    for _mt, name, is_dir, _s in entries:
        if is_dir or not name.lower().endswith(".md"):
            continue
        if "receipt" in name.lower() or "回执" in name:
            receipt_md = name
            break
        if not receipt_md:
            receipt_md = name
    excerpt = ""
    if receipt_md:
        try:
            head = (root / receipt_md).read_text(encoding="utf-8",
                                               errors="replace")[:600].strip()
            if head:
                excerpt = f"（{receipt_md} 开头）\n{head}"
        except OSError:
            pass
    return lines, excerpt


async def _h_report_done(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[worker 卡 2026-10-15] 完工回报（系统收口件，模型只报不判）。

    四步机械动作（§六块2）：
      ① 判态：当前卡 card_type=="worker" 且 state=="active"，否则 denied；
      ② 落盘：state=completed（闸4：active→completed 在册）＋ front matter
         ``coder_final`` 原样收下 summary；
      ③ 影子 ``## 迭代历史`` 追加完工行（时间＋summary＋write_file 调用数，
         机械统计、不判好坏）；
      ④ 系统发信：经流程专用薄壳 send_card_message（六道门槛一条不减；头部
         由 build_header 生成，content 只给 summary）向 owner＝parent 卡发
         worker_done —— 落信即记 TRIGGERS，worker 卡回合收口时 K3 调起主卡验收
         （现有链，零改）。

    权限：不设新开关——工具面由 build_tool_schemas 第三段 WORKER_ONLY_TOOLS
    收敛到 worker；执行层本函数再校验 card_type（双保险）。
    """
    # ① 判态（先用本轮 cards 字面快照，再读库内实况——state 是库内事实）
    card = (cards or {}).get(current_id) or {}
    if str(card.get("card_type", "") or "") != "worker":
        return err("denied", "report_done 仅限worker 卡调用")
    current = await card_store.read_card(work_id, current_id)
    meta = current["meta"]
    state = str(meta.get("state", "") or "")
    if state != "active":
        return err("denied", f"worker 卡当前状态为 {state or '未知'}，不可完工")
    summary = str(arguments.get("summary", "") or "").strip()
    if not summary:
        return err("validation", "summary 不能为空")
    # [角色机制 5710 v3] worker 卡同样受指令闸约束：有未验收的角色指令不许完工。
    try:
        _blockers = role_room.open_directives_for(work_id, current_id)
    except Exception:  # noqa: BLE001
        _blockers = []
    if _blockers:
        _desc = "；".join(f"{d.get('id')}: {str(d.get('instruction') or '')[:60]}"
                          for d in _blockers[:3])
        return err("validation",
                   f"完工被拒：本卡还有 {len(_blockers)} 条未验收的角色指令"
                   f"（{_desc}）。先完成、把结果写进现状；角色复核通过即清账。")
    owner = str(meta.get("parent", "") or "") or work_store.work_card_id_of(work_id)

    # ② 落盘 state=completed ＋ coder_final。
    #    coder_final：「照 toolcard_final 手法」＝ front matter 键写库行——但
    #    card_store._UPDATABLE_META_KEYS / card_db._CARD_COLUMNS 属**禁改文件**
    #    （任务书 §四：card_store/card_db 只消费不改），故在本文件内经
    #    card_db.run_write 幂等补列（新库/存量库两路通吃）＋ UPDATE 落值。
    def _persist_coder_final(conn: Any) -> None:
        cols = conn.execute("PRAGMA table_info(cards)").fetchall()
        if not any(str(c["name"]) == "coder_final" for c in cols):
            conn.execute("ALTER TABLE cards ADD COLUMN coder_final TEXT")
        conn.execute(
            "UPDATE cards SET coder_final = ? WHERE card_id = ?",
            (summary, current_id),
        )

    try:
        await card_db.run_write(work_id, _persist_coder_final)
    except Exception as exc:  # noqa: BLE001 完工留痕失败要让模型看见
        logger.error("report_done coder_final 落盘失败 card=%s: %s", current_id, exc)
        return err("internal", f"coder_final 落盘失败: {exc}")
    upd = await card_store.update_card(
        work_id, current_id,
        # [goal 域 2026-10-20 · 单一写者] **不再直接写 goal_state**。
        # 之前这里顺手把 goal_state 置 complete，等于绕开 goal 域改相位——实测
        # card_029 因此出现「DB 说 complete、事件流说 active」，驱动照着事件流
        # 一直续轮（跑到 43 轮才被人叫停）。目标相位只能由 GoalService 写。
        {"state": "completed"},
        f"worker 卡完工收口（report_done）：{summary[:80]}",
    )
    if isinstance(upd, dict) and upd.get("error"):
        return err("internal", str(upd["error"]))
    # [goal 域 2026-10-20] 完工 ⇒ 走 GoalService.complete（写 goal/change 事件 +
    # 投影），不碰列。目标已不在 active 就跳过（幂等）。
    try:
        from ..goal.runtime import GoalError
        from ..goal.service import get_service as _goal_service
        _svc = _goal_service()
        _goal = await _svc.get(work_id, current_id)
        if _goal is not None and _goal.phase == "active":
            await _svc.complete(work_id, current_id, _goal.ref())
    except GoalError:
        pass
    except Exception as exc:  # noqa: BLE001 收口失败不阻断完工本身
        logger.warning("[goal] report_done 收口失败 card=%s: %s", current_id, exc)
    # 同轮后续工具能看到新状态（与 _h_create_card 同手法的缓存同步）
    cards[current_id] = {**upd.get("meta", {}), "card_id": current_id}

    # ③ 影子完工行（时间＋summary＋write_file 调用数；机械统计）
    try:
        from ..storage import event_log as _event_log
        await _event_log.drain()
    except Exception:  # noqa: BLE001
        pass
    n_write = _count_write_file_calls(work_id, current_id)
    now = datetime.now().isoformat(timespec="seconds")
    await shadow_store.append_to_section(
        work_id, current_id, "## 迭代历史",
        [f"- [{now}] 完工：{summary}（write_file 调用 {n_write} 次）"],
    )

    # ④ 系统发信（回执由系统发，模型全程不碰发信工具）
    # [差事卡 2026-11-01] 回执尾部带验收提示：coding 完工不等于阶段验收过——
    # 要不要召 QA 由主卡裁，口子给在回执里。
    # [验收指引 2026-11-03 · 用户令「完工没汇报，父卡不知道验收内容在哪」]
    # 回执机械附上本卡产物落点＋回执摘录——父卡拿到路径直接读，不再上
    # 盘面搜文件；摘录让汇报内容即达，不用先找到文件才有东西看。
    _content = str(summary)
    _arts, _excerpt = _collect_deliverables(work_id, current_id)
    if _arts:
        _content += "\n\n——【验收指引】产物落点：\n" + "\n".join(_arts)
    if _excerpt:
        _content += f"\n\n——【回执摘录】{_excerpt}"
    _content += (f"\n\n——【可验收】如需验收本卡产物："
                 f"create_deploy_card(function=\"qa\", "
                 f"target=\"{current_id}\", task=\"验收本卡产物\")")
    sent = await send_card_message(
        work_id, current_id, target_id=owner,
        msg_type="worker_done",
        content=_content,
    )
    if isinstance(sent, dict) and sent.get("error"):
        # 落信失败：卡已完工、影子已留痕——错误如实回给模型（不回滚终态）
        logger.error("report_done 回执发信失败 card=%s owner=%s: %s",
                     current_id, owner, sent)
        return err("internal", f"完工回执发送失败: {sent.get('error')}")
    # [完工必达] 登记本次回执时刻——update_card 的兜底网据此判重。
    _CODER_DONE_SENT_AT[current_id] = datetime.now().timestamp()
    return {
        "ok": True,
        "card_id": current_id,
        "state": "completed",
        "coder_final": summary,
        "write_file_calls": n_write,
        "receipt_msg_id": str(sent.get("msg_id") or ""),
        "owner": owner,
        "note": "已完工：本卡静默，回执已由系统发给主卡",
    }


async def _h_create_worker_card(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[worker 卡 2026-10-15] 派生worker 卡并**建卡即首跑**（派生即调起）。

    §六块2：
      ① 建卡关口＝CREATE_CHILD（照 _h_create_card 同口径，parent＝当前卡自身）；
      ② card_store.create_card(card_type="worker", parent=当前卡,
         charter=带 ``## 使命`` 区头的 task) ⇒ 任务落使命块；
      ③ asyncio.create_task(_routes._run_message(…, append_user=False))
         用 message_runner._spawn_tracked 包（强引用防 GC，照 K3 手法）。
         注意：create_task 在工具 handler 内、回合未收口——K3 补齐不会覆盖
         它（新卡无 pending 信、TRIGGERS 无它），此直调是首跑唯一路径；
      ④ 返回 ok ＋ card_id ＋ title ＋ started。
    """
    title = str(arguments.get("title", "") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    task = str(arguments.get("task", "") or "").strip()
    if not task:
        return err("validation", "task 不能为空")
    if current_id not in cards:
        return err("not_found", f"卡片不存在: {current_id}")
    # ① 建卡关口（与 _h_create_card 完全同口径：parent＝当前卡自身）
    # [离开树的角色卡 2026-10-22 · 用户裁定] 新增可选 parent：
    #   · 不传 → 挂在当前卡下（原语义，逐字不变）
    #   · 传了 → 走 _org_scope_allows：树内卡仍限自己子树；
    #            离树角色卡可挂本项目树内任意位置（不含根卡）
    # 为什么角色卡需要：它要「挂载需要的卡片来执行它布置的任务」，
    # 而它自己不在树里——不让它指定 parent，它建出来的执行卡无处可挂。
    _worker_parent = str(arguments.get("parent", "") or "").strip() or current_id
    if _worker_parent not in cards:
        return err("not_found", f"父卡不存在: {_worker_parent}")
    if not _org_scope_allows(current_id, _worker_parent,
                             policy.SWITCH_CREATE_CHILD, cards):
        return err(
            "denied",
            f"越权：不能把执行卡建到 {_worker_parent} 之下"
            f"（当前卡 {current_id}）",
        )
    # ② 建 worker 卡：charter 带 ## 使命 区头 ⇒ 区头检测后原样入正文，任务落使命块
    # [2026-10-19 实验 permexp] **补 goal**：原先不传 goal ⇒ 自动模式下 worker 卡
    # goal 空、goal_state 不落 ⇒ auto_driver.will_continue 恒 False ⇒ 只有下面
    # ③ 的首跑一次，「一轮做不完就停在那里」。任务原文即目标文本（使命块同源）。
    result = await card_store.create_card(
        work_id, title, "模型生成",
        card_type="worker", parent=_worker_parent,
        charter=f"## 使命\n{task}",
        goal=task,
        # [调度改造 2026-10-19] 本工具**自己负责首跑**（下面 ③ `_spawn_tracked`
        # 带任务正文）。若建卡也点一次，会起一个没有任务正文的空回合，把正式首跑
        # 挡在互斥体外面 ⇒ worker 卡零产出（实测 test_worker_card 两条都挂）。
        skip_kick=True,
        # [2026-10-26 · 记录补全] 记下派发者（派生worker 卡 = 父卡派活）
        created_by=current_id,
    )
    if "error" in result:
        return result
    new_id = result["meta"]["card_id"]
    cards[new_id] = {**result["meta"], "card_id": new_id}
    # ③ 建卡即首跑（唯一路径，见 docstring）
    started = False
    try:
        from . import message_runner as _message_runner
        from . import routes as _routes
        owner_title = str(
            (await card_store.read_card(work_id, current_id))["meta"].get("title")
            or current_id
        )
        prompt = (
            f"卡 {current_id}（《{owner_title}》）给你发来开发任务。\n"
            f"任务内容：\n{task}"
        )
        first_run = _message_runner._spawn_tracked(
            _routes._run_message(work_id, new_id, prompt, None, append_user=False)
        )
        first_run._card_msg_target = new_id  # type: ignore[attr-defined]
        started = True
    except Exception as exc:  # noqa: BLE001 首跑拉起失败不吞——如实回报 started=False
        logger.error("create_worker_card 首跑拉起失败 card=%s: %s", new_id, exc)
        started = False
    return {
        "ok": True,
        "card_id": new_id,
        "title": title,
        "parent": current_id,
        "card_type": "worker",
        "started": started,
        "shadow": "已派生影子卡（过程记录，你不用管）",
    }


# [UI 卡不编程 2026-11-05 · 用户令] UI 卡的分流不做文本闸——把编程能力物理
# 拿掉：ui 在 permissions.json 没有 tools_file（write/edit/bash/python 全断），
# 模式能力清单里也没有 write_files/run_code。错派进来的编码活它**干不了**
# （没工具），只能如实 report_up 说明——错误显形而不是静默执行。


async def _h_create_ui_card(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[UI 卡 2026-11-02] 派生 UI 卡并**建卡即首跑**（同 create_worker_card 手法）。

    与 worker 卡同构、两点不同：
      ① card_type="ui"——不在 wb_exec 委派名单里 ⇒ 跑系统内模型回合
        （message_runner 内循环），手里有平台工具面；
      ② 汇报通道走 report_up（作业卡口径），不走 worker 的 report_done。
    任务落 ``## 使命`` 块＋goal；首跑经 _spawn_tracked 直调，唯一入口。
    """
    title = str(arguments.get("title", "") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    task = str(arguments.get("task", "") or "").strip()
    if not task:
        return err("validation", "task 不能为空")
    if current_id not in cards:
        return err("not_found", f"卡片不存在: {current_id}")
    _ui_parent = str(arguments.get("parent", "") or "").strip() or current_id
    if _ui_parent not in cards:
        return err("not_found", f"父卡不存在: {_ui_parent}")
    if not _org_scope_allows(current_id, _ui_parent,
                             policy.SWITCH_CREATE_CHILD, cards):
        return err(
            "denied",
            f"越权：不能把执行卡建到 {_ui_parent} 之下"
            f"（当前卡 {current_id}）",
        )
    result = await card_store.create_card(
        work_id, title, "模型生成",
        card_type="ui", parent=_ui_parent,
        charter=f"## 使命\n{task}",
        goal=task,
        skip_kick=True,
        created_by=current_id,
    )
    if "error" in result:
        return result
    new_id = result["meta"]["card_id"]
    cards[new_id] = {**result["meta"], "card_id": new_id}
    started = False
    try:
        from . import message_runner as _message_runner
        from . import routes as _routes
        owner_title = str(
            (await card_store.read_card(work_id, current_id))["meta"].get("title")
            or current_id
        )
        prompt = (
            f"卡 {current_id}（《{owner_title}》）给你发来 UI 任务。\n"
            f"任务内容：\n{task}"
        )
        first_run = _message_runner._spawn_tracked(
            _routes._run_message(work_id, new_id, prompt, None, append_user=False)
        )
        first_run._card_msg_target = new_id  # type: ignore[attr-defined]
        started = True
    except Exception as exc:  # noqa: BLE001 首跑拉起失败不吞——如实回报 started=False
        logger.error("create_ui_card 首跑拉起失败 card=%s: %s", new_id, exc)
        started = False
    return {
        "ok": True,
        "card_id": new_id,
        "title": title,
        "parent": _ui_parent,
        "card_type": "ui",
        "started": started,
        "shadow": "已派生影子卡（过程记录，你不用管）",
    }


# [差事卡 2026-11-01 · 用户令「建卡即召唤」] 职能 → 差事卡型：场卡挂在
# 召唤方树下（看得见），会话装职能的差事过程；卡自身不跑日常回合。
_DEPLOY_VENUE_TYPES: dict[str, str] = {"qa": "qa_card", "pm": "pm_card"}


async def _h_create_deploy_card(work_id: str, current_id: str,
                                arguments: dict[str, Any],
                                cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """建一张差事卡＝把职能部署进场干活，一次调用到底。

    流程（与 create_worker_card 同构）：
      ① 资格闸：职能存在且有 ``charters.deploy``（目录第一级——现在只有
        qa/pm 有；无派驻职责书的职能不接差事，召唤方和它的职责书互不欠债）；
      ② 对象闸：``target`` 缺省＝本卡；只许本卡或直接子卡（阶段验收验自己、
        替子卡执笔/验收，都出不了这一层）；
      ③ 建卡：``_DEPLOY_VENUE_TYPES`` 映射卡型，parent＝本卡，使命＝task，
        skip_kick（卡不自跑，唯一回合是差事回合）；
      ④ 落账：``create_deployment``（delegator＝本卡、target_card＝对象卡、
        exit_spec＝目录条目 ``deploy_exit``、tools＝``grant_for(,'deploy')``）
        并激活——**建账先于唤起**（幂等，重演不双开）；
      ⑤ 唤起：``_spawn_tracked`` 背景跑 ``routes._run_message(act_as=职能)``
        ——差事卡是新卡恒闲，不等谁的锁；办完的判定交给差事判据。
    """
    fn_key = str(arguments.get("function") or "").strip()
    task = str(arguments.get("task") or "").strip()
    if not fn_key:
        return err("validation", "function 必填：召哪个职能（目录 code，如 qa/pm）")
    if not task or len(task) < 8:
        return err("validation",
                   "task 必填且要真职责：召它来干什么、干到什么程度算完")
    target = str(arguments.get("target") or "").strip() or current_id
    if target not in cards:
        return err("not_found", f"对象卡不存在: {target}")
    if target != current_id and str(
            (cards.get(target) or {}).get("parent") or "") != current_id:
        return err("denied",
                   f"越权：差事只能对着本卡或直接子卡派（{target} 不是你的子卡）")

    # ① 职能解析＋资格闸（无派驻职责书 ⇒ 它不接差事）
    from . import function_room as _fr
    from ..storage import function_store as _fs
    prof, fid = _fr._profile_for(work_id, fn_key)
    code = _fr.catalog_code_of(prof) or fn_key
    if not fid or not _fs.function_exists(work_id, fid):
        return err("not_found", f"职能不存在: {fn_key}")
    if _fr.entries() and not _fr.venue_charter_for(code, "deploy"):
        return err("denied",
                   f"{code} 没有派驻职责书——它不接差事，派了也干不好")
    card_type = _DEPLOY_VENUE_TYPES.get(code)
    if not card_type:
        return err("denied",
                   f"{code} 没有差事卡型——现在只有 qa(验收)/pm(执笔) 接差事")

    # ② 建差事卡（挂本卡树下；使命＝task，不自跑）
    tgt_title = str((cards.get(target) or {}).get("title") or target)
    label = "验收" if code == "qa" else ("执笔" if code == "pm" else code)
    title = str(arguments.get("title") or "").strip() or f"{label}·{tgt_title}"
    mission = task if target == current_id else f"{task}（对象卡：{target}）"
    result = await card_store.create_card(
        work_id, title, "模型生成",
        card_type=card_type, parent=current_id,
        charter=f"## 使命\n{mission}",
        skip_kick=True, created_by=current_id)
    if "error" in result:
        return result
    new_id = result["meta"]["card_id"]
    cards[new_id] = {**result["meta"], "card_id": new_id}

    # ③ 落差事账＋激活
    try:
        entry = _fr.entry(code) or {}
        exit_spec = str(entry.get("deploy_exit") or "report")
        tools = sorted(set(_fr.grant_for(work_id, fid, "deploy") or []))
        ident = _fr.identity_for(work_id, fid, "deploy")
        # kick 正文＝职责书＋对象卡事实＋交差说明。两条执行路径共用：
        # 系统模型走 act_meta 身份＋工具；wb 执行体只有这段文本＋工作目录
        # 文件——所以对象卡的事实（验收的尺、执笔的据）必须落进正文。
        charter = str(_fr.venue_charter_for(code, "deploy") or "")
        try:
            _tc = await card_store.read_card(work_id, target)
            _tb = _tc.get("body") or {}
            _tm = _tc.get("meta") or {}
        except Exception:  # noqa: BLE001 读不到对象卡就裸差事
            _tb, _tm = {}, {}
        _facts = [
            f"对象卡：{target}（{tgt_title}）",
            f"使命：{str(_tb.get('mission') or '（空）')[:800]}",
        ]
        _req = str(_tb.get("requirements") or _tm.get("requirements_draft")
                   or "").strip()
        _facts.append(f"需求文档：{(_req[:1500] if _req else '（还没有）')}")
        if code == "qa":
            _how = ("这是阶段验收：对照需求文档逐条判产物现状。"
                    "办完用 report 工具把结论回给派遣方；若你在执行体里干活，"
                    "把完整验收结论写在最终回复正文（逐条过/不过＋证据＋总结论）。")
        elif code == "pm":
            _how = ("这是执笔：为对象卡写需求文档。"
                    "办完用 edit_requirements 工具把草稿写到对象卡；若你在执行体"
                    "里干活，把完整需求文档正文写在最终回复正文。")
        else:
            _how = "办完用 report 工具回执派遣方。"
        text = (f"{charter}\n\n" if charter else "") + \
               f"【差事】{task}\n派遣方：{current_id}\n" + \
               "\n".join(_facts) + f"\n\n{_how}"
        dep = await _fs.create_deployment(
            work_id, fid, venue_kind="deploy", venue_ref=new_id,
            venue_label=title, mandate=text, delegator=current_id,
            target_card=target, tools=tools,
            exit_kind="deliverable", exit_spec=exit_spec,
            meta={"deploy_card": True})
        did = int(dep.get("id") or 0)
        await _fs.activate_deployment(work_id, did)
    except Exception as exc:  # noqa: BLE001 落差事失败：卡已建——如实回报不吞
        logger.error("create_deploy_card 落差事失败 card=%s fn=%s: %s",
                     new_id, fid, exc)
        return err("internal", f"差事卡已建({new_id})但差事落账失败: {exc}")

    # ④ 唤起职能进场（背景任务；差事卡恒闲不等锁）
    started = False
    try:
        from . import message_runner as _message_runner
        from . import routes as _routes
        run = _message_runner._spawn_tracked(_routes._run_message(
            work_id, new_id, text, append_user=False,
            source={"kind": "deploy", "deployment_id": did,
                    "delegator": current_id},
            act_as=fid, act_meta=ident, ctx_card=new_id,
            tool_allow=set(dep.get("tools") or [])))
        run._card_msg_target = new_id  # type: ignore[attr-defined]
        started = True
    except Exception as exc:  # noqa: BLE001 唤起失败如实回报——差事账在可重唤
        logger.error("create_deploy_card 唤起失败 card=%s dep=%s: %s",
                     new_id, did, exc)
    return {
        "ok": True,
        "card_id": new_id,
        "card_type": card_type,
        "title": title,
        "parent": current_id,
        "function": code,
        "function_id": fid,
        "deployment_id": did,
        "target_card": target,
        "started": started,
        "note": "差事已落账、职能进场中；它办完会自动收工，结论回执到本卡。",
    }


async def _h_report_deploy(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[差事卡 2026-11-01] 差事回执：向**派遣方**汇报结论——正文落派遣方
    会话（``msg_report``）＋唤醒。收件人由差事账算（``delegator``），
    汇报人不能选——你在哪张差事卡上干活、结论就回给谁派的活。

    与 report_up 的区别：report_up 认父子线（卡→父卡）；report 认差事账
    （职能→派遣方）——职能不在树里没有父卡，差事账就是它唯一的上行线。
    """
    content = str(arguments.get("content") or "").strip()
    if not content:
        return err("validation", "content 不能为空")
    from ..storage import function_store as _fs
    deps = _fs.active_for_function(work_id, current_id)
    if not deps:
        return err("denied",
                   "你没有在派的差事——report 是差事回执；对卡汇报用 report_up")
    dep = deps[0]
    to = str(dep.get("delegator") or "") or str(dep.get("target_card") or "")
    if not to or to not in cards:
        return err("internal", f"差事 {dep.get('id')} 没有可汇报的派遣方")
    title = str(dep.get("venue_label") or dep.get("venue_ref") or "")
    text = f"【差事回执·{title}({current_id})】\n{content}"
    from . import message_runner as _mr
    try:
        _rec = await session_facade.append_card_message(
            work_id, to, from_card=current_id, text=text,
            msg_type="msg_report")
        _mid = str((_rec or {}).get("msg_id") or "")
        _mr._spawn_tracked(_mr.deliver_now(
            work_id, to,
            text=f"你收到一条来自 {current_id} 的差事回执，正文已在上文。",
            source={"kind": "card_msg", "from": current_id}))
    except Exception as exc:  # noqa: BLE001 投递失败要如实回给模型
        logger.warning("[report] 投递失败 to=%s: %s", to, exc)
        return err("internal", f"差事回执投递失败: {exc}")
    return {"ok": True, "delivered": [{"to": to, "msg_id": _mid}],
            "deployment_id": int(dep.get("id") or 0)}


async def _h_draft_rule_edit(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[规则卡 2026-10-15 · 块3] 根卡起草规则区修改——**只起草不落盘**。

    机械四步：
      ① 根卡闸：current_id 必须是剧本根卡（work_card_id_of），否则 denied；
      ② 归一参数：``sections=[{section,content}…]`` 多区一次起草，或
         ``section``+``content`` 单区简形（等价单元素数组）——**写哪个区是
         模型自己选的**，一篇治理文档该拆几个区就拆几个区，不必硬塞进一个区；
      ③ 区名校验＋内容闸：区名必须在合法名单；**内容不得含 `## ` 行**——
         文件按 `## ` 切区，内容里的 `## ` 行下次读取会被切成无人消费的幻影区
         （2026-11-01 work_027 实案：整篇带 `## 一、`…`## 七、` 的文档落进
         project 区后被切碎，规则静默零注入）；
      ④ 返回 ``{sections: [{section, old, draft, base_hash}…]}``——draft 即
         提议新文、old 即盘上现文、base_hash＝sha256(old)（供 /rule/commit
         逐区乐观比对）。**全程零写入**：落盘只能由用户在弹窗点「同意」后走
         ``POST /works/{w}/rule/commit``。

    权限：TOOL_SWITCH 挂 SWITCH_READ；工具面由第四段 ROOT_ONLY_TOOLS 收敛到
    根卡——本函数开头的闸是执行层双保险（同 report_done 手法）。
    """
    if current_id != work_store.work_card_id_of(work_id):
        return err("denied", "draft_rule_edit 仅限剧本根卡调用")
    from ..storage import rule_store as _rule_store
    raw_sections = arguments.get("sections")
    pairs: list[tuple[str, str]] = []
    if raw_sections is not None:
        if not isinstance(raw_sections, list):
            return err("validation",
                       "sections 须为 [{section, content}, …] 数组")
        for i, it in enumerate(raw_sections):
            if not isinstance(it, dict):
                return err("validation",
                           f"sections[{i}] 须为 {{section, content}} 对象")
            pairs.append((str(it.get("section") or "").strip(),
                          str(it.get("content") or "")))
    else:
        pairs = [(str(arguments.get("section") or "").strip(),
                  str(arguments.get("content") or ""))]
    if not pairs or not any(sec or content for sec, content in pairs):
        return err("validation", "sections 不能为空：至少一个 {section, content}")
    from . import function_room as _fr
    names = context_regions.rule_section_names(
        card_store.load_type_registry(),
        fn_codes=[str(e.get("code") or "")
                  for e in (_fr.entries() or [])])
    seen: set[str] = set()
    for sec, content in pairs:
        if sec not in names:
            return err("validation", f"非法区名: {sec}（只允许 {names}）")
        if sec in seen:
            return err("validation", f"同一草案里区 {sec!r} 出现多次")
        seen.add(sec)
        bad = _rule_store.find_section_header_lines(content)
        if bad:
            return err(
                "validation",
                f"区 {sec} 内容不得含 `## ` 行（会被切成幻影区，规则静默不进"
                f"上下文）：{bad[0]}。区内子标题用 `### ` 或更深；要进不同的区"
                "请拆成多个 section 条目")
    # [规则节点 2026-10-28 · 用户令「一个项目一个文件」] 规则读
    # ``works/<work_id>/rules.md``（``work_store.get_rules`` 已转发到 rule_store，
    # 内含 work.json 旧形态的惰性迁移）。不再有「扫规则卡 mission」的兜底：
    # 规则不是卡，没有卡可扫。
    from ..storage import work_store as _ws
    _rules = _ws.get_rules(work_id)
    out = []
    for sec, content in pairs:
        old = str(_rules.get(sec, "") or "")
        out.append({
            "section": sec,
            "old": old,
            "draft": content,
            "base_hash": hashlib.sha256(old.encode("utf-8")).hexdigest(),
        })
    return {"sections": out}


async def _h_propose_status(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[更新现状按钮改造 2026-10-16] 提议本卡「现状」新正文——**只提议、不落盘**。

    机械三步：
      ① 取本卡当前 status_block（空/占位符当空；口径与 card_draft 同源——
         `current_turn.read_status_block` 单一取数口，不另判一套）；
      ② base_hash＝sha256(old)（与 `card_draft._block_hash` 同算法，供 /commit 乐观比对）；
      ③ 返回 ``{proposed, old, base_hash}``——proposed 即提议新正文、old 即盘上现文。
    **全程零写入**：落盘只能由用户在对比窗点「同意」后走
    ``POST /works/{w}/cards/{c}/commit``（复用 card_draft.commit 的整块替换）。

    可见性：本工具列 ``OP_ONLY_TOOLS``——**不进常规卡片工具面**，只在
    op=update_status 的专用回合由 ``build_tool_schemas`` 追加（用户 2026-10-16 口径：
    「不点按钮，模型无法看到并调用它」）。执行层不另设权限关口——它零写入，
    且「谁能调」已由工具面唯一决定。
    """
    content = str(arguments.get("content") or "")
    if not content.strip():
        return err("validation", "content 不能为空：请把「新现状」内容交给本工具")
    from .current_turn import read_status_block  # noqa: PLC0415 延迟 import 破环
    old = read_status_block(work_id, current_id)
    return {
        "proposed": content,
        "old": old,
        "base_hash": hashlib.sha256(old.encode("utf-8")).hexdigest(),
    }


def _card_body_text(card: dict[str, Any]) -> str:
    """read_card 结果 → 「正文全文」（三块带标准新形态区头）。

    [卡片存储迁移 SQLite 2026-10-14] 库内正文三块**各占独立列**、不再有整篇
    文本（read_card 的 ``raw`` 已退役）。``_check_block_rules`` 要的是可拆块
    的正文，这里由库列按标准区头重组——重组后 ``split_body_sections`` 拆回
    同样三块。取什么查什么：不引入第二份真值。
    """
    b = card.get("body") or {}
    return (
        f"## 使命\n{b.get('mission', '')}\n"
        f"## 现状\n{b.get('status_block', '')}\n"
        f"## 需求文档\n{b.get('requirements', '')}\n"
    )


#: 三块 → 中文块名（写工具的返回里用中文，与卡面一致）
_BLOCK_NAMES = (("mission", "使命"), ("status_block", "现状"),
                ("requirements", "需求文档"))


async def _landed_payload(work_id: str, card_id: str,
                          before: dict[str, Any]) -> dict[str, Any]:
    """写工具的**落面回执**（[2026-10-27 用户裁定]）：

    「工具结果不应该只给『改成功了』，把改完后的内容一并推给他。」

    依据是实跑复盘：`work_027` 里 **91 次**是「刚写完就 read_card 回读」——
    因为写工具只回 `{ok:true}`，卡面到底变成什么样它不知道，只能再读一遍。

    返回：变化的块**全文**（没变的不重复推，只给字数）＋ 三块字数。
    这样"我改的那块成了什么样""别的块有没有被顺手改掉"一次就能看全。
    """
    after = (await card_store.read_card(work_id, card_id)).get("body") or {}
    changed: dict[str, str] = {}
    chars: dict[str, int] = {}
    for key, name in _BLOCK_NAMES:
        new = str(after.get(key) or "")
        chars[name] = len(new)
        if new != str(before.get(key) or ""):
            changed[name] = new
    return {"changed": list(changed), "blocks": changed, "chars": chars}


#: 块名 → 库列。块名用卡面上看得见的中文，不用 D1/D2 那套遗留编号。
#: [2026-10-27] edit_card 改成精准编辑后，这是「改哪一块」的唯一入口表。
_BLOCK_FIELD = {"使命": "mission", "现状": "status_block", "需求文档": "requirements"}

# ---------------------------------------------------------------------------
# [错放提醒 2026-11-02 · 用户令] 模型常把历史/备忘写进现状或需求文档——
# 这两块只放「现在成立」的东西（台账正是因同一理由退役）。写工具的
# new_string 扫一遍历史形态，命中只在落面回执附一句软提醒——**不拒**：
# 误报的代价是丢事实，比污染更糟。
# ---------------------------------------------------------------------------
_HIST_HEAD_RE = re.compile(r"历史|日志|记录|裁定|变更|更新")
_DATE_LINE_RE = re.compile(
    r"(?:^|\n)\s*[-•·*＊>＞]?\s*[【\[（(]?\s*(?:19|20)\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}")


def _history_like_note(new_text: str) -> str:
    """``new_string`` 长得像「历史/备忘」⇒ 一句软提醒文案；否则空串。

    判据刻意保守（宁漏勿报）：
    - 有区头/标题行含 历史/日志/记录/裁定/变更/更新；
    - 或 ≥2 行以日期开头（流水账形态）；
    - 或出现 「旧：」「已过期」「已作废」「（原：」并列标记（新旧并存的账本写法）。
    """
    t = str(new_text or "")
    if not t.strip():
        return ""
    head = any(
        _HIST_HEAD_RE.search(ln)
        for ln in t.splitlines()
        if ln.strip().startswith(("#", "【", "＊", "*", "=", "—"))
    )
    dated = len(_DATE_LINE_RE.findall(t)) >= 2
    marked = any(m in t for m in ("旧：", "已过期", "已作废", "（原：", "(原:"))
    if not (head or dated or marked):
        return ""
    return ("提醒：刚写入的内容像历史/备忘（区头、日期堆叠或新旧并列）。"
            "过程与判断依据该写影子「笔记」（edit_note）；「改过什么」不用记，"
            "系统自动留痕——本次已落面，放错的话请移过去。")

#: [影子精准编辑 2026-10-27] 模型侧节名 → 库内节头。四节固定，不许新增
#: （笔记是第五区，但它走 `edit_note`，不在这里）。
_SHADOW_SECTION_BY_NAME: dict[str, str] = {
    "进度": "## 进度",
    "待协作事项": "## 待协作事项",
    "产物清单": "## 产物清单",
    "迭代历史": "## 迭代历史",
}


#: 匹配用归一化表（[2026-10-27 用户裁定「按 Codex 的做」]）。
#: 只解决一件事：**长得一样但编码不同的字符**——模型抄原文时最容易把中文标点
#: 抄成 ASCII（“ ” → " "、「」→ ""、全角空格 → 半角），肉眼一模一样、逐字节不等，
#: 于是明明在卡面上却报「找不到原文」。
#: **只在定位时用，写回永远用卡面上的原文**（否则会把中文标点改坏）。
#: 逐字符 1:1 映射（不 trim、不折叠），这样归一化后的下标与原文下标一一对应。
_MATCH_NORM = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ",
    "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ",
    "\u3000": " ",
}


def _norm_for_match(s: str) -> str:
    """把「字形相同、编码不同」的字符统一成 ASCII（长度不变，下标可对齐）。

    比 Codex 多一条：**全角 ASCII 段（U+FF01–U+FF5E）整体折成半角**。
    理由：我们的卡面是中文，`：，（）；` 这些全角标点与半角同形，模型抄原文时
    混用极常见；Codex 面对英文源码用不着这条，我们绕不开。
    不做的：`「」『』《》` 不映射——它们与 ASCII 引号字形不同，混用不是「编码差异」。
    """
    out: list[str] = []
    for c in s:
        mapped = _MATCH_NORM.get(c)
        if mapped is not None:
            out.append(mapped)
            continue
        code = ord(c)
        # 全角 ！＂＃…～ → 半角 !"#…~
        out.append(chr(code - 0xFEE0) if 0xFF01 <= code <= 0xFF5E else c)
    return "".join(out)


# [卡面版本 2026-10-24 · 用户令「加历史版本记录，让模型操作失误后找回」]
# 版本查看与回退**挂在卡面编辑工具上**：模型改坏了，用同一个 edit_card 就能
# 看到有哪些版本、回到哪一版，不必再学第二个工具。
# 版本存的是「那次改动**之前**那块的内容」⇒ 回退到 N ＝ 回到那次改动之前。
_VERSION_HINT = ("回退：edit_card(card_id=…, restore_version=N)。"
                 "版本存的是「那次改动之前的内容」，回退到 N＝回到那次改动之前；"
                 "回退本身也会先记一版，回退错了还能再退回来。")


async def _edit_card_versions(work_id: str, target_id: str, list_only: bool,
                              restore_raw: Any) -> dict[str, Any]:
    """edit_card 的版本分支：``list_only`` 列版本；否则按 ``restore_raw`` 回退。"""
    if list_only:
        vers = await card_db.run_read(
            work_id, lambda conn: card_db.list_revisions(conn, target_id),
            default=[])
        return ok_({"card_id": target_id, "versions": vers, "hint": _VERSION_HINT})
    try:
        ver = int(restore_raw)
    except (TypeError, ValueError):
        return err("validation", f"restore_version 必须是整数，收到 {restore_raw!r}")
    blocks = await card_db.run_read(
        work_id, lambda conn: card_db.get_revision(conn, target_id, ver),
        default={})
    if not blocks:
        return err("not_found",
                   f"版本 {ver} 不存在。先用 list_versions=true 看这张卡有哪些版本")
    r = await card_store.update_card(
        work_id, target_id, blocks, f"从版本 {ver} 恢复（回退）")
    if "error" in r:
        return err("validation", str(r["error"]))
    return ok_({"card_id": target_id, "restored_from": ver,
                "restored_blocks": sorted(blocks)})


async def _h_edit_card(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]],
                        *, forced_block: str = "",
                        field_override: str = "") -> dict[str, Any]:
    """**精准编辑**卡面某一块里的一处文本（[2026-10-27 用户裁定] 编辑工具迭代）。

    改前是「整篇覆盖」：一次手滑把 45,935 字写成 351 字（card_067 实证）；
    只提交两块时第三块还会被清空。用户原话：「编辑 md 文件的时候，都是可以精准编辑的」。

    现在只接受「把这段换成那段」：另外两块**根本不进写入参数** ⇒ 结构上不可能被清空。
    整块重写仍有各自通道：`update_status`（现状）、`append_requirements(text=)`（需求文档）、
    `set_child_mission`（使命，上级给子树内卡设定）。使命锁对精准编辑同样生效。
    """
    target_id = arguments.get("card_id", "")
    # [语义修正 2026-10-02] 先判存在再判权限
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    # [影子卡 2026-10-03] 影子条目进字典后子树判据会放行正卡的上级，
    # 此守卫封死该后门：影子只能由对应正卡经 update_shadow 修改（R-06/R-09 写侧）
    if cards[target_id].get("card_type") == "shadow":
        return err("denied", "影子卡只能由对应正卡经 update_shadow 修改")
    # [能力底座 2026-10-30] 改判据：能力「改别人的卡面」＋ 作用域（子树）
    _bad = _cap_gate(current_id, "edit_other_face", policy.SWITCH_EDIT,
                     target_id, cards, "编辑", work_id=work_id)
    if _bad is not None:
        return _bad
    # [卡面版本 2026-10-24] 版本查看 / 回退——与正文写入互斥，且**先于写入闸**：
    # 回退是回到已知正确状态，不该被「使命锁 / 需求文档」那套写入闸拦。
    list_only = bool(arguments.get("list_versions"))
    restore_raw = arguments.get("restore_version")
    if list_only or restore_raw not in (None, "", 0):
        return await _edit_card_versions(work_id, target_id, list_only, restore_raw)

    # ---- [2026-10-27 用户裁定] 精准编辑 ----
    block = str(arguments.get("block") or "").strip()
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    replace_all = bool(arguments.get("replace_all"))
    lines_raw = arguments.get("lines")
    insert_after = arguments.get("insert_after")
    # [去全量更新 2026-10-28 · 用户令] **一个块只有一条写入通道**：
    #   现状 → edit_status（快照语义）／需求文档 → edit_requirements（需求语义）／使命 → 无（用户专改）
    # 这两个专属工具都是本函数的壳（forced_block 锁死块名），模型不用传 block。
    if forced_block:
        if block != forced_block:
            return err("validation", f"本工具只写「{forced_block}」块，收到：{block or '（空）'}")
    else:
        if block in ("现状", "需求文档"):
            _tool = "edit_status" if block == "现状" else "edit_requirements"
            return err("validation",
                       f"「{block}」块用 `{_tool}`（它带该块专属的语义）；edit_card 只管使命。")
        if block not in _BLOCK_FIELD:
            return err("validation",
                       f"block 只允许 使命（现状用 edit_status、需求文档用 edit_requirements），"
                       f"收到：{block or '（空）'}")
    if new is None and old is None:
        # [去全量更新 2026-10-28] 两个都不给时给一条能自己纠正的话（别让模型猜）
        return err("validation",
                   "必须同时给 old_string 与 new_string（精准编辑；整块写通道已取消）。"
                   "要新增内容：把某段原文替换成「原文＋新内容」；要删：new_string 给空串。")
    if new is None and old is None:
        # [去全量更新 2026-10-28] 两个都不给时给一条能自己纠正的话（别让模型猜）
        return err("validation",
                   "必须同时给 old_string 与 new_string（精准编辑；整块写通道已取消）。"
                   "要新增内容：把某段原文替换成「原文＋新内容」；要删：new_string 给空串。")
    if new is None:
        return err("validation", "new_string 必填；要删掉这一处就给空串")

    by_line = lines_raw not in (None, "") or insert_after not in (None, "")
    if by_line and isinstance(old, str) and old:
        return err("validation",
                   "old_string 与 lines/insert_after 只能给一个："
                   "按内容定位给 old_string，按行定位给 lines（或 insert_after）")
    # [需求文档生命周期 2026-10-28] 空块写入：块还是空的时候没有原文可定位，
    # 允许 old_string="" 直接落内容（否则第一笔草稿永远写不进去，只能靠 insert_after=0）。
    _empty_block_insert = (not by_line and old == "" and isinstance(new, str)
                           and new != "")
    if not by_line and not _empty_block_insert and (
            not isinstance(old, str) or old == ""):
        return err("validation",
                   "定位方式二选一：old_string（要替换的原文）或 lines / insert_after（行号）。"
                   "空块直接写：old_string 给空串。")

    field = _BLOCK_FIELD[block]
    _before_card = await card_store.read_card(work_id, target_id)
    before_body = dict(_before_card.get("body") or {})
    if field_override:
        # [需求文档生命周期 2026-10-28] 草稿列在 meta 通道：
        # 底稿＝草稿；草稿为空时以**正稿**为底（冲突时从现行需求上改）。
        field = field_override
        _meta_before = _before_card.get("meta") or {}
        _draft_txt = str(_meta_before.get(field_override) or "")
        _base_txt = str(before_body.get("requirements") or "")
        # 正稿若还是**骨架占位模板**，不算底稿——否则第一笔草稿要面对模板里几十处
        # 相同字样（实测 old_string="" 命中 49 次），根本写不进去。
        if _is_blank_text(_base_txt):
            _base_txt = ""
        text = _draft_txt or _base_txt
    else:
        text = str(before_body.get(field) or "")

    if _empty_block_insert and not _is_blank_text(text):
        return err("validation",
                   "old_string 给的是空串，但这一块里已经有内容——空串只能用于**空块首次写入**。"
                   "要改已有内容：old_string 填要替换的那段原文（先 read_card 核对）。")


    # [2026-10-27 用户裁定「不止要对应哪一块，还要精准可以操作某一行」]
    # 行模式：lines="12" / "12-15" 替换该区间（new_string 为空＝删除这几行）；
    # insert_after=N 在第 N 行之后插入（N=0 ＝ 插到块首）。
    # 行号以**当前块**为准（1 起算），越界即拒且零改动。
    matched = "line"
    if _empty_block_insert and _is_blank_text(text):
        new_text, at_line, before_snip, mode, matched = new, 1, "（空块，首次写入）", "insert", "exact"
        hits = 0
    elif by_line:
        rows = text.split("\n")
        if insert_after not in (None, ""):
            try:
                after = int(insert_after)
            except (TypeError, ValueError):
                return err("validation", f"insert_after 必须是整数，收到：{insert_after}")
            if after < 0 or after > len(rows):
                return err("validation",
                           f"insert_after 越界：该块共 {len(rows)} 行，"
                           f"insert_after 须在 0～{len(rows)} 之间，收到 {after}。"
                           f"**卡面未改动**。")
            add = str(new).split("\n") if str(new) else []
            rows[after:after] = add
            new_text = "\n".join(rows)
            at_line, before_snip, mode = after + 1, "（插入，无被替换内容）", "insert"
        else:
            spec = str(lines_raw).strip()
            try:
                if "-" in spec:
                    a, b = spec.split("-", 1)
                    start, end = int(a), int(b)
                else:
                    start = end = int(spec)
            except (TypeError, ValueError):
                return err("validation",
                           f"lines 格式只支持 \"12\" 或 \"12-15\"，收到：{spec}")
            if start < 1 or end < start or end > len(rows):
                return err("validation",
                           f"行号越界：该块共 {len(rows)} 行，lines 须在 1～{len(rows)}，"
                           f"且起≤止；收到 {spec}。**卡面未改动**。")
            before_snip = "\n".join(rows[start - 1:end])
            rows[start - 1:end] = str(new).split("\n") if str(new) else []
            new_text = "\n".join(rows)
            at_line, mode = start, "lines"
        if new_text == text:
            return err("validation", "操作后内容没有变化")
        hits, replace_all = 0, False
    else:
        # 先精确匹配；0 命中再退到**归一化匹配**（只用于定位，写回用原文）
        hits = text.count(old)
        matched = "exact"
        n_text = n_old = ""
        if hits == 0:
            n_text, n_old = _norm_for_match(text), _norm_for_match(old)
            hits = n_text.count(n_old) if n_old else 0
            matched = "normalized" if hits else "none"
        if hits == 0:
            # [2026-10-30 实跑] 只回一句"找不到"不够：模型手里是**过期原文**
            # （它自己刚改过），于是拿同一段旧文本反复重试——实测 21 次里 19 次
            # 这么废掉，一轮改稿卡十几分钟。
            # [2026-11-02 · card_021 实案·二次修正] 头 3000 字符在大块上等于
            # 没给——目标在深处时模型只能整块重读（8 万 token 文档重读 12 轮
            # 撞穿窗口）。改回**近似原文窗＋行号**：窗内逐字原文直接修锚点，
            # 或拿行号走 lines= 行定位，都不必重读。
            hint = file_tools.format_not_found_hint(
                text, old,
                fix_hint="或改用 lines=\"起-止\" 按行定位；要回读一段用 "
                         "read_card 的 block+para_start，不必整块重读；")
            return err("not_found",
                       f"「{block}」块里找不到 old_string，**卡面未改动**。{hint}")
        if hits > 1 and not replace_all:
            spots = file_tools.match_positions(text, old)
            listing = "；".join(f"第 {p['line']} 行：{p['head']}" for p in spots)
            return err("ambiguous",
                       f"old_string 在「{block}」块里出现 {hits} 次（{listing}"
                       + (f"等共 {hits} 处" if hits > len(spots) else "") +
                       f"），无法确定改哪一处，**卡面未改动**。"
                       f"请带上更多上下文让它唯一、用 lines= 行范围指定位次，"
                       f"或 replace_all=true。")

        if matched == "exact":
            new_text = (text.replace(old, new) if replace_all
                        else text.replace(old, new, 1))
            at_line = text[:text.find(old)].count("\n") + 1
            before_snip, mode = old, "content"
        else:
            # 归一化命中：在归一化空间找位置，回到原文取**真实片段**再替换。
            spans: list[tuple[int, int]] = []
            cur = 0
            while True:
                i = n_text.find(n_old, cur)
                if i < 0:
                    break
                spans.append((i, i + len(old)))
                cur = i + len(n_old)
            if not replace_all:
                spans = spans[:1]
            chunks: list[str] = []
            prev = 0
            for a, b in spans:
                chunks.append(text[prev:a])
                chunks.append(str(new))
                prev = b
            chunks.append(text[prev:])
            new_text = "".join(chunks)
            at_line = text[:spans[0][0]].count("\n") + 1
            before_snip = text[spans[0][0]:spans[0][1]]
            mode = "content"
        if new_text == text:
            return err("validation", "替换后内容没有变化（old 与 new 相同？）")

    # 同一道闸：使命锁 / 现状块 D 行——精准编辑不是例外通道
    merged = dict(before_body)
    merged["requirements" if field_override else field] = new_text
    verdict = _check_block_rules(_card_body_text(_before_card),
                                 _card_body_text({"body": merged}))
    if not verdict["ok"]:
        logger.info("精准编辑被闸拒: card=%s rule=%s", target_id, verdict["rule"])
        return err("validation", verdict["message"])

    current = await card_store.read_card(work_id, current_id)
    cur_title = current["meta"].get("title", "")
    _how = {"insert": "在第 %d 行后插入" % (at_line - 1),
            "lines": "按行编辑（起于第 %d 行）" % at_line,
            "content": "精准替换一处"}[mode]
    _write = {field: new_text}
    if field_override == "requirements_draft":
        # [需求文档生命周期 2026-10-28] 写草稿 ⇒ 状态到 draft；**只在原状态为空
        # 或本就是 draft 时落**：已在审（in_review）的修订不打断评审，已有正稿
        # （approved）上挂新草稿也不降格——正稿仍然有效。
        _cur_state = str((_before_card.get("meta") or {}).get(
            "requirements_state") or "")
        if _cur_state in ("", "draft"):
            _write["requirements_state"] = "draft"
    result = await card_store.update_card(
        work_id, target_id, _write,
        f"由「{cur_title}」编辑「{block}」块：{_how}",
    )
    if "error" in result:
        return result
    payload = await _landed_payload(work_id, target_id, before_body)
    out = {"card_id": target_id, "ok": True, "block": block, "mode": mode,
           "matched": matched,
           "occurrences": hits, "replaced": hits if replace_all else 1,
           "at_line": at_line,
           "before": before_snip[:400], "after": str(new)[:400], **payload}
    if matched == "normalized":
        # 归一化命中：old_string 与卡面原文**并不相等**（标点编码不同）。
        # 必须说清，否则模型会以为卡面写的就是它抄的那一版，以后引用就错。
        out["note"] = ("按「标点归一化」命中：你给的 old_string 与卡面原文在标点编码上不同，"
                       "上面 before 是**卡面上的真实原文**，写回用的也是它。"
                       "以后引用卡面请以 before / blocks 里的字面为准。")
    # [错放提醒 2026-11-02] 现状/需求文档两块只放「现在成立」的东西——
    # 写入内容像历史/备忘时，回执附一句软提醒（edit_card/edit_status/
    # edit_requirements 全走这一个回执点）。
    _hint = (_history_like_note(str(new or ""))
             if block in ("现状", "需求文档") else "")
    if _hint:
        out["note"] = (str(out.get("note") or "") + "\n" + _hint).strip()
    return out


# ============================================================
# [分块写入 2026-10-19 · permexp] 用户令：工具要便利模型，不要复杂化模型。
#
# 病根：edit_card 收「整篇正文」⇒ 模型只想改现状，也要为使命/需求文档**逐字不变**
# 负责；而新建卡正文里带着模板占位（D01/D02/D03 + 「→ 被D05取代」），模型重写
# 时必然带偏 ⇒ 撞「需求文档块只追加」。实测 MiniMax 连撞 8 次、GLM-5.3 连撞 3 次，
# 两个模型同一处失败 ⇒ 是接口问题，不是模型问题。
#
# 修法：**一个接口一种语义**，让错误不可能发生——
#   update_status  只替换现状块（输入里没有别的块，也就动不了别的块）
#   append_requirements  覆盖写需求文档块（要什么、验收标准是什么）
# 两者只在自动模式面下发（AUTO_ONLY_TOOLS）；普通卡仍走 draft/commit，逐字不变。
# ============================================================

def _guard_edit(work_id: str, current_id: str, target_id: str,
                cards: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """edit 类工具的公共前置：存在 → 非影子 → EDIT 权限。违规返 err 信封。"""
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if cards[target_id].get("card_type") == "shadow":
        return err("denied", "影子卡只能由对应正卡经 update_shadow 修改")
    # [编辑权统一 2026-10-22] 原来走 policy.check（纯子树判定）——离树卡（role/function）
    # parent 恒空，永远不在任何人的子树里 ⇒ 根卡改不了自己组建的职能卡面。
    # 改用 _org_scope_allows（与建卡/换父同源）：树内卡行为逐字不变，根卡对全项目有效。
    if not _org_scope_allows(current_id, target_id, policy.SWITCH_EDIT, cards):
        # 树闸不过 ⇒ 最后查差事：在派差事覆盖目标且实发面含"改别人卡面"
        # 的工具（PM 执笔写对象卡草稿的唯一通道，与 _cap_gate 同一把闸）。
        if not _deployment_scope(work_id, current_id, target_id,
                                 "edit_other_face"):
            return err("denied", f"越权：无权编辑 {target_id}")
    return None


async def _h_set_child_mission(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """上级给子树内的卡设定/修订使命（立项定稿）。

    三条不变量（改这个 handler 时不要破）：
    1. 只能改**子树内**的卡，且目标不是自己——自己的使命由上级或用户定；
    2. 关口与 create_card 同一把（SWITCH_CREATE_CHILD）：能建子卡者才能立使命，
       执行体（worker）没有该开关 ⇒ 既不能改自己也不能改别人；
    3. 每次写入都留痕（history_note），使命可追溯——它不是随手可改的备注。
    """
    target_id = str(arguments.get("card_id", "") or "").strip()
    mission = str(arguments.get("mission", "") or "").strip()
    if not target_id:
        return err("validation", "card_id 不能为空")
    if not mission:
        return err("validation", "mission 不能为空（要换使命请写明新的使命全文）")
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if cards[target_id].get("card_type") == "shadow":
        return err("denied", "影子卡没有使命块，不能设")
    if target_id == current_id:
        return err("denied", "不能给自己设使命：本卡使命由上级或用户设定")
    # [能力底座 2026-10-30] 能力「立子卡使命」＋ 作用域
    _bad = _cap_gate(current_id, "set_child_mission", policy.SWITCH_CREATE_CHILD,
                     target_id, cards, "给它立使命", work_id=work_id)
    if _bad is not None:
        return _bad
    result = await card_store.update_card(
        work_id, target_id, {"mission": mission},
        f"{current_id} 设定其使命（父卡立使命）",
    )
    if "error" in result:
        return result
    return {"ok": True, "card_id": target_id, "mission_chars": len(mission)}



# [2026-10-30 删] _h_update_status 已删：整块覆盖现状的通道 10-27 已退役，且它没有开关（执行层必拒、常规面不可见），只剩职能面上一件调不动的工具。


async def _h_append_ruling(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[2026-10-22 用户裁定] 写需求文档块。**两种模式，按需选，都不强制整块重写**：

    ① 精准编辑（推荐）：给 `old_string` + `new_string`——只替换那一处，
       `old_string` 必须在块里**恰好出现一次**，否则报错且不动块（与 edit_file 同纪律）。
       小改小修用这个，不必把整块重抄一遍（整块重抄最容易抄错）。
    ② 整块写：只给 `text`——整块替换。**骨架占位不算既有内容**，直接覆盖即可。

    旧的「追加一条 D{nn}、旧条目改不了」语义已废除。
    """
    target_id = str(arguments.get("card_id") or current_id)
    old_string = arguments.get("old_string")
    new_string = arguments.get("new_string")
    text = str(arguments.get("text") or "").strip()

    has_edit = old_string is not None or new_string is not None
    if has_edit:
        if old_string is None or new_string is None:
            return err("validation", "精准编辑必须同时给 old_string 与 new_string")
        if not str(old_string).strip():
            return err("validation", "old_string 不能为空")
        mode = "edit"
    elif text:
        mode = "rewrite"
    else:
        return err("validation",
                   "要么给 text（整块写），要么给 old_string＋new_string（精准编辑）")

    blocked = _guard_edit(work_id, current_id, target_id, cards)
    if blocked is not None:
        return blocked
    card = await card_store.read_card(work_id, target_id)
    if not card or not card.get("body"):
        return err("not_found", f"卡片不存在: {target_id}")
    rulings = str(card["body"].get("rulings") or "")

    if mode == "edit":
        n = rulings.count(str(old_string))
        if n == 0:
            return err("validation",
                       "old_string 在需求文档块里找不到，未作任何修改。当前块前 200 字："
                       + rulings[:200])
        if n > 1:
            return err("validation",
                       f"old_string 出现了 {n} 次，必须唯一才能改；请把前后文带上以缩小范围")
        new_rulings = rulings.replace(str(old_string), str(new_string), 1)
    else:
        # 整块写·单一最新（骨架占位不算既有内容，直接覆盖）。
        new_rulings = text

    current = await card_store.read_card(work_id, current_id)
    cur_title = (current.get("meta") or {}).get("title", "")
    upd = await card_store.update_card(
        work_id, target_id, {"rulings": new_rulings},
        f"由「{cur_title}」" + ("精准编辑" if mode == "edit" else "整块写") + "需求文档",
    )
    if isinstance(upd, dict) and upd.get("error"):
        return err("internal", str(upd["error"]))
    return {"card_id": target_id, "mode": mode, "bytes": len(new_rulings)}


async def _h_delete_card(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [模型删卡 2026-10-02] 用户在系统内授权（"你现在能删卡了，再试试"）。
    # 权限＝写权限（管辖域内）；结构性护栏由存储层 delete_card 机械执行
    # （剧本根卡拒删、有子卡拒删），本层不另设判断。
    target_id = arguments.get("card_id", "")
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    # [权限收口 2026-10-09] 身份护栏前移一行（原在权限判据之后）：根卡拒删的
    # 拒绝文案逐字不变——配置里根行无 delete 位，护栏后置会被通用越权文案盖掉
    if target_id == work_store.work_card_id_of(work_id):
        return err("denied", f"剧本根卡不可删除: {target_id}")
    # [能力底座 2026-10-30] 能力「删卡与归档」＋ 作用域
    _bad = _cap_gate(current_id, "dispose_card", policy.SWITCH_DELETE,
                     target_id, cards, "删除", work_id=work_id)
    if _bad is not None:
        return _bad
    try:
        result = await card_store.delete_card(work_id, target_id)
    except card_store.CardDeleteDeniedError as exc:
        # 结构性护栏（根卡/带子卡）＝拒绝，不是内部故障
        return err("denied", str(exc))
    if "error" in result:
        return result
    # cards 元数据缓存同步，防止同轮后续工具仍看到已删卡
    cards.pop(target_id, None)
    return result


async def _h_archive_card(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[2026-10-25 · 用户令「让所有卡都可以归档」] **不加任何前置判断**。

    不看卡类型、不看 state、不看有没有子卡——归档是"收起来"，不是"删除"，
    也不是一种状态。判断该不该归档归用户/模型，工具只执行。
    """
    target = str(arguments.get("card_id") or "").strip()
    reason = str(arguments.get("reason") or "").strip()
    if not target:
        return err("validation", "card_id 不能为空")
    if target not in cards and not card_store.card_exists(work_id, target):
        return err("not_found", f"卡片不存在: {target}")
    # [归档权限 2026-10-25] **不查目标卡自己的 perms**——归档是"上级处置下级"，
    # 判据只有一条：操作者在目标子树上游（根卡管全树），与 silence_card/stop_card 同款。
    # 原用 policy.check(..., SWITCH_DELETE) 会去查**被归档卡**有没有 delete 位，
    # 而 worker 卡的 perms 里没有 delete ⇒ 父卡永远归档不了自己的子卡（实测 card_021→card_023）。
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id) or ""
    except Exception:  # noqa: BLE001
        root_id = ""
    if current_id != root_id:
        try:
            if not policy._is_in_subtree(current_id, target, cards):
                return err("denied",
                           f"越权：{target} 不在你的子树内，不能归档（总裁可管全树）")
        except Exception as exc:  # noqa: BLE001 判定失败一律拒
            return err("denied", f"子树判定失败，已拒绝: {exc}")
    try:
        r = await card_store.archive_card(work_id, target, reason=reason,
                                          by=current_id)
    except card_store.CardNotFoundError as exc:
        return err("not_found", str(exc))
    if isinstance(r, dict) and r.get("error"):
        return err("internal", str(r["error"]))
    cards.pop(target, None)   # 同轮后续工具不应再看到这张卡
    return r


async def _h_unarchive_card(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把归档记录恢复成活卡（三块与修改历史写回；会话文件一直在）。"""
    target = str(arguments.get("card_id") or "").strip()
    if not target:
        return err("validation", "card_id 不能为空")
    # 目标卡已不在树上（归档后 cards 里没有它）⇒ 树类判据无从算起。
    # 恢复是"把卡放回去"，工具面本身已按 perms 过滤（能调到即有资格），不再另设判据。
    r = await card_store.unarchive_card(work_id, target)
    if isinstance(r, dict) and r.get("error"):
        code = str(r.get("code") or "")
        return err("not_found" if code == "not_archived" else "validation",
                   str(r["error"]))
    return r


async def _h_update_shadow(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [影子卡 2026-10-03] schema 无 card_id——写入目标恒为当前卡自己的影子
    # （R-09 写侧：不是可绕过的校验规则，是工具签名本身写不出别的目标）
    # [T5 补漏检口 2026-10-09][权限底座重做 2026-10-12] 关口判定：当前卡分类
    # 须授 shadow_write（配置里注册类型全授＝照写；未配置类型拒）。
    # 拒绝转既有 denied 信封。
    # [能力底座 2026-10-30] 能力「写过程记录」（自检，无作用域）
    _bad = _cap_gate(current_id, "write_process", policy.SWITCH_SHADOW_WRITE,
                     current_id, cards, "写影子", work_id=work_id)
    if _bad is not None:
        return _bad
    content = arguments.get("content", "")
    return await shadow_store.update_shadow(work_id, current_id, content)


async def _h_read_shadow(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [影子卡 2026-10-03] 读侧走统一关口 READ 判定（R-09 读侧：不另设判据）；
    # [卡片区域语义 v3] 影子＝本卡私有，只读本卡自己的影子。
    target_id = arguments.get("card_id", "") or current_id
    if target_id.startswith("shadow_"):
        target_id = target_id[len("shadow_"):]
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    # [关系治理 2026-10-30] 同上：按线判；影子是过程记录，关系线不给。
    _line = _line_between(work_id, current_id, target_id, cards)
    if not _line:
        return err("denied",
                   f"越权：{target_id} 与你之间没有父子线也没有关系线，读不到。")
    if _line == "relation":
        return err("denied",
                   f"{target_id} 与你是关系线：只读对外信息（需求文档），影子不给。")
    # [笔记泄漏修复 2026-11-01] 笔记是本卡私有：别的卡沿父子线读影子同样要剥掉，
    # 与 read_via（影子分支 include_private=False）同口径。改前这里恒取全文，
    # 父卡经 read_shadow 能读到子卡的「## 笔记」。
    raw = await shadow_store.read_shadow_raw(
        work_id, target_id, include_private=(target_id == current_id))
    if raw is None:
        return err("not_found", f"影子卡不存在: {target_id}")
    meta, _ = parse_front_matter(raw)
    return {
        "owner_card_id": target_id,
        "updated_at": str(meta.get("updated_at", "")),
        "content": raw,
    }


# ============================================================
# [卡片底座第一步 · 沿关系的数据访问与通信 2026-10-14]
#
# 底座补上「沿关系」这一个机制的两个方向：读（read_via）与写（卡间通信）。
# 本段＝读方向的**纯机制**：只做「沿边寻址 ＋ 读」，不含任何资格判断
# （权限判断在 handler 层，与现状 read_card 分层一致——底座给能力，资格归权限管）。
# ============================================================

#: read_via 的 part 取值（**开放取值**——只定机制不定形态；新增取值＝在此登记
#: 一句映射，不重做读工具）。
READ_VIA_PARTS: tuple[str, ...] = ("正文", "影子", "对话")

#: 对话读取窗口参数（2026-10-25 · 用户报「read_linked 把所有历史都传了」后定）：
#: 病根＝read_linked(对话) 直接返全量 timeline，两条卡的历史（各自含对方全史，
#: 嵌套膨胀）合计 287 万字符 ≈190 万 token，顶爆所有模型上下文，第二轮回写死。
#: 语义修正：沿通信线读对话＝**了解对方现在到哪了**，不是搬对方一生 ⇒
#: 尾部窗口 + 单条截断 + 总量预算；工具结果只留摘要（它是嵌套膨胀的载体）。
_CONV_TAIL_EVENTS = 80          # 尾部事件窗口
_CONV_TEXT_MAX = 1200           # 文本类事件单条上限（字符）
_CONV_TOOL_PREVIEW = 240        # 工具结果单条摘要上限（字符）
_CONV_TOTAL_MAX = 60_000        # 整次读取总量上限（字符）
#: 工具结果注入模型的字节预算（message_runner 单一投影点使用；事件留痕不受限）。
TOOL_RESULT_INJECT_BUDGET = 200_000


def bound_text(text: str, max_bytes: int = TOOL_RESULT_INJECT_BUDGET) -> str:
    """按 UTF-8 字节截断并附**明示标记**（不静默丢数据：模型必须知道不完整）。"""
    raw = str(text or "")
    data = raw.encode("utf-8")
    if len(data) <= max_bytes:
        return raw
    head = data[:max_bytes].decode("utf-8", "ignore")
    return (
        f"{head}\n…（工具结果已截断：原文 {len(data)} 字节，超过单次注入上限 "
        f"{max_bytes} 字节。这是**不完整数据**，不得当作全量依据；需要完整内容"
        f"请缩小范围后分次读取。）"
    )


def _clip(text: Any, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + f"…（共 {len(s)} 字，已截断）"


def _conversation_digest(work_id: str, card_id: str) -> dict[str, Any]:
    """对话的**可读窗口**（供 read_via part=对话 用）。

    只取尾部 ``_CONV_TAIL_EVENTS`` 条，按事件类型压成一行人话：
    发言/交付/结论全文（上限 _CONV_TEXT_MAX）、工具调用只留名与参数、
    工具结果只留 ok 与摘要（上限 _CONV_TOOL_PREVIEW）——工具结果是嵌套膨胀的
    载体（实测单条 109 万字），**永不回灌正文**。控制类事件（round_start/
    context/reasoning 等）不进摘要：读对话是为了了解进展，不是复读审计日志。
    总量超 ``_CONV_TOTAL_MAX`` 时从最旧的开始丢弃并明示。
    """
    events = event_log.timeline(work_id, card_id, tail=_CONV_TAIL_EVENTS)
    lines: list[dict[str, Any]] = []
    for e in events:
        t = str(e.get("type") or "")
        d = e.get("data") or {}
        seq = e.get("seq")
        text = ""
        if t == "user_message":
            text = "用户：" + _clip(d.get("content"), _CONV_TEXT_MAX)
        elif t == "assistant_text":
            text = "助手：" + _clip(d.get("content"), _CONV_TEXT_MAX)
        elif t in ("deliver", "proposal", "done"):
            text = f"{t}：" + _clip(d.get("content") or d.get("summary") or d, _CONV_TEXT_MAX)
        elif t == "error":
            text = "错误：" + _clip(d.get("error"), _CONV_TEXT_MAX)
        elif t == "tool_call":
            text = (f"工具调用 {d.get('tool')}（{d.get('target') or ''}）"
                    + _clip(d.get("arguments"), _CONV_TOOL_PREVIEW))
        elif t == "tool_result":
            payload = json.dumps(d.get("result"), ensure_ascii=False)
            text = (f"工具结果 {d.get('tool')}（{d.get('target') or ''}）"
                    f"ok={d.get('ok')}：" + _clip(payload, _CONV_TOOL_PREVIEW))
        else:
            continue  # reasoning/context/round_start/turn_end… 属过程噪声，不进摘要
        lines.append({"seq": seq, "type": t, "text": text})

    dropped = 0
    total = sum(len(x["text"]) for x in lines)
    while lines and total > _CONV_TOTAL_MAX:
        total -= len(lines[0]["text"])
        lines.pop(0)
        dropped += 1
    first_seq = events[0].get("seq") if events else None
    truncated = bool(dropped) or (len(events) >= _CONV_TAIL_EVENTS and (first_seq or 0) > 1)
    return {
        "events": lines,
        "window": f"最近 {len(events)} 条事件" + (f"，因超量丢弃最早 {dropped} 条" if dropped else ""),
        "truncated": truncated,
        "note": "仅为尾部窗口摘要（工具结果只给摘要不给正文）。需要更早内容：read 不支持分页时，"
                "改为让对方把结论写进正文/影子再读。",
    }


async def read_via(work_id: str, source_id: str, target_id: str,
                   part: str) -> dict[str, Any]:
    """沿**通信线**读对方卡的数据（纯机制）。
    ① **沿边寻址**：``source_id`` 的 ``links``（通信线）里必须存在一条指向
       ``target_id`` 的边；不存在 → ``{"ok": False, "reason": "no_link", ...}``
       （文案含「无通信线」）。只沿 links（通信线），**不碰 parent**，
       **也绝不沿新线 relations（依赖关联线）**——新线不参与通信
       （依赖关联线 2026-10-16 裁定 3）。
    ② **part → 现成读工具**（复用不重做，只加「沿边寻址」这一步）：
       - ``正文`` → ``card_store.read_card``（三块：使命／现状／需求文档）
       - ``影子`` → ``shadow_store.read_shadow_raw``
       - ``对话`` → ``event_log.timeline``
    ③ 本函数**不做任何权限判断**——资格归权限管（调用方 handler 走
       ``policy.SWITCH_READ_LINKED`` 关口）。

    返回 ``{"ok": True, "part", "card_id", "value"}`` 或
    ``{"ok": False, "reason", "message"}``（不抛异常）。
    """
    try:
        source = await card_store.read_card(work_id, source_id)
    except Exception as exc:  # noqa: BLE001 读源卡失败＝无路可寻，如实返回
        return {"ok": False, "reason": "source_unreadable",
                "message": f"读源卡失败: {exc}"}
    # [依赖关联线 2026-10-16] 沿边寻址**只认 links（通信线）**——新线 relations
    # （依赖关联线）不进寻址，故沿依赖线 read_linked 一律报「无通信线」。
    links = (source.get("meta") or {}).get("links") or []
    linked = any(
        isinstance(r, dict) and str(r.get("target") or "") == target_id
        for r in links
    )
    if not linked:
        return {"ok": False, "reason": "no_link",
                "message": f"无通信线：{source_id} 与 {target_id} 之间没有连线"}

    if part == "正文":
        result = await card_store.read_card(work_id, target_id)
        meta = result.get("meta") or {}
        body = result.get("body") or {}
        return {"ok": True, "part": part, "card_id": target_id, "value": {
            "title": meta.get("title", ""),
            "mission": body.get("mission", ""),
            "status_block": body.get("status_block", ""),
            # [2026-11-01 修] 库内第三块恒为 requirements 列（rulings 是
            # 改名前的化石键，恒读回 ""——通信线读不到对方的需求文档）。
            "requirements": body.get("requirements", ""),
            # [relations_in 2026-11-01] 卡面关系位置随正文一起给：
            # 出边（它依赖谁）＋ 入边（谁依赖它）。
            "relations": meta.get("relations") or [],
            "relations_in": meta.get("relations_in") or [],
        }}
    if part == "影子":
        # [笔记 2026-10-27] 笔记是**本卡私有**：沿通信线读别人的影子必须剥掉
        # 「## 笔记」那一节（用户原话「其他卡片不能读取」）。
        raw = await shadow_store.read_shadow_raw(
            work_id, target_id, include_private=(source_id == target_id))
        if raw is None:
            return {"ok": False, "reason": "not_found",
                    "message": f"影子卡不存在: {target_id}"}
        return {"ok": True, "part": part, "card_id": target_id, "value": raw}
    if part == "对话":
        # [2026-10-25] 不再返全量 timeline（实测两条卡全史合计 287 万字符顶爆上下文）；
        # 改返尾部窗口摘要（见 _conversation_digest）。
        return {"ok": True, "part": part, "card_id": target_id,
                "value": _conversation_digest(work_id, target_id)}
    return {"ok": False, "reason": "unknown_part",
            "message": f"未知 part: {part!r}（合法值：{list(READ_VIA_PARTS)}）"}


async def _h_read_linked(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """沿通信线读对方卡（模型工具 read_linked 的执行体）。

    权限＝只判**发起方**有 ``SWITCH_READ_LINKED``（与 read 分开授权——沿连线读比
    直接读更敏感）。[通信不看对方 2026-10-22 · 用户令] 原来走 ``policy.check``，
    会连**目标卡**有没有 read_linked 一起查——那等于「对方同意被读」，而 perms 是
    建卡时抄的静态值、没有任何同意动作（worker/tool/rule 因此读不了）。线本身就
    是「谁连了谁」的凭据，不需要第二道许可。
    源＝当前卡（工具签名里没有 source，写不出别的源）。
    越权回既有 denied 信封；无连线／目标不存在回 not_found（文案含「无通信线」）。
    """
    target_id = str(arguments.get("target_id", "") or "")
    part = str(arguments.get("part", "") or "")
    if not target_id:
        return err("validation", "target_id 不能为空")
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    bad = _require_wire_switch(current_id, policy.SWITCH_READ_LINKED, cards, "沿线读取")
    if bad is not None:
        return bad
    result = await read_via(work_id, current_id, target_id, part)
    if not result.get("ok"):
        code = "not_found" if result.get("reason") in ("no_link", "not_found") \
            else "validation"
        return err(code, str(result.get("message") or "读取失败"))
    return {"card_id": target_id, "part": part, "value": result["value"]}


async def _h_list_manuscript(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [工具面补齐 2026-10-12] manuscript 开关的执行层关口（原先只有工具面会挡，
    # 直接调 handler 是裸奔）。非树类开关 ⇒ 只判当前卡自己的类型分权，不叠子树。
    if not _actor_can_switch(cards.get(current_id), policy.SWITCH_MANUSCRIPT):
        return err("denied", "无权查阅本项目原始资料（manuscript 开关未授权）")
    chapters = manuscript_store.list_chapters(work_id)
    if not chapters:
        return {"chapters": [], "note": "本项目尚无原文"}
    return {"chapters": chapters}


async def _h_read_manuscript(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not _actor_can_switch(cards.get(current_id), policy.SWITCH_MANUSCRIPT):
        return err("denied", "无权查阅本项目原始资料（manuscript 开关未授权）")
    if not manuscript_store.list_chapters(work_id):
        return err("not_found", "本项目尚无原文")
    return manuscript_store.read_chapter(
        work_id,
        arguments.get("chapter", ""),
        para_start=arguments.get("para_start"),
        para_end=arguments.get("para_end"),
    )


def _is_offtree_role(current_id: str,
                     cards: dict[str, dict[str, Any]]) -> bool:
    """发起方是不是**离树的角色卡**。

    离树 = parent 为空串；role = card_type 为 "role"。
    这类卡没有子树，任何"在自己子树内"的判定对它都恒假——
    但用户要求它能组织整棵树（见 _org_scope_allows）。
    """
    me = cards.get(current_id) or {}
    return (str(me.get("parent") or "") == ""
            and str(me.get("card_type") or "") == "role")


def _root_id_of(cards: dict[str, dict[str, Any]]) -> str:
    """本 work 的项目根卡 id（card_type=project 的那一张）。找不到返回空串。"""
    for cid, m in (cards or {}).items():
        if str((m or {}).get("card_type") or "") == "project":
            return str(cid)
    return ""


def _org_scope_allows(current_id: str, target_id: str, switch: str,
                      cards: dict[str, dict[str, Any]]) -> bool:
    """组织权作用域判定：发起方能否对 target 施加 switch（link/unlink/edit/create_child）。

    **在树内的卡**：走 ``policy.check``（子树判定），与改造前逐字一致。
    **离树的角色卡**：作用域＝本项目整棵树，但有两条例外——
      · 不作用于项目根卡（根卡是项目身份，挂走它等于换项目）
      · 不作用于自己（对自身的操作走别的关口）

    为什么这样分：用户裁定「角色卡可以在整个树内任意位置挂载它觉得需要的卡片」。
    角色卡不在树里，用"在自己子树内"去卡它，等于让它什么都不能做。
    """
    # [根卡管全项目 2026-10-22 · 用户令「为什么根卡不能编辑角色卡的卡面？」]
    # 根卡是项目本体，本项目里任何卡（含离树的 role / function）都归它管——
    # 原来只走 policy.check（子树判定），而离树卡 parent 恒空、永远不在任何人的子树里
    # ⇒ 根卡改不了自己组建的职能卡面（update_status(fn_001) 直接 denied）。
    # 权限面不变：仍要求**根卡自身**持有该开关（has_perm），只是作用域放到全项目。
    if current_id and current_id == _root_id_of(cards) and target_id != current_id:
        return bool(target_id) and target_id in cards and _actor_can_switch(
            cards.get(current_id), switch)
    if _is_offtree_role(current_id, cards):
        if not target_id or target_id not in cards:
            return False
        # 写自己：照旧放行（自己算自己作用域内——原 policy.check 同口径）
        if target_id == current_id:
            return _actor_can_switch(cards.get(current_id), switch)
        if target_id == _root_id_of(cards):
            return False          # 不许动根卡
        # [角色不改别人卡面 2026-10-22] 编辑权对离树卡**一律拒**：角色/职能的职责是
        # 观察与判断，它写自己的现状走自己的口，不该改别人的卡面（原 policy.check
        # 靠子树判定天然拒；改用组织权后作用域放到全树，必须显式挡回来）。
        if switch == policy.SWITCH_EDIT:
            return False
        # 离树卡自身仍须挂了这项能力（权限面不因作用域扩大而变大）
        return _actor_can_switch(cards.get(current_id), switch)
    if not _actor_can_switch(cards.get(current_id), switch):
        return False
    return policy.can(current_id, target_id, switch, cards)


def _require_wire_switch(current_id: str, switch: str,
                         cards: dict[str, dict[str, Any]],
                         what: str) -> dict[str, Any] | None:
    """建线／解线的门槛：**只看发起方**，不看目标卡。通过返回 None。

    [连线不看对方 2026-10-22 · 用户令] 原来走 ``policy.check(..., SCOPE_ANY)``，
    而 ``check`` 的第一级是「**目标卡** perms 须授 link」——理由是「线是双向的，
    被连方也要能回话」。这个「同意」是假的：
      · 目标卡的 perms 是建卡时从类型模板抄下来的静态值，没有任何「同意」动作，
        用户也没有任何地方能表示同意／拒绝；
      · 实际效果只是「role / worker / tool / rule 四类永远连不上」（它们没有 link）。
    线的记录写在**发起方**卡上（``links`` / ``relations`` 字段），被连方不需要同意；
    真正需要双方的是**发信**，那一关由 ``check_linked``（两卡之间已有线）把着，
    与 perms 无关。
    ⇒ 门槛只判发起方有没有这个开关。打点照旧（fn="check" 同口径）。
    """
    # [能力底座 2026-10-30] 判据改走能力（与工具面/执行层同源）；未挂载的卡按它
    # 声明的开关反推（见 _abilities_of 的兼容层）。
    ok = _actor_can_switch(cards.get(current_id), switch)
    prompt_log.append_perm_log({
        "fn": "check",
        "actor_id": current_id,
        "target_id": current_id,
        "switch": switch,
        "granted": ok,
        "note": "wire_self_only",     # 与「查目标卡」的旧记录区分开
    })
    if ok:
        return None
    return err("denied",
               f"越权：本卡没有{what}权限（perms 未授 {switch}）")


async def _h_link_cards(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target_id = arguments.get("target_id", "")
    rel_type = arguments.get("type", "")
    note = (arguments.get("note") or "").strip()
    if rel_type not in MODEL_REL_TYPES:
        return err("validation", f"通信线类型非法: {rel_type}，只允许 先后/关联")
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if target_id == current_id:
        return err("validation", "不能与自身建立关系")
    # [5713 通道试走] 建线**不叠 parent 树作用域**：通信的边界是线本身，不是父子关系。
    # [连线不看对方 2026-10-22] 也**不看目标卡**的 perms——「对方同意」是伪命题
    # （静态开关、无处可同意），只判发起方有没有 link。见 _require_wire_switch。
    bad = _require_wire_switch(current_id, policy.SWITCH_LINK, cards, "建线")
    if bad is not None:
        return bad
    current = await card_store.read_card(work_id, current_id)
    # [依赖关联线 2026-10-16] 通信线读写走 **links**（原 relations 改名）。
    existing = current["meta"].get("links") or []
    for rel in existing:
        if (isinstance(rel, dict)
                and rel.get("target") == target_id
                and rel.get("type") == rel_type):
            # 幂等：同 target+type 已存在，不重复追加
            return {"ok": True, "existed": True}
    new_rel: dict[str, Any] = {"target": target_id, "type": rel_type}
    if note:
        new_rel["note"] = note
    cur_title = current["meta"].get("title", "")
    result = await card_store.update_card(
        work_id, current_id,
        {"links": list(existing) + [new_rel]},
        f"由「{cur_title}」对话建立关系：{rel_type} → {target_id}",
    )
    if "error" in result:
        return result
    # [5713 通道试走] 同轮 meta 缓存同步（与 _h_create_card 同手法）：模型可以在
    # **同一轮**里先 link_cards 再 send_card_message——不做这一步，发信读到的还是
    # 本回合开始时的旧卡面（没有新线），会被「无线即拒」错杀（g2 用例实测踩到）。
    cards[current_id] = {**(result.get("meta") or {}), "card_id": current_id}
    return {"ok": True, "source": current_id, "target": target_id, "type": rel_type}


async def _h_unlink_cards(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [解线 2026-10-03] 与 link_cards 对称；走 update_card(links)，
    # 与前端 ✕ 删线同一条存储通道。悬空线主用例：目标卡已删时
    # links 条目仍留在 source 卡上，此处不检查目标卡存在性。
    # [依赖关联线 2026-10-16] 通信线读写走 **links**（原 relations 改名）。
    target_id = arguments.get("target_id", "")
    rel_type = (arguments.get("type") or "").strip()
    if not target_id:
        return err("validation", "target_id 不能为空")
    if rel_type and rel_type not in MODEL_REL_TYPES:
        return err("validation", f"关系类型非法: {rel_type}，只允许 先后/关联")
    # [连线不看对方 2026-10-22] 解线同理：只判发起方有 unlink，不看目标卡。
    # 悬空线（目标卡已删）本来就是清理用途，不查存在性。
    if target_id in cards:
        bad = _require_wire_switch(current_id, policy.SWITCH_UNLINK, cards, "解线")
        if bad is not None:
            return bad
    current = await card_store.read_card(work_id, current_id)
    existing = current["meta"].get("links") or []

    def _match(rel: Any) -> bool:
        if not isinstance(rel, dict):
            return False
        if rel.get("target") != target_id:
            return False
        return (not rel_type) or rel.get("type") == rel_type

    remaining = [r for r in existing if not _match(r)]
    removed = len(existing) - len(remaining)
    if removed == 0:
        detail = f" 的 {rel_type} 关系" if rel_type else ""
        return err("not_found", f"关系不存在: 当前卡无指向 {target_id}{detail}")
    cur_title = current["meta"].get("title", "")
    result = await card_store.update_card(
        work_id, current_id,
        {"links": remaining},
        f"由「{cur_title}」对话解除关系 → {target_id}",
    )
    if "error" in result:
        return result
    # [5713 通道试走] 同轮 meta 缓存同步（与 _h_create_card 同手法）：模型可以在
    # **同一轮**里先 link_cards 再 send_card_message——不做这一步，发信读到的还是
    # 本回合开始时的旧卡面（没有新线），会被「无线即拒」错杀（g2 用例实测踩到）。
    cards[current_id] = {**(result.get("meta") or {}), "card_id": current_id}
    return {"ok": True, "source": current_id, "target": target_id, "removed": removed}


# ============================================================
# [换父 2026-10-20] 结构边可变：把自己子树内的卡挂到另一张卡下
# ============================================================
# 与 link_cards 的区别：那条改的是**通信线**（卡间发信沿它寻址）；本工具改的是
# **父子边**——树的形状、权限作用域（写侧限子树）、画布层级全由它决定。
#
# 用户 2026-10-20 裁定的边界是「只在自己子树内调整」：
#   · 不给自己换父（自己的归属归上级管）；
#   · 不把卡挪出子树（那是「交出去」，会让写侧子树语义松一档）。
# 人与模型共用同一道存储校验（card_store.update_card 的 parent 分支）——
# 画布拖拽走 PATCH、模型走这里，判据只有一份。
async def _h_reparent_card(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把 ``card_id`` 挂到 ``new_parent_id`` 下；被移动卡的整棵子树随之迁移。

    判据（全部机械、逐条拒绝并说明原因）：
      1. 被移动卡 ∈ 本卡**严格**子树（不能改自己的归属）；
      2. 新父卡 ∈ 本卡子树（**含本卡自身**——子卡可以直接挂到自己名下）；
      3. 新父卡不是被移动卡自身或其后代（防环；存储层再判一次，双保险）；
      4. 新父卡类型须允许有子卡（工具卡/角色卡/worker 卡/规则卡不可）——由存储层读
         注册表 can_have_children 判，本函数不重复硬编码；
      5. 已是该父卡 ⇒ 幂等返回，不重复写（不留无谓历史）。
    权限：关口 ``EDIT``（树类开关 ⇒ 被移动卡必须在自己子树内），与 edit_card 同一
    开关；新父卡的子树归属另判一次（第 2 条）。
    """
    card_id = str(arguments.get("card_id") or "").strip()
    new_parent_id = str(arguments.get("new_parent_id") or "").strip()
    if not card_id or not new_parent_id:
        return err("validation", "card_id 与 new_parent_id 都必填")
    if card_id == current_id:
        return err("denied", "不能改自己的归属——只能调整自己子树内的卡")
    if card_id not in cards:
        return err("not_found", f"卡片不存在: {card_id}")
    if new_parent_id not in cards:
        return err("not_found", f"卡片不存在: {new_parent_id}")
    # [离开树的角色卡 2026-10-22 · 用户裁定] 作用域由发起方位置决定：
    #   树内卡 → 自己子树；离树角色卡 → 本项目整棵树（不含根卡）
    if not _org_scope_allows(current_id, card_id, policy.SWITCH_EDIT, cards):
        return err("denied",
                   f"越权：{card_id} 不在你的作用域内，不能移动它"
                   f"（当前卡 {current_id}）")
    if new_parent_id != current_id and not _org_scope_allows(
            current_id, new_parent_id, policy.SWITCH_EDIT, cards):
        return err("denied",
                   f"越权：{new_parent_id} 不在你的作用域内，不能把卡挂过去"
                   f"（当前卡 {current_id}）")
    # 判据 3：防环
    if new_parent_id == card_id or policy._is_in_subtree(card_id, new_parent_id, cards):
        return err("validation", "不能挂到自身或自己的后代下（会成环）")
    # 判据 5：幂等（已是该父卡 ⇒ 不写）
    if str((cards.get(card_id) or {}).get("parent") or "") == new_parent_id:
        return {"ok": True, "existed": True,
                "card_id": card_id, "parent": new_parent_id}
    # 迁移规模在**改父之前**数——改完再数，子树已按新边算，数出来的是别人的子树
    moved_subtree = sum(
        1 for cid in cards
        if cid == card_id or policy._is_in_subtree(card_id, cid, cards))
    moved_title = str((cards.get(card_id) or {}).get("title") or "")
    parent_title = str((cards.get(new_parent_id) or {}).get("title") or "")
    actor_title = str((cards.get(current_id) or {}).get("title") or "")
    result = await card_store.update_card(
        work_id, card_id,
        {"parent": new_parent_id},
        f"由「{actor_title}」调整归属：「{moved_title}」挂到「{parent_title}」下"
        f"（连子树 {moved_subtree} 张）",
    )
    if "error" in result:
        return err("validation", str(result["error"]))
    # 同轮 meta 缓存同步（与 _h_link_cards 同手法）：同一轮里后续 read_card /
    # 再调本工具时读到的必须是新父卡，否则会按旧树判越权。
    cards[card_id] = {**(result.get("meta") or {}), "card_id": card_id}
    return {"ok": True, "card_id": card_id, "parent": new_parent_id,
            "moved_subtree": moved_subtree}


# ============================================================
# [依赖关联线 2026-10-16] 第三种线「依赖关联线」的建线/解线工具
#
# 与 link_cards / unlink_cards **对称但不混用**：写的是卡上**另一个字段**——
#   links     ＝ 通信线（link_cards / unlink_cards 写，read_linked 沿它寻址）
#   relations ＝ 依赖关联线（本对工具写，**不参与通信**）
# 权限复用现有 link / unlink 开关（不新开），树类收紧「只能连自己子树内的卡」同款。
# ============================================================

async def _h_link_rel(work_id: str, current_id: str,
                      arguments: dict[str, Any],
                      cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """建一条依赖关联线：在当前卡 ``relations`` 上追加 ``{target, kind, detail}``。

    校验：target 存在、≠自身、kind ∈ ``MODEL_DEP_KINDS``、detail 非空（必填）。
    权限：只判**发起方**有 ``SWITCH_LINK_REL``——不看目标卡、不叠树作用域
    （[连线不看对方 2026-10-22]；[三线拆开关 2026-10-27] 依赖线是独立开关，
    不复用 ``SWITCH_LINK``——工具面按 ``TOOL_SWITCH["link_rel"]`` 下发，
    执行层必须同一把闸，否则"看得见用不动/用得动看不见"）。
    幂等：同 ``(target, kind)`` 已存在则不重复追加——detail 变化则**更新** detail。
    """
    target_id = str(arguments.get("target_id", "") or "")
    kind = str(arguments.get("kind", "") or "")
    detail = str(arguments.get("detail", "") or "").strip()
    if not target_id:
        return err("validation", "target_id 不能为空")
    if kind not in MODEL_DEP_KINDS:
        return err("validation",
                   f"kind 非法: {kind}，只允许 {'/'.join(MODEL_DEP_KINDS)}")
    if not detail:
        return err("validation", "detail 不能为空（写清依赖什么/关联什么）")
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if target_id == current_id:
        return err("validation", "不能与自身建立关系")
    # 权限只判发起方（[连线不看对方 2026-10-22]）：不看目标卡 perms、不叠树作用域。
    bad = _require_wire_switch(current_id, policy.SWITCH_LINK_REL, cards, "建依赖线")
    if bad is not None:
        return bad
    current = await card_store.read_card(work_id, current_id)
    existing = list(current["meta"].get("relations") or [])
    hit = next((r for r in existing
                if isinstance(r, dict) and r.get("target") == target_id
                and r.get("kind") == kind), None)
    cur_title = current["meta"].get("title", "")
    if hit is not None:
        if str(hit.get("detail") or "") == detail:
            return {"ok": True, "existed": True}   # 幂等：同 (target, kind, detail)
        hit["detail"] = detail                      # detail 变化 → 就地更新
        result = await card_store.update_card(
            work_id, current_id, {"relations": existing},
            f"由「{cur_title}」对话更新依赖关联：{kind} → {target_id}",
        )
        if "error" in result:
            return result
        return {"ok": True, "updated": True, "source": current_id,
                "target": target_id, "kind": kind, "detail": detail}
    existing.append({"target": target_id, "kind": kind, "detail": detail})
    result = await card_store.update_card(
        work_id, current_id, {"relations": existing},
        f"由「{cur_title}」对话建立依赖关联：{kind} → {target_id}",
    )
    if "error" in result:
        return result
    cards[current_id] = {**(result.get("meta") or {}), "card_id": current_id}
    return {"ok": True, "source": current_id, "target": target_id, "kind": kind}


async def _h_unlink_rel(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """解除依赖关联线：从当前卡 ``relations`` 移除指向 target 的条目。

    ``kind`` 缺省＝移除**全部**指向该 target 的条目；给了 kind 只移除对应条目。
    权限：只判**发起方**有 ``SWITCH_UNLINK_REL``（[三线拆开关 2026-10-27]，
    与 ``link_rel`` 同门独立开关，不再复用 ``SWITCH_UNLINK``）；
    目标卡已删的悬空条目仍可清理（目标不存在则无卡可查，直接放行）。
    """
    target_id = str(arguments.get("target_id", "") or "")
    kind = str(arguments.get("kind", "") or "").strip()
    if not target_id:
        return err("validation", "target_id 不能为空")
    if kind and kind not in MODEL_DEP_KINDS:
        return err("validation",
                   f"kind 非法: {kind}，只允许 {'/'.join(MODEL_DEP_KINDS)}")
    if target_id in cards:
        # [连线不看对方 2026-10-22] 只判发起方（原来查目标卡 perms）。
        bad = _require_wire_switch(current_id, policy.SWITCH_UNLINK_REL, cards, "解依赖线")
        if bad is not None:
            return bad
    current = await card_store.read_card(work_id, current_id)
    existing = list(current["meta"].get("relations") or [])

    def _match(rel: Any) -> bool:
        if not isinstance(rel, dict):
            return False
        if rel.get("target") != target_id:
            return False
        return (not kind) or rel.get("kind") == kind

    remaining = [r for r in existing if not _match(r)]
    removed = len(existing) - len(remaining)
    if removed == 0:
        detail = f" 的 {kind} 依赖关联" if kind else ""
        return err("not_found", f"依赖关联不存在: 当前卡无指向 {target_id}{detail}")
    cur_title = current["meta"].get("title", "")
    result = await card_store.update_card(
        work_id, current_id, {"relations": remaining},
        f"由「{cur_title}」对话解除依赖关联 → {target_id}",
    )
    if "error" in result:
        return result
    # [5713 通道试走] 同轮 meta 缓存同步（与 _h_create_card 同手法）：模型可以在
    # **同一轮**里先 link_cards 再 send_card_message——不做这一步，发信读到的还是
    # 本回合开始时的旧卡面（没有新线），会被「无线即拒」错杀（g2 用例实测踩到）。
    cards[current_id] = {**(result.get("meta") or {}), "card_id": current_id}
    return {"ok": True, "source": current_id, "target": target_id, "removed": removed}


async def _h_search_manuscript(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not _actor_can_switch(cards.get(current_id), policy.SWITCH_MANUSCRIPT):
        return err("denied", "无权检索本项目原始资料（manuscript 开关未授权）")
    if not manuscript_store.list_chapters(work_id):
        return err("not_found", "本项目尚无原文")
    hits = manuscript_store.search_manuscript(
        work_id,
        arguments.get("keyword", ""),
        limit=int(arguments.get("limit") or 10),
    )
    return {"hits": hits}


# [联网能力 2026-10-02] 只给主模型的中性读工具：结果仅作工具返回进
# 当前调用，不写卡、不进常设上下文；三思考席收 tools=None 物理上不可调用
# [并发修复 2026-10-04] 联网两工具是同步 def（requests 网络 IO，SearxNG
# 最长 30s、阿里云备用通道最长 120s），必须卸载到线程池执行——直接
# 调用会冻结 uvicorn 事件循环，期间所有请求（progress 轮询/其他剧本
# 的消息/探活）全部排队，前端 15s 闸（api.js:26-28）一到就弹「请求超时」。
# 这与 10-03 16:20 file_tools 修复同族，当时只修了文件工具、漏了这两个。
# 这不是闸，是执行位置修正；search_tool.py 函数本身一行未改。
async def _h_web_search(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from . import search_tool
    return await asyncio.to_thread(
        search_tool.web_search,
        arguments.get("query", ""),
        count=int(arguments.get("count") or 10),
    )


async def _h_fetch_web(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from . import search_tool
    return await asyncio.to_thread(
        search_tool.fetch_web,
        arguments.get("url", ""),
    )


# [本地文件能力 2026-10-03] 四件文件工具：边界与兜底全在 file_tools 内，
# 与卡片树权限无耦合（材料全局共享），不进任何常设上下文
# [并发修复 2026-10-03] 四件工具是同步 def（文件 IO 阻塞），必须
# 卸载到线程池执行——直接调用会冻结 uvicorn 事件循环，期间所有
# 请求（progress 轮询/其他剧本的消息/探活）全部排队（实测：全库
# search_files 0.906s 期间事件循环 ticker 零打点）。这不是闸，
# 是执行位置修正；工具函数本身一行未改。
async def _h_list_files(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [T6 路径锁 2026-10-09][权限底座重做 2026-10-12] 文件路径锁：当前卡 perms
    # 须授 tools_file，路径须过 ALLOWED_ROOTS 边界＋FILE_SCOPE 前缀＋内部凭证
    # 排除（file_tools.check_file_access）
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id),
    ):
        return err("denied", f"越权：无权访问 {arguments.get('path', '')}")
    return await asyncio.to_thread(
        file_tools.list_files,
        arguments.get("path", ""),
        limit=int(arguments.get("limit") or 200),
    )


async def _h_read_file(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [T6 路径锁 2026-10-09] 同上（统一关口）
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id),
    ):
        return err("denied", f"越权：无权访问 {arguments.get('path', '')}")
    return await asyncio.to_thread(
        file_tools.read_file,
        arguments.get("path", ""),
        para_start=arguments.get("para_start"),
        para_end=arguments.get("para_end"),
    )


async def _h_search_files(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [基础工具找回 2026-10-18] 参数校验先于权限关口：keyword/pattern 两空＝validation；
    # pattern 非空优先于 keyword（正则非法也在此拦）。
    kw = str(arguments.get("keyword") or "")
    pattern = str(arguments.get("pattern") or "").strip()
    if not kw.strip() and not pattern:
        return err("validation", "keyword 与 pattern 至少给一个")
    if pattern:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return err("validation", f"正则非法: {exc}")
    # [T6 路径锁 2026-10-09] 同上（统一关口）
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id),
    ):
        return err("denied", f"越权：无权访问 {arguments.get('path', '')}")
    return await asyncio.to_thread(
        file_tools.search_files,
        kw,
        arguments.get("path", ""),
        limit=int(arguments.get("limit") or 50),
        pattern=pattern,
    )


async def _h_write_file(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [T6 路径锁 2026-10-09] 同上（统一关口）
    # [只读档 2026-10-22] 写类显式 write=True：只读卡（tools_read_file）不许写。
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id), write=True,
    ):
        return err("denied", f"越权：无权访问 {arguments.get('path', '')}")
    return await asyncio.to_thread(
        file_tools.write_file,
        arguments.get("path", ""),
        arguments.get("content", ""),
    )


async def _h_run_tool_card(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """第 19 件工具 · 调用一张工具卡（工具卡底座 T7，EC-054~EC-059）。

    代码执行四步，**四步全是 IO**（无一步是判断）：

    ① 按 ``script_name`` 读剧本 —— **读不到＝报错**（解析器的事实，不是
       「这个剧本名不该用」的判断，EC-059）；
    ② 建一张**新**卡：``card_type="tool"``、``owner_card_id=当前卡``、
       ``parent=当前卡``、``title=剧本名+卡号``；
    ③ 初始化运行时，取剧本的**第一步**；
    ④ 返回该步的步骤指令文本。

    过闸（EC-057，全部由 ``card_store._write_gate`` 现成执行，本 handler
    **不另设一套判据**）：

    - **闸 3**（owner 合法，``card_store`` L583-596）——owner 必须落在
      本树内且其 ``card_type`` 属允许集合；
    - **闸 4**（状态机，L608-617）；
    - **工具卡不可有子卡**（L597-606，现成结构规则）。

    ⚠️ **owner 类型边界（EC-057/任务书 T7 必读）**：闸 3 放宽后合法 owner 为
    ``sub_project`` 与 ``project``。**若主卡是 ``topic`` 类**（研究树现存
    35 张：work_001 22 张 + work_003 13 张），owner_type 不在放宽范围内
    ⇒ **闸 3 拒 ⇒ 建卡失败**。此处**如实把闸 3 的原文案回给模型**，
    **不自行扩大放宽范围、不绕过闸 3、不换个 owner 蒙混**（R-F）。
    该情形已列入回报的【待用户裁】项。

    **N 次 N 张**（EC-058）：每次调用**新建一张卡**，不覆盖、不删除、
    不复用上一张。已有卡与连线一律不受影响（R-G）。
    """
    script_name = str(arguments.get("script_name", "") or "").strip()
    instruction = str(arguments.get("instruction", "") or "").strip()
    if not script_name:
        return err("validation", "script_name 不能为空")

    # [工具面补齐 2026-10-12] invoke_tool 开关的执行层关口（原先无任何关口）。
    # 非树类开关 ⇒ 只判当前卡自己的类型分权，不叠子树。
    if not policy.has_perm(cards.get(current_id), policy.SWITCH_INVOKE_TOOL):
        return err("denied", "无权调用工具卡（invoke_tool 开关未授权）")

    # ① 读剧本（找不到＝报错；抛的是解析器事实）
    try:
        toolcard_script_loader.load_script(script_name)
    except toolcard_script_loader.ScriptError as exc:
        return err("not_found", str(exc))

    if current_id not in cards:
        return err("not_found", f"卡片不存在: {current_id}")

    # ② 建新卡。instruction 包进「## 现状」块再作正文种子——create_card 的
    #    区头检测：无区头会回落 NEW_CARD_SKELETON，模型这句话会被丢掉。
    #    包区头是**机械结构处置**，不替模型改写一个字。
    seed = f"## 现状\n{instruction}" if instruction else ""
    created = await card_store.create_card(
        work_id, script_name, "模型生成",
        card_type="tool",
        parent=current_id,
        conclusion=seed,
        owner_card_id=current_id,
    )
    if "error" in created:
        # 闸 3 / 闸 4 / 闸 1·2 的拒绝都走这条路径。**原文案透传**，
        # 不改写、不降级、不重试蒙混（"如实回报 topic owner 被闸3 拒"）。
        return err("validation", str(created.get("error")))
    new_id = created["meta"]["card_id"]

    # 卡片编号在 create_card 内部发放（card_store L739），拿不到号之前无法
    # 把「序号」写进 title；故建卡后按**公开接口**回写一次标题
    # （不用 _next_seq 之类私有量，保持跨层纪律——与 card_store B-09 同源口径）。
    try:
        await card_store.update_card(
            work_id, new_id, {"title": f"{script_name}·{new_id}"},
            "工具卡建卡：标题补卡号",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具卡标题回写失败 card=%s: %s", new_id, exc)

    # 同轮后续工具能看见这张新卡（与 _h_create_card 同手法）
    cards[new_id] = {**created["meta"], "card_id": new_id}

    # ③ 初始化运行时（落在剧本第一步）
    rt = await toolcard_driver.start(work_id, new_id, script_name)
    if not rt.get("ok"):
        return err("internal", f"工具卡运行时初始化未成: {rt}")

    # ④ 返回该步的步骤指令（只含事实与提示词原文，零评价——见 step_prompt）
    return {
        "card_id": new_id,
        "title": f"{script_name}·{new_id}",
        "parent": current_id,
        "owner_card_id": current_id,
        "script_name": script_name,
        "current_step": rt.get("step_id", ""),
        "instruction": toolcard_step_prompt.build_instruction(work_id, new_id) or "",
    }


# ============================================================
# 注册表单点（schema 字面量 ＋ handler ＋ readonly 住在一起）
#
# 顺序＝改前 build_tool_schemas() 的字面量顺序逐字保持（14 件内置），
# file_tools 四件在后（改前是 list + build_file_tool_schemas() 追加）。
# 字典保序⇒ 工具面顺序逐字不变。
# ============================================================


# [基础工具找回 2026-10-18 · Cardlo 搬运] 精准编辑 + 执行器 + 六件 handler（逐字自 Cardlo；置于 TOOL_REGISTRY 之前——注册表引用这些名字）
# ============================================================

# 输出截断上限（字节）与缺省超时（秒）。
_EXEC_MAX_OUTPUT_BYTES = 50 * 1024
_EXEC_TIMEOUT_SECONDS = 120.0
# 超时杀进程后**清管**（drain）的硬上限（秒）。Windows 下 shell 模式起的是
# cmd.exe → 孙进程的树：杀掉 cmd.exe 并不关掉孙进程手里的 stdout 句柄，
# 无界 drain 会等到孙进程自己退出——契约 ②「服务不挂死」就破了。故 drain
# 有界：管子关得快就拿到收尾数据，关不快也顶多再花这么多秒（输出反正要丢）。
_EXEC_DRAIN_TIMEOUT_SECONDS = 2.0


def _log_exec_guard(action: str, *, hits: int = 0, names: list[str] | None = None,
                    shell_cmd: str | None = None, argv: list[str] | None = None) -> None:
    """[凭证保护 2026-10-20] 凭证闸留痕：拒绝/脱敏各记一条（与权限判定同通道）。"""
    prompt_log.append_perm_log({
        "fn": "exec_credential_guard",
        "action": action,
        "hits": int(hits),
        "names": list(names or []),
        "cmd_head": (shell_cmd or " ".join(argv or []))[:200],
    })


def _credential_deny_message(names: list[str], tool: str) -> str:
    """静态闸的拒绝文案：说清为什么、并给出不改判据的替代做法。"""
    return (f"拒绝执行：{tool} 的入参里出现凭证类文件名 {names}。"
            "凭证文件的内容不得经 exec 类工具读取（与 read_file/search_files 同一份黑名单）；"
            "确需确认某个配置项时，请查非凭证文件（如 .env.example）或向用户索取。")


def _clip_output(data: bytes) -> tuple[str, bool]:
    """按**字节**截断到 50KB 再解码（utf-8 replace），返回 (文本, 是否截断)。"""
    total = len(data)
    if total <= _EXEC_MAX_OUTPUT_BYTES:
        return data.decode("utf-8", "replace"), False
    head = data[:_EXEC_MAX_OUTPUT_BYTES].decode("utf-8", "replace")
    return f"{head}\n…（输出已截断，共 {total} 字节）", True


def _kill_quietly(proc: asyncio.subprocess.Process) -> None:
    """杀子进程（善后用，失败不盖过原结论）。"""
    try:
        proc.kill()
    except (OSError, RuntimeError):
        pass


def _close_transport_quietly(proc: asyncio.subprocess.Process) -> None:
    """弃管时关掉子进程传输（等价于 3.13 起的公开 ``Process.close()``）。

    超时清管放弃后传输还开着，会在 GC 时向**已关闭的事件循环**要回调
    （「unclosed transport」「Event loop is closed」两条告警）。趁循环还活着
    先关掉，告警与 fd 泄漏一起消失。
    """
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001 —— 善后失败不盖过原结论
        pass


async def _h_edit_file(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[基础工具找回 2026-10-17 · 逐字搬自 toolfold] 精准编辑。与 ``_h_write_file``
    同构：同一道 ``check_file_access`` 关口、同一个 ``asyncio.to_thread`` 卸载
    ——文件 IO 不冻事件循环。
    """
    # [只读档 2026-10-22] 写类显式 write=True。
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id), write=True,
    ):
        return err("denied", f"越权：无权访问 {arguments.get('path', '')}")
    return await asyncio.to_thread(
        file_tools.edit_file,
        arguments.get("path", ""),
        arguments.get("old_string", ""),
        arguments.get("new_string", ""),
    )


async def _run_subprocess(
    argv: list[str] | None = None,
    shell_cmd: str | None = None,
    cwd: str | None = None,
    timeout: float = _EXEC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """异步执行一条命令，返回 ``{exit_code, stdout, stderr, truncated}`` 或 err 信封。

    ``cwd`` 缺省＝项目根（``file_tools.BASE_DIR``）：**调用时现取**而不是写死
    导入期常量——测试沙箱正是改写这个模块级常量（conftest 铁律一），写死会让
    测试期命令跑进真实 D:\\AI_station。
    """
    run_cwd = str(cwd or file_tools.BASE_DIR)
    try:
        if shell_cmd is not None:
            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                cwd=run_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *(argv or []),
                cwd=run_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
    except Exception as exc:  # noqa: BLE001 —— 起不了子进程＝error 信封（契约 ⑤）
        return err("internal", f"子进程启动失败: {exc}")

    try:
        out, e = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _kill_quietly(proc)
        try:
            # 有界清管：见 _EXEC_DRAIN_TIMEOUT_SECONDS（Windows 孙进程持有
            # 管道句柄时，无界 drain 会把「超时」变成第二次挂死）。
            await asyncio.wait_for(
                proc.communicate(), timeout=_EXEC_DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            # 管子还是不关（孙进程仍攥着句柄）⇒ 弃管松手
            _close_transport_quietly(proc)
        except Exception:  # noqa: BLE001 —— 善后失败不改变「已超时」的结论
            pass
        return err("locked", f"执行超时（>{timeout:g}s）")
    except asyncio.CancelledError:
        # [强制停止 2026-10-24] CancelledError 不走 Exception(3.8+ 是
        # BaseException)——不接住它,强制停止取消协程后子进程**还在跑**。
        _kill_quietly(proc)
        raise
    except Exception as exc:  # noqa: BLE001
        _kill_quietly(proc)
        return err("internal", f"子进程输出读取失败: {exc}")

    stdout, cut_out = _clip_output(out or b"")
    stderr, cut_err = _clip_output(e or b"")
    # [凭证保护 2026-10-20] 执行后值闸：返回值里出现凭证值就替换成 REDACTED。
    # **这是安全边界**（静态闸只挡直读，拼接/编码绕得过；值闸只认"明文相等"）。
    # 走 to_thread：首次采集要扫 BASE_DIR 下的凭证文件名，不冻住事件循环。
    stdout, n_out = await asyncio.to_thread(credential_guard.redact_output, stdout)
    stderr, n_err = await asyncio.to_thread(credential_guard.redact_output, stderr)
    hits = int(n_out) + int(n_err)
    if hits:
        note = f"\n…（输出中 {hits} 处凭证值已脱敏）"
        if stdout:
            stdout += note
        else:
            stderr += note
        _log_exec_guard("redact", hits=hits, shell_cmd=shell_cmd, argv=argv)
    return {
        "exit_code": int(proc.returncode or 0),
        "stdout": stdout,
        "stderr": stderr,
        "truncated": bool(cut_out or cut_err),
    }


async def _h_bash(work_id: str, current_id: str,
                  arguments: dict[str, Any],
                  cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """终端执行：命令失败（非零退出码）不是错误，原样回退出码与输出。"""
    command = str(arguments.get("command") or "").strip()
    if not command:
        return err("validation", "command 不能为空")
    # [凭证保护 2026-10-20] 执行前静态闸：命令里直接点名凭证文件 ⇒ 拒绝执行。
    refs = credential_guard.find_credential_refs(command)
    if refs:
        _log_exec_guard("deny", names=refs, shell_cmd=command)
        return err("denied", _credential_deny_message(refs, "bash"))
    return await _run_subprocess(shell_cmd=command)


async def _h_python(work_id: str, current_id: str,
                    arguments: dict[str, Any],
                    cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """代码执行：写系统临时 .py 再跑（不用 ``python -c``，规避 Windows 命令行
    多行字符串的引号/换行转义坑），finally 必删临时文件。"""
    code = str(arguments.get("code") or "")
    if not code.strip():
        return err("validation", "code 不能为空")
    # [凭证保护 2026-10-20] 执行前静态闸：代码里直接点名凭证文件 ⇒ 拒绝执行。
    refs = credential_guard.find_credential_refs(code)
    if refs:
        _log_exec_guard("deny", names=refs, argv=[code])
        return err("denied", _credential_deny_message(refs, "python"))
    tmp_path = ""
    try:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8",
            ) as fh:
                fh.write(code)
                tmp_path = fh.name
        except OSError as exc:
            return err("internal", f"临时文件创建失败: {exc}")
        return await _run_subprocess(argv=[sys.executable, tmp_path])
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                logger.warning("python 临时文件清理失败 %s: %s", tmp_path, exc)


def _glob_files_impl(pattern: str, path: str) -> dict[str, Any]:
    """glob 实现：只收文件，跳过剪枝目录与凭证文件，上限 200 条。

    起始目录：给了 ``path`` ⇒ 过 ``_resolve``（相对路径落项目根、越界拒，
    与文件四件同边界）；**缺省＝项目根**（``BASE_DIR`` 本身就是那条边界，
    无须再校验）。
    """
    if path:
        start, e = file_tools._resolve(path)
        if e is not None:
            return err("validation", str(e.get("error", "")))
    else:
        start = Path(file_tools.BASE_DIR)
    matches: list[str] = []
    truncated = False
    try:
        walker = start.rglob(pattern) if "**" in pattern else start.glob(pattern)
        for p in walker:
            if len(matches) >= 200:
                truncated = True
                break
            try:
                if not p.is_file():
                    continue
                if file_tools._is_pruned(p) or file_tools._credential_excluded(p):
                    continue
            except OSError:
                continue
            matches.append(file_tools._rel(p))
    except (OSError, ValueError) as exc:
        return err("validation", f"glob 模式无效: {exc}")
    return {
        "pattern": pattern,
        "scope": file_tools._rel(start),
        "matches": matches,
        "truncated": truncated,
    }


async def _h_glob_files(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """按文件名模式找文件。遍历可能很长 ⇒ 卸线程池（同文件四件口径，不冻事件循环）。"""
    pattern = str(arguments.get("pattern") or "")
    if not pattern.strip():
        return err("validation", "pattern 不能为空")
    return await asyncio.to_thread(
        _glob_files_impl, pattern.strip(), str(arguments.get("path") or "").strip(),
    )


def _path_or_err(raw: Any, label: str = "path") -> tuple[Path | None, dict[str, Any] | None]:
    """取参 → 非空校验 → _resolve 边界校验。返回 (路径, None) 或 (None, err 信封)。

    _resolve 的失败原文（空/越界/无法解析）按 **validation** 回——它是「这个
    path 参数不合法」，不是权限判定（权限在工具面，见裁定 1）。
    """
    text = str(raw or "").strip()
    if not text:
        return None, err("validation", f"{label} 不能为空")
    target, e = file_tools._resolve(text)
    if e is not None:
        return None, err("validation", str(e.get("error", "")))
    return target, None


async def _h_delete_file(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """删文件：只删文件（目录拒）；不存在＝not_found。"""
    target, e = _path_or_err(arguments.get("path"))
    if e is not None:
        return e
    assert target is not None
    if target.is_dir():
        return err("validation", "这是目录不是文件")
    if not target.exists():
        return err("not_found", f"文件不存在: {file_tools._rel(target)}")
    try:
        target.unlink()
    except OSError as exc:
        return err("internal", f"删除失败: {exc}")
    return {"path": file_tools._rel(target), "deleted": True}


async def _h_move_file(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """移动文件：src/dst 都过边界校验；src 不存在＝not_found；返回新路径。"""
    src, e = _path_or_err(arguments.get("src"), "src")
    if e is not None:
        return e
    dst, e = _path_or_err(arguments.get("dst"), "dst")
    if e is not None:
        return e
    assert src is not None and dst is not None
    if not src.exists():
        return err("not_found", f"文件不存在: {file_tools._rel(src)}")
    # dst 已是目录 ⇒ shutil 语义是「移进去」，回执须报**真实落点**而不是入参。
    final = dst / src.name if dst.is_dir() else dst
    try:
        shutil.move(str(src), str(final))
    except OSError as exc:
        return err("internal", f"移动失败: {exc}")
    return {"from": file_tools._rel(src), "path": file_tools._rel(final)}


async def _h_rename_file(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """改名（同目录）：new_name 只能是纯文件名（含路径分隔符＝validation）。"""
    target, e = _path_or_err(arguments.get("path"))
    if e is not None:
        return e
    assert target is not None
    new_name = str(arguments.get("new_name") or "").strip()
    if not new_name:
        return err("validation", "new_name 不能为空")
    if "/" in new_name or "\\" in new_name:
        return err("validation", "new_name 只能是文件名")
    if not target.exists():
        return err("not_found", f"文件不存在: {file_tools._rel(target)}")
    renamed = target.with_name(new_name)
    try:
        target.rename(renamed)
    except OSError as exc:
        return err("internal", f"改名失败: {exc}")
    return {"from": file_tools._rel(target), "path": file_tools._rel(renamed)}

# [拆分流程 2026-10-21] 使命「未填」的判据：新卡骨架里的模板句。
# 只用来判「有没有写」，不判文字好坏（那不是机械能判的）。
_MISSION_PLACEHOLDER = "（本卡作用/目标、授权来源"

# [拆分流程 2026-10-21] 拆分轮（op="split"）摘掉的工具：一切写产物/执行类。
# 理由：这一轮它只该做组织（按职责建卡、填使命），不该顺手开工。
# 保留读类与组织类 => 它能把事想清楚、把卡建起来，但动不了代码。
SPLIT_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "move_file", "rename_file", "delete_file",
    "bash", "python",
})

OP_SPLIT = "split"
TOOL_REGISTRY: dict[str, ToolDef] = {
    "read_card": ToolDef(
        schema={
            "name": "read_card",
            "description": (
                "读取指定卡（标题、父卡、连线、三个内容块）。返回三个块：使命／现状／第三块"
                "（第三块在本树的名字与用途见上下文「卡片区域说明」）。"
                "只能读取你有权访问的卡。"
                "大块默认只回前段（截到行边界并给出共多少行）；要看其中一块的指定行段："
                "带 block=块名＋para_start/para_end（1 起、闭区间），不必整块回读。"
            ),

            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id，如 card_002"},
                    "block": {"type": "string",
                              "description": "只读某一块时给块名：使命／现状／需求文档／需求文档·草稿（缺省回全卡面）"},
                    "para_start": {"type": "integer",
                                   "description": "块内起始行号（1 起，需带 block），缺省 1"},
                    "para_end": {"type": "integer",
                                 "description": "块内结束行号（闭区间，需带 block），缺省到块末"},
                },
                "required": ["card_id"],
            },
        },
        handler=_h_read_card,
        readonly=True,
    ),
    # [T12-R3] 记忆锚点下探（condense_fold 展开通道）：按块序号锚点取回已凝
    # 记忆条目——锚点不可用则凝练历史不可回查（方案 §七 展开工具）。只读拉取；
    # 记忆条目不是卡片正文，正文仍经 read_card。
    "read_memory": ToolDef(
        schema={
            "name": "read_memory",
            "description": "下探指定卡的凝练记忆（condense_fold：已凝练历史经归拢的条目，带锚点行可下探）。可用块序号区间按锚点取回。记忆条目不是卡片内容——三个块请用 read_card。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id，如 card_002；缺省＝当前卡"},
                    "from_seq": {"type": "integer", "description": "起始块序号（含），锚点下界；缺省不设下界"},
                    "to_seq": {"type": "integer", "description": "结束块序号（含），锚点上界；缺省不设上界"},
                },
                "required": [],
            },
        },
        handler=_h_read_memory,
        readonly=True,
    ),
    # [F19 · 2026-10-31] memory_expand（展开工具，dsh applyMemoryExpandTool）：
    # 记忆区渲染带锚点行（@锚点{card,seq}）＋展开说明 ⇒ 模型沿锚点下探：
    # 结论 → 描述层 → 逐字原文；sunk 命中即复活。只读。
    "memory_expand": ToolDef(
        schema={
            "name": "memory_expand",
            "description": (
                "展开一条凝练记忆：给记忆区锚点行里的 seq（@锚点{card,seq}），"
                "返回该条的结论、描述层与逐字原文。该条此前若已沉没（时效回收），"
                "本次展开会自动复活。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id；缺省＝当前卡"},
                    "seq": {"type": "integer", "description": "锚点块号（记忆区 @锚点{card,seq} 里的 seq）"},
                    "page": {"type": "integer", "description": "分页页码（原文过长时翻页，从 0 起）；缺省 0"},
                },
                "required": ["seq"],
            },
        },
        handler=_h_memory_expand,
        readonly=True,
    ),
    # [建卡工具化 2026-10-08] 标准工具通道建卡。权限＝关口 CREATE_CHILD 判定
    # （代码现状口径：只能建自己的子卡片）——parent 缺省＝当前卡自身。
    # source 恒「模型生成」、card_type 恒「线索」（不向模型开放类型枚举，
    # 避免模型选型；类型是系统内部概念，与文字协议块旧行为逐字一致）。
    "create_card": ToolDef(
        schema={
            "name": "create_card",
            "description": (
                "在你管辖范围内创建一张新卡片。只能建自己的子卡片："
                "parent 缺省＝当前卡自身；给 parent 时该卡必须落在你的子树内。"
                "建卡成功后系统会派生该卡的影子卡（过程记录），不需要你手动建。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "卡名，如「性能优化·监听器泄漏」"},
                    "parent": {
                        "type": "string",
                        "description": "父卡 id，如 card_006；缺省＝挂在当前卡下。必须在你的子树内，越权会被拒。",
                    },
                    "content": {
                        "type": "string",
                        "description": "卡的内容（三个块，缺省空）。",
                    },
                    "mission": {
                        "type": "string",
                        "description": (
                            "【必填】这张卡的使命（立项定稿）：它负责什么、为什么存在、"
                            "边界在哪、负责到什么程度。与 goal 不同——goal 是这一轮要达成的"
                            "目标，会随轮次变；mission 是这张卡的身份，建卡时定下、不随目标漂移。"
                            "只写职责，不写做法、步骤、文件清单、接口。"
                        ),
                    },
                },
                "required": ["title", "mission"],
            },
        },
        handler=_h_create_card,
        readonly=False,
    ),
    "edit_card": ToolDef(
        schema={
            "name": "edit_card",
            "description": (
                "改写你管辖范围内指定卡的三个内容块（覆盖写，content 是整份内容，按三个区头分段）。"
                "三块：## 使命（这张卡要做什么、要做成什么；立项定稿，你改它会被拒）、"
                "## 现状（这张卡现在实际是什么样：写事实，覆盖写·单一最新，只写能核实的）、"
                "## 需求文档（第三块：本卡全部需求的确定版，唯一确定源；"
                "按需求逐条写清「要什么、验收标准是什么」；只写确定版，覆盖写·单一最新；"
                "讨论、备选、过程不写这里）。"
                "需求文档该写的内容不要写进现状块（会被拒）。"
                "**改坏了能找回**：每次写入前系统自动存一版旧内容（每卡最近 20 版）。"
                "list_versions=true 列出可回退的版本（只读，不写任何东西）；"
                "restore_version=N 回退到该版本（此时不用给 content）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id"},
                    "content": {
                        "type": "string",
                        "description": "整份内容（覆盖写，按三个区头分段；第三块区头用本树的名字）。"
                                       "给了 restore_version 时不需要它",
                    },
                    "restore_version": {
                        "type": "integer",
                        "description": "回退到该版本号（先用 list_versions=true 查）。"
                                       "回退到 N＝回到第 N 次改动**之前**的内容",
                    },
                    "list_versions": {
                        "type": "boolean",
                        "description": "只列出版本历史（版本号/时间/改动说明/各块长度），不写任何东西",
                    },
                },
                "required": ["card_id"],
            },
        },
        handler=_h_edit_card,
        readonly=False,
    ),
    # [父卡设子卡使命 2026-10-21 · 用户裁定「父卡必须能设子卡使命」]
    # 为什么单开一把工具，而不是放开 edit_card 的使命锁：
    #   edit_card 拒改使命是对的（防模型覆盖**自己的**立项）；但那条锁把
    #   「上级给下级立使命」一起锁死了。实测 v11 样本2：六张子卡使命块 6/6
    #   全是占位符，根卡只能把职责塞进 goal（goal 随轮次漂移，跑完什么都不剩）。
    # 权限＝create_child ＋ 树作用域：能建子卡者才能立使命；worker 没有该开关
    # ⇒ 既不能改自己也不能改别人，权限面不扩大。
    "set_child_mission": ToolDef(
        schema={
            "name": "set_child_mission",
            "description": (
                "给你子树内的卡设定/修订使命（立项定稿）：它负责什么、为什么存在、边界在哪。"
                "使命是这张卡的身份，不随目标漂移；goal 才是这一轮要达成的目标。"
                "只写「负责哪一块、负责到什么程度」，不要写具体做法、步骤、文件清单、接口。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id（须在你的子树内）"},
                    "mission": {
                        "type": "string",
                        "description": "使命全文（覆盖写）：负责什么、边界在哪、负责到什么程度",
                    },
                },
                "required": ["card_id", "mission"],
            },
        },
        handler=_h_set_child_mission,
        readonly=False,
    ),
    # [分块写入 2026-10-19 · permexp] 一个接口一种语义（自动模式面专用）。
    # 普通卡不下发（仍走 draft/commit）。
    # [2026-10-30] 原注释指「_h_update_status 上方长注释」——该函数已删，指针作废。
    "delete_card": ToolDef(
        schema={
            "name": "delete_card",
            "description": "删除你管辖范围内指定编号的卡片及其对话。剧本根卡与有子卡的卡不可删除。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id"},
                },
                "required": ["card_id"],
            },
        },
        handler=_h_delete_card,
        readonly=False,
    ),
    "read_shadow": ToolDef(
        schema={
            "name": "read_shadow",
            "description": "读取指定卡的影子卡全文（过程性记录）。card_id 缺省＝当前卡自己的影子；只读本卡自己的影子。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "正卡 id，如 card_002；缺省＝当前卡"},
                },
                "required": [],
            },
        },
        handler=_h_read_shadow,
        readonly=True,
    ),
    "list_manuscript": ToolDef(
        schema={
            "name": "list_manuscript",
            "description": "列出本项目已有的原文资料章节目录（序号/标题/字数/段数）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=_h_list_manuscript,
        readonly=True,
    ),
    "read_manuscript": ToolDef(
        schema={
            "name": "read_manuscript",
            "description": "调取指定章节原文资料，长章请用段落区间分段阅读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter": {"type": "string", "description": "章节号，如 ch_0001"},
                    "para_start": {"type": "integer", "description": "起始段号（从 1 开始，可选）"},
                    "para_end": {"type": "integer", "description": "结束段号（闭区间，可选）"},
                },
                "required": ["chapter"],
            },
        },
        handler=_h_read_manuscript,
        readonly=True,
    ),
    "search_manuscript": ToolDef(
        schema={
            "name": "search_manuscript",
            "description": "在全部原文资料中检索关键词，返回命中章节与上下文片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "检索关键词（子串匹配）"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
                },
                "required": ["keyword"],
            },
        },
        handler=_h_search_manuscript,
        readonly=True,
    ),
    "web_search": ToolDef(
        schema={
            "name": "web_search",
            "description": "查询网页搜索服务，返回结果列表（标题、链接、摘要、来源、日期）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                    "count": {"type": "integer", "description": "返回条数，默认 10"},
                },
                "required": ["query"],
            },
        },
        handler=_h_web_search,
        readonly=True,
    ),
    "fetch_web": ToolDef(
        schema={
            "name": "fetch_web",
            "description": "获取指定网页的文本内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "网页地址"},
                },
                "required": ["url"],
            },
        },
        handler=_h_fetch_web,
        readonly=True,
    ),
    "link_cards": ToolDef(
        schema={
            "name": "link_cards",
            "description": "在当前卡与目标卡之间建立连线。source 固定为当前对话卡。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标卡 id，如 card_003"},
                    "type": {
                        "type": "string",
                        "enum": ["先后", "关联"],
                        "description": "关系类型",
                    },
                    "note": {"type": "string", "description": "可选备注"},
                },
                "required": ["target_id", "type"],
            },
        },
        handler=_h_link_cards,
        readonly=False,
    ),
    "unlink_cards": ToolDef(
        schema={
            "name": "unlink_cards",
            "description": "解除当前卡上指向目标卡的连线（source 固定为当前对话卡）。目标卡已被删除时仍可解除——用于清理指向已删卡的悬空连线。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标卡 id"},
                    "type": {
                        "type": "string",
                        "enum": ["先后", "关联"],
                        "description": "可选，关系类型；缺省解除全部指向该目标的连线",
                    },
                },
                "required": ["target_id"],
            },
        },
        handler=_h_unlink_cards,
        readonly=False,
    ),
    # [工具卡底座 T7 · 2026-10-09] 第 19 件工具：调用一张工具卡。
    # 主卡权限收回后**唯一的新增能力**——「只剩调用工具卡」（EC-004/063/161）。
    # readonly=False（它确实建卡、落产物），但**不在** WRITE_TOOLS_REVOKED 里：
    # 它是被保留的那一件，不受角色过滤与执行层拦截。
    # [卡片底座第一步 2026-10-14] 第 20 件工具：沿通信线读对方卡。
    # 追加在既有 19 件之后 ⇒ 前 19 件的工具面顺序逐字不变。
    # readonly=True（纯读）；开关 read_linked（独立于 read）。
    "read_linked": ToolDef(
        schema={
            "name": "read_linked",
            "description": (
                "沿通信线读取与你**已有连线关系**的卡（内容块／影子／对话）。"
                "只能读你与它之间已建立连线的卡；没有连线则报「无通信线」。"
                "part 取：正文（＝这张卡的三个内容块：使命／现状／第三块）／影子（过程记录）／"
                "对话（该卡的事件时序）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "对方卡 id，如 card_003"},
                    "part": {
                        "type": "string",
                        "enum": ["正文", "影子", "对话"],
                        "description": "读哪部分：正文／影子／对话",
                    },
                },
                "required": ["target_id", "part"],
            },
        },
        handler=_h_read_linked,
        readonly=True,
    ),
    # [worker 卡 2026-10-15 · §六块2] 第 17 件工具：完工回报。追加在既有 16 件字面量
    # 之后 ⇒ 前 16 件的工具面顺序逐字不变。下发面：TOOL_SWITCH 挂 edit ＋
    # build_tool_schemas 第三段 WORKER_ONLY_TOOLS（只对 worker 卡下发）；执行层
    # handler 再校验 card_type=="worker" 且 state=="active"（双保险）。
    "report_done": ToolDef(
        schema={
            "name": "report_done",
            "description": (
                "任务做完时调用，调完即完工、本卡静默（worker 卡专用）。"
                "完工回执由系统自动发给主卡并调起验收，你不需要也不能发信；"
                "完工后本卡不再被激活、不可删除、永久留档。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "完工小结：做了什么、产物在哪（事实陈述，不自评好坏）",
                    },
                },
                "required": ["summary"],
            },
        },
        handler=_h_report_done,
        readonly=False,
    ),
    # [worker 卡 2026-10-15 · §六块2] 第 18 件工具：派生worker 卡并建卡即首跑（派生
    # 即调起）。挂 create_child（无该开关的类型压根看不见）；执行层关口与
    # create_card 同口径（只能建自己的子卡片）。
    "create_worker_card": ToolDef(
        schema={
            "name": "create_worker_card",
            "description": (
                "把一件**要跑命令才能干完**的开发任务交给新建的worker 卡：建一张"
                "worker 卡（任务原样落进它的使命块）并立即自动开跑——跑委派执行体"
                "（WorkBuddy），手里有完整 shell：能跑命令、起服务、跑构建和"
                "测试、装依赖。一次派一张；只能建自己的子卡片。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "worker 卡卡名，如「修复监听器泄漏」"},
                    "task": {"type": "string", "description": "开发任务内容（原样落进新卡的使命块）"},
                },
                "required": ["title", "task"],
            },
        },
        handler=_h_create_worker_card,
        readonly=False,
    ),
    # [UI 卡 2026-11-02] 建 UI 卡＝派 UI 执行卡：卡挂在你的树下、跑系统内
    # 模型（内循环，不走 wb_exec 委派），手里有 browser＋读文件＋影子写等
    # 能力，能开真实浏览器看自己写的页面、验证交互。建卡即首跑，完工经
    # report_up 回报。挂 create_child 同关口（建的是子卡）。
    "create_ui_card": ToolDef(
        schema={
            "name": "create_ui_card",
            "description": (
                "建一张 UI 卡并立即开工。它干的是界面活：自己写前端页面文件"
                "（HTML/CSS/JS），再开真实浏览器把页面打开亲眼看——看渲染对不对、"
                "点按钮/填表单验交互、读 console 和网络诊断、截图取证。\n"
                "它手里有：写/改文件件（write_file、edit_file——写页面文件）、"
                "browser 工具（看页面、操作页面）、只读文件件（读源码对照渲染）。\n"
                "它手里没有：bash/python——**跑不了任何命令**：起不了服务、"
                "跑不了构建、跑不了测试、写不了要执行的后端逻辑。\n"
                "一张卡一个任务，建卡即开工，完工经 report_up 回报。"
                "parent 缺省＝本卡，只可挂自己子树内。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "UI 卡卡名，如「卡面详情页原型」"},
                    "task": {"type": "string", "description": "UI 任务内容（原样落进新卡的使命块）"},
                    "parent": {
                        "type": "string",
                        "description": "挂在哪张卡下（缺省＝本卡；只可指自己或自己子树内的卡）",
                    },
                },
                "required": ["title", "task"],
            },
        },
        handler=_h_create_ui_card,
        readonly=False,
        doc=ToolDoc(
            group="组织",
            scene="需要做界面/验界面时派一张：它自己写前端页面文件"
                  "（HTML/CSS/JS），再开真实浏览器亲眼验——渲染、交互、"
                  "console。跑系统内模型，手里有浏览器＋写文件件、"
                  "没有 bash/python。",
            usage="title 给卡名；task 写清做什么界面、要验什么（验收口径越"
                  "具体，它 inspect/click 的循环越有目标）。完工经 report_up "
                  "异步回来。",
            effect=ToolEffect(sink="card", wake=True, targets="child"),
        ),
    ),
    # [差事卡 2026-11-01 · 用户令「建卡即召唤」] 建差事卡＝部署职能进场：
    # qa→验收卡、pm→执笔卡（挂本卡树下看得见），建卡即落差事账并把职能
    # 唤起；职能办完自动收工，结论经 report 回执到本卡。挂 create_child
    # 同关口（建的是子卡）；资格闸看 charters.deploy——无派驻职责书的
    # 职能不接差事。
    "create_deploy_card": ToolDef(
        schema={
            "name": "create_deploy_card",
            "description": (
                "建一张差事卡把职能召进场干活：qa→验收卡（阶段验收产物）、"
                "pm→执笔卡（写需求文档）。卡挂在你的树下、会话装职能的干活过程；"
                "建卡即派差事并唤起职能，它办完自动收工、结论回执到你这里。"
                "target 缺省＝本卡（验收/执笔你自己）；只可指直接子卡。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "function": {
                        "type": "string",
                        "description": "召哪个职能（目录 code）：qa＝验收、pm＝执笔需求文档",
                    },
                    "task": {
                        "type": "string",
                        "description": "差事指令：干什么、干到什么程度算完（落进差事卡使命块）",
                    },
                    "target": {
                        "type": "string",
                        "description": "对象卡 id：被验收/被写文档的卡；缺省＝本卡，只可指直接子卡",
                    },
                    "title": {
                        "type": "string",
                        "description": "差事卡卡名（缺省自动命名，如「验收·xxx」）",
                    },
                },
                "required": ["function", "task"],
            },
        },
        handler=_h_create_deploy_card,
        readonly=False,
        doc=ToolDoc(
            group="组织",
            scene="需要职能进场干活时：阶段验收召 qa（验产物 vs 需求文档）、"
                  "写需求文档召 pm。建的是一张看得见的差事卡——职能的过程留在"
                  "那里，不脏你的会话。",
            usage="function 给目录 code（qa/pm）；task 写清干什么、干到什么"
                  "程度算完；target 只在自己或直接子卡上生效。结论是异步回来的"
                  "——办完经 report 回执进你的会话。",
            effect=ToolEffect(sink="card", wake=True, targets="child"),
        ),
    ),
    # [规则卡 2026-10-15 · 块3] 第 19 件工具：根卡起草规则区修改（只起草不落盘）。
    # 追加在既有 18 件字面量之后 ⇒ 前 18 件工具面顺序逐字不变。下发面：TOOL_SWITCH
    # 挂 read ＋ build_tool_schemas 第四段 ROOT_ONLY_TOOLS（只对剧本根卡下发）；
    # 执行层 handler 再校验 current_id==work_card_id_of（双保险，同 report_done 手法）。
    # readonly=True：全程零写入（落盘走用户审批 /rule/commit，不经过本工具）。
    "draft_rule_edit": ToolDef(
        schema={
            "name": "draft_rule_edit",
            "description": (
                "起草规则分区的修改提议（仅剧本根卡可用；只起草、不落盘）。"
                "sections 里每项 {section, content}：把不同内容块各自写进对应区——"
                "写哪个区由你选（all 进所有卡；project/sub_project/worker 等按卡类型"
                "注入；function.<职能code> 只进对应职能）。一次调用可覆盖多个区，"
                "一次审批一次落盘。注意：content 里不得出现 `## ` 行（文件按 `## ` 切区，"
                "含 `## ` 的内容会被切成不进任何上下文的幻影区）；区内子标题用 `### `。"
                "返回每区提议+盘上现文+指纹——用户点「同意」才写入生效。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "description": "一次起草的区修改清单：[{section, content}, …]",
                        "items": {
                            "type": "object",
                            "properties": {
                                "section": {
                                    "type": "string",
                                    "description": "目标分区名（all/project/sub_project/…，或 function.<code>）",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "该区提议新内容（整区替换草案；不得含 `## ` 行）",
                                },
                            },
                            "required": ["section", "content"],
                        },
                    },
                    "section": {
                        "type": "string",
                        "description": "单区简形：与 content 配对（等价 sections=[{section,content}]）",
                    },
                    "content": {
                        "type": "string",
                        "description": "单区简形的内容",
                    },
                },
            },
        },
        handler=_h_draft_rule_edit,
        readonly=True,
    ),
    # [依赖关联线 2026-10-16] 第 20/21 件工具：第三种线「依赖关联线」的建/解线。
    # 追加在既有 19 件字面量之后 ⇒ 前 19 件工具面顺序逐字不变。写的是卡上
    # relations 字段（非 links）——与 link_cards/unlink_cards 对称但不混用；
    # 权限复用 link/unlink 开关；**不参与通信**（不能沿它 read_linked）。
    "link_rel": ToolDef(
        schema={
            "name": "link_rel",
            "description": (
                "在当前卡与目标卡之间建立一条**关系线**（第三种线，不用于通信）。"
                "kind 二选一：**依赖**＝我消费它的什么（它的接口/文档我要读）；"
                "**影响**＝我动什么会波及它（它的接口/文档它要读）。"
                "source 固定为当前对话卡。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标卡 id，如 card_003"},
                    "kind": {
                        "type": "string",
                        "enum": ["依赖", "影响"],
                        "description": "依赖＝我消费它的什么；影响＝我动会波及它",
                    },
                    "detail": {
                        "type": "string",
                        "description": ("写清**依赖它的什么**／**影响它的什么**（必填）。这条会常驻双方上下文——被依赖/被影响的那张卡也看得到，所以写具体：哪个接口、哪份文档、哪块行为。"),
                    },
                },
                "required": ["target_id", "kind", "detail"],
            },
        },
        handler=_h_link_rel,
        readonly=False,
    ),
    "unlink_rel": ToolDef(
        schema={
            "name": "unlink_rel",
            "description": (
                "解除当前卡上指向目标卡的依赖/关联关系线（source 固定为当前对话卡）。"
                "kind 缺省＝解除全部指向该目标的条目；给了 kind 只解对应条目。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标卡 id"},
                    "kind": {
                        "type": "string",
                        "enum": ["依赖", "关联"],
                        "description": "可选，关系种类；缺省解除全部指向该目标的条目",
                    },
                },
                "required": ["target_id"],
            },
        },
        handler=_h_unlink_rel,
        readonly=False,
    ),
    # [更新现状按钮改造 2026-10-16] 第 22 件工具：propose_status（只提议、不落盘）。
    # 追加在既有 21 件字面量之后 ⇒ 前 21 件顺序逐字不变。**不进常规工具面**
    # （列 OP_ONLY_TOOLS，build_tool_schemas 在四段过滤之后、仅 op 回合追加）；
    # TOOL_SWITCH 挂 read 只为满足「注册表每件工具都要有开关映射」的完备性不变量。
    # readonly=True：全程零写入（落盘走用户确认后的 /commit，不经过本工具）。
    "propose_status": ToolDef(
        schema={
            "name": "propose_status",
            "description": (
                "提议本卡「现状」的新内容（只提议、不落盘）。返回提议内容与盘上现文、"
                "指纹——你不能直接改现状，本工具只出草案；调用后系统会弹对比窗交用户"
                "确认，用户点「同意」才写入本卡现状生效。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "新的现状内容（整块替换草案；只写能核实的事实）",
                    },
                },
                "required": ["content"],
            },
        },
        handler=_h_propose_status,
        readonly=True,
    ),
    # [自动模式 2026-10-17 · 回灌 5700 块0.4] 第 23 件工具：update_goal
    # （声明目标态——自动模式「停」的唯一权威在模型）。**非卡片内挂**：列
    # AUTO_ONLY_TOOLS，build_tool_schemas 在四段过滤之后、仅 auto_mode=1 的
    # 卡追加 ⇒ 常规工具面逐字不变（块0.4 硬判据 1）。
    # TOOL_SWITCH 挂 plan 只为满足「注册表每件工具都要有开关映射」的完备性
    # 不变量（执行层双保险见 _h_update_goal）。
    # [goal 域 2026-10-20 · 照抄 dsh-tool-goal] 三件目标工具。原版是
    # get_goal / create_goal / update_goal(goal_id, revision, action)。旧的
    # ``update_goal(action, reason, await_children)`` 已整体退役——那是我们自己
    # 发明的简化版，没有版本号、没有权威校验、没有轮次上限。
    "get_goal": ToolDef(
        schema=goal_tools.GET_GOAL_SCHEMA,
        handler=goal_tools.h_get_goal,
        readonly=True,
    ),
    "create_goal": ToolDef(
        schema=goal_tools.CREATE_GOAL_SCHEMA,
        handler=goal_tools.h_create_goal,
        readonly=False,
    ),
    "update_goal": ToolDef(
        schema=goal_tools.UPDATE_GOAL_SCHEMA,
        handler=goal_tools.h_update_goal,
        readonly=False,
    ),
    # [恢复子卡 2026-10-20] 父卡把停下的子卡拉回自主推进的唯一手段。
    "resume_child": ToolDef(
        schema=goal_tools.RESUME_CHILD_SCHEMA,
        handler=goal_tools.h_resume_child,
        readonly=False,
    ),
    "bash": ToolDef(
        schema={
            "name": "bash",
            "description": "执行一条 shell 命令，返回标准输出、标准错误与退出码。命令失败（非零退出码）不是错误，会原样返回退出码与输出，由你判断成败。执行超时（>120 秒）会返回错误。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令（可含管道、重定向、&& 等 shell 语法）"}
                },
                "required": ["command"]
            }
        },
        handler=_h_bash,
        readonly=False,
    ),
    "python": ToolDef(
        schema={
            "name": "python",
            "description": "执行一段 Python 代码，返回标准输出、标准错误与退出码。代码在项目 venv 解释器下运行（能 import 项目已安装的库）。命令失败（非零退出码）不是错误。执行超时（>120 秒）会返回错误。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码（多行直接写，会写入临时文件后运行）"}
                },
                "required": ["code"]
            }
        },
        handler=_h_python,
        readonly=False,
    ),
    "glob_files": ToolDef(
        schema={
            "name": "glob_files",
            "description": "按文件名模式（glob）查找文件，如 '**/*.py'、'*.md'。返回匹配的文件路径列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 '**/*.py'"},
                    "path": {"type": "string", "description": "起始目录（相对项目根或绝对），缺省在项目根下找"}
                },
                "required": ["pattern"]
            }
        },
        handler=_h_glob_files,
        readonly=True,
    ),
    "delete_file": ToolDef(
        schema={
            "name": "delete_file",
            "description": "删除一个文件（目录不可删，需先清空目录内容）。删除不可撤销，调用前确认目标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件路径（相对 D:\\AI_station 或绝对）"}
                },
                "required": ["path"]
            }
        },
        handler=_h_delete_file,
        readonly=False,
    ),
    "move_file": ToolDef(
        schema={
            "name": "move_file",
            "description": "把文件从一处移动到另一处（同盘为改路径，内容不变）。两端路径都要在允许范围内。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源文件路径（相对 D:\\AI_station 或绝对）"},
                    "dst": {"type": "string", "description": "目标路径（文件路径或目录；落目录则保留原文件名）"}
                },
                "required": ["src", "dst"]
            }
        },
        handler=_h_move_file,
        readonly=False,
    ),
    "rename_file": ToolDef(
        schema={
            "name": "rename_file",
            "description": "给文件改名（同目录内，只改文件名不改位置）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要改名的文件路径（相对 D:\\AI_station 或绝对）"},
                    "new_name": {"type": "string", "description": "新文件名（纯文件名，不能带路径分隔符）"}
                },
                "required": ["path", "new_name"]
            }
        },
        handler=_h_rename_file,
        readonly=False,
    ),
    "edit_file": ToolDef(
        schema={
            "name": "edit_file",
            "description": "精准编辑文件：把文件中的 old_string 替换为 new_string（只改那一处）。old_string 必须在文件中恰好出现一次，否则报错，不会做任何修改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对 D:\\AI_station 或绝对）"},
                    "old_string": {"type": "string", "description": "要替换的原文（须与文件内容逐字匹配，含缩进与换行）"},
                    "new_string": {"type": "string", "description": "替换后的新内容"}
                },
                "required": ["path", "old_string", "new_string"]
            }
        },
        handler=_h_edit_file,
        readonly=False,
    ),
}

# [换父 2026-10-20] 结构边（父子）可变——追加在字面量**之后**，不插进既有序列
# 中间：既有工具面的顺序因此逐字不变，新增项排末尾。
# 开关挂 EDIT（与 edit_card 同一授权）：EDIT 是树类开关 ⇒ 执行层再判一次子树，
# 与工具面「看得见」同源——没这个开关就看不见它，调了也必拒。
async def _h_wake_card(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """让目标卡**现在就跑一轮**——不论它是不是 auto、有没有 goal、是否已完成。

    [统一唤醒 2026-10-22 · 用户裁定「根卡逻辑上应该能唤醒所有卡」]

    旧通道 `resume_child` 只恢复「有 goal + 未完成 + 子树内」的卡（
    [2026-11-01] 已不再要求 auto），所以无 goal 卡、离树角色卡唤不醒。
    本工具补上这个缺口：它不关心目标处于什么状态，只做一件事——起一轮。

    作用域（`_org_scope_allows`，与建卡/换父同源）：
      · 项目根卡     → 本项目**任何**卡，含离树的角色卡
      · 其他卡       → 自己作用域内（树内卡＝自己子树；离树角色卡＝整棵树）
      · 不作用于自己、不作用于根卡（除非自己就是根卡）

    三种目标各走各的起法：
      · 角色卡          → role_room.manual_check（收关注卡的同步再 spawn 一轮巡检）
      · 普通卡 + 有未处理消息 → message_runner.deliver_now（把消息变成一轮）
      · 普通卡 + 无消息 → 用 why 造一轮（这就是"叫醒它"本身）
    """
    target_id = str(arguments.get("card_id") or "").strip()
    why = str(arguments.get("why") or "").strip()
    if not target_id:
        return err("validation", "card_id 不能为空")
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if target_id == current_id:
        return err("validation", "不能唤醒自己；要推进请直接用 update_goal")

    # 作用域：根卡能唤醒全项目（含离树角色）；其他卡走统一作用域判定
    root_id = _root_id_of(cards)
    is_root = bool(root_id) and current_id == root_id
    if not is_root and not _org_scope_allows(
            current_id, target_id, policy.SWITCH_EDIT, cards):
        return err(
            "denied",
            f"越权：{target_id} 不在你的作用域内"
            f"（当前卡 {current_id}；根卡可唤醒本项目任何卡，"
            f"其他卡只能唤醒自己作用域内的卡）",
        )

    # [唤醒即解除停止 2026-11-01 · 用户令「唤醒子卡不需要自动」]
    # 被强制停止(halt)的卡连内部唤醒都拒——不叫停就叫不醒，本工具会报
    # started:true 的空话。上级唤醒与 resume_child 同级：先把停止标记摘掉。
    try:
        from . import control as _control
        _control.unhalt(work_id, target_id, by=current_id, via="wake_card")
    except Exception:  # noqa: BLE001
        pass

    target = cards.get(target_id) or {}
    is_role = str(target.get("card_type") or "") == "role"

    # ── 角色卡：走巡检引擎 ──
    if is_role:
        try:
            from . import role_room
            res = await role_room.manual_check(work_id, target_id)
        except Exception as exc:  # noqa: BLE001
            return err("internal", f"唤醒角色卡失败: {exc}")
        if isinstance(res, dict) and res.get("error"):
            return err("validation", str(res["error"]))
        started = bool((res or {}).get("started"))
        if started:
            return {
                "ok": True, "card_id": target_id, "via": "role_manual_check",
                "started": True, "reason": "",
                "pending_sources": list((res or {}).get("pending_sources") or []),
            }
        # [2026-10-22] manual_check 起不来是因为「没有新东西可看」——但上级是**主动**
        # 叫它，不是等有新情况。所以退到 wake_prompt 造一轮：让它读自己的职责书、
        # 看当前状态、自己决定要不要说话。否则「根卡能激活角色卡」就只是半句话。
        logger.info("[唤醒] 角色卡无待处理同步，改用主动唤醒 card=%s", target_id)

    # ── 普通卡：优先把已有消息变成一轮 ──
    from . import message_runner as _mr
    started = False
    via = ""
    reason = ""
    try:
        started = await _mr.deliver_now(work_id, target_id)
        if started:
            via = "deliver_now"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[唤醒] deliver_now 失败 card=%s: %s", target_id, exc)

    if not started:
        # 没有未处理消息（或对方正在跑）⇒ 用 why 造一轮
        # 对方在跑时不打断：消息/意图留在下面，它本轮收口会自查
        try:
            from src.storage import session_facade
            busy = bool(session_facade.turn_holder(work_id, target_id))
        except Exception:  # noqa: BLE001
            busy = False
        if busy:
            return {
                "ok": True, "card_id": target_id, "via": "already_running",
                "started": False,
                "reason": "该卡正在跑，不打断；它本轮收口后会自查有无新任务",
            }
        _why_text = why or "上级要求你现在跑一轮，看看有没有需要你处理的事。"
        prompt = (
            "## 上级唤醒" + "\n\n"
            + _why_text + "\n\n"
            + f"这一轮由 `{current_id}` 主动叫起。你可以选择不做事直接结束。"
        )
        try:
            from . import routes as _routes
            task = _mr._spawn_tracked(
                _routes._run_message(work_id, target_id, prompt,
                                     None, append_user=False))
            started = True
            via = "wake_prompt"
        except Exception as exc:  # noqa: BLE001
            return err("internal", f"起回合失败: {exc}")

    return {"ok": True, "card_id": target_id, "via": via,
            "started": started, "reason": reason}


TOOL_REGISTRY["reparent_card"] = ToolDef(
    schema={
        "name": "reparent_card",
        "description": (
            "把自己子树内的一张卡挂到另一张卡下（改父子归属），它的整棵子树随之迁移。"
            "只能在自己子树内调整：被移动的卡、新的父卡都必须在你的子树内；"
            "不能改自己的归属、不能把卡挪出子树、不能成环。"
            "新父卡的类型必须允许有子卡（工具卡/角色卡/worker 卡/规则卡不行）。"
            "只改树形归属，不动卡片内容，也不动通信线。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string",
                            "description": "要移动的卡 id（必须在你子树内）"},
                "new_parent_id": {"type": "string",
                                  "description": "新的父卡 id（可以是你自己；必须在你子树内）"},
            },
            "required": ["card_id", "new_parent_id"],
        },
    },
    handler=_h_reparent_card,
    readonly=False,
)

TOOL_SWITCH["reparent_card"] = policy.SWITCH_EDIT

# [统一唤醒 2026-10-22 · 用户裁定「根卡逻辑上应该能唤醒所有卡」]
# 与 reparent_card 同挂 EDIT：都是**组织动作**，不是执行动作。
# 为什么单独造一个而不是扩 resume_child：resume_child 语义是「恢复自动推进的目标」，
# 有 goal 才有意义；而唤醒要能用在没有 goal、没有 auto、已完成、甚至离树角色卡上。

TOOL_SWITCH["wake_card"] = policy.SWITCH_CREATE_CHILD


# ============================================================
# [伴生评审卡 2026-10-27] 评审工具
#
# 设计要点（全部来自需求与决策原则）：
# · 通过＝工具（P3）：调 approve 即通过，不调即未通过，没有"通过但两处要改"这个第三态
# · 通过只认「没被撤销」：不再有判据版本
# · 现状块由系统渲染（P2）：模型写的现状会说"各方基本认可"，系统渲染的只会是
#   「通过 2/4 · 未过：fn_006」
# · 评审不写别人的卡面（P1）：只出结论，子卡自己改
# ============================================================

async def _render_review_status(work_id: str, review_id: str) -> str:
    """把批次状态**机械渲染**进评审卡现状块。"""
    from ..storage import card_store as _cs
    from ..storage import review_store as _rv

    st = await _rv.status(work_id, review_id)
    if not st.get("batch"):
        # 收口后也要能在卡面上看到**上一批次的正稿**（P1：卡面是权威事实源）。
        state = await _rv.state_of(work_id, review_id)
        verdict = str(state.get("verdict") or "").strip()
        text = "（当前没有进行中的批次）"
        if verdict:
            text += "\n\n" + verdict
    else:
        lines = [
            f"批次 {st['batch']} · 草稿 v{st['draft']}",
            f"通过 {len(st['passed'])}/{len(st['members'])}",
        ]
        lines += [f"● {m} 通过" for m in st["passed"]]
        # [投影分账 2026-11-02] rejected 与未表态**不许归并**——三票否决落账
        # 后全渲染成「未通过」，卡面跟没投过一样（实案：续跑完「还差三人」，
        # 用户看不出票已经记了）。否决是已落的真票，单列。
        rejected = set(st.get("rejected") or [])
        lines += [f"✗ {m} 否决" for m in st.get("rejected") or []]
        lines += [f"○ {m} 未表态" for m in st["pending"] if m not in rejected]
        if st.get("all_passed"):
            lines.append("—— 全员通过 ——")
        text = "\n".join(lines)
    try:
        await _cs.update_card(work_id, review_id, {"status_block": text},
                              "系统渲染：评审批次状态")
    except Exception as exc:  # noqa: BLE001 渲染失败不影响状态本身
        logger.warning("[review] 现状渲染失败 review=%s: %s", review_id, exc)
    return text


def _is_review_card(work_id: str, card_id: str,
                    cards: dict[str, dict[str, Any]] | None) -> bool:
    meta = (cards or {}).get(card_id) or {}
    return str(meta.get("card_type") or "") == "review"


def _venue_default_codes(batch_type: str) -> list[str]:
    """场默认席位集：读职能目录 `_meta.venue_defaults[<场型>]`，读不到回落
    到需求场旧默认三席（与 [评审邀请 2026-10-30] 口径一致）。

    默认名单是**场的属性**，挂在目录 _meta 下按场型取——
    acceptance 场的默认集（["qa","ceo"]）将来加在这里。
    """
    try:
        from . import function_room as _fr  # noqa: PLC0415
        got = (_fr.load().get("venue_defaults") or {}).get(str(batch_type or ""))
        if isinstance(got, list) and got:
            return [str(x) for x in got if str(x)]
    except Exception:  # noqa: BLE001 目录读不到走旧默认
        pass
    return ["ceo", "pm", "cto"]


async def _h_open_review(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """在目标卡的**伴生评审卡**上开一个批次（席位＝场点名＋场默认建议）。"""
    from ..storage import card_store as _cs
    from ..storage import function_store as _fs
    from ..storage import review_store as _rv
    from . import function_room as _fr

    target = str(arguments.get("card_id") or "").strip() or current_id
    focus = str(arguments.get("focus") or "").strip()
    brief = str(arguments.get("brief") or "").strip()
    batch_type = str(arguments.get("type") or "requirements").strip() \
        or "requirements"
    if not brief:
        return err("validation",
                   "brief 必填：写清任务背景／当前状况／要评审的内容"
                   "（原样进评审上下文，不截断）")
    meta = (cards or {}).get(target) or _cs._meta_of(work_id, target) or {}
    if not meta:
        return err("not_found", f"卡片不存在: {target}")
    if str(meta.get("card_type") or "") not in ("sub_project", "topic"):
        return err("validation",
                   f"只有子项目卡／课题卡有伴生评审卡（{target} 是 "
                   f"{meta.get('card_type')}）")
    # [2026-10-28 用户令「必须经过子卡发起评审」] 草稿谁都能写（自己／父卡／
    # 根卡），但开评审只能这张卡自己——代开等于替它答应。想让别的卡开评，
    # 用 direct 通知它来开。
    if target != current_id:
        return err("denied",
                   "评审只能由这张卡自己发起（card_id 必须是本卡）——"
                   "代开等于替它答应；想让它开评，用 direct 通知它。")
    # [2026-10-28] 没有草稿就没有可评审的内容。
    if not str(meta.get("requirements_draft") or "").strip():
        return err("validation",
                   "本卡的需求文档草稿是空的——先用 edit_requirements 写草稿，"
                   "再开评审。")
    rid = await _rv.ensure_review_card(work_id, target,
                                       str(meta.get("title") or target))
    if not rid:
        return err("internal", "伴生评审卡不可用（建卡失败）")

    # [venue 席位泛化 2026-10-31] 席位＝人格＋mandate，不再硬编码：
    # 发起方点名（members 参数：fn id 或目录 code 皆可）＞目录
    # `_meta.venue_defaults[type]`（场默认建议）＞旧默认三席。
    # 「默认名单是场的属性」——验收场的默认集跟需求场不一样，
    # 由各职能自报家门是错的。
    invited = list(arguments.get("members") or [])
    if not invited:
        invited = _venue_default_codes(batch_type)
    members: list[str] = []
    try:
        profiles = _fs.list_profiles(work_id)
        wanted = {str(x) for x in invited if str(x)}
        for p in profiles:
            fid = str(p.get("function_id") or "")
            if not fid or str((p.get("duty") or {}).get("state") or "active") != "active":
                continue
            # 点名可以写 fn id 也可以写目录 code——两个命名空间都认。
            if fid not in wanted and _fr.catalog_code_of(p) not in wanted:
                continue
            members.append(fid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 读取指定评审职能失败: %s", exc)
    if not members:
        return err("validation",
                   f"没有在职的评审席可用（点名：{invited}）")
    # [评审席资格 2026-11-01 · 用户令「守门员评审席没有场景」] 席位资格＝
    # 有评审职责书（条目 charters.review 或 review_charters.json 命中）。
    # 没有的职能只能穿日常帽子进场——没有【评审范围】边界，点名也剔；
    # 哪天写了职责书自动解禁（数据驱动，不硬编码名单）。
    # 只在目录有条目时查：目录关着（无条目）就没有「谁是观察岗」可甄别，
    # 闸随目录一起失效——资格数据整个出自目录体制，目录缺席无从判起。
    try:
        if _fr.entries():
            code_of = {str(p.get("function_id") or ""): _fr.catalog_code_of(p)
                       for p in profiles}
            members = [f for f in members
                       if _fr.venue_charter_for(code_of.get(f, ""), "review")]
    except Exception as exc:  # noqa: BLE001 核查失败放行（不误伤）
        logger.warning("[review] 职责书核查失败（放行）: %s", exc)
    if not members:
        return err("validation",
                   "点名的席位都没有评审职责书——没有职责书的职能不进评审席")
    # [职能底座 2026-10-31] 「作者不评自己」：在本目标卡上有过执笔/作者差事
    # 的职能只能当答辩人，不能当评委——点名也不例外（差事账直接判，
    # 不另立状态）。台账查不到 ⇒ 不剔除（不误伤）。
    try:
        members = [f for f in members
                   if not _fs.has_prior(work_id, f, target,
                                        venue_kinds=("deploy", "author"))]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 作者排除核查失败（放行）: %s", exc)
    if not members:
        return err("validation",
                   "点名/默认席位全是本稿作者——作者只能答辩，不能当评委")

    st = await _rv.open_batch(work_id, rid, focus, members=members,
                              target=target, brief=brief,
                              batch_type=batch_type)
    # [需求文档生命周期 2026-10-28] 开评审 ⇒ 目标卡状态到 in_review；
    # 评审收口（promote_draft 写正稿）时回 approved。
    try:
        await _cs.update_card(work_id, target,
                              {"requirements_state": "in_review"},
                              f"评审批次 {st['batch']} 开启")
    except Exception as exc:  # 状态位落不上不阻断已开的批次
        logger.warning("[review] 目标卡 requirements_state 落不上 %s: %s",
                       target, exc)
    # [职能底座 2026-10-31] 每席位一条差事：部署账与评审生命周期绑死。
    # exit=verdict：本批次投出一张立着的票（revoked=0）即算办完；
    # revoke 撤票 ⇒ 惰性核对自动让它回到在派。单席建账失败不卡开门。
    for _f in members:
        try:
            await _fs.create_deployment(
                work_id, _f, venue_kind="review", venue_ref=rid,
                venue_label=f"{batch_type} 评审 · {target} 批次 {st['batch']}",
                mandate=_fr.render_mandate(
                    "review", function=_f, review_card_id=rid,
                    batch=str(st["batch"]), target=target)
                or f"本批次 {st['batch']} 评审席（{target}）",
                delegator=current_id, batch_id=str(st["batch"]),
                target_card=target, exit_kind="verdict",
                tools=_fr.grant_for(work_id, _f, "review"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[差事] 评审席位建账失败 seat=%s: %s", _f, exc)
    await _render_review_status(work_id, rid)
    try:
        event_stream.emit(work_id, rid, "review_batch_opened",
                          {"batch": st["batch"], "target": target,
                           "members": members, "focus": focus[:200],
                           "type": batch_type})
    except Exception:  # noqa: BLE001 展示层零阻断
        pass
    # 召集后**后台**点第一席（一步）：其后流程存续全交 harness 的
    # review_round_done → continue 接力，不再起进程内循环。
    # 测试环境由 ECO_NO_AUTO_REVIEW 关掉（run_once 内判，conftest 已设）。
    try:
        from . import review_runner as _rr
        _rr.spawn_round(work_id, rid)
    except Exception as exc:  # noqa: BLE001 起不来不影响批次已开
        logger.warning("[review] 评审回合起不来 review=%s: %s", rid, exc)
    return ok_({"review_card_id": rid, "batch": st["batch"],
                "target": target, "members": members,
                "note": "批次已开：成员各自按自己职能评审后调 approve"})


# ============================================================
# [2026-10-30 重建] 精准编辑的四件套（原文件损坏后按测试契约重写）
#
# 这四件都是「一处一处改」：定位二选一（old_string 按内容 / lines 按行号），
# 命中唯一才写，命中 0 或多次一律拒且**零改动**。
# `_h_edit_status` / `_h_edit_requirements` 是 `_h_edit_card` 的薄壳：
# 块名锁死、需求文档写到**草稿**列，引擎不另写一套。
# ============================================================


def _is_blank_text(text: str) -> bool:
    """这一块是不是**还空着**（没写过东西）。

    空 = 只有空白，或只有建卡时那段骨架占位（整段被一对圆括号包住的单行提示，
    如「（当前状况快照：做到哪、什么状态…）」）。占位符当空，否则第一笔
    内容永远写不进去——它要在模板里几十处相同字样里找一个唯一位置。
    """
    t = (text or "").strip()
    if not t:
        return True
    return t.startswith("（") and t.endswith("）") and "\n" not in t


class _PatchErr(Exception):
    """定位失败：带信封用的 code 与给模型看的原话。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _patch_locate(text: str, old: Any, new: str) -> tuple[str, int, str]:
    """按内容定位并替换一处，返回 (新全文, 命中的行号, 命中方式)。

    命中方式：``exact`` 逐字命中／``normalized`` 只差标点编码／
    ``empty_insert`` 空块首次写入。0 命中、多次命中一律抛 `_PatchErr`。
    """
    if not isinstance(old, str):
        raise _PatchErr("validation", "old_string 必填（要替换的那段原文；空块首次写入给空串）")
    if old == "":
        if not _is_blank_text(text):
            raise _PatchErr("validation",
                            "old_string 给的是空串，但这一块里已经有内容——空串只用于"
                            "**空块首次写入**。要改已有内容：old_string 填要替换的那段原文。")
        return new, 1, "empty_insert"
    hits = text.count(old)
    matched = "exact"
    idx = text.find(old)
    if hits == 0:
        n_text, n_old = _norm_for_match(text), _norm_for_match(old)
        hits = n_text.count(n_old) if n_old else 0
        matched = "normalized" if hits else "none"
        idx = n_text.find(n_old) if hits else -1
    if hits == 0:
        # [2026-11-02 · card_021 实案] 只叫模型「read_card 核对」＝叫它整块
        # 重读——8 万 token 文档的重读循环就是这么死的。回近似原文窗＋行号，
        # 它拿逐字原文改 old_string，不必重读。
        hint = file_tools.format_not_found_hint(text, old)
        raise _PatchErr("not_found",
                        f"这一块里找不到 old_string，**内容未改动**。{hint}")
    if hits > 1:
        spots = file_tools.match_positions(text, old)
        listing = "；".join(f"第 {p['line']} 行：{p['head']}" for p in spots)
        raise _PatchErr("ambiguous",
                        f"old_string 在这一块里出现 {hits} 次（{listing}"
                        + (f"等共 {hits} 处" if hits > len(spots) else "") +
                        f"），无法确定改哪一处，**内容未改动**。"
                        f"请带上更多上下文让它唯一。")
    at_line = text[:idx].count("\n") + 1
    return text[:idx] + new + text[idx + len(old):], at_line, matched


async def _h_edit_status(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**精准编辑本卡（或子树内某卡）的「现状」块**——只动这一处。

    现状是**快照**：同一件事只留一条最新读数，作废的删掉，不往后堆段落。
    历史由系统自动留版（每卡最近 20 版），块本身只放现在成立的。
    """
    args = dict(arguments)
    args["card_id"] = str(arguments.get("card_id") or current_id).strip() or current_id
    args["block"] = "现状"
    return await _h_edit_card(work_id, current_id, args, cards,
                              forced_block="现状")


async def _h_edit_requirements(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**精准编辑需求文档的草稿**——写的是 `requirements_draft` 那一列。

    正稿不在这里改：正稿只能由评审全员通过后定稿产生。
    草稿空着时 old_string 给空串＝首次写入；有草稿就以草稿为底，
    没有草稿则以正稿为底（冲突时从现行需求上改）。
    """
    args = dict(arguments)
    args["card_id"] = str(arguments.get("card_id") or current_id).strip() or current_id
    args["block"] = "需求文档"
    out = await _h_edit_card(work_id, current_id, args, cards,
                             forced_block="需求文档",
                             field_override="requirements_draft")
    if isinstance(out, dict) and out.get("ok"):
        # 写的是**草稿列**，_landed_payload 比的是正稿 ⇒ 这里补上草稿全文
        card = await card_store.read_card(work_id, args["card_id"])
        draft = str((card.get("meta") or {}).get("requirements_draft") or "")
        out.setdefault("blocks", {})["需求文档"] = draft
        # [2026-11-01 · 用户令「草稿不进上下文，只有正稿常驻」] 草稿的唯一
        # 在场形态就是这个回执：lines/insert_after 定位用的行号按草稿标，
        # 与 _h_edit_card 的行模式（split("\n")）同口径。
        out["需求文档·草稿（行号）"] = context_regions._number_lines(draft)
    return out


async def _h_write_requirements_final(work_id: str, current_id: str,
                                      arguments: dict[str, Any],
                                      cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**写正稿**（只有剧本根卡能调）。

    根卡没有评审：它的需求由用户/总裁直接确定，所以由它自己写正稿。
    子卡的正稿只能由评审产生（全员通过后调 `promote_draft`），这里一律拒。
    写完清空草稿、状态置 `approved`。
    """
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id)
    except Exception as exc:  # noqa: BLE001 读不到根卡就不放行
        logger.warning("[edit] 读根卡失败 work=%s: %s", work_id, exc)
    target_id = str(arguments.get("card_id") or current_id).strip() or current_id
    if target_id != root_id:
        return err("denied", "写正稿只有剧本根卡能做；子卡的正稿由评审产生")
    guard = _guard_edit(work_id, current_id, target_id, cards)
    if guard:
        return guard
    card = await card_store.read_card(work_id, target_id)
    cur = str((card.get("body") or {}).get("requirements") or "")
    if _is_blank_text(cur):
        cur = ""
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    lines_raw = arguments.get("lines")
    if new is None:
        return err("validation", "new_string 必填（正稿内容）")
    if lines_raw not in (None, "") and isinstance(old, str) and old:
        return err("validation", "old_string 与 lines 只能给一个")
    if lines_raw not in (None, ""):
        rows = cur.split("\n") if cur else []
        spec = str(lines_raw).strip()
        try:
            if "-" in spec:
                a, b = spec.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = end = int(spec)
        except (TypeError, ValueError):
            return err("validation", f"lines 只支持 \"12\" 或 \"12-15\"，收到：{spec}")
        if start < 1 or end < start or end > max(len(rows), 1):
            return err("validation",
                       f"行号越界：当前共 {len(rows)} 行，lines 须在 1～{len(rows)}；收到 {spec}")
        rows[start - 1:end] = str(new).split("\n") if str(new) else []
        new_text, at_line, matched = "\n".join(rows), start, "line"
    else:
        try:
            new_text, at_line, matched = _patch_locate(cur, old, str(new))
        except _PatchErr as exc:
            return err(exc.code, exc.message)
    result = await card_store.update_card(
        work_id, target_id,
        {"requirements": new_text, "requirements_draft": "",
         "requirements_state": "approved"},
        "写正稿（根卡需求确定）")
    if isinstance(result, dict) and result.get("error"):
        return result
    payload = await _landed_payload(work_id, target_id, dict(card.get("body") or {}))
    return {"card_id": target_id, "ok": True, "block": "需求文档",
            "mode": "lines" if lines_raw not in (None, "") else "content",
            "matched": matched, "at_line": at_line,
            "requirements_state": "approved", **payload}


async def _h_edit_shadow(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**精准编辑影子的一节**（进度／待协作事项／产物清单／迭代历史）。

    只动指定那一节，其余节一字不碰。节空着时 old_string 给空串＝首次写入。
    影子是本卡私有的工作记录，别的卡读不到笔记节。
    """
    target_id = str(arguments.get("card_id") or current_id).strip() or current_id
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    guard = _guard_edit(work_id, current_id, target_id, cards)
    if guard:
        return guard
    section = str(arguments.get("section") or "").strip().lstrip("#").strip()
    if not section:
        return err("validation", "section 必填：进度／待协作事项／产物清单／迭代历史")
    head = "## " + section
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    if new is None:
        return err("validation", "new_string 必填；要删掉这一处就给空串")
    cur = await shadow_store.read_section(work_id, target_id, head)
    try:
        new_text, at_line, matched = _patch_locate(str(cur or ""), old, str(new))
    except _PatchErr as exc:
        return err(exc.code, exc.message)
    if new_text == str(cur or ""):
        return err("validation", "替换后内容没有变化（old 与 new 相同？）")
    await shadow_store.write_section(work_id, target_id, head, new_text)
    return {"card_id": target_id, "ok": True, "section": section,
            "at_line": at_line, "matched": matched,
            "value": new_text[:2000]}


async def _h_edit_note(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**精准编辑本卡的笔记**（影子第五区，常驻本卡上下文、别的卡读不到）。

    笔记空着时 old_string 给空串＝首次写入；已有内容时给空串会被拒
    （整块覆盖通道有意取消）。
    """
    target_id = str(arguments.get("card_id") or current_id).strip() or current_id
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    guard = _guard_edit(work_id, current_id, target_id, cards)
    if guard:
        return guard
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    if new is None:
        return err("validation", "new_string 必填；要删掉这一处就给空串")
    cur = await shadow_store.read_note(work_id, target_id)
    try:
        new_text, at_line, matched = _patch_locate(str(cur or ""), old, str(new))
    except _PatchErr as exc:
        return err(exc.code, exc.message)
    if new_text == str(cur or ""):
        return err("validation", "替换后内容没有变化（old 与 new 相同？）")
    await shadow_store.write_note(work_id, target_id, new_text)
    return {"card_id": target_id, "ok": True, "at_line": at_line,
            "matched": matched, "note": new_text[:2000]}


async def _h_promote_draft(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**写正稿**：把自己的需求草稿转成正式稿。

    [2026-10-29 用户口径]「最后也一定是调工具写正稿为结束」——转正稿不再由系统
    代劳，而是子卡在评审通过后自己调这个工具完成。系统只负责在「全过了但没写」
    时把它叫醒（harness 的 continue）。

    三条硬约束：
      · 只能写**自己**的正稿（不能替别人定稿）
      · 评审必须**全员通过**（没过就写＝绕过评审）
      · 草稿不能为空
    """
    from ..storage import review_store as _rv

    target = str(arguments.get("card_id") or current_id).strip() or current_id
    if target != current_id:
        return err("denied", f"只能写自己的正稿（你是 {current_id}）")
    try:
        rid = await _rv.review_card_of(work_id, target)
        if rid:
            st = await _rv.status(work_id, rid)
            if not st.get("all_passed"):
                return err("validation",
                           f"评审还没全员通过（未过：{st.get('pending')}），不能转正稿")
            # [venue 版本闸 · 已签收窄口径 2026-10-31]
            # 「全员批准的那个版本，才配叫正稿」：收口瞬间比对每票的 draft_ver
            # 与当前草稿——有人批的是旧版 ⇒ 不收口，发 review_draft_drift，
            # 只对 stale 席位补「只评增量」确认轮（不重投全文、不打扰已批新版者）。
            stale = [m for m in (st.get("stale") or [])]
            if stale:
                # 机械状态挂上：drift_pending=下一步该叫 stale 席补确认；
                # promote_asked 清掉=确认补完后允许重新派定稿。
                try:
                    await _rv.set_drift_pending(work_id, rid, stale)
                except Exception:  # noqa: BLE001 状态写不上不挡拒收
                    pass
                try:
                    event_stream.emit(work_id, rid, "review_draft_drift", {
                        "batch": st.get("batch"), "target": target,
                        "draft": int(st.get("draft") or 1),
                        "stale": stale,
                        "approvals": {m: int((st.get("votes") or {})
                                             .get(m, {}).get("draft_ver") or 0)
                                      for m in stale},
                    })
                except Exception:  # noqa: BLE001
                    pass
                return err("validation",
                           f"草稿在全员通过后又改过（现第 {st.get('draft')} 版；"
                           f"{stale} 批的是旧版）。已请相关席位确认增量——"
                           f"等它补投完成后，再调一次 promote_draft。")
    except Exception as exc:  # noqa: BLE001 判不出评审状态就不放行
        return err("validation", f"读评审状态失败：{exc}")
    r = await _promote_requirements(work_id, target)
    if not r.get("promoted"):
        return err("validation", str(r.get("reason") or "转正稿失败"))
    # [2026-10-29 用户令「评审结束，你得回到对话里，将结果回喂给对话」]
    # 收口＝正稿写完这一刻（不是全员通过那一刻）。落在子卡自己的会话里。
    try:
        _verdict = ""
        try:
            if rid:
                _fin = await _rv.finalize(work_id, rid)
                _verdict = str((_fin or {}).get("verdict") or "")
                if _fin:
                    event_stream.emit(work_id, rid, "review_batch_closed",
                                      {"batch": _fin.get("closed_batch"),
                                       "verdict": _verdict[:400]})
        except Exception:  # noqa: BLE001
            pass
        _lines = [
            f"〔评审通过 · 正稿已写〕",
            "",
            "全员通过，草稿已转成**正稿**（requirements_state=approved），草稿已清空。",
        ]
        if _verdict:
            _lines += ["", "评审结论：", _verdict[:1200]]
        await session_facade.append(work_id, target, {
            "actor": "system", "content": "\n".join(_lines)})
        # [2026-10-29 用户令「评审结束主动唤醒通知卡片」] 光写进会话不够——
        # 它不会自己醒。这里主动叫它一轮，让它知道评审过了、可以继续往下干。
        # deliver_now 自带忙判定：它正在跑就不打断，等它收口时的自查续轮接上。
        try:
            from ..api import message_runner as _mr
            await _mr.deliver_now(
                work_id, target,
                text="〔评审通过 · 正稿已写〕评审结束，正稿已定稿。请查看需求文档正稿，继续下一步工作。")
        except Exception as exc:  # noqa: BLE001 叫不醒不影响定稿本身
            logger.warning("[review] 收口唤醒失败 card=%s: %s", target, exc)
    except Exception as exc:  # noqa: BLE001 回喂失败不影响定稿
        logger.warning("[review] 收口回喂失败 card=%s: %s", target, exc)
    return {"ok": True, "data": {"card_id": target, "chars": int(r.get("chars") or 0),
                                 "requirements_state": "approved"}}


async def _promote_requirements(work_id: str, target: str) -> dict[str, Any]:
    """**评审全员通过 ⇒ 草稿转正稿**（[2026-10-28] 唯一能写正稿的动作）。

    正稿（`requirements`）没有任何工具能直接写：模型侧只有 `edit_requirements`
    写草稿。所以「定稿后不能再改」是结构上不可能——要改正稿，只能改草稿再走一遍评审。

    返回：{"promoted": bool, "chars": n} / {"promoted": False, "reason": ...}
    """
    try:
        card = await card_store.read_card(work_id, target)
        meta = card.get("meta") or {}
        draft = str(meta.get("requirements_draft") or "").strip()
        if not draft:
            return {"promoted": False, "reason": "草稿为空，没有可转正的内容"}
        r = await card_store.update_card(
            work_id, target,
            {"requirements": draft, "requirements_draft": "",
             "requirements_state": "approved"},
            "需求评审全员通过：草稿转正稿")
        if isinstance(r, dict) and r.get("error"):
            return {"promoted": False, "reason": str(r["error"])}
        return {"promoted": True, "chars": len(draft)}
    except Exception as exc:  # noqa: BLE001 转正失败不阻断评审收口本身
        logger.error("[review] 草稿转正稿失败 card=%s: %s", target, exc)
        return {"promoted": False, "reason": str(exc)}


async def _vote_gate(work_id: str, rid: str, current_id: str) -> dict[str, Any] | None:
    """投票闸（机械，对称 approve/reject）：批次在跑 ∧ 是成员。

    [2026-10-31 用户令]「我不限制它是不是先发言还是先投票，从来不需要限制」
    ——成员在本批次内随时可投、可改投（后票覆盖前票）；先不先发言是
    席位自己的分寸，机制不闸。
    """
    from ..storage import review_store as _rv

    st = await _rv.state_of(work_id, rid)
    if not str(st.get("batch") or ""):
        return err("validation", "当前没有进行中的批次")
    members = await _rv.members_of(work_id, rid)
    if current_id not in members:
        return err("denied",
                   f"{current_id} 不在这个评审的成员里（成员：{members}）")
    return None


async def _h_reject(work_id: str, current_id: str,
                     arguments: dict[str, Any],
                     cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """不通过＝只投票，不带理由（理由写在发言里）。

    [venue 真票 2026-10-31] 这是**真票**：与 approve 同一个 upsert 键，
    先通过又不通过＝不通过（后票覆盖前票）。[用户令] 不设机械僵局检测——
    上下文给到位，席位与正卡自然收敛；多次否决是正常迭代，不是结局。
    """
    from ..storage import review_store as _rv

    rid = str(arguments.get("review_card_id") or "").strip()
    if not rid:
        return err("validation", "review_card_id 不能为空")
    if not card_store.card_exists(work_id, rid):
        return err("not_found", f"评审卡不存在: {rid}")
    gate = await _vote_gate(work_id, rid, current_id)
    if gate is not None:
        return gate
    r = await _rv.reject(work_id, rid, current_id)
    if not r.get("ok"):
        return err("validation", str(r.get("error") or "reject 失败"))
    await _render_review_status(work_id, rid)
    return ok_({"review_card_id": rid, "role": current_id,
                "verdict": "reject", "batch": r.get("batch"),
                "draft": r.get("draft")})


async def _h_approve(work_id: str, current_id: str,
                     arguments: dict[str, Any],
                     cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**通过**。调了就是通过，不调就是未通过——没有第三态（P3）。"""
    from ..storage import review_store as _rv

    rid = str(arguments.get("review_card_id") or "").strip()
    if not rid:
        return err("validation", "review_card_id 不能为空")
    if not card_store.card_exists(work_id, rid):
        return err("not_found", f"评审卡不存在: {rid}")
    # [venue 投票闸] 批次在跑 ∧ 是成员——与 reject 同一道闸（投票零限制，
    # 先不先发言是席位自己的分寸，机制不查）。
    gate = await _vote_gate(work_id, rid, current_id)
    if gate is not None:
        return gate
    r = await _rv.approve(work_id, rid, current_id)
    if not r.get("ok"):
        return err("validation", str(r.get("error") or "approve 失败"))
    st = await _rv.status(work_id, rid)
    fin: dict[str, Any] = {}
    if st.get("all_passed"):
        # [2026-10-29 用户口径「最后也一定是调工具写正稿为结束」]
        # **全员通过时不收口**：批次要留着，等子卡调 `promote_draft` 写完正稿
        # 才 finalize（收口＝正稿写完，不是票凑齐）。
        # 提前 finalize 会清掉 batch ⇒ `run_once` 看到「没批次」就再也不处理
        # ⇒ 正稿永远写不出来，卡死在 in_review（实弹：card_038 / card_071）。
        fin = {}
        # [2026-10-29 用户口径「最后也一定是调工具写正稿为结束」]
        # 全员通过**不再由系统代劳转正稿**：只发事件，由 harness 把子卡叫醒，
        # 它自己调 `promote_draft` 完成定稿。系统代劳 = 模型的工作没有以工具收尾。
        try:
            event_stream.emit(work_id, rid, "review_all_passed",
                              {"batch": st["batch"], "members": st["members"],
                               "target": str((await _rv.state_of(work_id, rid)).get("target") or ""),
                               "need_promote": True})
            if fin:
                event_stream.emit(work_id, rid, "review_batch_closed",
                                  {"batch": fin.get("closed_batch"),
                                   "verdict": str(fin.get("verdict") or "")[:400]})
        except Exception:  # noqa: BLE001
            pass
        # [2026-10-29] 全员通过只是「可以写正稿了」，收口要等子卡调 promote_draft。
        # `review_all_passed` 事件（上面那条）已带 need_promote，harness 的 continue
        # 规则据此把子卡叫醒（情况②）。这里不再重复发。
    await _render_review_status(work_id, rid)
    return ok_({"review_card_id": rid, "role": current_id,
                "batch": r.get("batch"), "draft": r.get("draft"),
                "all_passed": bool(st.get("all_passed")),
                "closed": bool(fin),
                "pending": st.get("pending") or [],
                "draft_changed": bool(st.get("draft_changed"))})


async def _h_revoke_approve(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """撤销某角色的通过（甲方／总裁纠正用）。"""
    from ..storage import review_store as _rv

    rid = str(arguments.get("review_card_id") or "").strip()
    role = str(arguments.get("role_id") or "").strip()
    if not rid or not role:
        return err("validation", "review_card_id 与 role_id 都不能为空")
    r = await _rv.revoke(work_id, rid, role)
    if not r.get("ok"):
        return err("validation", str(r.get("error") or "撤销失败"))
    await _render_review_status(work_id, rid)
    # 被撤销的角色重新待评 ⇒ 起一轮（只跑未通过的，所以只会叫它自己）
    try:
        from . import review_runner as _rr
        await _rr.rerun_if_pending(work_id, rid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[review] 撤销后重评起不来 review=%s: %s", rid, exc)
    return ok_({"review_card_id": rid, "role": role, "revoked": True})


async def _h_decide(work_id: str, current_id: str,
                    arguments: dict[str, Any],
                    cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """裁决：`pass`＝一票通过（全员记为通过）；`close`＝解散本批次。"""
    from ..storage import review_store as _rv

    rid = str(arguments.get("review_card_id") or "").strip()
    action = str(arguments.get("action") or "").strip()
    if not rid or action not in ("pass", "close"):
        return err("validation",
                   "review_card_id 必填，action 只允许 pass/close")
    if action == "pass":
        members = await _rv.members_of(work_id, rid)
        for m in members:
            await _rv.approve(work_id, rid, m)
        await _render_review_status(work_id, rid)
        return ok_({"review_card_id": rid, "action": "pass",
                    "members": members, "all_passed": True})
    # close：批次作废，不留正稿
    st = await _rv.state_of(work_id, rid)

    def _fn(conn: Any) -> None:
        import json as _j
        st2 = dict(st)
        st2["batch"] = ""
        st2["members"] = []             # 本批邀请到此结束，不留下成员关系
        st2["closed_at"] = ""
        st2["closed_batch"] = str(st.get("batch") or "")
        conn.execute("UPDATE cards SET review_state=? WHERE card_id=?",
                     (_j.dumps(st2, ensure_ascii=False), rid))

    await card_db.run_write(work_id, _fn)
    await _render_review_status(work_id, rid)
    return ok_({"review_card_id": rid, "action": "close"})


TOOL_REGISTRY["open_review"] = ToolDef(
    schema={
        "name": "open_review",
        "description": (
            "**给本卡自己**开一个需求评审批次（评审发生在它的伴生评审卡上，"
            "每张子卡自带一张，你不用建）。开批次后在职职能各自按自己的职能评审，"
            "调 approve 表态；**全员通过** ⇒ 系统把草稿转成正稿。\n"
            "· **只能给本卡开**：草稿谁都能写（自己／父卡／根卡），但评审是这张卡对外的"
            "承诺，只能它自己发起——代开等于替它答应。\n"
            "· 前提：本卡的需求文档草稿非空（没有草稿就没有可评审的内容）\n"
            "· 批次开在评审卡上，不影响子卡自己的对话"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string",
                            "description": "必须是**本卡自己**的 id（缺省＝当前卡）"},
                "brief": {
                    "type": "string",
                    "description": (
                        "【必填】发起方填写的三样：任务背景／当前状况／要评审的内容。"
                        "会**原样**进入评审上下文（不截断），职能据此判断，"
                        "所以写清、写实——它替代不了，也不该靠职能去猜。"
                    ),
                },
                "focus": {"type": "string",
                          "description": "（可选）一句话焦点，只作标题用；正式内容写 brief"},
                "members": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "（可选）点名评审席：fn id 或目录 code 皆可"
                                   "（如 [\"ceo\",\"pm\",\"qa\"]）。"
                                   "缺省＝场默认建议名单（按场型挂在职能目录里）",
                },
                "type": {"type": "string",
                         "description": "（可选）评审场型，默认 requirements"
                                        "（需求评审）；验收场等将来按场型挂默认席位"},
            },
            "required": ["card_id", "brief"],
        },
    },
    handler=_h_open_review,
    readonly=False,
)
TOOL_SWITCH["open_review"] = policy.SWITCH_CREATE_CHILD

TOOL_REGISTRY["approve"] = ToolDef(
    schema={
        "name": "approve",
        "description": (
            "**通过**这个评审批次。调了就是通过，不调就是未通过——没有『通过但要改两处』"
            "这种中间态。通过之后本批次你不再参与讨论。\n"
            "· 你的通过在本批次内一直有效，直到被撤销\n"
            "· 你必须是这个评审的成员，否则会被拒"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "review_card_id": {"type": "string",
                                   "description": "评审卡 id（评审发生的那张卡）"},
            },
            "required": ["review_card_id"],
        },
    },
    handler=_h_approve,
    readonly=False,
)
TOOL_SWITCH["approve"] = policy.SWITCH_READ

TOOL_REGISTRY["promote_draft"] = ToolDef(
    schema={
        "name": "promote_draft",
        "description": (
            "**写正稿**：把自己的需求草稿转成正式稿（草稿清空、状态变 approved）。\n"
            "· 评审全员通过之后才能调；没过就调会被拒\n"
            "· 只能写自己的正稿\n"
            "· 这是定稿动作：转完之后要改正稿，只能改草稿再走一遍评审"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string",
                            "description": "要写正稿的卡 id（只能是自己）"},
            },
            "required": [],
        },
    },
    handler=_h_promote_draft,
    readonly=False,
)
TOOL_SWITCH["promote_draft"] = policy.SWITCH_EDIT

async def _h_submit_revision(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**交回本轮改稿**（被评卡的终态工具）：调了才算这轮回应结束。

    [venue 2026-10-31] 这是「和 approve/reject 同一种做法」的出口——
    正卡在自己的正常回合里改完（或决定不改），调它交回评审。
    机械动作只有两件：`responded_seq` 加一（否决席位据此知道有新回应可评）、
    `review_target_responded` 事件（harness 接力回评审卡的依据）。
    """
    from ..storage import review_store as _rv

    rid = str(arguments.get("review_card_id") or "").strip()
    if not rid:
        rid = await _rv.review_card_of(work_id, current_id)
    if not rid:
        return err("validation", "本卡没有进行中的评审批次")
    st = await _rv.state_of(work_id, rid)
    if not str(st.get("batch") or ""):
        return err("validation", "当前没有进行中的批次")
    if current_id != str(st.get("target") or ""):
        return err("denied",
                   f"只有被评卡（{st.get('target')}）能交回本轮改稿，你是 {current_id}")
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        return err("validation",
                   "summary 必填：改了哪几处（一条一行，写能核对的事实）；"
                   "决定不改也要写明理由")
    seq = await _rv.mark_responded(work_id, rid)
    try:
        event_stream.emit(work_id, rid, "review_target_responded", {
            "batch": st.get("batch"), "target": current_id,
            "summary": summary[:2000], "responded_seq": seq,
            "draft": int(st.get("draft") or 1),
        })
    except Exception:  # noqa: BLE001
        pass
    return ok_({"review_card_id": rid, "responded_seq": seq})


TOOL_REGISTRY["submit_revision"] = ToolDef(
    schema={
        "name": "submit_revision",
        "description": (
            "**交回本轮改稿/回应**：评审把意见交给你之后，这一轮该改的都改完了"
            "（或你决定不改并说了理由），就调它交回评审。\n"
            "· 调了才算这一轮结束；只回一段文字不算\n"
            "· 只有被评卡能调（评审成员/别的卡调会被拒）\n"
            "· 下一轮要不要再改，由评审重新评了再说"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "改了哪几处（一条一行，写能核对的事实）；"
                                           "决定不改也要写明理由"},
                "review_card_id": {"type": "string",
                                   "description": "评审卡 id（缺省＝本卡的伴生评审卡）"},
            },
            "required": ["summary"],
        },
    },
    handler=_h_submit_revision,
    readonly=False,
    doc=ToolDoc(
        group="评审",
        scene="评审把意见交给你之后，这一轮该改的都改完了（或你决定不改"
              "并写明理由）——**本轮回应的收口**：调了评审才算收到你的回应，"
              "否决你的席位才会被叫回来复评。只发消息不调它，评审会一直等你。",
        usage="summary 必填：改了哪几处，一条一行写能核对的事实；"
              "决定不改也写明理由。只有被评卡能调（席位/别的卡调会被拒）。",
        effect=ToolEffect(),
    ),
)
TOOL_SWITCH["submit_revision"] = policy.SWITCH_EDIT


TOOL_REGISTRY["reject"] = ToolDef(
    schema={
        "name": "reject",
        # [2026-10-31 用户令] 工具只是**投票**：只说"不通过"，不带理由。
        # 理由写在你的发言里——对话界面和后续成员要看的是你的话，不是参数。
        "description": (
            "**不通过**这个评审批次。\n"
            "· 这里只投票，不带理由：为什么不通过、要改成什么，写在你自己的发言里\n"
            "· 你必须是这个评审的成员，否则会被拒"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "review_card_id": {"type": "string", "description": "评审卡 id（评审发生的那张卡）"},
            },
            "required": ["review_card_id"],
        },
    },
    handler=_h_reject,
    readonly=False,
)
TOOL_SWITCH["reject"] = policy.SWITCH_READ


TOOL_REGISTRY["revoke_approve"] = ToolDef(
    schema={
        "name": "revoke_approve",
        "description": (
            "撤销某个角色在本批次的通过（纠正用）。撤销后该角色退回未通过，需重评。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "review_card_id": {"type": "string", "description": "评审卡 id"},
                "role_id": {"type": "string",
                            "description": "要撤销谁（职能卡 id，如 fn_004）"},
            },
            "required": ["review_card_id", "role_id"],
        },
    },
    handler=_h_revoke_approve,
    readonly=False,
)
TOOL_SWITCH["revoke_approve"] = policy.SWITCH_EDIT

TOOL_REGISTRY["decide"] = ToolDef(
    schema={
        "name": "decide",
        "description": (
            "裁决这个评审批次。`pass`＝一票通过（全员记为通过，用于某个角色卡死时）；"
            "`close`＝解散本批次（不留正稿）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "review_card_id": {"type": "string", "description": "评审卡 id"},
                "action": {"type": "string", "enum": ["pass", "close"],
                           "description": "pass＝一票通过；close＝解散批次"},
            },
            "required": ["review_card_id", "action"],
        },
    },
    handler=_h_decide,
    readonly=False,
)
TOOL_SWITCH["decide"] = policy.SWITCH_EDIT


def _register_file_tools() -> None:
    """把 file_tools 四件登记进同一注册表（模块加载时调用一次）。

    schema 走**延迟求值**（lambda → file_tools.build_file_tool_schemas 现取）：
    那四件的 description 是 f-string over ALLOWED_ROOTS，而 conftest 会
    monkeypatch 该常量——若在此刻取件填入，会把真实仓库路径冻结进 schema
    （实测该描述对 ALLOWED_ROOTS call-time 敏感）。延迟形态保持工具面逐字
    不变，且与改前「每次 build 现取」的行为完全一致。

    readonly 标志：前三件只读、write_file 写（与改前手工frozenset 逐字一致）。
    """
    handlers = {
        "list_files": (_h_list_files, True),
        "read_file": (_h_read_file, True),
        "search_files": (_h_search_files, True),
        "write_file": (_h_write_file, False),
    }
    # 取值时机：**每次派生现调** build_file_tool_schemas()（下面 lambda 内联求值）。
    # 改前这里在模块加载期先建好 ``schemas`` dict 再让 lambda 回引它——那只做到了
    # 「延迟调用」，**内容仍被冻结**在 import 那一刻的 BASE_DIR / ALLOWED_ROOTS。
    # 实测：把 file_tools.BASE_DIR 换成哨兵根后，经 TOOL_REGISTRY 取件仍是
    # D:\AI_station ⇒ 与本函数 docstring 宣称的「call-time 现取」不符。
    # 语义无差：工具面顺序、件名、参数、required、readonly 全部逐字不变。
    for name, (handler, readonly) in handlers.items():
        TOOL_REGISTRY[name] = ToolDef(
            schema=(lambda n=name: {s["name"]: s
                                   for s in file_tools.build_file_tool_schemas()}[n]),
            handler=handler,
            readonly=readonly,
        )


_register_file_tools()


def _register_browser_tool() -> None:
    """把 ``browser`` 单件登记进同一注册表（[浏览器能力 2026-11-02]）。

    机制移植自 MiniMax Code 的 in-agent browser：单工具紧凑形态
    （action+input）、会话级隔离 profile、语义树＋快照域 ref、显式
    效果校验。schema 延迟求值（ACTIONS 清单在 src/infra/browser/session
    现取），handler 在 browser_tool.py（to_thread 隔离 Chrome 阻塞）。
    """
    from . import browser_tool
    TOOL_REGISTRY["browser"] = ToolDef(
        schema=lambda: browser_tool.build_browser_tool_schemas()[0],
        handler=browser_tool.h_browser,
        readonly=False,
        doc=ToolDoc(
            group="联网",
            scene="需要看真实渲染出来的页面时：验 UI 长什么样、点页面上的东西、"
                  "填表单、读控制台报错。只抓文本用 fetch_web，不用开浏览器。",
            usage="循环：navigate 打开 → inspect 拿语义地图（元素带 ref，如 "
                  "e2.7）→ 用 ref 调动作 → inspect/query 验证。ref 导航后作废；"
                  "click 只证明输入已派发，效果要自己验证；预期导航的点击用 "
                  "click_and_wait_for_navigation；完事 close 放会话。",
            effect=ToolEffect(sink="none"),
        ),
    )


_register_browser_tool()

# [迭代二 S2] 思维链七工具单点接入（任务书 S2 步骤 5，照本文件 :707 注册法）：
# create_plan / plan_reorder / plan_insert / plan_remove / plan_modify /
# step_complete / escalate_to_plan——实现与 schema 住 src/thinkchain/plan_tools.py，
# 此处只做注册表装配；三处下游（build_tool_schemas / execute_tool /
# READONLY_TOOLS）自动派生，工具清单数量 +7（验收可机械核对）。
# 位置硬约束：必须在 _register_file_tools() 之后、READONLY_TOOLS 派生之前
# （七件全为写工具 readonly=False，晚于派生会漏出只读白名单口径）。
thinkchain_plan_tools.register_into(TOOL_REGISTRY, ToolDef)


# ============================================================
# [迭代四 K2 沿革 → 2026-10-11 摘除] 卡间发信原为模型工具 send_card_message
# （卡间册 §4.3 注册形态），经用户 2026-10-11 12:34 裁定改为**流程专用
# 基础设施接口**——工具注册条目已删除（模型工具面不再下发），函数体
# _h_send_card_message 保留为内部实现，流程入口＝下方薄壳 send_card_message。
# 六道门槛（存在/非己/类型在册/reply_to 一致/正文非空/权限）一条不减；
# 「默认不拦」保持（handler 内无人工审批分支）。
# ============================================================
async def _h_send_card_message(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]],
                               *, require_link: bool = True,
                               require_switch: bool = True) -> dict[str, Any]:
    """发信执行体。``require_link``＝是否要求两卡之间已有通信线。

    模型侧（工具面 send_card_message）一律 True——**通信走专门的通信线**；
    流程/系统侧（下方 send_card_message 薄壳：完工回执、blocked 通知、S4 自动
    回发）传 False：它们不是模型自主通信，不受"先建线"约束。

    ``require_switch``＝是否要求**发起方**有 ``send_card_message`` 开关。
    模型侧 True（它本来就是"这张卡能不能发信"）；系统侧 False——worker 卡刻意
    没有这个开关（v4：完工回执由系统发，不归模型），系统通道查它会自断回执。
    """
    target_id = arguments.get("target_card_id", "")
    # ① 收件卡存在（照 _h_read_card 先例：先判存在再判权限）
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    # ② 不得给自己发（放在 ① 之后——自己必存在）
    if target_id == current_id:
        return err("invalid", "不能给自己发消息")
    # ③ 类型合法（错误文案列出合法值，便于模型自我纠正）
    mt = arguments.get("msg_type", "")
    if mt not in card_msg.type_codes():
        return err("invalid", f"未知消息类型: {mt!r}（合法值：{list(card_msg.type_codes())}）")
    # ④ reply_to 与类型一致：msg_answer 必填；其余不得带
    reply_to = arguments.get("reply_to", "")
    if mt == "msg_answer" and not reply_to:
        return err("invalid", "应答类消息必须填 reply_to")
    if mt != "msg_answer" and reply_to:
        return err("invalid", f"{mt} 类消息不得携带 reply_to")
    # ⑤ 正文非空
    content = str(arguments.get("content", "")).strip()
    if not content:
        return err("invalid", "消息正文不得为空")
    # 权限＝只判**发起方**有发信开关（``require_switch``；系统通道跳过）。
    # [通信不看对方 2026-10-22 · 用户令] 原来这里是
    # ``policy.check(current_id, target_id, SWITCH_READ, cards)``——查**目标卡**有没有
    # read。这是「统一关口」的副作用（check 一律查 target），不是为通信设计的：
    # 所有类型都有 read，所以它从来没拦过任何人，只多一层误导。真正该管的两件事
    # 各有其主：能不能发＝发起方的 send_card_message（工具面已按它开关）；
    # 能不能发给这个对象＝下面那道线边界。
    if require_switch:
        bad = _require_wire_switch(current_id, policy.SWITCH_SEND_MESSAGE,
                                   cards, "发信")
        if bad is not None:
            return bad
    # [角色机制 5710 · rebase 后保留] 角色卡离树、不挂在任何一棵任务树里，但
    # 「角色 ↔ 关注卡」是**已建立的订阅边**（role_room.link 在两侧卡面各写一条
    # links），按新口径本就算已有通信线；这里仍显式认这条边，护住只有档案、
    # 未落 links 的 v1 订阅数据。非角色卡恒 False，一律走下面的线边界。
    _role_edge = role_room.linked(work_id, current_id, target_id)
    # [5713 通道试走 · 用户口径 2026-10-20 逐字]「卡间通信有专门的通信线」
    # ——通信的边界**是通信线，不是 parent 树**。故判定由「两卡同一棵任务树内」
    # 改为「两卡之间已有通信线」（scope=SCOPE_LINKED，任一侧有 links 条目即成立，
    # 这样 A 建的线 B 能沿它回话）。沿革：原判定用 _is_in_subtree 双向判定来
    # 近似"同树"，但兄弟卡彼此都不在对方子树内 ⇒ 平级卡恒不通过、永远通不了话。
    # 无线即挡死并指路（先 link_cards 建线），跨树跨枝一视同仁。
    if require_link and not _role_edge:
        # 走 policy.check_linked **而不是** check(..., scope=SCOPE_LINKED)：
        # read 不在 TREE_SWITCHES 内，scope 会被 check 整体跳过（实测放行任意卡）。
        #
        # [2026-10-27 · 用户裁定「删掉这个后门，统一用通信线」]
        # 改前这里是「有线 **或** 互为祖先-后代，任一即放行」——父子关系被当成
        # 通信许可，于是 parent 边实际在承载通信（登记在 system_claims.UNMET）。
        # 现在退回已拆：**只认通信线**。
        # 前提已验证：全部 60 个 work、229 对父子，线表覆盖率 100%（建卡与迁移
        # 都补了「父子默认通信线」）⇒ 拆掉后退回不会让现存纵向协作断掉。
        try:
            policy.check_linked(current_id, target_id, cards)
        except PermissionError:
            return err(
                "denied",
                f"越权：{target_id} 与你之间没有通信线——"
                f"卡间通信只走通信线，请先用 link_cards 建一条指向它的线"
                f"（type 取 关联），再发信")
    # 落信 + 留痕（错误在前面全走 err 信封不落档——发信成功才落信）
    #
    # [2026-10-27 · 用户裁定「inbox 直接取消了，没有意义，因为消息会留在上下文里
    # 直到被凝练掉」] **落点改回会话块**：消息进对方上下文历史，与 human/assistant
    # 同级参与凝练（`predicates.MESSAGE_TYPES` 已把 msg_* 纳入），
    # 不需要"主动取"、也不会有水位把它读没了。
    # 六道门槛、权限关口、通知帧一条不减。
    rec = await session_facade.append_card_message(
        work_id, target_id, from_card=current_id, text=content,
        msg_type=mt, reply_to=reply_to)
    msg_id = str((rec or {}).get("msg_id") or "")
    session_ops.ops_log("card_msg_send", work_id, target_id,
                        {"msg_id": msg_id, "type": mt, "from": current_id})
    # ============================================================
    # [发消息即唤醒 / 不是投递 2026-10-22 · 用户裁定 D20/D24]
    #
    # 用户原话：「发消息本身就是唤醒才对」「不是投递，是通信」。
    # 旧实现只在这里记一笔 TRIGGERS 就完事——那个字典只在「某张卡回合收口时」
    # 被消费；若此刻没有卡在跑，信就永远躺着。那是投递。
    #
    # 现在：落信后**立即投递**。
    #   · 对方空闲 ⇒ 立刻起一轮（deliver_now 内部判断）
    #   · 对方在跑 ⇒ deliver_now 返回 False，但对方本轮收口时会自查续轮
    #     （message_runner.schedule_card_message_triggers 末尾那段）
    # 两条路都通，不靠运气。TRIGGERS 保留作跨进程/异常兜底，但不再依赖它。
    # ============================================================
    try:
        from . import message_runner as _mr
        # [2026-10-27 · 用户裁定「唤醒应该以用户消息去包装」] 消息已在会话块里，
        # 所以走 **text 模式**：只负责叫起一轮，正文由上下文历史承载。
        await _mr.deliver_now(
            work_id, target_id,
            text=f"你收到一条来自 {current_id} 的消息，正文已在上文（卡间消息）。",
            source={"kind": "card_msg", "from": current_id})
    except Exception as exc:  # noqa: BLE001 投递失败不算发信失败（信已落库）
        logger.warning("[通信] 立即投递失败，交给收口兜底 target=%s: %s",
                       target_id, exc)
    # [消息底座能力对齐 · 件2] 事件流留痕照旧：消息仍记 msg_* 事件（账本不变），
    # 落点＝**收件方自己的键**（与改造前 _emit_block_event 同一落点、同一形状）。
    #
    # [harness 第 1 步 2026-10-28 · 删重复落点] 上面 `append_card_message` 内部
    # 已经经 `append → _emit_block_event` 落了一条 msg_*（带全零件）。这里原先
    # **又**发一条同型事件 ⇒ 同一次发信两条 msg_*，而且这条没有零件。
    # 实测历史里没出现重复，是因为该工具已退役、这条路径没被走到；但一旦有人
    # 再调 `send_card_message`，就会出现"有正文没发件人"的第二条。
    # 故删除此处重复落点——消息事件的唯一落点回到 `append` 那一条。
    # [迭代四 K4] 通知帧：落信成功后，在**收件方自己的键**上 emit 一帧
    # （event_stream 按 (work_id, card_id) 分键，不跨键；零阻塞零抛出——
    # 展示层绝不影响主流程）。前端靠它增量更新角标与未读数（F4 消费）。
    event_stream.emit(work_id, target_id, "card_message",
                      {"msg_id": msg_id, "from": current_id, "type": mt})
    # [画布 5711 · P0 修] **同时**广播到工作级通道。
    #
    # 病根：画布页原来靠 `new EventSource('/api/works/{w}/cards/{w}/stream')` 听
    # card_message —— 但那个 URL 的 card_id 传的是 **work_id**（不是卡号）⇒
    # 订阅的是一条**永远不会有帧**的键 ⇒ 未读角标的增量通道**从来没活过**
    # （只剩 60s 轮询）。而画布页要知道自己工作里"哪张卡来消息了"，本来
    # 就需要**工作级**的广播。
    # ⇒ 现在：工作级流多带一帧 card_message，画布页一条连接就能收全。
    #   顺带把画布页的**常驻长连接从 2 条降到 1 条**（浏览器每源 6 连接上限很紧，
    #   见 reloadCanvas 里"找根卡不再走网络"的说明）。
    event_stream.emit_work(work_id, "card_message",
                           {"card_id": target_id, "msg_id": msg_id,
                            "from": current_id, "type": mt})
    return {"msg_id": msg_id, "target_card_id": target_id, "type": mt}


# ============================================================
# [用户 2026-10-11 12:34 逐字]「发信不是随意调起的工具，是只有特定
# 流程才能使用的接口，这是一个基础设施，不是一共通用工具」——发信
# 已从 TOOL_REGISTRY 摘除（模型工具面不再下发），保留为**流程代码
# 专用**的内部接口。真实发信行为全部落在 _h_send_card_message（六道
# 门槛＋落信＋留痕＋触发意图＋通知帧一条不减）；「加类型＝加条目零
# 代码」由函数体校验（type_codes 现读）与注册表保持。
# 结构保证：模型侧无路（不在任何工具清单）；流程侧唯一入口＝本接口。
# 使用见《卡间发信接口-基础设施使用文档-20261011.md》。
# ============================================================
async def send_card_message(
    work_id: str,
    from_id: str,
    *,
    target_id: str,
    msg_type: str,
    content: str,
    reply_to: str = "",
) -> dict[str, Any]:
    """卡间发信 · 流程专用基础设施接口（模型不可见）。

    参数由流程语义决定（不吃模型风格参数包）；本函数只做参数翻译并
    取卡表，随后把真实行为委托给 _h_send_card_message（门槛一条不减）。

    [5713] 本薄壳的调用方全部是**系统/流程**路径（worker 卡完工回执、blocked 通知、
    S4 自动回发）——它们不是"模型自主发起通信"，故传 require_link=False，
    不受"两卡之间须有通信线"约束（否则完工回执到主卡会被自己的新闸挡死）。
    """
    cards = card_store.iter_card_metas(work_id)
    arguments = {
        "target_card_id": target_id,
        "msg_type": msg_type,
        "content": content,
        "reply_to": reply_to,
    }
    return await _h_send_card_message(work_id, from_id, arguments, cards,
                                      require_link=False,
                                      # 系统通道不查发起方开关：worker 卡刻意没有
                                      # send_card_message（v4），查它会自断完工回执。
                                      require_switch=False)


# ============================================================
# [自动模式 2026-10-17 · 回灌 5700 G2（041e25d/e4e612d）] 把卡片间通信
# **交回模型**——模型自主协作跑通（愿景）
#
# 2026-10-11 12:34 裁定把 send_card_message 从**常规**模型工具面摘除
# （"发信不是通用工具，是流程专用基础设施"）。本处**不撤销该裁定**：常规
# 卡面逐字不变（仍不可见），只在 auto_mode=1 的卡上、经 AUTO_ONLY_TOOLS 的
# **条件下发**机制追加（同 update_goal 手法，非卡片内挂）。执行体仍是
# _h_send_card_message（六道门槛一条不减：存在/非己/类型在册/reply_to 一致/
# 正文非空/权限＋同树判定），落点仍是收件箱（status=pending），调起与自动
# 回发仍走既有 K3/S4 通道——**不新造第二套通信**。
#
# 配套 read_inbox：模型要活用通信，必须先能看见自己收到过什么、哪些待
# 处理、哪些待回应——否则只能被动等激励文，谈不上"活用"。只读自己的
# 收件箱（current_id），零写入。
# ★ 注册位置：必须在 READONLY_TOOLS 派生之前（read_inbox 是只读件，要进
#   白名单；晚于派生会漏出"可按原参数重取"口径）。
# ============================================================

async def _h_read_inbox(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """读本卡收件箱。

    [增量语义 2026-10-22 · 用户裁定「读过的内容，下次不应再读到」]
    入向**默认只回你没读过的**（按读取水位算），返回后水位推进到本次最大 seq——
    下次再调不会再拿到同样的信。要 deliberate 回看历史，显式传 ``scope="all"``。
    """

    # 执行层关口（与全系统同构的双保险：工具面已按 auto 条件下发，这里再判一次）
    try:
        policy.check(current_id, current_id, policy.SWITCH_READ, cards)
    except PermissionError as exc:
        return err("denied", str(exc))
    # [G4 · 8eaf33e] direction：incoming（缺省，行为逐字不变）／
    # outgoing（本卡**发出**的信＋是否已被回答）／both。
    # 动机：发问方若只能读入向，问出去的信石沉大海时无从区分「对方还没答／
    # 答不了／不打算答」——出向视图让发问方知道自己的信有没有被回答。
    direction = str(arguments.get("direction") or "incoming").strip().lower()
    if direction not in ("incoming", "outgoing", "both"):
        return err("validation",
                   f"direction 非法: {direction or '(空)'}（只允许 incoming/outgoing/both）")
    # [取消 inbox 状态机 2026-10-22 · D25] `pending_only` 参数已移除；
    # [增量语义 2026-10-22] 由 `scope` 接管：unread（缺省，只回没读过的）／all（全量回看）。
    if "pending_only" in arguments:
        return err("validation",
                   "pending_only 参数已移除（收件箱状态机已取消）："
                   "入向默认就是增量（只回没读过的），无需该参数")
    scope = str(arguments.get("scope") or "unread").strip().lower()
    if scope not in ("unread", "all"):
        return err("validation",
                   f"scope 非法: {scope or '(空)'}（只允许 unread/all）")
    try:
        # 入向（缺省＝增量：只拿读取水位之后的；all＝完整往来记录）
        if direction in ("incoming", "both"):
            if scope == "unread":
                items = await card_db.list_pending(work_id, current_id)
            else:
                items = await card_db.list_inbox(work_id, current_id)
        else:
            items = []
        # 出向需要全量账本（判 answered 要找有没有人 reply_to 这条）
        all_rows = (await card_db.list_inbox(work_id)) if direction in ("outgoing", "both") else []
    except Exception as exc:  # noqa: BLE001
        return err("internal", f"收件箱读取失败: {exc}")

    def _row(it: dict[str, Any], *, outgoing: bool = False) -> dict[str, Any]:
        raw = str((it or {}).get("content") or "")
        hdr = card_msg.parse_header(raw) or {}
        mt = str((it or {}).get("type") or hdr.get("type") or "")
        mid = str((it or {}).get("msg_id") or hdr.get("id") or "")
        rec = {
            "msg_id": mid,
            "from": str((it or {}).get("from_card") or hdr.get("from") or ""),
            "to": str((it or {}).get("target_card_id") or ""),
            "type": mt,
            "type_display": card_msg.display_of(mt) if mt else "",
            "reply_expected": card_msg.reply_expected_of(mt) if mt else False,
            "status": str((it or {}).get("status") or ""),
            "reply_to": str((it or {}).get("reply_to") or hdr.get("reply_to") or ""),
            "created_at": str((it or {}).get("created_at") or ""),
            "content": card_msg.strip_header(raw),
        }
        if outgoing:
            # ★ answered＝有没有任何一行 reply_to 指向这条（通常是一条 msg_answer）。
            # 注意 status 的语义是**收件方是否已处理**，不等于"已回答"——
            # 实测有卡被调起、把信处理成 consumed 却从未回信。故 answered 独立判定。
            rec["answered"] = any(
                str((x or {}).get("reply_to") or
                    (card_msg.parse_header(str((x or {}).get("content") or "")) or {}).get("reply_to") or "") == mid
                for x in all_rows or [])
        return rec

    out: list[dict[str, Any]] = [_row(it) for it in (items or [])]
    if direction in ("outgoing", "both"):
        out += [_row(it, outgoing=True) for it in (all_rows or [])
                if str((it or {}).get("from_card") or "") == current_id]
    # [增量语义 2026-10-22] 读过即推进水位：下次不再重发同样这几条。
    # 只增不减 ⇒ 乱序/并发读都不会把已读退回未读。出向视图不推进（那不是"读"）。
    seen_after = None
    if direction in ("incoming", "both") and scope == "unread" and items:
        try:
            _max_seq = max(int(it.get("seq") or 0) for it in items)
            seen_after = await card_db.advance_seen_seq(work_id, current_id, _max_seq)
        except Exception as exc:  # noqa: BLE001 推进失败不阻断读取
            logger.warning("[read_inbox] 推进读取水位失败 card=%s: %s", current_id, exc)
    sent_pending = [r for r in out if r.get("answered") is False and r.get("reply_expected")]
    return {"card_id": current_id, "count": len(out), "messages": out,
            "direction": direction, "scope": scope, "seen_seq": seen_after,
            # 出向提醒：发了却没人回答的信（发问方据此做知情决策，别默默接受）
            "unanswered_sent": [r["msg_id"] for r in sent_pending]}


# ============================================================
# [通信收拢 2026-10-28 · 用户裁定「接口只有两个，且只能沿连线」]
#
# 退役两件（**不注册 = 模型看不见**，handler 保留供内部路径与既有测试直调）：
#   · `send_card_message` —— 通用发信。它的三种语义已分别落到：
#       msg_notify → `sync`（告知，不唤醒）
#       msg_ask / msg_answer → `direct`（要对方做事）与 `sync`（答回去）
#     **内部薄壳 `send_card_message()`（模块级函数）保留**：worker 卡完工回执、
#     blocked 通知等系统路径仍走它，且它不受连线校验（系统通道）。
#   · `read_inbox` —— 收件箱取消（消息落会话块，历史里天然可见），
#     账本水位不再作为模型可见面。
# ============================================================


# ============================================================
# [总裁制三原语 P3/P4/P5 2026-10-24] 四件新工具
#
# P4 report_up   —— 子卡唯一的对外通道(完工/阻塞/请示)。广播:系统按拓扑
#                   分发(父卡+盯线职能+总裁),发件人不选收件人——
#                   「汇报同时出现在父卡与关联职能卡眼中,信息多方汇聚」。
# P3 curate_face —— 总裁执笔:跨卡引导块(需求文档)的精准编辑;
#                   决策留痕(card_history 天然留痕,追加决策簿摘要)。
# P5 confirm_with_user / report_to_user —— 总裁对用户的两个口:
#                   确认请求(带待确认前提清单)与中间汇报(汇报簿留痕)。
#
# 权限与下发:三件不进常规池(同类手法:OP_ONLY/AUTO_ONLY),由工具面末段按
# 「目录条目 allow」追加(总裁回合)或按卡类型(子卡 report_up)下发;
# handler 内另有执行层双保险(谁在调、调没调对)。
# ============================================================

#: [P3] 引导块写权的执行层双保险:只有总裁(目录 ceo 条目、落在根卡的档案)可执笔。
def _is_ceo_curator(work_id: str, current_id: str) -> bool:
    try:
        from . import function_room as _fr
        prof = _fr.function_meta_for(work_id, current_id) or {}
        return _fr.catalog_code_of(prof) == "ceo" and current_id == (
            work_store.work_card_id_of(work_id) or "")
    except Exception:  # noqa: BLE001 判不出 ⇒ 不是总裁
        return False


async def _h_report_up(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[通信收拢 2026-10-28 · 用户裁定] **汇报只有一条路：向上一级**。

    改前：收件人＝父卡＋盯线职能＋总裁，且按 kind 分四档（done/phase/blocked/ask），
    done/phase 还会另发一条〔阶段收尾·请验收〕去召集职能。
    改后：
      · **收件人只有直接父卡**（系统算，发件人不选）。不发总裁、不召集职能。
      · **没有 kind**：汇报就是汇报。工作有问题，上级自然会 `direct` 下发修改；
        需要裁定的，也不走汇报这条路（用户令：裁定不靠口子，靠自裁权）。
    效果：正文落父卡会话块（进上下文、可凝练）＋ 唤醒父卡一轮。
    """
    content = str(arguments.get("content") or "").strip()
    if not content:
        return err("validation", "content 不能为空")
    if current_id == work_store.work_card_id_of(work_id):
        return err("denied", "总裁不汇报——总裁用 report_to_user 对用户汇报")

    meta = (cards or {}).get(current_id) or {}
    title = str(meta.get("title") or current_id)
    parent = str(meta.get("parent") or "")
    if not parent:
        return err("validation",
                   "本卡没有父卡（离树卡不向上汇报）——要向谁说明，用 sync/direct 沿连线发")
    text = f"【汇报·{title}({current_id})】\n{content}"

    from . import message_runner as _mr
    _mid = ""
    try:
        _rec = await session_facade.append_card_message(
            work_id, parent, from_card=current_id, text=text,
            msg_type="msg_report")
        _mid = str((_rec or {}).get("msg_id") or "")
        _mr._spawn_tracked(_mr.deliver_now(
            work_id, parent,
            text=f"你收到一条来自 {current_id} 的汇报，正文已在上文。",
            source={"kind": "card_msg", "from": current_id}))
    except Exception as exc:  # noqa: BLE001 投递失败要如实回给模型
        logger.warning("[report_up] 投递失败 to=%s: %s", parent, exc)
        return err("internal", f"汇报投递失败: {exc}")
    return {"card_id": current_id, "delivered": [{"to": parent, "msg_id": _mid}],
            "recipients": [parent]}


async def _h_read_archive(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[归档 2026-10-25] 读归档记录——**先查通用归档（card_archive，任何卡），
    再回落老的 worker 归档（coder_archive，只有worker 卡）**。归档卡不是活卡，
    read_card 查不到它们，本工具是唯一入口。"""
    from ..storage import card_archive as _ca_new
    from ..storage import coder_archive as _ca
    card_id = str(arguments.get("card_id") or "").strip()
    parent = str(arguments.get("parent") or "").strip()
    if card_id:
        rec = _ca_new.get_record(work_id, card_id)
        if rec is None:
            rec = _ca.get_record(work_id, card_id)
        if rec is None:
            return err("not_found", f"无归档记录: {card_id}")
        return {"ok": True, "record": rec}
    recs = _ca_new.list_records(work_id, parent or "")
    seen = {str(r.get("card_id")) for r in recs}
    recs += [r for r in _ca.list_records(work_id, parent or "")
             if str(r.get("card_id")) not in seen]
    return {"ok": True, "records": recs, "count": len(recs)}


async def _h_continue_task(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[任务延续 2026-10-25 · 用户裁定「worker 完工后应可续派任务,做任务延续」]

    给完工/空闲的worker 卡**续派下一件任务**——同一张卡接着干,之前交付的
    代码、回执、会话上下文全部延续(这是续派相对新建的全部价值)。

    机械动作(复用既有重派活链):
      ① 权限:祖先(父卡限子树,总裁全树);
      ② 目标必须 card_type=worker（其他卡型走建卡/消息通道）;
      ③ state: completed → active 复位;goal complete ⇒ 用新任务开新生命周期;
         paused/blocked ⇒ resume;
      ④ 新任务文本落 goal,随后系统按既有链点火(本 handler 不直接起回合——
         调用方(父卡)回合收口后驱动器接管,或卡被唤醒时以任务文本开跑)。
    """
    target = str(arguments.get("card_id") or "").strip()
    task = str(arguments.get("task") or "").strip()
    note = str(arguments.get("note") or "").strip()
    if not target:
        return err("validation", "card_id 不能为空")
    if not task:
        return err("validation", "task 不能为空(下一件任务是什么)")
    if target == current_id:
        return err("validation", "不能给自己续派——续派是上级对下级的动作")
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id) or ""
    except Exception:  # noqa: BLE001
        root_id = ""
    is_root = current_id == root_id
    if not is_root:
        try:
            if not policy._is_in_subtree(current_id, target, cards):
                return err("denied",
                           f"越权:{target} 不在你的子树内,你不能给它续派任务")
        except Exception as exc:  # noqa: BLE001 判定失败一律拒
            return err("denied", f"子树判定失败,已拒绝: {exc}")
    meta = (cards or {}).get(target) or card_store._meta_of(work_id, target) or {}
    if str(meta.get("card_type") or "") != "worker":
        return err("validation",
                   f"{target} 不是worker 卡——续派只对 worker(其他卡型的任务延续"
                   "走普通消息/目标管理)")

    # 复位 + 新生命周期(照 _revive_worker_card 的机械序列)
    from ..goal.service import get_service
    from ..goal.types import CreateGoalRequest
    svc = get_service()
    # [机制判断归harness 第3步] 生命周期分支判据走派生视图——事件流最后
    # 一条 state 变更说了算（无覆盖回落列），不再读手写 state 列。
    from ..infra import card_status as _cs
    cur_state = _cs.derive(work_id, target)["delivery"]
    if cur_state == "active":
        # [停在中途可续派 2026-11-01 · 用户令「唤醒子卡不需要自动」]
        # 「在干活」的判据不是 state 列：stop_card(halt) 只停 goal＋置停止
        # 标记，**不复位 state**——被停住的 worker 永远停在 active（实测
        # work_027/card_140：父卡更正任务下发无路）。真正表示「它在干活」
        # 的只有两种：
        #   ① 有在途回合（turn 互斥体被持——含等串行闸的排队回合）；
        #   ② auto 卡且目标 active+armed（驱动随时给它续轮，续派会撞车）。
        # 两者都不占 ⇒ 没在跑，可续派；顺手解除 halt 标记
        # （halt 卡的内部唤醒会被拒，点火走 _run_message 会先查它）。
        from ..storage import session_facade as _sf
        from . import control as _control
        try:
            _running = bool(_sf.turn_holder(work_id, target))
        except Exception:  # noqa: BLE001 判不出来按在跑处理（宁可不派不误抢）
            _running = True
        _driven = False
        if not _running and bool(meta.get("auto_mode")):
            try:
                _g = await svc.get(work_id, target)
                _driven = bool(_g is not None and _g.phase == "active"
                               and _g.activation == "armed")
            except Exception:  # noqa: BLE001 判不出来按在跑处理
                _driven = True
        if _running or _driven:
            return err("validation",
                       f"{target} 正在干活(state=active)——等它完工再续派;"
                       "要打断用 stop_card")
        if _control.unhalt(work_id, target, by=current_id,
                           via="continue_task"):
            logger.info("[continue_task] 解除强制停止并续派 card=%s by=%s",
                        target, current_id)
    upd = await card_store.update_card(
        work_id, target, {"state": "active",
                          "toolcard_final": ""},
        f"由「{(cards or {}).get(current_id, {}).get('title', '')}」续派任务"
        + (f"·{note}" if note else ""))
    if isinstance(upd, dict) and upd.get("error"):
        return err("internal", str(upd["error"]))

    goal_started = "resumed"
    try:
        goal = await svc.get(work_id, target)
        text = task
        if goal is None:
            if text:
                await svc.create(work_id, target,
                                 CreateGoalRequest(objective=text))
                goal_started = "created"
        elif goal.phase == "complete":
            await svc.create(work_id, target,
                             CreateGoalRequest(objective=text))
            goal_started = "created"
        elif goal.phase in ("paused", "blocked"):
            # [停在中途可续派 2026-11-01] 续派文本＝新目标——先改目标内容再
            # 恢复相位；只 resume 不改文本，跑的还是旧目标（实测父卡更正任务
            # 时旧 goal 纹丝不动，等于更正没发出去）。卡面 goal 区是真值，
            # svc.edit 只管相位域的事件投影，两处都写。
            if text:
                await card_store.update_card(
                    work_id, target, {"goal": text},
                    f"[continue_task] {current_id} 续派任务改目标")
                from ..goal.types import EditGoalRequest
                await svc.edit(work_id, target, goal.ref(),
                               EditGoalRequest(objective=text))
                goal = await svc.get(work_id, target)
            await svc.resume(work_id, target, goal.ref())
        elif goal.phase == "active" and text:
            # 空闲的 active 卡（没在跑、没被驱动）：续派文本同样要成为新目标，
            # 相位已是 active 无需 resume。
            await card_store.update_card(
                work_id, target, {"goal": text},
                f"[continue_task] {current_id} 续派任务改目标")
            from ..goal.types import EditGoalRequest
            await svc.edit(work_id, target, goal.ref(),
                           EditGoalRequest(objective=text))
            goal_started = "edited"
    except Exception as exc:  # noqa: BLE001 goal 层失败如实报
        return err("internal", f"目标续期失败: {exc}")

    # 点火:唤醒该卡跑新任务(激励文=新任务文本,经既有 _run_message 链)
    from . import message_runner as _mr
    from . import routes as _routes
    try:
        _mr._spawn_tracked(_routes._run_message(
            work_id, target, f"【续派任务】{task}", None, append_user=False))
        fired = True
    except Exception as exc:  # noqa: BLE001 点火失败不撤续派(目标已在,下轮会跑)
        logger.warning("[continue_task] 点火失败 card=%s: %s", target, exc)
        fired = False
    session_ops.ops_log("worker_continue_task", work_id, target,
                        {"by": current_id, "task": task[:200],
                         "goal": goal_started})
    return {"ok": True, "card_id": target, "goal": goal_started,
            "ignited": fired,
            "note": "任务已续派,上下文延续(代码/回执/会话均保留)"}


def _review_invite_text(subject: str, batch_id: str, focus: str) -> str:
    lines = [f"〔评审邀请·{subject}〕批次 {batch_id}。"]
    if focus:
        lines += ["", "评审焦点:", focus]
    lines += ["", "按你的职责对上述对象评审,结论用 submit_review"
              "(verdict=你的结论) 提交;收集齐自动呈报总裁。"]
    return "\n".join(lines)


async def _h_scene_report(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[场景·汇报] 阶段完工上行——report_up 的场景壳(报告成果给上级)。"""
    arguments.setdefault("kind", "done")
    return await _h_report_up(work_id, current_id, arguments, cards)


async def _h_scene_sync(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[场景·同步] 通知相关卡一件事/变更——不期待回音,**不唤醒**,
    只入收件箱(对方下次自然回合看到)。收件人=显式指定或沿连线。
    [事实分层 2026-11-01] 同步是**送达信息**不是搬运事实——对方的判断依据在它自己的卡面。"""
    import uuid as _uuid
    from ..storage import card_db as _cdb
    content = str(arguments.get("content") or "").strip()
    if not content:
        return err("validation", "content 不能为空(你要同步的事是什么)")
    targets = [str(x).strip() for x in (arguments.get("card_ids") or [])
               if str(x).strip()]
    if not targets:
        meta = (cards or {}).get(current_id) or {}
        targets = [str(l.get("target")) for l in (meta.get("links") or [])
                   if l.get("target")]
        # [2026-10-27 · 用户裁定「统一用通信线」] 删掉「没线就退到父卡」的兜底：
        # 那等于 parent 边隐含一条通信通道。父子之间现在有默认通信线，
        # 沿 links 就能找到，不需要这个后门。
        targets = [c for c in targets if c and c != current_id]
    delivered = []
    refused: list[dict[str, str]] = []
    for t in targets:
        if not card_store.card_exists(work_id, t):
            refused.append({"to": t, "why": "卡片不存在"})
            continue
        # [2026-10-27 · 统一用通信线] 收件人必须与自己有通信线——
        # 显式指定 card_ids 也不再是绕过线的通道。
        # [差事即发话权 2026-11-01] 例外一条：职能被**差事**派到这张卡时，
        # 差事本身就是线（它被叫来就是干这场上的活）——验收结论发回场卡
        # 不需要另建通信线。判据与 _cap_gate 的差事作用域同源。
        try:
            policy.check_linked(current_id, t, cards)
        except PermissionError:
            if not _deployment_scope(work_id, current_id, t, "notify"):
                refused.append({"to": t, "why": "与你之间没有通信线"})
                continue
        try:
            # [2026-10-26 · 用户裁定「同步/通知类消息不该用 inbox，也不该只在
            # 当轮可见」] 直接落成**会话块**：它是历史的一部分，下次任何回合的
            # prior_messages 都带着它 ⇒ **不唤醒也能被看到**（上下文机制本来
            # 就支持）。走 inbox 则是水位消费，读一次就永远查不到；塞工具结果
            # 更是只活在当轮。
            rec = await session_facade.append_card_message(
                work_id, t, from_card=current_id, text=content,
                msg_type="msg_sync")
            delivered.append({"to": t,
                              "seq": int((rec or {}).get("seq") or 0)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sync] 投递失败 to=%s: %s", t, exc)
    if not delivered:
        return err("not_found",
                   f"没有可送达的收件人（显式指定 card_ids 或建连线）。"
                   f"被拒：{refused}" if refused else
                   "没有可送达的收件人（显式指定 card_ids 或建连线）")
    out = {"ok": True, "delivered": delivered,
           "note": "已写入对方上下文(不唤醒,对方下次回合自然看到)"}
    if refused:
        out["refused"] = refused
    return out


async def _h_scene_direct(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[场景·指令] **下达任务**：沿通信线送达 + 把对方叫起来跑一轮。

    [通信收拢 2026-10-28] 语义定死为「指令/下达任务」，验收判据**必填**：
    没有判据，收令方做完也无法自判「做完了没有」，发令方也无法验收。
    验收不另设工具——通过就不说话，不通过再发一条 direct 要求改。
    """
    import uuid as _uuid
    from ..storage import card_db as _cdb
    from . import message_runner as _mr
    target = str(arguments.get("card_id") or "").strip()
    instruction = str(arguments.get("instruction") or "").strip()
    acceptance = str(arguments.get("acceptance") or "").strip()
    if not target:
        return err("validation", "card_id 不能为空")
    if not instruction:
        return err("validation", "instruction 不能为空(你要它做什么)")
    if not acceptance:
        return err("validation",
                   "acceptance 不能为空——没有验收判据，收令方做完也无法自判"
                   "「做完了没有」。写清：做到哪些事算完成。")
    if not card_store.card_exists(work_id, target):
        return err("not_found", f"卡片不存在: {target}")
    # [两源模型 2026-11-01 · 用户令「不需要 direct 到职能，会诊删了」]
    # 职能不是干活的卡：它不驻场、不接指令、不私答——它的判断依据只有
    # 场卡需求文档＋rules.md 准则，私问只会产「没人裁过的主张」。
    # 会诊差事（consult venue）与之一并退役。
    _tmeta0 = card_store._meta_of(work_id, target) or {}
    if str(_tmeta0.get("card_type") or "") == "function":
        return err("denied",
                   f"越权:{target} 是职能卡，不收指令——职能只经差事"
                   "（评审/派驻）被叫到，不私下问答")
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id) or ""
    except Exception:  # noqa: BLE001
        root_id = ""
    # [通信收拢 2026-10-28] 作用域判定统一走 `policy.can()`（权限唯一规则表）：
    #   · 树内卡 → ⑥ 目标须在自己子树内（与改前逐字等价）
    #   · 根卡   → ④ 本项目任何卡通过
    #   · 离树卡（职能/角色）→ ⑤ 读全树/通信通过，只有 edit 与「往根卡下建卡」受限
    # 改前这里硬写 `_is_in_subtree`，离树卡没有子树 ⇒ 职能永远下不了指令
    # （所以才有 issue_directive 那条旁路）。issue_directive 退役后，direct 必须
    # 认离树卡，否则职能变哑巴。
    try:
        if not policy.can(current_id, target, policy.SWITCH_READ, cards):
            return err("denied", f"越权:{target} 不在你的作用域内")
    except Exception as exc:  # noqa: BLE001
        return err("denied", f"作用域判定失败,已拒绝: {exc}")
    # [2026-10-27 · 用户裁定「统一用通信线」] 权限（子树）之外还要有通信线：
    # 子树决定「你能不能命令它」，通信线决定「你这条命令能不能送过去」。
    # 父子之间有默认通信线，所以正常纵向派活不受影响。
    try:
        policy.check_linked(current_id, target, cards)
    except PermissionError:
        return err("denied",
                   f"越权：{target} 与你之间没有通信线——"
                   f"先 link_cards 建一条再下指令")
    try:
        # [2026-10-26 · 记录补全] 改前只 append_inbox + 唤醒：**没有任何事件**
        # （type 还写的 msg_notify）⇒ 被派发的卡在自己事件流里看不到「我被父卡
        # 派了活」。改走 append_card_message：落会话块（永久在上下文里）＋落
        # msg_directive 事件（带 from），判据据此认「父卡派发」。
        _body = instruction
        if acceptance:
            _body += f"\n验收判据:{acceptance}"
        rec = await session_facade.append_card_message(
            work_id, target, from_card=current_id, text=_body,
            msg_type="msg_directive")
        msg_id = str((rec or {}).get("seq") or "")
    except Exception as exc:  # noqa: BLE001
        return err("internal", f"指令落信失败:{exc}")
    # [2026-10-26] 消息已落会话块 ⇒ 唤醒走 text 模式（不取件，只起轮）
    _mr._spawn_tracked(_mr.deliver_now(
        work_id, target,
        text=(f"你收到一条来自 {current_id} 的指令，正文已在上文（卡间指令）。"
              f"请按其执行，完成后用 report_up 汇报。"),
        source={"kind": "card_msg", "from": current_id}))
    return {"ok": True, "card_id": target, "msg_id": msg_id,
            "note": "指令已送达(对方被唤醒,按验收判据执行)"}


async def _h_stage_open(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[头尾介入 2026-10-25 · 用户令「模型开启子卡任务必须先走职能卡」]

    **阶段任务的头**——由模型自己判断时机(子卡建好、需求填写完整),调用
    本工具即宣布阶段开始:职能卡被唤醒做立项评审(前提/切分/可验收性)。
    代码不做任何"这是不是开始"的判断——判断权在模型。
    权限:父卡(子树内)/总裁。
    """
    target = str(arguments.get("card_id") or "").strip()
    if not target:
        return err("validation", "card_id 不能为空")
    if not card_store.card_exists(work_id, target):
        return err("not_found", f"卡片不存在: {target}")
    meta = (cards or {}).get(target) or card_store._meta_of(work_id, target) or {}
    if str(meta.get("card_type") or "") in ("function", "rule"):
        return err("validation", "职能卡/规则卡不是阶段任务")
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id) or ""
    except Exception:  # noqa: BLE001
        root_id = ""
    if current_id != root_id:
        try:
            if not policy._is_in_subtree(current_id, target, cards):
                return err("denied",
                           f"越权:{target} 不在你的子树内")
        except Exception as exc:  # noqa: BLE001 判定失败一律拒
            return err("denied", f"子树判定失败,已拒绝: {exc}")
    requirements = str(meta.get("requirements") or "")
    try:
        _cur = await card_store.read_card(work_id, target)
        requirements = str((_cur.get("body") or {}).get("requirements") or "")
    except Exception:  # noqa: BLE001
        pass
    # [职能唤醒收口 2026-10-28 · 用户令「摘掉所有现有的【评审】之外的唤醒」
    #  「立项评审不算，评审目前只算【发起评审】这个动作」]
    # 改前这里直发一条「〔阶段开启·立项评审〕…」给全部在职职能 ⇒ 每张子卡开阶段
    # 都叫醒四个职能各跑一整轮模型。现在**摘掉**：开阶段只发事件（下面那行
    # stage_opened），职能要参与的是 open_review 那个动作，不是这里。
    # [2026-10-26 · 记录补全] 阶段开启此前**零事件**——判据无法知道「这张卡的
    # 阶段真的开了」，职能/父卡也查不到这个动作发生过。
    try:
        event_stream.emit(work_id, target, "stage_opened",
                          {"by": current_id,
                           "title": str(meta.get("title") or "")})
    except Exception as exc:  # noqa: BLE001 留痕失败不影响开启
        logger.warning("[stage_open] 事件失败: %s", exc)
    return {"ok": True, "card_id": target,
            "note": "阶段已开启:职能卡在做立项评审,观点经汇聚到总裁"}


async def _h_silence_card(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[沉默 2026-10-24 · 用户令] 让**完工的卡**沉默/重新激活。

    沉默=持久管理状态:拒一切内部唤醒(收件箱的信留 pending 不投递),
    直到被再次激活。权限与 stop_card 同(祖先;总裁全树);只对
    state=completed 的卡生效(在跑的卡用 stop_card)。留痕:ops+事件帧。
    """
    target = str(arguments.get("card_id") or "").strip()
    action = str(arguments.get("action") or "silence").strip()
    reason = str(arguments.get("reason") or "").strip()
    if not target:
        return err("validation", "card_id 不能为空")
    if action not in ("silence", "activate"):
        return err("validation", f"action 非法: {action}(silence|activate)")
    if target == current_id:
        return err("validation", "不能沉默自己")
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id) or ""
    except Exception:  # noqa: BLE001
        root_id = ""
    if target == root_id:
        return err("denied", "总裁(根卡)不可被沉默——它是对用户的唯一汇报口")
    is_root = current_id == root_id
    if not is_root:
        try:
            if not policy._is_in_subtree(current_id, target, cards):
                return err("denied",
                           f"越权:{target} 不在你的子树内,你不能沉默它"
                           "(总裁可管全树)")
        except Exception as exc:  # noqa: BLE001 判定失败一律拒
            return err("denied", f"子树判定失败,已拒绝: {exc}")
    meta = (cards or {}).get(target) or card_store._meta_of(work_id, target) or {}
    # [机制判断归harness 第3步] 完工判定走派生视图（事件权威＋列回落）。
    from ..infra import card_status as _cs
    _dstate = _cs.derive(work_id, target)["delivery"]
    if _dstate != "completed":
        return err("validation",
                   f"{target} 尚未完工(state={_dstate or '?'})——"
                   "沉默只用于完工卡;在跑的卡要停用 stop_card")
    from ..storage import governance_store as _gov
    # [2026-10-25 · 用户裁定「worker 不能自动归档,应可续派」] worker 与其他
    # 完工卡同语义:沉默=治理标记(不折叠)。续派走 continue_task 工具。
    if action == "silence":
        rec = await _gov.set_state(work_id, target, _gov.STATE_SILENCED,
                                   reason=reason or "完工沉默", by=current_id)
    else:
        rec = await _gov.set_state(work_id, target, _gov.STATE_ACTIVE,
                                   reason=reason or "重新激活", by=current_id)
    try:
        from . import event_stream
        event_stream.emit(work_id, target, "card_governance", {
            "state": rec["state"], "reason": rec["reason"][:200],
            "by": current_id,
        })
    except Exception:  # noqa: BLE001
        pass
    session_ops.ops_log("card_governance", work_id, target,
                        {"state": rec["state"], "by": current_id,
                         "reason": rec["reason"][:200]})
    return {"ok": True, "card_id": target, "state": rec["state"],
            "note": ("已沉默:不再接收内部消息,直到被激活"
                     if action == "silence" else "已激活:恢复接收")}


async def _h_stop_card(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[强制停止 2026-10-24 · 用户令「根卡和父卡要有打断/停止子卡的权限,
    且停止必须是强制性的」] 权限=祖先(root 对全树);两档:

    - interrupt(打断):只杀**当轮**(流内信号赛跑/流外取消协程,子进程同杀);
      自动卡随后照常续轮——用于打断跑偏/卡住的一轮;
    - halt(停止,缺省):当轮 + goal 正规 pause(级联其子树) + 停止标记
      (拒绝一切内部唤醒,直至用户发话或 resume_child)。
    """
    target = str(arguments.get("card_id") or "").strip()
    mode = str(arguments.get("mode") or "halt").strip()
    reason = str(arguments.get("reason") or "").strip()
    if not target:
        return err("validation", "card_id 不能为空")
    if mode not in ("interrupt", "halt"):
        return err("validation", f"mode 非法: {mode}(interrupt|halt)")
    if target == current_id:
        return err("validation", "不能停止自己——你是执行停止的一方")
    root_id = ""
    try:
        root_id = work_store.work_card_id_of(work_id) or ""
    except Exception:  # noqa: BLE001
        root_id = ""
    if target == root_id:
        return err("denied", "总裁(根卡)不可被卡面停止——只有用户能停它")
    is_root = current_id == root_id
    if not is_root:
        try:
            if not policy._is_in_subtree(current_id, target, cards):
                return err("denied",
                           f"越权:{target} 不在你的子树内,你不能停止它"
                           "(总裁可停全树)")
        except Exception as exc:  # noqa: BLE001 判定失败一律拒
            return err("denied", f"子树判定失败,已拒绝: {exc}")
    if target not in cards and not card_store.card_exists(work_id, target):
        return err("not_found", f"卡片不存在: {target}")

    from . import control as _control
    out: dict[str, Any] = {"card_id": target, "mode": mode, "reason": reason}
    if mode == "interrupt":
        out["round"] = _control.request_stop(work_id, target)
    else:
        out["round"] = _control.halt(work_id, target,
                                     reason=reason or "由上级卡强制停止",
                                     by=current_id)
        try:
            out["goal"] = await _control.stop_auto(work_id, target)
        except Exception as exc:  # noqa: BLE001 goal 暂停失败不影响轮停止
            out["goal"] = {"error": str(exc)[:200]}
    try:
        from . import event_stream
        event_stream.emit(work_id, current_id, "card_stopped", {
            "target": target, "mode": mode, "reason": reason[:200],
        })
        event_stream.emit(work_id, target, "card_stopped_self", {
            "mode": mode, "reason": reason[:200], "by": current_id,
        })
    except Exception:  # noqa: BLE001
        pass
    session_ops.ops_log("card_stopped", work_id, target,
                        {"mode": mode, "by": current_id, "reason": reason[:200]})
    return out


async def _h_curate_face(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[P3] 总裁执笔:改任意卡的引导块(需求文档 requirements)。

    [2026-10-28 去全量更新] **整块写通道已取消**，只剩精准编辑（old_string 唯一匹配，
    纪律与 append_requirements 同）。要新增一段：把某段原文换成「原文＋新段」。
    执行层双保险：只认总裁。
    """
    if not _is_ceo_curator(work_id, current_id):
        return err("denied",
                   "引导块的执笔权在总裁——把你的观点发进汇聚,由总裁统一落面")
    target_id = str(arguments.get("card_id") or "").strip()
    if not target_id or not card_store.card_exists(work_id, target_id):
        return err("not_found", f"卡片不存在: {target_id}")
    if target_id == current_id:
        return err("validation", "curate_face 不用于自己——自己的面用 edit_status")
    old_string = arguments.get("old_string")
    new_string = arguments.get("new_string")
    if old_string is None or new_string is None:
        return err("validation",
                   "必须同时给 old_string 与 new_string（精准编辑；整块写通道已取消）")
    if not isinstance(old_string, str):
        return err("validation", "old_string 必填；块还空着就给空串（首次写入）")
    mode = "edit"
    card = await card_store.read_card(work_id, target_id)
    requirements = str((card.get("body") or {}).get("requirements") or "")
    if old_string == "" and _is_blank_text(requirements):
        # [空块首次写入 2026-10-29] 块空着没有原文可定位，空串＝直接落内容。
        new_requirements = str(new_string)
        mode = "insert"
    elif old_string == "":
        return err("validation",
                   "old_string 给的是空串，但需求文档已有内容——空串只用于**空块首次写入**。"
                   "要改已有内容：old_string 填要替换的那段原文（先 read_card 核对）。")
    else:
        n = requirements.count(str(old_string))
        if n == 0:
            return err("validation", "old_string 在需求文档块里找不到,未作任何修改。"
                       "当前块前 200 字:" + requirements[:200])
        if n > 1:
            return err("validation",
                       f"old_string 出现了 {n} 次,必须唯一才能改;请带前后文缩小范围")
        new_requirements = requirements.replace(str(old_string), str(new_string), 1)
    mode_label = "·首次写入" if mode == "insert" else "·精准编辑"
    note = str(arguments.get("note") or "").strip()
    cur = await card_store.read_card(work_id, current_id)
    cur_title = (cur.get("meta") or {}).get("title", "")
    upd = await card_store.update_card(
        work_id, target_id, {"requirements": new_requirements},
        f"总裁「{cur_title}」执笔" + (f"·{note}" if note else "")
        + mode_label + "需求文档")
    if isinstance(upd, dict) and upd.get("error"):
        return err("internal", str(upd["error"]))
    # 决策簿留痕:verdicts(判词簿)
    try:
        await function_store.add_verdict(
            work_id, current_id, "speak",
            f"策展 {target_id} 引导块{mode_label}"
            + (f"·{note}" if note else ""),
            sources=[], targets=[target_id],
            evidence=[(new_requirements[:200])], auto=False)
    except Exception:  # noqa: BLE001 留痕失败不挡执笔
        pass
    # [头尾介入 2026-10-25] 头事件**不在本 handler 发**——挂在底层写入路径
    # (card_store.update_card 的 requirements 首次有实质内容),任何执笔者/任何层
    # 都覆盖(用户令:阶段任务=任意层的子卡,不只总裁策展的那些)。
    return {"card_id": target_id, "mode": mode, "bytes": len(new_requirements)}


async def _h_confirm_with_user(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[P5] 总裁向用户发起确认:把待确认前提(或任何需要用户裁定的事)作为
    请求落进回合回复——用户在总裁卡(根卡)上直接看到并回答。
    执行层双保险:只认总裁。留痕=verdicts(决策簿)。"""
    if not _is_ceo_curator(work_id, current_id):
        return err("denied", "只有总裁能向用户发起确认")
    subject = str(arguments.get("subject") or "").strip()
    items = [str(x).strip() for x in (arguments.get("premises") or []) if str(x).strip()]
    if not subject and not items:
        return err("validation", "subject 或 premises 至少给一样")
    lines = ["〔待用户确认〕" + (subject or "以下前提需要你裁定")]
    for i, it in enumerate(items, 1):
        lines.append(f"  {i}. {it}")
    if arguments.get("context"):
        lines += ["", f"背景:{str(arguments['context'])[:800]}"]
    try:
        await function_store.add_verdict(
            work_id, current_id, "speak",
            "向用户发起确认:" + (subject or ";".join(items)[:120]),
            sources=[], targets=["user"], evidence=items[:8], auto=False)
    except Exception:  # noqa: BLE001
        pass
    from . import event_stream
    event_stream.emit(work_id, current_id, "ceo_confirm_requested", {
        "subject": subject, "premises": items[:8],
    })
    return {"asked": True, "premises": len(items), "text": "\n".join(lines)}


async def _h_report_to_user(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[P5] 总裁的中间汇报:落汇报簿(影子 ## 进度 + verdicts)+事件帧,
    用户界面可见。执行层双保险:只认总裁。"""
    if not _is_ceo_curator(work_id, current_id):
        return err("denied", "只有总裁能向用户汇报")
    content = str(arguments.get("content") or "").strip()
    if not content:
        return err("validation", "content 不能为空")
    try:
        await shadow_store.append_to_section(
            work_id, current_id, "## 进度",
            [f"- [{function_room_summary_ts()}] 汇报:{content[:400]}"])
    except Exception:  # noqa: BLE001
        pass
    try:
        await function_store.add_verdict(
            work_id, current_id, "speak",
            "向用户汇报:" + content[:120],
            sources=[], targets=["user"], evidence=[], auto=False)
    except Exception:  # noqa: BLE001
        pass
    from . import event_stream
    # [harness 第 1 步 2026-10-28] 本条**没有** kind 可补：`report_to_user` 的入参
    # 只有 content（总裁对用户的中间汇报不分 done/phase/blocked）。
    # 汇报的 kind 归 `msg_report` 事件（`_h_report_up` 发信时带 extra={"kind":…}），
    # 不在这一条上硬塞一个不存在的字段。
    event_stream.emit(work_id, current_id, "ceo_report", {
        "content": content[:2000],
    })
    return {"reported": True, "chars": len(content)}


def function_room_summary_ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


TOOL_REGISTRY["report_up"] = ToolDef(
    schema={
        "name": "report_up",
        "description": (
            "(向上汇报)**完成一段工作后，向你的上一级交代结果**——只发你的直接父卡，"
            "收件人由系统算，你不能选。\n"
            "**只有汇报这一条路，没有请示/裁定**：汇报得不好，上级自然会给你下指令；"
            "你自己职权内的事，自己定，不要报上来问。\n"
            "写什么：做成了什么、证据在哪能验、结论是什么、还剩什么没做。"
            "点名事实与证据，不自述成功，不写打算。\n"
            "什么时候用：一段工作做完（或做不下去）时。过程性进展不要报，"
            "攒到一段结束一次说清；横向告知别人用 sync，不要用汇报。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "汇报正文(非空,写事实与证据)"},
            },
            "required": ["content"],
        },
    },
    handler=_h_report_up,
    readonly=False,
    doc=ToolDoc(
        group="通信",
        scene="一段工作做完（或做不下去）时，向上一级交代结果——作业卡**唯一的"
              "向上通道**，只到直接父卡。过程性进展不要报，攒到一段结束一次说清。",
        usage="正文写**事实与证据**（做成了什么、在哪能验、结论是什么），"
              "不自述成功。**没有 kind、没有请示**：要别人做事用 direct，"
              "告诉别人一件事用 sync。",
        # [2026-10-27 改] 已补落会话块：正文进上下文、可被凝练。
        # 声明跟着实现走（这是「对照事实」的意义——改完声明自动变，不用改文字）。
        effect=ToolEffect(sink="session_block", wake=True, targets="parent"),
    ),
)

# [差事卡 2026-11-01] 差事回执：职能向派遣方交差——收件人由差事账算
# （delegator），不在树里的职能唯一上行线；report_up 认父子线、它认差事账。
TOOL_REGISTRY["report"] = ToolDef(
    schema={
        "name": "report",
        "description": (
            "（差事交差）向这次差事的**派遣方**汇报结论——正文落它的会话并唤醒。"
            "收件人由差事账算，你不能选；写结论与证据（逐条判了什么、证据在哪），"
            "不写过程自述。只有在派差事的职能能用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string",
                            "description": "回执正文（非空：结论＋证据）"},
            },
            "required": ["content"],
        },
    },
    handler=_h_report_deploy,
    readonly=False,
    doc=ToolDoc(
        group="通信",
        scene="差事办完交差时：把结论打回派遣方——它是你这场差事唯一的上行线。",
        usage="正文写结论＋证据（逐条过了没有、证据在哪、差什么）。交差即完，"
              "差事自动关账；怎么改归派遣方裁，不归你。",
        effect=ToolEffect(sink="session_block", wake=True, targets="delegator"),
    ),
)

TOOL_REGISTRY["curate_face"] = ToolDef(
    schema={
        "name": "curate_face",
        "description": (
            "(总裁专用)执笔任意卡的**需求文档**。你的统筹决策落在这里。"
            "**只有精准编辑一种写法**([2026-10-28] 整块写通道已取消):"
            "old_string+new_string,原文须在块里唯一匹配;找不到或多处 ⇒ 拒且零改动。"
            "要新增一段:把某段原文换成「原文＋新段」;要删:new_string 给空串。"
            "每次执笔自动进决策簿留痕。引导块的笔在你手里——各职能的观点经汇聚由你落面。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "目标卡 id"},
                "old_string": {"type": "string",
                               "description": "精准编辑:被替换原文(块内唯一)"},
                "new_string": {"type": "string", "description": "精准编辑:替换成什么"},
                "note": {"type": "string", "description": "一句话决策依据(进决策簿)"},
            },
            "required": ["card_id"],
        },
    },
    handler=_h_curate_face,
    readonly=False,
)

TOOL_REGISTRY["confirm_with_user"] = ToolDef(
    schema={
        "name": "confirm_with_user",
        "description": (
            "(总裁专用)向用户发起确认。凡「不是用户明示、也非物理不可绕」的"
            "推定前提,拆解后一律列出来向用户确认，不默认成立;"
            "用户在你的卡上直接看到并回答。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "这次确认的主题(一句话)"},
                "premises": {"type": "array", "items": {"type": "string"},
                             "description": "待确认前提清单,一条一句"},
                "context": {"type": "string", "description": "背景说明(可选)"},
            },
        },
    },
    handler=_h_confirm_with_user,
    readonly=False,
)

TOOL_REGISTRY["report_to_user"] = ToolDef(
    schema={
        "name": "report_to_user",
        "description": (
            "(总裁专用)向用户做中间汇报——按需主动汇报,不等被问;落汇报簿留痕,"
            "用户界面可见。汇报现状、进展与风险;细节在卡面,不整段搬运。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "汇报正文"},
            },
            "required": ["content"],
        },
    },
    handler=_h_report_to_user,
    readonly=False,
)



# [通信收拢 2026-10-28] `scene_report` **退役**：它与 `report_up` 是同一件事
# （同一收件人算法、同一落点），留着只会让模型在两个名字之间挑。
# handler `_h_scene_report` 保留（内部/测试直调），不再注册。

TOOL_REGISTRY["sync"] = ToolDef(
    schema={
        "name": "sync",
        "description": (
            "(同步)**让对方知道一件事，但不需要它现在动**——沿通信线送达，"
            "进它上下文，**不叫醒它**（它下次回合自然看到）。\n"
            "适合：改了接口/定了规范/状态变了/回答它问过你的问题。\n"
            "不适合：要它做事（用 direct）；向上交代（用 report_up）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要同步的事(常是你卡面的变化；非空)"},
                "card_ids": {"type": "array", "items": {"type": "string"},
                             "description": "收件人(缺省＝沿你的通信线全部送达)"},
            },
            "required": ["content"],
        },
    },
    handler=_h_scene_sync,
    readonly=False,
    doc=ToolDoc(
        group="通信",
        scene="要让对方**知道**一件事、但不需要它现在动的时候。"
              "要它立刻做，用 direct，不要用这个。",
        usage="收件人缺省沿通信线算，也可显式给 card_ids（每个都须有线，否则进 refused）。"
              "落进对方上下文（永久可读），**不叫醒它**——它下次回合自然看到。"
              "正文写事（发生了什么/变了什么），不写要求它做的事（那是 direct 的事）。",
        effect=ToolEffect(sink="session_block", wake=False,
                          targets="explicit|links"),
    ),
)

TOOL_REGISTRY["direct"] = ToolDef(
    schema={
        "name": "direct",
        "description": (
            "(指令)**下达任务：要对方现在就做一件事**——沿通信线送达、进它上下文、"
            "并**把它叫起来跑一轮**。\n"
            "必须给验收判据（acceptance）——没有判据，做完也没法判「做完了没有」。\n"
            "只能对你自己子树内的卡下达（权限线），且两卡之间须有通信线。\n"
            "不适合：只是让它知道（用 sync）；向上交代（用 report_up）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "收令的卡"},
                "instruction": {"type": "string", "description": "要它做什么(非空,可执行)"},
                "acceptance": {"type": "string", "description": "验收判据(怎么算做到)"},
            },
            "required": ["card_id", "instruction", "acceptance"],
        },
    },
    handler=_h_scene_direct,
    readonly=False,
    doc=ToolDoc(
        group="通信",
        scene="要对方**现在就做**一件事，而且它在你的子树内、与你有通信线。"
              "只是让它知道、不需要它动，用 sync。",
        usage="必须给 acceptance（验收判据）——没有判据，它做完你也没法判"
              "「做完了没有」。只能对子树内的卡下达，跨枝会被拒。"
              "落进对方上下文并**叫醒它跑一轮**。"
              "**验收不需要另一个工具**：通过就不说话，不通过就再发一条 direct 要求改。",
        effect=ToolEffect(sink="session_block", wake=True, targets="explicit"),
    ),
)

# [2026-10-28] 写现状＝写正文，与 edit_card 同一开关（perms 的 edit 位）。
TOOL_SWITCH["edit_status"] = policy.SWITCH_EDIT
TOOL_SWITCH["report_up"] = policy.SWITCH_READ          # 汇报权人人有(挂读权,不走发信开关)
TOOL_SWITCH["review_request"] = policy.SWITCH_READ
TOOL_SWITCH["submit_review"] = policy.SWITCH_READ
# [通信收拢 2026-10-28] scene_report 已退役（与 report_up 同一件事），开关映射同步删。
TOOL_SWITCH["sync"] = policy.SWITCH_READ
TOOL_SWITCH["direct"] = policy.SWITCH_READ
TOOL_SWITCH["curate_face"] = policy.SWITCH_EDIT
TOOL_SWITCH["confirm_with_user"] = policy.SWITCH_READ
TOOL_SWITCH["report_to_user"] = policy.SWITCH_READ

# [强制停止 2026-10-24] 根卡/父卡的打断与停止工具(权限=祖先,handler 双保险)
TOOL_REGISTRY["stop_card"] = ToolDef(
    schema={
        "name": "stop_card",
        "description": (
            "(根卡/父卡专用)打断或强制停止你子树内的一张卡(总裁可停全树)。\n"
            "mode=interrupt(打断):只终止它当前这一轮——用于那轮跑偏/卡住,"
            "它之后照常继续;\n"
            "mode=halt(停止,缺省,**强制性**):终止当轮 + 暂停它的目标"
            "(连同其子树) + 拒绝一切内部唤醒(收件箱/汇聚都叫不醒),"
            "直到用户发话或 resume_child 恢复。\n"
            "reason 必写:为什么停(留痕,恢复时看得到)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "目标卡 id"},
                "mode": {"type": "string", "enum": ["interrupt", "halt"],
                         "description": "打断(只杀当轮)/停止(强制,缺省)"},
                "reason": {"type": "string", "description": "停止原因(必填,留痕)"},
            },
            "required": ["card_id", "reason"],
        },
    },
    handler=_h_stop_card,
    readonly=False,
)
TOOL_SWITCH["stop_card"] = policy.SWITCH_READ

# [沉默 2026-10-24] 完工卡的静音/激活(持久治理状态,权限=祖先)
TOOL_REGISTRY["silence_card"] = ToolDef(
    schema={
        "name": "silence_card",
        "description": (
            "(根卡/父卡专用)让**完工的卡**沉默,或重新激活它。\n"
            "action=silence(沉默,缺省):该卡此后**不再接收任何内部消息**"
            "(收件箱的信会留存但不投递、也不唤醒它)——完工的卡不该再被"
            "后续消息搅扰;持久生效,重启不失效。\n"
            "action=activate(激活):解除沉默,恢复接收(积压的信会按增量"
            "投递补上)。\n"
            "worker 卡完工后**不折叠**:它的上下文是延续资产——续派任务用 "
            "continue_task。\n"
            "只对 state=completed 的卡生效——在跑的卡要停用 stop_card。\n"
            "reason 必写:为什么沉默/激活(留痕)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "目标卡 id"},
                "action": {"type": "string", "enum": ["silence", "activate"],
                           "description": "沉默/激活(缺省 silence)"},
                "reason": {"type": "string", "description": "原因(必填,留痕)"},
            },
            "required": ["card_id", "reason"],
        },
    },
    handler=_h_silence_card,
    readonly=False,
)
TOOL_SWITCH["silence_card"] = policy.SWITCH_READ

# [任务延续 2026-10-25 · 用户裁定] worker 卡完工后不折叠——续派下一件任务,
# 上下文延续(代码/回执/会话全保留)。根卡/父卡下发。
TOOL_REGISTRY["continue_task"] = ToolDef(
    schema={
        "name": "continue_task",
        "description": (
            "(根卡/父卡专用)给完工或暂停的worker 卡**续派下一件任务**——"
            "同一张卡接着干。\n"
            "为什么续派而不是新建:该卡之前交付的代码、完工回执、会话上下文"
            "**全部延续**——后续任务与已交付物相关时(返工/迭代/同域下一件),"
            "续派远优于新建(新建=上下文全断)。全新领域才值得新开一张。\n"
            "机械动作:卡复位 active+目标开新生命周期(任务文本即新目标)+点火。"
            "task 写清下一件任务是什么、验收判据是什么。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "目标worker 卡 id"},
                "task": {"type": "string",
                         "description": "下一件任务(写清做什么、验收判据)"},
                "note": {"type": "string", "description": "备注(可选,留痕)"},
            },
            "required": ["card_id", "task"],
        },
    },
    handler=_h_continue_task,
    readonly=False,
)
TOOL_SWITCH["continue_task"] = policy.SWITCH_READ

# [职能唤醒收口 2026-10-28] 阶段任务的头——模型显式调用，**只做标记**。
# 改前它会给全部在职职能直发消息做「立项评审」（每开一个阶段叫醒四个职能
# 各跑一整轮）；用户令「摘掉评审之外的唤醒」「立项评审不算」，已摘除。
TOOL_REGISTRY["stage_open"] = ToolDef(
    schema={
        "name": "stage_open",
        "description": (
            "(根卡/父卡专用)**标记一张子卡的阶段任务开始**——子卡的需求文档"
            "定稿(正稿)之后调用,告诉系统「这张卡从这一刻起进入执行阶段」。\n"
            "本工具**只做标记**：不改需求、不改权限、不召集任何人。"
            "需求评审是另一个动作(子卡自己调 open_review),与本工具无关。\n"
            "调用前提:目标卡的需求文档已有**正稿**(评审通过产生)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string",
                            "description": "阶段任务子卡的 id"},
                "summary": {"type": "string",
                            "description": "一句话说明本阶段(可选,进评审材料)"},
            },
            "required": ["card_id"],
        },
    },
    handler=_h_stage_open,
    readonly=False,
)
TOOL_SWITCH["stage_open"] = policy.SWITCH_READ

# [归档 2026-10-25 · 用户令「让所有卡都可以归档」] 任何卡都能收起来——
# 不看类型、不看 state、不看有没有子卡。三条处置语义并列、互不替代：
#   delete_card   ＝处置/清理（进回收站，会话一并删）
#   archive_card  ＝收起来（保留三块/历史/影子，会话原位不动，可恢复）
#   silence_card  ＝完工卡静音（卡还在树上，只是不再被唤醒）
TOOL_REGISTRY["archive_card"] = ToolDef(
    schema={
        "name": "archive_card",
        "description": (
            "把一张卡**归档**：卡面从树上收起，折叠成一条归档记录"
            "（三块／修改历史／影子都存下来），会话与事件记录原位保留。\n"
            "**任何卡都能归档**——不看卡类型、不看是否完工、不看有没有子卡。\n"
            "用于「过程有价值、但不需要继续」的卡：不再占树、不再参与协作，"
            "但随时能查（read_archive）、能恢复（unarchive_card）。\n"
            "要彻底删除（进回收站）用 delete_card；要静音完工卡用 silence_card。\n"
            "reason 必写：为什么归档（留痕，恢复时看得到）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "目标卡 id"},
                "reason": {"type": "string", "description": "归档原因（必填，留痕）"},
            },
            "required": ["card_id", "reason"],
        },
    },
    handler=_h_archive_card,
    readonly=False,
)
TOOL_SWITCH["archive_card"] = policy.SWITCH_DELETE

TOOL_REGISTRY["unarchive_card"] = ToolDef(
    schema={
        "name": "unarchive_card",
        "description": (
            "把归档的卡恢复成活卡（三块与修改历史写回；会话与事件一直在，无需恢复）。"
            "卡号不变——正文里对它的引用继续有效。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "归档记录里的卡号"},
            },
            "required": ["card_id"],
        },
    },
    handler=_h_unarchive_card,
    readonly=False,
)
TOOL_SWITCH["unarchive_card"] = policy.SWITCH_DELETE

# [worker 归档 2026-11-05 改名前为 coder 归档] 归档记录读口(记录全局可读——与 read_card 读侧同口径)
TOOL_REGISTRY["read_archive"] = ToolDef(
    schema={
        "name": "read_archive",
        "description": (
            "读**归档记录**（任何卡的归档，以及更早的worker 卡归档）。归档卡不再是"
            "活卡——read_card 查不到它们，这里是唯一查口。\n"
            "card_id=指定卡号 ⇒ 返回单条；parent=指定父卡 ⇒ 列其名下全部；"
            "都不给 ⇒ 列本 work 全部。\n"
            "记录含：三块正文（mission／status_block／requirements）、修改历史、影子三节、"
            "card_type、archived_at、reason；更早的worker 卡记录是 task／final_report／"
            "artifacts 形态。要回溯更细的过程，用时间线（事件文件仍在）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "按卡号取单条"},
                "parent": {"type": "string",
                           "description": "按父卡列其名下全部归档记录"},
            },
        },
    },
    handler=_h_read_archive,
    readonly=True,
)
TOOL_SWITCH["read_archive"] = policy.SWITCH_READ

# ==========================================================================
# [能力清单 2026-10-30 · 恢复] 这 6 件的注册被误删（handler 都在），按 13:46 的
# 编译产物逐字恢复：schema 原样、doc 原样。缺了它们，精准编辑与盯梢策略在模型面
# 上等于不存在（handler 成了死代码）。
# ==========================================================================

TOOL_REGISTRY["edit_status"] = ToolDef(
    schema=
        {'name': 'edit_status',
         'description': '**精准编辑本卡的「现状」块**——只改这一处，使命与需求文档一个字不碰。\n'
                        '## 现状是**快照**，不是流水账\n'
                        '现状回答一个问题：**这张卡现在实际是什么样**。所以它只有一份最新读数：同一件事只留一条，作废的要删掉，不要留着写「（已过期）」。\n'
                        '**不许只做增量追加**：往末尾堆一段新的，旧段落还留在上面 ⇒ 读卡的人分不清哪段是现在，卡面越写越长（实测有卡堆到 86 万字，现状块单次写 '
                        '2100–3400 字）。正确做法是**改旧的那一句**（替换成最新读数）或**删掉作废的那条**，不是新起一段。\n'
                        '## 写什么\n'
                        '能核实的事实：已经做成了什么（交付物在哪、能跑的命令、跑出什么结果）、对外接口是什么、现在卡在哪。不写计划、不写「我打算怎么做」、不写需求（需求走 '
                        'edit_requirements）；过程、备忘、判断依据也不写——那些写影子的「笔记」节（edit_note）。\n'
                        '## 作废的**删掉**，不要留着标「已过期」\n'
                        '现状块只放**现在成立**的东西。一条读数过时了 ⇒ 直接换成新的；一条作废了 ⇒ `new_string` '
                        '给空串删掉。**不要写成「旧：X（已过期）／新：Y」并列**——那样块会越滚越长，读卡的人还得自己判断哪条算数。\n'
                        '**历史不在这里**：每次写入系统自动留一版旧内容（每卡最近 20 版，可 list_versions '
                        '回退），过程记在影子的「迭代历史」节。块是**快照**，账本在别处。\n'
                        '## 怎么定位\n'
                        '①old_string 按内容——必须在现状块里出现；找不到 ⇒ 拒（零改动，先 read_card 核对），出现多次 ⇒ 拒（多带上下文，或 '
                        'replace_all=true）；**现状块还空着时给空串＝首次写入**；②lines / insert_after '
                        '按行——lines="12" 或 "12-15" 替换该区间（new_string 为空＝删掉这几行），insert_after=12 在第 12 '
                        '行后插入。\n'
                        '删掉一处就给 new_string=""。\n'
                        '## 回执\n'
                        '带改了第几行、改前改后片段、落面后的现状块全文——写完不用再 read_card 回读。',
         'parameters': {'type': 'object',
                        'properties': {'card_id': {'type': 'string',
                                                   'description': '目标卡 id（缺省＝当前卡）'},
                                       'old_string': {'type': 'string',
                                                      'description': '要替换的原文（必须在现状块里出现；多带上下文保证唯一）。现状块还空着时给空串＝首次写入'},
                                       'new_string': {'type': 'string',
                                                      'description': '换成什么（＝最新读数）；要删掉这一处就给空串'},
                                       'replace_all': {'type': 'boolean',
                                                       'description': 'true＝块里所有出现处都换掉（缺省只换一处）'},
                                       'lines': {'type': 'string',
                                                 'description': '按行定位：12 或 12-15（1 '
                                                                '起算、含两端）。行号见上下文该块的标号（形如 12│ '
                                                                '内容）——**不用抄原文**。与 old_string 二选一'},
                                       'insert_after': {'type': 'integer',
                                                        'description': '在第 N 行后插入（0＝块首）；与 '
                                                                       'old_string/lines 三选一'}},
                        'required': []}},
    handler=_h_edit_status,
    readonly=False,
    doc=ToolDoc(
        group='写卡面',
        scene='现状过时了、或这一步做完了要更新读数时——改的是**过时的那一句**，不是往后加一段。现状是快照：同一件事只留一条最新读数。',
        usage='old_string＋new_string 定位一处；找不到 ⇒ 拒且零改动；要删就 new_string 给空串。写能核实的事实，不写计划。',
        effect=ToolEffect(
            sink='card', wake=False, wire=False,
            targets='self', delegate='edit_card',
        ),
    ),
)
TOOL_SWITCH["edit_status"] = policy.SWITCH_EDIT

TOOL_REGISTRY["edit_requirements"] = ToolDef(
    schema=
        {'name': 'edit_requirements',
         'description': '**精准编辑「需求文档」块**——只改这一处，现状与使命一个字不碰。\n'
                        '## 这块写什么\n'
                        '**要什么 + 怎么算做到**：一条需求一条验收标准。不写设想、不写「我打算怎么做」、不写现状（现状是事实，用 '
                        'edit_status）；过程、备忘、「为什么定/何时定」也不写——判断依据写影子的「笔记」节（edit_note），改动本身自动留痕；对别人成立的决定落成条文本身，不写「某日裁定」。\n'
                        '## **只有「确定的需求」可以写进来**\n'
                        '这块是**验收判据**——写进来一条，就等于宣布「做不到这条就是没完成」。所以：\n'
                        '- **确定**＝已经定了、要按它验收：用户明示过／上级下达过／从使命与既有需求直接推得、不需要再问。\n'
                        '- **不确定的不要写**：猜测的、待确认的、可选的、「可能还要」的——写了它就变成判据，会把这张卡逼进「永远差一条」的死局。\n'
                        '- '
                        '不确定的东西写哪儿：**现在卡在哪**写进现状（edit_status）、待办与依赖写进影子的「待协作事项」、要上级拍板就直接问（direct）。\n'
                        '- 自检一句：**「做不到这一条，算不算没完成？」** 算 ⇒ 是确定需求，写；不算 ⇒ 别写。\n'
                        '## 怎么改：先找「该改/该删的那一条」，新增是例外\n'
                        '需求文档是**现行要求**（现在要求做到什么），不是需求变更史。所以：\n'
                        '- **改**：直接替换那条的需求或判据句；\n'
                        '- **删**：`new_string` 给空串（这条不再要求了）；\n'
                        '- **取代**：新要求顶掉旧的 ⇒ **把旧那条换掉**，不要写成「旧：X（已作废）／新：Y」并列；\n'
                        '- **新增**：确实是一条以前没有的要求才加——把相邻那条原文换成「原文＋新条」，**并同时删掉被它取代的旧条**（同一件事不要留两条）。\n'
                        '**历史不在这里**：每次写入自动留一版（每卡 20 版，可回退），过程在影子的「迭代历史」。\n'
                        '①old_string 按内容——必须在块里出现；找不到 ⇒ 拒（零改动，先 read_card 核对），出现多次 ⇒ 拒（多带上下文，或 '
                        'replace_all=true）；**块还空着时给空串＝首次写入**；②lines / insert_after 按行——lines="12" '
                        '或 "12-15" 替换该区间，insert_after=12 在第 12 行后插入。\n'
                        '## 回执\n'
                        '带改了第几行、改前改后片段、落面后的需求文档块全文——写完不用再 read_card 回读。',
         'parameters': {'type': 'object',
                        'properties': {'card_id': {'type': 'string',
                                                   'description': '目标卡 id（缺省＝当前卡）'},
                                       'old_string': {'type': 'string',
                                                      'description': '要替换的原文（必须在需求文档块里出现）。块还空着时给空串＝首次写入'},
                                       'new_string': {'type': 'string',
                                                      'description': '换成什么；要删掉这一处就给空串'},
                                       'replace_all': {'type': 'boolean',
                                                       'description': 'true＝块里所有出现处都换掉（缺省只换一处）'},
                                       'lines': {'type': 'string',
                                                 'description': '按行定位：12 或 12-15（1 '
                                                                '起算、含两端）。行号见上下文该块的标号（形如 12│ '
                                                                '内容）——**不用抄原文**。与 old_string 二选一'},
                                       'insert_after': {'type': 'integer',
                                                        'description': '在第 N 行后插入（0＝块首）；与 '
                                                                       'old_string/lines 三选一'}},
                        'required': []}},
    handler=_h_edit_requirements,
    readonly=False,
    doc=ToolDoc(
        group='写卡面',
        scene='需求要加一条、改一条、删一条，或改验收判据时——改的是**那一条**，不是往末尾堆。**只写确定的需求**（写进来就是验收判据）。',
        usage='old_string＋new_string 定位一处（找不到 ⇒ 拒且零改动）；新增一条＝把相邻原文换成「原文＋新条」；删＝new_string 给空串。写「要什么＋怎么算做到」，不写过程、不写现状。',
        effect=ToolEffect(
            sink='card', wake=False, wire=False,
            targets='self', delegate='edit_card',
        ),
    ),
)
TOOL_SWITCH["edit_requirements"] = policy.SWITCH_EDIT

TOOL_REGISTRY["edit_shadow"] = ToolDef(
    schema=
        {'name': 'edit_shadow',
         'description': '**精准编辑你自己（当前卡）影子的某一节**——只改指定节里的一处，其余三节与笔记一个字不碰。\n'
                        '## 影子是什么\n'
                        '**过程记录**：正卡写「事实与需求」，影子写「怎么走到这儿的」。别的卡读不到（本卡私有）。\n'
                        '## 四节各放什么（不得越界、不得改节名、不得加节）\n'
                        '· **进度**：本卡推进的时间线——什么时间、做到哪一步、当前什么状态。不写结论/分析、不写无关杂事。\n'
                        '· **待协作事项**：需要其他卡/模块提供的输入、待办、阻塞、协作关系。不放产物位置（那是产物清单）、不放改动记录（那是迭代历史）。\n'
                        '· **产物清单**：本卡产生的文件/代码/文档的**路径＋一句话用途**，一行一个；只写指针，不搬运内容。\n'
                        '· **迭代历史**：改动指针（改了什么、为什么改）；细节回既有通道查，不在此复述。\n'
                        '## 三条禁止\n'
                        '① 不把正卡内容写进影子（正卡＝事实与需求，影子＝过程，不得混）；② 不写与本卡推进无关的内容；③ 不把影子当备忘录堆杂事。\n'
                        '· section 取 进度／待协作事项／产物清单／迭代历史。\n'
                        '· old_string 必须在该节里出现：找不到 ⇒ 拒（该节零改动）；出现多次 ⇒ 拒（带上更多上下文让它唯一，或 '
                        'replace_all=true）。\n'
                        '· **该节还空着时 old_string 给空串＝首次写入**。\n'
                        '· 要删掉这一处就给 new_string=""。\n'
                        '· **没有整块覆盖的通道了**——这是有意的：整块写一次手滑就清空四节。\n'
                        '· 回执带改了第几行、改前改后片段、以及该节改完的全文。',
         'parameters': {'type': 'object',
                        'properties': {'section': {'type': 'string',
                                                   'enum': ['进度', '待协作事项', '产物清单', '迭代历史'],
                                                   'description': '改哪一节（一次只改一节）'},
                                       'old_string': {'type': 'string',
                                                      'description': '要替换的原文（必须在该节里出现）。该节还空着时给空串＝首次写入'},
                                       'new_string': {'type': 'string',
                                                      'description': '换成什么；删掉这一处就给空串'},
                                       'replace_all': {'type': 'boolean',
                                                       'description': 'true＝该节里所有出现处都换掉（缺省只换一处）'}},
                        'required': ['section', 'old_string', 'new_string']}},
    handler=_h_edit_shadow,
    readonly=False,
    doc=ToolDoc(
        group='写卡面',
        scene='过程记录要更新时——做到哪了（进度）／在等谁（待协作事项）／产出了什么（产物清单）／改过几轮（迭代历史）。这是本卡的过程账，别人要看你做到哪，先读现状，要看你**怎么做的**才读影子。',
        usage='按 section ＋ old_string 定位改一处，其余三节与笔记一个字不动。**没有整块覆盖通道**——这是有意的：整块写一次手滑就清空四节。',
        effect=ToolEffect(
            sink='shadow', wake=False, wire=False,
            targets='', delegate='',
        ),
    ),
)
TOOL_SWITCH["edit_shadow"] = policy.SWITCH_SHADOW_WRITE

TOOL_REGISTRY["edit_note"] = ToolDef(
    schema=
        {'name': 'edit_note',
         'description': '**精准编辑你自己（当前卡）的笔记**——影子第五区，只有你看得到，别的卡读不到。它记**判断、备忘、待验证的猜想**——写给自己的，常驻你的上下文，下一轮还读得到。给出 old_string（要替换的原文）与 '
                        'new_string（换成什么），**只动这一处**，其余字节原样保留。\n'
                        '· 找不到 ⇒ 拒，笔记零改动；出现多次 ⇒ 拒（带上更多上下文让它唯一，或 replace_all=true）。\n'
                        '· 要删掉这一处就给 new_string=""。\n'
                        '· **笔记还空着时 old_string 给空串＝首次写入**。\n'
                        '· 笔记常驻你的上下文（【笔记】节），写之前不必 read_shadow。\n'
                        '· 回执带改了第几行、改前改后片段、以及改完的笔记全文。',
         'parameters': {'type': 'object',
                        'properties': {'old_string': {'type': 'string',
                                                      'description': '要替换的原文（必须在笔记里出现）。笔记还空着时给空串＝首次写入'},
                                       'new_string': {'type': 'string',
                                                      'description': '换成什么；删掉这一处就给空串'},
                                       'replace_all': {'type': 'boolean',
                                                       'description': 'true＝笔记里所有出现处都换掉（缺省只换一处）'}},
                        'required': ['old_string', 'new_string']}},
    handler=_h_edit_note,
    readonly=False,
    doc=ToolDoc(
        group='写卡面',
        scene='有只给自己看的判断、线索、待验证的猜想——写进卡面会把别人的视野搅乱时。笔记是**本卡私有**：别的卡读不到，连 read_linked 也读不到。',
        usage='按 old_string 定位改一处，只动那一处。笔记常驻你自己的上下文，写之前不用 read_shadow。别当草稿纸堆流水——它是给之后的自己留的线索。',
        effect=ToolEffect(
            sink='shadow', wake=False, wire=False,
            targets='', delegate='',
        ),
    ),
)
TOOL_SWITCH["edit_note"] = policy.SWITCH_SHADOW_WRITE

TOOL_REGISTRY["write_requirements_final"] = ToolDef(
    schema=
        {'name': 'write_requirements_final',
         'description': '**写本卡需求文档的正稿**——只在这一轮（【需求确定】唤起的那一轮）有这件工具。\n'
                        '平时写需求只能写草稿、等评审通过才转正稿；本卡是根卡，没有评审：这份正稿由用户当面定，所以这一轮直接改正稿。\n'
                        '精准编辑：给 old_string（要替换的原文）与 new_string（换成什么），**只动这一处**，其余字节原样保留。\n'
                        '· 正稿还空着时 old_string 给空串＝首次写入。\n'
                        '· 找不到 ⇒ 拒（正稿零改动，先 read_card 核对原文）；出现多次 ⇒ 拒（带上更多上下文让它唯一，或 '
                        'replace_all=true）。\n'
                        '· 要删掉这一处就给 new_string=""。\n'
                        '· 回执带改了第几行、改前改后片段、以及落面后的正稿全文。',
         'parameters': {'type': 'object',
                        'properties': {'old_string': {'type': 'string',
                                                      'description': '要替换的原文（必须在正稿里出现）。正稿还空着时给空串＝首次写入。用 '
                                                                     'lines 定位时不必给'},
                                       'new_string': {'type': 'string',
                                                      'description': '换成什么；要删掉这一处就给空串'},
                                       'replace_all': {'type': 'boolean',
                                                       'description': 'true＝正稿里所有出现处都换掉（缺省只换一处）'},
                                       'lines': {'type': 'string',
                                                 'description': '按行定位：12 或 12-15（1 '
                                                                '起算、含两端）。行号见上下文需求文档的 `12│ ` '
                                                                '标号——**不用抄原文**。与 old_string 二选一'},
                                       'insert_after': {'type': 'integer',
                                                        'description': '在第 N 行后插入（0＝块首）；与 '
                                                                       'old_string/lines 三选一'}},
                        'required': []}},
    handler=_h_write_requirements_final,
    readonly=False,
    doc=ToolDoc(
        group='写卡面',
        scene='根卡【需求确定】那一轮：把本卡需求文档的正稿定下来。',
        usage='old_string＋new_string 定位一处（找不到 ⇒ 拒且零改动）；新增一条＝把相邻原文换成「原文＋新条」；删＝new_string 给空串。',
        effect=ToolEffect(
            sink='card', wake=False, wire=False,
            targets='self', delegate='',
        ),
    ),
)
TOOL_SWITCH["write_requirements_final"] = policy.SWITCH_EDIT

TOOL_REGISTRY["design_policy"] = ToolDef(
    schema=
        {'name': 'design_policy',
         'description': '把事件组合成自定义触发条件；条件成立时，用消息 / 唤醒 / 提示词注入影响卡片内模型的行为。\n'
                        '目的是让协作更顺、卡片更聪明、活干得更准。\n'
                        '\n'
                        '配一条规则：\n'
                        '· watch   看哪些事件\n'
                        '· when    这些事件组成什么条件\n'
                        '· target  影响哪张卡里的模型\n'
                        '· message 写进它的对话历史，之后每轮都看得到\n'
                        '· wake    要不要立刻叫醒它跑一轮\n'
                        '\n'
                        '配完立刻生效；同一 id 再调一次就是改它。喊过一次要重新攒条件，不会一直重复。\n',
         'parameters': {'type': 'object',
                        'properties': {'id': {'type': 'string',
                                              'description': '策略名（本项目内唯一）。重复即覆盖。'},
                                       'desc': {'type': 'string',
                                                'description': '人话说明：这条在盯什么。触发记录里会带上。'},
                                       'scope': {'type': 'string',
                                                 'enum': ['card', 'relation', 'work'],
                                                 'description': '本子挂在谁身上：card＝一张卡一本；relation＝**一对父子**一本（盯父子互动时用）；work＝整个项目一本。'},
                                       'watch': {'type': 'array',
                                                 'items': {'type': 'string'},
                                                 'description': '只关心哪几类事件：填已登记的事件类型名数组。拼错会当场报错并列出全部合法类型名。'},
                                       'window': {'type': 'string',
                                                  'description': '时间窗：24h / 7d / 90m / '
                                                                 'none。超窗的旧账自动划掉。'},
                                       'when': {'type': 'object',
                                                'description': '条件树。对象，键决定节点类型，可用 all/any/not '
                                                               '任意嵌套：event＝{types: [类型名], where: '
                                                               '{字段: 值}}，账里至少一条满足；count＝{types, '
                                                               'where} 同层再加 cmp 与 value，满足的条数与 '
                                                               'value 比较；field＝同层给 path / cmp / '
                                                               'value，取上下文 work_id / card_id / '
                                                               'parent 的值比较；after＝{anchor: spec, '
                                                               'expect: spec}，最近一条 anchor 之后没有 '
                                                               'expect（anchor 从未出现算不满足）；all / '
                                                               'any＝节点数组，全部 / '
                                                               '任一满足；not＝单个节点，取反。spec = {types: '
                                                               '[...], where: {...}}；types '
                                                               '空＝不限类型，where 空＝不限字段。cmp: '
                                                               'eq/ne/gt/ge/lt/le/in/contains/exists/matches，也认 '
                                                               '>= <= > < == !=。'},
                                       'target': {'type': 'string',
                                                  'description': '提醒发给谁：self（自己）/ parent（父卡）/ '
                                                                 'card:<id>。'},
                                       'message': {'type': 'string',
                                                   'description': '提醒正文。可用 {card_id} {card_title} '
                                                                  '{parent} 占位。'},
                                       'mode': {'type': 'string',
                                                'enum': ['message', 'inject'],
                                                'description': '用哪种手段：message＝写进它的会话历史，永久，之后每轮都看得到；inject＝只进它**下一轮**的上下文，整轮有效、不进历史、不唤醒（等它下次自己跑时生效）。默认 '
                                                               'message。'},
                                       'wake': {'type': 'boolean',
                                                'description': '要不要把它叫起来跑一轮。true＝立刻处理；false＝只留一句话，等它下次跑时看到。（mode=inject '
                                                               '时无效——注入不唤醒。）'},
                                       'active': {'type': 'boolean',
                                                  'description': '是否启用（默认 true）。设 false '
                                                                 '等于先存着不生效。'}},
                        'required': ['id', 'watch', 'when', 'message']}},
    handler=_policy_tools.handle,
    readonly=False,
    doc=None,
)
TOOL_SWITCH["design_policy"] = policy.SWITCH_PLAN


# [两源模型 2026-11-01 · 用户令「卡面是唯一权威事实源，不需要台账」]
# write_ledger 整体退役：职能的专业准则写在 rules.md 的「## function.<code>」
# 区（人改的基建文件），不私记台账；产出落在场卡面（票/写件/回信），
# 不进第二事实源。


# ============================================================
# [card_031] 职能工具的 handler 主体
#
# 统一纪律：**先判身份，再动手**。
#   _function_actor → 返回该回合的 function_id（职能）或 role_id（旧角色卡），
#   两者皆非 ⇒ 空串 ⇒ 任何职能专用工具都拒。绝不在身份不明时写任何表。
# ============================================================


def _function_actor(work_id: str, card_id: str,
                    cards: dict[str, dict[str, Any]] | None = None) -> str:
    """这一回合是「谁」：职能号 ``fn_NNN`` 或旧角色卡号，非职能回合 ⇒ 空串。

    **旧 role 卡的判定必须与旧路径逐字同一口径**：`role_room.is_role()`
    自己查卡面 meta，**不依赖调用方传进来的 cards 字典**（实测踩坑：判据⑧ 里
    用一份过期的 cards 快照调职能专用工具，本函数因拿不到 card_type 而判
    非职能回合，把旧 role 卡错误地拒了——那是旧路径行为的回归）。
    故：cards 已给时先走它作快捷路径；**没给或没命中时仍回库自查一次**，
    与 role_room.is_role 同源同结果。
    """
    cid = str(card_id or "")
    if not cid:
        return ""
    if cid.startswith("fn_"):
        try:
            return cid if function_store.function_exists(work_id, cid) else ""
        except Exception:  # noqa: BLE001 查不到＝不是职能回合（fail-closed）
            return ""
    if str(((cards or {}).get(cid) or {}).get("card_type") or "") == "role":
        return cid
    # 快捷路径没命中 ⇒ 回库自查（旧 role_room.is_role 的同一口径），
    # 绝不因为「调用方没传 cards / 传了旧快照」就把旧 role 卡拒掉。
    try:
        if role_room.is_role(work_id, cid):
            return cid
    except Exception:  # noqa: BLE001 查不到＝不是职能回合（fail-closed）
        return ""
    return ""


async def ensure_function_card(work_id: str, function_id: str,
                               function_name: str) -> str:
    """给职能建/确认它的 cards 行。**id 就用 function_id（fn_NNN）**。

    [职能实体化 A 案 2026-10-22 · 用户裁定「用任务卡底层，但本质要和任务卡区分开」
     「不能是 card_id，这样会很乱」]
    底层是 cards 行（perms / build_ctx / _run_message / create_card 全按 card_id 焊死），
    但**身份就是 fn_NNN**：事件流、会话、档案、左栏、画布全部用同一个号，
    不出现 card_NNN。呈现层（卡列表/画布/统计）按 card_type='function' 过滤。

    [职能卡信息结构 2026-10-23 · 用户令「卡片信息结构不该和普通卡一样」]
    改前建卡**一个块都不传** ⇒ 落 `card_store.NEW_CARD_SKELETON` 的通用占位模板
    （「（本卡作用/目标、授权来源…）」那三句），卡面看起来和普通项目卡一模一样。
    角色卡不是这么建的——`role_room` 建卡时显式拼 body：

        ## 使命    ← 职责书全文
        ## 现状    ← 值班中（职责与台账见职能档案）
        ## 需求文档 ← 空

    职能卡照同一形态拼：使命＝档案里的职责书（charter.text），现状＝待命，
    需求文档留空（职能的"裁定"落在职能档案的 verdicts，不写卡面）。
    """
    fid = str(function_id or "")
    if not fid:
        return ""
    if card_store.card_exists(work_id, fid):
        return fid
    body = await _function_card_body(work_id, fid)
    r = await card_store.create_card(
        work_id, function_name or fid, "系统派生",
        card_type="function", card_id=fid, charter=body)
    if "error" in r:
        logger.warning("[function] 建职能卡失败 work=%s fn=%s: %s",
                       work_id, fid, r["error"])
        return ""
    return fid


async def _function_card_body(work_id: str, function_id: str) -> str:
    """职能卡的三块正文——与角色卡同形态（使命＝职责书 / 现状＝待命 / 需求文档空）。

    职责书从职能档案取（`function_profiles.charter.text`）。档案还没落库时
    给一句**明确的待补提示**（不是通用模板那三句），让卡面一眼看出这是职能卡、
    缺的是职责书。
    """
    charter = ""
    try:
        prof = function_store.get_profile(work_id, function_id) or {}
        ch = prof.get("charter") or {}
        if isinstance(ch, dict):
            charter = str(ch.get("text") or "").strip()
        elif isinstance(ch, str):
            charter = ch.strip()
    except Exception as exc:  # noqa: BLE001 取不到档案不该挡建卡
        logger.warning("[function] 读职能档案失败 work=%s fn=%s: %s",
                       work_id, function_id, exc)
    if not charter:
        charter = "（职责书待补：对什么负责、什么情况发言、什么情况沉默）"
    return (f"## 使命\n{charter}\n\n"
            "## 现状\n（待命；判断、警报与裁定见职能档案）\n\n"
            "## 需求文档\n")


async def _h_create_function(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡组建**职能**：建一条 function_profiles 行，**不写 cards 表**。

    与旧 create_role 的结构差异只有一处、也是本步的全部要害：
    **职能不是卡**——它不占 card_NNN、不进画布/卡片清单/卡树，身份是独立号段
    ``fn_001``（function_store 内部发号，与建行同一写事务，不重号）。
    其余语义（jurisdiction/watch/speak_contract/escalation 的默认值与落库形状）
    复用 role_room.make_profile——同一份默认值，不另写一套。
    [职能唤醒收口 2026-10-28] `signals` 参数已删：事件订阅/自动唤醒整条链已移除，
    留着它等于让人以为"配了信号就会被叫醒"。
    """
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以组建职能")
    name = str(arguments.get("function_name") or "").strip()
    charter = str(arguments.get("charter") or "").strip()
    if not name:
        return err("validation", "function_name 不能为空（写清这是什么职能）")
    if not charter:
        return err("validation", "charter 不能为空（职责书：对什么负责、什么情况发言、什么情况沉默）")
    raw_watch = arguments.get("watch_card_ids") or []
    if isinstance(raw_watch, str):
        raw_watch = [raw_watch]
    watch = [str(c) for c in raw_watch if c]
    for cid in watch:
        if cid not in cards:
            return err("not_found", f"要关注的卡片不存在: {cid}")
    # make_profile 是纯函数（不落库、不查表），默认值与旧角色路径逐字一致；
    # 它已把 charter 包成 {"version":1,"text":...,"ratified_by":...,"ratified_at":...}
    prof = role_room.make_profile(
        name, charter,
        jurisdiction=arguments.get("jurisdiction"),
        watch_cards=watch,
        signals=arguments.get("signals"),
        speak_contract=arguments.get("speak_contract"),
        escalation=arguments.get("escalation"),
        ratified_by=f"root_card {root_id}",
    )
    prof["function_name"] = name
    created = await function_store.create_function(work_id, name, prof)
    fid = str(created.get("function_id") or "")
    # [职能实体化 A 案 2026-10-22 · 用户裁定] 建职能同时建它的 cards 行
    # （id 就用 fn_NNN）——底层复用卡的基础设施，对外身份仍是 fn。
    await ensure_function_card(work_id, fid, name)
    logger.info("[function] 根卡组建职能 work=%s function=%s watched=%s",
                work_id, fid, watch)
    return {"function_id": fid, "function_name": name,
            "watch": watch, "duty": (created.get("duty") or {}).get("state") or "active",
            "charter_version": (created.get("charter") or {}).get("version") or 1,
            "in_cards_table": True}


async def _h_function_watch_cards(work_id: str, current_id: str,
                                  arguments: dict[str, Any],
                                  cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡调整职能的关注对象：改 function_profiles.watch.cards（不再动 links）。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以调整职能的关注对象")
    fid = str(arguments.get("function_id") or "").strip()
    if not function_store.function_exists(work_id, fid):
        return err("not_found", f"职能不存在: {fid}")
    action = str(arguments.get("action") or "add").strip().lower()
    if action not in ("add", "remove"):
        return err("validation", f"action 非法: {action!r}（只允许 add/remove）")
    raw = arguments.get("card_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    prof = function_store.get_profile(work_id, fid) or {}
    old = [str(c) for c in ((prof.get("watch") or {}).get("cards") or [])]
    targets = [str(c or "").strip() for c in raw if str(c or "").strip()]
    for cid in targets:
        if cid not in cards:
            return err("not_found", f"卡片不存在: {cid}")
    changed: list[str] = []
    for cid in targets:
        if action == "add":
            if cid not in old:
                old.append(cid)
                changed.append(cid)
        else:
            if cid in old:
                old.remove(cid)
                changed.append(cid)
    if changed:
        watch = dict(prof.get("watch") or {})
        watch["cards"] = old
        await function_store.update_profile(work_id, fid, {"watch": watch})
    return {"function_id": fid, "action": action, "changed": changed, "watch": old}


async def _h_update_function_profile(work_id: str, current_id: str,
                                     arguments: dict[str, Any],
                                     cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡更新职能档案：管辖域、关注卡与信号、发言契约、升级线、职责书。

    职责书正文变了 → 走 function_store.append_charter（**追加新版本，不覆盖历史**）。
    """
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以修改职能档案")
    fid = str(arguments.get("function_id") or "").strip()
    prof = function_store.get_profile(work_id, fid)
    if prof is None:
        return err("not_found", f"职能不存在: {fid}")
    patch: dict[str, Any] = {}
    if "function_name" in arguments:
        nm = str(arguments["function_name"] or "").strip()
        if not nm:
            return err("validation", "function_name 不能为空")
        patch["function_name"] = nm
    for key in ("jurisdiction", "speak_contract", "escalation"):
        if key in arguments:
            patch[key] = arguments[key]
    if "signals" in arguments or "watch_card_ids" in arguments:
        old_watch = prof.get("watch") or {}
        if "watch_card_ids" in arguments:
            raw = arguments["watch_card_ids"] or []
            if isinstance(raw, str):
                raw = [raw]
            cards_now = [str(c) for c in raw if c]
            for cid in cards_now:
                if cid not in cards:
                    return err("not_found", f"要关注的卡片不存在: {cid}")
        else:
            cards_now = [str(c) for c in (old_watch.get("cards") or [])]
        signals = ([str(s) for s in (arguments.get("signals") or [])]
                   if "signals" in arguments
                   else [str(s) for s in (old_watch.get("signals") or [])])
        patch["watch"] = {"cards": cards_now, "signals": signals}
    charter_text = str(arguments.get("charter") or "").strip() if "charter" in arguments else ""
    if not patch and not charter_text:
        return err("validation", "没有可更新的档案字段")
    if patch:
        await function_store.update_profile(work_id, fid, patch)
    new_version = (prof.get("charter") or {}).get("version") or 1
    if charter_text:
        ch = await function_store.append_charter(
            work_id, fid, charter_text, ratified_by=f"root_card {root_id}",
            note="职责书改版")
        new_version = ch.get("version") or new_version
    return {"function_id": fid, "updated": sorted(patch.keys()),
            "charter_version": new_version}


async def _h_set_function_duty(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡给职能停职/复职/撤岗（撤岗保留档案与台账，只解��关注之外的一切）。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以调整职能值班态")
    fid = str(arguments.get("function_id") or "").strip()
    if not function_store.function_exists(work_id, fid):
        return err("not_found", f"职能不存在: {fid}")
    state = str(arguments.get("state") or "").strip().lower()
    if state not in ("active", "suspended", "archived"):
        return err("validation", f"state 非法: {state!r}（active/suspended/archived）")
    duty = await function_store.set_duty_state(
        work_id, fid, state, note=str(arguments.get("note") or ""))
    return {"function_id": fid, "state": duty.get("state"),
            "previous": duty.get("previous")}


async def _h_list_functions(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡组阁前先看已有哪些职能：function_id / function_name / duty / watch。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以查看职能清单")
    out: list[dict[str, Any]] = []
    for p in function_store.list_profiles(work_id):
        jur = p.get("jurisdiction") or {}
        watch = p.get("watch") or {}
        out.append({
            "function_id": p.get("function_id") or "",
            "function_name": p.get("function_name") or "",
            "jurisdiction": jur.get("statement") or jur.get("domain") or "",
            "watch": [str(c) for c in (watch.get("cards") or [])],
            "signals": [str(s) for s in (watch.get("signals") or [])],
            "duty": (p.get("duty") or {}).get("state") or "active",
            "charter_version": (p.get("charter") or {}).get("version") or 1,
        })
    return {"functions": out}


def _function_watches(work_id: str, function_id: str, target_id: str) -> bool:
    """该职能是否关注这张卡（指令只能下给自己盯的卡）。读不到＝False（fail-closed）。"""
    try:
        prof = function_store.get_profile(work_id, function_id) or {}
        return target_id in list(((prof.get("watch") or {}).get("cards")) or [])
    except Exception:  # noqa: BLE001
        return False


async def _h_issue_directive(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """下指令：旧 role 卡路径逐字不变；职能回合显式拒（台账未建，见上方说明）。"""
    actor = _function_actor(work_id, current_id, cards)
    if not actor:
        return err("denied", "只有职能（或角色卡）可以下指令")
    if not actor.startswith("fn_"):
        return await _h_issue_directive_legacy(work_id, current_id, arguments, cards)
    # [指令台账 2026-10-22] 职能路径：与 legacy 同一套规则，存储换 function_directives。
    target_id = str(arguments.get("target_card_id") or "").strip()
    instruction = str(arguments.get("instruction") or "").strip()
    acceptance = str(arguments.get("acceptance") or "").strip()
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if str((cards.get(target_id) or {}).get("card_type") or "") in ("role", "function"):
        return err("invalid", "不能给另一个职能/角色下指令")
    if not _function_watches(work_id, actor, target_id):
        return err("denied", f"{target_id} 不在你的关注范围（先让根卡把它加进 watch）")
    if not instruction:
        return err("validation", "instruction 不能为空")
    if not acceptance:
        return err("validation", "acceptance 不能为空（没有验收判据的指令不可验收）")
    content = (
        f"【职能指令】{instruction}\n\n"
        f"验收判据：{acceptance}\n\n"
        "这是指令，不是建议：请执行；不能在回复/现状里说明理由、需要什么支持、"
        "预计何时完成。执行结果要落进现状，便于我下一轮验收。"
    )
    sent = await _h_send_card_message(work_id, current_id, {
        "target_card_id": target_id,
        "msg_type": "msg_directive",
        "content": content,
    }, cards)
    if isinstance(sent, dict) and sent.get("ok") is False:
        return sent
    rec = await function_store.add_directive(
        work_id, actor, target_id, instruction, acceptance,
        msg_id=str((sent or {}).get("msg_id") or ""),
        due_at=str(arguments.get("due_at") or ""),
        parent_id=str(arguments.get("parent_id") or ""),
        attempt=int(arguments.get("attempt") or 1))
    return {
        "directive_id": rec["id"],
        "msg_id": str((sent or {}).get("msg_id") or ""),
        "target_card_id": target_id,
        "status": rec["status"],
    }


async def _h_verify_directive(work_id: str, current_id: str,
                              arguments: dict[str, Any],
                              cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """验收指令：旧 role 卡路径逐字不变；职能回合显式拒（台账未建）。"""
    actor = _function_actor(work_id, current_id, cards)
    if not actor:
        return err("denied", "只有职能（或角色卡）可以验收指令")
    if not actor.startswith("fn_"):
        return await _h_verify_directive_legacy(work_id, current_id, arguments, cards)
    directive_id = str(arguments.get("directive_id") or "").strip()
    status = str(arguments.get("status") or "").strip().lower()
    evidence = str(arguments.get("evidence") or "").strip()
    if status not in ("complied", "rejected", "cancelled"):
        return err("validation", f"status 非法: {status!r}（complied/rejected/cancelled）")
    if status == "rejected" and not evidence:
        return err("validation", "rejected 必须写 evidence（缺什么/差在哪）")
    ok = await function_store.verify_directive(
        work_id, actor, directive_id, status, evidence)
    if not ok:
        return err("not_found", f"你的台账里没有可验收的指令 {directive_id}")
    return {"ok": True, "directive_id": directive_id, "status": status}



# ============================================================
# [角色机制 5710] 组阁 / 关注管理（判词/警报 2026-10-31 退役）
# ============================================================

async def _h_create_role(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡组阁：建一张离树的角色卡，写结构化档案与职责书。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以组阁（创建角色卡）")
    title = str(arguments.get("title") or "").strip()
    charter = str(arguments.get("charter") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    if not charter:
        return err("validation", "charter 不能为空（写清这个角色对什么负责）")
    raw_watch = arguments.get("watch_card_ids") or []
    if isinstance(raw_watch, str):
        raw_watch = [raw_watch]
    res = await role_room.create_role_card(
        work_id, root_id, title, charter,
        jurisdiction=arguments.get("jurisdiction"),
        watch_cards=[str(c) for c in raw_watch if c],
        signals=arguments.get("signals"),
        speak_contract=arguments.get("speak_contract"),
        escalation=arguments.get("escalation"),
    )
    if res.get("error"):
        return err("validation", str(res["error"]))
    logger.info("[role] 根卡组阁 work=%s role=%s watched=%s",
                work_id, res.get("role_card_id"), res.get("watched"))
    return res


async def _h_role_watch_cards(work_id: str, current_id: str,
                              arguments: dict[str, Any],
                              cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡管理订阅：给角色卡加/减关注对象（同步更新档案 watch.cards）。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以调整角色卡的关注对象")
    role_id = str(arguments.get("role_card_id") or "").strip()
    if role_id not in cards:
        return err("not_found", f"卡片不存在: {role_id}")
    if str((cards.get(role_id) or {}).get("card_type") or "") != role_room.ROLE_TYPE:
        return err("validation", f"{role_id} 不是角色卡")
    action = str(arguments.get("action") or "add").strip().lower()
    raw = arguments.get("card_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    changed: list[str] = []
    for cid in raw:
        cid = str(cid or "").strip()
        if not cid:
            continue
        if action == "remove":
            res = await role_room.unlink(work_id, role_id, cid, actor=f"根卡 {current_id}")
        else:
            res = await role_room.link(work_id, role_id, cid, actor=f"根卡 {current_id}")
        if res.get("ok"):
            changed.append(cid)
    watched = await role_room.watched_cards(work_id, role_id)
    return {"role_card_id": role_id, "action": action, "changed": changed,
            "watched": [str(m.get("card_id")) for m in watched]}


async def _h_update_role_profile(work_id: str, current_id: str,
                                 arguments: dict[str, Any],
                                 cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡/用户更新角色档案：管辖域、关注与信号、发言契约、升级线、职责书。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以修改角色档案")
    role_id = str(arguments.get("role_card_id") or "").strip()
    if role_id not in cards:
        return err("not_found", f"卡片不存在: {role_id}")
    if str((cards.get(role_id) or {}).get("card_type") or "") != role_room.ROLE_TYPE:
        return err("validation", f"{role_id} 不是角色卡")
    prof = await role_room.get_profile(work_id, role_id)
    if prof is None:
        return err("not_found", f"角色档案不存在: {role_id}")
    patch: dict[str, Any] = {}
    for key in ("jurisdiction", "speak_contract", "escalation"):
        if key in arguments:
            patch[key] = arguments[key]
    if "charter" in arguments:
        patch["charter"] = arguments["charter"]
    if "signals" in arguments or "watch_card_ids" in arguments:
        old_watch = prof.get("watch") or {}
        cards_now = ([str(c) for c in (arguments.get("watch_card_ids") or [])]
                     if "watch_card_ids" in arguments
                     else [str(c) for c in (old_watch.get("cards") or [])])
        signals = ([str(s) for s in (arguments.get("signals") or [])]
                   if "signals" in arguments
                   else [str(s) for s in (old_watch.get("signals") or [])])
        patch["watch"] = {"cards": cards_now, "signals": signals}
    if not patch:
        return err("validation", "没有可更新的档案字段")
    res = await role_room.update_profile(work_id, role_id, patch,
                                         actor=f"根卡 {current_id}")
    if res.get("error"):
        return err("validation", str(res["error"]))
    return res


async def _h_set_role_duty(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡/用户给角色停职、复职、撤岗（撤岗保留档案与台账，只解除关注）。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以调整角色值班态")
    role_id = str(arguments.get("role_card_id") or "").strip()
    state = str(arguments.get("state") or "").strip().lower()
    if state not in ("active", "suspended", "archived"):
        return err("validation", f"state 非法: {state!r}（active/suspended/archived）")
    res = await role_room.set_duty(work_id, role_id, state,
                                   actor=f"根卡 {current_id}",
                                   note=str(arguments.get("note") or ""))
    if res.get("error"):
        return err("validation", str(res["error"]))
    return res


async def _h_issue_directive_legacy(work_id: str, current_id: str,
                                     arguments: dict[str, Any],
                                     cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """角色在管辖域内向关注卡下**指令**：不是建议，卡必须执行或说明不能执行。"""
    if not role_room.is_role(work_id, current_id):
        return err("denied", "只有角色卡可以下指令")
    target_id = str(arguments.get("target_card_id") or "").strip()
    instruction = str(arguments.get("instruction") or "").strip()
    acceptance = str(arguments.get("acceptance") or "").strip()
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if str((cards.get(target_id) or {}).get("card_type") or "") == role_room.ROLE_TYPE:
        return err("invalid", "不能给另一个角色下指令")
    if not role_room.linked(work_id, current_id, target_id):
        return err("denied", f"{target_id} 不在你的关注范围（先建立关注连线）")
    if not instruction:
        return err("validation", "instruction 不能为空")
    if not acceptance:
        return err("validation", "acceptance 不能为空（没有验收判据的指令不可验收）")
    content = (
        f"【角色指令】{instruction}\n\n"
        f"验收判据：{acceptance}\n\n"
        "这是指令，不是建议：请执行；不能在回复/现状里说明理由、需要什么支持、"
        "预计何时完成。执行结果要落进现状，便于我下一轮验收。"
    )
    sent = await _h_send_card_message(work_id, current_id, {
        "target_card_id": target_id,
        "msg_type": "msg_directive",
        "content": content,
    }, cards)
    if isinstance(sent, dict) and sent.get("ok") is False:
        return sent
    rec = await role_room.record_directive(
        work_id, current_id, target_id, instruction, acceptance,
        msg_id=str((sent or {}).get("msg_id") or ""),
        due_at=str(arguments.get("due_at") or ""),
        parent_id=str(arguments.get("parent_id") or ""),
        attempt=int(arguments.get("attempt") or 1))
    return {
        "directive_id": rec["id"],
        "msg_id": str((sent or {}).get("msg_id") or ""),
        "target_card_id": target_id,
        "status": rec["status"],
    }


async def _h_verify_directive_legacy(work_id: str, current_id: str,
                                      arguments: dict[str, Any],
                                      cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """角色按实际结果验收指令：complied / rejected / cancelled。"""
    if not role_room.is_role(work_id, current_id):
        return err("denied", "只有角色卡可以验收指令")
    directive_id = str(arguments.get("directive_id") or "").strip()
    status = str(arguments.get("status") or "").strip().lower()
    evidence = str(arguments.get("evidence") or "").strip()
    if status not in ("complied", "rejected", "cancelled"):
        return err("validation", f"status 非法: {status!r}（complied/rejected/cancelled）")
    if status == "rejected" and not evidence:
        return err("validation", "rejected 必须写 evidence（缺什么/差在哪）")
    res = await role_room.verify_directive(
        work_id, current_id, directive_id, status, evidence)
    if res.get("error"):
        return err("not_found", str(res["error"]))
    if status == "rejected":
        old = res.get("directive") or {}
        attempt = int(old.get("attempt") or 1)
        if attempt >= 3:
            res["escalate"] = True
            res["note"] = ("已连续打回 3 次，系统不再自动返工；"
                           "请角色升级处理（改判据/换方案/报警给人）。")
            return res
        # 打回即返工：自动下一条新指令（保留旧 rejected 作为阻塞与审计），
        # 目标卡在新指令验收前仍不能收口。
        rework = await _h_issue_directive_legacy(work_id, current_id, {
            "target_card_id": str(old.get("target_card_id") or ""),
            "instruction": (f"返工（第 {attempt + 1} 次）："
                            f"{old.get('instruction') or ''}\n"
                            f"上次打回：{evidence}"),
            "acceptance": str(old.get("acceptance") or ""),
            "due_at": str(old.get("due_at") or ""),
            "parent_id": str(old.get("id") or ""),
            "attempt": attempt + 1,
        }, cards)
        if isinstance(rework, dict) and rework.get("ok") is False:
            res["rework_error"] = rework
        else:
            res["rework"] = rework
            try:
                if res.get("directive", {}).get("id") and rework.get("directive_id"):
                    await role_store.mark_directive_rework(
                        work_id, str(res["directive"]["id"]),
                        str(rework["directive_id"]))
            except Exception:  # noqa: BLE001 返工接续失败不阻断回复
                pass
    return res


# [通信收拢 2026-10-28 · 用户裁定「裁定不靠关口子，靠自裁权与场景语义」]
# `issue_directive` / `verify_directive` **退役**，并进 `direct`：
#   · 下指令 → `direct`（同一条路：沿连线 + 子树 + 必带验收判据）
#   · 验收   → **通过就不说话**（沉默即通过），不通过就再发一条 `direct` 要求改
# 台账（`role_directives` 表与 `role_room.open_directives_for` 闸）保留为**内部记录**，
# 只是不再单独开两个工具给模型。handler 保留供内部路径/既有测试直调。


async def _h_list_roles(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡组阁前先看已有角色/责任域，避免重复组阁。"""
    root_id = work_store.work_card_id_of(work_id)
    if not root_id or current_id != root_id:
        return err("denied", "只有项目根卡可以查看角色清单")
    out: list[dict[str, Any]] = []
    for m in role_room.list_roles(work_id):
        rid = str(m.get("card_id") or "")
        try:
            s = await role_room.role_summary(work_id, rid)
        except Exception:  # noqa: BLE001
            continue
        jur = s.get("jurisdiction") or {}
        watch = s.get("watch") or {}
        out.append({
            "role_card_id": rid,
            "title": s.get("title") or "",
            "domain": jur.get("domain") or "",
            "statement": jur.get("statement") or "",
            "watch_cards": [w.get("card_id") for w in (watch.get("cards") or [])],
            "signals": watch.get("signals") or [],
            "duty": (s.get("duty") or {}).get("state") or "active",
            "open_directives": len(s.get("open_directives") or []),
            "open_alarms": len(s.get("open_alarms") or []),
        })
    return {"roles": out}


TOOL_REGISTRY["list_functions"] = ToolDef(
    schema={
        "name": "list_functions",
        "description": (
            "（根卡专用）列出本项目已建的所有职能：职能号、职能名、职责说明、"
            "盯的卡、关注信号、值班态、职责书版本。组阁前先看这里，避免重复设岗。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    handler=_h_list_functions,
    readonly=True,
)


TOOL_REGISTRY["create_function"] = ToolDef(
    schema={
        "name": "create_function",
        "description": (
            "（根卡专用）**组建一个职能岗**（如「质量管理」），写它的职责书与管辖。\n"
            "职能是**专家岗**：不占卡片号、不进卡片树、不干具体的活；它有自己的一本账"
            "（专业台账），只在**需求评审**时被叫来评，其余时候不自动醒。"
            "用户也可以直接找它对话。\n"
            "职能名按事情本身判断，系统不做白名单校验——人类团队里找不到这个岗位就别建。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "职能名，如「质量管理」「架构守护」"},
                "charter": {"type": "string", "description": "职责书正文：对什么负责、什么情况发言、什么情况沉默"},
                "jurisdiction": {
                    "type": "object",
                    "description": "管辖域：对什么负责（domain 是排他键，同一项目同一域只应有一个角色）",
                    "properties": {
                        "domain": {"type": "string", "description": "英文/拼音排他键，如 timeline_contract"},
                        "statement": {"type": "string", "description": "一句话职责说明"},
                    },
                },
                "watch_card_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "要关注的工作卡 id 列表",
                },
                "speak_contract": {
                    "type": "object",
                    "description": "发言契约（可选）：advice_when/alarm_when/silence_default/max_messages_per_wake",
                    "properties": {
                        "advice_when": {"type": "array", "items": {"type": "string"}},
                        "alarm_when": {"type": "array", "items": {"type": "string"}},
                        "silence_default": {"type": "boolean"},
                        "max_messages_per_wake": {"type": "integer"},
                    },
                },
                "escalation": {
                    "type": "object",
                    "description": "升级线（可选）：advice_to/alarm_to/critical_to",
                    "properties": {
                        "advice_to": {"type": "string"},
                        "alarm_to": {"type": "string"},
                        "critical_to": {"type": "string"},
                    },
                },
            },
            "required": ["function_name", "charter"],
        },
    },
    handler=_h_create_function,
    readonly=False,
)

TOOL_REGISTRY["function_watch_cards"] = ToolDef(
    schema={
        "name": "function_watch_cards",
        "description": "（根卡专用）调整职能的关注对象：action=add/remove。",
        "parameters": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "职能号，如 fn_001"},
                "action": {"type": "string", "enum": ["add", "remove"]},
                "card_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["function_id", "card_ids"],
        },
    },
    handler=_h_function_watch_cards,
    readonly=False,
)

TOOL_REGISTRY["update_function_profile"] = ToolDef(
    schema={
        "name": "update_function_profile",
        "description": (
            "（根卡专用）更新职能档案：职能名、管辖域、关注卡、关注信号、"
            "发言契约、升级线、职责书。职责书文本变化会生成新版本，不覆盖历史。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "职能号，如 fn_001"},
                "function_name": {"type": "string"},
                "jurisdiction": {"type": "object"},
                "watch_card_ids": {"type": "array", "items": {"type": "string"}},
                "signals": {"type": "array", "items": {"type": "string"}},
                "speak_contract": {"type": "object"},
                "escalation": {"type": "object"},
                "charter": {"type": "string"},
            },
            "required": ["function_id"],
        },
    },
    handler=_h_update_function_profile,
    readonly=False,
)

TOOL_REGISTRY["set_function_duty"] = ToolDef(
    schema={
        "name": "set_function_duty",
        "description": (
            "（根卡专用）调整职能值班态：active 复职、suspended 停职、"
            "archived 撤岗（保留档案与台账）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "function_id": {"type": "string", "description": "职能号，如 fn_001"},
                "state": {"type": "string", "enum": ["active", "suspended", "archived"]},
                "note": {"type": "string"},
            },
            "required": ["function_id", "state"],
        },
    },
    handler=_h_set_function_duty,
    readonly=False,
)

# [T11-T10] 只读工具白名单（2026-10-31 起折叠已整体移除，白名单只剩
# 「只读工具」这层语义本身：能力提升/权限判定仍消费它）。
# 白名单是「工具的属性」，属性跟工具住一起（方案自拍 #15）；落点＝工具定义处。
# [B-09 2026-10-08] 改为**从注册表派生**——改前是手工 frozenset 字面量，
# T12 增 read_memory 时需手工补，是漂移源；现 readonly 标志与 schema/handler
# 同处注册项，工具增删时只改一处。
# 名单＝调研纪要 §二 实证枚举（10 只读）；其余 6 件为写工具
# （edit_card/delete_card/update_shadow/link_cards/unlink_cards/write_file）。
# [T12-R3] 增补 read_memory（记忆锚点下探，condense_fold 展开通道，只读拉取）。
# [建卡工具化 2026-10-08] create_card 是写工具（落存储层新卡）——不进白名单。
READONLY_TOOLS = frozenset(
    name for name, def_ in TOOL_REGISTRY.items() if def_.readonly
)


# ============================================================
# [工具卡底座 T9] 角色表（用户 2026-10-09 12:54 裁定逐字）
#
# 「主卡将没有编辑、写文件的权限，只剩调用工具卡」——
# 故主卡侧**收回 7 件写工具**，只留「读＋查＋调用」三类：
#   读：read_card / read_memory / read_shadow / read_manuscript / read_file
#   查：list_manuscript / search_manuscript / web_search / fetch_web
#       list_files / search_files
#   调用：run_tool_card
# 「只」字读作「**留读＋查＋调用三类**」（EC-074）——主卡要理解上下文必须能读，
# 故读与查**不收回**（EC-075/EC-173）。
# ============================================================

WRITE_TOOLS_REVOKED: frozenset[str] = frozenset({
    "create_card",     # 建卡＝写树
    "edit_card",       # 改正文
    "delete_card",     # 删卡
    "update_shadow",   # 写影子
    "link_cards",      # 改连线
    "unlink_cards",    # 改连线
    "write_file",      # 写文件
})

_ROLE_TOOL = "tool"
_ROLE_MAIN = "main"


def _role_of(work_id: str, card_id: str,
             cards: dict[str, dict[str, Any]] | None = None) -> str:
    """查这张卡的**角色**（"tool" 或 "main"）——**读一个字段而已**。

    这是**权限表查表**，不是内容判定（与 EC-084 同性质）：
    查表回答「这张卡是什么角色、能调哪些工具」，**不回答**「这次调用该不该做」。

    查不到（卡不存在／读不出来／无 work 上下文）→ 返回 ``_ROLE_MAIN``，
    即按主卡口径（收回写工具）。⚠️ 这是**权限的保守默认**（拿不准就给少权限），
    不是对卡片的评价——它不对「这张卡该不该是工具卡」下任何结论。

    ⚠️ **为什么必须带 work_id**：卡片编号是 **work 内自增**的（``card_store``
    L739 ``card_id = f"card_{seq:03d}"``），六个 work 都有 ``card_001``。
    只给 card_id 无法唯一定位一张卡——故本函数与 ``build_tool_schemas``
    的 ``work_id`` 形参是**必需项而非可选装饰**（任务书 T10 EC-129 的
    ``build_tool_schemas(current_id)`` 单参写法在多 work 现场无法解析角色，
    此处为必要偏离，已在回报中列明）。
    """
    if not card_id:
        return _ROLE_MAIN
    if cards is None:
        # 只有 cards 缺省时才需要 work_id 去现取；cards 已给定时
        # work_id 不再是必需项（否则构造 meta 的直调场景会被误判为主卡）。
        if not work_id:
            return _ROLE_MAIN
        try:
            cards = _collect_card_meta(work_id)
        except Exception:  # noqa: BLE001
            return _ROLE_MAIN
    return _ROLE_TOOL if cards.get(card_id, {}).get("card_type") == "tool" else _ROLE_MAIN


def _filter_by_role(
    tools: list[dict[str, Any]], card_id: str, work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """**第一级过滤 · 角色过滤**——**接替未启用，当前为恒等（原样返回）**。

    [用户 2026-10-09 17:38 逐字指令]
      「现在先不要把写工具给干掉啊，等工具卡完善后，再接替啊」

    ⁉️ 本函数**曾经**在主卡侧收回 7 件写工具（``WRITE_TOOLS_REVOKED``）。
    那是我**方案层的缺陷**——把「主卡终将收回写工具」这个**最终态**，
    在工具卡还没完善的阶段就落地了，顺序错了。用户当场指正：

        正确顺序 ＝ **工具卡先完善 → 之后才接替**（不是现在就接替）。

    故本函数现已**回滚为恒等**：主卡工具面＝全量（含 7 件写工具，与
    「主卡将没有编辑、写文件的权限，只剩调用工具卡」的**最终态**不同）。
    接替的**时机**未到，接替的**形态**与**清单**（``WRITE_TOOLS_REVOKED``）
    留在此处备用——**接替是后续迭代，须用户另行发令，不得自行启用**。

    工具卡（``card_type == "tool"``）本就原样穿过（EC-131），回滚后两者一致。

    **这是权限表查表，不是内容判定**（EC-084）：它读 ``card_type`` 字段——
    查表回答「这张卡能调哪些工具」，**不回答**「这次调用该不该做」，
    更**不产出**任何关于产物或内容好坏的结论（R-A）。
    """
    return tools


def _filter_by_category(
    tools: list[dict[str, Any]], card_id: str, work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """**第二级过滤 · 分类过滤**（权限线 T4，2026-10-09；重做 2026-10-12；
    工具面补齐 2026-10-12）。

    **逐件查 TOOL_SWITCH 映射**：这件工具对应的开关在这张卡的 perms 里 ⇒ 下发，
    不在 ⇒ 模型压根看不见。查的是**卡上固化的 perms**（运行时不查配置，裁定 4）。

    为什么工具面要和执行层同源：用户裁定「没有权限的卡片应该压根看不见对应的
    工具」。此前只按 tools_file/tools_web 两组过滤，卡片 6 个开关与 shadow_write
    只在执行层有关口——配置删掉 edit，模型仍看得见 edit_card、调下去才被拒。
    现在同一张卡的 perms 同时决定「看不看得见」与「调不调得动」。

    **默认关门（裁定 5）落在三处**，全部返回空工具列表：
      ① card_id 空 / cards 缺省 —— 认不出这张卡是谁；
      ② 卡不在 cards 里 —— 同上；
      ③ 工具不在 TOOL_SWITCH 映射里 —— 认不出它归哪个开关（**有意**：
         以后加工具忘了配开关，会被静默拦下并打点，而不是漏发出去；
         这是「漏发一个没权限的工具」与「多拦一个还没配的工具」之间，
         选前者更安全的那个方向）。

    T7 打点：每次派生记一条（card_id／卡类型／下发工具名／被过滤工具名＋原因）。

    本函数与 ``_filter_by_role`` **各自独立、互不触碰**（两线 R-D）——
    只做权限查配，不产出任何内容判定（R-A）。
    """
    card = (cards or {}).get(card_id)
    if not card_id or cards is None or card is None:
        prompt_log.append_perm_log({
            "fn": "build_tool_schemas",
            "card_id": card_id or None,
            "card_type": None,
            "granted_tools": [],
            "filtered_tools": {t["name"]: "no_card_context"
                              for t in tools if "name" in t},
            "reason": "no_card_context",
        })
        return []

    # [能力底座 2026-10-30 · 第二刀 批次1] 这张卡已迁移到能力挂载 ⇒ 本步不再按开关过滤，
    # 工具面交给紧随其后的 `_filter_by_capability`。
    # 前提（已满足）：一项能力只对应一个权限类别，且迁移时逐卡比对过工具面等价。
    # 未迁移（abilities 为 None / 缺列）⇒ 旧路径逐字不变。
    if "abilities" in card and card.get("abilities") is not None:
        return tools

    visible: list[dict[str, Any]] = []
    filtered: dict[str, str] = {}
    for t in tools:
        name = str(t.get("name", ""))
        switch = TOOL_SWITCH.get(name)
        if switch is None:
            # 未配映射＝认不出归哪个开关 ⇒ 不下发（默认关门）
            filtered[name] = "unmapped"
            continue
        if policy.has_perm(card, switch):
            visible.append(t)
        else:
            filtered[name] = switch

    prompt_log.append_perm_log({
        "fn": "build_tool_schemas",
        "card_id": card_id,
        "card_type": card.get("card_type", ""),
        "granted_tools": [t["name"] for t in visible],
        "filtered_tools": filtered,
    })
    return visible


# ============================================================
# [worker 卡 2026-10-15 · C3] 第三段过滤 · card_type 闸
#
# worker 专属工具只对 card_type=="worker" 的当前卡下发，其余类型一律不出现。
# 与现两段（_filter_by_role 恒等 / _filter_by_category 按卡上 perms）互不触碰
# ——三段各管一线、只查表不判内容（同两线 R-D 纪律）：前两段按「卡有什么权限」
# 下发，本段按「卡是什么类型」收敛。
# ============================================================
WORKER_ONLY_TOOLS: frozenset[str] = frozenset({"report_done"})


def _tool_capability(tool_name: str) -> str | None:
    """工具 → 能力 code。不归任何能力管（回合口子 / 系统自用）⇒ None。

    与工具面读的是**同一份清单**（`config/capabilities.json`），所以不会出现
    「面上有、闸上判不出」的分叉。清单不可用 ⇒ None（调用方按"不归能力管"放行）。
    """
    try:
        from ..infra import capability_registry as _cap
        for c in _cap.capabilities():
            if tool_name in (c["impl"].get("tools") or []):
                return str(c["code"])
    except Exception:  # noqa: BLE001 清单读不到 ⇒ 当作不归能力管
        return None
    return None


def _filter_by_capability(
    tools: list[dict[str, Any]],
    card_id: str,
    cards: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """**能力挂载收窄**（[能力底座 2026-10-30]）。

    判据取卡上的 ``abilities`` 列（能力 code 数组）：
      · 缺列 / 值为 None ⇒ **未迁移**，原样返回（旧路径逐字不变）；
      · 空数组 ⇒ 明确挂了 0 项 ⇒ 空工具面（这是"什么都不能做"，不是"没配"）；
      · 非空 ⇒ 只留「这些能力展开出来的工具」。

    fail-open：能力清单读不到（文件坏/被删）⇒ 原样返回。清单坏一次不能把所有卡的
    工具面清空——那等于全站瘫痪，比"多给几件工具"糟得多。
    """
    card = (cards or {}).get(card_id)
    if not isinstance(card, dict):
        return tools
    mounted = _abilities_of(card, cards)
    if mounted is None:
        return tools          # 算不出来 ⇒ 不拦（清单坏了不能把工具面清空）
    try:
        from ..infra import capability_registry as _cap
        if not _cap.load().get("enabled"):
            return tools
        allowed = _cap.tools_of(mounted)
    except Exception as exc:  # noqa: BLE001 清单不可用 ⇒ 旧路径
        logger.warning("[capability] 挂载解析失败 card=%s: %s", card_id, exc)
        return tools
    return [t for t in tools if str(t.get("name", "")) in allowed]


def _filter_by_card_type(
    tools: list[dict[str, Any]], card_id: str, work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """**第三段过滤 · 类型闸**（worker 卡 2026-10-15 · C3）。

    ``WORKER_ONLY_TOOLS`` 内的工具只对 ``card_type=="worker"`` 的当前卡下发；
    其余工具原样穿过。认不出卡（card_id 空 / cards 缺省 / 卡不在表里）⇒ 原样
    返回——第二段已默认关门返回空列表，此处不重复拦（也绝不放宽）。

    [总裁制 P4 2026-10-24] **report_up 追加**（子卡专属的对外汇报通道）：
    根卡（总裁，对用户走 report_to_user）与职能卡不进；其余卡一律追加——
    「汇报权」是所有非总裁卡的默认配置。追加在闸前，后段过滤照常生效。
    """
    card = (cards or {}).get(card_id)
    if not card_id or cards is None or card is None:
        return tools
    _root_id = ""
    try:
        _root_id = work_store.work_card_id_of(work_id) or ""
    except Exception:  # noqa: BLE001
        _root_id = ""
    # [总裁制 P4 2026-10-24] report_up 只给**作业卡**(sub_project/topic)——
    # 汇报权是干活卡的配置;rule(只读)/tool(工具卡)不进,worker 保持 report_done
    # 既有通道不变。根卡(总裁)走 report_to_user,不进。
    # [总裁制 P4 2026-10-24] report_up 只给**作业卡**(sub_project/topic)——
    # 汇报权是干活卡的配置;rule(只读)/tool(工具卡)不进,worker 保持 report_done
    # 既有通道不变。根卡(总裁)走 report_to_user,不进。
    # ★ 整段挂「目录启用」下:目录关着 ⇒ 旧世界工具面逐字不变(红线)。
    _self_type = str(card.get("card_type") or "")
    _cat_ok = False
    try:
        from . import function_room as _fr
        _cat = _fr.load()
        _cat_ok = bool(_cat.get("enabled"))
    except Exception:  # noqa: BLE001 目录判不出 ⇒ 旧行为
        _cat_ok = False
    # [强制停止] 根卡/父卡(project/sub_project/topic)一律下发(治理权,通用)
    if (_self_type in ("project", "sub_project", "topic")
            and not any(str(t.get("name", "")) == "stop_card" for t in tools)):
        if "stop_card" in TOOL_REGISTRY:
            tools = tools + [_schema_of(TOOL_REGISTRY["stop_card"])]
        if "silence_card" in TOOL_REGISTRY:
            tools = tools + [_schema_of(TOOL_REGISTRY["silence_card"])]
        if "continue_task" in TOOL_REGISTRY:
            tools = tools + [_schema_of(TOOL_REGISTRY["continue_task"])]
    # [通信收拢 2026-10-28] 作业卡(sub_project/topic,非根)的通信件只剩 `sync`：
    # review_request / scene_report 已退役（评审改走伴生评审卡，汇报走 report_up）。
    if (_self_type in ("sub_project", "topic", "ui") and card_id != _root_id):
        for _extra in ("sync",):
            if (_extra in TOOL_REGISTRY
                    and not any(str(t.get("name", "")) == _extra for t in tools)):
                tools = tools + [_schema_of(TOOL_REGISTRY[_extra])]
    # [UI 卡 2026-11-02] ui 也拿 report_up（内循环执行卡的收口通道）；direct
    # 只给能有子卡的作业卡——ui 无子卡、没有指令对象。
    if (_cat_ok and _self_type in ("sub_project", "topic", "ui")
            and card_id != _root_id and "report_up" in TOOL_REGISTRY):
        if not any(str(t.get("name", "")) == "report_up" for t in tools):
            tools = tools + [_schema_of(TOOL_REGISTRY["report_up"])]
        # direct＝指令/下达任务（沿连线 + 子树，执行层兜底），随目录走
        if _self_type != "ui":
            for _extra in ("direct",):
                if (_extra in TOOL_REGISTRY
                        and not any(str(t.get("name", "")) == _extra for t in tools)):
                    tools = tools + [_schema_of(TOOL_REGISTRY[_extra])]
    # [通信收拢 2026-10-28] 原「摘横向发信权(lateral_enabled)」整段删除：
        # send_card_message 已退役（注册表里没有了），这个开关失去对象。
        # 横向通信现在只有 `sync`（沿连线、不唤醒），不再需要机制层收权。
    is_worker = str(card.get("card_type", "") or "") == "worker"
    keep: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    for t in tools:
        name = str(t.get("name", ""))
        if name in WORKER_ONLY_TOOLS and not is_worker:
            # worker 例外:report_done 就是它的完工通道(其余卡摘除)
            dropped[name] = f"card_type:{card.get('card_type', '')}"
            continue
        keep.append(t)
    if dropped:
        prompt_log.append_perm_log({
            "fn": "build_tool_schemas",
            "card_id": card_id,
            "card_type": card.get("card_type", ""),
            "granted_tools": [t.get("name") for t in keep],
            "filtered_tools": dropped,
            "reason": "worker_only_card_type_gate",
        })
    return keep


# ============================================================
# [规则卡 2026-10-15 · 块3] 第四段过滤 · 根卡闸
#
# draft_rule_edit 只对剧本根卡（card_id == work_card_id_of(work_id)）下发，
# 其余卡一律不见——与第三段同纪律：只查 id 不判内容，各段各管一线。
# **认不出 work ⇒ 收敛**（work_id 空／work 不存在 ⇒ root-only 工具一律收走，
# fail-closed，绝不放宽）：生产链路 message_runner 恒定传 work_id，缺 work
# 的只有合成卡面的测试直调——宁可少发、不可错发。
# ============================================================
ROOT_ONLY_TOOLS: frozenset[str] = frozenset(
    {"draft_rule_edit", "create_function", "function_watch_cards",
     "update_function_profile", "set_function_duty", "list_functions",
     # [harness 给根卡 2026-10-28] 盯梢策略是项目级口径 ⇒ 只对根卡下发。
     # handler 里还有一道 card_type=project 的实况校验（双保险）。
     "design_policy"})


def _filter_root_only(
    tools: list[dict[str, Any]], card_id: str, work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """**第四段过滤 · 根卡闸**（规则卡 2026-10-15 · 块3）。

    ``ROOT_ONLY_TOOLS`` 内的工具只对剧本根卡下发；其余原样穿过。认不出
    work（work_id 空／查不到根卡）⇒ root-only 工具全部收走（fail-closed）。
    执行层 handler 另有 current_id 校验（双保险，同 report_done 手法）。

    [预设职能 P-A 2026-10-24] **自由组建退役**：目录启用且 ``free_form_enabled``
    为假(预设独占期)⇒ 建职能四件(create_function/function_watch_cards/
    update_function_profile/set_function_duty)对**包括根卡在内**的所有卡收走——
    职能只能来自目录;list_functions(只读)保留。后期愿景兑现时把目录开关打开,
    本段自动恢复旧行为。
    """
    _free_form = False
    try:
        from . import function_room as _fr
        cat = _fr.load()
        _free_form = (not cat.get("enabled")) or bool(cat.get("free_form_enabled"))
    except Exception:  # noqa: BLE001 目录判不出 ⇒ 保守按可组建(旧行为)
        _free_form = True
    _retired = frozenset() if _free_form else frozenset(
        {"create_function", "function_watch_cards",
         "update_function_profile", "set_function_duty"})

    if not any(str(t.get("name", "")) in ROOT_ONLY_TOOLS for t in tools):
        return tools
    root_id = work_store.work_card_id_of(work_id) if work_id else ""
    card = (cards or {}).get(card_id) or {}
    if root_id and card_id == root_id and not _retired:
        return tools
    keep: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    for t in tools:
        name = str(t.get("name", ""))
        if name in _retired or name in ROOT_ONLY_TOOLS and not (
                root_id and card_id == root_id):
            dropped[name] = ("preset_exclusive" if name in _retired
                             else "root_only" if root_id else "no_work_context")
            continue
        keep.append(t)
    if dropped:
        prompt_log.append_perm_log({
            "fn": "build_tool_schemas",
            "card_id": card_id,
            "card_type": card.get("card_type", ""),
            "granted_tools": [t.get("name") for t in keep],
            "filtered_tools": dropped,
            "reason": "root_only_gate",
        })
    return keep


# ============================================================
# [使用律法 2026-10-22] 自动模式「契约轮」闸（原 card_040「必须轮」的修正）
#
# 组织设计（用户裁定）：自动模式＝「给一个目标、让卡多轮自主推进」，启动时
# 面对的必是复杂任务。动手做事之前，它必须先建职能（职能＝人类团队里真实
# 存在的岗位，不干活只盯）。否则它会直接开始搭交付树，跳到执行。
#
# 契约（判据**全部取自卡面事实**，不看请求参数——驱动器发起的回合不带 op，
# 历史上按 op="split" 做这个闸门端到端实测完全无效）：
#     是「必须轮」 =
#           根卡（cur_meta.card_id == work_card_id）
#       且 auto_mode == 1
#       且 本 work 一个职能都没有（function_store.list_profiles 为空）
#       且 goal_rounds == 0
# 命中契约轮 ⇒ 摘掉一切"动手"的工具（建卡/写文件/跑命令/完工），
# 只留读工具 + append_requirements（写需求文档）。
# ============================================================

#: 契约轮里**必须摘掉**的工具——一切"动手"的东西。只留读 + 写需求（append_requirements）。
#  [使用律法 2026-10-22 · 用户令「自动模式下卡片的第一轮的工作，就是固定的写需求文档」]
#  原来只摘 create_card / create_worker_card（"先建职能"），实际制造了死锁：
#  建职能要 watch 的卡还不存在 ⇒ 建不出来 ⇒ 闸永远命中 ⇒ 卡永远出不去
#  （work_024 实测 blocked）。现在改成"第一轮写需求文档"：需求是唯一事实源，
#  没有它，后面派活/执行/验收全都没有判据。
_MUST_ROUND_DROP: frozenset[str] = frozenset({
    "create_card", "create_worker_card", "delete_card",
    "write_file", "edit_file", "move_file", "rename_file", "delete_file",
    "bash", "python", "run_tool_card", "report_done",
    "link_cards", "link_rel",
    # [差事卡 2026-11-01] create_deploy_card **不摘**：契约轮的本意是「先
    # 立需求再动手」——PM 差事恰好是立需求的那只手（召它来写草稿正当其用）；
    # QA 差事此时没有产物可验，滥召只是报「没东西可验」的空差事，无害。
})


#: 自动模式子卡(sub_project/topic)要从工具面摘掉的件（手动卡不动）。
#:
#: [2026-10-28 · 用户裁定「需求文档它可以自己改，这个权限交还给它」]
#: **`edit_requirements` 已从本集合移除**——需求文档不再唯总裁执笔，子卡自己能改
#: （语义上限定：只写**确定的需求**，见 edit_requirements 的 schema 与 150 区定义）。
#: 改前摘它的理由是「引导块唯总裁执笔」，该口径已被用户推翻。
#:
#: 仍摘 `edit_card`：它现在只剩「使命（改不了）＋版本回退」，而回退会把三块整体
#: 还原到旧版——那能绕过「需求文档只写确定需求」这条，所以不给自动子卡。
_AUTO_LADDER_DROP: frozenset[str] = frozenset({"edit_card"})

#: 派工两件：**开干活的子卡**。非正稿的子卡拿不到这两件。
#: [差事卡 2026-11-01] create_deploy_card 有意**不在**此列：正稿闸拦的是
#: 「没合同就开工」——而 PM 差事本身就是立合同的动作（staff_pm 递话叫它
#: 召 PM 写草稿，闸了这条链就断了）；QA 差事没合同验无可验，无害。
#: [2026-11-04 · 用户令「没有需求文档不需要卡死不许派工，不许开干活的子卡
#: 就可以了」] `direct` 移出本闸——它指挥的是**已有**子树，不是开新工；
#: 且闸它自相矛盾：①子卡只能靠被闸的 create_* 造出来，没正稿的卡根本
#: 没有 direct 对象可指；②continue_task/sync 等更狠的指挥通道从不入闸；
#: ③需求修订期（in_review）恰好是最需要指挥在跑子树的时机。
_REQUIREMENTS_GATED_TOOLS: frozenset[str] = frozenset(
    {"create_card", "create_worker_card"})


def _apply_requirements_gate(tools: list[dict[str, Any]], card_id: str,
                             cards: dict[str, dict[str, Any]] | None
                             ) -> list[dict[str, Any]]:
    """[需求文档生命周期 2026-10-28 · 用户令] **没有正稿 ⇒ 不许派工**。

    流程：使命 → 写需求草稿 → 评审 → 通过成**正稿** → 才派工/执行。
    所以「不是正稿」时，这张卡只能：读、写自己的草稿与现状、汇报、同步、
    指挥已有子树（direct 不在闸内——2026-11-04 用户令）。
    派工两件（建卡/派worker 卡）摘掉——**看不见＝不该做**，不靠提示词劝。

    只对 sub_project / topic 生效：根卡（项目）与职能/角色卡不在这条流程里，
    worker 卡没有需求文档也不需要评审（用户令：worker 不走评审）。
    """
    if not card_id or cards is None:
        return tools
    meta = cards.get(card_id) or {}
    if str(meta.get("card_type") or "") not in ("sub_project", "topic"):
        return tools
    if str(meta.get("requirements_state") or "") == "approved":
        return tools
    return [t for t in tools
            if str(t.get("name", "")) not in _REQUIREMENTS_GATED_TOOLS]


def _auto_governance_face(tools: list[dict[str, Any]], card_id: str,
                          work_id: str,
                          cards: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """自动模式子卡的两套权限落地(目录启用时;手动卡与关目录 = 旧世界逐字不变)。

    ① 收权(全程):auto + sub_project/topic ⇒ 只摘 edit_card（版本回退）。
       需求文档**不摘**——[2026-10-28 用户裁定] 子卡自己能改自己的需求文档。
    ② 首轮需求闸(goal_rounds==0):需求文档仍是占位(总裁未策展)⇒ 摘一切
       动手工具,只留 读/report_up/update_status/update_goal——「先读总裁
       策展的需求再开工;缺则 ask,不许自己编」。
    fail-open:判不出(读不到/异常)⇒ 原样返回,不误杀。
    """
    cid = str(card_id or "")
    if not cid or not work_id:
        return tools
    card = (cards or {}).get(cid) or {}
    if not bool(card.get("auto_mode")):
        return tools                              # 手动模式:两套之外
    if str(card.get("card_type") or "") not in ("sub_project", "topic"):
        return tools                              # 根卡=总裁,worker 另有通道
    try:
        from . import function_room as _fr
        if not _fr.load().get("enabled"):
            return tools                          # 目录关着 = 旧世界
    except Exception:  # noqa: BLE001
        return tools

    # [2026-10-24 · 用户令「不要做太多代码层乱七八糟的判断」] 首轮的工具面
    # 摘除已撤——激活闸(auto_driver.kick:无策展不点火+信告上级)一处管住,
    # 不再叠加第二层工具面判断。这里只保留收权(两套权限本体)。
    return [t for t in tools
            if str(t.get("name", "")) not in _AUTO_LADDER_DROP]




def _is_must_round(
    card_id: str, work_id: str,
    cards: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """是否命中「自动模式启动必须轮」（判据全部取自卡面事实）。

    四条全满足才算：① 根卡（card_id == work_card_id_of(work_id)）；② ``auto_mode
    == 1``；③ 本 work **一个职能都没有**（``function_store.list_profiles`` 为空）；
    ④ ``goal_rounds == 0``。

    **fail-open（宁可放过，不可误杀）**：职能查询读不到/抛异常一律按「已有职能」
    处理 ⇒ 不触发闸门。为什么：摘掉 create_card 的代价远大于闸门失效——如果 DB
    读失败就摘卡，根卡会永远建不出交付树。
    """
    cid = str(card_id or "")
    if not cid or not work_id:
        return False
    card = (cards or {}).get(cid) or {}
    # ① 只对**根卡**（总裁的组织轮）。[两套权限 2026-10-24] 实现里根卡判定
    #    曾丢失 ⇒ 契约轮错杀所有 auto 子卡的首轮(策展后 rounds 仍 0 也开不了
    #    工)——恢复;子卡的首轮需求闸归 _auto_governance_face(读策展需求,
    #    缺则 ask,策展后即放行)。
    try:
        if cid != (work_store.work_card_id_of(work_id) or ""):
            return False
    except Exception:  # noqa: BLE001 根卡判不出 ⇒ 不触发(fail-open 同下)
        return False
    # ② 自动模式
    if not bool(card.get("auto_mode")):
        return False
    # ③ 第一轮（没跑过任何一轮）
    if int(card.get("goal_rounds") or 0) != 0:
        return False
    return True


def _apply_must_round_face(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """必须轮工具面：摘掉搭交付树的 create_card / create_worker_card，其余原样。"""
    if not any(str(t.get("name", "")) in _MUST_ROUND_DROP for t in tools):
        return tools
    return [t for t in tools if str(t.get("name", "")) not in _MUST_ROUND_DROP]


def build_tool_schemas(
    card_id: str = "",
    work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
    op: str = "",
) -> list[dict[str, Any]]:
    """OpenAI function calling 工具列表（直接传给 gateway.call(tools=...)）。

    [B-09 2026-10-08] 改为遍历注册表派生（改前是 14 段字面量 list ＋
    file_tools 追加）。顺序＝注册表插入序＝改前字面量顺序，逐字不变。

    [工具卡底座 T10 · EC-120] 加两级过滤，**各拥一个独立 helper**
    （两线 R-D：共用函数但各改各的内层，不得各改一套）：

        一级 ``_filter_by_role``     ← 角色过滤（工具卡底座 T9）
        二级 ``_filter_by_category`` ← 分类过滤（权限线 T4）
        三级 ``_filter_by_card_type``← 类型闸（worker 卡 2026-10-15 · C3：
                                        WORKER_ONLY_TOOLS 只对 worker 卡下发）
        四级 ``_filter_root_only``   ← 根卡闸（规则卡 2026-10-15 · 块3：
                                        ROOT_ONLY_TOOLS 只对剧本根卡下发）

    [T4 权限收口 2026-10-09] ``cards`` ＝ 分类查表所需卡面 meta（上游装配层补传）。

    [权限底座重做 2026-10-12] **无卡上下文 ⇒ 默认关门**：认不出当前卡是谁时，
    二级过滤返回空工具列表（不再逐字返回全量工具面）。带上卡上下文（生产链路
    message_runner 恒定传 ``card_id/work_id/cards``）⇒ 按该卡固化的 perms 下发。

    [更新现状按钮改造 2026-10-16] ``op`` ＝ 本轮操作类型（POST …/messages 的 op）。
    ``OP_ONLY_TOOLS``（propose_status）**不进上面四段**——它在**四段过滤之后**
    追加，且只在对应 op 回合（见下）。故常规卡片工具面逐字不变；专用工具不随
    卡 perms 下发，符合「不是卡片内挂的」口径。

    [自动模式 2026-10-17 · 回灌 5700 块0.4] ``AUTO_ONLY_TOOLS``（update_goal）
    与 create_card 的 ``goal`` 参数**只对 auto_mode=1 的卡**下发——它们不进上面
    四段（从工具池里先排除），故**常规卡片工具面逐字不变**。
    ★ TC5 裁剪：与 Cardlo 沙箱版不同，这里**不摘计划六件**——生产保留思维链
    （ND1 红线 11；拆解上板是产品能力，不是脚手架）。auto 卡的屏蔽面只有
    判断协议区（context_regions.render_judgment_region 的 auto 分支）。
    """
    tools = [_schema_of(def_) for name, def_ in TOOL_REGISTRY.items()
             if name not in OP_ONLY_TOOLS and name not in AUTO_ONLY_TOOLS
             and name not in ROLE_ONLY_TOOLS]
    # ---- 一级：角色过滤（工具卡底座 T9）----
    tools = _filter_by_role(tools, card_id, work_id, cards)
    # ---- 二级：分类过滤（权限线 T4）----
    tools = _filter_by_category(tools, card_id, work_id, cards)
    # ---- 二级半：[能力底座 2026-10-30] 按卡上挂的能力再收窄 ----
    # 与上一步并存：perms 仍是上限，能力是岗位配置，两者同时成立才下发。
    # 卡上没有 abilities（未迁移 / NULL）⇒ 本步原样返回，行为与改前逐字一致。
    tools = _filter_by_capability(tools, card_id, cards)
    # ---- 三级：card_type 闸（worker 卡 2026-10-15 · C3）----
    tools = _filter_by_card_type(tools, card_id, work_id, cards)
    # ---- 四级：根卡闸（规则卡 2026-10-15 · 块3）----
    tools = _filter_root_only(tools, card_id, work_id, cards)
    # ---- 五级：[换父 2026-10-20] 不能有子卡的卡不下发换父工具 ----
    # 工具卡/worker 卡/规则卡自己没有子树可调（角色卡的整个工具面在末段③被替换，
    # 走不到这里也无妨）。执行层同样会拒，但「工具面与执行层同源」的意思是
    # **看不见＝不该做**：给它一个永远调不通的工具，只会白烧一次模型调用。
    # 判据读注册表 can_have_children，不硬编码类型名。
    if card_id and cards is not None:
        _self_type = str((cards.get(card_id) or {}).get("card_type") or "")
        if _self_type and not card_store._type_can_have_children(_self_type):
            tools = [t for t in tools
                     if str(t.get("name", "")) != "reparent_card"]
    # ---- 末段①：自动模式专用工具面（非卡片内挂；只对 auto_mode=1 的卡）----
    # 追加在四段之后 ⇒ 不经任何按卡过滤（不随 perms 收敛），可见性唯一由
    # auto_mode 决定。非 auto 卡 ⇒ 本段不进 ⇒ 输出与基线逐字一致。
    _card = (cards or {}).get(card_id) or {}
    if card_id and cards is not None and _card and bool(_card.get("auto_mode")):
        tools = _apply_auto_toolface(tools)
        # [两套权限 2026-10-24] 自动模式治理面:子卡收权(引导块唯总裁执笔)
        # ＋ 首轮需求闸(未策展 ⇒ 只留读/汇报)。手动卡不进。
        tools = _auto_governance_face(tools, card_id, work_id, cards)
        # [card_040 · 2026-10-21] 自动模式启动「必须轮」：根卡 auto 首轮且本 work
        # 一个职能都没有 ⇒ 只许组建职能、不许搭交付树（摘 create_card/create_worker_
        # card）。判据全取卡面事实，不看 op（驱动器回合不带 op）。
        if _is_must_round(card_id, work_id, cards):
            tools = _apply_must_round_face(tools)
            logger.info("must_round 触发：根卡 %s auto 启动第一轮且无职能 ⇒ 须先建职能",
                        card_id)
        # [规划驱动 2026-10-19 · permexp **测试闸**] 无活板 ⇒ 只留「看得见、查得到」，
        # 不给建结构与写产物。想动手，先 create_plan 把路线写下来。
        # ★ 必须与思维链开关联动：思维链关着就没有 create_plan 可调，闸一开就是
        #   死锁（要先规划才能建卡，可规划工具已关）——实测 4 组实验全废在这。
        from ..thinkchain import enabled as _tc_on
        if _tc_on() and not _has_active_board(work_id, card_id):
            tools = _apply_plan_gate(tools)
    # ---- 末段②a：拆分轮（op="split"）——只许组织，不许开工 ----
    # [2026-10-21 修正·用户裁定] 只保留**显式**路径：由调用方在确实想
    # 要一次纯组织回合时指定 op="split"。**不再**按「第一轮/没拆过」
    # 自动触发——一张卡的第一轮完全可能是对话、讨论、澄清，这时摘掉写
    # 工具就是拿闸门挡正常行为，属于机械闸门。
    # 「该拆了」这件事改为能力区里的**提示**（见 context_regions
    # render_card_ability_region），不动工具面。
    if op == OP_SPLIT:
        tools = _apply_split_face(tools)
        tools = _apply_split_face(tools)
    if op == OP_UPDATE_STATUS:
        tools.append(_schema_of(TOOL_REGISTRY["propose_status"]))
    # [需求确定按钮改造 2026-10-29 · 用户令] 根卡【需求确定】那一轮才有的口子：
    # 写需求文档**正稿**。加两道判据——op 对得上，且本卡确实是根卡（project）；
    # 其他卡即使伪造 op 也拿不到（正稿对它们只能由评审产生）。
    if op == OP_REQUIREMENTS_FINAL:
        _op_rt = str((cards.get(card_id) or {}).get("card_type") or "") if (
            card_id and cards is not None) else ""
        if _op_rt == "project" and "write_requirements_final" in TOOL_REGISTRY:
            # 这一轮只留一条写需求的路：写正稿。草稿工具摘掉——根卡没有评审，
            # 写进草稿没人能把它转正，等于白写。
            tools = [t for t in tools
                     if str(t.get("name")) != "edit_requirements"]
            if all(str(t.get("name")) != "write_requirements_final" for t in tools):
                tools.append(_schema_of(TOOL_REGISTRY["write_requirements_final"]))
    # ---- 末段③：[card_031] 职能回合工具面 ----
    # 与 auto 面互斥（职能回合不是 auto 卡）。判定看「是不是职能回合」
    # （_is_function_turn：fn_NNN 真在职能表里，或旧 role 卡回合），
    # 不再单看 card_type=="role"——职能没有 cards 行，按卡型判不出来。
    if card_id and _is_function_turn(work_id, card_id, cards):
        tools = _apply_function_toolface(tools, work_id, card_id)
    # ---- 末段④:[预设职能 P-A 2026-10-24] 总裁工具面 ----
    # 根卡已按目录 ceo 条目职能化 ⇒ 追加总裁的 allow 工具(curate_face/
    # issue_directive 等,P3/P5 注册后即自动出现;现在注册表里没有就不加,
    # 行为零变化)。非根卡/未职能化的根卡 ⇒ 不进。
    if card_id and cards is not None and work_id:
        try:
            _root_id = work_store.work_card_id_of(work_id) or ""
        except Exception:  # noqa: BLE001
            _root_id = ""
        if _root_id and card_id == _root_id:
            _ceo_deny, _ceo_allow = _catalog_toolface(work_id, card_id)
            if _ceo_allow:
                _have = {str(t.get("name", "")) for t in tools}
                for name in _ceo_allow:
                    if name in TOOL_REGISTRY and name not in _have:
                        tools.append(_schema_of(TOOL_REGISTRY[name]))
            # [send_card_message 退役 2026-10-25] 总裁面摘除——系统基础设施
            # 不上模型面,总裁的对外通信走四场景件与 report_to_user。
            tools = [t for t in tools
                     if str(t.get("name", "")) != "send_card_message"]
            # [总裁制 P4 2026-10-24] 目录 deny 对总裁同样生效(如 send_card_message/
            # append_requirements——总裁的出口是 curate_face/confirm/report_to_user)。
            if _ceo_deny:
                tools = [t for t in tools
                         if str(t.get("name", "")) not in _ceo_deny]
    # ---- 末段⑥：[需求文档生命周期 2026-10-28 · 用户令] 非正稿 ⇒ 摘派工 ----
    # 用户原话：「子卡的需求文档不是正稿的情况下，取消它的派工权限」。
    # 派工＝让别人干活的三件：建卡（create_card）、派worker 卡（create_worker_card）、
    # 给下级下指令（direct）。汇报（report_up）、同步（sync）、读、写自己的草稿都不摘。
    # 判据取卡面事实（requirements_state），不看 op；只有 sub_project/topic 受此限。
    tools = _apply_requirements_gate(tools, card_id, cards)

    # ---- 末段⑦：[需求确定按钮改造 2026-10-29] edit_requirements 只给有需求流程的卡型 ----
    # 它已从 AUTO_ONLY_TOOLS 移出（那是错挂：手动卡被漏掉，而「草稿随便改」对所有
    # 子卡都成立）。移出后按权限面走常规过滤，但只有 **sub_project / topic** 走
    # 「草稿 → 评审 → 正稿」这条流程：
    #   · 根卡（project）平时不给——它开不出评审，写草稿没人能转正；点【需求确定】
    #     那一轮才拿到写**正稿**的口子（write_requirements_final）。
    #   · worker / tool / 职能卡没有这条流程（用户 2026-10-28：worker 不走评审），
    #     给了只会写出一份没人评审、也没人读的草稿。
    if card_id and cards is not None:
        _req_rt = str((cards.get(card_id) or {}).get("card_type") or "")
        if _req_rt and _req_rt not in ("sub_project", "topic"):
            tools = [t for t in tools
                     if str(t.get("name", "")) != "edit_requirements"]

    return tools


def _create_card_schema_with_goal(schema: dict[str, Any]) -> dict[str, Any]:
    """复制一份 create_card schema 并补上 ``goal`` 属性（**不改注册表原字面量**）。

    ``goal`` 只在自动模式卡上下发（派活用）——常规工具面因此逐字不变。
    """
    params = dict(schema.get("parameters") or {})
    props = dict(params.get("properties") or {})
    if "goal" in props:
        return schema
    props["goal"] = {
        "type": "string",
        "description": (
            "（自动模式）给新建子卡的目标文本——子卡继承自动模式后**建卡即起跑**。"
            "非自动模式卡不必传。"
        ),
    }
    params["properties"] = props
    out = dict(schema)
    out["parameters"] = params
    return out


#: [规划驱动 2026-10-19 · permexp **测试闸**] 无活板时**不给**的工具：
#: 建结构（create_card / create_worker_card / delete_card / 连线）与写产物
#: （write_file / edit_file / move_file / rename_file / delete_file）。
#: 保留：只读类 ＋ bash/python/glob_files/list_files/search_files（允许"看一眼再规划"）
#:      ＋ create_plan（唯一出口）＋ update_status/append_requirements/update_shadow（记录）。
#: ★ 仅实验期使用；落地形态见「第二轮起要求有板」。
_PLAN_GATE_BLOCKED: frozenset[str] = frozenset({
    "create_card", "create_worker_card", "delete_card",
    "link_cards", "unlink_cards", "link_rel", "unlink_rel",
    # [换父 2026-10-20] 改父子边＝改结构，与建卡/连线同档：无活板时不给
    "reparent_card",
    "write_file", "edit_file", "move_file", "rename_file", "delete_file",
    "send_card_message",
})


def _has_active_board(work_id: str, card_id: str) -> bool:
    """当前卡是否有**未收口**的计划板（有板才放行建结构/写产物）。"""
    try:
        from ..thinkchain import board as _board  # 延迟 import：thinkchain 反向依赖本模块
        snap = _board.snapshot_sync(work_id, card_id)
    except Exception:  # noqa: BLE001 读不到＝无板（fail-closed 到「先规划」）
        return False
    if not snap:
        return False
    steps = snap.get("steps") or []
    return any(str(s.get("status") or "") != "done" for s in steps)


def _apply_plan_gate(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """无活板时的工具面：摘掉建结构与写产物，只留「看 + 查 + 记录 + 上板」。"""
    return [t for t in tools if str(t.get("name", "")) not in _PLAN_GATE_BLOCKED]


def _apply_split_face(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """拆分轮工具面（op="split"）：只留组织类，摘掉一切写产物/执行类。

    为什么（依据实测）：v11 之前所有臂的根卡都能在同一轮里一边建卡一边写代码，
    于是它把「组织」和「实现」混在一条思路里——结果是给子卡派任务书，而不是给职责。
    把执行工具在这一轮摘掉，它想动也动不了，只能停在组织层面想清楚：
    这件事该按什么职责切开、每块负责什么。

    只由 build_tool_schemas 在 op == OP_SPLIT 时调用；其它回合逐字不变。
    """
    return [t for t in tools
            if str(t.get("name", "")) not in SPLIT_BLOCKED_TOOLS]



def _apply_auto_toolface(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """自动模式工具面（**本任务对工具面唯一的 auto 开口**，TC5 裁剪版）：

    1. 追加自动模式专用工具（AUTO_ONLY_TOOLS 全量——update_goal ＋
       send_card_message/read_inbox，**按注册追加序**：update_goal 首位，
       既有语义保留；不按 sorted 字母序）；
    2. 给 ``create_card`` 补上 ``goal`` 参数（派活）；
    3. **不摘计划六件**（TC5：生产保留思维链；与 Cardlo 沙箱版 ND1 口径的
       差异就在此——那版摘六件是「最原始单模型」实验口径，不进生产）。

    只由 ``build_tool_schemas`` 在「当前卡 auto_mode=1」时调用——**非 auto 卡
    一切逐字不变**（红线 2 / 块0.4 硬判据 1）。
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        name = str(t.get("name", ""))
        # [去全量更新 2026-10-28] 原来这里摘 edit_card（理由：它是整篇覆盖、
        # 会绕过块级边界）。精准编辑之后该理由不成立 ⇒ **auto 卡拿回 edit_card**，
        # 现状/需求文档的日常更新都走精准编辑；整块替换的 update_status 已退役。
        if name == "create_card":
            out.append(_create_card_schema_with_goal(t))
        else:
            out.append(t)
    # 追加序固定（块0.4 起 update_goal 首位；G2 起两件通信随后）——集合成员
    # 增长不改首位语义，测试与审计都认这个序。
    # [goal 域 2026-10-20 · 修漏] 这份清单是**硬编码**的：只在 TOOL_REGISTRY /
    # AUTO_ONLY_TOOLS 里登记、没加到这里 ⇒ 工具注册了但**永远不下发**（实测
    # get_goal/create_goal 就漏了这一层，resume_child 同样）。
    for auto_name in ("update_goal", "get_goal", "create_goal", "resume_child"):
        if auto_name in AUTO_ONLY_TOOLS:
            out.append(_schema_of(TOOL_REGISTRY[auto_name]))
    return out


# ============================================================
# [角色机制 5710] 角色卡工具面（非卡片内挂）
#
# 角色卡不是作业卡：摘掉 edit_card（不许改别人/整篇正文），只留
# 读、沿连线读、影子、发建议、写自己的台账、
# 现状/需求文档。全部在四段过滤之后追加，常规卡工具面逐字不变。
# ============================================================
# [职能实体化 A 案 2026-10-22 · 用户裁定] 职能是**管理者**（人类团队里的岗位），
# 要能派活（create_card / create_worker_card）和建立通信（link/unlink）。
# 只留 delete_card 在摘除集里——职能不删别人的卡。
# 原集合把 create_card/create_worker_card/link_* 全摘掉，等于"职能不能派活、
# 不能连线"，与"职能盯卡、必要时要求返工"的职责直接矛盾。
_ROLE_FACE_DROP = frozenset({"edit_card", "delete_card",
                             # [send_card_message 退役 2026-10-25] 职能/总裁面
                             # 摘除——系统基础设施不上模型面(四场景件替代)。
                             "send_card_message"})


def _is_function_turn(work_id: str, card_id: str,
                      cards: dict[str, dict[str, Any]] | None = None) -> bool:
    """**这一回合是不是职能回合**（取代旧「按 card_type=="role" 判」）。

    职能不是卡 ⇒ 没有 cards 行、按 card_type 判不出来。判据两条，满足其一即是：
      ① ``card_id`` 形如 ``fn_NNN`` **且** function_profiles 里真有它
         （前缀只防误判，必须真查过表才算——否则随便一个叫 fn_xx 的卡
         就能混进职能面拿到指令权）；
      ② 旧 role 卡回合（card_type=="role"）——**新旧并存**，旧路径行为逐字不变。

    查表失败一律判 False（fail-closed：拿不准就不给职能面）。
    """
    cid = str(card_id or "")
    if not cid:
        return False
    # [职能实体化 A 案 2026-10-22 · 用户裁定] 职能现在**在 cards 表里有一行**
    # （card_type="function"），回合以 card_id 跑 ⇒ 直接按类型判，最可靠。
    _ct = str(((cards or {}).get(cid) or {}).get("card_type") or "")
    if _ct == "function":
        return True
    # 旧路径①：card_id 形如 fn_NNN 且 function_profiles 里真有它（迁移前的职能回合）。
    if cid.startswith("fn_"):
        try:
            return function_store.function_exists(work_id, cid)
        except Exception:  # noqa: BLE001 查不到＝不是职能回合（fail-closed）
            return False
    # 旧路径②：旧 role 卡回合
    return _ct == "role"


def _catalog_toolface(work_id: str, card_id: str) -> tuple[set[str], list[str]]:
    """[预设职能 P-A 2026-10-24] 查这张卡对应的目录条目工具面(deny 集, allow 列)。

    覆盖顺序(声明式,目录是唯一事实源):基础面(旧算法) → **deny 摘除** →
    **allow 追加**(注册表里存在才加,为 P3/P5 的 curate_face 等预铺)。
    非目录职能(work_025 存量、查不到条目)⇒ (空集,[])——基础面逐字不变。
    """
    try:
        from . import function_room
        prof = function_room.function_meta_for(work_id, card_id) or {}
        code = function_room.catalog_code_of(prof)
        if not code:
            return set(), []
        entry = function_room.entry(code) or {}
        tf = entry.get("toolface") or {}
        return ({str(x) for x in (tf.get("deny") or [])},
                [str(x) for x in (tf.get("allow") or [])])
    except Exception:  # noqa: BLE001 目录不可用 ⇒ 基础面
        return set(), []


def _apply_function_toolface(tools: list[dict[str, Any]],
                             work_id: str = "", card_id: str = "") -> list[dict[str, Any]]:
    out = [t for t in tools if str(t.get("name", "")) not in _ROLE_FACE_DROP]
    # [通信收拢 2026-10-28] 固定追加元组：read_inbox / issue_directive /
    # verify_directive 已退役（前者随收件箱取消，后两者并进 direct）。
    # 职能要「下指令」用 `direct`（沿连线 + 必带验收判据），要「验收」就
    # 通过不说话、不通过再发一条 direct——不再有单独的验收工具。
    # [2026-10-30 删] update_status / append_requirements 两件已不在注册表——
    # 前者是整块覆盖现状（10-27 退役），后者早被 edit_requirements 取代。
    # 留着名字只会让这段清单看起来比实际多两件。
    for name in ("approve",):
        if name in TOOL_REGISTRY:
            out.append(_schema_of(TOOL_REGISTRY[name]))
    # [预设职能 P-A 2026-10-24] 目录条目覆盖:deny 摘除(如 send_card_message/
    # create_card——新架构职能不发信不派工,影响力经汇聚),allow 追加(P3/P5 工具)。
    _deny, _allow = _catalog_toolface(work_id, card_id)
    if _deny:
        out = [t for t in out if str(t.get("name", "")) not in _deny]
    if _allow:
        _have = {str(t.get("name", "")) for t in out}
        for name in _allow:
            if name in TOOL_REGISTRY and name not in _have:
                out.append(_schema_of(TOOL_REGISTRY[name]))
    # 去重（防同一件工具从基础面与追加面各来一份）
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for t in out:
        n = str(t.get("name", ""))
        if n in seen:
            continue
        seen.add(n)
        uniq.append(t)
    return uniq


#: [card_031] 旧名保留别名（旧 role 用例按旧名调用；指向同一实现，不是第二份）。
_apply_role_toolface = _apply_function_toolface


def _collect_card_meta(work_id: str) -> dict[str, dict[str, Any]]:
    """轻量收集 work 下所有卡的 meta（含 parent/card_type/links/relations）。

    [T-0007写路径收口] 改走card_store/shadow_store 的公开接口
    （iter_card_metas / iter_shadow_metas），不再跨层摸 _cards_dir/_meta_of/
    _shadows_dir。影子 meta 并入同一字典（R-09 读侧：影子挂在正卡之下，
    统一关口经 parent 链自动作用在影子上）；键＝shadow_{正卡id}。
    """
    cards = card_store.iter_card_metas(work_id)
    cards.update(shadow_store.iter_shadow_metas(work_id))
    return cards


# ============================================================
# [卡片信息结构改造 S3] 写入闸（只拦模型工具通道）
# ============================================================

# 规则文案逐字锁定（V2/V3/V8a/V8b 以文本逐字含为判据，改文案即改判据）
MSG_MISSION_LOCKED = "使命块为立项定稿，修改需用户确认"
MSG_RULINGS_OVERWRITE = "需求文档块覆盖写·单一最新：整块重写，只保留最新一份"
MSG_DECISION_IN_STATUS = "现状块不装决定；决定进需求文档（D{nn} 条目属需求文档块）"

# 现状块禁装决定的机械判据：整行以 D{nn}【 开头
_DECISION_LINE_RE = re.compile(r"^D\d{2}【")

# 失效标记行判据（S3-3 规则③）。**两种指代方向都收**：
#   「D08【…】…」 → 就地替换为 「→ 被D09取代（因…）」：标记在原条目位置，
#     D09 是取代者；被失效的是**这一行原本的编号**——但那行原文已被替换掉，
#     标记行自身不再携带 D08。机械判据无法从标记行反推原编号，
#     故此处按「标记行出现在某 D 条目被移除的位置即视为该条目合规失效」处理
#     （见 _requirements_superset 的一对一配对逻辑）。
#   「D08 已失效」/「D08 被取代」形态：编号直陈，一眼可判。
# 两种形态都放行（规则③：行首 → 放行），判据只负责「不把它们当偷偷删除」。
_SUPERSEDED_SELF_RE = re.compile(r"^→\s*(?:被D\d{2}取代|D\d{2}\s*(?:已失效|被取代))")
_SUPERSEDED_TARGET_RE = re.compile(r"被D(\d{2})取代|D(\d{2})\s*已失效")


def _requirements_entries(text: str) -> dict[str, str]:
    """把需求文档块解析成 {D编号: 该行原文}；非 D 行（说明性文字）不入表。

    失效标记行（行首 →）不算条目——它是标记不是决定；它指名的编号
    由 _superseded_ids 单独收走（规则③：就地失效不算删除）。
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("→"):
            continue
        m = re.match(r"^(D\d{2})", s)
        if m:
            out[m.group(1)] = s
    return out


def _superseded_markers(text: str) -> set[str]:
    """取出需求文档块里的失效标记行（行首 →）逐字原文集合（S3-3 规则③）。

    语义：失效条目**就地一行标记、不保留原文、不设已废区**——被标记的条目
    在需求文档块里不再以原文出现，这是**合规的处置**，不是「偷偷删掉」。
    规则②判「旧条目是否被删改」时，用它做一对一抵扣（见 _requirements_superset）。
    """
    return {ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("→")}


def _check_block_rules(old_body: str, new_body: str) -> dict[str, Any]:
    """分区 diff 判闸（S3-1/S3-3）。返回 {"ok","rule","message"}。

    七条规则：
      ①使命区不等 → 拒（立项定稿，模型改需用户确认）
      ②需求文档区非超集 → 拒（只追加；既有 D 条目被删或被改写即拒）
      ③行首「→」的失效标记行放行（它在 ② 里不算被删的条目）
      ④现状区 diff 不校验（覆盖写·单一最新，本就该自由改）
      ⑤修改历史区 diff 忽略（机械尾，系统自己写）
      ⑥旧结构卡不校验（旧卡无三块语义，闸不作用于存量卡）
      ⑦现状区出现整行 D{nn}【 → 拒（决定归需求文档块）

    [S3-4/RD-11] **不含锚点闸**：现状块的「事实带锚点自指」是语义纪律，
    不做机械校验。逐字理由：机械强制会激励伪造锚点，伪证比缺证更伤权威机制。
    """
    old_sec = split_body_sections(old_body or "")
    new_sec = split_body_sections(new_body or "")

    # 规则⑥：旧结构卡不校验（含混挂卡——它们没有可判的使命/需求文档语义）
    if old_sec["_form"] != "new":
        return {"ok": True, "rule": "skip_old_form", "message": ""}

    # [S3-6 配套] 新正文无任何区头 → 按「只写现状正文」判：三块中另两块视为未提交，
    # 不参与 diff。与 _h_edit_card 的落点口径逐字一致（闸与落点必须同语义，
    # 否则闸会拦下自己刚放的行）。
    new_has_heading = any(
        ln.strip().startswith("## ") for ln in (new_body or "").splitlines()
    )

    # 规则①：使命区不等即拒（仅在本次提交确实带了使命块时才比）
    if new_has_heading and (old_sec["mission"] or "").strip() != (new_sec["mission"] or "").strip():
        return {"ok": False, "rule": "mission_locked", "message": MSG_MISSION_LOCKED}

    # [2026-10-22 用户裁定] 需求文档块**覆盖写·单一最新**，不再是只追加 ⇒
    # 原「规则②③ 超集校验」整条废除（模型可整块重写需求文档）。

    # 规则⑦：现状块整行出现 D{nn}【 → 拒（决定归需求文档）。
    # 判据对象＝**实际落进现状块的内容**——有区头时是 status_block 子块，
    # 无区头时是整篇正文（它整篇落进现状块），两种形态都要照住。
    status_effective = (new_sec["status_block"] if new_has_heading else (new_body or ""))
    for line in status_effective.splitlines():
        if _DECISION_LINE_RE.match(line.strip()):
            return {
                "ok": False, "rule": "decision_in_status",
                "message": MSG_DECISION_IN_STATUS,
            }

    # 规则④⑤：现状区不校验、修改历史忽略 → 放行
    return {"ok": True, "rule": "pass", "message": ""}


def _exec_perm_gate(
    current_id: str,
    tool_name: str,
    cards: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """**执行层权限闸**（与工具面同一判据）：卡上没有该工具的开关 ⇒ 拒。

    返回 None＝放行；返回错误信封＝拒绝（文案自带出路，验收纪律 4）。

    【为什么必须有这一道】
    工具面（schema）只回答「模型看不看得见」，**不是执行授权**。实测
    work_027/card_021（sub_project、perms 无 ``tools_file``）在 16 小时里
    调了 **646 次 bash ＋ 7 次 python**，每一次都真执行；work_027/card_001
    （project、perms 无 ``delete``）调了 **7 次 delete_card** 也照删。
    机制是三处叠加：① ``execute_tool`` 原先按名字直接派发、零权限校验；
    ② ``prompts.py`` 的 ``_SEC_TOOLS`` 是无条件全量清单，模型从提示词里就知道
    bash/python 存在；③ 工具面把它过滤掉了，但没人管模型硬调。
    ⇒ 「看不见」必须等于「调不动」，否则权限只是提示词里的礼貌用语。

    【判据与工具面完全同源】
    同一张 ``TOOL_SWITCH`` 表、同一个 ``policy.has_perm``（与
    ``_filter_by_category`` 逐字一致），故不会与工具面分叉：
      · 未配开关（unmapped）⇒ 拒（同工具面的「默认关门」）；
      · 卡上无该开关 ⇒ 拒。
    特殊集（``AUTO_ONLY_TOOLS`` / ``OP_ONLY_TOOLS`` / ``FUNCTION_ONLY_TOOLS``）
    不受影响：它们各自挂的开关（edit / plan / read / create_child …）在对应卡上
    本来就有——实测 work_027 两张卡的全部历史调用中，被本闸拒掉的只有
    bash／python／write_file／delete_card 四种，其余 36 种全部放行。

    【它不做什么】
    只管**权限**，不管**流程**。``_MUST_ROUND_DROP`` / ``_AUTO_LADDER_DROP``
    这类「本轮先别动手」的工作流收窄仍只作用于工具面——本闸不看面，故不会
    因流程收窄而拒掉一张有权限的卡。两者是两层，不要混。
    """
    card = (cards or {}).get(current_id)
    if card is None:
        return err("denied",
                   f"认不出当前卡（{current_id}）⇒ 按默认关门，本轮不执行任何工具。")
    # [能力底座 2026-10-30 · 第二刀 批次2] 已迁移的卡按**能力**判，与工具面同源。
    # 不归能力管的工具（回合口子，如 propose_status / write_requirements_final）
    # 一律放行——它们由工具面与轮次闸管，不参与授予。
    mounted = _abilities_of(card, cards)
    if mounted is None:
        # 算不出来（清单坏/卡型不认识）⇒ 老路：按开关判
        switch0 = TOOL_SWITCH.get(tool_name)
        if switch0 is None:
            return err("denied", f"工具 {tool_name} 未配置权限开关（unmapped）⇒ 按默认关门不执行。")
        return None if policy.has_perm(card, switch0) else err(
            "denied", f"本卡未被授予工具 {tool_name}（需要开关「{switch0}」，本卡没有）。")
    if mounted is not None:
        code = _tool_capability(tool_name)
        if code is None:
            return None
        if code in {str(x) for x in mounted}:
            return None
        logger.warning("[perm-gate] 执行层拒绝：card=%s tool=%s cap=%s（卡上没挂这项能力）",
                       current_id, tool_name, code)
        try:
            prompt_log.append_perm_log({
                "fn": "execute_tool",
                "card_id": current_id,
                "card_type": str(card.get("card_type") or ""),
                "granted_tools": [],
                "filtered_tools": {tool_name: code},
                "reason": "exec_capability_gate",
            })
        except Exception:  # noqa: BLE001 打点失败不阻断拒绝
            pass
        return err("denied", (
            f"本卡没挂「{code}」这项能力，所以不能调 {tool_name}。"
            "换写法、换参数或重复调用都不会通过。"
            "若确实需要，请让上级卡或用户在卡详情里给本卡挂上这项能力。"
        ))
    switch = TOOL_SWITCH.get(tool_name)
    if switch is None:
        return err("denied",
                   f"工具 {tool_name} 未配置权限开关（unmapped）⇒ 按默认关门不执行。")
    if policy.has_perm(card, switch):
        return None
    logger.warning("[perm-gate] 执行层拒绝：card=%s tool=%s switch=%s（卡上无此开关）",
                   current_id, tool_name, switch)
    try:
        prompt_log.append_perm_log({
            "fn": "execute_tool",
            "card_id": current_id,
            "card_type": str(card.get("card_type") or ""),
            "granted_tools": [],
            "filtered_tools": {tool_name: switch},
            "reason": "exec_perm_gate",
        })
    except Exception as exc:  # noqa: BLE001 打点失败不阻断拒绝本身
        logger.warning("[perm-gate] 打点失败 card=%s tool=%s: %s",
                       current_id, tool_name, exc)
    return err("denied", (
        f"本卡未被授予工具 {tool_name}（需要开关「{switch}」，本卡没有）。"
        "换写法、换参数或重复调用都不会通过。"
        "若确实需要，请让上级卡或用户在卡详情里给本卡开对应权限；"
        "否则请用本轮工具清单里已有的工具完成任务。"
    ))


async def execute_tool(
    work_id: str,
    current_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    *,
    perm_gate: bool = False,
) -> dict[str, Any]:
    """执行单个工具调用，返回**信封**（成功 {"ok":True,...,"data":{...}}，失败
    {"ok":False,...,"error":{"code","message"}}）。

    ``perm_gate``：**执行层权限闸开关**（缺省 False＝旧行为，既有 155 处测试
    调用点与 handler 直调零改动）。生产主链 ``message_runner`` 传 True——
    见 :func:`_exec_perm_gate` 的理由段。加它是为了让「工具面过滤」之外
    还有一道**真正的**执行授权：看不见 ⇔ 调不动。

    不抛异常（吞掉异常转 internal 错误信封，便于继续工具循环）。

    [B-09 2026-10-08] 由约 300 行 if 链改为查表调用：handler = TOOL_REGISTRY.get(tool_name)，
    命中即调；未命中仍返回未知工具错误信封。三层兜底（CardNotFoundError／
    ChapterNotFoundError／Exception→internal）与错误文案逐字不动。

    [T-0010 §1.4] 出口统一信封：handler 已信封化的原样返回；未信封化的一律包一层——
      - 带 "error" 键的（storage 直传，如 update_card/delete_card 的失败 dict）→ internal
      - 其余（成功 dict）→ ok_(原 dict)，**键集合与文案原样进 data**
    """
    try:
        def_ = TOOL_REGISTRY.get(tool_name)
        if def_ is None:
            return err("not_found", f"未知工具: {tool_name}")
        # [执行层权限闸 2026-10-25] 与工具面同一判据：卡上没有该工具的开关 ⇒ 拒。
        # 位置＝注册表命中之后、handler 之前：未知工具仍走 not_found（文案不变），
        # 已知但无权限的工具在此被拦，**不再有第二条路能把它送到 handler**。
        if perm_gate:
            _denied = _exec_perm_gate(current_id, tool_name, cards)
            if _denied is not None:
                return _denied
        # [工具卡底座 T9 · 执行层角色拦 —— **已回滚，接替未启用**]
        # [用户 2026-10-09 17:38 逐字]「现在先不要把写工具给干掉啊，
        #   等工具卡完善后，再接替啊」
        # 此处原按同一份 WRITE_TOOLS_REVOKED 拦主卡的写工具调用。同
        # `_filter_by_role` 一并回滚——**接替的时机未到**，此刻主卡工具面
        # 为全量。接替启用时，工具面清单（第一道）与本处执行层拦（第二道）
        # 必须同批启用，不得只启一道（否则要么看得见调不动、要么绕过清单硬发）。
        # 权限表查表、非内容判定（R-A）这一性质不变，留待接替时复用。
        # [差事·事实归属 2026-11-01] 行动者绑进上下文：handler 里任何
        # update_card 的卡面写事件都带上 by=current_id——「谁改的」从此
        # 可查（差事交活判据、观测面都靠它）。
        with card_store.actor_ctx(current_id):
            result = await def_.handler(work_id, current_id, arguments, cards)
        # [差事 2026-11-01] 工具落完顺手扫场：deliverable 差事的交活事实
        # （场卡上的写件/回执）可能就此翻完——持久关账+发关账事件。
        # 单点覆盖所有产出通道，不往每个写路径里散钩子。扫不动不拦。
        try:
            from ..storage import function_store as _fs
            await _fs.sweep_deployments(work_id)
        except Exception:  # noqa: BLE001
            pass
        if _is_envelope(result):
            return result
        if isinstance(result, dict) and "error" in result:
            # storage 直传的错误：文案逐字保留，码取 internal（工具层无从细分）
            return err("internal", str(result.get("error")))
        return ok_(result)
    except card_store.CardNotFoundError:
        return err("not_found", f"卡片不存在: {arguments.get('card_id','')}")
    except manuscript_store.ChapterNotFoundError:
        return err("not_found", f"章节不存在: {arguments.get('chapter','')}")
    except Exception as exc:  # noqa: BLE001
        logger.error("execute_tool 异常 tool=%s args=%s: %s", tool_name, arguments, exc)
        return err("internal", str(exc))


# ============================================================
# [影子精准编辑 2026-10-27 · 用户令「把影子卡编辑也全部改成精准编辑」]
# **`update_shadow` 暂留**：新工具 `edit_shadow`（按节精准编辑）已就位，
# 但下线 `update_shadow` 会破 32 个既有用例（它们拿它当"构造影子数据"的载体，
# 且测的是权限闸）。下线要连带改测试，属单独一轮。
# ⇒ 现在两条路并存：模型侧**优先用 `edit_shadow`**（schema 里写明），
#   `update_shadow` 的说明里也标注了「整块覆盖有清空风险，优先用 edit_shadow」。

# [伴生评审卡 2026-10-27] **旧评审工具下线**（评审换代）
#
# 必须放在**文件末尾**——本文件按顺序执行，`review_request` / `submit_review`
# 在下方（~5531）才注册；放在前面 pop 会被后面的注册覆盖回来（实测过）。
#
# 按 P5「结构上不可能」：从注册表移除，模型看不见才不会调——不是写"不推荐"。
# handler 保留（`_h_review_request` / `_h_submit_review`），供历史路径与既有测试直调；
# 它们不再出现在任何卡的工具面上。
# ============================================================
for _legacy_review_tool in ("review_request", "submit_review"):
    TOOL_REGISTRY.pop(_legacy_review_tool, None)
    TOOL_SWITCH.pop(_legacy_review_tool, None)
