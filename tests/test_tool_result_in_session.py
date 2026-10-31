# -*- coding: utf-8 -*-
"""工具结果落会话（对齐通用 agent 的对话形态）· 施工验收测试（2026-10-17）。

判据见《工具结果落会话-任务书-20261017.md》§4：V1–V12（正向）＋ N1–N10（负向）。
本文件为**离线/mock 证据**；端点实测（V7 实弹）另见施工回报，二者不混写。
"""
from __future__ import annotations

import json

from src.api import context_regions
from src.storage import event_log, memory_store, session_facade, session_store


_TOOL_CALLS = [
    {"id": "call_abc_1_0", "type": "function",
     "function": {"name": "read_card", "arguments": json.dumps({"card_id": "card_002"})}},
    {"id": "call_abc_1_1", "type": "function",
     "function": {"name": "write_file",
                  "arguments": json.dumps({"path": "a.txt", "content": "X" * 50})}},
]


async def _append_tool_round(wid: str, cid: str, *, results=("结果一", "结果二")) -> list[dict]:
    """落一个工具轮：块 A（assistant+tool_calls）＋ 2 个块 B（tool 结果）。"""
    blocks = [{"actor": "assistant", "content": "", "tool_calls": _TOOL_CALLS}]
    for tc, r in zip(_TOOL_CALLS, results):
        blocks.append({"actor": "tool", "tool_call_id": tc["id"], "content": r})
    return await session_facade.append_many(wid, cid, blocks)


# ================================================================
# V1：成对落盘
# ================================================================

async def test_v1_tool_round_pairs_persisted(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "干活"})
    await _append_tool_round(wid, cid)
    await session_facade.append(wid, cid, {"role": "assistant", "content": "完成"})

    blocks = await session_facade.read(wid, cid)
    assert [b["actor"] for b in blocks] == ["human", "assistant", "tool", "tool", "assistant"]

    a = blocks[1]
    assert a["actor"] == "assistant" and a["content"] == ""
    assert [tc["id"] for tc in a["tool_calls"]] == ["call_abc_1_0", "call_abc_1_1"]
    assert a["end"] == "", "带 tool_calls 的 assistant 块不补 end（未闭合轮）"

    assert blocks[2]["tool_call_id"] == "call_abc_1_0" and blocks[2]["content"] == "结果一"
    assert blocks[3]["tool_call_id"] == "call_abc_1_1" and blocks[3]["content"] == "结果二"

    # 工具块与用户/回复同 turn（工具块不闭合轮）
    assert {b["turn"] for b in blocks} == {0}
    assert blocks[-1]["end"] == "model_done"
    # seq 单调
    assert [b["seq"] for b in blocks] == [0, 1, 2, 3, 4]

    # 落盘文件里两字段确实写出（JSON 单行）
    raw = session_store._session_path(wid, cid).read_text(encoding="utf-8")
    assert "tool_calls: " in raw and "tool_call_id: call_abc_1_0" in raw
    assert raw.count("---MSG---") == 5


async def test_v1b_render_materialize_roundtrip(sandbox):
    """块 A 经 _render_v2_block → _parse_v2 往返，tool_calls 原样读回。"""
    blk = session_facade._assign_block(
        [], {"actor": "assistant", "content": "", "tool_calls": _TOOL_CALLS})
    text = session_facade._render_v2_block(blk)
    parsed = session_facade._parse_v2("---SESSION---\nversion: 2\n---\n" + text)
    b = parsed["blocks"][0]
    assert b["tool_calls"] == _TOOL_CALLS
    assert b["end"] == ""
    assert not b.get("damaged")


# ================================================================
# V2 / V7：工具块进上下文投影，配对完整
# ================================================================

async def test_v2_tool_blocks_in_history_projection(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "干活"})
    await _append_tool_round(wid, cid)
    await session_facade.append(wid, cid, {"role": "assistant", "content": "完成"})
    blocks = await session_facade.read(wid, cid)

    proj = context_regions.project_history_messages(blocks)
    # [契约变更 2026-10-23 · 用户需求] 工具结果**不再进上下文**：
    # 投影只剩有正文的 user / assistant；带 tool_calls 的 A 块与 role=tool 成对丢弃
    # （只丢一半会留孤立调用 ⇒ 端点 400）。
    assert [m["role"] for m in proj] == ["user", "assistant"]
    assert proj[1]["content"] == "完成"
    assert sum(1 for m in proj if m["role"] == "tool") == 0
    assert all(not m.get("tool_calls") for m in proj)


