# -*- coding: utf-8 -*-
"""卡间消息落成的会话块必须**三尺同尺**：进上下文、进触发计量、进凝练。

[2026-10-26 用户需求] 消息模型改成「同步/通知直接落会话块」之后，用户明确要求：
「以上提到的进入上下文的消息，确保也能进入凝练线结算」。

判据链（三者同一把尺 `predicates.enters_context` / `is_condense_original_event`）：
  1. 落成 `actor=human` 的原文块 —— 不是 tool 块、不是 plugin 注入；
  2. `enters_context()` 为真 —— 计入上下文（`project_history_messages`）；
  3. `is_condense_original_event()` 为真 —— 计入触发线与窗口预算，并被凝练；
  4. 系统提醒（from=system）同样满足 1~3，且口吻不以某张卡的名义说话。
"""
from __future__ import annotations

import pytest

from src.storage import memory_store as M, predicates, session_facade


@pytest.mark.asyncio
async def test_card_message_block_enters_context_and_condense(sandbox, work):
    wid, cid = work["work_id"], work["work_card_id"]
    blk = await session_facade.append_card_message(
        wid, cid, from_card="card_099", text="【补件】请核对第三条", msg_type="msg_sync")

    # [2026-10-27 · 用户裁定「唤醒以用户消息包装、同步只出现在历史」]
    # 消息块用 `actor=card:<发信卡>`（不冒充人声），**进凝练靠
    # `predicates.MESSAGE_TYPES` 收 msg_***，不再靠 actor=human。
    assert str(blk["actor"]).startswith("card:"), blk["actor"]
    from src.storage import predicates
    assert predicates.is_condense_original_event(blk), "消息块必须进凝练"
    assert predicates.enters_context(blk) is True
    assert predicates.is_condense_original_event(blk) is True
    assert M._msg_meter_tokens(blk) > 0, "必须计入触发线/窗口的 token 计量"
    assert "给你发来一条" in blk["content"]


@pytest.mark.asyncio
async def test_system_notice_block_also_counted(sandbox, work):
    wid, cid = work["work_id"], work["work_card_id"]
    blk = await session_facade.append_card_message(
        wid, cid, from_card="system", text="该汇报了", msg_type="msg_notify")

    assert str(blk["actor"]).startswith("card:"), blk["actor"]
    from src.storage import predicates
    assert predicates.is_condense_original_event(blk), "系统消息块也必须进凝练"
    assert predicates.enters_context(blk) is True
    assert predicates.is_condense_original_event(blk) is True
    assert "系统给你发来一条" in blk["content"]
    assert "卡 system" not in blk["content"], "系统提醒不能以某张卡的名义说话"


@pytest.mark.asyncio
async def test_card_messages_reach_context_projection(sandbox, work):
    """落块之后，历史投影里能看到它（不需要唤醒、不需要工具再读一次）。"""
    from src.api import context_regions

    wid, cid = work["work_id"], work["work_card_id"]
    await session_facade.append(wid, cid, {"role": "user", "content": "开始干活"})
    await session_facade.append_card_message(
        wid, cid, from_card="card_099", text="【同步】接口改了", msg_type="msg_sync")

    blocks = await session_facade.read(wid, cid)
    proj = context_regions.project_history_messages(blocks)
    texts = "\n".join(str(m.get("content") or "") for m in proj)
    assert "【同步】接口改了" in texts
