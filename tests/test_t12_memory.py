# -*- coding: utf-8 -*-
"""T12 · 凝练机制移植（阶段一/二＋阶段三基础件）回归网。

覆盖（沙箱内，铁律一：不触真实 works/）：
- 任务 1-1：read_session 块序号读时现算（存储格式零改动）
- 任务 1-2：memory_store 空水位建账（幂等/无会话不建/坏文件按空水位）
- 任务 2-1/2-2：200 区插入（开关缺省关 RD-08）＋渲染零加工（不排序/不截断/不设预算）
- V1：开关开＋空记忆 → 装配与旧实现字节级相等（旧实现＝改造前函数体冻结副本）
- V2：一卡一链不串卡（card_A 装配只含 card_A entries）
- V3：memory 写入前后会话/卡片文件字节不变
- V6：开关缺省关、开闸生效、关闸即回旧路
- 基础件：触发线 K（SP-01）／回合末异步触发（SP-04）／read_memory 锚点下探（R3）
"""
from __future__ import annotations

import json

import pytest

from src.api import context_assembler, context_regions, card_tools, prompts
from src.api.prompts import BASE_PROMPT
from src.storage import memory_store, session_store, work_store, card_store

# ---------------------------------------------------------------------------
# 旧实现冻结副本（V1 比对基准；逐字拷自 T12 改造前 build_context_regions/
# build_neutral_context 的现行为——即 T11 交付形态，勿改动）
# ---------------------------------------------------------------------------


def _legacy_regions_text(work_id: str, current_id: str) -> str:
    cards = context_assembler._load_cards_index(work_id)
    wc_id = work_store.work_card_id_of(work_id)
    cur = next((c for c in cards if c["card_id"] == current_id), None)
    lines: list[str] = ["## 卡片清单"]
    for c in cards:
        if c["card_id"] == wc_id or not c["parent"]:
            lines.append(f"{c['card_id']} · {c['title']}（根）")
        else:
            lines.append(f"{c['card_id']} · {c['title']}（父：{c['parent']}）")
    if cur is not None:
        lines.append("")
        lines.append("## 当前卡")
        lines.append(f"{cur['card_id']} · {cur['title']}")
    return "\n".join(lines)


def _legacy_build_system_prompt(ctx):
    if ctx and ctx.strip():
        return BASE_PROMPT + "\n" + ctx.rstrip() + "\n"
    return BASE_PROMPT


async def _make_work_with_cards():
    wdata = await work_store.create_work("T12凝练剧本")
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    child = await card_store.create_card(wid, "子卡甲", "用户直接创建", parent="card_001")
    other = await card_store.create_card(wid, "子卡乙", "用户直接创建", parent="card_001")
    return wid, child["meta"]["card_id"], other["meta"]["card_id"]


def _entry(seq: int, conclusion: str, evidence: str = "【依据】read_card card_001",
           fingerprint: str = "sha256:0123456789abcdef", occurred_at: str = "2026-10-05T12:00:00+08:00"):
    return {"seq": seq, "conclusion": conclusion, "evidence": evidence,
            "occurred_at": occurred_at, "fingerprint": fingerprint}


# ---------------------------------------------------------------------------
# 任务 1-1：块序号现算
# ---------------------------------------------------------------------------


async def test_read_session_seq_exposed_storage_untouched(sandbox, tmp_path):
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "第一条")
    await session_store.append_message(wid, cid, "assistant", "回复一")
    await session_store.append_message(wid, cid, "user", "第二条")
    msgs = await session_store.read_session(wid, cid)
    assert [m["seq"] for m in msgs] == [0, 1, 2]
    # 存储格式零改动：文件内 header 无 seq 字段
    raw = session_store._session_path(wid, cid).read_text(encoding="utf-8")
    assert "seq:" not in raw
    # 既有键原样保留（消费方按键取用不受增键影响）
    assert msgs[0]["role"] == "user" and msgs[1]["content"] == "回复一"


# ---------------------------------------------------------------------------
# 任务 1-2：memory_store 空水位
# ---------------------------------------------------------------------------