async def test_v7_projection_tool_pairs_complete(sandbox, work):
    """每个 role=tool 紧跟其配对的 assistant(tool_calls)、id 一致。"""
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "干活"})
    await _append_tool_round(wid, cid)
    blocks = await session_facade.read(wid, cid)
    proj = context_regions.project_history_messages(blocks)

    pending_ids: list[str] = []
    for m in proj:
        if m["role"] == "assistant" and m.get("tool_calls"):
            pending_ids = [tc["id"] for tc in m["tool_calls"]]
        elif m["role"] == "tool":
            assert pending_ids, "role=tool 必须紧跟其配对的 assistant(tool_calls)"
            assert m["tool_call_id"] in pending_ids, "tool_call_id 必须与配对调用一致"


# ================================================================
# V3 / N3 / N4：计量含工具（含 tool_calls 参数）
# ================================================================

def test_v3_meter_includes_content_and_tool_call_args(sandbox):
    empty = {"content": ""}
    base = memory_store._msg_meter_tokens(empty)
    assert base == memory_store._MSG_SEP_TOKENS

    tcs = [{"function": {"arguments": "a" * 10}}]
    with_args = {"content": "", "tool_calls": tcs}
    expected = base + len(memory_store._ENC.encode("a" * 10))
    assert memory_store._msg_meter_tokens(with_args) == expected, "tool_calls 参数计入"

    # content 与 tool_calls 参数同时计入
    both = {"content": "六六六", "tool_calls": tcs}
    assert memory_store._msg_meter_tokens(both) == (
        len(memory_store._ENC.encode("六六六")) + memory_store._MSG_SEP_TOKENS
        + len(memory_store._ENC.encode("a" * 10)))
    # _meter_sum 与单条同尺（N4：口径不改，含工具＝默认行为）
    assert memory_store._meter_sum([empty, with_args]) == (
        memory_store._msg_meter_tokens(empty) + memory_store._msg_meter_tokens(with_args))


# ================================================================
# V4 / N2：凝练输入剔除工具块与空 content 块
# ================================================================

def test_v4_condense_prompt_excludes_tool_and_empty(sandbox):
    pending = [
        {"seq": 0, "actor": "human", "content": "用户话"},
        {"seq": 1, "actor": "assistant", "content": "", "tool_calls": _TOOL_CALLS},
        {"seq": 2, "actor": "tool", "content": "工具结果"},
        {"seq": 3, "actor": "assistant", "content": "回复"},
    ]
    prompt = memory_store._build_condense_user_prompt(pending)
    assert "[块0]" in prompt and "[块3]" in prompt
    assert "[块1]" not in prompt, "空 content 的 assistant 块 A 不进凝练"
    assert "[块2]" not in prompt, "工具结果块不进凝练"
    assert "工具结果" not in prompt, "凝练 prompt 不得出现工具块内容（N2）"
    assert "read_card" not in prompt and "a.txt" not in prompt, "tool_calls 参数不进凝练"


# ================================================================
# V5：窗口切片按含工具块的完整窗口
# ================================================================

def test_v5_truncate_window_includes_tool_blocks(sandbox):
    pending = [
        {"seq": 0, "actor": "human", "content": "x"},
        {"seq": 1, "actor": "assistant", "content": "", "tool_calls": _TOOL_CALLS},
        {"seq": 2, "actor": "tool", "content": "y"},
    ]
    w = memory_store._truncate_window(pending, 100_000)
    assert [m["seq"] for m in w] == [0, 1, 2], "窗口含工具块（按含工具窗口算）"
    assert memory_store._meter_sum(w) > memory_store._meter_sum([pending[0]])


# ================================================================
# V6：销账带工具块
# ================================================================

