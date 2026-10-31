# -*- coding: utf-8 -*-
"""开新批次后，在途旧轮不许再写会话（2026-10-31 用户令）。

实测漏洞（work_027/card_124）：
  10:26:53 启动续跑给旧批次 rv_8ab5c6cadd 起了一个 run_once
  10:29:06 用户开新批次 rv_b8b85bbfc7 ⇒ open_batch 把会话清空了
  10:32:16 那条在途旧轮才跑完，把总裁发言**写回了刚清空的会话**
⇒ seq=0/1 是旧批次块，seq=2 才是新批次。
"""
from __future__ import annotations

import pytest

from src.api import model_service, review_runner
from src.storage import review_store, session_facade


async def _setup(n: int = 2):
    from tests.test_review_user_intervention import _setup as _base
    return await _base(n)



async def test_inflight_old_round_does_not_write_after_new_batch(sandbox, monkeypatch):
    """在途旧轮：开新批次发生在它跑的过程中 ⇒ 它的发言**不许**落会话。"""
    wid, cid, fids, rid = await _setup()
    old_st = await review_store.state_of(wid, rid)
    old_batch = str(old_st.get("batch") or "")
    speech = "旧批次的总裁发言，不该出现在新批次的会话里"

    async def mid_flight(sys_p, user_p, convo, tools, model):
        # 模拟：模型调用期间用户开了新批次
        await review_store.open_batch(wid, rid, "新焦点", members=fids,
                                      target=cid, brief="新背景")
        return ({"content": speech,
                 "tool_calls": [{"name": "reject",
                                 "arguments": {"review_card_id": rid}}]},
                "fake")

    monkeypatch.setattr(model_service, "call_main", mid_flight)
    await review_runner.speak(wid, rid, fids[0], old_st)

    blocks = await session_facade.read(wid, rid) or []
    texts = "\n".join(str(b.get("content") or "") for b in blocks)
    assert speech not in texts, "在途旧轮的发言被写回了新批次的会话（时差漏洞没堵住）"


async def test_new_batch_keeps_only_current_blocks(sandbox):
    """开新批次 ⇒ 会话只剩本批次的块（旧块被清）。"""
    wid, cid, fids, rid = await _setup()
    await session_facade.append(wid, rid, {"actor": "assistant",
                                           "content": "〔总裁 · rv_OLD〕旧内容"})
    await review_store.open_batch(wid, rid, "新焦点", members=fids, target=cid, brief="新")
    blocks = await session_facade.read(wid, rid) or []
    assert blocks == [], f"新批次开会后会话应清空，实际还有 {len(blocks)} 块"