async def test_memory_load_default_and_roundtrip(sandbox):
    wid, cid, _other = await _make_work_with_cards()
    st = memory_store.load_memory(wid, cid)
    assert st == {"card_id": cid, "mark": 0, "entries": []}
    data = {"card_id": cid, "mark": 3, "entries": [_entry(2, "【结论】x")]}
    await memory_store.save_memory(wid, cid, data)
    assert memory_store.load_memory(wid, cid) == data


async def test_ensure_watermark_idempotent_and_session_bound(sandbox):
    wid, cid, no_session = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "hi")
    assert await memory_store.ensure_watermark(wid, cid) is True
    first = memory_store.memory_path(wid, cid).read_text(encoding="utf-8")
    # 幂等：二次不覆盖
    assert await memory_store.ensure_watermark(wid, cid) is False
    assert memory_store.memory_path(wid, cid).read_text(encoding="utf-8") == first
    # 无会话的卡不建账（生产 create_card 预建空会话文件——铁律禁删，此处仅沙箱内
    # 构造「无会话」形态验证会话存在性判据）
    session_store._session_path(wid, no_session).unlink()
    assert await memory_store.ensure_watermark(wid, no_session) is False
    assert not memory_store.memory_path(wid, no_session).exists()
    st = json.loads(first)
    assert st["mark"] == 0 and st["entries"] == [] and st["card_id"] == cid


async def test_memory_bad_file_treated_empty_not_rewritten(sandbox):
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "hi")
    p = memory_store.memory_path(wid, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{这不是JSON", encoding="utf-8")
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 0 and st["entries"] == []
    # 原文件不被静默改写（V5 留痕哲学）
    assert p.read_text(encoding="utf-8") == "{这不是JSON"


# ---------------------------------------------------------------------------
# 任务 2-1/2-2＋V1/V6：200 区插入与开关
# ---------------------------------------------------------------------------


async def test_v6_switch_default_off_and_toggle(sandbox, monkeypatch):
    wid, cid, _other = await _make_work_with_cards()
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
                                              "entries": [_entry(1, "【结论】甲卡记忆")]})
    # 缺省（无环境变量）＝关：有 entries 也不出现 200 区（RD-08/V6）
    monkeypatch.delenv("CONDENSE_ENABLED", raising=False)
    assert memory_store.condense_enabled() is False
    assert [r["order"] for r in context_assembler.build_context_regions(wid, cid)] == [100, 150]
    # 开闸生效
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    assert [r["order"] for r in context_assembler.build_context_regions(wid, cid)] == [100, 150, 200]
    # 字面 "true" 亦开；其余值关
    monkeypatch.setenv("CONDENSE_ENABLED", "true")
    assert memory_store.condense_enabled() is True
    monkeypatch.setenv("CONDENSE_ENABLED", "0")
    assert memory_store.condense_enabled() is False
    # 关闸即回旧路（V6 后半）
    assert [r["order"] for r in context_assembler.build_context_regions(wid, cid)] == [100, 150]


async def test_v1_byte_equal_with_switch_on_and_empty_memory(sandbox, monkeypatch):
    """V1：开关开＋空记忆 → system/中性上下文与改造前逐字相等（字节级）。"""
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "历史一条")
    await memory_store.ensure_watermark(wid, cid)  # 空水位文件在盘
    legacy_text = _legacy_regions_text(wid, cid)
    new_ctx = context_assembler.build_neutral_context(wid, cid)
    assert new_ctx.encode("utf-8") == legacy_text.encode("utf-8")
    assert prompts.build_system_prompt(new_ctx).encode("utf-8") == _legacy_build_system_prompt(legacy_text).encode("utf-8")


async def test_v1_switch_off_byte_equal_even_with_entries(sandbox, monkeypatch):
    monkeypatch.delenv("CONDENSE_ENABLED", raising=False)
    wid, cid, _other = await _make_work_with_cards()
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
                                              "entries": [_entry(0, "【结论】关闸下不出现")]})
    new_ctx = context_assembler.build_neutral_context(wid, cid)
    assert new_ctx.encode("utf-8") == _legacy_regions_text(wid, cid).encode("utf-8")


