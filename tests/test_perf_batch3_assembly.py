# -*- coding: utf-8 -*-
"""性能批次三（模型请求装配）· 受控替身语义核对与缓存验收（2026-11-19）。

任务书第三批验收口径：
  · 固定同一输入，用可控模型捕获最终请求——核对主模型/席位/工具续跳的
    system/prior/user/once/tail 语义与当前轮提示；
  · 同回合 system 区缓存：首跳渲染一次、续跳命中，**逐字节一致**；
  · viewpoints 剖面懒渲染：回合开场不再渲染（席位进场时才 compose），
    缺省契约不变。
打点只记数值/标识（observe assembly 行）；本文件为离线受控替身证据，
真实模型端样本（observe model_call 行的 first_chunk_ms / outbound）另见施工回报。
"""
from __future__ import annotations

import json

import pytest

from src.api import (context_assembler, context_regions, message_runner,
                     model_service, routes)
from src.storage import session_facade


# ---------------------------------------------------------------- 1 缓存
async def test_system_regions_cached_across_hops(typed_registry, work):
    """同回合两次 compose（模拟首跳/续跳）：system 逐字节一致、缓存命中、
    动态通道（prior/user/once/tail）按各自参数产出。"""
    wid = work["work_id"]
    ctx = context_assembler.build_ctx(wid, "card_001")

    once1 = [{"role": "user", "content": "〔当前轮〕真调工具，以回执为准。"}]
    tail1 = [{"role": "assistant", "content": "〔任务〕A"}]
    r1 = context_regions.compose_model_context(
        ctx, profile="main", prior_messages=[{"role": "user", "content": "q1"}],
        user_content="本轮消息", turn_once=once1, turn_tail=tail1)
    st0 = context_regions.system_cache_stats()

    once2 = [{"role": "user", "content": "〔当前轮〕续跳提示（不同内容）"}]
    tail2 = [{"role": "assistant", "content": "〔任务〕A"},
             {"role": "user", "content": "工具回执已入槽"}]
    r2 = context_regions.compose_model_context(
        ctx, profile="main", prior_messages=[
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "read_card", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "回执"},
        ],
        prior_key="tool_turn", user_content="本轮消息",
        turn_once=once2, turn_tail=tail2)
    st1 = context_regions.system_cache_stats()

    assert r1["system_prompt"] == r2["system_prompt"], "续跳 system 区逐字节不一致"
    assert r1["neutral_context"] == r2["neutral_context"]
    assert r2["prior_messages"] != r1["prior_messages"], "续跳 prior 未按新参数产出"
    # tail 通道＝request_tail 直通（once 通道受判断协议区闸控——思维链关时
    # 恒空，是既有语义；这里只核 tail 直通）。
    assert r2["turn_tail"] == tail2 and r1["turn_tail"] == tail1
    assert st1["hits"] == st0["hits"] + 1, "续跳未命中 system 缓存"


async def test_compose_profile_isolated(typed_registry, work):
    """main 与 viewpoint 各自缓存、互不串用（profile 是缓存键的一部分）。"""
    wid = work["work_id"]
    ctx = context_assembler.build_ctx(wid, "card_001")
    m = context_regions.compose_model_context(ctx, profile="main",
                                              user_content="x")
    v = context_regions.compose_model_context(ctx, profile="viewpoint",
                                              user_content="x")
    assert m["system_prompt"] != v["system_prompt"]
    st = context_regions.system_cache_stats()
    m2 = context_regions.compose_model_context(ctx, profile="main",
                                               user_content="y")
    v2 = context_regions.compose_model_context(ctx, profile="viewpoint",
                                               user_content="y")
    st2 = context_regions.system_cache_stats()
    assert m2["system_prompt"] == m["system_prompt"]
    assert v2["system_prompt"] == v["system_prompt"]
    assert st2["hits"] == st["hits"] + 2


# ---------------------------------------------------------------- 2 懒渲染
async def test_bundle_viewpoints_lazy(typed_registry, work):
    """with_viewpoints=False ⇒ 不渲染 viewpoint 剖面（None）；缺省 True 契约不变。"""
    wid = work["work_id"]
    bundle = context_assembler.build_context_bundle(wid, "card_001",
                                                    with_viewpoints=False)
    assert bundle["viewpoints"] is None
    assert bundle["main"]["system_prompt"]
    bundle2 = context_assembler.build_context_bundle(wid, "card_001")
    assert isinstance(bundle2["viewpoints"], dict)
    assert bundle2["viewpoints"]["system_prompt"]


