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

import logging
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..storage import (
    canvas_store, card_store, manuscript_store,
    session_store, work_store,
)
from ..storage.card_store import CardLockedError, CardNotFoundError
from ..storage.card_store import CardDeleteDeniedError
from ..storage.manuscript_store import ChapterNotFoundError
from . import card_proposal, card_tools, context_assembler, model_service, progress, prompt_log, prompts, simple_service, text_tool_rescue

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


@router.post("/works/{work_id}/cards/{card_id}/messages")
async def post_work_message(work_id: str, card_id: str, req: PostMessageRequest):
    """对话主链路（模块 H2）。"""
    if work_store.read_work(work_id) is None:
        return _err(404, f"剧本不存在: {work_id}")
    return await _run_message(work_id, card_id, req.content)


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
    return await _run_message(DEFAULT_WORK_ID, card_id, req.content)


# ============================================================
# messages 主链路（工具循环 + 建卡协议闭环）
# ============================================================

async def _run_message(work_id: str, card_id: str, content: str) -> Any:
    """完整 messages 链路，新旧路由共用。

    顺序：读卡 → 取历史 → 追加用户消息 → context_assembler 组装 →
    prompts.build_system_prompt → 带工具的工具循环 → parse_proposals →
    apply_proposals → 写 prompt 日志 → 追加 assistant 消息 → 返回。
    """
    # a. 确认卡片存在
    try:
        card = await card_store.read_card(work_id, card_id)
    except CardNotFoundError:
        return _err(404, f"卡片不存在: {card_id}")
    except CardLockedError:
        return _err(423, "卡片被占用，请稍后重试")

    # b. 先取历史（此时不含本条）
    prior = await session_store.read_session(work_id, card_id)

    # c. 追加用户消息
    await session_store.append_message(work_id, card_id, "user", content)

    # d. 上下文组装（白板版 2026-10-02）：中性卡片清单＋当前卡。
    # 无目标锚/血统链/管辖域/契约/charter/空位提示注入（全量审计定案：结构即引导）。
    neutral_context = context_assembler.build_neutral_context(work_id, card_id)
    system_prompt = prompts.build_system_prompt(neutral_context)
    vp_system_prompt = prompts.build_viewpoint_system_prompt(neutral_context)

    # e. 工具循环
    prior_for_gateway = [{"role": m["role"], "content": m["content"]} for m in prior]
    tools = card_tools.build_tool_schemas()
    tool_events: list[dict[str, Any]] = []
    user_prompt = content
    rounds = 0
    final_reply: Optional[str] = None
    model_used: Optional[str] = None
    error: Optional[str] = None
    viewpoints_meta: list[dict[str, Any]] = []  # [phase9-E] 观点元信息（含内部 text）

    try:
        # [无闸改造 2026-10-02] 工具循环不设轮次封顶：模型停止调用工具即自然结束
        progress.set_stage(work_id, card_id, "started", "已提交")
        while True:
            rounds += 1
            if rounds == 1:
                # [phase9-E] 首轮（用户原始问题）走 simple 编排：观点并发 + 统合
                # [职责分离裁定] vp_system_prompt 只含中性上下文——三席只做分析
                result, model_used, viewpoints_meta = await simple_service.call_simple(
                    system_prompt,
                    vp_system_prompt,
                    user_prompt,
                    prior_for_gateway,
                    tools=tools,
                    on_progress=lambda s, d, _w=work_id, _c=card_id: progress.set_stage(_w, _c, s, d),
                )
            else:
                # 工具续跳轮不重复咨询观点模型
                progress.set_stage(work_id, card_id, "tools", f"第 {rounds - 1} 轮工具续跳")
                result, model_used = await model_service.call_main(
                    system_prompt,
                    user_prompt,
                    prior_for_gateway,
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
                elif unresolved:
                    text_rescue_note = text_tool_rescue.feedback_note()

            if not tool_calls:
                final_reply = assistant_content
                if text_rescue_note:
                    # 反馈兜底：疑似调用未被识别这个事实必须回到模型上下文，
                    # 否则模型永远在等一个不会来的结果（等待黑洞）。
                    prior_for_gateway.append({
                        "role": "assistant", "content": final_reply,
                    })
                    user_prompt = text_rescue_note
                    continue
                break

            # [phase8.2] 第一次进入工具轮时，把用户原文固化进 prior，
            # 否则后续跳的 user_prompt 被替换后原文会从对话中消失
            if rounds == 1:
                prior_for_gateway.append({"role": "user", "content": content})

            # 把 assistant 消息（含 tool_calls）+ tool 结果追加进 prior
            prior_for_gateway.append({
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

            # 收集卡片 meta 用于权限判定
            cards_meta = card_tools._collect_card_meta(work_id)

            for i, tc in enumerate(tool_calls):
                name = tc.get("name", "")
                args = tc.get("arguments", {}) or {}
                # [phase8.5] manuscript 工具没有 card_id：target_id 记 chapter 或 keyword，都没有则空串
                # [phase8.6-D] link_cards 记 target_id
                # [联网能力 2026-10-02] web_search 记 query、fetch_web 记 url——审计可见
                target_id = (args.get("card_id") or args.get("target_id")
                             or args.get("chapter") or args.get("keyword")
                             or args.get("query") or args.get("url") or "")
                tool_result = await card_tools.execute_tool(work_id, card_id, name, args, cards_meta)
                ok = "error" not in tool_result
                tool_events.append({"tool": name, "card_id": target_id, "ok": ok})
                prior_for_gateway.append({
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": __import__("json").dumps(tool_result, ensure_ascii=False),
                })

            user_prompt = "工具结果已返回。"
    except Exception as exc:
        error = str(exc)
        progress.set_stage(work_id, card_id, "error", str(exc)[:120])
        logger.error("模型调用/工具循环失败 card=%s: %s", card_id, exc)

    # f. 写 prompt 日志（无论成败）
    log_path = prompt_log.write_prompt_log(
        card_id=card_id,
        system_prompt=system_prompt,
        prior_messages=prior_for_gateway,
        user_prompt=content,
        model_used=model_used if error is None else None,
        response=final_reply if error is None else None,
        error=error,
        tool_events=tool_events,
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

    # g. 解析建卡协议 + 自动建卡
    progress.set_stage(work_id, card_id, "cards", "解析建卡提案")
    clean_text, proposals = card_proposal.parse_proposals(final_reply or "")
    created_cards = await card_proposal.apply_proposals(work_id, card_id, proposals)

    # h. 追加 assistant 消息（存 clean_text，剥离协议块）
    await session_store.append_message(work_id, card_id, "assistant", clean_text, model=model_used)
    progress.set_stage(work_id, card_id, "done", "完成")

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
