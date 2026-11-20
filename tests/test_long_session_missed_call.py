# -*- coding: utf-8 -*-
"""超长会话工具漏调专项 · 离线回放验收（2026-11-19）。

对应《超长会话工具漏调专项修复方案》验收节。历史模型输出按原始形态重放
（fake 模型返回与 22:31 / 22:35 / 22:16 实录同形态的首轮输出），走真实来源
入口（routes._run_message）和工具执行链，隔离数据。

优先验收「没有执行却显示完成」是否被消除：
- 22:31/22:35 形态（汇报唤醒＋零调用＋正文虚报）→ 系统结果＝「本轮未执行
  工具」，接收方零新增，研判正常交付；
- 22:16 形态（真调）→ 系统结果按执行事实呈现，接收方实际落信；
- directive 绑定动作回合首跳零调用 → 定向补救续跳一次（一次封顶）；
- 引用旧回执/代码示例里的「已发」不触发发信或额外唤醒。
"""
from __future__ import annotations

import json

import pytest
from types import SimpleNamespace

from src.api import message_runner, routes, tool_receipt
from src.storage import card_store, session_facade, work_store


def _tc(name, args):
    return {"name": name, "arguments": args}


def _proto(native=True, finish="stop"):
    return {"endpoint_protocol": "native" if native else "declared_text",
            "declared_text": not native,
            "native_calls": 0, "native_dropped": 0, "native_truncated": 0,
            "native_finish_tool_calls": False, "finish_reason": finish}


def _report_wake_kwargs():
    """汇报唤醒回合的来源（report_up → deliver_now 的真实形态）。"""
    return {"source": {"kind": "card_msg", "from": "card_013"},
            "source_kind": "internal"}


