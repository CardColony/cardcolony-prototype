# -*- coding: utf-8 -*-
"""用户发言必须进评审现场（2026-10-31 用户令）。

用户原话：「我说话，它们看不见，那我为什么要说话」

venue 架构下场会话＝评审卡自己的会话：`_prior_blocks` 把**每席最近一次
发言＋全部用户发言**进席位历史——用户发言是 actor=human 的普通块（角色
映射成 user），席位发言带 `from=席位` 归属标记；工具块/信封不进。
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
    said = "第一阶段只验基线，不要拿后续阶段的目标来评"

    # 用户发言：普通 human 块（真实形态）
    await session_facade.append(wid, rid, {"actor": "human", "content": said})

    prior = await review_runner._prior_blocks(wid, rid)
    joined = "\n".join(str(m.get("content") or "") for m in prior)
    assert said in joined, "用户说的话必须进评审现场（改前被批次标记过滤掉了）"
    # 角色要映射成 user
    assert any(m["role"] == "user" and said in str(m.get("content") or "")
               for m in prior), "用户发言的角色必须是 user"


async def test_old_batch_speech_stays_in_the_archive(sandbox):
    """[2026-10-31 用户令] 上一批次的发言**归档不删**，但新批次的 prior
    里没有它——隔离靠会话文件物理分，不靠标记过滤。"""
    wid, cid, fids, rid = await _setup()
    old_batch = str((await review_store.state_of(wid, rid)).get("batch") or "")
    old_said = "上一批次的老发言"

    await session_facade.append(wid, rid, {
        "actor": "assistant", "from": fids[0], "content": old_said})

    # 开新批次：旧块归档、场会话空白
    await review_store.open_batch(wid, rid, "新焦点",
                                  members=fids, target=cid, brief="新")

    prior = await review_runner._prior_blocks(wid, rid)
    joined = "\n".join(str(m.get("content") or "") for m in prior)
    assert old_said not in joined, "上一批次发言不进新批次的现场"
    arch = await session_facade.read(wid, f"{rid}-{old_batch}") or []
    assert any(old_said in str(b.get("content") or "") for b in arch), \
        "但归档里必须有它——是搬走了，不是删了"


async def test_seat_prior_keeps_only_latest_speech_per_seat(sandbox):
    """[2026-11 用户令] 场内过程**顶多上一轮三席观点**：

    每席只留最近一次发言（新一轮开场时正好＝上一轮各席观点）；
    工具回执、工具调用信封不进席位上下文；用户发言仍然全留。"""
    wid, cid, fids, rid = await _setup()

    for rnd in range(2):                       # 每席说两轮
        for f in fids:
            await session_facade.append(wid, rid, {
                "actor": "assistant", "from": f,
                "content": f"{f} 第{rnd}轮的意见"})
    await session_facade.append(
        wid, rid, {"actor": "tool",
                   "content": '{"verdict":"reject","ok":true}'})
    await session_facade.append(
        wid, rid, {"actor": "assistant",
                   "content": '{"tool_calls":[{"name":"read_card"}]}'})
    await session_facade.append(
        wid, rid, {"actor": "human", "content": "人话要留住"})

    prior = await review_runner._prior_blocks(wid, rid)
    texts = [str(m.get("content") or "") for m in prior]

    assert any("第1轮的意见" in t for t in texts), "每席最近一次发言要留"
    assert not any("第0轮的意见" in t for t in texts), \
        "同席更早的发言不再进（顶多上一轮观点）"
    assert not any("verdict" in t or "tool_calls" in t for t in texts), \
        "工具回执/工具信封不进席位上下文"
    assert any("人话要留住" in t for t in texts), "用户发言仍然全留"
    # 席位发言带归属标记——三席观点要能分清是谁说的
    assert any(t.startswith(f"—— {fids[0]} ——") for t in texts)


async def test_tool_face_is_always_complete(sandbox, monkeypatch):
    """工具面永远完整，不做任何收敛（用户：模型不可能无限查下去）。"""
    from src.api import model_service
    wid, cid, fids, rid = await _setup()
    seen = []

    async def fake(**kw):
        seen.append([str((t.get("function") or t).get("name") or "")
                     for t in (kw.get("tools") or [])])
        n = len(seen)
        if n <= 3:                            # 连查三次
            return ({"content": "", "tool_calls": [
                {"name": "read_card", "arguments": {"card_id": cid}}]}, "fake")
        if n == 4:
            return ({"content": "查完了，不通过。", "tool_calls": [
                {"name": "reject", "arguments": {"review_card_id": rid}}]},
                "fake")
        return ({"content": "查完了，不通过。", "tool_calls": []}, "fake")

    monkeypatch.setattr(model_service, "call_main", fake)
    st = await review_store.state_of(wid, rid)
    await review_runner._seat_turn(wid, rid, fids[0], st, kind="speak")

    # reject 执行后普通工具循环还要一轮收尾 ⇒ ≥4 轮；钉的是每轮面一致。
    assert len(seen) >= 4, f"应该一直能查下去，实际只有 {len(seen)} 轮"
    for i, names in enumerate(seen, 1):
        assert len(names) == len(seen[0]), f"第 {i} 轮工具面被收敛了：{names}"
        assert "read_file" in names and "read_card" in names, \
            f"第 {i} 轮缺查阅工具：{names}"
        assert "approve" in names and "reject" in names, \
            f"第 {i} 轮缺表态工具：{names}"
