# -*- coding: utf-8 -*-
"""[思考流支持层 2026-10-12] 三席 think 流展示 + 拆「思考吃输出」两雷的回归网。

覆盖三段：
1. ThinkStreamSplitter —— 内联在 content 里的思考块按标签边界切出（minimax 形态）；
2. clean_viewpoint_text —— 拆雷①（未闭合思考块不再整段作废）+ 两种标签写法；
3. _call_viewpoint —— 拆雷②（改走 stream_call）：字段类思考与内联类思考都按
   model 标签落 /tokens，三席正文**绝不**上 /tokens。

零真实模型调用：全部走假网关（签名与真实网关 stream_call 对齐）。
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.api import event_stream
from src.api.simple_service import ThinkStreamSplitter, _call_viewpoint, clean_viewpoint_text


# ---------------------------------------------------------------- 内联思考切分

def test_splitter_splits_closed_inline_think():
    """minimax-m3 实测形态：思考带 <think>…</think> 标签内联在 content 里。"""
    sp = ThinkStreamSplitter()
    think, body = sp.feed("<think>先看题面</think>结论：可行。")
    assert think == "先看题面"
    assert body == "结论：可行。"
    assert not sp.in_think


def test_splitter_handles_tag_split_across_frames():
    """标签跨帧切裂（真实流里逐帧到达）：残片缓冲等补齐，不得丢字也不得误判。"""
    sp = ThinkStreamSplitter()
    think1, body1 = sp.feed("前<thi")
    assert (think1, body1) == ("", "前")
    think2, body2 = sp.feed("nk>思考中")
    assert (think2, body2) == ("思考中", "")
    think3, body3 = sp.feed("</think>正文")
    assert (think3, body3) == ("", "正文")
    assert "".join([think1, think2, think3]) == "思考中"
    assert "".join([body1, body2, body3]) == "前正文"


def test_splitter_does_not_swallow_closing_tag_prefix():
    """正文里出现 "</thin" 这种半截标记：缓冲但不误判，flush 时按正文吐出。"""
    sp = ThinkStreamSplitter()
    think, body = sp.feed("正文</thin")
    assert think == ""
    assert body == "正文"
    assert sp.pending == "</thin"
    tail_think, tail_body = sp.flush()
    assert (tail_think, tail_body) == ("", "</thin")


def test_splitter_handles_thinking_tag_variant():
    """<thinking>…</thinking> 写法一视同仁（形态表里的第二行）。"""
    sp = ThinkStreamSplitter()
    think, body = sp.feed("<thinking>甲</thinking>正文乙")
    assert (think, body) == ("甲", "正文乙")


def test_splitter_unclosed_think_goes_to_think_side():
    """未闭合（模型中断）：闭合标记后的内容当帧即算思考（think 流即时展示），
    flush 只负责把帧尾残片吐干净，不吞字。"""
    sp = ThinkStreamSplitter()
    think, body = sp.feed("结论：可行。<think>还没想完就断了")
    assert (think, body) == ("还没想完就断了", "结论：可行。")
    assert sp.in_think
    assert sp.flush() == ("", "")


# ---------------------------------------------------------------- 观点清洗（拆雷①）

def test_clean_keeps_body_before_unclosed_think():
    """拆雷①主断言：未闭合思考块**不再整段作废**——标记前的正文必须留下。"""
    out = clean_viewpoint_text("结论：可行。<think>还没想完就断了")
    assert out == "结论：可行。"


def test_clean_drops_think_after_body_with_thinking_tag():
    assert clean_viewpoint_text("正文在前。<thinking>思考</thinking>") == "正文在前。"


def test_clean_empty_when_whole_text_is_unclosed_think():
    """整段都是思考（开始标记在开头）仍返空串——旧行为逐字不变
    （tests/test_proposal_parse.py 钉的就是这一条）。"""
    assert clean_viewpoint_text("<think>未闭合的思考，没有结束标记") == ""


def test_clean_strips_closed_think_and_proposal_block():
    out = clean_viewpoint_text(
        "<think>内部推理</think>观点正文。\n```card-proposal\n[{\"title\": \"t\"}]\n```")
    assert out == "观点正文。"


# ---------------------------------------------------------------- 模型配置侧方言（逐家适配）

def test_minimax_thinking_on_dialect_adaptive_not_enabled():
    """[2026-10-16 裁定 B · 双断言] 编译层不翻译；minimax 方言表产出 adaptive。

    官方文档（阿里云百炼 MiniMax-M3）：``thinking.type`` 只有 ``adaptive``（默认，模型自判）
    与 ``disabled``；**无 ``enabled``**。故 thinking=on → adaptive。"""
    from src.api import model_config
    from src.model_gateway.llm_openai_compat.dialects import ThinkingSpec, apply_thinking

    cfg = model_config.compile({
        "providers": {"minimax": {"protocol": "openai",
                                  "base_url": "https://x.invalid/v1", "api_key_env": "K"}},
        "models": {"minimax-m3": {"provider": "minimax", "model": "MiniMax-M3",
                                  "thinking": "on"}},
        "system": {},
    })
    entry = cfg["models"]["minimax-m3"]
    # ① 编译层不翻译
    assert "extra_body" not in entry
    assert entry["thinking"] == "on"
    assert entry["vendor"] == "minimax"
    # ② 适配器层锁行为
    body: dict = {}
    apply_thinking(body, ThinkingSpec(vendor=entry["vendor"], thinking=entry["thinking"]))
    assert body["thinking"] == {"type": "adaptive"}


def test_minimax_thinking_off_dialect_still_disabled():
    """off 分支：编译层原样带下，方言表产出 disabled。"""
    from src.api import model_config
    from src.model_gateway.llm_openai_compat.dialects import ThinkingSpec, apply_thinking

    cfg = model_config.compile({
        "providers": {"minimax": {"protocol": "openai",
                                  "base_url": "https://x.invalid/v1", "api_key_env": "K"}},
        "models": {"minimax-m3": {"provider": "minimax", "model": "MiniMax-M3",
                                  "thinking": "off"}},
        "system": {},
    })
    entry = cfg["models"]["minimax-m3"]
    assert "extra_body" not in entry
    body: dict = {}
    apply_thinking(body, ThinkingSpec(vendor=entry["vendor"], thinking=entry["thinking"]))
    assert body["thinking"] == {"type": "disabled"}


# ---------------------------------------------------------------- 三席流式转发（拆雷②）

class _FakeStreamGateway:
    """签名与真实网关 stream_call 对齐的假网关：回放一段脚本化的流。"""

    def __init__(self, script: list[dict[str, Any]], label: str = "seat-model"):
        self.config = {"models": {"vp1": {"model": label, "temperature": 0.7}}}
        self._script = script
        self.kwargs: dict[str, Any] = {}

    async def stream_call(self, model_name, system_prompt, user_prompt, temperature=0.5,
                          max_tokens=None, timeout=None, on_status=None, tools=None,
                          on_reasoning=None, prior_messages=None,
                          turn_tail=None, turn_once=None):
        # [三席身份注入 2026-10-14] 与真实网关签名对齐：补 turn_tail/turn_once
        # （三席调用经 turn_tail 下发身份注入；本替身只关心流回放，不消费它）。
        self.kwargs = {"model": model_name, "temperature": temperature,
                       "max_tokens": max_tokens, "timeout": timeout, "tools": tools}
        for ev in self._script:
            yield ev


def _frames(wid: str, cid: str, kind: str) -> list[dict[str, Any]]:
    return event_stream.token_journal_of(wid, cid, kind)


@pytest.fixture
def card_ctx():
    """给一张**本测试独有**的虚拟卡挂上逐字流上下文。

    逐字流 journal 是模块级内存（按 (work,card) 分桶、不随请求清空），所以每条
    用例必须用自己的卡号，否则后一条会读到前一条的帧。
    """
    cid = "card_" + uuid.uuid4().hex[:8]
    wid = "t_think"
    token = event_stream.set_stream_context(wid, cid)
    event_stream.emit(wid, cid, "run_start", {"pair_id": "p1"})
    try:
        yield wid, cid
    finally:
        event_stream.reset_stream_context(token)


async def test_seat_field_thinking_forwarded_with_model_tag(card_ctx):
    """字段类思考（qwen/glm/deepseek：delta.reasoning_content）：原样转发、带 model。"""
    wid, cid = card_ctx
    gw = _FakeStreamGateway([
        {"chunk_type": "reasoning", "chunk": "推理A", "text": "推理A"},
        {"chunk_type": "content", "chunk": "正文B", "text": "正文B"},
        {"content": "正文B", "tool_calls": [], "finish_reason": "stop",
         "tokens_used": 1, "latency_ms": 7},
    ], label="glm-5.3")
    meta = await _call_viewpoint(gw, "vp1", "sys", "user", None)

    assert meta["ok"] is True and meta["model"] == "glm-5.3"
    assert meta["text"] == "正文B"          # 思考不进观点正文
    assert gw.kwargs["max_tokens"] is None  # 拆闸铁律：不传 max_tokens
    assert gw.kwargs["tools"] is None       # 三席零工具
    reasons = _frames(wid, cid, "reasoning_chunk")
    assert [f["data"]["text"] for f in reasons] == ["推理A"]
    assert reasons[0]["data"]["model"] == "glm-5.3"
    # 三席正文绝不进逐字流（否则审议正文会串成助手回复）
    assert _frames(wid, cid, "content_chunk") == []


async def test_seat_inline_thinking_split_out_of_content(card_ctx):
    """内联类思考（minimax-m3 实测）：思考标签在 content 里，须被切到 think 流。"""
    wid, cid = card_ctx
    gw = _FakeStreamGateway([
        {"chunk_type": "content", "chunk": "<think>先看题面", "text": "<think>先看题面"},
        {"chunk_type": "content", "chunk": "再核判据</think>结论：可行。", "text": "再核判据</think>结论：可行。"},
        {"content": "<think>先看题面再核判据</think>结论：可行。",
         "tool_calls": [], "finish_reason": "stop", "tokens_used": 1, "latency_ms": 3},
    ], label="MiniMax-M3")
    meta = await _call_viewpoint(gw, "vp1", "sys", "user", None)

    assert meta["ok"] is True
    assert meta["text"] == "结论：可行。"                 # 终帧 content 经清洗剥净
    reasons = _frames(wid, cid, "reasoning_chunk")
    assert "".join(f["data"]["text"] for f in reasons) == "先看题面再核判据"
    assert {f["data"]["model"] for f in reasons} == {"MiniMax-M3"}
    assert _frames(wid, cid, "content_chunk") == []


async def test_seat_only_thinking_reports_distinct_error(card_ctx):
    """只有思考没有正文（如 qwen 开思考实测的空正文）：记 meta 失败，口径可辨。"""
    wid, cid = card_ctx
    gw = _FakeStreamGateway([
        {"chunk_type": "reasoning", "chunk": "只有思考", "text": "只有思考"},
        {"content": "", "tool_calls": [], "finish_reason": "stop",
         "tokens_used": 1, "latency_ms": 1},
    ])
    meta = await _call_viewpoint(gw, "vp1", "sys", "user", None)

    assert meta["ok"] is False
    assert "think 流" in meta["error"]
    assert len(_frames(wid, cid, "reasoning_chunk")) == 1   # 思考仍然给人看


async def test_seat_stream_exception_is_captured(card_ctx):
    """异常仍被 _call_viewpoint 吞进 meta（旧语义不变，永不抛出）。"""

    class _BoomGateway(_FakeStreamGateway):
        async def stream_call(self, *a, **kw):
            raise RuntimeError("上游炸了")
            yield {}   # pragma: no cover

    meta = await _call_viewpoint(_BoomGateway([]), "vp1", "sys", "user", None)
    assert meta["ok"] is False
    assert "上游炸了" in meta["error"]


async def test_seat_without_stream_context_emits_nothing():
    """无卡上下文（凝练/归拢等后台调用）时零逐字流——不硬塞。"""
    tok = event_stream.set_stream_context("t_tmp", "card_tmp")
    event_stream.reset_stream_context(tok)      # 还原成「未设置」
    assert event_stream.current_stream_context() is None
    gw = _FakeStreamGateway([
        {"chunk_type": "reasoning", "chunk": "思考", "text": "思考"},
        {"content": "正文", "tool_calls": [], "finish_reason": "stop",
         "tokens_used": 1, "latency_ms": 1},
    ])
    meta = await _call_viewpoint(gw, "vp1", "sys", "user", None)
    assert meta["ok"] is True
    assert event_stream.token_journal_of("t_tmp", "card_tmp", "reasoning_chunk") == []


# ------------------------------------------- 卡主路径内联思考剥除（2026-11-10）
# 用户实踩：卡的内部推理以 <think> 整段泄到卡面——主路径（model_service →
# message_runner → 卡面/会话）原来一个字都没剥，只有三席路径剥过。

class _MainGw:
    """model_service.call_main 的假网关：回放内联 <think> 的 content 流。"""

    def __init__(self, script: list[dict[str, Any]]):
        self.config = {"models": {"main": {"model": "fake-main",
                                           "temperature": 0.5}}}
        self._script = script

    async def stream_call(self, model_name, system_prompt, user_prompt, **_kw):
        import asyncio
        await asyncio.sleep(0)
        for ev in self._script:
            yield ev


async def test_main_path_strips_inline_think(monkeypatch):
    """终帧正文剥净内联思考；思考落 reasoning_content（不丢、走思考通道）。"""
    from src.api import model_service
    monkeypatch.setattr(model_service, "_gateway", _MainGw([
        {"chunk_type": "content", "text": "先说半句<thi"},
        {"chunk_type": "content",
         "text": "nk>内部推理不该见人</think>正文完。"},
        {"content": "先说半句<think>内部推理不该见人</think>正文完。",
         "tool_calls": [], "truncated_tool_call": None,
         "finish_reason": "stop", "tokens_used": 1, "latency_ms": 1},
    ]))
    res, _ = await model_service.call_main("sys", "user", model_name="main")
    assert "<think>" not in res["content"]
    assert "内部推理不该见人" not in res["content"]
    assert res["content"].endswith("正文完。")
    assert "内部推理不该见人" in res["reasoning_content"]


async def test_main_path_think_delta_goes_to_reasoning_channel(
        card_ctx, monkeypatch):
    """逐字流：content delta 里的思考分进 reasoning_chunk，正文通道不见标签。"""
    wid, cid = card_ctx
    from src.api import model_service
    monkeypatch.setattr(model_service, "_gateway", _MainGw([
        {"chunk_type": "content", "text": "前半句<thi"},
        {"chunk_type": "content", "text": "nk>想一想</think>后半句"},
        {"content": "前半句<think>想一想</think>后半句",
         "tool_calls": [], "truncated_tool_call": None,
         "finish_reason": "stop", "tokens_used": 1, "latency_ms": 1},
    ]))
    await model_service.call_main("sys", "user", model_name="main")
    reasons = "".join(str((f.get("data") or {}).get("text") or "") for f in
                      event_stream.token_journal_of(wid, cid, "reasoning_chunk"))
    bodies = "".join(str((f.get("data") or {}).get("text") or "") for f in
                     event_stream.token_journal_of(wid, cid, "content_chunk"))
    assert "想一想" in reasons, "思考要进 reasoning 逐字流"
    assert "<think>" not in bodies and "想一想" not in bodies, "正文通道不见思考"
    assert "前半句" in bodies and "后半句" in bodies