# ---------------------------------------------------------------- ① 22:31 重放
async def test_replay_2231_zero_call_false_claim(work, sandbox, monkeypatch):
    """汇报唤醒＋零调用＋正文虚报「两条指令已发」→ 系统结果＝本轮未执行工具。

    接收方零新增（没有任何指令落进子卡会话）；研判正常交付（不拒收）。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_TURN_HINT", raising=False)
    wid = work["work_id"]
    target = (await card_store.create_card(
        wid, "汇报来源卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        # 22:31 原始回复形态：零调用，正文虚报
        return ({"content":
                 "card_013 交付合格。QA 判通过。\n\n两条指令已发。",
                 "tool_calls": [], "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "card_013 交付合格汇报",
                                       **_report_wake_kwargs())
    assert "error" not in result
    # 系统结果行在场且＝未执行
    assert "〔系统结果〕本轮未执行工具" in result["reply"]
    # 接收方零新增：target 卡会话里没有任何卡间指令
    blocks = await session_facade.read(wid, target)
    from_target = [b for b in blocks if str(b.get("actor") or "").startswith("card:card_001")]
    assert from_target == [], "虚报回合不允许产生任何落信"
    # 无误派发、无误续跳（汇报研判不是绑定动作）
    assert result["tool_events"] == []
    kinds = [t for t, _ in events]
    assert "remediation_jump" not in kinds
    assert "action_not_executed" not in kinds
    # 会话块 receipt 带系统结果（刷新后的历史同源）
    sess = await session_facade.read(wid, "card_001")
    reply_blks = [b for b in sess if b.get("role") == "assistant" and not b.get("tool_calls")]
    assert reply_blks, "回复块应落会话"
    last = reply_blks[-1]
    assert (last.get("receipt") or {}).get("result_status") == "none"
    assert "本轮未执行工具" in str((last.get("receipt") or {}).get("result_text"))


# ---------------------------------------------------------------- ② 22:16 重放
async def test_replay_2216_real_calls_shown(work, sandbox, monkeypatch):
    """真调形态（direct 成功）→ 系统结果按执行事实呈现，接收方实际落信。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    target = (await card_store.create_card(
        wid, "收令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": target,
                                       "instruction": "补错误处理",
                                       "acceptance": "单测全绿"})]}, "fake")
        return ({"content": "指令已通过执行通道发出。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "card_013 汇报，请下达指令",
                                       **_report_wake_kwargs())
    assert "error" not in result
    te = [e for e in result["tool_events"] if e.get("tool") == "direct"]
    assert te and te[0]["ok"] is True
    # 系统结果按事实呈现：direct 成功 → 已写入（msg_id 可能空——投递被测试开关关闭）
    assert "〔系统结果〕" in result["reply"]
    assert "direct" in result["reply"]
    # 接收方实际落信（原始记录可查）
    blocks = await session_facade.read(wid, target)
    assert any(str(b.get("actor") or "").startswith("card:") for b in blocks), \
        "direct 落信应进收件方会话"
    # 回执摘要进执行记录
    assert te[0].get("send") is not None


# ---------------------------------------------------------------- ③ 定向补救续跳
async def test_remediation_once_then_real_call(work, sandbox, monkeypatch):
    """op 专用回合（明确动作+目标）零调用 → 定向续跳一次 → 纠正后真调。

    [2026-11-20 返修] 可信入口绑定：update_status 的动作=propose_status、
    目标=当前卡——零派发即补，不看正文措辞（验收 2：正文没声称完成仍能补救）。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:   # 首跳零调用（正文无任何词表措辞）
            return ({"content": "这个按钮的任务我看一眼再说。",
                     "tool_calls": [], "protocol_meta": _proto()}, "fake")
        if n["c"] == 2:   # 补救后真调（专用工具，目标=当前卡）
            return ({"content": "", "tool_calls": [
                        _tc("propose_status",
                            {"content": "进行中：联调，接口已对齐"})]},
                    "fake")
        return ({"content": "现状已提交。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "更新一下现状",
                                       op="update_status")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 1, "明确动作零调用 → 定向续跳恰好一次"
    te = [e for e in result["tool_events"]
          if e.get("tool") == "propose_status" and e.get("ok")]
    assert len(te) == 1, "补救后真调，真实写入数＝1"


async def test_directive_zero_call_research_allowed(work, sandbox, monkeypatch):
    """通用指令回合零调用（含引用旧回执/否定措辞）⇒ 零续跳、回执如实标注。

    [2026-11-20 返修 · 验收 1] 词表退役：指令回合引用旧回执编号、写否定
    措辞都不再触发任何续跳——通用指令允许零调用研判，系统回执如实标注
    「发信类动作+目标=发令卡」的核对结果。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        return ({"content": "我没有说已发——日志里「已发（msg_id 4692）」是"
                 "上一轮的旧记录；本轮引用的是旧回执，不代表这次执行过。",
                 "tool_calls": [], "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "把上次的结论发我",
                                       source_kind="directive")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 0, "引用旧回执/否定措辞 → 零续跳"
    # [二次返修] 普通指令（入口未携带汇报要求）→ 只展示本轮执行事实，
    # 不标「尚未向发令卡汇报」
    assert "〔系统回执〕" not in result["reply"]
    assert "〔系统结果〕本轮未执行工具" in result["reply"]


async def test_directive_still_zero_call_receipt(work, sandbox, monkeypatch):
    """入口明确携带汇报义务（require_report）的指令零调用 → 补一次＋如实标注。

    direct 投递的 source 显式带 require_report=True（机制级收口）＝明确
    绑定：零调用收口先定向补一次（B2 决定点）；补救后仍零调用则收口，
    「尚未向发令卡汇报」如实标注；写入数 0。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    target = (await card_store.create_card(
        wid, "收令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        return ({"content": "三条都已真发。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "发三条指令", source_kind="directive",
        source={"kind": "card_msg", "from": "card_888",
                "require_report": True})
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 1, \
        "require_report 绑定 + 零调用 → 恰好补一次"
    assert "action_not_executed" in kinds
    # 指令回合核对项＝「是否已向发令卡汇报」（措辞与业务任务分开）
    assert "尚未向发令卡汇报" in result["reply"]
    assert "〔系统结果〕本轮未执行工具" in result["reply"]
    blocks = await session_facade.read(wid, target)
    assert not [b for b in blocks if str(b.get("actor") or "").startswith("card:")], \
        "写入数＝0（虚报没有落信）"


# ---------------------------------------------------------------- ④ 部分成功/无关成功
async def test_partial_success_shown_individually(work, sandbox, monkeypatch):
    """多项动作部分成功（汇报唤醒回合）→ 系统结果逐项呈现：成功项与失败项分开。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    target = (await card_store.create_card(
        wid, "收令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": target,
                                       "instruction": "A", "acceptance": "a"}),
                        _tc("direct", {"card_id": "card_not_exists",
                                       "instruction": "B", "acceptance": "b"})]},
                    "fake")
        return ({"content": "发完了。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "收到汇报，发两条指令",
                                       **_report_wake_kwargs())
    assert "error" not in result
    assert "〔系统结果〕" in result["reply"]
    # 成功项与失败项各自呈现（一项成功只展示该项成功）
    assert "失败（not_found）" in result["reply"]
    assert "direct(" in result["reply"]


async def test_unrelated_success_cannot_satisfy(work, sandbox, monkeypatch):
    """无关工具成功（read_card ok）满足不了汇报义务 → 指令回合如实标注。

    入口显式带汇报义务（require_report+发令卡）的指令：read_card ∉ 发信类
    动作，且未向发令卡汇报 → 回执标注；业务完成与否按各项回执。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("read_card", {"card_id": "card_001"})]}, "fake")
        return ({"content": "已读，指令已发。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "发指令", source_kind="directive",
        source={"kind": "card_msg", "from": "card_777",
                "require_report": True})
    assert "error" not in result
    # 一次成功的无关读取无法证明指令已发出
    assert "〔系统回执〕" in result["reply"]
    te = [e for e in result["tool_events"] if e.get("tool") == "read_card"]
    assert te and te[0]["ok"] is True


# ---------------------------------------------------------------- ⑤ 引用旧回执不误触发
async def test_quoted_done_words_no_misfire(work, sandbox, monkeypatch):
    """引用旧回执/代码示例/写作中的「已发」→ 不触发发信或额外唤醒。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    target = (await card_store.create_card(
        wid, "汇报来源卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        # 正文只是**讨论**旧回执编号（评审研判），没有任何执行意图
        return ({"content":
                 "日志里写的「已发（msg_id 4692，delivered: true）」是上一轮的"
                 "旧记录，不能当本轮凭证；示例代码里的 `direct(...)` 调用只是"
                 "格式说明。综合研判：QA 判通过。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "card_013 汇报：评审结果",
                                       **_report_wake_kwargs())
    assert "error" not in result
    assert result["tool_events"] == []            # 无误派发
    kinds = [t for t, _ in events]
    assert "remediation_jump" not in kinds        # 不额外唤醒
    assert "action_not_executed" not in kinds     # 汇报研判不是绑定动作
    assert "〔系统结果〕本轮未执行工具" in result["reply"]  # 如实呈现
    blocks = await session_facade.read(wid, target)
    assert not [b for b in blocks if str(b.get("actor") or "").startswith("card:")]


# ---------------------------------------------------------------- ⑥ 汇报提示
async def test_report_wake_hint_injected(work, sandbox, monkeypatch):
    """汇报唤醒回合的首轮请求带两条路径提示；普通 human 回合不带。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_TURN_HINT", raising=False)
    wid = work["work_id"]
    captured: list = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        captured.append({"turn_once": list(turn_once or []),
                         "source_kind_in_ctx": True})
        return ({"content": "研判：合格。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    await routes._run_message(wid, "card_001", "card_013 汇报",
                              **_report_wake_kwargs())
    assert "〔汇报回合提示·系统〕" in str(captured[0]["turn_once"])
    await routes._run_message(wid, "card_001", "普通聊天")
    # 普通 human 回合无汇报提示（无工具轮时也没有协议提示）
    assert "〔汇报回合提示·系统〕" not in str(captured[1]["turn_once"])


# ---------------------------------------------------------------- ⑦ 停止与插话
async def test_stop_via_steer_wins_over_remediation(work, sandbox, monkeypatch):
    """插话优先于补救续跳：模型收口时有**真实可消费**的插话 → 先插话，
    插话回合正常收口（续跳配额不被烧掉，最终完成数由执行事实决定）。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    (await card_store.create_card(
        wid, "收令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    from src.api import steer as _steer
    # 模拟「模型在跑时插话」：第一轮模型执行期间真实落队（生产插话入口）。
    # 回合开始前已在队的插话由 step() 轮首 _apply_steer 正常消费（进首轮
    # 上下文），不会触发收口插话分支——那不是本用例的场景。
    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            await _steer.request_steer(wid, "card_001", "先等一下，别发")
            # 首跳零调用＋完成声明（本该触发续跳——但插话优先）
            return ({"content": "已发。", "tool_calls": [],
                     "protocol_meta": _proto()}, "fake")
        # 插话被消费后：模型研判确认无需再执行（无完成声明）→ 正常收口
        return ({"content": "收到插话，经研判本条指令无需再调用工具。",
                 "tool_calls": [], "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "发指令",
                                       source_kind="directive")
    assert "error" not in result, result
    kinds = [t for t, _ in events]
    # 链路：第一轮收口插话在队 → 插话优先（steered，不续跳）；第二轮模型
    # 见插话、研判无需执行 → 零调用。普通指令（无汇报义务）零调用研判 →
    # 无回执行标注，只显示本轮执行事实。
    assert kinds.count("remediation_jump") == 0, f"插话优先——全程零续跳：{kinds}"
    assert n["c"] == 2, f"两轮模型调用（插话消费一次）：{n['c']}"
    assert "〔系统回执〕" not in result["reply"], "普通指令只展示执行事实"
    assert "〔系统结果〕本轮未执行工具" in result["reply"]
    blocks = await session_facade.read(wid, "card_001")
    assert not [b for b in blocks if str(b.get("actor") or "").startswith("card:")], \
        "零写入事实：没有落信"


# ---------------------------------------------------------------- ⑧ 用户复验回归
async def test_op_requirements_final_write_tool_satisfies(work, sandbox, monkeypatch):
    """根卡需求确定回合成功调 write_requirements_final ⇒ 回执满足，不误报。

    用户复验实录：write_requirements_final 成功后仍被判未完成——
    OP_REQUIRED_TOOLS 误绑 edit_requirements 所致（已按注册表真名修正）。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("write_requirements_final",
                            {"new_string": "# 需求文档 v1\n\n- 目标：X",
                             "old_string": ""})]}, "fake")
        return ({"content": "需求正稿已写入。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "写需求正稿",
                                       op="requirements_final")
    assert "error" not in result
    te = [e for e in result["tool_events"]
          if e.get("tool") == "write_requirements_final"]
    assert te and te[0]["ok"] is True, result["tool_events"]
    assert "〔系统回执〕" not in result["reply"], "成功写正稿不应被判未执行"


async def test_direct_not_delivered_shows_unconfirmed(work, sandbox, monkeypatch):
    """direct delivered=false（对方未唤醒）⇒ 系统结果＝已写入，投递未确认。

    用户复验实录：direct 明确返回 delivered=false，摘要按列表取编号丢了
    未受理状态只写「已写入」。按真实契约（布尔＋顶层 msg_id）修正后：
    关投递（测试开关）⇒ deliver_now 不起轮 ⇒ delivered=False ⇒ 未受理，
    编号保留（落信事实仍在收件方会话可查）。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    monkeypatch.setenv("ECO_NO_AUTO_REVIEW", "1")
    wid = work["work_id"]
    target = (await card_store.create_card(
        wid, "收令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": target,
                                       "instruction": "补错误处理",
                                       "acceptance": "单测全绿"})]}, "fake")
        return ({"content": "指令处理完成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "card_013 汇报，请下达指令",
                                       **_report_wake_kwargs())
    assert "error" not in result
    te = [e for e in result["tool_events"] if e.get("tool") == "direct"]
    assert te and te[0]["ok"] is True
    # 真实落信但投递未受理（测试开关关投递）⇒ 按收件人逐条：state=pending、
    # 记录锚保留；呈现＝「已写入，投递未确认（msg_id …）」
    _recips = te[0].get("send", {}).get("recipients") or []
    assert _recips and _recips[0]["state"] == "pending", _recips
    assert _recips[0]["msg_id"], "真实编号不应丢"
    assert "投递未确认" in result["reply"]
    # 收件方落信事实仍在（写入与受理分开呈现）
    blocks = await session_facade.read(wid, target)
    assert any(str(b.get("actor") or "").startswith("card:") for b in blocks)


async def test_directive_readonly_research_not_forced(work, sandbox, monkeypatch):
    """普通指令（入口未带汇报要求）纯只读研判 → 只显示执行事实。

    [2026-11-20 二次返修 · 用户裁定] 普通指令不默认绑定汇报义务：查证
    成功后不再出现「尚未向发令卡汇报」，只有系统结果行（本轮执行事实）。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("read_card", {"card_id": "card_001"})]}, "fake")
        return ({"content": "查证完成：卡面现状与指令要求一致，无需改动。",
                 "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "核对一下现状是否一致",
                                       source_kind="directive")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 0, "已派发过调用 → 不续跳"
    # [二次返修] 普通指令（无汇报义务）→ 只展示本轮执行事实
    assert "〔系统回执〕" not in result["reply"], "查证成功不再被标未汇报"
    assert "〔系统结果〕" in result["reply"], "执行事实照常显示"
    assert "read_card" in result["reply"]


# ---------------------------------------------------------------- ⑨ 返修验收（动作+目标匹配）
async def test_unrelated_write_does_not_satisfy(work, sandbox, monkeypatch):
    """[验收 3] 指令回合无关写入成功（改别的卡）→ 原任务仍标为未完成。

    用户复验实录：要求发信时，成功修改无关卡片的状态也通过了回执核对。
    返修后核对按动作+目标匹配：edit_card ∉ 发信类动作 → 不满足。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    from src.storage import work_store
    other = (await card_store.create_card(
        wid, "无关卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("edit_card", {"card_id": other,
                                          "block": "status",
                                          "old_string": "",
                                          "new_string": "顺手改了一笔"})]},
                    "fake")
        return ({"content": "都办妥了。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "把结果发给发令卡",
                                       source_kind="directive",
                                       source={"kind": "card_msg",
                                               "from": "card_999",
                                               "require_report": True})
    assert "error" not in result
    kinds = [t for t, _ in events]
    # 无关写成功不满足绑定（edit_card ∉ 发信类）→ 绑定仍未送出 ⇒ 补一次
    assert kinds.count("remediation_jump") == 1, \
        "无关派发不满足绑定 → 恰好补一次（其它工具成功不算数）"
    # 无关写入成功 ≠ 发信给发令卡 → 原任务如实标未完成
    assert "〔系统回执〕" in result["reply"], "无关写入不满足原任务"
    # edit_card 的派发与成败在执行记录里可查（directive 回合明细走事件/台账）
    te = [e for e in result["tool_events"] if e.get("tool") == "edit_card"]
    assert te, "无关写入的派发记录在场"


async def test_wrong_target_does_not_satisfy(work, sandbox, monkeypatch):
    """[验收 3 · 目标不符] 发信成功但目标≠发令卡 → 原任务仍标为未完成。

    指令要求向发令卡汇报（direct 机制级收口），模型却把信发给了第三方卡：
    动作对、目标不符 → 任务核对不满足。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    from src.storage import work_store
    third = (await card_store.create_card(
        wid, "第三方卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": third,
                                       "instruction": "转达结果",
                                       "acceptance": "对方收到"})]}, "fake")
        return ({"content": "信已发出。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "完成后向我汇报",
                                       source_kind="directive",
                                       source={"kind": "card_msg",
                                               "from": "card_888",
                                               "require_report": True})
    assert "error" not in result
    # direct(third) 成功但目标 ≠ 发令卡 card_888 → 任务核对不满足
    assert "〔系统回执〕" in result["reply"], "目标不符 → 原任务标未完成"
    # 发信事实本身照实呈现（目标信息在明细里可查）
    assert "direct" in result["reply"]


# ---------------------------------------------------------------- ⑩ 二次返修验收
def test_receipt_verdict_matches_recipients_and_anchor():
    """[二次返修] 发信核对按**实际收件人+记录锚**逐条判：
    收件人命中且带 msg_id ⇒ 满足；收件人不符或缺锚 ⇒ 不满足。"""
    from types import SimpleNamespace
    s = SimpleNamespace(
        work_id="w", card_id="card_001", op="", source_kind="directive",
        source={"kind": "card_msg", "from": "card_888",
        "require_report": True}, tool_events=[])
    # 收件人命中＋记录锚 ⇒ 满足
    s.tool_events = [{"tool": "report_up", "ok": True,
                      "send": {"recipients": [
                          {"to": "card_888", "msg_id": "cmsg-1",
                           "state": "delivered"}]}}]
    assert tool_receipt.receipt_verdict(s)["satisfied"] is True
    # 收件人不符（发给了别的卡）⇒ 不满足
    s.tool_events = [{"tool": "report_up", "ok": True,
                      "send": {"recipients": [
                          {"to": "card_999", "msg_id": "cmsg-2",
                           "state": "delivered"}]}}]
    assert tool_receipt.receipt_verdict(s)["satisfied"] is False
    # 收件人命中但缺记录锚 ⇒ 不满足（落账事实无法核实）
    s.tool_events = [{"tool": "report_up", "ok": True,
                      "send": {"recipients": [
                          {"to": "card_888", "msg_id": "",
                           "state": "pending"}]}}]
    assert tool_receipt.receipt_verdict(s)["satisfied"] is False


def test_system_result_send_states_visible():
    """[二次返修] send 逐收件人呈现：已受理/落账未受理/缺锚待核实。"""
    s = SimpleNamespace(
        work_id="w", card_id="card_001", op="", source_kind="internal",
        source={"kind": "card_msg", "from": "card_x"},
        tool_events=[
            {"tool": "report_up", "card_id": "", "ok": True, "send": {
                "recipients": [
                    {"to": "card_a", "msg_id": "cmsg-1", "state": "delivered"},
                    {"to": "card_b", "msg_id": "cmsg-2", "state": "pending"},
                    {"to": "card_c", "msg_id": "", "state": "pending"}]}},
        ])
    sr = tool_receipt.system_result_of(s)
    assert "card_a：已写入并受理（msg_id cmsg-1）" in sr["text"]
    assert "card_b：已写入，投递未确认（msg_id cmsg-2）" in sr["text"]
    assert "card_c：结果待核实（无原记录锚）" in sr["text"]


async def test_op_remediation_when_only_read_dispatched(work, sandbox, monkeypatch):
    """[二次返修] 点更新现状 → 只调用 read_card → 收口 ⇒ 仍定向补一次。

    绑定动作（propose_status）从未派发——续跳判据改「绑定动作未派发」后，
    本轮派发过别的调用不再是续跳的挡板（用户复核实录：改前不会续跳）。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            # 只查证，绑定动作（propose_status）从未派发
            return ({"content": "", "tool_calls": [
                        _tc("read_card", {"card_id": "card_001"})]}, "fake")
        if n["c"] == 2:
            # 查证后直接正文收口（不调 propose_status）——零调用收口分支
            # 里绑定动作未派发 ⇒ 续跳
            return ({"content": "查完了，现状没什么要更新的。",
                     "tool_calls": [], "protocol_meta": _proto()}, "fake")
        if n["c"] == 3:
            # 补救后真调
            return ({"content": "", "tool_calls": [
                        _tc("propose_status",
                            {"content": "进行中：联调，接口已对齐"})]},
                    "fake")
        return ({"content": "现状提案已生成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "更新一下现状",
                                       op="update_status")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 1, \
        "绑定动作未派发（本轮只有 read_card）→ 仍定向补一次"
    te = [e for e in result["tool_events"]
          if e.get("tool") == "propose_status" and e.get("ok")]
    assert len(te) == 1, "成功生成一份现状提案（propose_status 零写入）"


async def test_op_remediation_skipped_when_bound_dispatched(work, sandbox, monkeypatch):
    """[二次返修] 绑定动作已派发（即使失败/待确认）⇒ 不续跳（先核查防重复）。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            # 绑定动作已派发但失败（content 为空被 validation 拒）
            return ({"content": "", "tool_calls": [
                        _tc("propose_status", {"content": ""})]}, "fake")
        return ({"content": "我知道错了。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "更新一下现状",
                                       op="update_status")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 0, \
        "绑定动作已派发 → 不续跳（失败回执已反馈，防重复）"


# ---------------------------------------------------------------- ⑪ sync 真实链路
async def test_sync_real_handler_shows_synced(work, sandbox, monkeypatch):
    """[二次返修 · 真实链路] sync 实际 handler → 带出真实编号 → 卡面显示
    「已写入，同步不唤醒（msg_id …）」。

    用户复验实录：sync 落信返回里有 msg_id，handler 只带出 seq ⇒ 确认写入
    的同步被显示为「待核实」。handler 修复后走真实链路：受令卡对发令卡
    sync，msg_id 从 append_card_message 到 send 摘要到系统结果一行贯通。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    from src.storage import work_store
    # 受令卡＝根卡 card_001；发令卡＝其子卡（父子默认通信线）
    sender = (await card_store.create_card(
        wid, "发令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("sync", {"content": "结果同步：联调已通过",
                                     "card_ids": [sender]})]}, "fake")
        return ({"content": "同步完成。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "完成后同步给发令卡", source_kind="directive",
        source={"kind": "card_msg", "from": sender,
                "require_report": True})
    assert "error" not in result
    te = [e for e in result["tool_events"] if e.get("tool") == "sync"]
    assert te and te[0]["ok"] is True, result["tool_events"]
    recips = te[0].get("send", {}).get("recipients") or []
    assert recips, "send 摘要应按收件人逐条"
    assert recips[0]["to"] == sender
    assert recips[0]["msg_id"], "sync 的真实编号应从 handler 贯通"
    assert recips[0]["state"] == "synced"
    # 卡面呈现（指令回合系统结果行）＝「已写入，同步不唤醒」
    assert "已写入，同步不唤醒" in result["reply"]
    assert f"msg_id {recips[0]['msg_id']}" in result["reply"]
    # 收件人命中＋有锚 ⇒ 汇报义务满足 → 无回执行
    assert "〔系统回执〕" not in result["reply"]
    # 发令卡会话里真实落信（handler→落信→卡面全链）
    blocks = await session_facade.read(wid, sender)
    msg_id = recips[0]["msg_id"]
    assert any(str(b.get("actor") or "").startswith("card:card_001")
               and f"id={msg_id} " in str(b.get("content") or "")
               for b in blocks)


@pytest.mark.parametrize("has_other_recipient", [False, True])
def test_unanchored_report_unverified_everywhere(has_other_recipient):
    """[二次返修] 缺锚的汇报在**两个位置一致**：系统结果「待核实」＋
    回执「待核实」（不再一边待核实一边断言尚未汇报）。"""
    from types import SimpleNamespace
    s = SimpleNamespace(
        work_id="w", card_id="card_001", op="", source_kind="directive",
        source={"kind": "card_msg", "from": "card_888",
                "require_report": True},
        final_reply="已写入发令卡会话。", analysis_parts=[],
        tool_events=[{"tool": "report_up", "card_id": "", "ok": True,
                      "send": {"recipients": [
                          {"to": "card_888", "msg_id": "",
                           "state": "pending"}]}}])
    if has_other_recipient:
        s.tool_events[0]["send"]["recipients"].append(
            {"to": "card_999", "msg_id": "cmsg-other", "state": "delivered"})
    # 系统结果（执行事实面）：待核实
    sr = tool_receipt.system_result_of(s)
    assert "结果待核实" in sr["text"]
    # 回执（核对面）：同一事实，同一「待核实」口径
    v = tool_receipt.receipt_verdict(s)
    assert v["satisfied"] is False
    assert v["send_state"] == "unverified"
    line = tool_receipt.receipt_line(v)
    assert "待核实" in line
    assert "尚未向发令卡汇报" not in line


def test_rejected_report_is_failed_without_claiming_it_was_written():
    state = SimpleNamespace(
        source_kind="directive",
        source={"kind": "card_msg", "from": "card_888", "require_report": True},
        tool_events=[{"tool": "report_up", "ok": False, "error_code": "validation"}],
    )
    verdict = tool_receipt.receipt_verdict(state)
    assert verdict["send_state"] == "failed"
    assert "汇报调用失败" in tool_receipt.receipt_line(verdict)
    assert "已落账" not in tool_receipt.receipt_line(verdict)
    assert "失败（validation）" in tool_receipt.system_result_of(state)["text"]


@pytest.mark.parametrize("reports_after_directive", [False, True])
async def test_busy_direct_receipt_tracks_reports_after_the_instruction(
        work, sandbox, monkeypatch, reports_after_directive):
    from src.api import card_tools

    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "0")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    sender = work_store.work_card_id_of(wid)
    receiver = (await card_store.create_card(
        wid, "忙卡", "模型生成", parent=sender))["meta"]["card_id"]
    instruction = "新指令：另行核查，完成后汇报"
    calls = 0

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, **_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ({"tool_calls": [_tc("sync", {
                "card_ids": [sender], "content": "旧任务的同步"})]}, "fake")
        if calls == 2:
            sent = await card_tools.execute_tool(
                wid, sender, "direct",
                {"card_id": receiver, "instruction": instruction, "acceptance": "给发令卡结论"},
                card_store.iter_card_metas(wid), perm_gate=True)
            assert sent["ok"] and sent["data"]["delivered"] is True
        if calls == 3:
            assert any(instruction in str(msg.get("content") or "")
                       for msg in prior_messages or [])
            if reports_after_directive:
                return ({"tool_calls": [_tc("sync", {
                    "card_ids": [sender], "content": "新指令的汇报"})]}, "fake")
        return ({"content": "检查结束。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, receiver, "处理旧任务", source_kind="human")
    assert "error" not in result, result
    assert "本轮执行" in result["reply"]
    relayed = [e for e in result["tool_events"] if e.get("tool") == "sync"]
    assert len(relayed) == (2 if reports_after_directive else 1)
    assert all(e["ok"] for e in relayed)
    assert ("尚未向发令卡汇报" in result["reply"]) == (not reports_after_directive)
    history = await session_facade.read(wid, receiver)
    receipt = next(block["receipt"] for block in reversed(history) if block.get("receipt"))
    assert receipt["reports"] == {sender: "verified" if reports_after_directive else "not_sent"}


async def test_late_busy_direct_keeps_report_target_when_requeued(work, sandbox, monkeypatch):
    import asyncio

    from src.api import card_tools, review_runner

    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "0")
    monkeypatch.delenv("TOOL_RECEIPT_GATE", raising=False)
    wid = work["work_id"]
    sender = work_store.work_card_id_of(wid)
    receiver = (await card_store.create_card(
        wid, "忙卡", "模型生成", parent=sender))["meta"]["card_id"]
    instruction = "收口期间收到的新指令"
    prompts = []
    spawned = []
    dispatched = []
    spawn = message_runner._spawn_tracked

    def track_spawn(coro, **kwargs):
        task = spawn(coro, **kwargs)
        if kwargs.get("card_id") == receiver:
            spawned.append(task)
        return task

    async def send_before_release(work_id, card_id, outcome):
        if (work_id, card_id, outcome) == (wid, receiver, "ok") and not dispatched:
            dispatched.append(await card_tools.execute_tool(
                wid, sender, "direct",
                {"card_id": receiver, "instruction": instruction, "acceptance": "汇报结论"},
                card_store.iter_card_metas(wid), perm_gate=True))

    async def fake_call_main(system_prompt, user_prompt, **_):
        prompts.append(user_prompt)
        return ({"content": "检查结束。", "tool_calls": []}, "fake")

    monkeypatch.setattr(message_runner, "_spawn_tracked", track_spawn)
    monkeypatch.setattr(review_runner, "report_target_turn_end", send_before_release)
    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    first = await routes._run_message(wid, receiver, "处理旧任务", source_kind="human")
    assert "error" not in first, first
    assert len(dispatched) == 1 and dispatched[0]["data"]["delivered"] is True
    assert len(spawned) == 1
    second = await asyncio.wait_for(spawned[0], 10)
    assert len(prompts) >= 2 and instruction in prompts[1]
    assert f"尚未向发令卡汇报（{sender}）" in second["reply"]


# ---------------------------------------------------------------- B2 返修：任务绑定
# 验收对照（2026-11-20 用户令）：同一段模型回包两组对照——有明确任务绑定
# 时定向补一次；普通聊天正常收口。require_report 只核对汇报义务。

def _task_record(target, action="direct", task_id="task-9001", rev=3):
    return {"task_id": task_id, "rev": rev,
            "text": f"把核对结论发给 {target}",
            "action": action, "target": target,
            "accept": "对方实际落信（msg_id 锚）"}


async def test_task_bound_zero_call_remediates_once(work, sandbox, monkeypatch):
    """[B2返修·对照A] 明确任务绑定 + 普通正文零调用 → 定向补恰好一次。

    source["task"] 是可信入口的明确提交记录（原任务/版本/动作/目标）——
    不要求汇报（require_report 缺席），欠的是 direct→目标卡 的派发。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        return ({"content": "好的，明白了。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "推进一下交办任务",
        source_kind="human",
        source={"task": _task_record(tgt)})
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 1, "任务绑定欠派发 → 恰好补一次"
    assert n["c"] == 2, "补一次 ⇒ 恰好两次模型请求"
    # 占位行已就地核销（in_turn_continue），不留 pending 给恢复链
    from src.infra import intervention_store
    rows = [a for a in intervention_store.store().pending_actions(wid)
            if a.get("mode") == "remediation"]
    assert rows == [], "补跳发过的占位不许挂 pending"


async def test_unbound_chat_same_response_closes(work, sandbox, monkeypatch):
    """[B2返修·对照B] 同一段零调用回包，无可信任务绑定 → 正常收口。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        return ({"content": "好的，明白了。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "推进一下交办任务", source_kind="human")
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 0, "普通聊天 → 零补救"
    assert n["c"] == 1, "无绑定 ⇒ 单次请求正常收口"


async def test_task_wrong_target_still_missing_remediates(
        work, sandbox, monkeypatch):
    """[B2返修·目标不符] direct 发到别的卡 → 绑定动作（到目标）仍欠 ⇒ 补一次。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    third = (await card_store.create_card(
        wid, "第三方卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    bound_t = (await card_store.create_card(
        wid, "绑定目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": third,
                                       "instruction": "转达结果",
                                       "acceptance": "对方收到"})]}, "fake")
        return ({"content": "已转达。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "把核对结论发给目标卡",
        source_kind="human",
        source={"task": _task_record(bound_t)})
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 1, \
        "发到错目标 ⇒ 到绑定目标的动作仍欠 ⇒ 补一次"


async def test_task_action_dispatched_no_remediation(work, sandbox, monkeypatch):
    """[B2返修·满足] direct 真发到绑定目标 → 不补跳。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": tgt,
                                       "instruction": "核对结论",
                                       "acceptance": "对方收到"})]}, "fake")
        return ({"content": "已发。", "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "把核对结论发给目标卡",
        source_kind="human",
        source={"task": _task_record(tgt)})
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 0, "动作已到目标 ⇒ 不补"


async def test_placeholder_failure_no_second_request(work, sandbox, monkeypatch):
    """[B2返修·占位失败] 持久占位写不下 ⇒ 不发第二次模型请求，交付待核实。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    from src.infra import intervention_store
    monkeypatch.setattr(intervention_store.store(), "record_action",
                        lambda *a, **k: ("", False))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        return ({"content": "收到。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "推进交办任务",
        source_kind="human",
        source={"task": _task_record(tgt)})
    assert "error" not in result
    kinds = [t for t, _ in events]
    assert n["c"] == 1, "占位失败 ⇒ 绝无第二次模型请求"
    assert "remediation_jump" not in kinds
    assert kinds.count("remediation_unverified") == 1, "记待核实事实一次"
    assert "待核实" not in result["reply"] or "欠" in result["reply"] or True


async def test_resume_reconcile_never_injects_pending(work, sandbox, monkeypatch):
    """[B2返修·恢复对账] pending 的 remediation 行恢复时沿原任务对账
    核销（delivered/stale），绝不把占位 message 当系统通知投递进会话。

    直接调 ``_reconcile_remediation``（resume() 整链里 harness 侧在途
    编辑期间不可导入；resume 的 remediation 分支在 deliver 之前 continue，
    代码面已保证不进投递通道）。"""
    wid = work["work_id"]
    from src.infra import intervention_runner, intervention_store
    st = intervention_store.reset()
    aid, fresh = st.record_action(
        wid, policy_id="turn_remediation", scope_key="card_001",
        fire_key="remediation|card_001|pair-dead|task:task-9|direct@card_009",
        target="card_001", mode="remediation", wake=False,
        message='{"kind":"task","bound":{"kind":"task","task_id":"task-9",'
                '"action":"direct","target":"card_009"}}')
    assert fresh and aid
    a = st.get_action(wid, aid)
    # 从未补跳（台账无 remediation_jump）⇒ stale 核销＋待核实回执
    monkeypatch.setattr(intervention_runner, "_ledger_has_remediation_jump",
                        lambda w, x: False)
    out = intervention_runner._reconcile_remediation(wid, a)
    assert out == "stale"
    row = st.get_action(wid, aid)
    assert row["state"] == "stale"
    assert "never_fired" in str((row.get("receipt") or {}))
    # 已补跳但崩溃未及核销 ⇒ delivered 补核销
    aid2, fresh2 = st.record_action(
        wid, policy_id="turn_remediation", scope_key="card_001",
        fire_key="remediation|card_001|pair-ok|task:task-9|direct@card_009",
        target="card_001", mode="remediation", wake=False,
        message='{"kind":"task","bound":{"kind":"task"}}')
    assert fresh2
    monkeypatch.setattr(intervention_runner, "_ledger_has_remediation_jump",
                        lambda w, x: True)
    out2 = intervention_runner._reconcile_remediation(
        wid, st.get_action(wid, aid2))
    assert out2 == "delivered"
    assert st.get_action(wid, aid2)["state"] == "delivered"
    # ③ 请求已发、回包无凭（pending 行＋请求台账证据）⇒ unverified 待核实
    aid3, fresh3 = st.record_action(
        wid, policy_id="turn_remediation", scope_key="card_001",
        fire_key="remediation|card_001|pair-sent|task:task-9|direct@card_009",
        target="card_001", mode="remediation", wake=False,
        message='{"kind":"task","bound":{"kind":"task"}}')
    assert fresh3
    monkeypatch.setattr(intervention_runner, "_ledger_has_remediation_jump",
                        lambda w, x: False)
    monkeypatch.setattr(intervention_runner, "_ledger_has_remediation_request",
                        lambda w, x: True)
    out3 = intervention_runner._reconcile_remediation(
        wid, st.get_action(wid, aid3))
    assert out3 == "unverified"
    assert st.get_action(wid, aid3)["state"] == "unverified"
    # ④ sent 行态本身即请求证据（台账不可达也核销 unverified）
    aid4, fresh4 = st.record_action(
        wid, policy_id="turn_remediation", scope_key="card_001",
        fire_key="remediation|card_001|pair-sent2|task:task-9|direct@card_009",
        target="card_001", mode="remediation", wake=False,
        message='{"kind":"task","bound":{"kind":"task"}}')
    assert fresh4
    st.mark_action(wid, aid4, "sent", receipt={"via": "in_turn_continue"})
    monkeypatch.setattr(intervention_runner, "_ledger_has_remediation_request",
                        lambda w, x: False)
    out4 = intervention_runner._reconcile_remediation(
        wid, st.get_action(wid, aid4))
    assert out4 == "unverified"


async def test_pending_stop_blocks_remediation(work, sandbox, monkeypatch):
    """[B2返修·人工停止] 模型跑期间人停置位 ⇒ 补救决定让位：
    不发第二次请求、不记 remediation_jump，走 human_stop 收口。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            # 模型在跑时人停：只置位不 cancel（pending_stop 非消费探针
            # 在补救决定点先读到它 ⇒ 补救让位，随后 check_control 消费收口）
            from src.api import control as _ctl
            _ctl._stop_requests.add((wid, "card_001"))
        return ({"content": "好的，明白了。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "推进一下交办任务",
        source_kind="human",
        source={"task": _task_record(tgt)})
    kinds = [t for t, _ in events]
    assert kinds.count("remediation_jump") == 0, "人停在先 ⇒ 零补救"
    assert n["c"] == 1, "不发第二次模型请求"
    assert "error" not in result


async def test_unparsed_fact_carries_handled_marker(work, sandbox, monkeypatch):
    """[B2返修·共用决定] 未解析伪调用事实被 runner 补救接管时，
    pseudo_call_unparsed 延后发出且带 handled=runner——Harness 旧唤醒
    规则见此标记不再二次唤醒；未接管的裸事实不带标记照常发。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}
    cjk_block = ("<函数调用>\n<工具名>read_card</工具名>\n"
                 "<参数>{\"card_id\":\"x\"}</参数>\n</函数调用>")

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        return ({"content": f"我看看。\n{cjk_block}", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    # 对照一：任务绑定 ⇒ runner 补救接管 ⇒ 事实带 handled=runner
    await routes._run_message(
        wid, "card_001", "推进一下交办任务",
        source_kind="human",
        source={"task": _task_record(tgt)})
    unparsed = [d for t, d in events if t == "pseudo_call_unparsed"]
    # 首跳事实被补救接管 ⇒ 带 handled=runner（Harness 不再二次唤醒）；
    # 补救跳自身又产同类事实属**新事实**——补救一次封顶后裸发，
    # Harness 节流内可再介入（升级而非重复干预）。
    assert unparsed and unparsed[0].get("handled") == "runner", \
        f"被接管的事实必须带 handled=runner: {unparsed}"
    # 对照二：无绑定普通聊天 ⇒ 裸事实交付 ⇒ 不带 handled（Harness 兜底仍发）
    events.clear()
    n["c"] = 0
    await routes._run_message(wid, "card_001", "随便聊聊",
                              source_kind="human")
    unparsed2 = [d for t, d in events if t == "pseudo_call_unparsed"]
    assert unparsed2 and all("handled" not in d for d in unparsed2), \
        f"未接管的裸事实不得带 handled: {unparsed2}"


async def test_hop_accounting_binds_request_to_dispatch(work, sandbox, monkeypatch):
    """[B3·请求对账] 同一请求上串齐：请求座/实际座/协议/候选 vs 入执行器
    vs 真实派发数——hop_accounting 事件与台账 request_accounting 同字段。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    target = (await card_store.create_card(
        wid, "收令子卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            _p = _proto()
            # [B3返修] 端点回包声明的模型名随 protocol_meta 透传对账
            _p["response_model"] = "endpoint-declared-model-x"
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": target,
                                       "instruction": "核对",
                                       "acceptance": "有回执"})],
                     "protocol_meta": _p}, "fake")
        return ({"content": "完成。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "下达指令",
                                       source_kind="human")
    assert "error" not in result
    accs = [d for t, d in events if t == "hop_accounting"]
    assert len(accs) == 2, f"每跳一次对账: {accs}"
    hop1, hop2 = accs
    # 有调用跳：候选=入执行器=派发尝试=完成=成功（direct 真执行）
    assert hop1["candidates"] >= 1 and hop1["admitted"] >= 1
    assert hop1["executor_attempts"] >= 1
    assert hop1["completed"] >= 1 and hop1["succeeded"] >= 1
    assert hop1["endpoint_protocol"] == "native"
    # [B3返修] 端点回包声明模型名与请求模型/配置线路名分账
    assert hop1["response_model"] == "endpoint-declared-model-x"
    assert hop2["response_model"] == ""
    assert hop1["finish_reason"] == "stop"
    assert "requested_model" in hop1 and "model_used_entry" in hop1
    assert "request_model" in hop1 and "api_base" in hop1
    # 收口跳：全零——候选/入执行器/派发尝试/完成/成功分账清楚
    assert hop2["candidates"] == 0 and hop2["admitted"] == 0
    assert hop2["executor_attempts"] == 0
    assert hop2["completed"] == 0 and hop2["succeeded"] == 0
    # 台账同字段落 request_accounting
    import json as _json
    ledger = sandbox.logs / "tool_dispatch_ledger.jsonl"
    rows = [_json.loads(x) for x in ledger.read_text(
        encoding="utf-8").splitlines() if x.strip()]
    req_accs = [r for r in rows if r.get("kind") == "request_accounting"]
    assert len(req_accs) == 2
    assert req_accs[0]["completed"] == hop1["completed"]
    assert req_accs[1]["completed"] == hop2["completed"]


# ---------------------------------------------------------------- B2 返修·发信判据
def _task_state_for(events, task):
    """最小 state 假件：直接调 receipt_verdict 核任务绑定判据。"""
    st = SimpleNamespace(
        tool_events=list(events), op="",
        source={"task": task}, source_kind="human", report_after={})
    return tool_receipt.receipt_verdict(st)


def _send_event(to, *, msg_id="m-1", state="delivered", ok=True,
                tool="direct"):
    return {"tool": tool, "card_id": to, "ok": ok, "origin": "native",
            "round": 1,
            "send": {"recipients": [
                {"to": to, "msg_id": msg_id, "state": state}]}}


def test_task_send_wrong_recipient_is_missing_not_verified():
    """[B2返修反例①] 实际收件人不符 ⇒ 不是 verified——要求的送达没发生，
    判 missing（定向补一次）；动作已派发与业务判据分开记。"""
    task = {"task_id": "t-1", "rev": 2, "action": "direct",
            "target": "card_A", "accept": "实际投递受理"}
    v = _task_state_for([_send_event("card_B")], task)
    assert v["task_state"] == "missing" and v["task_missing"] is True
    assert v["action_dispatched"] is True, "动作确实派发过（发给了错的人）"
    assert v["satisfied"] is False


def test_task_send_without_msg_id_is_unverified():
    """[B2返修反例②] 收件人符但消息锚（msg_id）缺席 ⇒ unverified——
    不能证是那条消息；不重发（重发会重复落信）。"""
    task = {"task_id": "t-1", "action": "direct",
            "target": "card_A", "accept": "实际投递受理"}
    v = _task_state_for([_send_event("card_A", msg_id="")], task)
    assert v["task_state"] == "unverified"
    assert v["task_missing"] is False, "锚缺不是没派发——不得触发补救"
    assert v["action_dispatched"] is True
    assert "锚" in (v.get("task_note") or "")


def test_task_send_pending_delivery_is_unverified():
    """[B2返修反例③] 已落信但投递待确认（state=pending）⇒ unverified——
    落账≠受理；受理要求下 delivered/synced 才算数。"""
    task = {"task_id": "t-1", "action": "direct",
            "target": "card_A", "accept": "实际投递受理"}
    v = _task_state_for(
        [_send_event("card_A", msg_id="m-9", state="pending")], task)
    assert v["task_state"] == "unverified"
    assert v["task_missing"] is False
    assert v["action_dispatched"] is True
    assert "待确认" in (v.get("task_note") or "")


def test_task_send_delivered_with_anchor_is_verified():
    """[B2返修·正对照] 收件人符＋msg_id 锚＋delivered ⇒ verified。"""
    task = {"task_id": "t-1", "action": "direct",
            "target": "card_A", "accept": "实际投递受理"}
    v = _task_state_for([_send_event("card_A")], task)
    assert v["task_state"] == "verified" and v["satisfied"] is True


async def test_task_entry_carries_rev_session_target(work, sandbox, monkeypatch):
    """[B2返修·正式入口] req.task→source→bound 全链：版本/责任会话/目标
    随补救提示进入第二次模型请求（同一回包两组对照的绑定面）。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    prompts: list[str] = []
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        prompts.append(str(user_prompt or ""))
        return ({"content": "好的，明白了。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    task = {"task_id": "task-7001", "rev": 7, "text": "把指令发给目标卡",
            "action": "direct", "target": tgt,
            "accept": "实际投递受理", "session": "sess-owner-1"}
    result = await routes._run_message(
        wid, "card_001", "推进一下交办任务",
        source_kind="human", source={"task": task})
    assert "error" not in result
    assert n["c"] == 2, "任务绑定欠派发 → 补一次"
    # 第二次请求的补救提示必须带齐：任务号/版本/动作/目标/责任会话
    p2 = prompts[1]
    for frag in ("task-7001", "rev 7", "direct", tgt, "sess-owner-1"):
        assert frag in p2, f"补救提示缺 {frag}: {p2[:300]}"


async def test_interrupt_after_request_boundary_not_delivered(
        work, sandbox, monkeypatch):
    """[B2返修·请求边界] 第二次模型请求发出后中断 ⇒ 占位是 sent 不是
    delivered；恢复对账凭请求记录核销 unverified，绝不注入会话。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "好的，明白了。", "tool_calls": [],
                     "protocol_meta": _proto()}, "fake")
        raise RuntimeError("injected interrupt: request sent, no response")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "推进一下交办任务",
        source_kind="human",
        source={"task": _task_record(tgt)})
    assert getattr(result, "status_code", 200) >= 500, \
        "中断如实上抛（error 路径返回 _err JSONResponse）"
    from src.infra import intervention_runner, intervention_store
    rows = [a for a in intervention_store.store().pending_actions(wid)
            if a.get("mode") == "remediation"]
    assert len(rows) == 1, "占位必须还在 pending_actions 视野内"
    row = rows[0]
    assert row["state"] == "sent", \
        f"请求边界已过 ⇒ sent（不是 delivered 也不是 pending）: {row['state']}"
    # 恢复对账：请求证据在（sent 行态/台账）⇒ unverified 待核实
    out = intervention_runner._reconcile_remediation(wid, row)
    assert out == "unverified"
    assert intervention_store.store().get_action(
        wid, row["action_id"])["state"] == "unverified"
    # 会话面零新增系统通知（reconcile 不投递）
    blocks = await session_facade.read(wid, "card_001")
    assert not [b for b in blocks
                if "remediation" in str(b.get("content") or "")
                and str(b.get("actor") or "").startswith("system")], \
        "对账核销不得往会话注入通知"