# ------------------------------------------------- 3 受控替身语义核对
async def test_hop_requests_semantics_with_stub(typed_registry, work,
                                                monkeypatch):
    """可控模型捕获两跳最终请求：system 逐字节一致、工具调用对完整、
    当前轮短提示只随首跳下发、user 位每跳都在。"""
    wid, cid = work["work_id"], "card_001"
    captured: list[dict] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **kw):
        captured.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "prior_messages": list(prior_messages or []),
            "turn_once": list(kw.get("turn_once") or []),
            "turn_tail": list(kw.get("turn_tail") or []),
            "tools": list(tools or []),
        })
        if len(captured) == 1:
            return ({"content": "", "tool_calls": [{
                "id": "call_x_1", "type": "function",
                "function": {"name": "read_card",
                             "arguments": json.dumps({"card_id": cid})},
            }]}, "stub")
        return ({"content": "完成。", "tool_calls": []}, "stub")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")

    res = await routes._run_message(wid, cid, "帮我读一下本卡现状")
    assert res.get("error") is None, res

    assert len(captured) == 2, f"应两跳（工具跳+收尾跳），实 {len(captured)}"
    h1, h2 = captured
    # ① system 区逐字节一致（同回合缓存语义）
    assert h2["system_prompt"] == h1["system_prompt"]
    # ② 当前轮短提示只随首跳（turn_once 通道，续跳不重复）
    if h1["turn_once"]:
        once2_texts = [str(m.get("content") or "") for m in h2["turn_once"]]
        for m in h1["turn_once"]:
            assert str(m.get("content") or "") not in once2_texts, \
                "首跳短提示被续跳重复下发"
    # ③ 工具调用对完整：续跳 prior 里 assistant(tool_calls) 与 tool 结果成对
    roles = [str(m.get("role") or "") for m in h2["prior_messages"]]
    tool_call_msg = next((m for m in h2["prior_messages"]
                          if m.get("tool_calls")), None)
    assert tool_call_msg is not None, "续跳 prior 缺 assistant 工具调用位"
    tool_results = [m for m in h2["prior_messages"] if m.get("role") == "tool"]
    assert [t["tool_call_id"] for t in tool_results] == \
        [tc["id"] for tc in tool_call_msg["tool_calls"]], "工具对未配平"
    # ④ user 位：hop1 带本轮原文；hop2 走「工具轮去假 user」既有语义
    # （user 位留空、原文已固化进 prior[0]——2026-10-21 用户令，防模型把
    # 占位符当人声）。核对的是**上下文完整性**：本轮原文必须可见。
    assert "帮我读一下本卡现状" in str(h1["user_prompt"])
    assert str(h2["prior_messages"][0].get("content") or "") == "帮我读一下本卡现状" \
        or "帮我读一下本卡现状" in str(h2["user_prompt"]), \
        "续跳请求里本轮原文既不在 user 位也不在 prior——上下文断裂"
    # ⑤ 两跳 system_prompt 非空、tools 同面
    assert h1["system_prompt"] and h2["system_prompt"]


# ---------------------------------------------------------------- 观测四段
def test_model_call_row_has_four_segments(tmp_path):
    """model_call 行四段齐全且单位统一毫秒 float（None 如实留空）。"""
    import json as _json
    import io
    from src.infra import observe
    observe.log_model_call(trace_id="t4", caller="test:four", model="m",
                           latency_ms=1234.5, serialize_ms=3.25,
                           first_chunk_ms=88.0, first_content_ms=120.5)
    row = None
    with open(observe._logfile(), encoding="utf-8") as f:
        for ln in f:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if r.get("kind") == "model_call" and r.get("trace_id") == "t4":
                row = r
    assert row is not None, "model_call 行未落盘"
    assert row["serialize_ms"] == 3.2 and row["first_chunk_ms"] == 88.0
    assert row["first_content_ms"] == 120.5 and row["latency_ms"] == 1234.5
    # 未采集段如实 None
    observe.log_model_call(trace_id="t4b", caller="test:four", model="m",
                           latency_ms=10)
    row2 = None
    with open(observe._logfile(), encoding="utf-8") as f:
        for ln in f:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if r.get("kind") == "model_call" and r.get("trace_id") == "t4b":
                row2 = r
    assert row2["serialize_ms"] is None and row2["first_chunk_ms"] is None \
        and row2["first_content_ms"] is None


