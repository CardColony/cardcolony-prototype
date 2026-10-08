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
from . import card_proposal, card_tools, context_assembler, context_regions, event_stream, model_service, progress, prompt_log, prompts, simple_service, text_tool_rescue, tool_fold

logger = logging.getLogger("api.routes")

router = APIRouter(prefix="/api")

DEFAULT_WORK_ID = "work_001"  # 兼容层固定转发目标

ALLOWED_UPDATE_KEYS = {
    "title", "tags", "relations", "session_ref",
    "conclusion", "rejections",  # 旧
    "card_type", "parent", "charter",  # Phase 7 新增
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
    """_run_message 的实际链路体（外层只负责逐字流 contextvar 的设/还原）。"""
    # a. 确认卡片存在
    try:
        card = await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")

    # a0. [T10-1-1] 读取历史之前生成本次请求标识（UUID4 十六进制串），打点含标识与卡号
    pair_id = uuid.uuid4().hex
    logger.info("[T10] 生成请求标识 pair_id=%s card=%s", pair_id, card_id)

    # b. 先取历史（此时不含本条）
    prior = await session_store.read_session(work_id, card_id)

    # c. 追加用户消息
    # [T10-3-3] 后端二次判定（最终仲裁，3-5）：请求携带的 pair_id 已存在且其后无回复条
    # → 复用而不追加。判定口径只有这一条，不含「时间接近」类模糊条件（3-4）；
    # 前端漏判时后端拦住。
    # 〔代拍 R-048〕4.3 后端二次判定（改 routes.py:346）——此条由诸葛代拍，用户可事后推翻；
    # 依据＝用户 18:58 授权 ＋ 17:54 立的边界 ＋ 本改动只加判据、不删任何既有行为。
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
        logger.info("[T10] 重发复用 pair_id=%s card=%s（用户条不重复落盘）", pair_id, card_id)
    else:
        await session_store.append_message(work_id, card_id, "user", content,
                                           pair_id=pair_id)
        logger.info("[T10] 用户消息落盘 pair_id=%s card=%s", pair_id, card_id)

    # [信息流 Task3] run_start：清该卡 journal 重放（A45）＋过程卡秒现（V1）。
    # 位置＝落盘之后、装配之前——先清后写，context 埋点才能留在 journal 里。
    event_stream.emit(work_id, card_id, "run_start", {"pair_id": pair_id, "chars": len(content)})

    # d. 上下文组装（T11 分区装配线）：0 使用说明（BASE_PROMPT 指针）＋100 卡片清单＋150 当前卡。
    # 装配只按 order 收区拼接、零内容判断（总表 #2/#3）；vp_system_prompt＝100＋150（职责分离裁定）。
    # 无目标锚/血统链/管辖域/契约/charter/空位提示注入（全量审计定案：结构即引导）。
    # [T12-SP-05] 凝练态（水位 mark＋entries）routes 层一次读取、结果传 200/300 区——
    # context_regions 保持纯函数不做 IO（L17 硬约束）；开关关（RD-08 缺省）＝None＝逐字旧路。
    # 300 区收窄为未凝段的切片属三期路线裁定后补（L387 本批不动，mark 读取基础件先落）。
    condense_state = memory_store.load_memory(work_id, card_id) if memory_store.condense_enabled() else None
    ctx_regions = context_assembler.build_context_regions(work_id, card_id, memory_state=condense_state)
    neutral_context = context_regions.assemble_regions([r["text"] for r in ctx_regions])
    system_prompt = prompts.build_system_prompt(neutral_context)
    vp_system_prompt = prompts.build_viewpoint_system_prompt(neutral_context)
    # [信息流 Task3] 装配段埋点：context 秒现（V1）
    event_stream.emit(work_id, card_id, "context", {
        "regions": [str(r.get("region", "")) for r in ctx_regions],
        "bytes": len(neutral_context.encode("utf-8")),
    })

    # e. 工具循环
    # [T15-6] 300 区收窄为未凝段（路线甲，CONDENSE_ROUTE_RULINGS 2026-10-05 闸开）：
    # 开关开→按水位 mark 切片（condense_state 上方一次读取——T12 SP-05 同一读取传
    # 200/300 区），mark 前已凝段由 200 区记忆代表（原文全量在会话文件，一条不删）；
    # 开关关（缺省）＝condense_state None→全量投影逐字旧路（V1/V5）。mark 越界防御
    # 回退全量（与会话追加式存储的半开区间口径一致）。
    if condense_state is not None:
        _condense_mark = int(condense_state.get("mark", 0) or 0)
        _hist = prior[_condense_mark:] if 0 <= _condense_mark <= len(prior) else prior
    else:
        _hist = prior
    prior_for_gateway = [{"role": m["role"], "content": m["content"]} for m in _hist]
    # [T11-T6] 轮槽（请求局部对象）：包住同一条列表。折叠关时装配逐字同旧
    # （wire_messages 原样返回本列表）；首轮固化与抢救回注走 append_plain
    # （非轮、永不折叠——不折红线），assistant＋tool 结果走 begin_round/
    # append_tool_result（成轮，可折单元）。
    history_len = len(prior_for_gateway)
    slot = tool_fold.ToolTurnSlot(prior_for_gateway)
    tools = card_tools.build_tool_schemas()
    tool_events: list[dict[str, Any]] = []
    # [E-探针制] 工具结果全文累积（口径④）：供三席后置注入，不折叠不截断
    tool_full_texts: list[str] = []
    # [E-探针制] 三席一次进场闸（口径①：只进场一次；统合官若续查证不重问三席）
    seats_done = False
    # [信息流 Task1] 快车道分流：确定性动作（删卡/建卡/改卡/查询）单模型直答；
    # 未命中保守落全链（chain 现状逐字不变）。打点＝路由判定（A96）。
    mode = route(content)
    matched_rule = _route_rule(content)
    event_stream.emit(work_id, card_id, "fastlane_route", {"mode": mode, "matched_rule": matched_rule})
    logger.info("[flow] 路由判定 mode=%s matched_rule=%s card=%s", mode, matched_rule, card_id)
    user_prompt = content
    rounds = 0
    model_calls = 0
    final_reply: Optional[str] = None
    # [T14-1-1] 带工具轮的分析正文采集（只读）：落盘型工作流的分析写在带工具轮
    # 的 assistant_content 里，末轮判定只取纯对话轮正文——分析结构性上不了屏。
    # 采集后与末轮回执拼接交付（分析在前、回执在后），不取代回执。
    analysis_parts: list[str] = []
    model_used: Optional[str] = None
    error: Optional[str] = None
    viewpoints_meta: list[dict[str, Any]] = []  # [phase9-E] 观点元信息（含内部 text）

    try:
        # [无闸改造 2026-10-02] 工具循环不设轮次封顶：模型停止调用工具即自然结束
        progress.set_stage(work_id, card_id, "started", "已提交")
        while True:
            rounds += 1
            if rounds == 1:
                # [E-探针制 2026-10-07] 首轮＝纯主模型带工具查证（原 call_simple
                # 观点前置编排退役；三席改在工具循环收尾处进场，见下方 break 分支）。
                progress.set_stage(work_id, card_id, "started", "主模型查证中")
                event_stream.emit(work_id, card_id, "round_start", {"round": rounds, "phase": "main"})
                model_calls += 1
                result, model_used = await model_service.call_main(
                    system_prompt,
                    user_prompt,
                    prior_for_gateway,
                    tools=tools,
                )
            else:
                # 工具续跳轮不重复咨询观点模型
                progress.set_stage(work_id, card_id, "tools", f"第 {rounds - 1} 轮工具续跳")
                # [T11] 装配时折叠：折叠关原样返回本列表（逐字同旧）；
                # 折叠开对窗口外旧轮次压成【结论】＋【依据】（不改条数与配对）
                wire_messages = await slot.wire_messages()
                event_stream.emit(work_id, card_id, "round_start", {"round": rounds, "phase": "tools"})
                model_calls += 1
                result, model_used = await model_service.call_main(
                    system_prompt,
                    user_prompt,
                    wire_messages,
                    tools=tools,
                )
            tool_calls = result.get("tool_calls") or []
            assistant_content = result.get("content") or ""

            # [等待黑洞修复 2026-10-02] 标准字段为空时，抢救正文中
            # 文本形态的工具调用（DeepSeek 原生标记泄漏，三次同族发作定案）。
            # 纯机械模式匹配：解析出的调用并入标准执行链（执行＋结果回注），
            # 模型收到真实反馈；疑似标记解析不出调用时，把「未识别未执行」
            # 这个机械事实回注下一轮——关闭等待黑洞。
            text_rescue_note = ""
            if not tool_calls and assistant_content:
                rescue_calls, unresolved, stripped = \
                    text_tool_rescue.extract_text_tool_calls(assistant_content)
                if rescue_calls:
                    tool_calls = rescue_calls
                    assistant_content = stripped or assistant_content
                    # stdlib logging 只吃位置参数（%s 占位）——本文件 logger 是
                    # logging.getLogger 而非 infra.logging.get_logger（structlog），
                    # kwargs 写法会 TypeError（2026-10-03 首次点火定案）。
                    logger.info(
                        "text_tool_calls_rescued count=%s names=%s",
                        len(rescue_calls),
                        [c["name"] for c in rescue_calls],
                    )
                    event_stream.emit(work_id, card_id, "rescue", {"count": len(rescue_calls)})
                elif unresolved:
                    text_rescue_note = text_tool_rescue.feedback_note()

            if not tool_calls:
                final_reply = assistant_content
                if text_rescue_note:
                    # 反馈兜底：疑似调用未被识别这个事实必须回到模型上下文，
                    # 否则模型永远在等一个不会来的结果（等待黑洞）。
                    # [T11] 经轮槽 append_plain 入槽（非轮消息，永不折叠）
                    slot.append_plain({
                        "role": "assistant", "content": final_reply,
                    })
                    user_prompt = text_rescue_note
                    continue
                # [信息流 Task1] 快车道＝单模型直答（A97）：跳三席与统合官，
                # 本轮即交付（chain 路径完全不受影响——mode=="chain" 不进此支）。
                if mode == "fastlane":
                    break
                # [E-探针制 2026-10-07] 三席后置进场（口径①：工具查证收尾后、交付前，
                # 全程恰一次）。判据锚 V1：本段位于工具执行（上方 for 循环与历史工具轮）
                # 之后——三席输入携带本回合全量工具结果（task 3 口径④全文不截断）。
                # 上下文回流铁则：观点只进统合官 user_prompt，不入槽/不入 session；
                # 三席 prior 取纯对话历史切片（不含本轮工具机制消息，且只读不回写）。
                if seats_done:
                    # [E] 三席已进场过（统合官续查证后的收尾轮）：本轮即交付回执
                    break
                seats_done = True
                # [信息流 Task3] 三席进场埋点＋每席起止转发（on_seat 缺省 None 语义不变）
                event_stream.emit(work_id, card_id, "seats_enter", {"tool_results": len(tool_full_texts)})
                seats_prior = prior_for_gateway[:history_len]
                ok_metas, viewpoints_meta = await simple_service.run_viewpoints(
                    vp_system_prompt,
                    simple_service.build_informed_prompt(content, tool_full_texts),
                    seats_prior,
                    on_progress=lambda s, d, _w=work_id, _c=card_id: progress.set_stage(_w, _c, s, d),
                    on_seat=lambda kind, meta, _w=work_id, _c=card_id: event_stream.emit(_w, _c, kind, meta),
                )
                if not ok_metas:
                    # [E-口径②] 全败留痕不阻断：主模型携工具结果独自交付（草稿即回执）
                    if viewpoints_meta:
                        logger.warning("[E] 三席观点全败（%d 席尝试），主模型独自交付 card=%s",
                                       len(viewpoints_meta), card_id)
                    break
                # 统合官最终统合交付（合同不动 RD-03：同 call_main、带 tools）
                progress.set_stage(work_id, card_id, "synthesize", "三席观点统合中")
                logger.info("[E] 三席后置进场 工具结果=%d条 成功席=%d card=%s",
                            len(tool_full_texts), len(ok_metas), card_id)
                synth_input = simple_service.build_viewpoint_prompt(content, ok_metas)
                event_stream.emit(work_id, card_id, "round_start", {"round": rounds, "phase": "synthesize"})
                model_calls += 1
                result, model_used = await model_service.call_main(
                    system_prompt,
                    synth_input,
                    prior_for_gateway,
                    tools=tools,
                )
                tool_calls = result.get("tool_calls") or []
                assistant_content = result.get("content") or ""
                if not tool_calls:
                    final_reply = assistant_content
                    break
                # 统合官仍需查证：清空草稿、借道下方工具执行段续跑（无闸 RD-01——
                # 不设任何轮次上限）；seats_done 保证三席不再有第二次。
                # 末轮草稿由统合稿取代（统合稿=交付回执）。
                final_reply = None

            # [phase8.2] 第一次进入工具轮时，把用户原文固化进 prior，
            # 否则后续跳的 user_prompt 被替换后原文会从对话中消失
            # [T11] 经轮槽 append_plain 入槽（首轮用户原文固化＝不折红线之一）
            if rounds == 1:
                slot.append_plain({"role": "user", "content": content})

            # 把 assistant 消息（含 tool_calls）+ tool 结果追加进 prior
            # [T11-T6] assistant 经 begin_round 入槽（成轮，可折单元）；
            # 消息构造与既有逐字一致（折叠关时槽即本列表，零变化）
            slot.begin_round({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": __import__("json").dumps(tc["arguments"], ensure_ascii=False)},
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            })
            # [T14-1-2] 只读采集：带工具轮的 assistant_content（含 L459 抢救处理后
            # 的正文）同步留一份进交付采集列表。不改 begin_round 入参、不额外入槽
            # （T11 轮槽消息条数与配对不变量不破坏，RD-09）；不限长不折叠不截断。
            if assistant_content:
                analysis_parts.append(assistant_content)

            # 收集卡片 meta 用于权限判定
            cards_meta = card_tools._collect_card_meta(work_id)

            for i, tc in enumerate(tool_calls):
                name = tc.get("name", "")
                args = tc.get("arguments", {}) or {}
                # [phase8.5] manuscript 工具没有 card_id：target_id 记 chapter 或 keyword，都没有则空串
                # [phase8.6-D] link_cards 记 target_id
                # [联网能力 2026-10-02] web_search 记 query、fetch_web 记 url——审计可见
                # [建卡工具化 2026-10-08] create_card 无 card_id/parent 入参（parent 可选），
                #   target_id 记 title（回执前唯一可定位的入参）
                target_id = (args.get("card_id") or args.get("target_id")
                             or args.get("chapter") or args.get("keyword")
                             or args.get("query") or args.get("url")
                             or (args.get("title") if name == "create_card" else "") or "")
                # [信息流 Task3] 工具执行前后埋点（V2 即时可见）
                event_stream.emit(work_id, card_id, "tool_call", {"tool": name, "target": target_id})
                tool_result = await card_tools.execute_tool(work_id, card_id, name, args, cards_meta)
                ok = "error" not in tool_result
                _ev: dict[str, Any] = {"tool": name, "card_id": target_id, "ok": ok}
                # [建卡工具化 2026-10-08] create_card 成功时把真身卡号/卡名/父带进
                # tool_events——交付段据此收拢工具通道建卡结果（前端绿条与文本通道同构）
                if name == "create_card" and ok:
                    _ev["card_id"] = tool_result.get("card_id", "")
                    _ev["title"] = tool_result.get("title", "")
                    _ev["parent"] = tool_result.get("parent", "")
                tool_events.append(_ev)
                # [信息流 Task4] 并表（A101）：快车道的卡操作落 card_op（op/op_result，
                # 一类卡操作只此一份事件）；chain 保持 tool_result。全日志打点（A96）。
                if mode == "fastlane" and name in ("create_card", "edit_card", "delete_card", "read_card"):
                    event_stream.emit(work_id, card_id, "card_op", {
                        "op": name, "target": target_id, "ok": ok,
                        "result": tool_result if ok else str(tool_result.get("error", ""))[:200],
                    })
                    logger.info("[flow] 卡操作落地 op=%s target=%s ok=%s card=%s", name, target_id, ok, card_id)
                else:
                    event_stream.emit(work_id, card_id, "tool_result", {"tool": name, "target": target_id, "ok": ok})
                # [T11-T6] tool 结果经 append_tool_result 入槽（归入当前轮）
                slot.append_tool_result({
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": __import__("json").dumps(tool_result, ensure_ascii=False),
                })
                # [E-探针制] 全文累积（口径④）：三席后置注入用，不折叠不截断
                tool_full_texts.append(
                    f"{name}(target={target_id}) → "
                    + __import__("json").dumps(tool_result, ensure_ascii=False)
                )

            user_prompt = "工具结果已返回。"
            # [信息流 Task2/打点 A92/A96] 同请求续跳：占位符只进模型不入用户流；
            # resume_kind=同请求续跳（新请求重入=0 才是预期）
            logger.info("[flow] 断点续跳 resume_kind=同请求续跳 no_user_visible=True round=%s card=%s",
                        rounds, card_id)
    except Exception as exc:
        error = str(exc)
        progress.set_stage(work_id, card_id, "error", str(exc)[:120])
        logger.error("模型调用/工具循环失败 card=%s: %s", card_id, exc)
        # [信息流 Task2·A93] 确须结束型（上下文超限等）：checkpoint 落 data_snapshot
        # 定格现状（works 快照批），下条消息从现状续走而非全链重跑。只读对接、零改动面。
        if rounds > 0:
            try:
                from ..storage import data_snapshot
                snap = data_snapshot.snapshot_sync(
                    reason=f"checkpoint:{type(exc).__name__}", work_id=work_id)
                logger.info("[flow] checkpoint 落位 type=%s work=%s batch=%s",
                            type(exc).__name__, work_id,
                            (snap or {}).get("dir", "") if isinstance(snap, dict) else str(snap)[:80])
            except Exception as snap_exc:  # noqa: BLE001 checkpoint 失败不阻断错误返回
                logger.warning("[flow] checkpoint 落位失败（不阻断）: %s", snap_exc)
        event_stream.emit(work_id, card_id, "error", {"error": error[:200], "rounds": rounds})

    # f. 写 prompt 日志（无论成败）
    # [T11-T7] regions＝六区构成（只存指针与指纹）；fold_ops＝每轮折叠
    # 原字节/折后字节/结果。两键非空才写入（约束 13）；prior_messages 仍落
    # 原文全量（轮槽不改写原列表，折叠账在 fold_ops 对账）。
    log_regions = context_regions.build_log_regions(
        ctx_regions,
        prior_for_gateway[:history_len],
        prior_for_gateway[history_len:],
        content,
    )
    log_path = prompt_log.write_prompt_log(
        card_id=card_id,
        system_prompt=system_prompt,
         prior_messages=prior_for_gateway,
         user_prompt=content,
         model_used=model_used if error is None else None,
         # [T14-3] response 保末轮供审计（拼接在日志写之后，此时 final_reply 仍是
         # 末轮正文——prompt_log.response 语义不动，RD-08）；交付全文（分析＋回执）
         # 可经会话 assistant 条/prompt 日志 prior_messages 复核。
         response=final_reply if error is None else None,
        error=error,
        tool_events=tool_events,
        pair_id=pair_id,  # [T10-5-3] 日志关联：从调用方接收配对标识
        regions=log_regions,
        fold_ops=slot.fold_ops or None,
    )
    # [phase9-E] 日志追加 viewpoints 字段（观点全文数组，落盘可查）。
    # [白板证据位 2026-10-02] vp_system_prompt 一并落日志：三思考席实际收到的
    # system prompt（应仅为中性卡片清单+当前卡，无使用说明/工具/建卡接口）。
    if log_path is not None:
        try:
            _json = __import__("json")
            payload = _json.loads(log_path.read_text(encoding="utf-8"))
            if viewpoints_meta:
                payload["viewpoints"] = [
                    {"model": m.get("model"), "ok": m.get("ok"),
                     "latency_ms": m.get("latency_ms"), "chars": m.get("chars"),
                     "error": m.get("error"), "text": m.get("text", "")}
                    for m in viewpoints_meta
                ]
            payload["vp_system_prompt"] = vp_system_prompt
            log_path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception as exc:
            logger.warning("prompt 日志 viewpoints 追加失败（不中断）: %s", exc)

    if error is not None:
        return _err(502, f"模型调用失败: {error}")

    # [T14-2] 拼接交付全文：分析在前、回执在后、\n\n 分隔（待裁定 1）。落位命门：
    # 在日志写（response 保末轮，RD-08）之后、parse_proposals 之前——解析/落盘/
    # 返回三处共用拼接后的 final_reply（任务 4）。V7 三边界：
    #   有分析有回执＝正常拼接；有分析无回执＝只交付中间轮；
    #   皆空＝空回复；无分析有回执＝纯对话轮原样（回归不变）。
    if analysis_parts:
        if final_reply:
            branch = "normal"
            delivered = "\n\n".join(analysis_parts + [final_reply])
        else:
            branch = "analysis-only"
            delivered = "\n\n".join(analysis_parts)
    elif final_reply:
        branch = "reply-only"
        delivered = final_reply
    else:
        branch = "empty"
        delivered = ""
    logger.info(
        "[T14] 分析交付拼接 branch=%s 分析段数=%s 拼接后字节=%s 末轮字节=%s",
        branch, len(analysis_parts),
        len(delivered.encode("utf-8")),
        len((final_reply or "").encode("utf-8")),
    )
    final_reply = delivered
    # [信息流 Task3] 交付拼接埋点（deliver 与 POST 一致性锚，V4）
    event_stream.emit(work_id, card_id, "deliver",
                      {"branch": branch, "bytes": len(delivered.encode("utf-8"))})

    # g. 建卡 · 工具通道 ＋ 文本通道并存，跨通道去重
    # [建卡工具化 2026-10-08] 建卡主通道＝标准工具 create_card（卡片带回执进
    # tool_events，模型能确证建成）；本段只做两件事：
    #   ① 收拢工具通道已建的卡（tool_events 里 create_card 且ok 的项）
    #   ② 文本协议块兜底（模型过渡期仍写 card-proposal）——按同轮工具通道
    #      已建的卡名去重，防同一轮建两次；并施加同一权限口径（只能建自己的子卡片）
    progress.set_stage(work_id, card_id, "cards", "建卡")
    created_cards: list[dict[str, Any]] = []
    for ev in tool_events:
        if ev.get("tool") == "create_card" and ev.get("ok"):
            created_cards.append({
                "card_id": ev.get("card_id", ""),
                "title": ev.get("title", ""),
                "parent": ev.get("parent", ""),
                "channel": "tool",
            })
    tool_titles = {c["title"] for c in created_cards if c.get("title")}
    denied_cards: list[dict[str, str]] = []

    # [T14-4-1/6] 文本通道扫描输入＝拼接后全文（含分析正文）——照既有机制运行
    # （RD-11 不新增筛选不设闸）；观察点日志使「扫描面是否含工具轮正文」可事后审计。
    logger.info(
        "[T14] 文本建卡扫描输入 分析段数=%s 交付分支=%s 扫描总字节=%s 工具通道已建=%d",
        len(analysis_parts),
        branch,
        len((final_reply or "").encode("utf-8")),
        len(created_cards),
    )
    clean_text, proposals = card_proposal.parse_proposals(final_reply or "")
    text_created, denied_cards = await card_proposal.apply_proposals(
        work_id, card_id, proposals, exclude_titles=tool_titles,
    )
    for c in text_created:
        created_cards.append({**c, "channel": "text"})
    if denied_cards:
        # 越权/去重的项如实回给模型上下文——不静默丢卡（模型能读到"没建成+为什么"）
        _d = "；".join(f"{d['title']}（{d['reason']}）" for d in denied_cards[:5])
        slot.append_plain({
            "role": "assistant",
            "content": f"[系统回执] 本轮有 {len(denied_cards)} 张卡未建成：{_d}。"
                       f"建卡只能建自己的子卡片（parent 须落在你的子树内，缺省＝当前卡自身）。",
        })
        logger.warning("[建卡未建] %d 项被拒 card=%s 明细=%s",
                       len(denied_cards), card_id, denied_cards)
    # [信息流 Task3] 建卡埋点（✚ 可见）
    event_stream.emit(work_id, card_id, "proposal", {
        "count": len(created_cards),
        "cards": [str(c.get("card_id", "")) for c in created_cards],
    })

    # h. 追加 assistant 消息（存 clean_text，剥离协议块）
    # [T10-1-4] 回复条携带与用户条逐字相同的 pair_id（V1）；打点含标识
    await session_store.append_message(work_id, card_id, "assistant", clean_text,
                                       model=model_used, pair_id=pair_id)
    logger.info("[T10] 回复落盘 pair_id=%s card=%s", pair_id, card_id)
    # [T12-SP-04] 回合末凝练触发（condense_fold）：assistant 落盘之后异步触发、
    # 不阻塞对话（异步是工程手段非闸，RD-04）。开关缺省关（RD-08）＝零动作；
    # 开时三期管线仍挂起待路线裁定，触发只量测＋留痕（memory_store.condense_turn_end）。
    memory_store.maybe_schedule_turn_end(work_id, card_id)
    progress.set_stage(work_id, card_id, "done", "完成")
    # [信息流 Task3/打点 A96] done 事件＋快车道调用数打点（lane/calls，快车道应为单模型）
    event_stream.emit(work_id, card_id, "done", {"rounds": rounds, "model_calls": model_calls})
    if mode == "fastlane":
        logger.info("[flow] 快车道调用 lane=fastlane calls=%s rounds=%s card=%s",
                    model_calls, rounds, card_id)

    return {
        "reply": clean_text,
        "model": model_used,
        "created_cards": created_cards,
        "tool_events": tool_events,
        # [phase9-E] 观点元信息（剔除内部 text，观点全文只落 prompt 日志，
        # 不进 session、不进 prior_messages——上下文回流铁则）
        "viewpoints_meta": [
            {k: v for k, v in m.items() if k != "text"} for m in viewpoints_meta
        ],
    }
