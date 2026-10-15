# -*- coding: utf-8 -*-
"""[卡片编辑机制改造 2026-10-12 · 块2] 起草 draft ＋ 确认写入 commit。

模型只起草（draft，无工具纯文本调用，不进 message_runner 工具循环）；
写入唯一入口＝commit（用户按钮确认后调用，hash 比对防并发覆盖）。

- draft：上下文 = 当前块现有内容（必带）+ 本卡对话相关段 + 上一版草稿 + 用户反馈。
  草稿只存内存/会话态、不落盘；返回 {draft, base_hash}。
- commit：base_hash 与卡内该块当前 hash 不一致 → 拒绝、卡面零改动；
  一致则 card_store.update_card 整块替换（requirements 经 rulings 键落盘、
  status 经 status_block 键）；随后 shadow_store.append_to_section 往
  ## 迭代历史 记一行（时间、按钮类型、旧/新块 hash、反馈轮数）。

使命块永远不经此口（kind 只允许 requirements/status）。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from ..storage import card_store, memory_store, session_facade, shadow_store, work_store
from ..storage.hash_utils import apply_hash, parse_front_matter, split_body_sections
from . import context_assembler, context_regions, model_config, model_service
from .routes import _apply_block_set

logger = logging.getLogger("api.card_draft")

# 合法 kind：requirements（第三块·需求文档/定论）| status（现状块）。
# 使命不可经此口——它不在本元组内，经此口一律被 draft/commit 开头那道
# ``kind not in ALLOWED_KINDS`` 拒掉（模型无写通道，只有用户能在界面改）。
ALLOWED_KINDS = ("requirements", "status")


def _block_hash(content: str) -> str:
    """块内容指纹：sha256 hex（截 16 位展示用；比对用全长）。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


async def _is_rd_project(work_id: str) -> bool:
    """判断 work 是否 rd_project 树（根卡 project_class 判据）。"""
    try:
        return card_store._root_project_class(work_id) == "rd_project"
    except Exception:  # noqa: BLE001
        return False


async def _rename_rulings_to_requirements(work_id: str, card_id: str) -> None:
    """[已停用 · 卡片存储迁移 SQLite 2026-10-14] 原为「盘上区头修正」：
    rd_project 卡的第三块区头由「## 定论」改为「## 需求文档」。

    库化后第三块是**一列**（``cards.rulings``），「叫什么名」由读侧按本树
    ``project_class`` 决定（``routes._apply_block_set``），盘上不再有区头可改
    ——本函数随之停用。**清理步删除**（见任务书 §七 第三步）。
    """
    return None


# 占位符标记：卡正文三块模板的字面占位说明（真实内容绝不会出现这些字）。
# draft/commit 命中占位符＝当空——模型从零起草，不照抄模板示例（D01【裁定】…）。
_PLACEHOLDER_MARKERS = (
    "（本卡作用", "（当前状况快照", "（对本卡相关事项",
    "（日期·依据）", "（日期·死过什么）", "（暂无记录）",
)


def _is_placeholder(text: str) -> bool:
    """是否占位符（模板示例）或空。命中＝当前块视为未写。"""
    t = (text or "").strip()
    if not t:
        return True
    return any(m in t for m in _PLACEHOLDER_MARKERS)


def _get_current_block(result: dict, kind: str) -> str:
    """从 read_card 结果（已经 _apply_block_set）取当前 kind 对应块内容。
    占位符当空——draft 从零起草、commit 以空块为比对基准（两侧同口径，hash 一致）。"""
    body = result.get("body") or {}
    if kind == "requirements":
        raw = str(body.get("requirements") or body.get("rulings") or "")
    else:
        raw = str(body.get("status_block") or "")
    return "" if _is_placeholder(raw) else raw


def _kind_label(kind: str) -> str:
    return "需求文档" if kind == "requirements" else "现状"


def _build_prompts(
    kind: str,
    card: dict,
    current_block: str,
    context_ctx: context_regions.RegionCtx,
    prior: list[dict[str, Any]],
    prev_draft: Optional[str],
    feedback: Optional[str],
) -> dict[str, Any]:
    """构造起草调用帧：区块上下文、对话历史、user 草稿材料均由统一底座组合。"""
    label = _kind_label(kind)
    mission = (card.get("body") or {}).get("mission", "")
    title = (card.get("meta") or {}).get("title", "")

    system_prefix = (
        "你是推演工作台的卡片内容起草助手。你的唯一职责：根据下方卡片清单、当前卡、"
        "记忆与对话历史，起草/修订卡片的「{label}」块内容。你只输出该块的正文文本，"
        "不输出解释、不输出其他块、不调用任何工具、不修改使命块。\n"
        "要求：\n"
        "1. 输出即为该块完整正文（整块替换语义）。\n"
        "2. 使命块不可改——只起草 {label} 块本身。\n"
        "3. 若给了上一版草稿与用户反馈，按反馈修订，保持上下文连续。"
    ).format(label=label)

    parts = [f"卡片标题：{title}"]
    if mission.strip():
        parts.append(f"【使命块·只读参考】\n{mission.strip()}")
    parts.append(f"【当前{label}块内容】\n{current_block or '（空，从零起草）'}")
    if prev_draft is not None and prev_draft.strip():
        parts.append(f"【上一版草稿】\n{prev_draft.strip()}")
    if feedback is not None and feedback.strip():
        parts.append(f"【用户反馈·按此修订】\n{feedback.strip()}")
    parts.append(f"请起草{label}块完整正文：")

    return context_regions.compose_model_context(
        context_ctx, profile="draft", prior_messages=prior,
        prior_key="history", user_content="\n\n".join(parts),
        system_prefix=system_prefix,
    )