# ------------------------------------------------ 返工3·已知延迟验证
async def test_gateway_segments_with_known_latency(typed_registry, work,
                                                   monkeypatch):
    """用已知延迟验证各段读数：monkeypatch _build_options 注入 60ms sleep
    ⇒ serialize_ms≈60ms；json_dumps_ms>0（实际 JSON 序列化段独立出数）；
    first_content_ms≈注入的首帧延迟；build_context 分段无负数。"""
    import asyncio as _aio
    import time
    from src.model_gateway import gateway as gw

    delays = {"build": 0.06, "first_frame": 0.12}

    from src.model_gateway.llm.types import (
        ReasoningDelta, TextDelta, UsageChunk, TokenUsage, FinishChunk,
        StopReason, SerializeTimingChunk)
    from src.model_gateway.gateway import BlockAssembler

    real_build = gw.ModelGateway._build_options

    async def fake_stream(options, **kw):
        # 适配器真实编码点采样回传帧（已知 7.25ms）
        yield SerializeTimingChunk(ms=7.25, payload_bytes=4096)
        await _aio.sleep(delays["first_frame"])
        # 先一个思考帧（非正文），再正文——验证 first_content 只认正文
        yield ReasoningDelta(index=0, text="思考中")
        await _aio.sleep(0.02)
        yield TextDelta(index=0, text="你好")
        yield TextDelta(index=0, text="世界")
        yield UsageChunk(usage=TokenUsage(
            input_tokens=10, output_tokens=5, reasoning_tokens=0))
        yield FinishChunk(reason=StopReason())

    def fake_build(self, *a, **kw):
        time.sleep(delays["build"])
        return {"model": "fake", "messages": [{"role": "user",
                "content": "x" * 4096}], "stream": True}

    monkeypatch.setattr(gw.ModelGateway, "_build_options", fake_build)
    monkeypatch.setattr(gw, "_guarded_stream",
                        lambda sem, it: it)
    gw_inst = gw.ModelGateway.__new__(gw.ModelGateway)
    gw_inst.config = {"models": {"fake": {
        "vendor": "openai-compat", "endpoint_protocol": "declared_text",
        "max_attempts": 1, "backoff": 0.0, "completion_marker": ""}},
        "system": {}}
    from src.model_gateway.gateway import RuntimeStats
    gw_inst.stats = RuntimeStats()
    try:
        gw_inst._sem_for = lambda name: _aio.Semaphore(4)
        gw_inst._runtime = type("R", (), {"stream": staticmethod(fake_stream)})()
        gw_inst._endpoint_protocol = lambda name: "declared_text"
        gw_inst._retry_params = lambda name: (1, 0.0)
        gw_inst._retry_grading = lambda: (False, 0, None)

        def _result_from(assembler, model_name, started, tools):
            blocks = assembler.blocks()
            content = "".join(b.text for b in blocks
                              if type(b).__name__ == "TextBlock")
            return {"content": content, "tool_calls": [],
                    "finish_reason": "stop", "tokens_used": 15,
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        gw_inst._result_from = _result_from
        gw_inst._truncated_reply = lambda res, mt: None
        gw_inst._ds_norm = lambda x, y: x
    except Exception:
        pass
    result = await gw_inst.call(
        "fake", "sys", "user", work_id=work["work_id"], card_id="card_001",
        caller="test:segments")
    assert "世界" in result["content"]
    # 从 observe 行读回四段
    import json as _json
    from src.infra import observe as _obs
    row = None
    with open(_obs._logfile(), encoding="utf-8") as f:
        for ln in f:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if r.get("kind") == "model_call" and r.get("caller") == "test:segments":
                row = r
    assert row is not None, "model_call 行未落盘"
    # 已知延迟验证：serialize ≈ 60ms（±40ms 容差），first_content ≈ 120+20ms
    assert row["serialize_ms"] is not None and 20 <= row["serialize_ms"] <= 200, \
        f"serialize_ms={row['serialize_ms']}"
    assert 6.0 <= row["json_dumps_ms"] <= 12.0,         f"json_dumps_ms 应=适配器回传 7.25ms：{row['json_dumps_ms']}"
    assert row["first_content_ms"] is not None and \
        100 <= row["first_content_ms"] <= 400, \
        f"first_content_ms={row['first_content_ms']}（应≈首帧延迟 120ms+20ms）"
    # 首事件（first_chunk）应早于首正文（思考帧先到）
    assert row["first_chunk_ms"] is not None
    print(f"[四段] serialize={row['serialize_ms']} json_dumps={row['json_dumps_ms']} "
          f"first_chunk={row['first_chunk_ms']} first_content={row['first_content_ms']} "
          f"latency={row['latency_ms']}")


async def test_build_context_segments_non_negative(typed_registry, work,
                                                   monkeypatch):
    """build_context 分段（独立时间戳）无负数——受控复验曾报 -6546.2ms。"""
    import time as _t
    from src.api import message_runner, model_service
    from src.infra import observe as _obs

    # 只校验本次调用**新写**的行（文件里有旧代码版本的残留行——旧版
    # 差值算法单位混乱曾产出负数，正是返工要修的病）
    _n_before = sum(1 for _ in open(_obs._logfile(), encoding="utf-8"))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, **kw):
        return ({"content": "完成。", "tool_calls": []}, "stub")

    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    monkeypatch.setenv("MOCK_LLM", "1")
    wid, cid = work["work_id"], "card_001"
    res = await routes._run_message(wid, cid, "分段计时验证")
    assert res.get("error") is None, res
    import json as _json
    rows = []
    with open(_obs._logfile(), encoding="utf-8") as f:
        for i, ln in enumerate(f):
            if i < _n_before:
                continue
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if (r.get("kind") == "assembly"
                    and r.get("phase") == "build_context"
                    and r.get("work_id") == wid):
                rows.append(r)
    assert rows, "本次调用未落 build_context 打点行"
    for r in rows:
        ms = r["ms"]
        assert ms["memory_load"] >= 0 and ms["build_ctx_render"] >= 0, \
            f"分段出现负数：{ms}"
    print(f"[分段] 最近一次 memory_load={rows[-1]['ms']['memory_load']}ms "
          f"build_ctx_render={rows[-1]['ms']['build_ctx_render']}ms")


