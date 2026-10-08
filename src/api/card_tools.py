# -*- coding: utf-8 -*-
"""卡片工具与权限（白板版 2026-10-02）。

工具接口只含机械事实：
- read_card：返回标题、父卡、连线、正文——不暴露内部四区语义
- edit_card(card_id, content)：覆盖写指定卡正文（内部落存储层正文区）
- link_cards：连线，模型可用类型限 先后/关联（锚定为系统内部关系，不对模型开放）

[卡片权限改造 2026-10-07] 权限纯函数：读＝全盘可读（存在即放行，亲属链/连线/锚卡读限整制去除——小说项目机制）；写＝自己的子树（不随读放开）。
越权不抛异常，返回 {"error": ...} 给模型。

[建卡工具化 2026-10-08] create_card 入标准工具通道（此前只走文本协议块
card-proposal，见 card_proposal.py）。两条通道并存：工具通道优先、文本通道
兜底（模型过渡期仍写文本块），跨通道同轮去重防重复建卡。
**建卡权限口径＝写权限（用户 2026-10-08 14:14 定）**：只能建自己的子卡片
（parent 落在current 的子树内；parent 缺省＝当前卡自身，即建自己的下一层）。
与 edit_card/delete_card 同用 can_edit 判据，不新增第二套权限规则。

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

from ..storage import card_store, manuscript_store, memory_store, shadow_store, work_store
from ..storage.hash_utils import parse_front_matter, split_body_sections
from . import file_tools

logger = logging.getLogger("api.card_tools")

# [白板版 2026-10-02] 模型可建的连线类型收窄为两个中性关系；
# "锚定"（系统派生目标卡专用）与"兑现"（写作agent残留语义）不再对模型开放
MODEL_REL_TYPES = ("先后", "关联")

# [T12-R3] 工具表基准版本化固化：工具集/schema 变更须递增版本号并对账基准指纹。
# tools-t12-1：T12 增补 read_memory（其余 16 件与 T11 基线逐字一致）。
# tools-tc-1：[建卡工具化 2026-10-08] 增补 create_card（共 18 件）。
# tools-tc-2：[卡片信息结构改造 S3-7/S3-8 · 2026-10-08] edit_card schema 描述随闸语义
# 更新（三块语法＋条目语法＋三条写入规则）；工具件数不变。
# [B-09 2026-10-08] 注册表落地**不改工具面** ⇒ 版本号保持 tools-tc-2 不递增。
TOOL_TABLE_VERSION = "tools-tc-2"


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
    if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
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
    if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
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
    # [建卡工具化 2026-10-08] 权限＝写权限（can_edit）：只能建自己的子卡片
    # （用户 2026-10-08 14:14 定）。parent 缺省＝当前卡自身（建自己下一层）；
    # 给了 parent 则该卡必须落在 current 的子树内——用 can_edit 判，
    # 与 edit_card/delete_card 同一判据，不新增第二套权限规则。
    # 结构性护栏（剧本根卡/带子卡者不可删）不在此——建卡不涉删。
    title = str(arguments.get("title", "") or "").strip()
    if not title:
        return err("validation", "title 不能为空")
    # parent 缺省＝当前卡自身；显式给空串也按缺省处理（模型常传空串）
    target_parent = str(arguments.get("parent", "") or "").strip() or current_id
    # 存在性先判：已删/不存在的 parent 不误报越权（与 read_card 同序）
    if target_parent not in cards:
        return err("not_found", f"父卡不存在: {target_parent}")
    if not can_edit(current_id, target_parent, cards):
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
    # [影子卡 2026-10-03] 影子进字典后 can_edit 会放行正卡的上级（子树判据），
    # 此守卫封死该后门：影子只能由对应正卡经 update_shadow 修改（R-06/R-09 写侧）
    if cards[target_id].get("card_type") == "shadow":
        return err("denied", "影子卡只能由对应正卡经 update_shadow 修改")
    if not can_edit(current_id, target_id, cards):
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
    if not can_edit(current_id, target_id, cards):
        return err("denied", f"越权：无权删除 {target_id}")
    if target_id == work_store.work_card_id_of(work_id):
        return err("denied", f"剧本根卡不可删除: {target_id}")
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
    content = arguments.get("content", "")
    return await shadow_store.update_shadow(work_id, current_id, content)


async def _h_read_shadow(work_id: str, current_id: str,
                         arguments: dict[str, Any],
                         cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # [影子卡 2026-10-03] 读侧跟随现有 can_read（R-09 读侧：不另设判据）；
    # 上级卡经此查子卡影子——推送（裁定 1）的到达机制
    target_id = arguments.get("card_id", "") or current_id
    if target_id.startswith("shadow_"):
        target_id = target_id[len("shadow_"):]
    if target_id not in cards:
        return err("not_found", f"卡片不存在: {target_id}")
    if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
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
    chapters = manuscript_store.list_chapters(work_id)
    if not chapters:
        return {"chapters": [], "note": "本项目尚无原文"}
    return {"chapters": chapters}


async def _h_read_manuscript(work_id: str, current_id: str,
                             arguments: dict[str, Any],
                             cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
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
    if not can_read(current_id, target_id, cards, work_store.work_card_id_of(work_id)):
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
    return await asyncio.to_thread(
        file_tools.list_files,
        arguments.get("path", ""),
        limit=int(arguments.get("limit") or 200),
    )


async def _h_read_file(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await asyncio.to_thread(
        file_tools.read_file,
        arguments.get("path", ""),
        para_start=arguments.get("para_start"),
        para_end=arguments.get("para_end"),
    )


async def _h_search_files(work_id: str, current_id: str,
                          arguments: dict[str, Any],
                          cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await asyncio.to_thread(
        file_tools.search_files,
        arguments.get("keyword", ""),
        arguments.get("path", ""),
        limit=int(arguments.get("limit") or 50),
    )


async def _h_write_file(work_id: str, current_id: str,
                        arguments: dict[str, Any],
                        cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return await asyncio.to_thread(
        file_tools.write_file,
        arguments.get("path", ""),
        arguments.get("content", ""),
    )


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
    # [建卡工具化 2026-10-08] 标准工具通道建卡。权限＝写权限（can_edit）：
    # 只能建自己的子卡片（用户 2026-10-08 14:14 定）——parent 缺省＝当前卡自身。
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


def build_tool_schemas() -> list[dict[str, Any]]:
    """OpenAI function calling 工具列表（直接传给 gateway.call(tools=...)）。

    [B-09 2026-10-08] 改为遍历注册表派生（改前是 14 段字面量 list ＋
    file_tools 追加）。顺序＝注册表插入序＝改前字面量顺序，逐字不变。
    """
    return [_schema_of(def_) for def_ in TOOL_REGISTRY.values()]


def _collect_card_meta(work_id: str) -> dict[str, dict[str, Any]]:
    """轻量收集 work 下所有卡的 meta（含 parent/card_type/relations）。

    [T-0007写路径收口] 改走card_store/shadow_store 的公开接口
    （iter_card_metas / iter_shadow_metas），不再跨层摸 _cards_dir/_meta_of/
    _shadows_dir。影子 meta 并入同一字典（R-09 读侧：影子挂在正卡之下，
    can_read 判据经 parent 链自动作用在影子上）；键＝shadow_{正卡id}。
    """
    cards = card_store.iter_card_metas(work_id)
    cards.update(shadow_store.iter_shadow_metas(work_id))
    return cards


def can_read(
    current_id: str, target_id: str, cards: dict[str, dict[str, Any]],
    work_card_id: str = "",
) -> bool:
    """读权限：[卡片权限改造 2026-10-07] 全盘放开——target 存在即放行。

    用户令（2026-10-07 11:26）：任一卡可读全项目任何卡的信息，但不放开写；
    原按亲属链限读（判据①读自己/②子树/③血统链/④连线/⑤锚卡全局）是小说
    项目移植的机制，整制退役。仅保留存在性校验（不因放开而丢）：
    target 不在 cards（不存在/已删）→ False；current 不在 cards
    （无有效对话卡上下文，维持旧态行为）→ False。

    cards: {card_id: meta dict（含 parent, card_type, relations）}
    work_card_id: 形参保留（四个消费点调用签名不动）；判据⑤退役后无判据使用。
    """
    if cards.get(target_id) is None:
        return False
    if cards.get(current_id) is None:
        return False
    return True


def can_edit(current_id: str, target_id: str, cards: dict[str, dict[str, Any]]) -> bool:
    """写权限：target 在 current 的子树内（含 current 自身）→ True。

    血统链上级不可写、契约卡不可写。
    """
    if target_id == current_id:
        return True
    # 反向遍历 target 的 parent 链，能否到达 current
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
        if node == current_id:
            return True
        nm = cards.get(node)
        if nm is None:
            break
        node = nm.get("parent", "")
    return False


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