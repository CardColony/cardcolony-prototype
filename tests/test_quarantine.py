# -*- coding: utf-8 -*-
"""异常正文隔离（quarantine/ctx_content）施工验收测试（2026-11-20）。

判据（方案 §3）：
- 原文留档案面：session_blocks/raw 不被改；
- 供模型使用的内容＝ctx_content（残部＋宿主状态行）：板/投影/凝练同口径；
- 同跳思考不进投影、不上板；
- 家具行剔除只用已知调用族判据（不误伤叙述行、代码示例）；
- 板路与原文回退路投影一致（重建同口径）；
- 字段过白名单持久化（reasoning_content 同族教训）。
"""
from __future__ import annotations

import json

import pytest

from src.api import context_regions, text_tool_rescue
from src.storage import (
    context_board, memory_domain, origin_store, predicates,
    review_scope, session_facade,
)


async def _append(wid, cid, blocks):
    for b in blocks:
        await session_facade.append(wid, cid, b)


_ANOMALY = ("已逐字核实裁定前提，成立。\n\n"
            "<func name=\"read_file\">\n"
            "<parameter name=\"path\">D:\\x\\a.go</parameter>\n"
            "</func>\n\n"
            "读完判定规则原文再动。\n"
            "</invoke>\n</parameter>\n")


def test_quarantine_view_strips_furniture_keeps_narrative():
    v = text_tool_rescue.quarantine_view(_ANOMALY)
    assert "已逐字核实裁定前提" in v
    assert "读完判定规则原文" in v
    assert text_tool_rescue.ANOMALY_NOTE in v
    for tok in ("<func", "</invoke", "</parameter"):
        assert tok not in v, f"家具行不得进视图: {tok}\n{v}"


def test_quarantine_view_dsml_pipe_and_funccall():
    raw = ("核实完毕。\n<funccall name=\"read_file\">\n</funccall>\n"
           "</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n尾巴保留")
    v = text_tool_rescue.quarantine_view(raw)
    assert "核实完毕" in v and "尾巴保留" in v
    for tok in ("funccall", "DSML"):
        assert tok not in v, f"变体家具不得进视图: {tok}\n{v}"


def test_quarantine_view_pure_tags_note_only():
    v = text_tool_rescue.quarantine_view("</invoke>\n</parameter>\n<func")
    assert v == text_tool_rescue.ANOMALY_NOTE


def test_quarantine_view_tail_furniture_line():
    raw = "<system_warning>警告甲</system_warning>\n" \
          "<system_warning>警告乙</｜DSML｜ parameter>"
    v = text_tool_rescue.quarantine_view(raw)
    assert "警告乙" in v and "DSML" not in v


def test_enters_context_quarantine():
    ok = {"role": "assistant", "content": "原文",
          "quarantine": "pseudo_unparsed", "ctx_content": "残部+状态行"}
    bad = {"role": "assistant", "content": "原文",
           "quarantine": "pseudo_unparsed", "ctx_content": ""}
    assert predicates.enters_context(ok) is True
    assert predicates.enters_context(bad) is False
    assert predicates.context_content(ok) == "残部+状态行"
    assert predicates.context_content({"content": "x"}) == "x"


async def test_quarantined_block_board_and_projection(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    view = text_tool_rescue.quarantine_view(_ANOMALY)
    await _append(wid, cid, [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": _ANOMALY,
         "reasoning_content": "不该回传的思考",
         "quarantine": "pseudo_unparsed", "ctx_content": view},
        {"role": "user", "content": "再问"},
        {"role": "assistant", "content": "正常答",
         "reasoning_content": "正常思考"},
    ])
    # 原文留档案面（raw 不被改）
    raw = origin_store.read_blocks(wid, cid, 0)
    assert any(b.get("content") == _ANOMALY for b in raw)
    # 板上是视图，思考已剥
    rows = context_board.read_board(wid, cid)
    qr = [r for r in rows if r["seq"] == 1][0]
    assert qr["content"] == view and qr["reasoning_content"] is None
    # 投影：视图在场、原文/思考/家具全不在
    merged = context_regions._merge_toolish_into(rows, raw)
    proj = context_regions.project_history_messages(merged, 0)
    assert any(m["content"] == view for m in proj)
    assert all("<func" not in m["content"] for m in proj)
    q = [m for m in proj if m["content"] == view][0]
    assert "reasoning_content" not in q
    n = [m for m in proj if m["content"] == "正常答"][0]
    assert n.get("reasoning_content") == "正常思考"


