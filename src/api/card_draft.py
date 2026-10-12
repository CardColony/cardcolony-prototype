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

from ..storage import card_store, session_facade, shadow_store, work_store
from ..storage.hash_utils import apply_hash, parse_front_matter, split_body_sections
from . import model_service
from .routes import _apply_block_set

logger = logging.getLogger("api.card_draft")

# 合法 kind：requirements（第三块·需求文档/定论）| status（现状块）。使命不可经此口。
ALLOWED_KINDS = ("requirements", "status")

# 对话相关段截取上限（条）——draft 上下文用，防超长
_CONVO_LIMIT = 20
_CONVO_MSG_MAX = 400  # 每条消息截断字符数


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
    """[卡片编辑机制改造 2026-10-12] 盘上区头修正：rd_project 卡的第三块区头
    由「## 定论」改为「## 需求文档」。card_store 零触碰，故在 commit 写盘后
    直接改文件（只改区头行，不碰内容；重新 apply_hash 保证 hash 一致）。"""
    card_path = card_store._card_path(work_id, card_id)
    if not card_path.exists():
        return
    raw = card_path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(raw)
    # 只改第三块区头行（## 定论 → ## 需求文档），不动内容
    lines = body.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == "## 定论":
            lines[i] = line.replace("## 定论", "## 需求文档")
            break
    new_body = "".join(lines)
    # 重建全文：front matter + 新正文，重新 apply_hash
    fm_end = raw.find("\n---\n", 4)  # 跳过开头 --- 找下一个 ---
    if fm_end == -1:
        return
    fm_part = raw[: fm_end + 5]  # 含第二个 --- 及其换行
    new_raw = fm_part + new_body
    new_raw = apply_hash(new_raw)
    card_path.write_text(new_raw, encoding="utf-8")


def _get_current_block(result: dict, kind: str) -> str:
    """从 read_card 结果（已经 _apply_block_set）取当前 kind 对应块内容。"""
    body = result.get("body") or {}
    if kind == "requirements":
        # rd 卡：body.requirements；research 卡：body.rulings（第三块语义随树类走）
        return str(body.get("requirements") or body.get("rulings") or "")
    return str(body.get("status_block") or "")


def _kind_label(kind: str) -> str:
    return "需求文档" if kind == "requirements" else "现状"


def _build_prompts(
    kind: str,
    card: dict,
    current_block: str,
    convo: str,
    prev_draft: Optional[str],
    feedback: Optional[str],
) -> tuple[str, str]:
    """构造 draft 的 system/user prompt（纯文本，无工具）。"""
    label = _kind_label(kind)
    mission = (card.get("body") or {}).get("mission", "")
    status = (card.get("body") or {}).get("status_block", "")
    title = (card.get("meta") or {}).get("title", "")

    system = (
        "你是推演工作台的卡片内容起草助手。你的唯一职责：根据提供的上下文，"
        "起草/修订卡片的「{label}」块内容。你只输出该块的正文文本，不输出解释、"
        "不输出其他块、不调用任何工具、不修改使命块。\n"
        "要求：\n"
        "1. 输出即为该块完整正文（整块替换语义），保持原有格式风格。\n"
        "2. 使命块不可改——只起草 {label} 块本身。\n"
        "3. 若给了上一版草稿与用户反馈，按反馈修订，保持上下文连续。"
    ).format(label=label)

    parts = [f"卡片标题：{title}"]
    if mission.strip():
        parts.append(f"【使命块·只读参考】\n{mission.strip()}")
    if status.strip() and kind == "requirements":
        parts.append(f"【现状块·只读参考】\n{status.strip()}")
    parts.append(f"【当前{label}块内容】\n{current_block or '（空）'}")
    if convo.strip():
        parts.append(f"【本卡对话相关段】\n{convo.strip()}")
    if prev_draft is not None and prev_draft.strip():
        parts.append(f"【上一版草稿】\n{prev_draft.strip()}")
    if feedback is not None and feedback.strip():
        parts.append(f"【用户反馈·按此修订】\n{feedback.strip()}")
    parts.append(f"请起草{label}块完整正文：")

    return system, "\n\n".join(parts)


def _build_convo_context(blocks: list[dict]) -> str:
    """从 session 块截取对话相关段（user/assistant 正文，截断限量）。"""
    lines: list[str] = []
    for b in blocks[-_CONVO_LIMIT:]:
        role = str(b.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = str(b.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _CONVO_MSG_MAX:
            content = content[:_CONVO_MSG_MAX] + "…"
        prefix = "用户" if role == "user" else "助手"
        lines.append(f"{prefix}：{content}")
    return "\n".join(lines)


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

    # 本卡对话相关段（session 只读，不进 message_runner）
    try:
        convo_blocks = await session_facade.read(work_id, card_id)
    except Exception:  # noqa: BLE001
        convo_blocks = []
    convo = _build_convo_context(convo_blocks)

    system_prompt, user_prompt = _build_prompts(
        kind, card, current_block, convo, prev_draft, feedback,
    )
    # 独立路由、独立调用：不传工具（tools 缺省 None）、纯文本；绝不进 message_runner 工具循环
    try:
        result, _model = await model_service.call_main(system_prompt, user_prompt)
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

    # [卡片编辑机制改造 2026-10-12] rd_project 卡的第三块区头改名：
    # update_card 写盘恒用「## 定论」（_render_card 硬编码），此处改盘为
    # 「## 需求文档」——写侧真正产生该区头，NEW_SECTION_MAPPING 映射不再死代码。
    if kind == "requirements" and await _is_rd_project(work_id):
        try:
            await _rename_rulings_to_requirements(work_id, card_id)
        except Exception as exc:  # noqa: BLE001
            # 区头改名失败不阻断 commit 主链（内容已落盘，区头是次要修正）
            logger.warning("区头改名失败（不阻断）work=%s card=%s: %s",
                           work_id, card_id, exc)

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