async def test_200_region_shape_and_seats(sandbox, monkeypatch):
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, cid, _other = await _make_work_with_cards()
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 1,
                                              "entries": [_entry(0, "【结论】A0"), _entry(2, "【结论】A2")]})
    regions = context_assembler.build_context_regions(wid, cid)
    r200 = next(r for r in regions if r["order"] == 200)
    assert set(r200) == {"order", "name", "channel", "seats", "text", "entries"}
    assert r200["channel"] == "system" and r200["seats"] == ("main",) and r200["entries"] == 2
    # 区条目自动进 prompt 日志 regions（200 位补一区，六区口径不动）
    log_regions = context_regions.build_log_regions(regions, [], [], "u")
    assert 200 in [e["order"] for e in log_regions]


async def test_render_memory_region_zero_processing(sandbox):
    long_conclusion = "【结论】" + "长" * 5000  # RD-05：无长度限制、不截断
    entries = [_entry(2, long_conclusion), _entry(0, "【结论】先存的在前")]  # 故意乱序
    text = context_regions.render_memory_region(entries)
    assert text.splitlines()[0] == "## 记忆（condense_fold 凝练归拢）"
    # 存储序即输出序（不排序）；长结论一字不截（不截断）
    assert "【结论】先存的在前" in text
    assert long_conclusion in text
    lines = text.splitlines()
    assert lines[1].startswith("[块2 · 2026-10-05T12:00:00+08:00]")
    assert lines[2] == "【依据】read_card card_001（sha256:0123456789abcdef）"
    # 空条目 → 空串（没有来源就不出现这个区）
    assert context_regions.render_memory_region([]) == ""


# ---------------------------------------------------------------------------
# V2：一卡一链不串卡
# ---------------------------------------------------------------------------


async def test_v2_no_cross_card_leak(sandbox, monkeypatch):
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, card_a, card_b = await _make_work_with_cards()
    await memory_store.save_memory(wid, card_a, {"card_id": card_a, "mark": 0,
                                                 "entries": [_entry(0, "【结论】甲卡独有记忆ABC")]})
    await memory_store.save_memory(wid, card_b, {"card_id": card_b, "mark": 0,
                                                 "entries": [_entry(0, "【结论】乙卡独有记忆XYZ")]})
    ctx_a = context_assembler.build_neutral_context(wid, card_a)
    ctx_b = context_assembler.build_neutral_context(wid, card_b)
    assert "甲卡独有记忆ABC" in ctx_a and "乙卡独有记忆XYZ" not in ctx_a
    assert "乙卡独有记忆XYZ" in ctx_b and "甲卡独有记忆ABC" not in ctx_b


# ---------------------------------------------------------------------------
# V3：memory 写入不触碰会话/卡片
# ---------------------------------------------------------------------------


async def test_v3_session_and_card_bytes_untouched(sandbox):
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "原文一条")
    sess_path = session_store._session_path(wid, cid)
    card_path = card_store._card_path(wid, cid)
    sess_before = sess_path.read_bytes()
    card_before = card_path.read_bytes()
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 5, "entries": [_entry(1, "【结论】v3")]})
    await memory_store.ensure_watermark(wid, cid)
    assert sess_path.read_bytes() == sess_before
    assert card_path.read_bytes() == card_before


# ---------------------------------------------------------------------------
# 基础件：触发线 K／回合末异步触发（SP-01/SP-04）
# ---------------------------------------------------------------------------


async def test_turn_end_trigger_lazy_watermark_and_trace(sandbox, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "短历史")
    await memory_store.condense_turn_end(wid, cid)
    # 懒建账：触发即幂等落空水位
    assert memory_store.memory_path(wid, cid).exists()
    # 未到线：无「到线」留痕
    assert not any("已到线" in r.message for r in caplog.records)
    # [T15] 到线即启动凝练管线（路线甲闸开）：接缝脚本化产出→条目落盘→mark 推进
    monkeypatch.setenv("CONDENSE_TRIGGER_K", "1")

    async def fake_condense_call(user_prompt: str) -> str:
        return "【结论】短历史确立了一条用户消息。\n【依据】块0"

    monkeypatch.setattr(memory_store, "_condense_call", fake_condense_call)
    await memory_store.condense_turn_end(wid, cid)
    assert any("已到线" in r.message for r in caplog.records)
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 1 and len(st["entries"]) == 1
    assert st["entries"][0]["conclusion"] == "短历史确立了一条用户消息。"