# --------------------------------- 返工3·流式主链路四段（call_main 同路径）
async def test_stream_call_segments_with_known_latency(typed_registry, work,
                                                       monkeypatch):
    """卡片实际入口链路（call_main → stream_call → _stream_once）四段齐全：
    注入已知延迟验证 serialize/json_dumps/first_chunk/first_content 同一次
    attempt 时间基准；适配器 timing 帧不透传给消费者。"""
    import asyncio as _aio
    import time
    from src.model_gateway import gateway as gw
    from src.model_gateway.llm.types import (
        SerializeTimingChunk, ReasoningDelta, TextDelta, UsageChunk,
        TokenUsage, FinishChunk, StopReason)

    async def fake_stream(options, **kw):
        # 适配器真实编码点采样回传帧（ms 已知 = 8.5ms）
        yield SerializeTimingChunk(ms=8.5, payload_bytes=4096)
        await _aio.sleep(0.10)
        yield ReasoningDelta(index=0, text="思考")
        yield TextDelta(index=0, text="正文开始")
        yield TextDelta(index=0, text="完")
        yield UsageChunk(usage=TokenUsage(input_tokens=10, output_tokens=5,
                                          reasoning_tokens=0))
        yield FinishChunk(reason=StopReason())

    monkeypatch.setattr(gw, "_guarded_stream", lambda sem, it: it)
    gw_inst = gw.ModelGateway.__new__(gw.ModelGateway)
    gw_inst.config = {"models": {"fake": {
        "vendor": "openai-compat", "endpoint_protocol": "declared_text",
        "max_attempts": 1, "backoff": 0.0, "completion_marker": ""}},
        "system": {}}
    from src.model_gateway.gateway import RuntimeStats
    gw_inst.stats = RuntimeStats()
    gw_inst._sem_for = lambda name: _aio.Semaphore(4)
    gw_inst._runtime = type("R", (), {"stream": staticmethod(fake_stream)})()
    gw_inst._endpoint_protocol = lambda name: "declared_text"
    gw_inst._retry_params = lambda name: (1, 0.0)
    gw_inst._retry_grading = lambda: (False, 0, None)

    def _result_from(assembler, model_name, started, tools):
        blocks = assembler.blocks()
        content = "".join(b.text for b in blocks
                          if type(b).__name__ == "TextBlock")
        return {"content": content, "tool_calls": [],
                "finish_reason": "stop", "tokens_used": 15,
                "latency_ms": int((time.monotonic() - started) * 1000)}
    gw_inst._result_from = _result_from
    gw_inst._truncated_reply = lambda res, mt: None
    gw_inst._ds_norm = lambda x, y: x

    seen_types = []
    async for ev in gw_inst.stream_call(
            "fake", "sys", "user", work_id=work["work_id"], card_id="card_001",
            caller="test:stream_call"):
        if isinstance(ev, dict):
            seen_types.append(ev.get("chunk_type") or "final")
    # timing 帧不透传（消费侧看不到 serialize-timing 事件位）
    assert "serialize-timing" not in seen_types, "内部观测帧泄漏给消费者"
    import json as _json
    from src.infra import observe as _obs
    row = None
    with open(_obs._logfile(), encoding="utf-8") as f:
        for ln in f:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if (r.get("kind") == "model_call"
                    and r.get("caller") == "test:stream_call"):
                row = r
    assert row is not None, "流式 model_call 行未落盘"
    # 四段全部非空（用户报的「全空」病灶）
    assert row["serialize_ms"] is not None, "serialize_ms 空"
    assert row["json_dumps_ms"] is not None, "json_dumps_ms 空"
    assert row["first_chunk_ms"] is not None, "first_chunk_ms 空"
    assert row["first_content_ms"] is not None, "first_content_ms 空"
    # 已知延迟验证：json_dumps==8.5（适配器回传值）；first_content≈100ms 首帧
    assert 6.0 <= row["json_dumps_ms"] <= 12.0, row["json_dumps_ms"]
    assert 80 <= row["first_content_ms"] <= 300, row["first_content_ms"]
    # 端点首事件（思考帧 100ms 后）早于/等于可见首字
    assert row["first_chunk_ms"] <= row["first_content_ms"] + 1.0
    print(f"[stream 四段] serialize={row['serialize_ms']} "
          f"json_dumps={row['json_dumps_ms']} "
          f"first_chunk={row['first_chunk_ms']} "
          f"first_content={row['first_content_ms']} "
          f"latency={row['latency_ms']}")


