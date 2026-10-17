# -*- coding: utf-8 -*-
"""T14 · 交付链-分析进对话 回归网（V1–V7，mock 层 e2e）。

手法＝T11/T15 e2e 同款：`routes._run_message` ＋ monkeypatch model_service.call_main
（模块级对象 patch，simple/tool 轮全部拦截）；fake 按调用序脚本化返回。
改动面红线 RD-01：源码只 routes.py——本文件是配套施工件（测试），零源码旁路。
"""
from __future__ import annotations

import json

import pytest

from src.api import routes
from src.storage import card_store, session_facade, session_store, work_store


@pytest.fixture
async def work(sandbox):
    wdata = await work_store.create_work("T14交付链剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    await card_store.create_card(wid, "子卡甲", "用户直接创建", parent="card_001")
    return wdata["work_id"]


def _tc(name, args):
    return {"name": name, "arguments": args}


def _dialog(blocks):
    """[迭代二 S4 配套] 环节留痕块（behavior 非空：triage 等）不进对话断言
    口径——与 message_runner prior 投影过滤同一裁定的测试侧。
    [工具结果落会话 2026-10-17] 再滤掉工具块（工具调用块 assistant+tool_calls、
    工具结果块 actor=tool）——它们是新落盘事实，不参与「对话主干」断言。"""
    return [m for m in blocks if not m.get("behavior")
            and m.get("role") != "tool"
            and not (m.get("role") == "assistant" and m.get("tool_calls"))]


ANALYSIS_1 = "【第一轮分析】读完 card_002 正文后，我认为该段论证依赖一个隐含前提：需求已冻结。"
ANALYSIS_2 = "【第二轮分析】结合子卡甲的产物清单，上述前提在实际推进中并未成立，需回到范围裁定。"
REPLY_FINAL = "已完成两轮调研，结论见上文两段分析。以上为回执。"


def _fake_three_rounds(calls_box):
    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):  # [迭代三 S5 同步] model_name kwargs 吸收
        calls_box["n"] += 1
        n = (calls_box["n"] - 1) % 3 + 1  # 循环脚本化：重发请求重走三段（V3 需要）
        if n == 1:      # simple 统合轮：正文＝分析一，发起读卡
            return ({"content": ANALYSIS_1,
                     "tool_calls": [_tc("read_card", {"card_id": "card_002"})]}, "fake")
        if n == 2:      # 工具续跳轮 1：正文＝分析二，再发起读卡
            return ({"content": ANALYSIS_2,
                     "tool_calls": [_tc("read_card", {"card_id": "card_003"})]}, "fake")
        return ({"content": REPLY_FINAL, "tool_calls": []}, "fake")  # 末轮：纯回执
    return fake_call_main


# ---------------------------------------------------------------------------
# V1 · 分析＋回执同屏（分析在前、回执在后）
# ---------------------------------------------------------------------------


