# -*- coding: utf-8 -*-
"""HTTP API 路由。

错误约定：CardNotFoundError→404；CardLockedError→423；请求体校验失败→400；
模型调用失败→502。错误响应 {"error": "说明"}。

Phase 7：
- 新增 /api/works 全套路由（多剧本）。
- messages 主链路：context_assembler 组装 → prompts.build_system_prompt →
  带工具的工具循环（不设轮次封顶）→ parse_proposals → apply_proposals → tool_events。
- 旧路由 /api/canvas、/api/cards... 保留，内部固定转发 work_001（兼容层）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..storage import (
    activity, canvas_store, card_store, manuscript_store,
    memory_store, session_store, work_store,
)
from ..storage.card_store import CardLockedError, CardNotFoundError
from ..storage.card_store import CardDeleteDeniedError
from ..storage.manuscript_store import ChapterNotFoundError
from . import card_proposal, card_tools, context_assembler, context_regions, event_stream, message_runner, model_service, progress, prompt_log, prompts, simple_service, text_tool_rescue, tool_fold

logger = logging.getLogger("api.routes")

router = APIRouter(prefix="/api")

DEFAULT_WORK_ID = "work_001"  # 兼容层固定转发目标

# [卡片信息结构改造 S2-8] 白名单全集。新键 mission/status_block/rulings 为当前口径；
# 旧键 charter/conclusion/rejections 保留（存量前端与脚本照旧可用，存储层内归一）。
# 两个校验点（新版 PATCH :276 ＋ 兼容层 PATCH :463）共用本集合——改一处即两处同步。
ALLOWED_UPDATE_KEYS = {
    "title", "tags", "relations", "session_ref",
    "mission", "status_block", "rulings",  # 正文三块·新键
    "conclusion", "rejections", "charter",  # 正文三块·旧键（兼容保留）
    "card_type", "parent",
}
# [phase8.3] status 二态已废除：PATCH 带 status 一律忽略（不报错）

# [无闸改造 2026-10-02] MAX_TOOL_ROUNDS 已删除：工具循环不设封顶


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


# ============================================================
# [信息流与快车道 Task1 · 乙线核心] 快车道路由分流
# 纯函数规则集：确定性动作（删卡/建卡/改卡/查询）四类消息形态 → fastlane
# （单模型带工具直答，跳三席与统合官——用户裁决 A97）；未命中保守落 chain
# （全链现状逐字不变）。函数纯、无副作用、可单测（R3 样本「那你倒是删掉一张啊」）。
# ============================================================
_FASTLANE_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    ("删卡", re.compile(r"(删掉|删除|删了)[^。！？\n]{0,8}(卡|一张|这张|那张|它)")),
    ("建卡", re.compile(r"(建|创建|新建)[^。！？\n]{0,6}卡(片)?")),
    ("改卡", re.compile(
        r"((改一下|改改|修改|编辑|更新|替换)[^。！？\n]{0,8}卡(片)?(的|正文|内容|结论)"
        r"|(内容|正文|结论)[^。！？\n]{0,4}(改一下|改改|改掉|更新|换掉|替换|修改))"
    )),
    ("查询", re.compile(
        r"((查看|看一下|看下|读一下|读取|查一下|查下)[^。！？\n]{0,6}卡(片)?(的)?(内容|正文|信息|结论|详情)?"
        r"|(查看|看一下|看下|读一下|读取|查一下|查下|打开)[^。！？\n]{0,4}card_\d+)"
    )),
]


def _route_rule(content: str) -> str:
    """命中规则名（打点用 matched_rule）；未命中返回「未命中」。"""
    for name, pat in _FASTLANE_RULES:
        if pat.search(str(content or "")):
            return name
    return "未命中"


def route(content: str) -> str:
    """快车道判定（Task1）：返回 "fastlane" | "chain"。保守不误判：规则全未
    命中即 chain（开放论证永远走全链）。"""
    return "fastlane" if _route_rule(content) != "未命中" else "chain"


# ============================================================
# Request models
# ============================================================

class CreateWorkRequest(BaseModel):
    title: str = Field(min_length=1)


class UpdateWorkRequest(BaseModel):
    title: Optional[str] = None
    ending_derived: Optional[bool] = None


class PatchPositionsRequest(BaseModel):
    positions: dict[str, dict[str, Any]]


class CreateCardRequest(BaseModel):
    title: str = Field(min_length=1)
    source: str
    card_type: str = "线索"
    parent: Optional[str] = None
    charter: str = ""
    conclusion: str = ""


class PatchCardRequest(BaseModel):
    updates: dict[str, Any] = Field(default_factory=dict)
    history_note: str


class PostMessageRequest(BaseModel):
    content: str = Field(min_length=1)
    # [T10-3-3] 重发去重：前端重发时携带被重发那条提问的原 pair_id；正常提问缺省 None。
    pair_id: Optional[str] = None


# [无内容预设改造 2026-10-02] PatchMethodologyRequest 已删除（方法论库停用）


# ============================================================
# Works 路由
# ============================================================

@router.get("/works")
async def list_works():
    return {"works": work_store.list_works()}


@router.post("/works")
async def create_work(req: CreateWorkRequest):
    result = await work_store.create_work(req.title)
    if "error" in result:
        return _err(400, result["error"])
    return JSONResponse(status_code=201, content=result)


# ============================================================
# [T17-B4] 跨项目导航树（一次请求拿全，避免 N+1）
# ============================================================

@router.get("/nav")
async def get_nav():
    """返回 {"projects": [...]}，供工作区左栏对话导航渲染。

    排序契约（方案 §4.3，服务端负责、前端不重排）：
    - projects 按 last_active_at 降序（list_works 已排好）；
    - 每项目 cards 按 last_active_at 降序，无时间者排末位，
      并列以 card_id 升序稳定收尾（刷新不跳序）；
    - 卡清单从卡文件真身现读（activity.iter_card_metas），活跃度请求时现算、
      不入盘（RD-06）；根卡作为分组头不重复列树（方案 §3.2）；
    - 孤儿卡（parent 指向已删卡）不丢弃：parent 原样返回，前端挂该项目根层末尾。
    既有端点返回结构零改动；/api/works 仅按 B-3 附 last_active_at 并改排序。
    """
    projects = []
    for w in work_store.list_works():
        wid = w.get("work_id", "")
        metas = list(activity.iter_card_metas(wid))
        child_count: dict[str, int] = {}
        for m in metas:
            p = m.get("parent", "")
            if p:
                child_count[p] = child_count.get(p, 0) + 1
        root_id = w.get("work_card_id", "")
        # [主界面优化 E2] 同源一次现算每卡 own/branch 活跃（activity.compute_card_activity）；
        # 端点现算增列、不写任何真身文件（RD-02 数据层零写）。
        act = activity.compute_card_activity(wid)
        cards = []
        for m in metas:
            cid = m.get("card_id", "")
            if cid and cid == root_id:
                continue
            cards.append({
                "card_id": cid,
                "title": m.get("title", ""),
                "parent": m.get("parent", ""),
                "child_count": child_count.get(cid, 0),
                "last_active_at": act.get(cid, {}).get("own", ""),
                # [主界面优化 E2] 分支活跃（该卡及其全部子孙最近活跃）现算增列
                "branch_active_at": act.get(cid, {}).get("branch", ""),
            })
        # 两段稳定排序：先并列收尾键 card_id 升序，再按活跃降序（空串排末位）
        cards.sort(key=lambda c: c["card_id"])
        cards.sort(key=lambda c: c["last_active_at"] or "", reverse=True)
        projects.append({
            "work_id": wid,
            "title": w.get("title", ""),
            "work_card_id": root_id,
            "last_active_at": w.get("last_active_at", ""),
            "card_count": w.get("card_count", 0),
            "cards": cards,
        })
    return {"projects": projects}


@router.get("/works/{work_id}/canvas")
async def get_work_canvas(work_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return canvas_store.load(work_id)


@router.patch("/works/{work_id}/canvas/positions")
async def patch_work_positions(work_id: str, req: PatchPositionsRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return canvas_store.merge_positions(work_id, req.positions)


@router.post("/works/{work_id}/cards")
async def create_work_card(work_id: str, req: CreateCardRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    if req.source not in card_store.ALLOWED_SOURCE:
        return _err(400, f"source 非法: {req.source}，只允许 {list(card_store.ALLOWED_SOURCE)}")
    try:
        result = await card_store.create_card(
            work_id, req.title, req.source,
            card_type=req.card_type, parent=req.parent,
            charter=req.charter, conclusion=req.conclusion,
        )
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    return JSONResponse(status_code=201, content=result)


@router.get("/works/{work_id}/cards/{card_id}")
async def get_work_card(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        return await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")


# [用户删卡 2026-10-02] 仅前端调用；模型工具清单不含删卡（模型无权删卡）
@router.delete("/works/{work_id}/cards/{card_id}")
async def delete_work_card(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        result = await card_store.delete_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    except CardDeleteDeniedError as exc:
        # 根卡与带子卡的卡拒绝删除（结构性防护，机械判据）
        is_root = card_id == work_store.work_card_id_of(work_id)
        return _err(403 if is_root else 400, str(exc))
    if "error" in result:
        return _err(400, result["error"])
    return result


@router.patch("/works/{work_id}/cards/{card_id}")
async def patch_work_card(work_id: str, card_id: str, req: PatchCardRequest):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    if not req.history_note.strip():
        return _err(400, "history_note 必填非空")
    req.updates.pop("status", None)  # [phase8.3] 收到 status 忽略，不报错
    illegal = set(req.updates) - ALLOWED_UPDATE_KEYS
    if illegal:
        return _err(400, f"updates 含不允许的键: {sorted(illegal)}")
    try:
        result = await card_store.update_card(work_id, card_id, req.updates, req.history_note)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    return result


@router.get("/works/{work_id}/cards/{card_id}/session")
async def get_work_session(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    return await session_store.read_session(work_id, card_id)


@router.get("/works/{work_id}/cards/{card_id}/stream")
async def stream_card_events(work_id: str, card_id: str, frm: int = Query(0, alias="from")):
    """[信息流 Task3] SSE 过程通道：内存环形事件日志（event_stream）按 seq 重放＋
    实时跟进；from=0 全量重放（刷新重建，A50）。事件纯展示层，不进 session/prior
    （A42）；单进程内存态（A44）。心跳＝SSE 注释帧（本体论 ping，不占 journal）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")

    async def gen():
        yield "retry: 3000\n\n"
        last = frm - 1
        idle = 0.0
        while True:
            batch = [e for e in event_stream.journal_of(work_id, card_id) if e["seq"] > last]
            if batch:
                for evt in batch:
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                    last = evt["seq"]
                idle = 0.0
                continue
            await asyncio.sleep(0.4)
            idle += 0.4
            if idle >= 15.0:
                yield ": ping\n\n"
                idle = 0.0

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/works/{work_id}/cards/{card_id}/tokens")
async def stream_card_tokens(work_id: str, card_id: str, frm: int = Query(0, alias="from")):
    """[信息流重出 Task4] SSE 逐字流通道（**独立**于 /stream 的过程卡流）。

    与既有 17 类事件隔离申报（红线4）：独立 journal、独立 seq 计数器、不占主 seq、
    不被过程卡的 300 条环形挤出。from=0 全量重放——刷新后逐字流仍能完整重建
    （V-T4-1）。帧形态与 /stream 同构：{"seq","ts","type","data"}，
    type ∈ {reasoning_chunk, content_chunk}。

    纯展示层：不进 session/prior/模型输入（红线3/A42），不落盘、单进程内存态（A44）。
    """
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")

    async def gen():
        yield "retry: 3000\n\n"
        last = frm - 1
        idle = 0.0
        while True:
            batch = [e for e in event_stream.token_journal_of(work_id, card_id) if e["seq"] > last]
            if batch:
                for evt in batch:
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                    last = evt["seq"]
                idle = 0.0
                continue
            await asyncio.sleep(0.25)
            idle += 0.25
            if idle >= 10.0:
                yield ": ping\n\n"
                idle = 0.0

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/works/{work_id}/cards/{card_id}/messages")
async def post_work_message(work_id: str, card_id: str, req: PostMessageRequest):
    """对话主链路（模块 H2）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return await _run_message(work_id, card_id, req.content, req.pair_id)


# [实时反馈 2026-10-02] 调用进度查询：纯展示层，前端轮询用。
# 只读内存态，不进任何模型输入，不拦截任何调用——不是闸。
@router.get("/works/{work_id}/cards/{card_id}/progress")
async def get_work_progress(work_id: str, card_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    st = progress.get_stage(work_id, card_id)
    return st if st is not None else {"stage": "idle", "detail": "", "ts": 0.0}


# [phase8.5] 原文只读端点（给未来前端留口，不加写端点）
@router.get("/works/{work_id}/manuscript")
async def list_work_manuscript(work_id: str):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return {"chapters": manuscript_store.list_chapters(work_id)}


@router.get("/works/{work_id}/manuscript/{chapter}")
async def read_work_manuscript(
    work_id: str,
    chapter: str,
    para_start: Optional[int] = None,
    para_end: Optional[int] = None,
):
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    try:
        return manuscript_store.read_chapter(
            work_id, chapter, para_start=para_start, para_end=para_end,
        )
    except ChapterNotFoundError:
        return _err(404, f"章节不存在: {chapter}")


# [无内容预设改造 2026-10-02] 方法论库三端点（GET /methodology 等）已删除：
# 模型自产内容入库并回流注入是自我繁殖污染链（m_0001/m_0002 事件）。


# ============================================================
# 兼容层（旧前端不断服）：固定转发 work_001
# ============================================================

@router.get("/canvas")
async def get_canvas_compat():
    return canvas_store.load(DEFAULT_WORK_ID)


@router.post("/cards")
async def create_card_compat(req: CreateCardRequest):
    if req.source not in card_store.ALLOWED_SOURCE:
        return _err(400, f"source 非法: {req.source}，只允许 {list(card_store.ALLOWED_SOURCE)}")
    try:
        result = await card_store.create_card(
            DEFAULT_WORK_ID, req.title, req.source,
            card_type=req.card_type, parent=req.parent,
            charter=req.charter, conclusion=req.conclusion,
        )
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    return JSONResponse(status_code=201, content=result)


@router.get("/cards/{card_id}")
async def get_card_compat(card_id: str):
    try:
        return await card_store.read_card(DEFAULT_WORK_ID, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")


@router.patch("/cards/{card_id}")
async def patch_card_compat(card_id: str, req: PatchCardRequest):
    if not req.history_note.strip():
        return _err(400, "history_note 必填非空")
    req.updates.pop("status", None)  # [phase8.3] 收到 status 忽略，不报错
    illegal = set(req.updates) - ALLOWED_UPDATE_KEYS
    if illegal:
        return _err(400, f"updates 含不允许的键: {sorted(illegal)}")
    try:
        result = await card_store.update_card(DEFAULT_WORK_ID, card_id, req.updates, req.history_note)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    if "error" in result:
        return _err(400, result["error"])
    return result


@router.get("/cards/{card_id}/session")
async def get_session_compat(card_id: str):
    try:
        await card_store.read_card(DEFAULT_WORK_ID, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")
    return await session_store.read_session(DEFAULT_WORK_ID, card_id)


@router.post("/cards/{card_id}/messages")
async def post_message_compat(card_id: str, req: PostMessageRequest):
    return await _run_message(DEFAULT_WORK_ID, card_id, req.content, req.pair_id)


# ============================================================
# messages 主链路（工具循环 + 建卡协议闭环）
# ============================================================

async def _run_message(work_id: str, card_id: str, content: str,
                       client_pair_id: Optional[str] = None) -> Any:
    """完整 messages 链路，新旧路由共用。

    顺序：读卡 → 取历史 → 追加用户消息 → context_assembler 组装 →
    prompts.build_system_prompt → 带工具的工具循环 → parse_proposals →
    apply_proposals → 写 prompt 日志 → 追加 assistant 消息 → 返回。

    [T10] client_pair_id：前端重发时携带的被重发提问原 pair_id（正常提问为 None）。
    """
    # [信息流重出 Task4] 标记本请求的逐字流归属卡。model_service.call_main 经
    # contextvar 取到它，才把增量落到 /tokens 独立流；函数退出时还原，
    # 避免污染同进程后续请求（凝练/归拢等无卡调用自然无逐字流）。
    _stream_ctx_token = event_stream.set_stream_context(work_id, card_id)
    try:
        return await _run_message_inner(work_id, card_id, content, client_pair_id)
    finally:
        event_stream.reset_stream_context(_stream_ctx_token)


async def _run_message_inner(work_id: str, card_id: str, content: str,
                             client_pair_id: Optional[str] = None) -> Any:
    """_run_message 的实际链路体（外层只负责逐字流 contextvar 的设/还原）。

    [T-0009 拆分] 本函数**只做三件事**：① 装配（读卡/pair 去重/上下文）
    → ② 循环步进（step/state）→ ③ 落盘与返回。六段编排细节已迁至
    `message_runner`（RunState ＋ step ＋ check_control 中止缝）。
    **编排语义逐字不变**——判据＝tests/_golden/ 三剧本金样本逐字节回放一致。
    """
    # 段① 读卡 ＋ pair_id 去重追加（详见旧 L524-564，逻辑逐字迁移）
    state = message_runner.RunState(work_id=work_id, card_id=card_id, content=content)

    # a. 确认卡片存在
    try:
        await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")

    # a0. [T10-1-1] 读取历史之前生成本次请求标识（UUID4 十六进制串），打点含标识与卡号
    pair_id = uuid.uuid4().hex
    state.pair_id = pair_id
    logger.info("[T10] 生成请求标识 pair_id=%s card=%s", pair_id, card_id)

    # b. 先取历史（此时不含本条）
    prior = await session_store.read_session(work_id, card_id)
    state.prior = prior

    # c. 追加用户消息
    # [T10-3-3] 后端二次判定（最终仲裁，3-5）：请求携带的 pair_id 已存在且其后无回复条
    # → 复用而不追加。判定口径只有这一条，不含「时间接近」类模糊条件（3-4）；
    # 前端漏判时后端拦住。
    # 〔代拍 R-048〕4.3 后端二次判定（改 routes.py:346）——此条由诸葛代拍，用户可事后推翻；
    # 依据＝用户 18:58 授权＋ 17:54 立的边界 ＋ 本改动只加判据、不删任何既有行为。
    # 此标注须原样保留，不得抹去（红线 10）。
    reuse_client_pair = False
    if client_pair_id:
        for idx in range(len(prior) - 1, -1, -1):
            if prior[idx].get("pair_id") == client_pair_id:
                reuse_client_pair = not any(
                    m.get("role") == "assistant" for m in prior[idx + 1:]
                )
                break
    if reuse_client_pair:
        pair_id = client_pair_id
        state.pair_id = pair_id
        logger.info("[T10] 重发复用 pair_id=%s card=%s（用户条不重复落盘）", pair_id, card_id)
    else:
        await session_store.append_message(work_id, card_id, "user", content,
                                           pair_id=pair_id)
        logger.info("[T10] 用户消息落盘 pair_id=%s card=%s", pair_id, card_id)

    # [信息流 Task3] run_start：清该卡 journal 重放（A45）＋过程卡秒现（V1）。
    # 位置＝落盘之后、装配之前——先清后写，context 埋点才能留在 journal 里。
    event_stream.emit(work_id, card_id, "run_start",
                      {"pair_id": pair_id, "chars": len(content)})

    # 段②③前半：上下文组装＋工具列＋轮槽
    message_runner.build_context(state)

    # [信息流 Task1] 快车道分流：确定性动作（删卡/建卡/改卡/查询）单模型直答；
    # 未命中保守落全链（chain 现状逐字不变）。打点＝路由判定（A96）。
    state.mode = route(content)
    state.matched_rule = _route_rule(content)
    event_stream.emit(work_id, card_id, "fastlane_route",
                      {"mode": state.mode, "matched_rule": state.matched_rule})
    logger.info("[flow] 路由判定 mode=%s matched_rule=%s card=%s",
                state.mode, state.matched_rule, card_id)
    state.user_prompt = content

    # 段③ 工具循环：**单步推进**（每轮一个显式状态点）
    # [无闸改造 2026-10-02] 工具循环不设轮次封顶：模型停止调用工具即自然结束
    try:
        progress.set_stage(work_id, card_id, "started", "已提交")
        while True:
            outcome = await message_runner.step(state)
            if outcome.kind == "seats":
                outcome = await message_runner.run_seats(state)
            if outcome.kind != "continue":
                break
    except Exception as exc:
        message_runner.handle_error(state, exc)

    # 段④ prompt 日志（无论成败）
    message_runner.write_logs(state)

    if state.error is not None:
        return _err(502, f"模型调用失败: {state.error}")

    # 段⑤ T14 拼接交付（落位在日志写之后、parse_proposals 之前）
    delivered = message_runner.compose_delivery(state)
    state.final_reply = delivered

    # 段⑥ 建卡 · 工具通道 ＋ 文本通道并存，跨通道去重
    await message_runner.create_cards(state, delivered)

    # 段⑧ assistant 落盘＋回合末凝练触发＋done 埋点＋返回
    return await message_runner.finish(state)
