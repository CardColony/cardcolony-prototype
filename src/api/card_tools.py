# -*- coding: utf-8 -*-
"""卡片工具与权限（白板版 2026-10-02）。

工具接口只含机械事实：
- read_card：返回标题、父卡、连线、正文——不暴露内部四区语义
- edit_card(card_id, content)：覆盖写指定卡正文（内部落存储层正文区）
- link_cards：连线，模型可用类型限 先后/关联（锚定为系统内部关系，不对模型开放）

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
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Union

from ..permissions import policy
from ..storage import card_store, manuscript_store, memory_store, shadow_store, work_store
from ..storage import session_facade, session_ops
from ..storage.hash_utils import parse_front_matter, split_body_sections

from . import card_msg, event_stream
from ..toolcard import driver as toolcard_driver
from ..toolcard import script_loader as toolcard_script_loader
from ..toolcard import step_prompt as toolcard_step_prompt
from ..thinkchain import plan_tools as thinkchain_plan_tools
from . import file_tools, prompt_log

logger = logging.getLogger("api.card_tools")

# [白板版 2026-10-02] 模型可建的连线类型收窄为两个中性关系；
# "锚定"（系统派生目标卡专用）与"兑现"（写作agent残留语义）不再对模型开放
MODEL_REL_TYPES = ("先后", "关联")

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
#   update_shadow                          → shadow_write（L361）
#   list_manuscript/read_manuscript/search_manuscript → manuscript（新增开关，原无）
#   run_tool_card                          → invoke_tool（新增开关，原无）
#   create_plan/plan_reorder/plan_insert/plan_remove/plan_modify/step_complete
#                                           → plan（新增开关，原无；这六件由
#                                             src/thinkchain/plan_tools 注册进同一个
#                                             TOOL_REGISTRY，同样会被本过滤扫到）
#   web_search / fetch_web                 → tools_web
#   list_files/read_file/search_files/write_file → tools_file
TOOL_SWITCH: dict[str, str] = {
    "read_card": policy.SWITCH_READ,
    "read_memory": policy.SWITCH_READ,
    "read_shadow": policy.SWITCH_READ,
    "edit_card": policy.SWITCH_EDIT,
    "delete_card": policy.SWITCH_DELETE,
    "create_card": policy.SWITCH_CREATE_CHILD,
    "link_cards": policy.SWITCH_LINK,
    "unlink_cards": policy.SWITCH_UNLINK,
    "update_shadow": policy.SWITCH_SHADOW_WRITE,
    "list_manuscript": policy.SWITCH_MANUSCRIPT,
    "read_manuscript": policy.SWITCH_MANUSCRIPT,
    "search_manuscript": policy.SWITCH_MANUSCRIPT,
    "run_tool_card": policy.SWITCH_INVOKE_TOOL,
    "web_search": policy.SWITCH_TOOLS_WEB,
    "fetch_web": policy.SWITCH_TOOLS_WEB,
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
TOOL_TABLE_VERSION = "tools-tc-3"


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
    return {
        "card_id": target_id,
        "title": result["meta"].get("title", ""),
        "parent": result["meta"].get("parent", ""),
        "relations": result["meta"].get("relations") or [],
        "body": result["body"].get("status_block", ""),
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
    # [卡片信息结构改造 S3-2] 取数路径：读 target 的 raw（不是 current——
    # current 只为取 cur_title），拆出旧正文；不新增存储层接口、
    # 不改 read_card 签名。read_card 已在上面为权限/存在性读过一遍，
    # 这里再读一次取 raw 是同一路径同一签名，无新增面。
    old_raw = (await card_store.read_card(work_id, target_id))["raw"]
    _, old_body = parse_front_matter(old_raw)
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
    # 上级卡经此查子卡影子——推送（裁定 1）的到达机制
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
    existing = current["meta"].get("relations") or []
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
        {"relations": list(existing) + [new_rel]},
        f"由「{cur_title}」对话建立关系：{rel_type} → {target_id}",
    )
    if "error" in result:
        return result
    return {"ok": True, "source": current_id, "target": target_id, "type": rel_type}


async def _h_unlink_cards(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [解线 2026-10-03] 与 link_cards 对称；走 update_card(relations)，
    # 与前端 ✕ 删线同一条存储通道。悬空线主用例：目标卡已删时
    # relations 条目仍留在 source 卡上，此处不检查目标卡存在性。
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
    existing = current["meta"].get("relations") or []

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
        {"relations": remaining},
        f"由「{cur_title}」对话解除关系 → {target_id}",
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
    # [T6 路径锁 2026-10-09] 同上（统一关口）
    if not file_tools.check_file_access(
        arguments.get("path", ""), cards.get(current_id),
    ):
        return err("denied", f"越权：无权访问 {arguments.get('path', '')}")
    return await asyncio.to_thread(
        file_tools.search_files,
        arguments.get("keyword", ""),
        arguments.get("path", ""),
        limit=int(arguments.get("limit") or 50),
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

TOOL_REGISTRY: dict[str, ToolDef] = {
    "read_card": ToolDef(
        schema={
            "name": "read_card",
            "description": "读取指定卡（标题、父卡、连线、正文）。只能读取你有权访问的卡。",
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
            "description": "覆盖写你自己（当前卡）的影子卡——过程性记录。无 card_id 参数，只能写当前卡自己的影子。影子卡正文有且只有三节，每节内容边界（不得越界、不得改节名、不得加节）：【进度】只放本卡推进的时间线记录（什么时间、做到哪一步、当前什么状态），不写结论/分析/无关杂事；【产物清单】只放本卡推进中产生的文件/代码/文档的路径与用途指针（一行一个：路径＋一句话说明它是什么），只写指针不搬运内容正文；【迭代历史】只放改动指针（改了什么、为什么改），细节回既有通道查、不在此复述。三条禁止：①不许把正卡的结论正文写进影子卡（正卡写结论、影子卡写过程，不得混）；②不许写与本卡推进无关的内容；③不许把影子卡当备忘录堆杂事。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "影子正文全文（覆盖写）。必须含且仅含三节，节名固定：## 进度 / ## 产物清单 / ## 迭代历史。每节只放该节边界内的内容（进度＝时间线；产物清单＝路径与用途指针；迭代历史＝改动指针），不得写入正卡结论正文、不得写无关内容、不得堆杂事。"},
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
            "description": "读取指定卡的影子卡全文（过程性记录）。card_id 缺省＝当前卡自己的影子；上级卡可借此查看任一子卡的影子（子卡影子的更新由此到达上级）。",
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
    schemas = {s["name"]: s for s in file_tools.build_file_tool_schemas()}
    for name, (handler, readonly) in handlers.items():
        TOOL_REGISTRY[name] = ToolDef(
            schema=(lambda n=name: schemas[n]),
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
    msg_id = card_msg.make_msg_id(current_id)
    body = card_msg.build_header(msg_id, mt, current_id, reply_to) + "\n" + content
    await session_facade.append(work_id, target_id, {
        "actor": f"card:{current_id}",   # 显式给 actor → 绕过 role 映射表
        "from": current_id,
        "content": body,
        "behavior": "",                  # 空串 → 进对话投影 + 进模型视野
        "end": "",                       # 卡块不闭合轮
    })
    session_ops.ops_log("card_msg_send", work_id, target_id,
                        {"msg_id": msg_id, "type": mt, "from": current_id})
    card_msg.TRIGGERS.setdefault((work_id, target_id), []).append(msg_id)
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


def build_tool_schemas(
    card_id: str = "",
    work_id: str = "",
    cards: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """OpenAI function calling 工具列表（直接传给 gateway.call(tools=...)）。

    [B-09 2026-10-08] 改为遍历注册表派生（改前是 14 段字面量 list ＋
    file_tools 追加）。顺序＝注册表插入序＝改前字面量顺序，逐字不变。

    [工具卡底座 T10 · EC-120] 加两级过滤，**各拥一个独立 helper**
    （两线 R-D：共用函数但各改各的内层，不得各改一套）：

        一级 ``_filter_by_role``     ← 角色过滤（工具卡底座 T9）
        二级 ``_filter_by_category`` ← 分类过滤（权限线 T4）

    [T4 权限收口 2026-10-09] ``cards`` ＝ 分类查表所需卡面 meta（上游装配层补传）。

    [权限底座重做 2026-10-12] **无卡上下文 ⇒ 默认关门**：认不出当前卡是谁时，
    二级过滤返回空工具列表（不再逐字返回全量工具面）。带上卡上下文（生产链路
    message_runner 恒定传 ``card_id/work_id/cards``）⇒ 按该卡固化的 perms 下发。
    """
    tools = [_schema_of(def_) for def_ in TOOL_REGISTRY.values()]
    # ---- 一级：角色过滤（工具卡底座 T9）----
    tools = _filter_by_role(tools, card_id, work_id, cards)
    # ---- 二级：分类过滤（权限线 T4）----
    tools = _filter_by_category(tools, card_id, work_id, cards)
    return tools


def _collect_card_meta(work_id: str) -> dict[str, dict[str, Any]]:
    """轻量收集 work 下所有卡的 meta（含 parent/card_type/relations）。

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