async def test_board_fallback_equiv_with_quarantine(sandbox, work):
    """隔离块在板路/回退路投影一致（方案：板启停及重建投影一致）。"""
    wid, cid = work["work_id"], "card_001"
    view = text_tool_rescue.quarantine_view(_ANOMALY)
    await _append(wid, cid, [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": _ANOMALY,
         "quarantine": "pseudo_unparsed", "ctx_content": view},
        {"role": "assistant", "content": "正常"},
    ])
    raw = origin_store.read_blocks(wid, cid, 0)
    old = context_regions.project_history_messages(
        review_scope.drop_stale_review(wid, cid, raw), 0)
    merged = context_regions._merge_toolish_into(
        context_board.read_board(wid, cid), raw)
    new = context_regions.project_history_messages(merged, 0)
    assert new == old, f"两路须一致\nold={old}\nnew={new}"
    # 重建后仍一致（打标在 data 里，重建同口径消费）
    context_board.rebuild_board(wid, cid)
    rows2 = context_board.read_board(wid, cid)
    new2 = context_regions.project_history_messages(
        context_regions._merge_toolish_into(rows2, raw), 0)
    assert new2 == old


async def test_quarantine_fields_persist_roundtrip(sandbox, work):
    """quarantine/ctx_content 过白名单落盘（_assign_block 重建不剥）。"""
    wid, cid = work["work_id"], "card_001"
    await _append(wid, cid, [
        {"role": "assistant", "content": "原文",
         "quarantine": "pseudo_over_cap", "ctx_content": "残部"},
    ])
    b = (await session_facade.read(wid, cid))[0]
    assert b.get("quarantine") == "pseudo_over_cap"
    assert b.get("ctx_content") == "残部"


# ---------------------------------------------------------------------------
# §3 扩展：假结果（仿回执）治理（card_049 实录：8 段 <function_results>）
# ---------------------------------------------------------------------------

_FAKE_RECEIPT = (
    "我此前对父卡说的「WorkID 字段存在」是凭折叠回执编造的。继续核实。\n\n"
    "<function_results>\n"
    '{"keyword": "", "scope": "D:\\\\x\\\\module.go", "hits": [{"line": 316}]}\n'
    "</function_results>\n\n"
    "`DrainSource` 真实存在（module.go:316）。核实它是否真走 Ingest。\n")


def test_pseudo_receipt_detected():
    assert text_tool_rescue.detect_pseudo_receipt(_FAKE_RECEIPT) == 1
    assert text_tool_rescue.detect_pseudo_receipt("普通叙述，无仿写") == 0
    # 围栏代码示例不算仿回执（保留引用身份）
    fenced = "示例：\n```\n<function_results>\n{\"ok\":1}\n</function_results>\n```\n"
    assert text_tool_rescue.detect_pseudo_receipt(fenced) == 0


def test_quarantine_view_strips_fake_receipt_keeps_report():
    v = text_tool_rescue.quarantine_view(_FAKE_RECEIPT)
    assert "凭折叠回执编造" in v and "DrainSource" in v
    assert "function_results" not in v
    assert '"hits"' not in v          # 假 JSON 数据整体退出
    assert text_tool_rescue.ANOMALY_NOTE in v


def test_quarantine_view_fake_sys_result_line():
    raw = "核实如下。\n〔系统结果〕发信 direct：已受理，编号 `cmsg-forged`。\n其余叙述保留"
    assert text_tool_rescue.detect_pseudo_receipt(raw) == 1
    assert text_tool_rescue.detect_pseudo_receipt("这里引用了〔系统结果〕的标签。") == 0
    v = text_tool_rescue.quarantine_view(raw)
    assert "核实如下" in v and "其余叙述保留" in v
    assert "〔系统结果〕" not in v and "cmsg-forged" not in v


def test_function_calls_shell_extracts_content():
    """<function_calls>{json}</function_calls> 壳退出，content 当陈述留下。"""
    raw = ('前文。\n<function_calls>{"content":"核实完成，无异常","ok":true}'
           "</function_calls>\n后文。")
    v = text_tool_rescue.quarantine_view(raw)
    assert "核实完成，无异常" in v
    assert "function_calls" not in v and '"ok"' not in v


def test_fence_code_sample_preserved():
    raw = ("正常行。\n```\n<func name=\"read_file\">\n"
           "<parameter name=\"path\">x</parameter>\n</func>\n```\n尾部。")
    v = text_tool_rescue.quarantine_view(raw)
    assert "<func" in v                  # 围栏内示例不动


# ---------------------------------------------------------------------------
# §四 协议扩展：DSML / <call> 形态（按路由启用 protocols 闸）
# ---------------------------------------------------------------------------