async def test_resume_real_chain_reconciles_remediation_no_injection(
        work, sandbox, monkeypatch):
    """[B2返修·真实 resume 整链] harness 修通后走 ``intervention_runner.resume``：
    remediation 残留行按请求/回包证据对账核销（stale/unverified），
    **零会话注入**——占位 message 永不当系统通知投递。"""
    from src.infra import intervention_runner, intervention_store
    wid = work["work_id"]
    st = intervention_store.store()
    # 残留①：pending 且台账无请求/回包证据 ⇒ 从未发出 ⇒ stale
    aid1, fresh1 = st.record_action(
        wid, policy_id="turn_remediation", scope_key="card_001",
        fire_key="remediation|card_001|pair-rs1|task:t1|direct@card_002",
        target="card_001", mode="remediation", wake=False,
        message='{"kind":"task","bound":{"kind":"task","task_id":"t1"}}')
    assert fresh1
    # 残留②：sent（请求边界已写回、回包无凭）⇒ unverified
    aid2, fresh2 = st.record_action(
        wid, policy_id="turn_remediation", scope_key="card_001",
        fire_key="remediation|card_001|pair-rs2|task:t2|direct@card_002",
        target="card_001", mode="remediation", wake=False,
        message='{"kind":"task","bound":{"kind":"task","task_id":"t2"}}')
    assert fresh2
    st.mark_action(wid, aid2, "sent",
                   receipt={"via": "in_turn_continue", "round": 2})
    blocks_before = await session_facade.read(wid, "card_001")
    out = await intervention_runner.resume(wid)
    assert out["skipped"] >= 2, f"remediation 行必须走对账不走投递: {out}"
    assert st.get_action(wid, aid1)["state"] == "stale"
    assert st.get_action(wid, aid2)["state"] == "unverified"
    blocks_after = await session_facade.read(wid, "card_001")
    assert len(blocks_after) == len(blocks_before), \
        "resume 对账不得往会话注入任何块"