async def test_v6_mark_advances_over_tool_blocks(sandbox, work, monkeypatch):
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "干活"})
    await _append_tool_round(wid, cid, results=("r0", "r1"))
    blocks = await session_facade.read(wid, cid)   # human, A, B, B

    # [2026-10-31] 凝练已是三步（S2a→L1→L2）；替身按当前协议出三段——
    # 单段旧格式产出过不了 S2a 的 G-1 闸（F3，整批作废）。
    from _condense_stub import three_stage_stub as _tss
    monkeypatch.setattr(memory_store, "_condense_call", _tss(gist="做了事"))
    ok = await memory_store._condense_one_batch(wid, cid, blocks, 0)
    assert ok is True
    state = memory_store.load_memory(wid, cid)
    assert state["mark"] == blocks[-1]["seq"] + 1, "mark 越过含工具块的窗口最后一块"
    assert blocks[state["mark"]:] == [], "未凝窗口里工具块已消失"


async def test_v6b_all_tool_window_sells_account_without_model_call(sandbox, work, monkeypatch):
    """整窗筛完为空（全是工具块）→ 不发模型调用，销账（推进 mark）不卡死。"""
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "干活"})
    await _append_tool_round(wid, cid)
    blocks = await session_facade.read(wid, cid)
    window = [b for b in blocks if b["actor"] in ("assistant", "tool")]  # A + B + B

    called = {"n": 0}

    async def fake_call(_p, *args, **_):
        called["n"] += 1
        return "不应被调用"

    monkeypatch.setattr(memory_store, "_condense_call", fake_call)
    ok = await memory_store._condense_one_batch(wid, cid, window, 1)
    assert ok is True
    assert called["n"] == 0, "不得送空 prompt 给模型"
    assert memory_store.load_memory(wid, cid)["mark"] == window[-1]["seq"] + 1


# ================================================================
# V8：存量会话零迁移
# ================================================================

