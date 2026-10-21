# -*- coding: utf-8 -*-
"""**思考事件合并压缩**工具的回归网（[5715] · 性能）。

测的是**纯逻辑**（``merge_records``）——工具本体是「改生产数据」的动作，
判据必须落在可测的地方：合并规则对不对、会不会误伤别的语义。

跑法：pytest tests/test_compact_reasoning.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "compact_reasoning_events", ROOT / "scripts" / "compact_reasoning_events.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOOL = _load_tool()


def _rec(seq, type_, text="", run_id="r1", round_=1, model="m1"):
    return {"seq": seq, "type": type_, "run_id": run_id, "round": round_,
            "model": model, "ts": 1700000000.0 + seq,
            "data": {"text": text} if type_ == "reasoning" else {"x": text}}


def test_consecutive_reasoning_of_same_key_merge_into_one():
    """连续同 (run, round, model) 的 reasoning ⇒ 合并成一条（文本按序拼接）。"""
    recs = [_rec(1, "reasoning", "第一段"), _rec(2, "reasoning", "第二段"),
            _rec(3, "reasoning", "第三段")]
    out = TOOL.merge_records(recs)
    assert len(out) == 1, f"三帧应合并成一条：{len(out)}"
    assert out[0]["data"]["text"] == "第一段第二段第三段"
    assert out[0]["seq"] == 1, "合并后以首条的身份字段为准"


def test_different_round_or_model_are_not_merged():
    """轮号或 model 不同 ⇒ **不合并**（三席的思考按 model 分桶，混了会串桶）。"""
    recs = [_rec(1, "reasoning", "甲", model="m1"),
            _rec(2, "reasoning", "乙", model="m2"),
            _rec(3, "reasoning", "丙", model="m1", round_=2)]
    out = TOOL.merge_records(recs)
    assert len(out) == 3, f"不同键不得合并：{len(out)}"
    assert [o["data"]["text"] for o in out] == ["甲", "乙", "丙"]


def test_non_adjacent_reasoning_is_not_merged():
    """**不相邻**的 reasoning 不合并（中间隔着别的语义，揉成一段会改语义）。"""
    recs = [_rec(1, "reasoning", "第一段"),
            _rec(2, "tool_call", "x"),
            _rec(3, "reasoning", "第二段")]
    out = TOOL.merge_records(recs)
    assert len(out) == 3, f"不相邻不得合并：{len(out)}"
    assert out[1]["type"] == "tool_call", "非 reasoning 事件原样保留"


def test_other_event_types_are_untouched():
    """非 reasoning 事件**一字不动**（顺序、条数、内容）。"""
    recs = [_rec(1, "user_message", "问"), _rec(2, "tool_call", "调"),
            _rec(3, "assistant_text", "答"), _rec(4, "done", "")]
    out = TOOL.merge_records(recs)
    assert out == recs, "非 reasoning 事件必须原样保留"


def test_file_roundtrip_is_stable(tmp_path):
    """写盘再读回 ⇒ 条数与内容稳定（合并是幂等的）。"""
    p = tmp_path / "card_001.jsonl"
    recs = [_rec(1, "reasoning", "一"), _rec(2, "reasoning", "二"),
            _rec(3, "round_start", ""), _rec(4, "reasoning", "三")]
    TOOL.write_records(p, recs)
    back = TOOL.read_records(p)
    assert len(back) == 4
    merged = TOOL.merge_records(back)
    # 相邻的两帧合并（1+2）；第 4 帧与第 2 帧**不相邻**（中间隔着 round_start）⇒ 不合
    assert len(merged) == 3, f"合并后应剩三条：{len(merged)}"
    assert merged[0]["data"]["text"] == "一二"
    assert merged[1]["type"] == "round_start"
    assert merged[2]["data"]["text"] == "三"
    # 幂等：再合一次不再变
    assert TOOL.merge_records(merged) == merged, "合并必须幂等"


def test_bad_line_is_preserved_not_dropped(tmp_path):
    """坏行**不丢**（标记出来）——压缩不是删数据的借口。"""
    p = tmp_path / "card_002.jsonl"
    p.write_text('{"type":"reasoning","data":{"text":"好"}}\n这不是 JSON\n',
                 encoding="utf-8")
    recs = TOOL.read_records(p)
    assert len(recs) == 2, f"坏行也要读出来：{len(recs)}"
    assert recs[1]["type"] == "<坏行>", "坏行要有标记"
    out = TOOL.merge_records(recs)
    assert len(out) == 2, "坏行不得被合并掉"
    assert json.dumps(out[0], ensure_ascii=False)