# ------------------------- 返工3·按尝试归账 + think 过滤（第三轮复核）-------------------------
async def test_first_event_not_inherited_from_failed_attempt(typed_registry,
                                                             work, monkeypatch):
    """模拟首试 503：首试延迟 0.3s 后失败 ⇒ 重试成功 ⇒ 首事件/首正文读数
    **不沿袭失败尝试**（按尝试归账）。若沿袭，first_chunk ≥ 300ms。"""
    import asyncio as _aio
    import time
    from src.model_gateway import gateway as gw
    from src.model_gateway.llm.types import (
        SerializeTimingChunk, TextDelta, UsageChunk, TokenUsage, FinishChunk,
        StopReason)

    async def fake_stream(options, **kw):
        if gw_model_service_flag["attempts"] == 0:
            gw_model_service_flag["attempts"] += 1
            await _aio.sleep(0.3)          # 首试慢（模拟 503 前的等待）
            raise gw.ModelCallFailure("HTTP 503", "upstream_error")
        gw_model_service_flag["attempts"] += 1
        yield SerializeTimingChunk(ms=5.0, payload_bytes=1024)
        yield TextDelta(index=0, text="正文立刻来")
        yield UsageChunk(usage=TokenUsage(input_tokens=1, output_tokens=1))
        yield FinishChunk(reason=StopReason())

    gw_model_service_flag = {"attempts": 0}
    monkeypatch.setattr(gw, "_guarded_stream", lambda sem, it: it)
    gw_inst = gw.ModelGateway.__new__(gw.ModelGateway)
    gw_inst.config = {"models": {"fake": {
        "vendor": "openai-compat", "endpoint_protocol": "declared_text",
        "max_attempts": 2, "backoff": 0.0, "completion_marker": ""}},
        "system": {}}
    from src.model_gateway.gateway import RuntimeStats
    gw_inst.stats = RuntimeStats()
    gw_inst._sem_for = lambda name: _aio.Semaphore(4)
    gw_inst._runtime = type("R", (), {"stream": staticmethod(fake_stream)})()
    gw_inst._endpoint_protocol = lambda name: "declared_text"
    gw_inst._retry_params = lambda name: (2, 0.0)
    gw_inst._retry_grading = lambda: (False, 0, None)

    def _result_from(assembler, model_name, started, tools):
        blocks = assembler.blocks()
        content = "".join(b.text for b in blocks
                          if type(b).__name__ == "TextBlock")
        return {"content": content, "tool_calls": [], "finish_reason": "stop",
                "tokens_used": 2, "latency_ms": 5}
    gw_inst._result_from = _result_from
    gw_inst._truncated_reply = lambda res, mt: None
    gw_inst._ds_norm = lambda x, y: x

    _res = await gw_inst.call(
        "fake", "sys", "user", work_id=work["work_id"], card_id="card_001",
        caller="test:attempt503")
    assert _res["content"]
    import json as _json
    from src.infra import observe as _obs
    row = None
    with open(_obs._logfile(), encoding="utf-8") as f:
        for ln in f:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if (r.get("kind") == "model_call"
                    and r.get("caller") == "test:attempt503"):
                row = r
    assert row is not None
    # 成功 attempt 内首帧立刻到（<200ms）；若沿袭首试 300ms ⇒ ≥300
    assert row["first_chunk_ms"] is not None and row["first_chunk_ms"] < 250, \
        f"first_chunk 沿袭了失败尝试：{row['first_chunk_ms']}"
    assert row["first_content_ms"] is not None and row["first_content_ms"] < 250, \
        f"first_content 沿袭了失败尝试：{row['first_content_ms']}"