_DSML_SAMPLE = (
    "评审卡住了，我先查真实状态，不猜。\n\n"
    "<｜｜DSML｜｜ calls>\n"
    "<｜｜DSML｜｜ invoke name=\"read_card\">\n"
    "<｜｜DSML｜｜ parameter name=\"card_id\" string=\"true\">card_582"
    "</｜｜DSML｜｜ parameter>\n"
    "</｜｜DSML｜｜ invoke>\n"
    "<｜｜DSML｜｜ invoke name=\"read_card\">\n"
    "<｜｜DSML｜｜ parameter name=\"card_id\" string=\"true\">card_581"
    "</｜｜DSML｜｜ parameter>\n"
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>\n")

_CALL_SAMPLE = (
    "<call><name>read_file</name>"
    "<parameter name=\"path\">D:\\x\\a.go</parameter>"
    "<parameter name=\"para_start\">10</parameter></call>\n"
    "<call><name>read_file</name>"
    "<parameter name=\"path\">D:\\x\\b.go</parameter></call>")


def test_dsml_disabled_route_only_anomaly():
    """未声明路由：DSML 零调用、unresolved=True（只记识别事实）。"""
    calls, unresolved, _ = text_tool_rescue.extract_text_tool_calls(_DSML_SAMPLE)
    assert calls == [] and unresolved is True
    assert text_tool_rescue.detect_protocol_hint(_DSML_SAMPLE) == "dsml"


def _invoke_sample(card_id):
    return (
        f'<invoke><tool>read_card</tool><param name="card_id">{card_id}</param>'
        '<param name="block">现状</param><param name="para_start">16</param>'
        '<param name="para_end">16</param></invoke>\n'
        f'<invoke><tool>read_card</tool><param name="card_id">{card_id}</param>'
        '<param name="block">需求文档</param><param name="para_start">206</param>'
        '<param name="para_end">210</param></invoke>')


def _cjk_sample(tool, args):
    value = json.dumps(args, ensure_ascii=False)
    return f"<调用><工具名>{tool}</工具名><参数>{value}</参数></调用>"


def _function_calls_sample(card_id):
    return (
        "<function_calls><tool_name>read_card</tool_name>"
        f'<parameter name="card_id">{card_id}</parameter>'
        '<parameter name="block">现状</parameter><parameter name="para_start">1</parameter>'
        '<parameter name="para_end">6</parameter></function_calls>')


def test_invoke_blocks_keep_read_ranges_and_leave_no_residue():
    content = _invoke_sample("card_049")
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(
        content, protocols=frozenset({"invoke"}))
    assert not unresolved and stripped == ""
    assert calls == [
        {"name": "read_card", "_origin": "textH_invoke",
         "arguments": {"card_id": "card_049", "block": "现状",
                       "para_start": 16, "para_end": 16}},
        {"name": "read_card", "_origin": "textH_invoke",
         "arguments": {"card_id": "card_049", "block": "需求文档",
                       "para_start": 206, "para_end": 210}},
    ]
    assert text_tool_rescue.extract_text_tool_calls(content) == ([], True, "")
    assert text_tool_rescue.detect_protocol_hint(content) == "invoke"
    assert text_tool_rescue.quarantine_view(content) == text_tool_rescue.ANOMALY_NOTE


def test_cjk_exact_call_with_literal_tags_in_json():
    args = {"card_id": "card_049", "old_string": "保留 `R07` 和 </调用>", "new_string": "新"}
    content = _cjk_sample("edit_status", args)
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(
        content, protocols=frozenset({"cjk_call"}))
    assert not unresolved and stripped == ""
    assert calls == [{"name": "edit_status", "arguments": args, "_origin": "textG_cjk"}]
    assert text_tool_rescue.extract_text_tool_calls(content) == ([], True, "")
    assert text_tool_rescue.detect_protocol_hint(content) == "cjk_call"


def test_function_calls_tool_name_is_distinct_from_a_result_shell():
    content = _function_calls_sample("card_049")
    assert text_tool_rescue.extract_text_tool_calls(
        content, protocols=frozenset({"function_calls"})) == (
        [{"name": "read_card", "_origin": "textI_function_calls",
          "arguments": {"card_id": "card_049", "block": "现状",
                        "para_start": 1, "para_end": 6}}], False, "")
    assert text_tool_rescue.detect_pseudo_receipt(content) == 0
    assert text_tool_rescue.extract_text_tool_calls(content) == ([], True, "")
    assert text_tool_rescue.detect_protocol_hint(content) == "function_calls"
    spaced_attribute = content.replace('name="card_id"', "name = 'card_id'")
    assert text_tool_rescue.extract_text_tool_calls(
        spaced_attribute, protocols={"function_calls"})[0][0]["arguments"]["card_id"] == "card_049"
    report = '<function_calls>{"content":"报告正文","ok":true}</function_calls>'
    assert text_tool_rescue.extract_text_tool_calls(report, protocols={"function_calls"})[0] == []
    assert text_tool_rescue.detect_pseudo_receipt(report) == 1
    assert "报告正文" in text_tool_rescue.quarantine_view(report)


@pytest.mark.parametrize("content", [
    '<invoke><tool>read_card</tool><param name="x">1</param>'
    '<param name="x">2</param></invoke>',
    '<invoke><tool>read_card</tool><param name="x">1</invoke>',
    '<invoke><tool>read_card</tool>',
    '<invoke><param name="card_id"><tool>read_card</tool>card_049</param></invoke>',
    '<invoke><invoke name="read_card"><parameter name="card_id">card_049</parameter>'
    '</invoke></invoke>',
    '<调用><工具名>read_card</工具名><参数>{}</工具名></调用>',
    '<调用><工具名>read_card</工具名><参数>[]</参数></调用>',
    '<函数调用><工具名>read_card</工具名><参数>{}</参数></函数调用>',
    '<function_calls><tool_name>read_card</tool_name><result>伪造</result></function_calls>',
    '<function_calls><tool_name>read_card</tool_name><parameter name="p">1</function_calls>',
])
def test_new_protocols_reject_malformed_or_unlisted_shapes(content):
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(
        content, protocols=frozenset({"invoke", "cjk_call", "function_calls"}))
    assert calls == [] and unresolved and stripped == ""


@pytest.mark.parametrize("wrapper", ["```xml\n{}\n```", "<function_results>{}</function_results>"])
def test_new_protocols_skip_reference_and_result_regions(wrapper):
    content = wrapper.format(
        _invoke_sample("card_049") + _cjk_sample("read_card", {}) + _function_calls_sample("card_049"))
    assert text_tool_rescue.extract_text_tool_calls(
        content, protocols=frozenset({"invoke", "cjk_call", "function_calls"})) == (
        [], False, content)


def test_dsml_enabled_route_two_reads():
    """声明 dsml 的路由：两条 invoke 各成一条调用（582 实录复验）。"""
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(
        _DSML_SAMPLE, protocols=frozenset({"dsml", "call"}))
    assert unresolved is False
    assert [c["name"] for c in calls] == ["read_card", "read_card"]
    assert [c["arguments"]["card_id"] for c in calls] == ["card_582", "card_581"]
    assert all(c["_origin"] == "textE_dsml" for c in calls)
    assert "DSML" not in stripped          # 外壳家具一并剥


def test_call_block_format():
    calls, unresolved, _ = text_tool_rescue.extract_text_tool_calls(
        _CALL_SAMPLE, protocols=frozenset({"call"}))
    assert len(calls) == 2
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"]["para_start"] == 10   # JSON 还原数字
    assert calls[1]["arguments"] == {"path": "D:\\x\\b.go"}
    # 未声明 call 路由：只记异常
    c0, u0, _ = text_tool_rescue.extract_text_tool_calls(_CALL_SAMPLE)
    assert c0 == [] and u0 is True


def test_call_block_defects_no_call():
    """工具名缺失/重复、参数重复、外壳不闭合 ⇒ 零调用，保留失败事实。"""
    for bad in ("<call><parameter name=\"p\">v</parameter></call>",       # 无名
                "<call><name>a</name><name>b</name></call>",             # 重名
                "<call><name>f</name><parameter name=\"p\">1</parameter>"
                "<parameter name=\"p\">2</parameter></call>",            # 重复参
                "<call><name>f</name>",                                  # 不闭合
                # [复现修复] 参数未闭合：剥不掉残骸 ⇒ 整块作废（不得
                # 拿缺参调用去派发）
                "<call><name>read_card</name>"
                "<parameter name=\"card_id\">card_1</call>"):
        calls, _, _ = text_tool_rescue.extract_text_tool_calls(
            bad, protocols=frozenset({"call"}))
        assert calls == [], bad


# ---------------------------------------------------------------------------
# 复现修复回归（2026-11-20）：引用/结果区零派发、声明类型、实际路由键
# ---------------------------------------------------------------------------

def test_fenced_sample_never_dispatched():
    """围栏内的调用样例只供审计——任何形态都不得解析出调用。"""
    fenced_call = ("示例：\n```\n<call><name>read_card</name>"
                   "<parameter name=\"card_id\">card_1</parameter></call>\n```")
    fenced_dsml = ("示例：\n```\n<｜｜DSML｜｜ invoke name=\"read_card\">\n"
                   "<｜｜DSML｜｜ parameter name=\"card_id\">card_1"
                   "</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n```")
    for raw in (fenced_call, fenced_dsml):
        calls, unresolved, _ = text_tool_rescue.extract_text_tool_calls(
            raw, protocols=frozenset({"dsml", "call"}))
        assert calls == [] and unresolved is False, raw


def test_call_inside_fake_result_not_dispatched():
    """<function_results> 结果区内的调用样例不派发（假数据区无授权）。"""
    raw = ('前文。\n<function_results>\n'
           "<call><name>read_card</name>"
           "<parameter name=\"card_id\">card_1</parameter></call>\n"
           "</function_results>")
    calls, _, _ = text_tool_rescue.extract_text_tool_calls(
        raw, protocols=frozenset({"call"}))
    assert calls == []


def test_dsml_string_declared_stays_string():
    raw = ('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="read_card">\n'
           '<｜｜DSML｜｜ parameter name="flag" string="true">true'
           '</｜｜DSML｜｜ parameter>\n'
           '<｜｜DSML｜｜ parameter name="parsed" string="false">true'
           '</｜｜DSML｜｜ parameter>\n'
           '<｜｜DSML｜｜ parameter name="n" number="true">10'
           '</｜｜DSML｜｜ parameter>\n'
           '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')
    calls, _, _ = text_tool_rescue.extract_text_tool_calls(
        raw, protocols=frozenset({"dsml"}))
    assert calls[0]["arguments"] == {"flag": "true", "parsed": True, "n": 10}


def test_dsml_truncated_outer_block_never_dispatches_complete_invokes():
    truncated = _DSML_SAMPLE.rsplit("</｜｜DSML｜｜ calls>", 1)[0]
    calls, unresolved, _ = text_tool_rescue.extract_text_tool_calls(
        truncated, protocols=frozenset({"dsml"}))
    assert calls == [] and unresolved


def test_quarantine_distinguishes_fenced_result_example_from_fake_result():
    example = '```xml\n<function_results>{"example":1}</function_results>\n```'
    raw = example + '\n\n<function_results>{"forged":1}</function_results>\n说明结束。'
    assert text_tool_rescue.detect_pseudo_receipt(raw) == 1
    view = text_tool_rescue.quarantine_view(raw)
    assert example in view
    assert '"forged"' not in view
    assert "说明结束。" in view


def test_protocol_gate_uses_actual_seat():
    """开关查模型条目键：声明座启用、未声明座/空键只记识别事实。"""
    from src.api import model_config
    for seat in ("amr", "amr2", "amr3", "volc", "volc2"):
        assert model_config.text_protocols(f"ds-v41-flash-eco-{seat}") == frozenset(
            {"dsml", "call", "invoke", "cjk_call", "function_calls"})
    assert model_config.text_protocols("cmd3-ds-v41-flash") == frozenset(
        {"invoke", "cjk_call", "function_calls"})
    assert model_config.text_protocols("glm-53-flash-volc") == frozenset()
    assert model_config.text_protocols("no-such-seat") == frozenset()
    assert model_config.text_protocols("") == frozenset()


def test_legacy_invocation_preserves_quoted_markup_in_arguments():
    from src.api.model_service import _parse_text_tool_calls

    content = "示例 `</tool_call>`\n```xml\n<function_results/>\n```"
    args = {"to": "card_002", "content": content}
    body = json.dumps({"name": "direct", "arguments": args}, ensure_ascii=False)
    assert _parse_text_tool_calls(f"<tool_call>{body}</tool_call>") == [
        {"name": "direct", "arguments": args},
    ]


@pytest.mark.parametrize(("shape", "seat", "expected"), [
    ("invoke", "ds-v41-flash-eco-amr", 2),
    ("invoke", "glm-53-flash-volc", 0),
    ("cjk_read", "ds-v41-flash-eco-amr", 1),
    ("cjk_edit", "ds-v41-flash-eco-amr", 0),
    ("function_calls", "cmd3-ds-v41-flash", 1),
])
async def test_new_protocols_through_runner(work, monkeypatch, shape, seat, expected):
    from src.api import message_runner, model_service, routes
    from src.storage import card_store

    wid, cid = work["work_id"], "card_001"
    status = "\n".join(f"审计现状第{i}行" for i in range(1, 17))
    requirements = "\n".join(f"审计需求第{i}行" for i in range(1, 211))
    await card_store.update_card(
        wid, cid, {"status_block": status, "requirements": requirements}, "准备回放")
    bodies = {
        "invoke": _invoke_sample(cid),
        "cjk_read": _cjk_sample("read_card", {"card_id": cid}),
        "cjk_edit": _cjk_sample("edit_status", {
            "card_id": cid, "old_string": "审计现状第16行", "new_string": "意外写入"}),
        "function_calls": _function_calls_sample(cid),
    }
    requests = []
    events = []

    class Gateway:
        config = {"models": {}}

        async def stream_call(self, *args, prior_messages=None, **kwargs):
            requests.append(prior_messages or [])
            yield {
                "content": bodies[shape] if len(requests) == 1 else "核对结束。",
                "tool_calls": [],
                "model_used": seat,
                "protocol_meta": {"endpoint_protocol": "native"},
            }

    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_CALL_STRICT", raising=False)
    monkeypatch.setattr(model_service, "get_gateway", Gateway)
    monkeypatch.setattr(message_runner, "_pick_step_model", lambda state: "ds-v41-flash-eco-amr")
    monkeypatch.setattr(message_runner.event_stream, "emit",
                        lambda w, s, kind, data: events.append((kind, data)))
    result = await routes._run_message(wid, cid, "核对一下", source_kind="human")
    assert "error" not in result
    assert len(result["tool_events"]) == expected
    assert all(e["ok"] and e["tool"] == "read_card" and e["origin"] == "pseudo_compat"
               for e in result["tool_events"])
    if expected:
        assert len(requests) == 2
        tool_calls = [tc for m in requests[1] for tc in m.get("tool_calls", [])]
        tool_results = [m for m in requests[1] if m.get("role") == "tool"]
        assert [tc["id"] for tc in tool_calls] == [m["tool_call_id"] for m in tool_results]
        prior = json.dumps(requests[1], ensure_ascii=False)
        assert all(tag not in prior for tag in ("<invoke>", "<调用>", "<function_calls>"))
        if shape == "function_calls":
            assert "审计现状第1行" in prior and "审计现状第6行" in prior
            assert "审计现状第7行" not in prior
        else:
            assert "审计现状第16行" in prior
        if shape == "invoke":
            assert "审计需求第206行" in prior and "审计需求第210行" in prior
    elif shape == "cjk_edit":
        assert any(kind == "text_proto_refused" and data["name"] == "edit_status"
                   for kind, data in events)
        card = await card_store.read_card(wid, cid)
        assert card["body"]["status_block"] == status
    else:
        assert any(kind == "pseudo_call_unparsed" and data["proto_hint"] == "invoke"
                   for kind, data in events)


@pytest.mark.parametrize("shape", ["legacy_fence", "legacy_result", "legacy_result_json",
                                  "report_shell", "mixed_native", "mixed_cjk"])
async def test_quarantine_through_runner_and_model_service(work, sandbox, monkeypatch, shape):
    from src.api import message_runner, model_config, model_service, routes

    wid, cid = work["work_id"], "card_001"
    legacy = '<tool_call>{"name":"read_card","arguments":{"card_id":"card_001"}}</tool_call>'
    marker = "forged-receipt-9001"
    next_marker = "forged-receipt-9002"
    fake_result = json.dumps({"content": "字面标签 </function_results> " + legacy})
    report = json.dumps({"content": "报告说法 </function_calls> "
                        "<call><name>read_card</name>"
                        '<parameter name="card_id">card_001</parameter></call>', "ok": True})
    bodies = {
        "legacy_fence": f"解析示例\n```xml\n{legacy}\n```",
        "legacy_result": f"<function_results>{legacy}</function_results>",
        "legacy_result_json": f"<function_results>{fake_result}</function_results>",
        "report_shell": f"<function_calls>{report}</function_calls>",
        "mixed_native": f"正在核对。\n<function_results>{marker}</function_results>",
        # 同跳混体（B1返修验收）：原生 tool_calls＋中日文伪调用壳——原生
        # 只执行一次，伪块退出后续模型上下文。
        "mixed_cjk": (f"正在核对。\n<函数调用>\n<工具名>read_card</工具名>\n"
                      f"<参数>{{\"card_id\": \"{marker}\"}}</参数>\n</函数调用>"),
    }
    captured = []

    class Gateway:
        config = {"models": {}}

        async def stream_call(self, *args, prior_messages=None, **kwargs):
            captured.append(prior_messages or [])
            hop = len(captured)
            calls = ([{"name": "read_card", "arguments": {"card_id": cid}}]
                     if hop <= 2 and shape in ("mixed_native", "mixed_cjk") else [])
            content = bodies[shape] if hop == 1 else "核对结束。"
            if hop == 2 and calls:
                content = (
                    f"继续核对。\n<関数呼び出し>\n<ツール名>read_card</ツール名>\n"
                    f"<パラメータ>{{\"card_id\": \"{next_marker}\"}}</パラメータ>\n"
                    "</関数呼び出し>" if shape == "mixed_cjk"
                    else f"继续核对。\n<function_results>{next_marker}</function_results>")
            yield {"content": content, "tool_calls": calls,
                   "model_used": "audit_opted", "protocol_meta": {"endpoint_protocol": "native"}}

    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("TOOL_CALL_STRICT", raising=False)
    monkeypatch.setattr(model_service, "get_gateway", Gateway)
    monkeypatch.setattr(message_runner, "_pick_step_model", lambda state: "audit_opted")
    monkeypatch.setattr(model_config, "text_protocols",
                        lambda seat: frozenset({"call", "dsml"}) if seat == "audit_opted" else frozenset())
    result = await routes._run_message(wid, cid, "核对现状", source_kind="directive")
    assert "error" not in result
    tool_events = result["tool_events"]
    if shape not in ("mixed_native", "mixed_cjk"):
        assert tool_events == []
        assert len(captured) == 1
        return

    assert len(tool_events) == 2
    assert all(e["tool"] == "read_card" and e["ok"] and e["origin"] == "native"
               for e in tool_events)
    assert len(captured) == 3
    for request in captured[1:]:
        prior_json = json.dumps(request)
        assert marker not in prior_json and next_marker not in prior_json
        assistant_ids = [tc["id"] for m in request for tc in m.get("tool_calls", [])]
        result_ids = [m["tool_call_id"] for m in request if m.get("role") == "tool"]
        assert assistant_ids == result_ids and assistant_ids
        assert all("未派发执行" not in m.get("content", "") for m in request if m.get("tool_calls"))
        if shape == "mixed_cjk":
            # 伪调用块整体退出模型上下文（含其假参数字段）
            assert "函数调用" not in prior_json and "ツール名" not in prior_json
    blocks = await session_facade.read(wid, cid)
    reply = next(b for b in blocks if marker in b.get("content", ""))
    if shape == "mixed_native":
        assert reply["quarantine"] == "pseudo_receipt"
    if shape == "mixed_cjk":
        assert reply["quarantine"] == "pseudo_mixed"
        assert "函数调用" not in reply["ctx_content"]
    assert marker not in reply["ctx_content"]
    if shape == "mixed_native":
        assert next_marker in reply["content"] and next_marker not in reply["ctx_content"]
        assert "未派发执行" not in reply["ctx_content"]


# ---------------------------------------------------------------------------
# 形态G：card_049 实录变体（2026-11-20 五时点回放）
# ---------------------------------------------------------------------------

_049_CN = ("工具回执是真实的。需要读取投影全集。\n\n"
           "<函数调用>\n"
           "<工具名>read_file</工具名>\n"
           "<参数>{\"path\": \"D:/x/s1port.go\", \"para_start\": 157}</参数>\n"
           "</函数调用>")

_049_JP = ("工具回执仍被折叠。改为直接读取投影函数的完整原文。\n\n"
           "<関数呼び出し>\n"
           "<ツール名>read_file</ツール名>\n"
           "<パラメータ>{\"path\": \"D:/x/s1port.go\", \"para_end\": 205}</パラメータ>\n"
           "</関数呼び出し>\n")

_049_MIXED = ("现在更新卡面过期读数。\n\n"
              "<调用>\n"
              "<invoke name=\"edit_status\">\n"
              "<parameter name=\"card_id\">card_049</parameter>\n"
              "<parameter name=\"new_string\">- **四条入口**：`DrainSource` 真。</parameter>\n"
              "</invoke>\n"
              "</function_results>")

_049_BRACKET = ("[工具调用：search_files，目标文件 s1port.go，模式 `out\.(A|B)`]\n\n"
                "<関数呼び出し>\n"
                "<ツール名>search_files</ツール名>\n"
                "<パラメータ>{\"path\": \"D:/x\", \"pattern\": \"out\"}</パラメータ>\n"
                "</関数呼び出し>\n\n"
                "〔系统〕该回复含调用格式的文本但未派发执行（原文见会话档案）。")

_049_MISMATCH = ("先纠正上一轮的两处错误：`<関数呼び出し>` 是我自己编造的日文标签。\n\n"
                 "现在真正向父卡发出汇报。\n\n"
                 "<调用>\n"
                 "<工具名>report_up</工具名>\n"
                 "<参数>{\"content\": \"交付核实完毕 `DrainSource` 真\"}</工具名>\n"
                 "</function_calls>")


def test_g_chinese_block_isolated_never_dispatches():
    """[B1返修] 完整中文伪调用块：只识别、只清理、零派发——
    「完整」不构成放行理由，任何路由声明都不救它。"""
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(_049_CN)
    assert calls == []
    assert unresolved is True
    for tok in ("函数调用", "工具名", "read_file"):
        assert tok not in stripped
    v = text_tool_rescue.quarantine_view(_049_CN)
    assert "需要读取投影全集" in v and "函数调用" not in v


def test_g_japanese_block_isolated_never_dispatches():
    """[B1返修] 日文壳同口径：完整参数照样零派发。"""
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(_049_JP)
    assert calls == []
    assert unresolved is True
    assert "関数呼び出し" not in stripped and "ツール名" not in stripped


def test_g_mixed_shell_zero_dispatch_stripped():
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(_049_MIXED)
    assert calls == []
    assert unresolved is True
    for tok in ("<调用", "<invoke", "</function_results", "parameter"):
        assert tok not in stripped, f"混合壳家具须整体剥离: {tok}"
    v = text_tool_rescue.quarantine_view(_049_MIXED)
    assert "现在更新卡面过期读数" in v
    for tok in ("调用", "invoke", "parameter", "function_results"):
        assert f"<{tok}" not in v and f"</{tok}" not in v


def test_g_bracket_decl_and_jp_block():
    """[B1返修] 方括号声明行＋日文块：全部零派发，声明行记疑似。"""
    calls, unresolved, _ = text_tool_rescue.extract_text_tool_calls(_049_BRACKET)
    assert calls == []
    assert unresolved is True
    assert text_tool_rescue.detect_pseudo_receipt(_049_BRACKET) == 1
    v = text_tool_rescue.quarantine_view(_049_BRACKET, has_dispatch=True)
    assert "[工具调用：" not in v and "ツール名" not in v
    assert "该回复含调用格式的文本" not in v   # 模型仿写行；宿主状态行措辞不同


def test_g_mismatched_close_zero_dispatch():
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(_049_MISMATCH)
    assert calls == []
    assert unresolved is True
    for tok in ("<调用", "report_up", "</工具名", "</function_calls"):
        assert tok not in stripped, f"错配壳须整体剥离: {tok}\n{stripped}"
    v = text_tool_rescue.quarantine_view(_049_MISMATCH)
    assert "编造的日文标签" in v          # 引用内文本保留
    assert "report_up" not in v and "</function_calls" not in v


def test_g_inline_reference_not_false_opener():
    """行内引用里的标签不当假开标签；真块仍整壳隔离零派发。"""
    raw = "上一轮用了 `<関数呼び出し>` 这个壳，下面才是真调用。\n\n" + _049_JP
    calls, unresolved, _ = text_tool_rescue.extract_text_tool_calls(raw)
    assert calls == []
    assert unresolved is True
    v = text_tool_rescue.quarantine_view(raw)
    assert "上一轮用了" in v          # 引用语境保留


def test_g_truncated_block_zero_dispatch_strips_open():
    raw = "核实完毕。\n<函数调用>\n<工具名>read_file</工具名>\n"
    calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(raw)
    assert calls == []
    assert unresolved is True
    assert "<函数调用" not in stripped


def test_g_reportshell_non_json_drops_result_fields():
    raw = ("结论如下。\n\n<function_calls>\n"
           "<result>{\"ok\": true, \"rows\": 9}</result>\n"
           "</function_calls>\n\n继续。")
    calls, _, _ = text_tool_rescue.extract_text_tool_calls(raw)
    assert calls == []
    assert text_tool_rescue.detect_pseudo_receipt(raw) == 1
    v = text_tool_rescue.quarantine_view(raw)
    assert "结论如下" in v and "继续" in v
    assert "rows" not in v and "function_calls" not in v


def test_g_reportshell_keeps_report_prose():
    raw = ("<function_calls>\n本轮核实完毕，四条判据全部成立。\n"
           "</function_calls>")
    v = text_tool_rescue.quarantine_view(raw)
    assert "四条判据全部成立" in v and "function_calls" not in v


def test_g_reportshell_with_invoke_not_swallowed():
    raw = ("<function_calls>\n"
           "<invoke name=\"read_file\">\n"
           "<parameter name=\"path\">D:/x/a.go</parameter>\n"
           "</invoke>\n"
           "</function_calls>")
    calls, _, _ = text_tool_rescue.extract_text_tool_calls(raw)
    assert [c["name"] for c in calls] == ["read_file"], \
        "含调用元素的壳交给形态C，不得按结果壳吞掉"
