# -*- coding: utf-8 -*-
"""卡片工具与权限（白板版 2026-10-02）。

工具接口只含机械事实：
- read_card：返回标题、父卡、连线、正文——不暴露内部四区语义
- edit_card(card_id, content)：覆盖写指定卡正文（内部落存储层正文区）
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
from ..storage import card_db, card_store, manuscript_store, memory_store, shadow_store, work_store
from ..storage import event_log, session_facade, session_ops
from ..storage.hash_utils import parse_front_matter, split_body_sections

from . import card_msg, context_regions, event_stream
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
    {"update_goal", "send_card_message", "read_inbox"})

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
    # [编程卡 2026-10-15 · §六块2 · **v1.1 修订**] report_done 挂 tools_file——
    # 原挂 SWITCH_EDIT 系派发方笔误：tool 卡有 edit 无 tools_file，挂 edit 会让
    # readopen:287 与 :252 在「零翻新」下数学上不可能同时成立。挂 tools_file 后：
    # tool 卡第二段即滤（:287 绿）、sub_project 绕第三段闸可见恰为全量（:252 绿）、
    # coder 有 tools_file 正常可见（判据1 绿）。判定权在第三段闸＋执行层
    # card_type 校验双保险，开关只决定「有没有资格进闸」。
    "report_done": policy.SWITCH_TOOLS_FILE,
    "delete_card": policy.SWITCH_DELETE,
    "create_card": policy.SWITCH_CREATE_CHILD,
    # [自动模式 2026-10-17 · 回灌 5700 块0.4] update_goal 挂 plan（「建计划/改计划/
    # 推进步骤」语义最近——【起草】可推翻，Open Item TC-6.4）。★ 它**不进常规
    # 工具面**：被 AUTO_ONLY_TOOLS 从工具池里排除，只在 auto_mode=1 的卡上于
    # 四段过滤之后追加（同「非卡片内挂」手法）。
    "update_goal": policy.SWITCH_PLAN,
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
    "update_shadow": policy.SWITCH_SHADOW_WRITE,
    "list_manuscript": policy.SWITCH_MANUSCRIPT,
    "read_manuscript": policy.SWITCH_MANUSCRIPT,
    "search_manuscript": policy.SWITCH_MANUSCRIPT,
    "run_tool_card": policy.SWITCH_INVOKE_TOOL,
    "web_search": policy.SWITCH_TOOLS_WEB,
    "fetch_web": policy.SWITCH_TOOLS_WEB,
    # [基础工具找回 2026-10-18 · Cardlo 搬运] 七件全挂 tools_file
    "bash": policy.SWITCH_TOOLS_FILE,
    "python": policy.SWITCH_TOOLS_FILE,
    "glob_files": policy.SWITCH_TOOLS_FILE,
    "delete_file": policy.SWITCH_TOOLS_FILE,
    "move_file": policy.SWITCH_TOOLS_FILE,
    "rename_file": policy.SWITCH_TOOLS_FILE,
    "edit_file": policy.SWITCH_TOOLS_FILE,
    "list_files": policy.SWITCH_TOOLS_FILE,
    "read_file": policy.SWITCH_TOOLS_FILE,
    "search_files": policy.SWITCH_TOOLS_FILE,
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
TOOL_TABLE_VERSION = "tools-tc-12"


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
    # [建卡工具化 2026-10-08][权限底座重做 2026-10-12] 权限＝关口 CREATE_CHILD
    # 判定（代码现状口径：只能建自己的子卡片）。parent 缺省＝当前卡自身（建自己
    # 下一层）；给了 parent 则经 policy.check 判定（parent 卡须授 CREATE_CHILD
    # ＋ actor 在 target 子树上游），与 edit_card/delete_card 同一关口，
    # 无第二套规则。结构性护栏（剧本根卡/带子卡者不可删）不在此——建卡不涉删。
    title = str(arguments.get("title", "") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    # parent 缺省＝当前卡自身；显式给空串也按缺省处理（模型常传空串）
    target_parent = str(arguments.get("parent", "") or "").strip() or current_id
    # 存在性先判：已删/不存在的 parent 不误报越权（与 read_card 同序）
    if target_parent not in cards:
        return err("not_found", f"父卡不存在: {target_parent}")
    # [权限底座重做 2026-10-12] 关口 CREATE_CHILD：parent 卡上的固化 perms 须授
    # create_child；树类开关再收紧为「只能建自己子树内的卡」
    try:
        policy.check(current_id, target_parent, policy.SWITCH_CREATE_CHILD, cards)
    except PermissionError:
        return err(
            "denied",
            (
                f"越权：只能建自己的子卡片，{target_parent} 不在你的子树内"
                f"（当前卡 {current_id}）"
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
        work_id, current_id, {"state": "completed"},
        f"编程卡完工收口（report_done）：{summary[:80]}",
    )
    if isinstance(upd, dict) and upd.get("error"):
        return err("internal", str(upd["error"]))
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
    try:
        policy.check(current_id, current_id, policy.SWITCH_CREATE_CHILD, cards)
    except PermissionError:
        return err(
            "denied",
            f"越权：只能建自己的子卡片，{current_id} 不在你的子树内"
            f"（当前卡 {current_id}）",
        )
    # ② 建 coder 卡：charter 带 ## 使命 区头 ⇒ 区头检测后原样入正文，任务落使命块
    result = await card_store.create_card(
        work_id, title, "模型生成",
        card_type="coder", parent=current_id,
        charter=f"## 使命\n{task}",
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
        return err("validation", "content 不能为空：请把「新现状」正文交给本工具")
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
        f"## 定论\n{b.get('rulings', '')}\n"
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
    history_note = f"由「{cur_title}」对话编辑正文"
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
    # 若三块全写会把使命与定论一起清空 → 必然触使命闸（V2），
    # 等于「模型永远无法 edit」。故：无区头正文按「这是现状正文」处理，
    # 只写 status_block，使命与定论沿用卡内现状。
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
       - ``正文`` → ``card_store.read_card``（三块：使命／现状／定论）
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

    权限＝关口 ``policy.SWITCH_READ_LINKED``（与 read 分开授权——沿连线读比
    直接读更敏感）。源＝当前卡（工具签名里没有 source，写不出别的源）。
    越权回既有 denied 信封；无连线／目标不存在回 not_found（文案含「无通信线」）。
    """
    target_id = str(arguments.get("target_id", "") or "")
    part = str(arguments.get("part", "") or "")
    if not target_id:
        return err("validation", "target_id 不能为空")
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    try:
        policy.check(current_id, target_id, policy.SWITCH_READ_LINKED, cards)
    except PermissionError:
        return err("denied", f"越权：无权沿通信线读取 {target_id}")
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
    # [权限底座重做 2026-10-12] 关口 LINK：目标卡 perms 须授 link；树类开关再
    # 收紧为「只能连自己子树内的卡」（交办单「待用户知悉」明示的收紧，可推翻）
    try:
        policy.check(current_id, target_id, policy.SWITCH_LINK, cards)
    except PermissionError:
        return err("denied", f"越权：无权与 {target_id} 建立关系")
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
    # [T5 补漏检口 2026-10-09][权限底座重做 2026-10-12] 关口 UNLINK：目标卡
    # perms 须授 unlink；树类开关再收紧为「只能解自己子树内的线」
    # （交办单「待用户知悉」，可推翻）。
    # 悬空线（目标卡已删，本 handler 既有语义＝清理用途）不在关口判定面内：
    # 目标不存在则无卡可查，保留直接可解的现状，不误伤清理主用例。
    if target_id in cards:
        try:
            policy.check(current_id, target_id, policy.SWITCH_UNLINK, cards)
        except PermissionError:
            return err("denied", f"越权：无权解除与 {target_id} 的连线")
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
    return {"ok": True, "source": current_id, "target": target_id, "removed": removed}


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
    权限：关口 ``SWITCH_LINK``（树类收紧：只能连自己子树内的卡）。
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
    # 权限复用 LINK 关口（不新开）：目标卡 perms 须授 link；树类收紧同款
    try:
        policy.check(current_id, target_id, policy.SWITCH_LINK, cards)
    except PermissionError:
        return err("denied", f"越权：无权与 {target_id} 建立依赖关联")
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
    return {"ok": True, "source": current_id, "target": target_id, "kind": kind}


