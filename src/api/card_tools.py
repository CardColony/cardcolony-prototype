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
from . import card_msg, context_regions, credential_guard, event_stream, role_room
from ..toolcard import driver as toolcard_driver
from ..toolcard import script_loader as toolcard_script_loader
from ..toolcard import step_prompt as toolcard_step_prompt
from ..thinkchain import plan_tools as thinkchain_plan_tools
from . import file_tools, prompt_log

logger = logging.getLogger("api.card_tools")

# [白板版 2026-10-02] 模型可建的**通信线**类型收窄为两个中性关系；
# "锚定"（系统派生目标卡专用）与"兑现"（写作agent残留语义）不再对模型开放
MODEL_REL_TYPES = ("先后", "关联")

# [依赖关联线 2026-10-16] 模型可建的**依赖关联线** kind（第三种线 relations）。
# 与通信线的 type 是两个字段上的概念，重名不影响（任务书 §二.12）。
MODEL_DEP_KINDS = ("依赖", "关联")

# [更新现状按钮改造 2026-10-16] 「更新现状」专用操作回合的操作标记
# （POST …/messages 请求体的 op 字段）。它不是权限开关，是**本轮操作类型**：
#   message_runner 认它 → 轮首注入专用任务（current_turn.build_status_task_event）；
#   build_tool_schemas 认它 → 把 OP_ONLY_TOOLS 临时放进本轮工具面。
OP_UPDATE_STATUS = "update_status"

# [更新现状按钮改造 2026-10-16] **非卡片内挂**的专用工具集。
# 与 CODER_ONLY_TOOLS（按卡类型收敛）/ ROOT_ONLY_TOOLS（按卡 id 收敛）不同——那两类
# 仍是「卡片内挂、按条件收窄」；本类**压根不进常规工具面**（不进四段过滤），只在
# 对应 op 回合由 build_tool_schemas 在过滤之后追加。用户 2026-10-16 口径：
# 「这个工具不是卡片内挂的，我不点按钮，模型是无法看到这个工具并调用这个工具的」。
# 同时为「未来在特定模式下开放给模型」留口——加一种 op 即多一组专用工具。
OP_ONLY_TOOLS: frozenset[str] = frozenset({"propose_status"})

# ============================================================
# [自动模式 2026-10-17 · 回灌 5700 块0.4] 末段追加 · 自动模式专用工具（非卡片内挂）
#
# 与 CODER_ONLY_TOOLS（按卡类型收敛）/ ROOT_ONLY_TOOLS（按卡 id 收敛）/ 
# OP_ONLY_TOOLS（按 op 收敛）不同——本类**压根不进常规工具面**（不进四段过滤），
# 只在 ``auto_mode=1`` 的卡上由 build_tool_schemas 在四段之后追加。
# ⇒ 常规卡片工具面**逐字不变**（红线 2 / 块0.4 硬判据 1）。
# ============================================================
#: 块 0.4：update_goal（声明目标态——「停」的唯一权威在模型）。
#: G2 起扩为三件：＋send_card_message/read_inbox（把卡间通信交回模型）。
AUTO_ONLY_TOOLS: frozenset[str] = frozenset(
    # [v12 2026-10-22 · 用户裁定「coder 至少能给自己的父卡发信」] send_card_message
    # **移出本集**：它原来只随 auto_mode=1 下发，于是非自动卡（含 coder）压根看不见
    # 发信工具——即使 perms 里给了开关。现在改由 TOOL_SWITCH 常规过滤
    # （有 send_card_message 开关就可见），与其它工具同一条规则。
    {"update_goal", "get_goal", "create_goal", "resume_child",
     "read_inbox", "update_status", "append_ruling"})

