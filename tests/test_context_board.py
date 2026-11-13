# -*- coding: utf-8 -*-
"""上下文缓存板（context_board）施工验收测试（2026-11-12）。

判据：
- 闸：enters_context 为假的块（工具结果/调用壳/留痕）永不进板；
- 裁：mark 推进同事务把 seq<mark 的行裁掉（seq 比较，无下标错位可能）；
- 逐出：评审流量写时 keep-N；评审型卡全流按 seq 窗截尾；
- 等价：板投影 ≡ 旧四层筛投影（这是换路的硬判据）；
- 自愈：板空+原文有货 → 读侧就地重建；重建幂等；
- 读路：_render_prior_for 走板 + seqless 附加块接尾。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.api import context_regions
from src.storage import (
    card_db, context_board, memory_domain, origin_store,
    review_scope, session_facade,
)


async def _append(wid, cid, blocks):
    for b in blocks:
        await session_facade.append(wid, cid, b)


def _tx(wid, fn):
    return card_db._write_sync(wid, fn)  # noqa: SLF001 测试同包写事务


# ================================================================
# 闸：不该进板的块永远不进
# ================================================================

async def test_gate_blocks_filtered(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [
        {"role": "user", "content": "问"},
        {"actor": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"actor": "tool", "tool_call_id": "t1", "content": "结果"},
        {"actor": "assistant", "content": "留痕", "behavior": "plan"},
        {"role": "assistant", "content": "答"},
    ])
    rows = context_board.read_board(wid, cid)
    assert [(r["actor"], r["seq"]) for r in rows] == [
        ("human", 0), ("assistant", 4)], \
        f"工具/留痕块不得进板: {rows}"


# ================================================================
# 裁：mark 推进同事务裁板
# ================================================================

async def test_trim_on_mark_advance(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [
        {"role": "user", "content": f"u{i}"} for i in range(6)])
    assert context_board.board_count(wid, cid) == 6
    # save_chain 同事务裁板（mark=4 → seq<4 全裁）
    assert memory_domain.save_chain(wid, cid, [], condense_mark=4)
    rows = context_board.read_board(wid, cid)
    assert [r["seq"] for r in rows] == [4, 5], rows
    # mark 不前进不裁（None 不动板）
    memory_domain.save_chain(wid, cid, [], condense_mark=None)
    assert [r["seq"] for r in context_board.read_board(wid, cid)] == [4, 5]


# ================================================================
# 逐出：评审 keep-N / 评审卡 seq 窗
# ================================================================

async def test_review_keep_n_evict(sandbox, work, monkeypatch):
    wid, cid = work["work_id"], "card_001"
    monkeypatch.setattr(review_scope, "_keep_n", lambda: 2)
    monkeypatch.setattr(review_scope, "review_card_ids",
                        lambda w: frozenset({"card_117"}))
    blocks = [{"actor": "card:card_117", "role": "user",
               "content": f"派单{i}"} for i in range(5)]
    blocks.append({"role": "user", "content": "真人话"})
    await _append(wid, cid, blocks)
    rows = context_board.read_board(wid, cid)
    rev = [r for r in rows if r["is_review"]]
    assert len(rev) == 2, f"评审流量板上恒 ≤keep: {rows}"
    assert [r["seq"] for r in rev] == [3, 4]
    assert rows[-1]["actor"] == "human"


async def test_self_review_span(sandbox, work, monkeypatch):
    wid, cid = work["work_id"], "card_001"
    monkeypatch.setattr(review_scope, "is_review_card",
                        lambda w, c: True)
    monkeypatch.setattr(review_scope, "_self_seq_span", lambda: 3)
    await _append(wid, cid, [
        {"role": "user", "content": f"u{i}"} for i in range(6)])
    rows = context_board.read_board(wid, cid)
    assert [r["seq"] for r in rows] == [3, 4, 5], \
        f"评审卡板只留最近 span 窗: {rows}"


# ================================================================
# 等价：板投影 ≡ 旧四层筛投影（换路硬判据）
# ================================================================

async def test_board_equiv_old_pipeline(sandbox, work, monkeypatch):
    wid, cid = work["work_id"], "card_001"
    monkeypatch.setattr(review_scope, "review_card_ids",
                        lambda w: frozenset({"card_117"}))
    blocks = (
        [{"role": "user", "content": f"u{i}"} for i in range(4)]
        + [{"actor": "card:card_117", "role": "user", "content": "派单"}]
        + [{"actor": "assistant", "content": "", "tool_calls": [{"id": "t"}]}]
        + [{"actor": "tool", "tool_call_id": "t", "content": "r"}]
        + [{"role": "assistant", "content": "答"},
           {"role": "assistant", "content": "带思考",
            "reasoning_content": "想过了"}]
    )
    await _append(wid, cid, blocks)
    mark = 3
    memory_domain.save_chain(wid, cid, [], condense_mark=mark)
    raw = origin_store.read_blocks(wid, cid, 0)

    old = context_regions.project_history_messages(
        review_scope.drop_stale_review(wid, cid, raw), mark)
    # [调用对存根 2026-11-14] 板行不含工具块——等价判据须镜像 _render_prior_for
    # 的合并路径：板行 + 原文工具对块按 seq 合回后投影。
    merged = context_regions._merge_toolish_into(
        context_board.read_board(wid, cid), raw)
    new = context_regions.project_history_messages(merged, mark)
    assert new == old, f"板投影须与旧管线逐条一致\nold={old}\nnew={new}"
    assert any(m.get("reasoning_content") == "想过了" for m in new)


# ================================================================
# 调用对存根回灌（2026-11-14 · 用户令「试下方案1」）
# 判据：最近 K 组完整调用对以存根形态回灌；更早的照剥；孤儿不救；
# 长参数折叠；结果体不泄回上下文。
# ================================================================


def _pair(call_id: str, name: str = "read_card", ok: bool = True,
          args: str = '{"a":1}') -> list[dict]:
    body = json.dumps({"ok": ok, "x": "y" * 500}, ensure_ascii=False)
    return [
        {"actor": "assistant", "role": "assistant", "content": "",
         "tool_calls": [{"id": call_id, "type": "function",
                         "function": {"name": name, "arguments": args}}]},
        {"actor": "tool", "role": "tool", "tool_call_id": call_id,
         "content": body},
    ]


def test_stub_recent_pair_emitted():
    msgs = ( [{"role": "user", "content": "问", "seq": 1}]
             + [dict(b, seq=2 + i) for i, b in enumerate(_pair("c1"))]
             + [{"role": "assistant", "content": "答", "seq": 9}] )
    out = context_regions.project_history_messages(msgs)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool", "assistant"], out
    call_msg = out[1]
    assert call_msg["tool_calls"][0]["function"]["name"] == "read_card"
    stub = out[2]
    assert stub["tool_call_id"] == "c1"
    assert "read_card" in stub["content"] and "ok" in stub["content"]
    assert "yyyy" not in stub["content"], "结果正文不得泄回上下文"


def test_stub_drops_orphan_tool_and_old_pairs(monkeypatch):
    monkeypatch.setattr(context_regions, "_TOOL_PAIR_STUB_KEEP", 1)
    blocks = (
        _pair("old1")                      # 旧对：超 K 剥掉
        + [{"actor": "tool", "tool_call_id": "x", "content": "孤儿"}]
        + [{"role": "user", "content": "间"}]
        + _pair("new1")                    # 新对：保留
    )
    for i, b in enumerate(blocks):
        b["seq"] = i
    out = context_regions.project_history_messages(blocks)
    texts = [str(m.get("content", "")) for m in out]
    assert any("new1" == m.get("tool_call_id") for m in out)
    assert not any("old1" == m.get("tool_call_id") for m in out), out
    assert not any("孤儿" in t for t in texts), "孤儿 tool 不得回灌"


def test_stub_args_and_error_folded():
    long_args = "x" * 900
    msgs = [dict(b, seq=i) for i, b in enumerate(
        _pair("c9", args=long_args, ok=False))]
    out = context_regions.project_history_messages(msgs)
    args = out[0]["tool_calls"][0]["function"]["arguments"]
    assert len(args) <= context_regions._TOOL_STUB_ARGS_CAP + 10
    assert "…[参数已折叠]" in args
    assert "error" in out[1]["content"]


def test_stub_no_pairs_no_change():
    msgs = [{"role": "user", "content": "纯对话", "seq": 1},
            {"role": "assistant", "content": "好", "seq": 2}]
    assert context_regions.project_history_messages(msgs) == [
        {"role": "user", "content": "纯对话"},
        {"role": "assistant", "content": "好"}]


# ================================================================
# 自愈与幂等
# ================================================================

async def test_lazy_rebuild_self_heal(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [{"role": "user", "content": "有货"}])
    _tx(wid, lambda c: c.execute(
        "DELETE FROM context_board WHERE card_id=?", (cid,)))
    context_board._REBUILD_TRIED.clear()
    rows = context_board.read_board(wid, cid)
    assert [r["content"] for r in rows] == ["有货"], "板空+原文有货须就地重建"


async def test_rebuild_idempotent(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"}])
    n1 = context_board.rebuild_board(wid, cid)
    n2 = context_board.rebuild_board(wid, cid)
    assert n1 == n2 == 2
    assert context_board.board_count(wid, cid) == 2


# ================================================================
# 读路：_render_prior_for 走板 + seqless 附加块接尾
# ================================================================

async def test_render_prior_from_board(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "答"}])
    ctx = context_regions.RegionCtx(
        work_id=wid, card_id=cid,
        history_source=[
            {"seq": 0, "actor": "human", "role": "user", "content": "被板取代"}],
        history_extra=[
            # prior_extra 是别的卡的会话块——带真 seq，必须走独立通道而非 seq 辨别
            {"seq": 42, "actor": "card:card_009", "role": "user",
             "content": "[[card-msg]] 附加评审发言"}],
        history_mark=0)
    out = context_regions._render_prior_for(ctx, "history")
    contents = [m["content"] for m in out]
    assert "被板取代" not in contents, "板开启时 history_source 原文不再作数"
    assert "附加评审发言" in contents[-1], "附加块走 history_extra 接在板后"
    assert [m["role"] for m in out] == ["user", "assistant", "system"]


async def test_current_pair_excluded_from_board(sandbox, work):
    """[本轮消息双重计数 2026-11-14] 本轮 user 块落盘即进板；装配须按
    current_pair_id 把它滤出历史——否则同条消息在 prior 与 user_prompt
    各出现一次（work_073 card_001 实录"你粘贴了两遍"）。"""
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [
        {"role": "user", "content": "上一条", "pair_id": "old-pair"},
        {"role": "assistant", "content": "上一条回复", "pair_id": "old-pair"},
        {"role": "user", "content": "本条", "pair_id": "this-pair"}])
    ctx = context_regions.RegionCtx(
        work_id=wid, card_id=cid, current_pair_id="this-pair")
    out = context_regions._render_prior_for(ctx, "history")
    contents = [m["content"] for m in out]
    assert "本条" not in contents, "本轮 user 块不得出现在历史里"
    assert contents == ["上一条", "上一条回复"], contents
    # pair_id 缺省＝不过滤（评审/草稿路径），行为不变
    ctx2 = context_regions.RegionCtx(work_id=wid, card_id=cid)
    out2 = context_regions._render_prior_for(ctx2, "history")
    assert [m["content"] for m in out2] == ["上一条", "上一条回复", "本条"]


async def test_flag_off_falls_back(sandbox, work, monkeypatch):
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [{"role": "user", "content": "旧路"}])
    monkeypatch.setenv("CONTEXT_BOARD", "0")
    raw = origin_store.read_blocks(wid, cid, 0)
    ctx = context_regions.RegionCtx(
        work_id=wid, card_id=cid, history_source=raw, history_mark=0)
    out = context_regions._render_prior_for(ctx, "history")
    assert [m["content"] for m in out] == ["旧路"]