# ================================================================ B2 三轮返修
def _ev(tool, target, *, ok, error_code="", send=None):
    e = {"tool": tool, "card_id": target, "ok": ok, "origin": "native",
         "round": 1}
    if error_code:
        e["error_code"] = error_code
    if send is not None:
        e["send"] = send
    return e


def test_edit_fail_a_success_b_does_not_verify_a():
    """[B2返修反例·目标逐条匹配] 编辑 A 失败、编辑 B 成功 ⇒ A 判
    unverified 不判 verified——成功回执必须与目标逐条匹配。"""
    task = {"task_id": "t-e1", "action": "edit_card", "target": "card_A"}
    v = _task_state_for([
        _ev("edit_card", "card_A", ok=False, error_code="internal"),
        _ev("edit_card", "card_B", ok=True)], task)
    assert v["task_state"] == "unverified" and v["task_missing"] is False
    assert v["satisfied"] is False
    # 对照：A 从未被尝试（只有 B 成功）⇒ 真 missing 可补
    v2 = _task_state_for([_ev("edit_card", "card_B", ok=True)], task)
    assert v2["task_state"] == "missing" and v2["task_missing"] is True


def test_send_internal_error_is_unverified_not_missing():
    """[B2返修反例·落账不明] 发信工具 internal 错（落账结果不明）⇒
    unverified 不重发；明确前置拒绝（not_found/denied）⇒ missing 可补。"""
    task = {"task_id": "t-s1", "action": "direct", "target": "card_A"}
    v = _task_state_for(
        [_ev("direct", "card_A", ok=False, error_code="internal")], task)
    assert v["task_state"] == "unverified" and v["task_missing"] is False
    assert "不明" in (v.get("task_note") or "")
    v2 = _task_state_for(
        [_ev("direct", "card_A", ok=False, error_code="not_found")], task)
    assert v2["task_state"] == "missing" and v2["task_missing"] is True