async def test_v8_legacy_roundtrip_byte_stable(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    p = session_store._session_path(wid, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    legacy = (
        "---SESSION---\nversion: 2\nsession_id: card_001\n"
        "identity_owner: human\nidentity_purpose: discussion\nidentity_mode: \ninbox_ref: \n---\n"
        "---MSG---\nseq: 0\nactor: human\nturn: 0\npair: 0\n"
        "ts: 2026-10-01T00:00:00+08:00\n---\n旧话\n---END---\n"
        "---MSG---\nseq: 1\nactor: assistant\nturn: 0\npair: 0\nend: model_done\n"
        "ts: 2026-10-01T00:00:01+08:00\n---\n旧答\n---END---\n"
    )
    p.write_text(legacy, encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    blocks = await session_facade.read(wid, cid)
    assert [b["seq"] for b in blocks] == [0, 1]
    for b in blocks:
        assert b.get("tool_calls") is None, "旧块无 tool_calls（零迁移）"
        assert b.get("tool_call_id") == ""

    # 再追加一块：旧块字节逐字不变（零迁移）
    await session_facade.append(wid, cid, {"role": "user", "content": "新话"})
    after = p.read_text(encoding="utf-8")
    assert after.startswith(before), "存量块字节逐字不变"
    assert "tool_calls" not in after, "普通块不写工具字段"


# ================================================================
# V9 / N9：显示不重复（前端显示源＝事件通道，装配器不读 /session）
# ================================================================

def test_v9_assembler_does_not_read_session(sandbox):
    """装配器（static/esm/assembler.js）不读 /session：本任务新增的是喂模型的
    会话块，不是显示源；工具显示由事件通道 tool_call/tool_result 承载。"""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static" / "esm" / "assembler.js"
          ).read_text(encoding="utf-8")
    assert "/session" not in js and "getSession" not in js, (
        "装配器若开始读会话块，显示会与事件通道双份（N9）——停手回报")


# ================================================================
# V10：日志 regions 对照（history 条目字节/指纹随放行工具块而变）
# ================================================================

def test_v10_log_regions_history_bytes_change(sandbox):
    plain = [{"role": "user", "content": "话"},
             {"role": "assistant", "content": "答"}]
    with_tool = [
        {"role": "user", "content": "话"},
        {"role": "assistant", "content": "", "tool_calls": _TOOL_CALLS},
        {"role": "tool", "tool_call_id": "call_abc_1_0", "content": "结果一"},
    ]
    r_plain = context_regions.build_log_regions([], plain, [], "话")
    r_tool = context_regions.build_log_regions([], with_tool, [], "话")
    h_plain = next(e for e in r_plain if e["name"] == "对话历史")
    h_tool = next(e for e in r_tool if e["name"] == "对话历史")
    assert h_tool["bytes"] > h_plain["bytes"], "history 条目字节随工具块增大"
    assert h_tool["fingerprint"] != h_plain["fingerprint"], "指纹随内容改变"
    assert h_tool["entries"] == 3


# ================================================================
# V11 / N10：prior_key 双区不重复
# ================================================================

def test_v11_prior_key_two_regions_no_duplicate(sandbox):
    from types import SimpleNamespace
    payload = [{"role": "user", "content": "整条 prior"}]

    ctx_turn = SimpleNamespace(request_prior=payload, request_prior_key="tool_turn",
                               history_source=[], history_mark=0)
    assert context_regions._render_prior_for(ctx_turn, "tool_turn") == payload
    assert context_regions._render_prior_for(ctx_turn, "history") == [], "round≥2 历史让位"

    ctx_hist = SimpleNamespace(request_prior=payload, request_prior_key="history",
                               history_source=[{"role": "user", "content": "历史块"}],
                               history_mark=0)
    assert context_regions._render_prior_for(ctx_hist, "history") == payload
    assert context_regions._render_prior_for(ctx_hist, "tool_turn") == [], "round1 工具轮让位"


# ================================================================
# N1：不得出现孤立 tool 块（配对自愈）
# ================================================================

def test_n1_heal_drops_orphan_tool(sandbox):
    blocks = [
        {"role": "user", "content": "u", "actor": "human", "seq": 0},
        {"role": "assistant", "content": "", "actor": "assistant", "seq": 1,
         "tool_calls": _TOOL_CALLS},
        {"role": "tool", "content": "r0", "actor": "tool", "seq": 2,
         "tool_call_id": "call_abc_1_0"},
        {"role": "tool", "content": "r1", "actor": "tool", "seq": 3,
         "tool_call_id": "call_abc_1_1"},
    ]
    # mark 切在 A 与其 B 之间（mark=2 → 从第一个 tool 起）⇒ 孤立 → 整组丢弃
    proj = context_regions.project_history_messages(blocks, mark=2)
    assert all(m["role"] != "tool" for m in proj), "孤立 tool 块必须丢弃"
    assert all(not m.get("tool_calls") for m in proj)

    # mark=1（从 A 起）⇒ 配对完整 → 保留
    proj2 = context_regions.project_history_messages(blocks, mark=1)
    # [契约变更 2026-10-23] mark 落在工具段 ⇒ 剩下的全是 A 块与工具块，一律不进上下文
    assert proj2 == []


def test_n1b_heal_strips_tool_calls_when_tool_filtered(sandbox):
    """allowed_roles 排除 tool（如 card_draft）⇒ A 的 tool_calls 被剥离，
    空 content 的 A 整块丢弃（不留空 assistant 块）。"""
    blocks = [
        {"role": "user", "content": "u", "actor": "human", "seq": 0},
        {"role": "assistant", "content": "", "actor": "assistant", "seq": 1,
         "tool_calls": _TOOL_CALLS},
        {"role": "tool", "content": "r0", "actor": "tool", "seq": 2,
         "tool_call_id": "call_abc_1_0"},
    ]
    proj = context_regions.project_history_messages(blocks, allowed_roles={"user", "assistant"})
    assert [m["role"] for m in proj] == ["user"]
    assert all(not m.get("tool_calls") for m in proj)


# ================================================================
# N5：不得额外发消息事件（工具块不发 user_message/assistant_text）
# ================================================================

def test_n5_tool_blocks_emit_no_message_event(sandbox):
    assert session_facade._block_event_type("assistant", "", _TOOL_CALLS) == ""
    assert session_facade._block_event_type("tool", "") == ""
    assert session_facade._block_event_type("assistant", "", None) == "assistant_text"


async def test_n5b_event_log_has_no_tool_message_type(sandbox, work):
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "干活"})
    await _append_tool_round(wid, cid)
    await session_facade.append(wid, cid, {"role": "assistant", "content": "完成"})
    types = [e.get("type") for e in event_log.read(wid, cid)]
    assert types.count("user_message") == 1
    assert types.count("assistant_text") == 1
    assert "tool_message" not in types


# ================================================================
# 连带核对（§0.1 疑问1）：块 A 不补 end 后，回发取块仍命中最终回复块
# ================================================================