async def test_maybe_schedule_off_noop_on_async(sandbox, monkeypatch):
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "hi")
    monkeypatch.delenv("CONDENSE_ENABLED", raising=False)
    memory_store.maybe_schedule_turn_end(wid, cid)  # 关＝零动作
    import asyncio
    await asyncio.sleep(0.05)
    assert not memory_store.memory_path(wid, cid).exists()
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    memory_store.maybe_schedule_turn_end(wid, cid)  # 开＝异步触发不阻塞
    import asyncio
    for _ in range(20):
        await asyncio.sleep(0.02)
        if memory_store.memory_path(wid, cid).exists():
            break
    assert memory_store.memory_path(wid, cid).exists()


async def test_trigger_k_env_override_and_default(sandbox, monkeypatch):
    monkeypatch.delenv("CONDENSE_TRIGGER_K", raising=False)
    assert memory_store.trigger_k_bytes() == memory_store.DEFAULT_TRIGGER_K_BYTES
    monkeypatch.setenv("CONDENSE_TRIGGER_K", "12345")
    assert memory_store.trigger_k_bytes() == 12345
    monkeypatch.setenv("CONDENSE_TRIGGER_K", "abc")
    assert memory_store.trigger_k_bytes() == memory_store.DEFAULT_TRIGGER_K_BYTES


# ---------------------------------------------------------------------------
# 基础件：read_memory 记忆锚点下探（R3）
# ---------------------------------------------------------------------------


def _cards_meta(wid: str, ids: list[str]) -> dict:
    return {cid: {"card_id": cid, "parent": "" if cid == "card_001" else "card_001",
                  "card_type": "normal"} for cid in ids}


async def test_read_memory_tool_filter_and_permission(sandbox, monkeypatch):
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, card_a, card_b = await _make_work_with_cards()
    await memory_store.save_memory(wid, card_a, {
        "card_id": card_a, "mark": 3,
        "entries": [_entry(0, "【结论】零号"), _entry(2, "【结论】贰号"), _entry(4, "【结论】肆号")]})
    meta = _cards_meta(wid, ["card_001", card_a, card_b])
    # 缺省＝当前卡全量（原样返回）
    r = await card_tools.execute_tool(wid, card_a, "read_memory", {}, meta)
    assert r["card_id"] == card_a and r["mark"] == 3 and len(r["entries"]) == 3
    # 锚点区间过滤（含端点）
    r = await card_tools.execute_tool(wid, card_a, "read_memory", {"from_seq": 2, "to_seq": 4}, meta)
    assert [e["seq"] for e in r["entries"]] == [2, 4]
    # 不存在的卡
    r = await card_tools.execute_tool(wid, card_a, "read_memory", {"card_id": "card_999"}, meta)
    assert "error" in r
    # [卡片权限改造 2026-10-07] 读侧全盘放开：平级 read_memory 放行
    # （原越权断言随亲属链读限机制退役）
    r = await card_tools.execute_tool(wid, card_b, "read_memory", {"card_id": card_a}, meta)
    assert r["card_id"] == card_a and r["mark"] == 3 and len(r["entries"]) == 3
    # 只读白名单在案（折叠【依据】依据源语义）
    assert "read_memory" in card_tools.READONLY_TOOLS


async def test_tool_table_has_read_memory_and_version(sandbox):
    tools = card_tools.build_tool_schemas()
    names = [t["name"] for t in tools]
    assert names.count("read_memory") == 1
    # [T-0004 §3.1 同步] tc-1 ＝ 建卡工具化（2026-10-08）增补 create_card 有意推进；
    # 同步纪律：版本号推进必须同步本用例，否则每次推进都会留一条假红。
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-1"
    # 建卡工具化后 read_memory 仍在案（未被取代，只是同表多一件）
    assert names.count("create_card") == 1
    # 语义随推进不变：read_memory 仍是只读件
    assert "read_memory" in card_tools.READONLY_TOOLS
    assert prompts.BASE_PROMPT_VERSION == "tc-1"
    # BASE_PROMPT 工具行在案（read_memory 行逐字未动；建卡工具化只增 create_card 行、
    # 删文本协议段——read_memory 语义不受影响）
    assert "read_memory(card_id?, from_seq?, to_seq?)" in BASE_PROMPT