async def draft(
    work_id: str,
    card_id: str,
    kind: str,
    feedback: Optional[str] = None,
    prev_draft: Optional[str] = None,
) -> dict[str, Any]:
    """起草：无工具纯文本调用主模型，返回 {draft, base_hash}。不落盘。"""
    if kind not in ALLOWED_KINDS:
        return {"error": f"kind 非法：{kind}，只允许 {list(ALLOWED_KINDS)}"}
    if work_store.read_work(work_id) is None:
        return {"error": f"剧本不存在: {work_id}"}
    try:
        raw_card = await card_store.read_card(work_id, card_id)
    except card_store.CardNotFoundError:
        return {"error": f"卡片不存在: {card_id}"}
    except card_store.CardLockedError:
        return {"error": "卡片被占用，请稍后重试"}

    card = _apply_block_set(raw_card, work_id)
    current_block = _get_current_block(card, kind)
    base_hash = _block_hash(current_block)

    # 六区中性上下文（复用主对话装配：卡片清单＋当前卡＋记忆 condense_fold；零截断）
    condense_state = (
        memory_store.load_memory(work_id, card_id)
        if memory_store.condense_enabled() else None
    )
    # 完整对话历史（不截断；投影由 context base 统一处理，卡片原始块不改）。
    try:
        blocks = await session_facade.read(work_id, card_id)
    except Exception:  # noqa: BLE001
        blocks = []
    prior = context_regions.project_history_messages(
        blocks, allowed_roles={"user", "assistant"})

    # 区块上下文从统一注册表取；起草助手的任务说明作为 system_prefix 注入同一帧。
    context_ctx = context_assembler.build_ctx(
        work_id, card_id, memory_state=condense_state)
    request = _build_prompts(
        kind, card, current_block, context_ctx, prior, prev_draft, feedback,
    )
    # 独立路由、独立调用：不传工具（tools 缺省 None）、纯文本；绝不进 message_runner 工具循环。
    # system/user/prior 已由统一 context base 编排。
    try:
        result, _model = await model_service.call_main(
            request["system_prompt"], request["user_prompt"],
            prior_messages=request["prior_messages"] or None,
            model_name=model_config.resolve("draft") or model_config.default_model(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft 模型调用失败 work=%s card=%s: %s", work_id, card_id, exc)
        return {"error": f"模型调用失败: {exc}"}

    draft_text = str(result.get("content") or "")
    return {"draft": draft_text, "base_hash": base_hash, "kind": kind}


async def commit(
    work_id: str,
    card_id: str,
    kind: str,
    draft: str,
    base_hash: str,
    feedback_rounds: int = 0,
) -> dict[str, Any]:
    """确认写入：hash 比对 → update_card 整块替换 → 影子 ## 迭代历史 记一行。"""
    if kind not in ALLOWED_KINDS:
        return {"error": f"kind 非法：{kind}，只允许 {list(ALLOWED_KINDS)}（使命块不可经此口）"}
    if work_store.read_work(work_id) is None:
        return {"error": f"剧本不存在: {work_id}"}
    try:
        raw_card = await card_store.read_card(work_id, card_id)
    except card_store.CardNotFoundError:
        return {"error": f"卡片不存在: {card_id}"}
    except card_store.CardLockedError:
        return {"error": "卡片被占用，请稍后重试"}

    card = _apply_block_set(raw_card, work_id)
    current_block = _get_current_block(card, kind)
    current_hash = _block_hash(current_block)

    # hash 比对：不一致 → 拒绝、卡面零改动
    if base_hash != current_hash:
        return {
            "error": "内容已被改，请重新起草",
            "code": "hash_mismatch",
            "current_hash": current_hash,
        }

    old_hash = current_hash
    new_hash = _block_hash(draft)
    now = _now_iso()
    kind_label = _kind_label(kind)

    # 整块替换：requirements 经 rulings 键落盘（第三块存储键）、status 经 status_block。
    # card_store.update_card 签名不变；使命键不在 updates 内。
    if kind == "requirements":
        updates = {"rulings": draft}
    else:
        updates = {"status_block": draft}
    history_note = f"用户确认写入{kind_label}块（draft→commit）"
    result = await card_store.update_card(work_id, card_id, updates, history_note)
    if "error" in result:
        return result

    # [卡片存储迁移 SQLite 2026-10-14] rd_project 卡的第三块区头改名补丁**停用**：
    # 库内第三块是一列，「叫什么名」由读侧按本树 project_class 决定
    # （routes._apply_block_set），不再需要改盘。原 _rename_rulings_to_requirements
    # 保留为停用占位，清理步删除。

    # 影子 ## 迭代历史 记一行（系统自动、不让模型写；复用 append_to_section）
    shadow_line = (
        f"- [{now}] {kind_label}确认 旧块hash={old_hash[:16]} "
        f"新块hash={new_hash[:16]} 反馈轮数={feedback_rounds}"
    )
    try:
        await shadow_store.append_to_section(
            work_id, card_id, "## 迭代历史", [shadow_line],
        )
    except Exception as exc:  # noqa: BLE001
        # 影子失败不阻断 commit 主链（append_to_section 本身永不抛，双保险）
        logger.warning("影子迭代历史记失败（不阻断）work=%s card=%s: %s",
                       work_id, card_id, exc)

    return {
        "ok": True,
        "card_id": card_id,
        "kind": kind,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "feedback_rounds": feedback_rounds,
    }
