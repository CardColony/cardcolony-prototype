# -*- coding: utf-8 -*-
"""原文判定（predicates）· 照抄 dsh `gen3/context/predicates.ts`。

判据：
P1  人类直输（无 source）＝原文
P2  `source.kind='plugin'` **不是**原文（否则折叠占位符/注入行会被重凝）
P3  `source.kind='steer'` **不是**原文（机器生成的复核指令不得被记忆吸收）
P4  assistant / 工具块＝原文（供 fold 配对）
P5  `is_condense_original_event` 额外排除工具块（工具结果正文不录凝练）
P6  凝练窗口用共享判据：plugin/steer 注入块不进 prompt
"""
from __future__ import annotations

from src.storage import memory_store, predicates


def _blk(actor: str, content: str = "x", **kw) -> dict:
    b = {"seq": 1, "actor": actor, "turn": 1, "pair": "0", "content": content,
         "ts": "2026-10-20T10:00:00+08:00"}
    b.update(kw)
    return b


# ---------------------------------------------------------------- P1–P4

def test_human_without_source_is_original():
    assert predicates.is_original_event(_blk("human")) is True


def test_plugin_injected_is_not_original():
    b = _blk("human", source={"kind": "plugin"})
    assert predicates.is_original_event(b) is False


def test_steer_is_not_original():
    b = _blk("human", source={"kind": "steer"})
    assert predicates.is_original_event(b) is False


def test_other_source_kind_is_original():
    b = _blk("human", source={"kind": "user"})
    assert predicates.is_original_event(b) is True


def test_assistant_and_tool_are_original():
    assert predicates.is_original_event(_blk("assistant")) is True
    assert predicates.is_original_event(_blk("tool", tool_call_id="c1")) is True
    assert predicates.is_original_event(
        _blk("assistant", tool_calls=[{"id": "c1"}])) is True


def test_non_message_actor_is_not_original():
    assert predicates.is_original_event(_blk("system")) is False


# ---------------------------------------------------------------- P5

def test_condense_excludes_tool_blocks():
    assert predicates.is_condense_original_event(_blk("tool", tool_call_id="c1")) is False
    assert predicates.is_condense_original_event(
        _blk("assistant", tool_calls=[{"id": "c1"}])) is False, "空 content 调用壳不录凝练"
    assert predicates.is_condense_original_event(_blk("assistant")) is True
    assert predicates.is_condense_original_event(_blk("human")) is True


# ---------------------------------------------------------------- P6

def test_condense_prompt_uses_shared_predicate():
    pending = [
        {"seq": 0, "actor": "human", "content": "用户真话"},
        {"seq": 1, "actor": "human", "content": "插件注入", "source": {"kind": "plugin"}},
        {"seq": 2, "actor": "human", "content": "复核续命", "source": {"kind": "steer"}},
        {"seq": 3, "actor": "assistant", "content": "模型回复"},
        {"seq": 4, "actor": "tool", "content": "工具结果", "tool_call_id": "c1"},
        {"seq": 5, "actor": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
    ]
    prompt = memory_store._build_condense_user_prompt(pending)
    print("PROMPT:", prompt)
    assert "用户真话" in prompt and "模型回复" in prompt
    assert "插件注入" not in prompt, "plugin 注入不进凝练"
    assert "复核续命" not in prompt, "steer 不进凝练"
    assert "工具结果" not in prompt, "工具结果不进凝练"