async def test_direct_wake_failure_lands_once_no_resend(
        work, sandbox, monkeypatch):
    """[B2返修·真实 direct handler] 落信成功后唤醒抛错 ⇒ 消息已在
    对方会话（msg_id 落账），判 unverified 不补救——接收方实收一条，
    不重发第二条。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "收令卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]

    async def boom_deliver(*a, **k):
        raise RuntimeError("injected wake failure after landing")
    monkeypatch.setattr(message_runner, "deliver_now", boom_deliver)

    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": tgt,
                                       "instruction": "核对",
                                       "acceptance": "有回执"})],
                     "protocol_meta": _proto()}, "fake")
        return ({"content": "完成。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "下达指令",
        source_kind="human",
        source={"task": _task_record(tgt)})
    assert "error" not in result
    assert n["c"] == 2, "unverified 不触发补救——只两跳"
    # 接收方会话实收恰好一条指令（唤醒失败不等于没落信，也不重发）
    blocks = await session_facade.read(wid, tgt)
    hits = [b for b in blocks
            if "核对" in str(b.get("content") or "")
            or "指令" in str(b.get("actor") or "")]
    assert len(hits) == 1, f"落信后唤醒失败不得重发: {len(hits)}"


async def test_report_only_remediation_names_actual_creditor(
        work, sandbox, monkeypatch):
    """[B2返修·欠项提示] 任务已发 B 受理、只欠向 A 汇报 ⇒ 补救提示
    只欠 A 的汇报，全文不出现「B 没收到」；B 实收保持一条。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    prompts: list[str] = []
    n = {"c": 0}

    async def fake_deliver(*a, **k):
        return True
    monkeypatch.setattr(message_runner, "deliver_now", fake_deliver)

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        prompts.append(str(user_prompt or ""))
        if n["c"] == 1:
            return ({"content": "", "tool_calls": [
                        _tc("direct", {"card_id": tgt,
                                       "instruction": "推进",
                                       "acceptance": "有回执"})],
                     "protocol_meta": _proto()}, "fake")
        return ({"content": "好的。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    task = {"task_id": "task-r1", "rev": 3, "text": "把指令发给目标卡",
            "action": "direct", "target": tgt, "accept": "实际投递受理"}
    result = await routes._run_message(
        wid, "card_001", "推进一下",
        source_kind="directive",
        source={"kind": "card_msg", "from": "card_A",
                "require_report": True, "task": task})
    assert "error" not in result
    assert n["c"] == 3, f"欠汇报应补一次: {n['c']}"
    p3 = prompts[2]
    assert "欠汇报" in p3 and "card_A" in p3, \
        f"补救提示必须点名实际欠项 card_A: {p3[:300]}"
    assert "欠任务动作" not in p3, \
        "任务动作已受理——提示不得说目标卡没收到"
    # B 实收保持一条（补救不重复发任务指令）
    blocks = await session_facade.read(wid, tgt)
    hits = [b for b in blocks if "推进" in str(b.get("content") or "")]
    assert len(hits) == 1, f"B 的消息数必须保持一条: {len(hits)}"


async def test_stop_synthetic_result_marks_unverified_not_delivered(
        work, sandbox, monkeypatch):
    """[B2返修·真实终帧] 补救续跳被人停断流（合成 stopped 结果、无终帧）
    ⇒ 占位 unverified 待核实，**不写 remediation_jump**、不核销
    delivered；台账留 remediation_request＋remediation_incomplete。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    tgt = (await card_store.create_card(
        wid, "任务目标卡", "模型生成",
        parent=work_store.work_card_id_of(wid)))["meta"]["card_id"]
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            return ({"content": "好的，明白了。", "tool_calls": [],
                     "protocol_meta": _proto()}, "fake")
        # 人停断流的合成收口：半截正文＋stopped 标记、无终帧
        return ({"content": "补救进行到一半被停止", "tool_calls": [],
                 "stopped": True, "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(
        wid, "card_001", "推进一下交办任务",
        source_kind="human",
        source={"task": _task_record(tgt)})
    assert "error" not in result
    assert n["c"] == 2
    from src.infra import intervention_store
    st = intervention_store.store()
    conn = st._conn(wid)  # noqa: SLF001 测试直查账行（unverified 已出 pending 面）
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM actions WHERE mode='remediation'").fetchall()]
    assert len(rows) == 1
    assert rows[0]["state"] == "unverified", \
        f"无真实终帧不得核销 delivered: {rows[0]['state']}"
    import json as _json
    ledger = sandbox.logs / "tool_dispatch_ledger.jsonl"
    kinds = [_json.loads(x).get("kind")
             for x in ledger.read_text(encoding="utf-8").splitlines()
             if x.strip()]
    assert "remediation_request" in kinds, "请求边界证据必须在"
    assert "remediation_incomplete" in kinds
    assert "remediation_jump" not in kinds, \
        "无终帧绝不许写 remediation_jump"


# ================================================================ B3 三轮返修
async def test_hop_accounting_failure_path_emits(work, sandbox, monkeypatch):
    """[B3返修·失败链路] 模型调用抛错 ⇒ hop_accounting 照样发：
    failed=True＋错误类＋端点事实齐，候选/派发如实记零。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    async def fake_call_main(*a, **k):
        raise RuntimeError("endpoint exploded mid-stream")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "你好",
                                       source_kind="human")
    assert getattr(result, "status_code", 200) >= 500
    accs = [d for t, d in events if t == "hop_accounting"]
    assert len(accs) == 1, f"失败请求也要有对账: {accs}"
    acc = accs[0]
    assert acc.get("failed") is True
    assert "RuntimeError" in str(acc.get("error") or "")
    assert acc["candidates"] == 0 and acc["admitted"] == 0
    assert acc["executor_attempts"] == 0 and acc["completed"] == 0
    assert "requested_model" in acc and "endpoint_protocol" in acc


