# -*- coding: utf-8 -*-
"""用户发言必须进评审现场（2026-10-31 用户令）。

用户原话：「我说话，它们看不见，那我为什么要说话」

改前：`_prior_messages` 只收首行带 `· rv_xxx〕` 批次标记的块；用户发言是普通
human 块，没有标记 ⇒ 被过滤 ⇒ 评审成员**从来没看见过用户说过什么**。

现在只按 `actor` 一个字段放行 human 块——不解释内容、不写状态、不碰卡面。
"""
from __future__ import annotations

from src.api import review_runner
from src.storage import review_store, session_facade


async def _setup(n: int = 2):
    from tests.test_review_user_intervention import _setup as _base
    return await _base(n)


async def test_human_message_reaches_review_context(sandbox):
    """用户在评审卡说的话，必须出现在后续评审轮的 prior 里。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)
    batch = str(st.get("batch") or "")
    said = "第一阶段只验基线，不要拿后续阶段的目标来评"

    # 用户发言：普通 human 块，**没有**批次标记（真实形态）
    await session_facade.append(wid, review_runner._sid(rid, str((await review_store.state_of(wid, rid)).get('batch') or '')), {"actor": "human", "content": said})

    prior = await review_runner._prior_messages(wid, rid, batch, cid)
    joined = "\n".join(str(m.get("content") or "") for m in prior)
    assert said in joined, "用户说的话必须进评审现场（改前被批次标记过滤掉了）"
    # 角色要映射成 user
    assert any(m["role"] == "user" and said in str(m.get("content") or "")
               for m in prior), "用户发言的角色必须是 user"


async def test_old_batch_member_speech_still_filtered(sandbox):
    """上一批次**成员**的发言仍然不进（只带它的正稿）。"""
    wid, cid, fids, rid = await _setup()
    st = await review_store.state_of(wid, rid)
    batch = str(st.get("batch") or "")
    await session_facade.append(wid, review_runner._sid(rid, str((await review_store.state_of(wid, rid)).get('batch') or '')), {
        "actor": "assistant", "content": "〔产品经理 · rv_OLDBATCH〕上一批次的老发言"})

    prior = await review_runner._prior_messages(wid, rid, batch, cid)
    joined = "\n".join(str(m.get("content") or "") for m in prior)
    assert "rv_OLDBATCH" not in joined, "上一批次成员发言不该带进来"


async def test_tool_face_is_always_complete(sandbox, monkeypatch):
    """工具面永远完整，不做任何收敛（用户：模型不可能无限查下去）。"""
    from src.api import model_service
    wid, cid, fids, rid = await _setup()
    seen = []

    async def fake(sys_p, user_p, convo, tools, model):
        seen.append([str((t.get("function") or t).get("name") or "") for t in (tools or [])])
        if len(seen) <= 3:                      # 连查三次
            return ({"content": "", "tool_calls": [
                {"name": "read_card", "arguments": {"card_id": cid}}]}, "fake")
        return ({"content": "查完了，不通过。", "tool_calls": [
            {"name": "reject", "arguments": {"review_card_id": rid}}]}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake)
    await review_runner.speak(wid, rid, fids[0])

    assert len(seen) == 4, f"应该一直能查下去，实际只有 {len(seen)} 轮"
    for i, names in enumerate(seen, 1):
        assert len(names) == len(seen[0]), f"第 {i} 轮工具面被收敛了：{names}"
        assert "read_file" in names and "read_card" in names, f"第 {i} 轮缺查阅工具"