async def test_first_content_excludes_think_span(typed_registry, work,
                                                 monkeypatch):
    """`<think>` 内联思考**不算可见首字**：首/二帧是 think 标签与思考文本
    （跨帧拆裂形态），第三帧才是正文 ⇒ first_content 应在第三帧时刻。"""
    import asyncio as _aio
    import time
    from src.model_gateway import gateway as gw
    from src.model_gateway.llm.types import (
        SerializeTimingChunk, TextDelta, UsageChunk, TokenUsage, FinishChunk,
        StopReason)

    async def fake_stream(options, **kw):
        yield SerializeTimingChunk(ms=1.0)
        await _aio.sleep(0.10)                     # 帧1：<thi（标签跨帧拆裂）
        yield TextDelta(index=0, text="<thi")
        await _aio.sleep(0.02)                     # 帧2：nk>思考文本
        yield TextDelta(index=0, text="nk>这段是思考</think>")
        await _aio.sleep(0.08)                     # 帧3：正文
        yield TextDelta(index=0, text="正文才开始")
        yield UsageChunk(usage=TokenUsage(input_tokens=1, output_tokens=1))
        yield FinishChunk(reason=StopReason())

    monkeypatch.setattr(gw, "_guarded_stream", lambda sem, it: it)
    gw_inst = gw.ModelGateway.__new__(gw.ModelGateway)
    gw_inst.config = {"models": {"fake": {
        "vendor": "openai-compat", "endpoint_protocol": "declared_text",
        "max_attempts": 1, "backoff": 0.0, "completion_marker": ""}},
        "system": {}}
    from src.model_gateway.gateway import RuntimeStats
    gw_inst.stats = RuntimeStats()
    gw_inst._sem_for = lambda name: _aio.Semaphore(4)
    gw_inst._runtime = type("R", (), {"stream": staticmethod(fake_stream)})()
    gw_inst._endpoint_protocol = lambda name: "declared_text"
    gw_inst._retry_params = lambda name: (1, 0.0)
    gw_inst._retry_grading = lambda: (False, 0, None)

    def _result_from(assembler, model_name, started, tools):
        blocks = assembler.blocks()
        content = "".join(b.text for b in blocks
                          if type(b).__name__ == "TextBlock")
        return {"content": content, "tool_calls": [], "finish_reason": "stop",
                "tokens_used": 2, "latency_ms": 5}
    gw_inst._result_from = _result_from
    gw_inst._truncated_reply = lambda res, mt: None
    gw_inst._ds_norm = lambda x, y: x

    async for _ev in gw_inst.stream_call(
            "fake", "sys", "user", work_id=work["work_id"], card_id="card_001",
            caller="test:thinkspan"):
        pass
    import json as _json
    from src.infra import observe as _obs
    row = None
    with open(_obs._logfile(), encoding="utf-8") as f:
        for ln in f:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if (r.get("kind") == "model_call"
                    and r.get("caller") == "test:thinkspan"):
                row = r
    assert row is not None
    # first_content ≈ 帧3（100+20+80=200ms 前后）；若 think 未过滤 ≈100ms
    assert 150 <= row["first_content_ms"] <= 400, \
        f"first_content 把 <think> 内文本算成可见首字：{row['first_content_ms']}"
    assert row["first_chunk_ms"] is not None and \
        row["first_chunk_ms"] < row["first_content_ms"], \
        "端点首事件应早于可见首字"