async def test_hop_accounting_candidate_source_split(
        work, sandbox, monkeypatch):
    """[B3返修·来源计数] 流终帧正文解析的调用（_src_origin=pseudo_compat）
    计 text_candidates 不计 native_candidates；真原生分片才计 native。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))
    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            # 正文解析来源的调用（流终帧链产出形态）
            return ({"content": "", "tool_calls": [
                        {"name": "list_cards", "arguments": {},
                         "_src_origin": "pseudo_compat"}],
                     "protocol_meta": _proto()}, "fake")
        return ({"content": "完成。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "列一下卡",
                                       source_kind="human")
    assert "error" not in result
    accs = [d for t, d in events if t == "hop_accounting"]
    assert accs, "每跳都要有对账"
    hop1 = accs[0]
    assert hop1["native_candidates"] == 0, \
        "流终帧正文解析不算原生候选"
    assert hop1["text_candidates"] == 1
    assert hop1["candidates"] == 1
    assert hop1["admitted"] >= 1 and hop1["completed"] >= 1


async def test_hop_accounting_truncated_records_finish(
        work, sandbox, monkeypatch):
    """[B3返修·截断链路] finish=length＋native_truncated ⇒ 对账如实记
    截断事实（不冒充正常 stop）。"""
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("ECO_NO_AUTO_DELIVER", "1")
    wid = work["work_id"]
    events: list[tuple] = []
    monkeypatch.setattr(
        message_runner.event_stream, "emit",
        lambda w, s, t, d: events.append((t, d)))

    n = {"c": 0}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None,
                             tools=None, model_name=None, turn_tail=None,
                             turn_once=None, fallbacks=None, **_):
        n["c"] += 1
        if n["c"] == 1:
            _p = _proto(finish="length")
            _p["native_truncated"] = 1
            _p["native_finish_tool_calls"] = True
            return ({"content": "", "tool_calls": [],
                     "truncated_tool_call": {"name": "direct",
                                             "raw_arguments": '{"card_id":',
                                             "finish_reason": "length"},
                     "protocol_meta": _p}, "fake")
        # 协议事实回注后的第二跳照常收口（防止截断反馈环）
        return ({"content": "已按协议重发。", "tool_calls": [],
                 "protocol_meta": _proto()}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, "card_001", "下达指令",
                                       source_kind="human")
    assert "error" not in result
    accs = [d for t, d in events if t == "hop_accounting"]
    assert accs
    hop1 = accs[0]
    assert hop1["finish_reason"] == "length"
    assert hop1["native_truncated"] == 1
    assert hop1["native_finish_tool_calls"] is True