# ============================================================
# [角色机制 5710] 角色卡专用工具（非卡片内挂）
#
# 与 AUTO_ONLY_TOOLS 同手法：不进常规工具面、不随 perms 过滤；只在
# ``card_type=="role"`` 的当前卡上由 build_tool_schemas 末段追加。
# 角色卡不是作业卡：它只判断、记录判断、必要时发言/警报。
# ============================================================
FUNCTION_ONLY_TOOLS: frozenset[str] = frozenset({
    "record_verdict", "raise_alarm", "correct_alarm",
    "issue_directive", "verify_directive",
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
#   link_rel / unlink_rel                  → link / unlink（依赖关联线，复用同款开关）
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
    "read_shadow": policy.SWITCH_READ,
    # [卡片底座第一步 2026-10-14] 沿通信线读——独立开关（沿连线读比直接读更敏感）
    "read_linked": policy.SWITCH_READ_LINKED,
    # [规则卡 2026-10-15 · 块3] draft_rule_edit 挂 read（起草是读性质，零新开关；
    # 只起草不落盘，落盘走用户审批 /rule/commit）。可见性再由 build_tool_schemas
    # 第四段 ROOT_ONLY_TOOLS 收敛到剧本根卡，执行层 handler 再校验一次（双保险）。
    "draft_rule_edit": policy.SWITCH_READ,
    "edit_card": policy.SWITCH_EDIT,
    # [分块写入 2026-10-19 · permexp] 与 edit_card 同开关：写正文就是写正文。
    "update_status": policy.SWITCH_EDIT,
    "append_ruling": policy.SWITCH_EDIT,
    # [编程卡 2026-10-15 · §六块2 · **v1.1 修订**] report_done 挂 tools_file——
    # 原挂 SWITCH_EDIT 系派发方笔误：tool 卡有 edit 无 tools_file，挂 edit 会让
    # readopen:287 与 :252 在「零翻新」下数学上不可能同时成立。挂 tools_file 后：
    # tool 卡第二段即滤（:287 绿）、sub_project 绕第三段闸可见恰为全量（:252 绿）、
    # coder 有 tools_file 正常可见（判据1 绿）。判定权在第三段闸＋执行层
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
    # [编程卡 2026-10-15 · §六块2] create_coder_card 挂既有 create_child（建
    # coder 子卡走同一建卡关口）：无 create_child 的类型（tool/coder）压根看不见
    # 它，有 create_child 的（project/sub_project）按下发面规则可见。
    "create_coder_card": policy.SWITCH_CREATE_CHILD,
    "link_cards": policy.SWITCH_LINK,
    "unlink_cards": policy.SWITCH_UNLINK,
    # [依赖关联线 2026-10-16] 第三种线（依赖关联线）的建/解线工具——复用 link/unlink
    # 开关（不新开）。写的是卡上另一个字段（relations，非 links），权限同款，
    # 工具面按现有四级过滤机制自动生效。
    "link_rel": policy.SWITCH_LINK,
    "unlink_rel": policy.SWITCH_UNLINK,
    # [更新现状按钮改造 2026-10-16] propose_status 挂 SWITCH_READ——**只为满足
    # 「注册表每件工具都要有开关映射」的完备性不变量**（tests/test_cardperm_readopen.py
    # ::test_tool_switch_covers_every_registered_tool），语义上「只提议不落盘」也是读性质
    # （同 draft_rule_edit 的口径）。**实际下发不走这个开关**：本工具在 OP_ONLY_TOOLS 里，
    # 被 build_tool_schemas 从常规工具面摘除，只在 op=update_status 回合追加（见该函数）。
    "propose_status": policy.SWITCH_READ,
    # [职能机制 card_031] 职能专用工具：只为「注册表每件工具都有开关映射」的完备性。
    # 实际下发不走这些开关（FUNCTION_ONLY_TOOLS 从常规池排除，职能回合末段追加）。
    "record_verdict": policy.SWITCH_READ,
    "raise_alarm": policy.SWITCH_READ,
    "correct_alarm": policy.SWITCH_READ,
    "issue_directive": policy.SWITCH_SEND_MESSAGE,
    "verify_directive": policy.SWITCH_READ,
    # 组阁权挂 create_child（根卡才有），关注管理挂 link；两件都只对根卡下发。
    "create_function": policy.SWITCH_CREATE_CHILD,
    "function_watch_cards": policy.SWITCH_LINK,
    "update_function_profile": policy.SWITCH_CREATE_CHILD,
    "set_function_duty": policy.SWITCH_CREATE_CHILD,
    "list_functions": policy.SWITCH_READ,
    "update_shadow": policy.SWITCH_SHADOW_WRITE,
    "list_manuscript": policy.SWITCH_MANUSCRIPT,
    "read_manuscript": policy.SWITCH_MANUSCRIPT,
    "search_manuscript": policy.SWITCH_MANUSCRIPT,
    "web_search": policy.SWITCH_TOOLS_WEB,
    "fetch_web": policy.SWITCH_TOOLS_WEB,
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
# tools-tc-5：[编程卡 2026-10-15] **工具集变更 ⇒ 递增**：新增第 17 件 report_done
#   （编程卡完工回报）与第 18 件 create_coder_card（主卡派生编程卡并首跑）；
#   build_tool_schemas 增第三段 card_type 闸（CODER_ONLY_TOOLS，report_done 只对
#   coder 下发）。TOOL_REGISTRY 字面量条目数 由 16 → 18（验收 grep 口径见任务书
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
TOOL_TABLE_VERSION = "tools-tc-16"


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


def _schema_of(def_: ToolDef) -> dict[str, Any]:
    """取 schema 字面量（延迟形态在此现取）。"""
    return def_.schema() if callable(def_.schema) else def_.schema


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
}


def _card_activity(work_id: str, card_id: str, meta: dict[str, Any]) -> str:
    """给上级看的一行活性：**运行态 · 最近活动（多久前）· 已跑轮次**。

    [上级可见性 2026-10-19 · permexp] 用户裁定：上级看不见下属在干什么 ⇒ 只能看
    文件大小 ⇒ 误判"零产出" ⇒ 换人。本函数把活性放进 read_card 的返回里（它本来
    就会读子卡），不占常驻上下文、也不需要模型多学一个工具。

    ★ 最近活动必须认 **reasoning**：实测 card_009 连续推理 40 分钟没有一次工具调用，
      只看 tool_call 会判成"没动"。
    """
    parts: list[str] = []

    # ① 运行态：内存里的 stage 优先（正在跑），否则看目标态
    stage = None
    try:
        from . import progress
        stage = (progress.get_stage(work_id, card_id) or {}).get("stage")
    except Exception:  # noqa: BLE001 读不到＝按目标态判
        stage = None
    if stage and str(stage) not in ("", "done"):
        parts.append("运行中")
    else:
        gs = str((meta or {}).get("goal_state") or "")
        if gs == "complete":
            parts.append("已收口")
        elif gs == "blocked":
            parts.append("卡住了")
        elif gs == "paused":
            parts.append("已暂停")
        elif gs == "active":
            # active 只表示「目标还没终止」，不代表此刻在跑——说"空闲"会被读成
            # "它没事干"，说"待推进"才准确（等下一轮/等唤醒）。
            parts.append("待推进")
        else:
            parts.append("空闲")

    # ② 最近活动（类型 + 距今）
    try:
        from ..storage import event_log
        line = _last_event_line(event_log.event_path(work_id, card_id))
        if line:
            rec = json.loads(line)
            label = _EVENT_LABEL.get(str(rec.get("type") or ""), str(rec.get("type") or ""))
            ago = ""
            try:
                ts = datetime.fromisoformat(str(rec.get("ts") or ""))
                mins = int((datetime.now().astimezone() - ts).total_seconds() // 60)
                ago = "刚刚" if mins < 1 else f"{mins} 分钟前"
            except Exception:  # noqa: BLE001 时间戳坏了就不报距今
                ago = ""
            parts.append(f"{label}（{ago}）" if ago else label)
    except Exception:  # noqa: BLE001 活性是增益信息，读不到不阻断 read_card
        pass

    # ③ 已跑轮次
    rounds = (meta or {}).get("goal_rounds")
    if rounds:
        parts.append(f"已跑 {rounds} 轮")

    return " · ".join(parts)


async def _h_read_card(work_id: str, current_id: str,
                       arguments: dict[str, Any],
                       cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """读另一张卡。**跨卡可见性开口**（卡片区域语义 2026-10-14 · v3）。

    此前只回 ``body``＝现状块一项，使命／第三块跨卡读不到（方案 §二.3 缺口）。
    现在一次给全三个正文块：**使命 / 现状 / 第三块**（v3：三块全是对外，
    「职责与边界」已按用户 17:56 令整体取消——边界靠权限卡，不靠文字描述）。

    - 第三块的**对外名由本树 project_class 决定**（rd_project → requirements；
      其余 → rulings），与 routes._apply_block_set 同一判据、同一口径。
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
    try:
        policy.check(current_id, target_id, policy.SWITCH_READ, cards)
    except PermissionError:
        return err("denied", f"越权：无权读取 {target_id}")
    result = await card_store.read_card(work_id, target_id)
    body = result["body"] or {}
    # [角色机制 5710 v2] 角色卡返回**独立结构**（管辖/关注/发言契约/升级线/
    # 值班/判断簿/警报簿/职责书），不再拿普通卡三块冒充。
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
    # 第三块在库内恒为 rulings 一列；对外叫什么名由本树 project_class 决定。
    try:
        pc = card_store._root_project_class(work_id)
    except Exception:  # noqa: BLE001  读路径宽容：缺省按非研发树
        pc = ""
    third_key = "requirements" if pc == "rd_project" else "rulings"
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
        "links": result["meta"].get("links") or [],
        "relations": result["meta"].get("relations") or [],
        "body": body.get("status_block", ""),
        "使命": body.get("mission", ""),
        "现状": body.get("status_block", ""),
        third_key: body.get("rulings", ""),
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
    # [权限收口 2026-10-09] 统一关口（读）：拒绝转既有 denied 信封；
    # 不存在已在上面走 not_found，关口不再见到缺卡
    try:
        policy.check(current_id, target_id, policy.SWITCH_READ, cards)
    except PermissionError:
        return err("denied", f"越权：无权读取 {target_id}")
    state = memory_store.load_memory(work_id, target_id)
    entries = [e for e in (state.get("entries") or []) if isinstance(e, dict)]
    lo = arguments.get("from_seq")
    hi = arguments.get("to_seq")
    if isinstance(lo, int) and not isinstance(lo, bool):
        entries = [e for e in entries if isinstance(e.get("seq"), int) and e["seq"] >= lo]
    if isinstance(hi, int) and not isinstance(hi, bool):
        entries = [e for e in entries if isinstance(e.get("seq"), int) and e["seq"] <= hi]
    return {"card_id": target_id, "mark": state.get("mark", 0), "entries": entries}


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
    )
    if "error" in result:
        return result
    # 建卡同步刷新 meta 缓存：同轮后续工具（如 link_cards→新卡）能看到新卡
    cards[result["meta"]["card_id"]] = {
        **result["meta"], "card_id": result["meta"]["card_id"],
    }
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

    # [G4 · 61b83fb/b6e339a · BUG-AM-004 修法②] 收口前查收件箱：本卡仍有
    # reply_expected 的**实质未回应**询问（典型 msg_ask）⇒ 拒 complete——
    # 「不许带着未回答的询问收工」（注册表口径：收到 msg_ask 不许沉默，沉默
    # 会让发问方永远等）。pause/blocked 不拦（说清在等什么本身是合法出口）。
    # complete 是唯一的「宣称全部做完」动作，须闭环放行。
    # 判据纯机械（_unanswered_asks）：pending 里有 reply_expected 且全账本
    # 无 answer 指向它。拒绝信息自带出路（验收纪律 4：拒绝必须自带出路，
    # 否则自主模型会换姿势重试甚至去读源码）。
    from ..storage import card_db

    async def _unanswered_asks() -> list[dict[str, str]]:
        """本卡收到的、**实质未回应**的询问（reply_expected 且全账本无 answer 指向它）。

        闸与 blocked 通知共用同一判据（修法甲口径）：answer 一落库即算回应，
        不依赖收件方后续回合收口 ⇒ 无陈旧窗（曾造出 work_027 双卡死锁）。
        """
        try:
            pend = await card_db.list_pending(work_id, current_id)
            all_rows = await card_db.list_inbox(work_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[自动模式] 查未回应询问失败 card=%s: %s", current_id, exc)
            return []
        answered = set()
        for r in (all_rows or []):
            rt = str((r or {}).get("reply_to") or "")
            if rt:
                answered.add(rt)
        return [it for it in (pend or [])
                if card_msg.reply_expected_of(str((it or {}).get("type") or ""))
                and str((it or {}).get("msg_id") or "") not in answered]

    if action == "complete":
        # [角色机制 5710 v3] **指令闸**：目标卡上有未验收的角色指令
        # （issued/rejected/overdue）时不许收口；出路由角色 verify_directive
        # 判 complied/cancelled，或先把卡退回 blocked 说明卡在哪。
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
                       f"（{_desc}）。先按指令做完，让角色 verify_directive(complied)，"
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
            _body += "\n" + str(((_cur or {}).get("body") or {}).get("rulings") or "")
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
        _unwritten = "（当前状况快照：" in _body or "（本卡全部需求的确定版" in _body
        _req = str(((_cur or {}).get("body") or {}).get("rulings") or "")
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

        _unanswered = await _unanswered_asks()
        if _unanswered:
            _ids = [str((it or {}).get("msg_id") or "") for it in _unanswered]
            return err("validation",
                       "收口被拒：你还有未回应的询问（不许沉默——发问方在等你的回答，"
                       f"收到 msg_ask 不答就收工会把对方晾死）。未回应 msg_id：{_ids}。"
                       "出路：先用 send_card_message 发 msg_answer（reply_to 填上面的 msg_id，"
                       "内容＝你的回答），再调 update_goal(complete)。"
                       "若确实答不了，用 update_goal(blocked, reason=...) 说明卡在哪——"
                       "blocked 后系统会把你的理由自动通知发问方，不是沉默。")

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
    if action == "blocked":
        _asks = await _unanswered_asks()
        _title = str((cards.get(current_id) or {}).get("title") or current_id)
        for _sender in {str((it or {}).get("from_card") or "") for it in _asks}:
            if not _sender or _sender == current_id:
                continue
            _ids = [str((it or {}).get("msg_id") or "") for it in _asks
                    if str((it or {}).get("from_card") or "") == _sender]
            try:
                _nres = await send_card_message(
                    work_id, current_id, target_id=_sender, msg_type="msg_notify",
                    content=(
                        f"系统通知：《{_title}》（{current_id}）已声明 blocked（做不下去）。"
                        f"其上报理由：{reason or '（未填）'}。\n"
                        f"你此前发给它的询问（msg_id：{_ids}）未获回答——它不会自己回信了。\n"
                        f"请据此调整：可沿通信线读它的对话/影子取细节，或改换手段；"
                        f"若你要等它，本卡不会自动恢复。"
                    ))
                if isinstance(_nres, dict) and _nres.get("error"):
                    logger.warning("[自动模式] blocked 通知发送失败 card=%s→%s: %s",
                                   current_id, _sender, _nres.get("error"))
            except Exception as exc:  # noqa: BLE001 通知失败不阻断 blocked
                logger.warning("[自动模式] blocked 通知异常 card=%s→%s: %s",
                               current_id, _sender, exc)
    # 目标终止 ⇒ **释放执行锁**（串行模式下队列前进；并行模式为 no-op）
    from . import serial_gate
    serial_gate.release(work_id, current_id)
    return {"ok": True, "action": action, "goal_state": state, "reason": reason}


# ============================================================
# [编程卡 2026-10-15 · §六块2] 编程卡两件工具的 handler
#
# report_done（第 17 件·完工收口）与 create_coder_card（第 18 件·派生即调起）。
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


async def _h_report_done(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[编程卡 2026-10-15] 完工回报（系统收口件，模型只报不判）。

    四步机械动作（§六块2）：
      ① 判态：当前卡 card_type=="coder" 且 state=="active"，否则 denied；
      ② 落盘：state=completed（闸4：active→completed 在册）＋ front matter
         ``coder_final`` 原样收下 summary；
      ③ 影子 ``## 迭代历史`` 追加完工行（时间＋summary＋write_file 调用数，
         机械统计、不判好坏）；
      ④ 系统发信：经流程专用薄壳 send_card_message（六道门槛一条不减；头部
         由 build_header 生成，content 只给 summary）向 owner＝parent 卡发
         coder_done —— 落信即记 TRIGGERS，编程卡回合收口时 K3 调起主卡验收
         （现有链，零改）。

    权限：不设新开关——工具面由 build_tool_schemas 第三段 CODER_ONLY_TOOLS
    收敛到 coder；执行层本函数再校验 card_type（双保险）。
    """
    # ① 判态（先用本轮 cards 字面快照，再读库内实况——state 是库内事实）
    card = (cards or {}).get(current_id) or {}
    if str(card.get("card_type", "") or "") != "coder":
        return err("denied", "report_done 仅限编程卡调用")
    current = await card_store.read_card(work_id, current_id)
    meta = current["meta"]
    state = str(meta.get("state", "") or "")
    if state != "active":
        return err("denied", f"编程卡当前状态为 {state or '未知'}，不可完工")
    summary = str(arguments.get("summary", "") or "").strip()
    if not summary:
        return err("validation", "summary 不能为空")
    # [角色机制 5710 v3] 编程卡同样受指令闸约束：有未验收的角色指令不许完工。
    try:
        _blockers = role_room.open_directives_for(work_id, current_id)
    except Exception:  # noqa: BLE001
        _blockers = []
    if _blockers:
        _desc = "；".join(f"{d.get('id')}: {str(d.get('instruction') or '')[:60]}"
                          for d in _blockers[:3])
        return err("validation",
                   f"完工被拒：本卡还有 {len(_blockers)} 条未验收的角色指令"
                   f"（{_desc}）。先完成并让角色 verify_directive(complied)。")
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
        f"编程卡完工收口（report_done）：{summary[:80]}",
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
    sent = await send_card_message(
        work_id, current_id, target_id=owner,
        msg_type="coder_done", content=summary,
    )
    if isinstance(sent, dict) and sent.get("error"):
        # 落信失败：卡已完工、影子已留痕——错误如实回给模型（不回滚终态）
        logger.error("report_done 回执发信失败 card=%s owner=%s: %s",
                     current_id, owner, sent)
        return err("internal", f"完工回执发送失败: {sent.get('error')}")
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


async def _h_create_coder_card(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[编程卡 2026-10-15] 派生编程卡并**建卡即首跑**（派生即调起）。

    §六块2：
      ① 建卡关口＝CREATE_CHILD（照 _h_create_card 同口径，parent＝当前卡自身）；
      ② card_store.create_card(card_type="coder", parent=当前卡,
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
    _coder_parent = str(arguments.get("parent", "") or "").strip() or current_id
    if _coder_parent not in cards:
        return err("not_found", f"父卡不存在: {_coder_parent}")
    if not _org_scope_allows(current_id, _coder_parent,
                             policy.SWITCH_CREATE_CHILD, cards):
        return err(
            "denied",
            f"越权：不能把执行卡建到 {_coder_parent} 之下"
            f"（当前卡 {current_id}）",
        )
    # ② 建 coder 卡：charter 带 ## 使命 区头 ⇒ 区头检测后原样入正文，任务落使命块
    # [2026-10-19 实验 permexp] **补 goal**：原先不传 goal ⇒ 自动模式下 coder 卡
    # goal 空、goal_state 不落 ⇒ auto_driver.will_continue 恒 False ⇒ 只有下面
    # ③ 的首跑一次，「一轮做不完就停在那里」。任务原文即目标文本（使命块同源）。
    result = await card_store.create_card(
        work_id, title, "模型生成",
        card_type="coder", parent=_coder_parent,
        charter=f"## 使命\n{task}",
        goal=task,
        # [调度改造 2026-10-19] 本工具**自己负责首跑**（下面 ③ `_spawn_tracked`
        # 带任务正文）。若建卡也点一次，会起一个没有任务正文的空回合，把正式首跑
        # 挡在互斥体外面 ⇒ coder 卡零产出（实测 test_coder_card 两条都挂）。
        skip_kick=True,
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
        logger.error("create_coder_card 首跑拉起失败 card=%s: %s", new_id, exc)
        started = False
    return {
        "ok": True,
        "card_id": new_id,
        "title": title,
        "parent": current_id,
        "card_type": "coder",
        "started": started,
        "shadow": "已派生影子卡（过程记录，你不用管）",
    }


async def _h_draft_rule_edit(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """[规则卡 2026-10-15 · 块3] 根卡起草规则区修改——**只起草不落盘**。

    机械四步：
      ① 根卡闸：current_id 必须是剧本根卡（work_card_id_of），否则 denied；
      ② 区名校验：合法区名＝注册表全词表 codes（rule_section_names，恰六区）；
      ③ 读规则卡对应区当前内容（查首张 rule 卡的 mission 列，纯读零写；
         无规则卡 → not_found）；
      ④ 返回 ``{draft, base_hash, section, old, rule_card_id}``——draft 即提议
         新内容、old 即盘上现文、base_hash＝sha256(old)（供 /rule/commit 乐观
         比对）。**全程零写入**：落盘只能由用户在弹窗点「同意」后走
         ``POST /works/{w}/rule/commit``。

    权限：TOOL_SWITCH 挂 SWITCH_READ；工具面由第四段 ROOT_ONLY_TOOLS 收敛到
    根卡——本函数开头的闸是执行层双保险（同 report_done 手法）。
    """
    if current_id != work_store.work_card_id_of(work_id):
        return err("denied", "draft_rule_edit 仅限剧本根卡调用")
    section = str(arguments.get("section", "") or "")
    names = context_regions.rule_section_names(card_store.load_type_registry())
    if section not in names:
        return err("validation", f"非法区名: {section}（只允许 {names}）")
    draft = str(arguments.get("content", "") or "")

    def _fn(conn: Any) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT card_id, mission FROM cards WHERE card_type = 'rule' "
            "ORDER BY card_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {"card_id": row["card_id"], "mission": row["mission"] or ""}

    row = card_db.read_sync(work_id, _fn, None)
    if row is None:
        return err("not_found", "规则卡不存在（本 work 尚未创建 rule 卡）")
    old = context_regions.split_rule_sections(row["mission"]).get(section, "")
    return {
        "draft": draft,
        "base_hash": hashlib.sha256(old.encode("utf-8")).hexdigest(),
        "section": section,
        "old": old,
        "rule_card_id": row["card_id"],
    }


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
        f"## 需求文档\n{b.get('rulings', '')}\n"
    )


async def _h_edit_card(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target_id = arguments.get("card_id", "")
    content = arguments.get("content", "")
    # [语义修正 2026-10-02] 先判存在再判权限
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    # [影子卡 2026-10-03] 影子条目进字典后子树判据会放行正卡的上级，
    # 此守卫封死该后门：影子只能由对应正卡经 update_shadow 修改（R-06/R-09 写侧）
    if cards[target_id].get("card_type") == "shadow":
        return err("denied", "影子卡只能由对应正卡经 update_shadow 修改")
    # [权限底座重做 2026-10-12] 关口 EDIT：目标卡 perms 须授 edit；树类开关再
    # 收紧为「操作者须在目标上游」
    try:
        policy.check(current_id, target_id, policy.SWITCH_EDIT, cards)
    except PermissionError:
        return err("denied", f"越权：无权编辑 {target_id}")
    current = await card_store.read_card(work_id, current_id)
    cur_title = current["meta"].get("title", "")
    history_note = f"由「{cur_title}」对话编辑内容"
    # [卡片存储迁移 SQLite 2026-10-14] 取数路径：读 target 的正文三块（库列），
    # 由 _card_body_text 重组为可拆块的正文——``read_card`` 的 ``raw`` 已退役
    # （它原是 md 整篇文本）。current 只为取 cur_title，两处都走同一 read_card。
    old_body = _card_body_text(await card_store.read_card(work_id, target_id))
    # [S3-1] 写入闸：放行才继续；拒绝即返回，不落盘
    verdict = _check_block_rules(old_body, content)
    if not verdict["ok"]:
        # [L-2] 记命中的规则与分区名，**不含正文内容**
        logger.info(
            "写入闸拒绝: card=%s rule=%s", target_id, verdict["rule"],
        )
        return err("validation", verdict["message"])
    # [S3-6] 落点改造：正文级覆盖写——不再硬编码塞单个键。
    # 整篇正文过拆分器后由 update_card 归一落盘，三块各归各位。
    #
    # [S3-6 配套 · 方案未列，本环新发现] 无区头正文的语义归一：
    # 模型交来一段不带任何 "## " 区头的纯文本时，拆分器三块全空，
    # 若三块全写会把使命与需求文档一起清空 → 必然触使命闸（V2），
    # 等于「模型永远无法 edit」。故：无区头正文按「这是现状正文」处理，
    # 只写 status_block，使命与需求文档沿用卡内现状。
    # 这与 S1-2 建卡的区头检测同一套判据、同一语义。
    has_heading = any(
        ln.strip().startswith("## ") for ln in content.splitlines()
    )
    new_sections = split_body_sections(content)
    if has_heading:
        updates = {
            "mission": new_sections["mission"],
            "status_block": new_sections["status_block"],
            "rulings": new_sections["rulings"],
        }
    else:
        updates = {"status_block": content}
    result = await card_store.update_card(
        work_id, target_id, updates, history_note,
    )
    if "error" in result:
        return result
    return {"card_id": target_id, "ok": True}


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
#   append_ruling  覆盖写需求文档块（要什么、验收标准是什么）
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
        return err("denied", f"越权：无权编辑 {target_id}")
    return None


async def _h_set_child_mission(work_id: str, current_id: str,
                               arguments: dict[str, Any],
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """上级给子树内的卡设定/修订使命（立项定稿）。

    三条不变量（改这个 handler 时不要破）：
    1. 只能改**子树内**的卡，且目标不是自己——自己的使命由上级或用户定；
    2. 关口与 create_card 同一把（SWITCH_CREATE_CHILD）：能建子卡者才能立使命，
       执行体（coder）没有该开关 ⇒ 既不能改自己也不能改别人；
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
    try:
        policy.check(current_id, target_id, policy.SWITCH_CREATE_CHILD, cards)
    except PermissionError:
        return err("denied",
                   f"越权：{target_id} 不在你的子树内，或你无权给它立使命")
    result = await card_store.update_card(
        work_id, target_id, {"mission": mission},
        f"{current_id} 设定其使命（父卡立使命）",
    )
    if "error" in result:
        return result
    return {"ok": True, "card_id": target_id, "mission_chars": len(mission)}



async def _h_update_status(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """只写「现状」块：使命与需求文档一个字都不碰，调用方也不需要知道它们长什么样。"""
    target_id = str(arguments.get("card_id") or current_id)
    content = str(arguments.get("content") or "")
    if not content.strip():
        return err("validation", "content 不能为空")
    blocked = _guard_edit(work_id, current_id, target_id, cards)
    if blocked is not None:
        return blocked
    # 语义纪律仍在：现状块是给下游读的事实，不装决定（决定走 append_ruling）。
    for line in content.splitlines():
        if _DECISION_LINE_RE.match(line.strip()):
            return err(
                "validation",
                f"这一行是决定条目，不能放现状块：{line.strip()[:40]}"
                "——改用 append_ruling 追加到需求文档；现状块只写能核实的事实。",
            )
    current = await card_store.read_card(work_id, current_id)
    cur_title = (current.get("meta") or {}).get("title", "")
    upd = await card_store.update_card(
        work_id, target_id, {"status_block": content},
        f"由「{cur_title}」更新现状",
    )
    if isinstance(upd, dict) and upd.get("error"):
        return err("internal", str(upd["error"]))
    return {"card_id": target_id, "updated": "status", "chars": len(content)}


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
    # [权限底座重做 2026-10-12] 关口 DELETE：目标卡 perms 须授 delete；树类开关
    # 再收紧为「操作者须在目标上游」
    try:
        policy.check(current_id, target_id, policy.SWITCH_DELETE, cards)
    except PermissionError:
        return err("denied", f"越权：无权删除 {target_id}")
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


async def _h_update_shadow(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [影子卡 2026-10-03] schema 无 card_id——写入目标恒为当前卡自己的影子
    # （R-09 写侧：不是可绕过的校验规则，是工具签名本身写不出别的目标）
    # [T5 补漏检口 2026-10-09][权限底座重做 2026-10-12] 关口判定：当前卡分类
    # 须授 shadow_write（配置里注册类型全授＝照写；未配置类型拒）。
    # 拒绝转既有 denied 信封。
    try:
        policy.check(current_id, current_id, policy.SWITCH_SHADOW_WRITE, cards)
    except PermissionError:
        return err("denied", f"越权：无权写影子（当前卡 {current_id}）")
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
    try:
        policy.check(current_id, target_id, policy.SWITCH_READ, cards)
    except PermissionError:
        return err("denied", f"越权：无权读取 {target_id} 的影子")
    raw = await shadow_store.read_shadow_raw(work_id, target_id)
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
            "rulings": body.get("rulings", ""),
        }}
    if part == "影子":
        raw = await shadow_store.read_shadow_raw(work_id, target_id)
        if raw is None:
            return {"ok": False, "reason": "not_found",
                    "message": f"影子卡不存在: {target_id}"}
        return {"ok": True, "part": part, "card_id": target_id, "value": raw}
    if part == "对话":
        return {"ok": True, "part": part, "card_id": target_id,
                "value": event_log.timeline(work_id, target_id)}
    return {"ok": False, "reason": "unknown_part",
            "message": f"未知 part: {part!r}（合法值：{list(READ_VIA_PARTS)}）"}


async def _h_read_linked(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """沿通信线读对方卡（模型工具 read_linked 的执行体）。

    权限＝只判**发起方**有 ``SWITCH_READ_LINKED``（与 read 分开授权——沿连线读比
    直接读更敏感）。[通信不看对方 2026-10-22 · 用户令] 原来走 ``policy.check``，
    会连**目标卡**有没有 read_linked 一起查——那等于「对方同意被读」，而 perms 是
    建卡时抄的静态值、没有任何同意动作（coder/tool/rule 因此读不了）。线本身就
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
    if not policy.has_perm(cards.get(current_id), policy.SWITCH_MANUSCRIPT):
        return err("denied", "无权查阅本项目原始资料（manuscript 开关未授权）")
    chapters = manuscript_store.list_chapters(work_id)
    if not chapters:
        return {"chapters": [], "note": "本项目尚无原文"}
    return {"chapters": chapters}


async def _h_read_manuscript(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not policy.has_perm(cards.get(current_id), policy.SWITCH_MANUSCRIPT):
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
        return bool(target_id) and target_id in cards and policy.has_perm(
            cards.get(current_id), switch)
    if _is_offtree_role(current_id, cards):
        if not target_id or target_id not in cards:
            return False
        # 写自己：照旧放行（自己算自己作用域内——原 policy.check 同口径）
        if target_id == current_id:
            return policy.has_perm(cards.get(current_id), switch)
        if target_id == _root_id_of(cards):
            return False          # 不许动根卡
        # [角色不改别人卡面 2026-10-22] 编辑权对离树卡**一律拒**：角色/职能的职责是
        # 观察与判断，它写自己的现状走自己的口，不该改别人的卡面（原 policy.check
        # 靠子树判定天然拒；改用组织权后作用域放到全树，必须显式挡回来）。
        if switch == policy.SWITCH_EDIT:
            return False
        # 离树卡自身仍须持有该开关（权限面不因作用域扩大而变大）
        return policy.has_perm(cards.get(current_id), switch)
    try:
        policy.check(current_id, target_id, switch, cards)
        return True
    except PermissionError:
        return False


def _require_wire_switch(current_id: str, switch: str,
                         cards: dict[str, dict[str, Any]],
                         what: str) -> dict[str, Any] | None:
    """建线／解线的门槛：**只看发起方**，不看目标卡。通过返回 None。

    [连线不看对方 2026-10-22 · 用户令] 原来走 ``policy.check(..., SCOPE_ANY)``，
    而 ``check`` 的第一级是「**目标卡** perms 须授 link」——理由是「线是双向的，
    被连方也要能回话」。这个「同意」是假的：
      · 目标卡的 perms 是建卡时从类型模板抄下来的静态值，没有任何「同意」动作，
        用户也没有任何地方能表示同意／拒绝；
      · 实际效果只是「role / coder / tool / rule 四类永远连不上」（它们没有 link）。
    线的记录写在**发起方**卡上（``links`` / ``relations`` 字段），被连方不需要同意；
    真正需要双方的是**发信**，那一关由 ``check_linked``（两卡之间已有线）把着，
    与 perms 无关。
    ⇒ 门槛只判发起方有没有这个开关。打点照旧（fn="check" 同口径）。
    """
    ok = policy.has_perm(cards.get(current_id), switch)
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
        return err("validation", f"关系类型非法: {rel_type}，只允许 先后/关联")
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
      4. 新父卡类型须允许有子卡（工具卡/角色卡/编程卡/规则卡不可）——由存储层读
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
    权限：只判**发起方**有 ``SWITCH_LINK``——不看目标卡、不叠树作用域
    （[连线不看对方 2026-10-22]；原来 docstring 写的「树类收紧：只能连自己子树内」
    是 2026-10-20 就已撤销的化石，实现与注释不符，这次一并对齐）。
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
    bad = _require_wire_switch(current_id, policy.SWITCH_LINK, cards, "建线")
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
    权限：只判**发起方**有 ``SWITCH_UNLINK``（[连线不看对方 2026-10-22]）；
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
        bad = _require_wire_switch(current_id, policy.SWITCH_UNLINK, cards, "解线")
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
    if not policy.has_perm(cards.get(current_id), policy.SWITCH_MANUSCRIPT):
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
            ),

            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id，如 card_002"},
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
            "description": "下探指定卡的凝练记忆（condense_fold：已凝练历史经归拢的条目，含【结论】/【依据】）。可用块序号区间按锚点取回。记忆条目不是卡片内容——三个块请用 read_card。",
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
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id"},
                    "content": {
                        "type": "string",
                        "description": "整份内容（覆盖写，按三个区头分段；第三块区头用本树的名字）",
                    },
                },
                "required": ["card_id", "content"],
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
    # 权限＝create_child ＋ 树作用域：能建子卡者才能立使命；coder 没有该开关
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
    # [分块写入 2026-10-19 · permexp] 一个接口一种语义（自动模式面专用，
    # 见 _h_update_status 上方长注释）。普通卡不下发（仍走 draft/commit）。
    "update_status": ToolDef(
        schema={
            "name": "update_status",
            "description": (
                "更新这张卡的「现状」块——只改现状，使命与第三块一个字都不碰。"
                "现状写**能核实的事实**：已经做成了什么（交付物在哪、能跑的命令、"
                "跑出什么结果）、对外接口是什么、现在卡在哪。"
                "不写计划、不写「我打算怎么做」、不写第三块该写的内容（第三块用 append_ruling）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id（缺省＝当前卡）"},
                    "content": {"type": "string", "description": "新的现状内容（整块替换）"},
                },
                "required": ["content"],
            },
        },
        handler=_h_update_status,
        readonly=False,
    ),
    "append_ruling": ToolDef(
        schema={
            "name": "append_ruling",
            "description": (
                "写这张卡的**需求文档**块（按需求逐条写清「要什么、验收标准是什么」；只写确定版）。"
                "**两种模式**：①精准编辑（推荐）——给 old_string＋new_string，只改那一处，"
                "old_string 必须在块里恰好出现一次，否则报错且不动块；小改小修用它，别整块重抄。"
                "②整块写——只给 text，整块替换（骨架占位可直接覆盖）。"
                "两者都不是「只追加」：改错了可以再改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id（缺省＝当前卡）"},
                    "text": {
                        "type": "string",
                        "description": "整块写时的需求文档全文（与 old_string/new_string 二选一）",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "精准编辑：要被替换的原文（必须在块里恰好出现一次）",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "精准编辑：替换成什么",
                    },
                },
            },
        },
        handler=_h_append_ruling,
        readonly=False,
    ),
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
    "update_shadow": ToolDef(
        schema={
            "name": "update_shadow",
            "description": (
                "覆盖写你自己（当前卡）的影子卡——过程性记录。无 card_id 参数，只能写当前卡"
                "自己的影子。影子卡内容有且只有四节，每节内容边界（不得越界、不得改节名、"
                "不得加节）：【进度】只放本卡推进的时间线记录（什么时间、做到哪一步、当前"
                "什么状态），不写结论/分析/无关杂事；【待协作事项】只放需要其他卡/模块提供的"
                "输入、待办、阻塞、协作关系，不放产物位置（那是产物清单）、不放改动记录"
                "（那是迭代历史）；【产物清单】只放本卡推进中产生的文件/代码/文档的路径与用途"
                "指针（一行一个：路径＋一句话说明它是什么），只写指针不搬运内容；"
                "【迭代历史】只放改动指针（改了什么、为什么改），细节回既有通道查、不在此"
                "复述。三条禁止：①不许把正卡的内容写进影子卡（正卡写事实与第三块、影子卡写过程，"
                "不得混）；②不许写与本卡推进无关的内容；③不许把影子卡当备忘录堆杂事。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "影子内容全文（覆盖写）。必须含且仅含四节，节名固定：## 进度 / ## 待协作事项 / ## 产物清单 / ## 迭代历史。每节只放该节边界内的内容（进度＝时间线；待协作事项＝需他卡/模块提供的输入、待办、阻塞、协作关系；产物清单＝路径与用途指针；迭代历史＝改动指针），不得写入正卡内容、不得写无关内容、不得堆杂事。"},
                },
                "required": ["content"],
            },
        },
        handler=_h_update_shadow,
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
    # [编程卡 2026-10-15 · §六块2] 第 17 件工具：完工回报。追加在既有 16 件字面量
    # 之后 ⇒ 前 16 件的工具面顺序逐字不变。下发面：TOOL_SWITCH 挂 edit ＋
    # build_tool_schemas 第三段 CODER_ONLY_TOOLS（只对 coder 卡下发）；执行层
    # handler 再校验 card_type=="coder" 且 state=="active"（双保险）。
    "report_done": ToolDef(
        schema={
            "name": "report_done",
            "description": (
                "任务做完时调用，调完即完工、本卡静默（编程卡专用）。"
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
    # [编程卡 2026-10-15 · §六块2] 第 18 件工具：派生编程卡并建卡即首跑（派生
    # 即调起）。挂 create_child（无该开关的类型压根看不见）；执行层关口与
    # create_card 同口径（只能建自己的子卡片）。
    "create_coder_card": ToolDef(
        schema={
            "name": "create_coder_card",
            "description": (
                "把一件开发任务交给新建的编程卡执行：建一张编程卡（任务原样落进"
                "它的使命块）并立即自动开跑。一次派一张；只能建自己的子卡片。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "编程卡卡名，如「修复监听器泄漏」"},
                    "task": {"type": "string", "description": "开发任务内容（原样落进新卡的使命块）"},
                },
                "required": ["title", "task"],
            },
        },
        handler=_h_create_coder_card,
        readonly=False,
    ),
    # [规则卡 2026-10-15 · 块3] 第 19 件工具：根卡起草规则区修改（只起草不落盘）。
    # 追加在既有 18 件字面量之后 ⇒ 前 18 件工具面顺序逐字不变。下发面：TOOL_SWITCH
    # 挂 read ＋ build_tool_schemas 第四段 ROOT_ONLY_TOOLS（只对剧本根卡下发）；
    # 执行层 handler 再校验 current_id==work_card_id_of（双保险，同 report_done 手法）。
    # readonly=True：全程零写入（落盘走用户审批 /rule/commit，不经过本工具），
    # 折叠【依据】按「可按原参数重取」口径成立。
    "draft_rule_edit": ToolDef(
        schema={
            "name": "draft_rule_edit",
            "description": (
                "起草规则卡某一分区的修改提议（仅剧本根卡可用；只起草、不落盘）。"
                "返回提议内容与盘上现文、指纹——你不能直接改规则，本工具只出草案；"
                "调用后修改将弹窗交用户审批，用户点「同意」才写入规则卡生效。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "规则卡分区名（六区之一：project/sub_project/tool/coder/rule/topic，对应各卡类型的注入区）",
                    },
                    "content": {
                        "type": "string",
                        "description": "该分区的提议新内容（整区替换草案）",
                    },
                },
                "required": ["section", "content"],
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
                "在当前卡与目标卡之间建立一条**依赖/关联**关系线（第三种线：记录"
                "依赖什么、关联什么；**不用于通信**）。source 固定为当前对话卡。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标卡 id，如 card_003"},
                    "kind": {
                        "type": "string",
                        "enum": ["依赖", "关联"],
                        "description": "关系种类：依赖／关联",
                    },
                    "detail": {
                        "type": "string",
                        "description": "写清依赖什么／关联什么（必填，自由文本）",
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

    旧通道 `resume_child` 只能恢复「auto + 有 goal + 未完成 + 子树内」的卡，
    所以非 auto 卡、无 goal 卡、已完成卡、离树角色卡全都唤不醒。
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
            "新父卡的类型必须允许有子卡（工具卡/角色卡/编程卡/规则卡不行）。"
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
TOOL_REGISTRY["wake_card"] = ToolDef(
    schema={
        "name": "wake_card",
        "description": (
            "让一张卡**现在就跑一轮**——不论它是否自动模式、有没有目标、"
            "是否已完成；角色卡也可以被唤醒。跑完它自己决定做什么（可以什么都不做）。\n"
            "· 作用域：项目根卡可唤醒本项目**任何**卡（含离树的角色卡）；"
            "其他卡只能唤醒自己作用域内的卡。\n"
            "· 与 resume_child 的区别：resume_child 是「恢复自动推进的目标」，"
            "要求目标卡有 auto+goal 且未完成；wake_card 只要求「叫得动」。\n"
            "· 建议在 why 里写清为什么要它跑，它会读到这句话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "要唤醒的卡 id"},
                "why": {"type": "string",
                        "description": "为什么要它跑一轮（它会读到；不写则给一句通用说明）"},
            },
            "required": ["card_id"],
        },
    },
    handler=_h_wake_card,
    readonly=False,
)

TOOL_SWITCH["wake_card"] = policy.SWITCH_CREATE_CHILD


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
    模型侧 True（它本来就是"这张卡能不能发信"）；系统侧 False——coder 卡刻意
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
        # [5713 同步进 5700 · 兼容退回 2026-10-20] 5713 原口径是**纯线**：父↔子
        # 之间没有线也一律 DENIED（纯函数实测 5713 policy：card_001→card_004
        # DENIED、card_004→card_001 DENIED）。原样搬到生产会顺手收走本来能走的
        # 纵向通道——现存所有「子卡发信回报主卡」的协作会立刻断掉，那是对既有
        # 行为的回归。故补一条退回：**已有通信线 OR 互为祖先-后代**，任一成立
        # 即放行。线是**新增的平级通道**，不是对纵向通道的替代；纵深与横向并存。
        try:
            policy.check_linked(current_id, target_id, cards)
        except PermissionError:
            if not (policy._is_in_subtree(current_id, target_id, cards)
                    or policy._is_in_subtree(target_id, current_id, cards)):
                return err(
                    "denied",
                    f"越权：{target_id} 与你既没有通信线、也不是祖先-后代——"
                    f"卡间通信走专门的通信线，请先用 link_cards 建一条指向它的线"
                    f"（type 取 关联），再发信")
    # 落信 + 留痕 + 记触发意图（错误在前面全走 err 信封不落档——发信成功才落信）
    #
    # [卡片底座第一步 2026-10-14] **落点改**：从「塞对话块（session_facade.append）」
    # 改为「进待处理收件箱（card_db.append_inbox, status=pending）」。通信的本质是
    # 「消息进入 B 模型的处理流程」，不是「存哪」；待处理从此是**显式状态**，
    # 不再靠 find_pending 从对话顺序推导（脆弱约定）。对话块只放 B 自己的话。
    # 六道门槛、权限关口、通知帧一条不减；事件流留痕照旧（见下 emit_message_event）。
    msg_id = card_msg.make_msg_id(current_id)
    body = card_msg.build_header(msg_id, mt, current_id, reply_to) + "\n" + content
    await card_db.append_inbox(
        work_id, msg_id=msg_id, target_card_id=target_id,
        from_card=current_id, type=mt, content=body, reply_to=reply_to,
    )
    session_ops.ops_log("card_msg_send", work_id, target_id,
                        {"msg_id": msg_id, "type": mt, "from": current_id})
    card_msg.TRIGGERS.setdefault((work_id, target_id), []).append(msg_id)
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
        await _mr.deliver_now(work_id, target_id)
    except Exception as exc:  # noqa: BLE001 投递失败不算发信失败（信已落库）
        logger.warning("[通信] 立即投递失败，交给收口兜底 target=%s: %s",
                       target_id, exc)
    # [消息底座能力对齐 · 件2] 事件流留痕照旧：消息仍记 msg_* 事件（账本不变），
    # 落点＝**收件方自己的键**（与改造前 _emit_block_event 同一落点、同一形状）。
    # 类型码本身已是 msg_*（注册表现读的事实形态），缺前缀时补——两种写法都归一。
    await session_facade.emit_message_event(
        work_id, target_id,
        mt if str(mt).startswith("msg_") else f"msg_{mt}",
        body,
    )
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

    [5713] 本薄壳的调用方全部是**系统/流程**路径（编程卡完工回执、blocked 通知、
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
                                      # 系统通道不查发起方开关：coder 卡刻意没有
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
    """读本卡收件箱（默认全部：待处理＋已处理；pending_only=true 只看待处理）。"""
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
    # [取消 inbox 状态机 2026-10-22 · D25] `pending_only` 参数已移除。
    # 语义变更：不再有「待处理状态位」；incoming 返回的就是**你没读过的消息**
    # （按读取水位算），已读的也在返回里但带上 read 标记，模型自己看得出来龙去脉。
    if "pending_only" in arguments:
        return err("validation",
                   "pending_only 参数已移除（收件箱状态机已取消）："
                   "incoming 直接返回未读消息，无需该参数")
    try:
        # 入向（缺省口径与改造前逐字一致）
        if direction in ("incoming", "both"):
            # [取消 inbox 状态机 2026-10-22 · D25] 始终返回该卡的完整往来记录；
            # 「哪些是新的」由读取水位算（list_pending），不再靠 pending_only 开关。
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
    sent_pending = [r for r in out if r.get("answered") is False and r.get("reply_expected")]
    return {"card_id": current_id, "count": len(out), "messages": out,
            "direction": direction,
            # 出向提醒：发了却没人回答的信（发问方据此做知情决策，别默默接受）
            "unanswered_sent": [r["msg_id"] for r in sent_pending]}


TOOL_REGISTRY["send_card_message"] = ToolDef(
    schema={
        "name": "send_card_message",
        "description": (
            "（自动模式专用）向另一张卡发消息——卡片间通信是你协作的主要手段。\n"
            "msg_ask＝你需要对方的信息或判断才能继续：对方会被系统自动调起，"
            "处理完后**自动把答案回发给你**（你会被再次唤醒）。\n"
            "msg_answer＝回应你收到的一条询问：必须带 reply_to（原询问的 msg_id）。\n"
            "msg_notify＝告知对方一件事，不需要对方回话。\n"
            "**前提：卡间通信走专门的通信线**——你和收件卡之间要有一条通信线，"
            "没有就先用 link_cards 建一条指向它的线（type 取 关联）再发；无线会被拒。"
            "线是双向的：你建的线，对方能沿同一条线回话。\n"
            "发完不必空等：你可以先做别的，答案回来时系统会唤醒你。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_card_id": {"type": "string", "description": "收件卡 id"},
                "msg_type": {
                    "type": "string",
                    "enum": ["msg_ask", "msg_answer", "msg_notify"],
                    "description": "询问／应答／通知",
                },
                "content": {"type": "string", "description": "消息正文（非空）"},
                "reply_to": {
                    "type": "string",
                    "description": "仅 msg_answer 必填＝要回应的那条询问的 msg_id",
                },
            },
            "required": ["target_card_id", "msg_type", "content"],
        },
    },
    handler=_h_send_card_message,
    readonly=False,
)

TOOL_REGISTRY["read_inbox"] = ToolDef(
    schema={
        "name": "read_inbox",
        "description": (
            "（自动模式专用）读你自己的消息账本：direction=incoming 你收到的（缺省）；"
            "direction=outgoing 你**发出**的——含 answered（对方是否已回答）与 "
            "unanswered_sent（你问了但没人答的清单）。"
            "发过 msg_ask 后用 outgoing 确认闭环：没人回答时不要默默接受，"
            "改换手段或 update_goal(pause) 说清在等什么。回应询问前用 incoming 查清 msg_id。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pending_only": {
                    "type": "boolean",
                    "description": "true＝只列待处理；缺省＝全部（含已处理）。仅对 incoming 生效",
                },
                "direction": {
                    "type": "string",
                    "enum": ["incoming", "outgoing", "both"],
                    "description": (
                        "incoming＝你收到的（缺省）；outgoing＝**你发出的**信及其状态——"
                        "含 answered（是否已有人回答）与 unanswered_sent（你问了但没人答的清单）。"
                        "发过 msg_ask 后用它确认是否闭环；未回答就别把未经核实的数当结论。"
                    ),
                },
            },
        },
    },
    handler=_h_read_inbox,
    readonly=True,
)

TOOL_SWITCH["send_card_message"] = policy.SWITCH_SEND_MESSAGE
TOOL_SWITCH["read_inbox"] = policy.SWITCH_READ


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
    用一份过期的 cards 快照调 record_verdict，本函数因拿不到 card_type 而判
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
    """
    fid = str(function_id or "")
    if not fid:
        return ""
    if card_store.card_exists(work_id, fid):
        return fid
    r = await card_store.create_card(
        work_id, function_name or fid, "系统派生",
        card_type="function", card_id=fid)
    if "error" in r:
        logger.warning("[function] 建职能卡失败 work=%s fn=%s: %s",
                       work_id, fid, r["error"])
        return ""
    return fid


async def _h_create_function(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """根卡组建**职能**：建一条 function_profiles 行，**不写 cards 表**。

    与旧 create_role 的结构差异只有一处、也是本步的全部要害：
    **职能不是卡**——它不占 card_NNN、不进画布/卡片清单/卡树，身份是独立号段
    ``fn_001``（function_store 内部发号，与建行同一写事务，不重号）。
    其余语义（jurisdiction/watch/signals/speak_contract/escalation 的默认值与
    落库形状）复用 role_room.make_profile——同一份默认值，不另写一套。
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


async def _h_record_verdict(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """职能记录本次巡检判断（发言或沉默）——沉默也必须落账。写 function_verdicts。"""
    actor = _function_actor(work_id, current_id, cards)
    if not actor:
        return err("denied", "只有职能（或角色卡）需要记录判词")
    decision = str(arguments.get("decision") or "").strip().lower()
    if decision not in ("speak", "silent"):
        return err("validation", f"decision 非法: {decision!r}（只允许 speak/silent）")
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return err("validation", "reason 不能为空（写清判断依据）")
    targets = arguments.get("targets") or []
    if isinstance(targets, str):
        targets = [targets]
    evidence = arguments.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if actor.startswith("fn_"):
        rec = await function_store.add_verdict(
            work_id, actor, decision, reason,
            [], [str(t) for t in targets], [str(e) for e in evidence])
        if arguments.get("supersedes"):
            await function_store.supersede_verdict(
                work_id, str(arguments["supersedes"]), rec["id"])
        return {"recorded": True, "id": rec["id"], "decision": decision,
                "function_id": actor}
    # 旧 role 卡路径：逐字不变
    rec = await role_room.record_verdict(
        work_id, actor, decision, reason,
        targets=[str(t) for t in targets],
        evidence=[str(e) for e in evidence],
        supersedes=str(arguments.get("supersedes") or ""))
    return {"recorded": True, "id": rec["id"], "decision": decision}


async def _h_raise_alarm(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """职能把「只有用户能决定的事」直通用户——写 function_alarms。"""
    actor = _function_actor(work_id, current_id, cards)
    if not actor:
        return err("denied", "只有职能（或角色卡）可以发警报")
    title = str(arguments.get("title") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    question = str(arguments.get("question") or "").strip()
    if not question:
        return err("validation",
                   "报警必须是决策请求：question 写清『用户要决定什么』；技术问题请下指令")
    default_action = str(arguments.get("default_action") or "").strip()
    if not default_action:
        return err("validation",
                   "报警必须带 default_action：用户不回复时你按什么默认动作执行")
    severity = str(arguments.get("severity") or "warn").strip().lower()
    if severity not in ("info", "warn", "critical"):
        severity = "warn"
    raw_opts = arguments.get("options") or []
    if isinstance(raw_opts, str):
        raw_opts = [raw_opts]
    supersedes = str(arguments.get("supersedes") or "")
    if actor.startswith("fn_"):
        rec = await function_store.add_alarm(
            work_id, actor, severity, title,
            str(arguments.get("detail") or "").strip(),
            supersedes=supersedes, question=question,
            options=[str(x) for x in raw_opts],
            recommendation=str(arguments.get("recommendation") or ""),
            default_action=default_action,
            deadline=str(arguments.get("deadline") or ""))
        return {"alarm_id": rec["id"], "severity": rec["severity"],
                "title": rec["title"], "supersedes": supersedes,
                "question": question, "default_action": default_action,
                "function_id": actor}
    rec = await role_room.raise_alarm(
        work_id, actor, severity, title, str(arguments.get("detail") or "").strip(),
        supersedes=supersedes, question=question,
        options=[str(x) for x in raw_opts],
        recommendation=str(arguments.get("recommendation") or ""),
        default_action=default_action,
        deadline=str(arguments.get("deadline") or ""))
    if rec.get("error"):
        return err("validation", str(rec["error"]))
    return {"alarm_id": rec["id"], "severity": rec["severity"],
            "title": rec["title"], "supersedes": supersedes,
            "question": question, "default_action": default_action}


async def _h_correct_alarm(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """职能更正自己的一条警报（置 corrected，不新发）。"""
    actor = _function_actor(work_id, current_id, cards)
    if not actor:
        return err("denied", "只有职能（或角色卡）可以更正警报")
    alarm_id = str(arguments.get("alarm_id") or "").strip()
    if not alarm_id:
        return err("validation", "alarm_id 不能为空")
    if actor.startswith("fn_"):
        own = [a for a in function_store.list_alarms(work_id, status="", function_id=actor)
               if str(a.get("id")) == alarm_id]
        if not own:
            return err("not_found", f"你的警报里没有 {alarm_id}")
        ok = await function_store.correct_alarm(work_id, alarm_id, actor)
    else:
        own = [a for a in role_room.list_alarms(work_id, status="", role_id=actor)
               if str(a.get("id")) == alarm_id]
        if not own:
            return err("not_found", f"你的警报里没有 {alarm_id}")
        ok = await role_room.correct_alarm(work_id, alarm_id, actor)
    if not ok:
        return err("invalid", f"警报 {alarm_id} 已不可更正（可能已更正/撤岗）")
    return {"ok": True, "alarm_id": alarm_id, "status": "corrected"}


_NO_FUNCTION_DIRECTIVE_LEDGER = (
    "职能的指令台账还没建（function_directives 表尚不存在，本步只做工具层与上下文层）。"
    "所以职能这一回合不能下指令——请改用 raise_alarm 把决策请求直通用户，"
    "或用 send_card_message 给相关的工作卡发建议（普通建议不受影响）。")


async def _h_issue_directive(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """下指令：旧 role 卡路径逐字不变；职能回合显式拒（台账未建，见上方说明）。"""
    actor = _function_actor(work_id, current_id, cards)
    if not actor:
        return err("denied", "只有职能（或角色卡）可以下指令")
    if actor.startswith("fn_"):
        return err("not_implemented", _NO_FUNCTION_DIRECTIVE_LEDGER)
    return await _h_issue_directive_legacy(work_id, current_id, arguments, cards)


async def _h_verify_directive(work_id: str, current_id: str,
                              arguments: dict[str, Any],
                              cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """验收指令：旧 role 卡路径逐字不变；职能回合显式拒（台账未建）。"""
    actor = _function_actor(work_id, current_id, cards)
    if not actor:
        return err("denied", "只有职能（或角色卡）可以验收指令")
    if actor.startswith("fn_"):
        return err("not_implemented", _NO_FUNCTION_DIRECTIVE_LEDGER)
    return await _h_verify_directive_legacy(work_id, current_id, arguments, cards)



# ============================================================
# [角色机制 5710] 组阁 / 关注管理 / 判词 / 警报
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


async def _h_record_verdict_legacy(work_id: str, current_id: str,
                            arguments: dict[str, Any],
                            cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """角色卡记录本次判断（发言/沉默）。这是「沉默落账」的执行体。"""
    if not role_room.is_role(work_id, current_id):
        return err("denied", "只有角色卡需要记录判词")
    decision = str(arguments.get("decision") or "").strip().lower()
    if decision not in ("speak", "silent"):
        return err("validation", f"decision 非法: {decision!r}（只允许 speak/silent）")
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return err("validation", "reason 不能为空（写清判断依据）")
    targets = arguments.get("targets") or []
    if isinstance(targets, str):
        targets = [targets]
    evidence = arguments.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    rec = await role_room.record_verdict(
        work_id, current_id, decision, reason,
        targets=[str(t) for t in targets],
        evidence=[str(e) for e in evidence],
        supersedes=str(arguments.get("supersedes") or ""),
    )
    return {"recorded": True, "id": rec["id"], "decision": decision}


async def _h_raise_alarm_legacy(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """角色卡把严重风险直通用户；可传 supersedes 更正自己此前的警报。"""
    if not role_room.is_role(work_id, current_id):
        return err("denied", "只有角色卡可以发警报")
    title = str(arguments.get("title") or "").strip()
    detail = str(arguments.get("detail") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    severity = str(arguments.get("severity") or "warn").strip().lower()
    raw_opts = arguments.get("options") or []
    if isinstance(raw_opts, str):
        raw_opts = [raw_opts]
    rec = await role_room.raise_alarm(
        work_id, current_id, severity, title, detail,
        supersedes=str(arguments.get("supersedes") or ""),
        question=str(arguments.get("question") or ""),
        options=[str(x) for x in raw_opts],
        recommendation=str(arguments.get("recommendation") or ""),
        default_action=str(arguments.get("default_action") or ""),
        deadline=str(arguments.get("deadline") or ""))
    if rec.get("error"):
        return err("validation", str(rec["error"]))
    return {"alarm_id": rec["id"], "severity": rec["severity"],
            "title": rec["title"], "supersedes": rec.get("supersedes", ""),
            "question": rec.get("question", ""),
            "default_action": rec.get("default_action", "")}


async def _h_correct_alarm_legacy(work_id: str, current_id: str,
                           arguments: dict[str, Any],
                           cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """角色卡更正自己的一条警报（不新发警报，只把它置为 corrected）。"""
    if not role_room.is_role(work_id, current_id):
        return err("denied", "只有角色卡可以更正警报")
    alarm_id = str(arguments.get("alarm_id") or "").strip()
    if not alarm_id:
        return err("validation", "alarm_id 不能为空")
    own = [a for a in role_room.list_alarms(work_id, status="",
                                            role_id=current_id)
           if str(a.get("id")) == alarm_id]
    if not own:
        return err("not_found", f"你的警报里没有 {alarm_id}")
    ok = await role_room.correct_alarm(work_id, alarm_id, current_id)
    if not ok:
        return err("invalid", f"警报 {alarm_id} 已不可更正（可能已更正/撤岗）")
    return {"ok": True, "alarm_id": alarm_id, "status": "corrected"}


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


TOOL_REGISTRY["issue_directive"] = ToolDef(
    schema={
        "name": "issue_directive",
        "description": (
            "（角色卡专用）在管辖域内给已关注的工作卡下指令。不是建议："
            "卡必须执行，或说明不能执行的理由/需要什么支持/何时完成。"
            "必须同时给验收判据；下一轮用 verify_directive 验收。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_card_id": {"type": "string"},
                "instruction": {"type": "string", "description": "要它做什么（明确、可执行）"},
                "acceptance": {"type": "string", "description": "验收判据（怎么算做到）"},
                "due_at": {"type": "string", "description": "期望完成时间（可选）"},
            },
            "required": ["target_card_id", "instruction", "acceptance"],
        },
    },
    handler=_h_issue_directive,
    readonly=False,
)

TOOL_REGISTRY["verify_directive"] = ToolDef(
    schema={
        "name": "verify_directive",
        "description": (
            "（角色卡专用）按实际结果验收一条指令：complied=达标，rejected=未达标"
            "（必须写 evidence 差在哪），cancelled=撤销。不要凭对方回话说“已改”就判达标，"
            "先读实际现状/证据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directive_id": {"type": "string"},
                "status": {"type": "string", "enum": ["complied", "rejected", "cancelled"]},
                "evidence": {"type": "string"},
            },
            "required": ["directive_id", "status"],
        },
    },
    handler=_h_verify_directive,
    readonly=False,
)


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
            "（根卡专用）组建一个**职能**（如「质量管理」），写结构化档案与职责书。"
            "职能**不是卡**：它不占卡片号、不进卡片清单与任务树、不干活；"
            "它盯着你指定的工作卡，命中关注信号时被唤醒，"
            "能记录判断、发建议、发决策请求。职能名由你按事情本身判断，"
            "系统不做白名单校验——人类团队里找不到这个岗位就别建。"
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
                "signals": {
                    "type": "array", "items": {"type": "string",
                                               "enum": ["turn_completed", "status_changed",
                                                        "decision_appended", "turn_error"]},
                    "description": "关注信号；命中才同步/唤醒。缺省 turn_completed",
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

TOOL_REGISTRY["record_verdict"] = ToolDef(
    schema={
        "name": "record_verdict",
        "description": (
            "（角色卡专用）记录本次巡检判断。每次被调起都必须调用："
            "decision=speak/silent；reason 一句话依据；evidence 填实际看过的"
            "文件/行/卡（可空）。沉默不落账＝没有判断。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["speak", "silent"]},
                "reason": {"type": "string"},
                "targets": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "supersedes": {"type": "string", "description": "被本判词取代的旧判词 id（可空）"},
            },
            "required": ["decision", "reason"],
        },
    },
    handler=_h_record_verdict,
    readonly=False,
)

TOOL_REGISTRY["raise_alarm"] = ToolDef(
    schema={
        "name": "raise_alarm",
        "description": (
            "（角色卡专用）把严重风险直通用户。普通建议用 send_card_message；"
            "只有用户需要立刻知道的风险才用警报。severity：info/warn/critical。"
            "更正自己此前的警报时传 supersedes=<旧 alarm_id>。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["info", "warn", "critical"]},
                "title": {"type": "string"},
                "detail": {"type": "string"},
                "question": {"type": "string", "description": "要用户决定什么（不是技术问题复述）"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "可选项（用户能理解的取舍）"},
                "recommendation": {"type": "string", "description": "角色推荐哪个选项及理由"},
                "default_action": {"type": "string", "description": "用户不回复时角色按什么默认动作执行"},
                "deadline": {"type": "string", "description": "默认动作生效时间（可选）"},
                "supersedes": {"type": "string"},
            },
            "required": ["severity", "title", "question", "default_action"],
        },
    },
    handler=_h_raise_alarm,
    readonly=False,
)

TOOL_REGISTRY["correct_alarm"] = ToolDef(
    schema={
        "name": "correct_alarm",
        "description": "（角色卡专用）更正自己的一条警报（置为 corrected，不新发警报）。",
        "parameters": {
            "type": "object",
            "properties": {"alarm_id": {"type": "string"}},
            "required": ["alarm_id"],
        },
    },
    handler=_h_correct_alarm,
    readonly=False,
)


# [T11-T10] 只读工具白名单（折叠【依据】「可按原参数重取」语义的依据源）。
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
# [编程卡 2026-10-15 · C3] 第三段过滤 · card_type 闸
#
# coder 专属工具只对 card_type=="coder" 的当前卡下发，其余类型一律不出现。
# 与现两段（_filter_by_role 恒等 / _filter_by_category 按卡上 perms）互不触碰
# ——三段各管一线、只查表不判内容（同两线 R-D 纪律）：前两段按「卡有什么权限」
# 下发，本段按「卡是什么类型」收敛。
# ============================================================
CODER_ONLY_TOOLS: frozenset[str] = frozenset({"report_done"})


def _filter_by_card_type(
    tools: list[dict[str, Any]], card_id: str, work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """**第三段过滤 · 类型闸**（编程卡 2026-10-15 · C3）。

    ``CODER_ONLY_TOOLS`` 内的工具只对 ``card_type=="coder"`` 的当前卡下发；
    其余工具原样穿过。认不出卡（card_id 空 / cards 缺省 / 卡不在表里）⇒ 原样
    返回——第二段已默认关门返回空列表，此处不重复拦（也绝不放宽）。
    """
    card = (cards or {}).get(card_id)
    if not card_id or cards is None or card is None:
        return tools
    is_coder = str(card.get("card_type", "") or "") == "coder"
    if is_coder:
        return tools
    keep: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    for t in tools:
        name = str(t.get("name", ""))
        if name in CODER_ONLY_TOOLS:
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
            "reason": "coder_only_card_type_gate",
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
     "update_function_profile", "set_function_duty", "list_functions"})


def _filter_root_only(
    tools: list[dict[str, Any]], card_id: str, work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """**第四段过滤 · 根卡闸**（规则卡 2026-10-15 · 块3）。

    ``ROOT_ONLY_TOOLS`` 内的工具只对剧本根卡下发；其余原样穿过。认不出
    work（work_id 空／查不到根卡）⇒ root-only 工具全部收走（fail-closed）。
    执行层 handler 另有 current_id 校验（双保险，同 report_done 手法）。
    """
    if not any(str(t.get("name", "")) in ROOT_ONLY_TOOLS for t in tools):
        return tools
    root_id = work_store.work_card_id_of(work_id) if work_id else ""
    card = (cards or {}).get(card_id) or {}
    if root_id and card_id == root_id:
        return tools
    keep: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    for t in tools:
        name = str(t.get("name", ""))
        if name in ROOT_ONLY_TOOLS:
            dropped[name] = "root_only" if root_id else "no_work_context"
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
# 只留读工具 + append_ruling（写需求文档）。
# ============================================================

#: 契约轮里**必须摘掉**的工具——一切"动手"的东西。只留读 + 写需求（append_ruling）。
#  [使用律法 2026-10-22 · 用户令「自动模式下卡片的第一轮的工作，就是固定的写需求文档」]
#  原来只摘 create_card / create_coder_card（"先建职能"），实际制造了死锁：
#  建职能要 watch 的卡还不存在 ⇒ 建不出来 ⇒ 闸永远命中 ⇒ 卡永远出不去
#  （work_024 实测 blocked）。现在改成"第一轮写需求文档"：需求是唯一事实源，
#  没有它，后面派活/执行/验收全都没有判据。
_MUST_ROUND_DROP: frozenset[str] = frozenset({
    "create_card", "create_coder_card", "delete_card",
    "write_file", "edit_file", "move_file", "rename_file", "delete_file",
    "bash", "python", "run_tool_card", "report_done",
    "link_cards", "link_rel",
})


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
    # ① 自动模式
    if not bool(card.get("auto_mode")):
        return False
    # ② 第一轮（没跑过任何一轮）
    if int(card.get("goal_rounds") or 0) != 0:
        return False
    # ③ 只对**管理卡**（要组织、要派活的那些）。coder 的任务本来就写在使命块里，
    #    给它加契约轮只会拖慢执行——它不需要再"写一遍需求"。
    if str(card.get("card_type") or "") not in ("project", "sub_project", "topic"):
        return False
    return True


def _apply_must_round_face(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """必须轮工具面：摘掉搭交付树的 create_card / create_coder_card，其余原样。"""
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
        三级 ``_filter_by_card_type``← 类型闸（编程卡 2026-10-15 · C3：
                                        CODER_ONLY_TOOLS 只对 coder 卡下发）
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
    # ---- 三级：card_type 闸（编程卡 2026-10-15 · C3）----
    tools = _filter_by_card_type(tools, card_id, work_id, cards)
    # ---- 四级：根卡闸（规则卡 2026-10-15 · 块3）----
    tools = _filter_root_only(tools, card_id, work_id, cards)
    # ---- 五级：[换父 2026-10-20] 不能有子卡的卡不下发换父工具 ----
    # 工具卡/编程卡/规则卡自己没有子树可调（角色卡的整个工具面在末段③被替换，
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
        # [card_040 · 2026-10-21] 自动模式启动「必须轮」：根卡 auto 首轮且本 work
        # 一个职能都没有 ⇒ 只许组建职能、不许搭交付树（摘 create_card/create_coder_
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
    # ---- 末段③：[card_031] 职能回合工具面 ----
    # 与 auto 面互斥（职能回合不是 auto 卡）。判定看「是不是职能回合」
    # （_is_function_turn：fn_NNN 真在职能表里，或旧 role 卡回合），
    # 不再单看 card_type=="role"——职能没有 cards 行，按卡型判不出来。
    if card_id and _is_function_turn(work_id, card_id, cards):
        tools = _apply_function_toolface(tools)
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
#: 建结构（create_card / create_coder_card / delete_card / 连线）与写产物
#: （write_file / edit_file / move_file / rename_file / delete_file）。
#: 保留：只读类 ＋ bash/python/glob_files/list_files/search_files（允许"看一眼再规划"）
#:      ＋ create_plan（唯一出口）＋ update_status/append_ruling/update_shadow（记录）。
#: ★ 仅实验期使用；落地形态见「第二轮起要求有板」。
_PLAN_GATE_BLOCKED: frozenset[str] = frozenset({
    "create_card", "create_coder_card", "delete_card",
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
        # [分块写入 2026-10-19 · permexp] auto 卡摘掉 edit_card：改用
        # update_status / append_ruling（一个接口一种语义，见其上方长注释）。
        # 普通卡不受影响（本函数只对 auto_mode=1 调用）。
        if name == "edit_card":
            continue
        if name == "create_card":
            out.append(_create_card_schema_with_goal(t))
        else:
            out.append(t)
    # 追加序固定（块0.4 起 update_goal 首位；G2 起两件通信随后）——集合成员
    # 增长不改首位语义，测试与审计都认这个序。
    # [goal 域 2026-10-20 · 修漏] 这份清单是**硬编码**的：只在 TOOL_REGISTRY /
    # AUTO_ONLY_TOOLS 里登记、没加到这里 ⇒ 工具注册了但**永远不下发**（实测
    # get_goal/create_goal 就漏了这一层，resume_child 同样）。
    for auto_name in ("update_goal", "get_goal", "create_goal", "resume_child",
                      "send_card_message", "read_inbox",
                      "update_status", "append_ruling"):
        if auto_name in AUTO_ONLY_TOOLS:
            out.append(_schema_of(TOOL_REGISTRY[auto_name]))
    return out


# ============================================================
# [角色机制 5710] 角色卡工具面（非卡片内挂）
#
# 角色卡不是作业卡：摘掉 edit_card（不许改别人/整篇正文），只留
# 读、沿连线读、影子、发建议、读收件箱、记录判词、发警报、写自己的
# 现状/需求文档。全部在四段过滤之后追加，常规卡工具面逐字不变。
# ============================================================
# [职能实体化 A 案 2026-10-22 · 用户裁定] 职能是**管理者**（人类团队里的岗位），
# 要能派活（create_card / create_coder_card）和建立通信（link/unlink）。
# 只留 delete_card 在摘除集里——职能不删别人的卡。
# 原集合把 create_card/create_coder_card/link_* 全摘掉，等于"职能不能派活、
# 不能连线"，与"职能盯卡、必要时要求返工"的职责直接矛盾。
_ROLE_FACE_DROP = frozenset({"edit_card", "delete_card"})


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


def _apply_function_toolface(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [t for t in tools if str(t.get("name", "")) not in _ROLE_FACE_DROP]
    for name in ("send_card_message", "read_inbox", "record_verdict",
                 "raise_alarm", "correct_alarm", "issue_directive",
                 "verify_directive", "update_status", "append_ruling"):
        if name in TOOL_REGISTRY:
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
#     （见 _rulings_superset 的一对一配对逻辑）。
#   「D08 已失效」/「D08 被取代」形态：编号直陈，一眼可判。
# 两种形态都放行（规则③：行首 → 放行），判据只负责「不把它们当偷偷删除」。
_SUPERSEDED_SELF_RE = re.compile(r"^→\s*(?:被D\d{2}取代|D\d{2}\s*(?:已失效|被取代))")
_SUPERSEDED_TARGET_RE = re.compile(r"被D(\d{2})取代|D(\d{2})\s*已失效")


def _rulings_entries(text: str) -> dict[str, str]:
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
    规则②判「旧条目是否被删改」时，用它做一对一抵扣（见 _rulings_superset）。
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


async def execute_tool(
    work_id: str,
    current_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    cards: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """执行单个工具调用，返回**信封**（成功 {"ok":True,...,"data":{...}}，失败
    {"ok":False,...,"error":{"code","message"}}）。

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
        # [工具卡底座 T9 · 执行层角色拦 —— **已回滚，接替未启用**]
        # [用户 2026-10-09 17:38 逐字]「现在先不要把写工具给干掉啊，
        #   等工具卡完善后，再接替啊」
        # 此处原按同一份 WRITE_TOOLS_REVOKED 拦主卡的写工具调用。同
        # `_filter_by_role` 一并回滚——**接替的时机未到**，此刻主卡工具面
        # 为全量。接替启用时，工具面清单（第一道）与本处执行层拦（第二道）
        # 必须同批启用，不得只启一道（否则要么看得见调不动、要么绕过清单硬发）。
        # 权限表查表、非内容判定（R-A）这一性质不变，留待接替时复用。
        result = await def_.handler(work_id, current_id, arguments, cards)
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