async def test_v1_analysis_and_reply_delivered(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    calls = {"n": 0}
    monkeypatch.setattr(routes.model_service, "call_main", _fake_three_rounds(calls))
    result = await routes._run_message(work, "card_001", "请调研 card_002 并给出分析")
    assert "error" not in result
    reply = result["reply"]
    # 分析在前、回执在后；两段分析都在（改造前只有末轮回执上屏）
    assert reply.index(ANALYSIS_1) < reply.index(ANALYSIS_2) < reply.index(REPLY_FINAL)
    # 末尾单个 \n＝parse_proposals 预存行为（strip()+\n，改造前同形态）
    assert reply == ANALYSIS_1 + "\n\n" + ANALYSIS_2 + "\n\n" + REPLY_FINAL + "\n"
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# V2 · 上屏面＝会话面（reply 与 assistant 条逐字相等）
# ---------------------------------------------------------------------------


async def test_v2_reply_equals_session_message(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    calls = {"n": 0}
    monkeypatch.setattr(routes.model_service, "call_main", _fake_three_rounds(calls))
    result = await routes._run_message(work, "card_001", "端到端一致性问题")
    sess = _dialog(await session_facade.read(work, "card_001"))
    assistant_msgs = [m for m in sess if m["role"] == "assistant"]
    # 一次请求恰一条 assistant（V3 配对不变量的落盘面）
    assert len(assistant_msgs) == 1
    # 前端 appendMessage 渲染的 resp.reply 与会话条同源同文（V2 逐字）
    assert result["reply"] == assistant_msgs[-1]["content"]
    assert assistant_msgs[-1]["content"].startswith(ANALYSIS_1)


# ---------------------------------------------------------------------------
# V3 · 配对不变＋重发同样交付
# ---------------------------------------------------------------------------


async def test_v3_pairing_and_resend(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    calls = {"n": 0}
    monkeypatch.setattr(routes.model_service, "call_main", _fake_three_rounds(calls))
    r1 = await routes._run_message(work, "card_001", "配对验证提问")
    sess1 = _dialog(await session_facade.read(work, "card_001"))
    pair_ids = [m["pair_id"] for m in sess1]
    assert pair_ids[0] and pair_ids[0] == pair_ids[1]  # 1 问 1 答、pair 一致
    n_after_first = len(sess1)
    raw1 = session_store._session_path(work, "card_001").read_bytes()

    # 重发同文（T10 重发判定走 pair 复用或新 pair——回复同样含分析正文）
    r2 = await routes._run_message(work, "card_001", "配对验证提问")
    assert ANALYSIS_1 in r2["reply"] and REPLY_FINAL in r2["reply"]
    sess2 = _dialog(await session_facade.read(work, "card_001"))
    raw2 = session_store._session_path(work, "card_001").read_bytes()
    # 无重复落盘：第二次请求仍恰新增一对（2 条），且首轮字节前缀原样未动
    # （append-only 硬证：比对文件字节，绕开 read_session「末条多保留一个
    # 尾换行」的预存解析边界——同条在末条/非末条时点读回差一个 \n）
    assert len(sess2) == n_after_first + 2
    assert raw1 == raw2[:len(raw1)]


# ---------------------------------------------------------------------------
# V4 · 纯对话轮回归：逐字同旧＋会话零新增字段
# ---------------------------------------------------------------------------


async def test_v4_pure_dialogue_unchanged(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):  # [迭代三 S5 同步] model_name kwargs 吸收
        return ({"content": "纯对话轮回复，未调用任何工具。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(work, "card_001", "一个不涉工具的问题")
    # 逐字同旧：含 parse_proposals 预存尾换行（改造前纯对话轮同形态）
    assert result["reply"] == "纯对话轮回复，未调用任何工具。\n"
    # 会话文件零新增字段（RD-03）：原始文本不含 T14 相关键
    raw = session_store._session_path(work, "card_001").read_text(encoding="utf-8")
    assert "analysis" not in raw and "delivered" not in raw


# ---------------------------------------------------------------------------
# V5 · 建卡协议扫描：夹块真建卡、无块不误扫
# ---------------------------------------------------------------------------


async def test_v5_proposal_in_analysis_parsed(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    analysis_with_block = (
        "【分析】范围须拆卡推进。\n```card-proposal\n"
        "[{\"title\": \"范围裁定卡\", \"parent\": \"card_001\", \"content\": \"裁定范围与边界\"}]\n"
        "```\n以上建议建卡。")

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):  # [迭代三 S5 同步] model_name kwargs 吸收
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": analysis_with_block,
                     "tool_calls": [_tc("read_card", {"card_id": "card_002"})]}, "fake")
        return ({"content": "回执：拆卡建议见分析段。", "tool_calls": []}, "fake")

    calls = {"n": 0}
    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(work, "card_001", "给出拆卡建议")
    # 建卡真发生（V5 前半）
    assert result["created_cards"], "协议块应被扫出并建卡"
    # 剥离后落盘不含协议块本体，但分析正文保留
    assert "card-proposal" not in result["reply"]
    assert "【分析】范围须拆卡推进。" in result["reply"]
    sess = _dialog(await session_facade.read(work, "card_001"))
    assert "card-proposal" not in sess[-1]["content"]
    assert "【分析】范围须拆卡推进。" in sess[-1]["content"]


async def test_v5_no_false_proposal_from_plain_analysis(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    calls = {"n": 0}
    monkeypatch.setattr(routes.model_service, "call_main", _fake_three_rounds(calls))
    result = await routes._run_message(work, "card_001", "普通分析请求（无协议块）")
    assert result["created_cards"] == []
    assert "【第一轮分析】" in result["reply"]  # 分析原样交付，未被扫描误伤


# ---------------------------------------------------------------------------
# V6 · 边界：末轮空→只交付分析；皆空→空回复不报错；失败路径不拼接
# ---------------------------------------------------------------------------


async def test_v6_analysis_only_when_final_empty(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):  # [迭代三 S5 同步] model_name kwargs 吸收
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"content": ANALYSIS_1,
                     "tool_calls": [_tc("read_card", {"card_id": "card_002"})]}, "fake")
        return ({"content": "", "tool_calls": []}, "fake")  # 末轮空正文

    calls = {"n": 0}
    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(work, "card_001", "末轮为空的场景")
    assert result["reply"] == ANALYSIS_1 + "\n"  # 只交付中间轮分析正文（尾换行预存）
    sess = _dialog(await session_facade.read(work, "card_001"))
    assert sess[-1]["content"].strip() == ANALYSIS_1


async def test_v6_all_empty_no_crash_no_extra_message(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):  # [迭代三 S5 同步] model_name kwargs 吸收
        return ({"content": "", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    before = len(_dialog(await session_facade.read(work, "card_001")))
    result = await routes._run_message(work, "card_001", "全空场景")
    assert result["reply"].strip() == ""  # 空回复不报错（parse 预存补尾换行）
    sess = _dialog(await session_facade.read(work, "card_001"))
    assert len(sess) == before + 2  # 恰一对（问+空答），不多落


async def test_v6_failure_path_no_delivery_and_response_none(work, sandbox, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")

    async def boom(system_prompt, user_prompt, prior_messages=None, tools=None):
        raise RuntimeError("模拟调用失败")

    monkeypatch.setattr(routes.model_service, "call_main", boom)
    result = await routes._run_message(work, "card_001", "失败场景")
    # _err 返回 JSONResponse（502）——拼接段不执行、无 assistant 落盘
    assert getattr(result, "status_code", None) == 502
    sess = _dialog(await session_facade.read(work, "card_001"))
    # 失败路径无 assistant 落盘（analysis_parts 不写任何长期存储）
    assert all(m["role"] != "assistant" for m in sess)


# ---------------------------------------------------------------------------
# V7 · simple_service 首轮统合路径回归（输入不经拼接段）
# ---------------------------------------------------------------------------


async def test_v7_simple_viewpoint_cleaning_unchanged():
    """clean_viewpoint_text 是首轮统合链路的独立清洗面（simple_service.py:50）：
    输入＝观点模型输出，不经 T14 拼接段——行为必须与改造前一致。"""
    from src.api.simple_service import clean_viewpoint_text
    text = "<think>推理内部</think>观点正文。\n```card-proposal\n[{\"title\": \"t\"}]\n```"
    clean, proposals = clean_viewpoint_text(text), None
    # 返回值形态未变：字符串（views 清洗）＋协议剥离
    assert clean == "观点正文。"
    # 三席观点正文不进 session/prior 由 RD-05 既有机制保证——此处锁定清洗面回归