async def test_last_block_still_final_reply_after_tool_round(sandbox, work):
    """_auto_reply_pending_letters 取「最后一块且 actor=assistant 且 end=model_done」
    ——块 A 无 end 后仍由最终回复块命中（回发行为不变）。"""
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "u"})
    await _append_tool_round(wid, cid)
    await session_facade.append(wid, cid, {"role": "assistant", "content": "done"})
    last = (await session_facade.read(wid, cid))[-1]
    assert last["actor"] == "assistant" and last["end"] == "model_done"
    assert last["content"] == "done"


# ================================================================
# N7：tool_fold 只剩轮槽（折叠已移除 2026-10-31 · 用户裁定）
# ================================================================

def test_n7_tool_fold_untouched(sandbox):
    """折叠（【结论】/【依据】两件套）已整体移除：开关/窗口不复存在，
    wire_messages 恒零加工。工具结果只在当前轮展示、更早回合不进上下文
    （由 predicates.enters_context 承担，与本模块其余用例互证）。"""
    from src.api import tool_fold
    assert not hasattr(tool_fold, "fold_enabled"), "折叠开关应已移除"
    assert not hasattr(tool_fold, "KEEP_WINDOW"), "保留窗口应已移除"
    slot = tool_fold.ToolTurnSlot([{"role": "user", "content": "原文"}])
    import asyncio
    wire = asyncio.run(slot.wire_messages())
    assert wire is slot._messages or wire == [{"role": "user", "content": "原文"}]
    assert slot.fold_ops == []


# ================================================================
# 边界：append_many 空入参 / 与 append 分配一致
# ================================================================

async def test_append_many_matches_sequential_append(sandbox, work):
    """append_many 的 seq/turn/pair 分配与逐次 append 完全一致。"""
    wid, cid = work["work_id"], "card_001"
    await session_facade.append(wid, cid, {"role": "user", "content": "u"})
    seq_batch = await session_facade.append_many(wid, cid, [
        {"actor": "assistant", "content": "", "tool_calls": _TOOL_CALLS},
        {"actor": "tool", "tool_call_id": "call_abc_1_0", "content": "r0"},
        {"actor": "tool", "tool_call_id": "call_abc_1_1", "content": "r1"},
    ])
    assert [b["seq"] for b in seq_batch] == [1, 2, 3]
    assert {b["turn"] for b in seq_batch} == {0}
    assert [b["pair"] for b in seq_batch] == ["0", "0", "0"]
    assert await session_facade.append_many(wid, cid, []) == []


# ================================================================
# §3.1 证据：上下文体积改前/改后对照（按卡抽样，≥3 张）
# ================================================================

async def test_evidence_context_size_before_after(sandbox, work):
    """含工具块的 history 投影字节 vs 不含（改前）——任务书 §3.1 实测要求。"""
    from src.storage import card_store

    wid = work["work_id"]
    cards = ["card_001"]
    for i in range(2):
        r = await card_store.create_card(wid, f"抽样卡{i}", "用户直接创建")
        cards.append(r["meta"]["card_id"])

    rows = []
    for cid in cards:
        await session_facade.append(wid, cid, {"role": "user", "content": "请读卡并写文件"})
        for rnd in range(2):
            tcs = [{"id": f"call_{cid}_{rnd}_0", "type": "function",
                    "function": {"name": "read_card",
                                 "arguments": json.dumps({"card_id": "card_002"})}}]
            await session_facade.append_many(wid, cid, [
                {"actor": "assistant", "content": "", "tool_calls": tcs},
                {"actor": "tool", "tool_call_id": tcs[0]["id"], "content": "X" * 400},
            ])
        await session_facade.append(wid, cid, {"role": "assistant", "content": "完成"})
        blocks = await session_facade.read(wid, cid)
        after = json.dumps(context_regions.project_history_messages(blocks), ensure_ascii=False)
        before_blocks = [b for b in blocks
                         if not b.get("tool_calls") and b.get("actor") != "tool"]
        before = json.dumps(context_regions.project_history_messages(before_blocks),
                            ensure_ascii=False)
        rows.append((cid, len(before.encode()), len(after.encode())))
    for cid, b, a in rows:
        print(f"[§3.1] {cid}: 改前(不含工具)={b}B 改后(含工具)={a}B")
    # [契约变更 2026-10-23] 工具结果已不进上下文 ⇒ 含工具与不含工具**一样大**
    # （旧断言 a > b 编码的是"工具结果会撑大上下文"的旧契约）
    assert len(rows) >= 3 and all(a == b for _, b, a in rows)