async def _h_unlink_rel(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """解除依赖关联线：从当前卡 ``relations`` 移除指向 target 的条目。

    ``kind`` 缺省＝移除**全部**指向该 target 的条目；给了 kind 只移除对应条目。
    权限：关口 ``SWITCH_UNLINK``（与 unlink_cards 同款：目标卡已删的悬空条目
    仍可清理——目标不存在则无卡可查，直接放行）。
    """
    target_id = str(arguments.get("target_id", "") or "")
    kind = str(arguments.get("kind", "") or "").strip()
    if not target_id:
        return err("validation", "target_id 不能为空")
    if kind and kind not in MODEL_DEP_KINDS:
        return err("validation",
                   f"kind 非法: {kind}，只允许 {'/'.join(MODEL_DEP_KINDS)}")
    if target_id in cards:
        try:
            policy.check(current_id, target_id, policy.SWITCH_UNLINK, cards)
        except PermissionError:
            return err("denied", f"越权：无权解除与 {target_id} 的依赖关联")
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
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id),
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
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id),
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
    return await _run_subprocess(shell_cmd=command)


async def _h_python(work_id: str, current_id: str,
                    arguments: dict[str, Any],
                    cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """代码执行：写系统临时 .py 再跑（不用 ``python -c``，规避 Windows 命令行
    多行字符串的引号/换行转义坑），finally 必删临时文件。"""
    code = str(arguments.get("code") or "")
    if not code.strip():
        return err("validation", "code 不能为空")
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

TOOL_REGISTRY: dict[str, ToolDef] = {
    "read_card": ToolDef(
        schema={
            "name": "read_card",
            "description": (
                "读取指定卡（标题、父卡、连线、正文各块）。返回含使命／现状／"
                "第三块（第三块名按本树类型为 requirements 或 rulings），"
                "以及正文（=现状）。只能读取你有权访问的卡。"
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
            "description": "下探指定卡的凝练记忆（condense_fold：已凝练历史经归拢的条目，含【结论】/【依据】）。可用块序号区间按锚点取回。记忆条目不是卡片正文——正文请用 read_card。",
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
                        "description": "卡的正文（结论正文，缺省空）。",
                    },
                },
                "required": ["title"],
            },
        },
        handler=_h_create_card,
        readonly=False,
    ),
    "edit_card": ToolDef(
        schema={
            "name": "edit_card",
            "description": (
                "改写你管辖范围内指定卡的正文（覆盖写，content 是整篇正文）。"
                "正文分三块：## 使命（本卡作用/目标、授权来源；立项定稿，你改它会被拒）、"
                "## 现状（当前状况快照，覆盖写·单一最新，只写已核实）、"
                "## 定论（对本卡事项的决定，只追加）。"
                "定论条目语法 D{nn}【来源】内容（日期·依据），来源三类【裁定】/【决定】/【收敛】，"
                "序号终身不改；条目失效就地标记「→ 被D05取代（因……）」，不删原文。"
                "决定不要写进现状块（会被拒）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "目标卡 id"},
                    "content": {
                        "type": "string",
                        "description": "新的正文全文（覆盖写，含 ## 使命/## 现状/## 定论 三块区头）",
                    },
                },
                "required": ["card_id", "content"],
            },
        },
        handler=_h_edit_card,
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
                "自己的影子。影子卡正文有且只有四节，每节内容边界（不得越界、不得改节名、"
                "不得加节）：【进度】只放本卡推进的时间线记录（什么时间、做到哪一步、当前"
                "什么状态），不写结论/分析/无关杂事；【待协作事项】只放需要其他卡/模块提供的"
                "输入、待办、阻塞、协作关系，不放产物位置（那是产物清单）、不放改动记录"
                "（那是迭代历史）；【产物清单】只放本卡推进中产生的文件/代码/文档的路径与用途"
                "指针（一行一个：路径＋一句话说明它是什么），只写指针不搬运内容正文；"
                "【迭代历史】只放改动指针（改了什么、为什么改），细节回既有通道查、不在此"
                "复述。三条禁止：①不许把正卡的结论正文写进影子卡（正卡写结论、影子卡写过程，"
                "不得混）；②不许写与本卡推进无关的内容；③不许把影子卡当备忘录堆杂事。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "影子正文全文（覆盖写）。必须含且仅含四节，节名固定：## 进度 / ## 待协作事项 / ## 产物清单 / ## 迭代历史。每节只放该节边界内的内容（进度＝时间线；待协作事项＝需他卡/模块提供的输入、待办、阻塞、协作关系；产物清单＝路径与用途指针；迭代历史＝改动指针），不得写入正卡结论正文、不得写无关内容、不得堆杂事。"},
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
    "run_tool_card": ToolDef(
        schema={
            "name": "run_tool_card",
            "description": (
                "调用一张工具卡：按剧本建一张新工具卡并取回第一步的步骤指令。"
                "每次调用都新建一张卡，不覆盖已有卡。"
                "参数：script_name＝剧本名（用错名字会直接报错）；"
                "instruction 可选，写你这本次想让它做的事（会记进新卡的现状块）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script_name": {
                        "type": "string",
                        "description": "剧本名，如「方案链」。找不到会报错。",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "可选：本次调用意图，原文记入新卡现状块。",
                    },
                },
                "required": ["script_name"],
            },
        },
        handler=_h_run_tool_card,
        readonly=False,
    ),
    # [卡片底座第一步 2026-10-14] 第 20 件工具：沿通信线读对方卡。
    # 追加在既有 19 件之后 ⇒ 前 19 件的工具面顺序逐字不变。
    # readonly=True（纯读）；开关 read_linked（独立于 read）。
    "read_linked": ToolDef(
        schema={
            "name": "read_linked",
            "description": (
                "沿通信线读取与你**已有连线关系**的卡（正文／影子／对话）。"
                "只能读你与它之间已建立连线的卡；没有连线则报「无通信线」。"
                "part 取：正文（使命／现状／定论三块）／影子（过程记录）／"
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
                    "task": {"type": "string", "description": "开发任务正文（原样落进新卡的使命块）"},
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
                "提议本卡「现状」的新正文（只提议、不落盘）。返回提议内容与盘上现文、"
                "指纹——你不能直接改现状，本工具只出草案；调用后系统会弹对比窗交用户"
                "确认，用户点「同意」才写入本卡现状生效。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "新的现状正文（整块替换草案；只写已确定的结论）",
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
    "update_goal": ToolDef(
        schema={
            "name": "update_goal",
            "description": (
                "（自动模式专用）声明你的目标推进状态——这是「停」的唯一权威："
                "不声明，系统会自动再起一轮。"
                "action：complete＝目标已完成；pause＝暂停（等外部条件）；"
                "blocked＝卡住做不下去（必须给 reason）。"
                "await_children：只在 pause 时有意义——传 true 表示「我这次 pause 是在等子卡跑完」，"
                "系统据此在子卡全部完工时自动叫醒你；不传则按「单纯暂停」处理，不会被叫醒。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["complete", "pause", "blocked"],
                        "description": "complete＝完成；pause＝暂停；blocked＝卡住",
                    },
                    "reason": {
                        "type": "string",
                        "description": "原因（blocked 必填：说清卡在哪）",
                    },
                    "await_children": {
                        "type": "boolean",
                        "description": (
                            "仅 action=pause 时有意义：true＝这次暂停是在等子卡跑完，"
                            "子卡全部完工时叫醒我。只在「当下确实还有未完工的子卡」时才传 true。"
                        ),
                    },
                },
                "required": ["action"],
            },
        },
        handler=_h_update_goal,
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
                               cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
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
    # 权限＝分类 READ 关口 ＋ 显式子树判定（政策收口前的卡间册 §4.3 字面口径；
    # 详见下方修正说明的沿革）。
    # [锚点实测修正——卡间册 §4.3 字面与 policy 现状有一处偏差，见回报卡点①]
    # policy.check 的树作用域只对 TREE_SWITCHES（edit/delete/link/unlink/
    # create_child）生效，read 不在其中——照册字面只传 read 会退化为纯类型
    # 判权、子树限定失效（兄弟卡互发不被挡）。故类型关口照走 read，
    # 「收件方必须落在发件方子树内」显式落 _is_in_subtree（向上/跨树发信挡死）。
    try:
        policy.check(current_id, target_id, policy.SWITCH_READ, cards)
    except PermissionError:
        return err("denied", f"越权：无权向 {target_id} 发送消息")
    # [用户 10-09 20:24 要点]「方案书卡有不确定的信息，直接发回主卡」——
    # 子卡须能发回父卡/祖先链。判定由「收件卡必须在发件卡子树内」放宽为
    # 「两卡同一棵任务树内」（双向各查一次）：父子/祖先链可互发；兄弟、
    # 跨树仍挡死（双向均不命中）。发信不改树结构，不引入环。
    if not (policy._is_in_subtree(current_id, target_id, cards)
            or policy._is_in_subtree(target_id, current_id, cards)):
        return err("denied", f"越权：{target_id} 与你不在一棵任务树内")
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
    """
    cards = card_store.iter_card_metas(work_id)
    arguments = {
        "target_card_id": target_id,
        "msg_type": msg_type,
        "content": content,
        "reply_to": reply_to,
    }
    return await _h_send_card_message(work_id, from_id, arguments, cards)


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
    pending_only = bool(arguments.get("pending_only"))
    try:
        # 入向（缺省口径与改造前逐字一致）
        if direction in ("incoming", "both"):
            items = (await card_db.list_pending(work_id, current_id) if pending_only
                     else await card_db.list_inbox(work_id, current_id))
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
            "只能发给同一棵任务树内的卡（父卡／子卡／祖先）；跨树会被拒。"
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
ROOT_ONLY_TOOLS: frozenset[str] = frozenset({"draft_rule_edit"})


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
             if name not in OP_ONLY_TOOLS and name not in AUTO_ONLY_TOOLS]
    # ---- 一级：角色过滤（工具卡底座 T9）----
    tools = _filter_by_role(tools, card_id, work_id, cards)
    # ---- 二级：分类过滤（权限线 T4）----
    tools = _filter_by_category(tools, card_id, work_id, cards)
    # ---- 三级：card_type 闸（编程卡 2026-10-15 · C3）----
    tools = _filter_by_card_type(tools, card_id, work_id, cards)
    # ---- 四级：根卡闸（规则卡 2026-10-15 · 块3）----
    tools = _filter_root_only(tools, card_id, work_id, cards)
    # ---- 末段①：自动模式专用工具面（非卡片内挂；只对 auto_mode=1 的卡）----
    # 追加在四段之后 ⇒ 不经任何按卡过滤（不随 perms 收敛），可见性唯一由
    # auto_mode 决定。非 auto 卡 ⇒ 本段不进 ⇒ 输出与基线逐字一致。
    _card = (cards or {}).get(card_id) or {}
    if card_id and cards is not None and _card and bool(_card.get("auto_mode")):
        tools = _apply_auto_toolface(tools)
    # ---- 末段②：专用工具追加（非卡片内挂；只在 op 回合）----
    # [合并保留] 与末段①**互相独立**（auto 卡 / op 回合），各判各的。
    if op == OP_UPDATE_STATUS:
        tools.append(_schema_of(TOOL_REGISTRY["propose_status"]))
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
        if name == "create_card":
            out.append(_create_card_schema_with_goal(t))
        else:
            out.append(t)
    # 追加序固定（块0.4 起 update_goal 首位；G2 起两件通信随后）——集合成员
    # 增长不改首位语义，测试与审计都认这个序。
    for auto_name in ("update_goal", "send_card_message", "read_inbox"):
        if auto_name in AUTO_ONLY_TOOLS:
            out.append(_schema_of(TOOL_REGISTRY[auto_name]))
    return out



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
MSG_RULINGS_APPEND_ONLY = "定论块只追加；失效用就地标记（→被N取代）"
MSG_DECISION_IN_STATUS = "现状块不装决定；决定进定论（D{nn} 条目属定论块）"

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
    """把定论块解析成 {D编号: 该行原文}；非 D 行（说明性文字）不入表。

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
    """取出定论块里的失效标记行（行首 →）逐字原文集合（S3-3 规则③）。

    语义：失效条目**就地一行标记、不保留原文、不设已废区**——被标记的条目
    在定论块里不再以原文出现，这是**合规的处置**，不是「偷偷删掉」。
    规则②判「旧条目是否被删改」时，用它做一对一抵扣（见 _rulings_superset）。
    """
    return {ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("→")}


def _rulings_superset(old_text: str, new_text: str) -> bool:
    """定论区超集判定（S3-3 规则②③）：True＝合规（只追加或合规失效）。

    判据：
      1. 旧条目逐条在新块里找「同编号且原文逐字一致」的——找到即合规；
      2. 找不到的旧条目（被删或被改写）逐条与新块里的**新增失效标记行**做
         一对一抵扣：一条标记行抵一条被移除的条目。抵扣后仍有剩余 ⇒ 拒。
    一对一而非「标记行数量 ≥ 0 即放行」：否则模型可以一次性删光所有条目、
    只留一行标记，绕过只追加约束。抵扣不掉的那一条才是真删除。
    """
    old_entries = _rulings_entries(old_text)
    new_entries = _rulings_entries(new_text)
    old_markers = _superseded_markers(old_text)
    new_markers = _superseded_markers(new_text)
    # 本次新增的标记行（旧的标记行不参与抵扣——它们上一轮就抵过了）
    fresh_markers = new_markers - old_markers
    removed = [
        line for num, line in old_entries.items() if new_entries.get(num) != line
    ]
    if len(removed) <= len(fresh_markers):
        return True
    return False


def _check_block_rules(old_body: str, new_body: str) -> dict[str, Any]:
    """分区 diff 判闸（S3-1/S3-3）。返回 {"ok","rule","message"}。

    七条规则：
      ①使命区不等 → 拒（立项定稿，模型改需用户确认）
      ②定论区非超集 → 拒（只追加；既有 D 条目被删或被改写即拒）
      ③行首「→」的失效标记行放行（它在 ② 里不算被删的条目）
      ④现状区 diff 不校验（覆盖写·单一最新，本就该自由改）
      ⑤修改历史区 diff 忽略（机械尾，系统自己写）
      ⑥旧结构卡不校验（旧卡无三块语义，闸不作用于存量卡）
      ⑦现状区出现整行 D{nn}【 → 拒（决定归定论块）

    [S3-4/RD-11] **不含锚点闸**：现状块的「事实带锚点自指」是语义纪律，
    不做机械校验。逐字理由：机械强制会激励伪造锚点，伪证比缺证更伤权威机制。
    """
    old_sec = split_body_sections(old_body or "")
    new_sec = split_body_sections(new_body or "")

    # 规则⑥：旧结构卡不校验（含混挂卡——它们没有可判的使命/定论语义）
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

    # 规则②③：定论区只追加。旧条目逐条在新块里找**同编号且原文逐字一致**的；
    # 找不到即视为被删或被改写 → 拒。失效标记行不参与（规则③）。
    # 无区头正文＝只提交现状块 ⇒ 定论块未提交 ⇒ 不参与本规则。
    if new_has_heading:
        if not _rulings_superset(old_sec["rulings"], new_sec["rulings"]):
            return {
                "ok": False, "rule": "rulings_not_superset",
                "message": MSG_RULINGS_APPEND_ONLY,
            }

    # 规则⑦：现状块整行出现 D{nn}【 → 拒（决定归定论）。
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