"""依赖变更通知（底层能力，开关后置）。

用户 2026-10-27：「你先把【能实现自动通知】的底层能力具备了，要不要自动通知，
后续都可以开关。」

## 做什么

某张卡的正文块（使命／现状／需求文档）真的变了 ⇒ 查 `card_relations` 里
**谁依赖它** ⇒ 给那些卡各发一条**同步**（落上下文、不唤醒）。

## 为什么能防自激

通知走 `append_card_message`——它只往会话块写，**不碰卡面**，
所以不会触发 `update_card`，也就不会形成「A 改→通知 B→B 改→通知 A」的环。

## 开关

`config/edge_notify.json` 的 `dependency_change_notify`，**默认关**。
关着的时候本模块什么都不做（连查库都不查）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config" / "edge_notify.json"

_BLOCK_ZH = {"mission": "使命", "status_block": "现状", "requirements": "需求文档"}


def enabled() -> bool:
    """依赖变更通知是否开启。读不到配置 ⇒ 关（fail-closed，不乱发消息）。"""
    try:
        d = json.loads(_CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return bool(d.get("dependency_change_notify"))


async def dependents_of(work_id: str, card_id: str) -> list[dict[str, Any]]:
    """谁依赖我（含它们声明的 kind/detail）。底层能力之一。"""
    from ..storage import card_db

    try:
        return await card_db.run_read(
            work_id,
            lambda conn: card_db.dependents_detail(conn, card_id), []) or []
    except Exception as exc:  # noqa: BLE001 查不到就当没人依赖
        logger.warning("[edge] 查依赖方失败 card=%s: %s", card_id, exc)
        return []


async def notify_dependents(work_id: str, card_id: str,
                            changed_blocks: list[str]) -> list[str]:
    """给依赖方各发一条同步。返回实际通知到的卡号。"""
    if not enabled() or not changed_blocks:
        return []
    from ..storage import session_facade

    deps = await dependents_of(work_id, card_id)
    if not deps:
        return []
    blocks_zh = "、".join(_BLOCK_ZH.get(b, b) for b in changed_blocks)
    sent: list[str] = []
    for d in deps:
        target = str(d.get("from_card") or "")
        if not target or target == card_id:
            continue
        detail = str(d.get("detail") or "").strip()
        text = (
            f"【依赖变更】{card_id} 的**{blocks_zh}**变了。\n"
            f"你在依赖线里声明过：{detail or '（未写清依赖什么）'}\n"
            f"迭代前请确认：你依赖的那部分是否受影响；受影响就同步改，"
            f"并通知你的下游。"
        )
        try:
            await session_facade.append_card_message(
                work_id, target, from_card=card_id, text=text,
                msg_type="msg_sync")
            sent.append(target)
        except Exception as exc:  # noqa: BLE001 单个失败不挡其余
            logger.warning("[edge] 依赖通知失败 to=%s: %s", target, exc)
    return sent
