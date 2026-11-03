# -*- coding: utf-8 -*-
"""★ **Command Code 执行体**的单测（2026-11-04）。

钉死四件事（都是实测踩出来的）：
  ① **旗标**：`--yolo` 必须给（`--permission-mode yolo` 不够 ⇒ 第一次工具调用
     会被拦一次）；`--tools-all` 必须给；`--no-session` 必须给（不落盘）；
  ② **事件解析**：NDJSON 的 ``tool_queued`` / ``tool_completed`` /
     ``text_delta`` / ``run_end`` / ``result`` 都能翻译成我们要的事件；
  ③ **空结果守卫**：0 工具 + 0 正文 ⇒ 判失败（不当成功）；
  ④ **额度措辞**能认出 ⇒ 让路给下一优先级。
"""
from __future__ import annotations

import json

import pytest

from src.api.wb_exec import cc_client


# ===================================================================== ① 旗标
def test_build_argv_has_the_three_mandatory_flags():
    """★ 三个必给旗标：``--yolo`` / ``--tools-all`` / ``--no-session``。"""
    r = cc_client.CcRunner("C:/x/index.mjs", model="m1")
    argv = r.build_argv("干活的正文")
    assert "--yolo" in argv, argv
    assert "--dangerously-skip-permissions" not in argv, "用旗标即可"
    assert "--tools-all" in argv, argv
    assert "--no-session" in argv, argv
    assert "--output-format" in argv and "json" in argv
    assert argv[argv.index("-p") + 1] == "干活的正文"
    assert argv[argv.index("-m") + 1] == "m1"


def test_build_argv_model_override():
    r = cc_client.CcRunner("C:/x/index.mjs", model="m1")
    argv = r.build_argv("x", "m2")
    assert argv[argv.index("-m") + 1] == "m2"


def test_long_prompt_keeps_head_and_tail():
    """★ 超长 prompt ⇒ **掐中间、保头尾**（尾部有回显指令，掐了就废）。"""
    head = "H" * 100
    tail = "T" * 100
    long = head + ("X" * 40000) + tail
    clipped = cc_client._clip_prompt(long)
    assert len(clipped) <= cc_client.MAX_PROMPT_CHARS + 200
    assert clipped.startswith("H")
    assert clipped.endswith("T")
    assert "截断" in clipped


# ===================================================================== ② 事件解析
@pytest.mark.asyncio
async def test_event_parsing_maps_tools_text_and_result():
    events = [
        {"type": "event", "event": {"type": "run_start", "sessionId": "s-1"}},
        {"type": "event", "event": {"type": "tool_queued", "toolCallId": "c1",
                                    "toolName": "write_file",
                                    "input": {"file_path": "a.txt"}}},
        {"type": "event", "event": {"type": "tool_completed", "toolCallId": "c1",
                                    "toolName": "write_file",
                                    "result": [{"type": "text", "text": "写好了"}]}},
        {"type": "event", "event": {"type": "text_delta", "delta": "干完了"}},
        {"type": "event", "event": {"type": "run_end",
                                    "result": {"finalText": "干完了",
                                               "stopReason": "end_turn",
                                               "usage": {"inputTokens": 10}}}},
        {"type": "result", "subtype": "success", "sessionId": "s-1",
         "finalText": "干完了", "usage": {"inputTokens": 10}},
    ]
    got: list[tuple[str, dict]] = []
    tools: list[dict] = []
    errors: list[str] = []
    texts: list[str] = []
    state: dict = {}
    for obj in events:
        await cc_client._handle_event(obj, tools, errors, texts,
                                      lambda k, p: got.append((k, p)), state)
    kinds = [k for k, _ in got]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert "final" in kinds
    assert tools and tools[0]["name"] == "write_file"
    assert tools[0]["status"] == "completed"
    assert tools[0]["output"] == "写好了"
    assert state["final_text"] == "干完了"
    assert state["session_id"] == "s-1"
    assert not errors


@pytest.mark.asyncio
async def test_event_parsing_captures_blocked_and_run_error():
    """★ 工具被拦 / run_error ⇒ 都要收进 errors（不然失败会被当成成功）。"""
    errors: list[str] = []
    state: dict = {}
    await cc_client._handle_event(
        {"type": "event", "event": {"type": "tool_hook_blocked",
                                    "toolName": "write_file",
                                    "hookOutput": "requires permissions"}},
        [], errors, [], None, state)
    await cc_client._handle_event(
        {"type": "event", "event": {"type": "run_error",
                                    "error": {"message": "boom"}}},
        [], errors, [], None, state)
    assert len(errors) == 2
    assert "requires permissions" in errors[0]
    assert "boom" in errors[1]


@pytest.mark.asyncio
async def test_quota_wording_is_recognised():
    """★ 额度措辞能认出来（⇒ 让路给下一优先级）。"""
    from src.api.wb_exec.bridge import _looks_exhausted
    for s in ("Free usage limit reached", "rate limit exceeded",
              "free_tier_limit", "quota exceeded"):
        assert _looks_exhausted(s) is True, s
    assert _looks_exhausted("connection reset") is False
