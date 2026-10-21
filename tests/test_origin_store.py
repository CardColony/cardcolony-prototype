# -*- coding: utf-8 -*-
"""原文取数（origin_store）· 追溯链回归（2026-10-20 · 照抄 dsh origin-contract/selfread）。

判据：
V1  块 → 事件型映射（human/assistant/tool/工具调用块各自留型）
V2  `read_origin_span` 半开起点（from 含）、按 seq 升序、`throughSeq` = 末条 seq
V3  `through_seq` 为闭区间上界
V4  幂等：同 (card_id, seq) 重复写不重复、以最后一次为准
V5  **追溯**：记忆条目范围 → `trace_entry` 取回原文块（这是"记忆→原文"那条链）
V6  fail-open：库不存在/未迁移 → events=[]、不抛
V7  `from_seq` 必填（签名层面：无默认值）
V8  双写：`session_facade.append` 之后，原文表里有对应行
"""
from __future__ import annotations

import inspect

import pytest

from src.storage import card_db, card_store, origin_store, session_facade, work_store


def _blk(seq: int, actor: str, content: str = "", **kw) -> dict:
    b = {"seq": seq, "actor": actor, "turn": 1, "pair": "0", "content": content,
         "ts": "2026-10-20T10:00:00+08:00"}
    b.update(kw)
    return b


async def _mk_work() -> tuple[str, str]:
    wdata = await work_store.create_work("原文取数剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    return wid, "card_001"


# ---------------------------------------------------------------- V1

def test_event_type_mapping():
    assert origin_store.event_type_of(_blk(0, "human")) == "user_message"
    assert origin_store.event_type_of(_blk(1, "assistant")) == "assistant_text"
    assert origin_store.event_type_of(
        _blk(2, "assistant", tool_calls=[{"id": "c1"}])) == "tool_call"
    assert origin_store.event_type_of(_blk(3, "tool", tool_call_id="c1")) == "tool_result"
    assert origin_store.event_type_of({"seq": 4, "actor": "weird"}) == ""


# ---------------------------------------------------------------- V2/V3

async def test_origin_span_semantics(sandbox):
    wid, cid = await _mk_work()
    blocks = [_blk(i, "human" if i % 2 == 0 else "assistant", f"第{i}块") for i in range(5)]
    origin_store.write_blocks_sync(wid, cid, blocks)

    span = origin_store.read_origin_span(wid, cid, 2)
    print("SPAN seqs:", [e["seq"] for e in span["events"]])
    assert [e["seq"] for e in span["events"]] == [2, 3, 4], "半开起点：from 含、按 seq 升序"
    assert span["from"] == 2
    assert span["throughSeq"] == 4
    assert span["events"][0]["content"] == "第2块"

    # V3 闭区间上界
    span2 = origin_store.read_origin_span(wid, cid, 1, through_seq=3)
    assert [e["seq"] for e in span2["events"]] == [1, 2, 3]
    assert span2["throughSeq"] == 3


# ---------------------------------------------------------------- V4

async def test_write_is_idempotent(sandbox):
    wid, cid = await _mk_work()
    origin_store.write_blocks_sync(wid, cid, [_blk(0, "human", "第一版")])
    origin_store.write_blocks_sync(wid, cid, [_blk(0, "human", "第二版")])
    span = origin_store.read_origin_span(wid, cid, 0)
    assert len(span["events"]) == 1, "同 seq 不重复"
    assert span["events"][0]["content"] == "第二版", "以最后一次为准"


# ---------------------------------------------------------------- V5

async def test_trace_entry_returns_source_blocks(sandbox):
    """记忆条目说"我压自第 1–3 块" ⇒ 必须能取回那三块原文。"""
    wid, cid = await _mk_work()
    origin_store.write_blocks_sync(wid, cid, [
        _blk(0, "human", "块0"),
        _blk(1, "assistant", "块1"),
        _blk(2, "human", "块2"),
        _blk(3, "assistant", "块3"),
    ])
    traced = origin_store.trace_entry(wid, cid, 1, 3)
    assert [e["seq"] for e in traced["events"]] == [1, 2, 3]
    assert [e["content"] for e in traced["events"]] == ["块1", "块2", "块3"]


# ---------------------------------------------------------------- V6

async def test_read_fail_open_without_db(sandbox):
    """库里没有这张卡 ⇒ 空 span、不抛。"""
    span = origin_store.read_origin_span("work_不存在", "card_001", 0)
    assert span["events"] == [] and span["throughSeq"] is None


# ---------------------------------------------------------------- V7

def test_from_seq_is_required():
    sig = inspect.signature(origin_store.read_origin_span)
    p = sig.parameters["from_seq"]
    assert p.default is inspect.Parameter.empty, "from_seq 必填（禁全量，照抄 dsh PC-1）"


# ---------------------------------------------------------------- V8

async def test_append_dual_writes_origin(sandbox):
    """`session_facade.append` 之后，原文表里有对应行（md 与库双写）。"""
    wid, cid = await _mk_work()
    await session_facade.append(wid, cid, {"role": "user", "content": "你好"})
    span = origin_store.read_origin_span(wid, cid, 0)
    print("ORIGIN:", span)
    assert span["events"], "append 之后原文表应有行"
    assert span["events"][-1]["content"] == "你好"
    assert span["events"][-1]["type"] == "user_message"


# ---------------------------------------------------------------- 结构

def test_origin_table_exists_in_schema():
    ddl = "\n".join(card_db._SESSION_BLOCKS_DDL)
    assert "session_blocks" in ddl
    assert "PRIMARY KEY (card_id, seq)" in ddl.replace("\n", " ").replace("  ", " ")


# ---------------------------------------------------------------- 端到端追溯

async def test_memory_entry_traces_to_original_blocks(sandbox):
    """**用户要的那条链**：记忆条目 → 它压自的原文块。

    造 6 个块（3 轮 u/a，经真实 append 落 md＋库），再拿一条"压自 0–5"的条目去追溯，
    必须逐字取回那 6 块。这就是"记忆可追溯到原文"的最小证明。
    """
    wid, cid = await _mk_work()
    for i in range(3):
        await session_facade.append(wid, cid, {"role": "user", "content": f"问{i}"})
        await session_facade.append(wid, cid, {"role": "assistant", "content": f"答{i}"})

    entry = {"seq": 0, "conclusion": "结论", "evidence": "依据",
             "seq_from": 0, "seq_to": 5}
    traced = origin_store.trace_entry(wid, cid, entry["seq_from"], entry["seq_to"])
    print("TRACED:", [(e["seq"], e["content"]) for e in traced["events"]])
    assert [e["seq"] for e in traced["events"]] == [0, 1, 2, 3, 4, 5]
    assert [e["content"] for e in traced["events"]] == [
        "问0", "答0", "问1", "答1", "问2", "答2"]
    assert [e["type"] for e in traced["events"]] == [
        "user_message", "assistant_text"] * 3
