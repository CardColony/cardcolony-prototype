# -*- coding: utf-8 -*-
"""T15 · 凝练管线本体（路线甲）回归网：V1/V2/V3/V5/V7 mock 层＋V6 回归面。

V4（真实层真模型）不在 pytest——独立证据脚本 _t12_evidence/../_t15_evidence/
v4_real_run.py 对真实卡实跑（pytest 不碰网、不碰真实数据目录——铁律一）。

改动面（RD-10）：管线本体全在 memory_store.py；routes.py 仅 300 区收窄切片一处。
模型接缝 _condense_call 在 mock 层测试中脚本化（MockGateway 罐头无【结论】/【依据】
形态且 gateway.py 禁改——RD-10 两文件面不容第三件）；全链其余环节走真代码。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import pytest

from src.api import context_assembler, routes
from src.storage import card_store, memory_store, session_facade, session_store, work_store


def _entry_text(seq: int, conclusion: str) -> str:
    return f"【结论】{conclusion}\n【依据】块{seq}"


async def _make_work_with_card(prefix: str = "T15管线剧本"):
    wdata = await work_store.create_work(prefix)
    wid = wdata["work_id"]
    await card_store.create_card(wid, "根卡标题", "用户直接创建")
    return wid, "card_001"


async def _append_conversation(wid: str, cid: str, turns: int, filler: str = "讨论内容") -> None:
    for i in range(turns):
        await session_store.append_message(wid, cid, "user", f"{filler}用户第{i}块")
        await session_store.append_message(wid, cid, "assistant", f"{filler}助手第{i}块回复")


# ---------------------------------------------------------------------------
# V1 · mock 层全链：触发→取段→模型写→落条→推进 mark→200 区出现条目
# ---------------------------------------------------------------------------


async def test_v1_mock_full_chain(sandbox, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "1")
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 3)

    scripted = "\n".join([
        _entry_text(0, "【结论】占位甲"),
        _entry_text(0, "首条：用户与助手确立了问题定义。"),
        _entry_text(2, "末条：助手给出收束回复。"),
    ])

    async def fake_condense_call(user_prompt: str) -> str:
        return scripted

    monkeypatch.setattr(memory_store, "_condense_call", fake_condense_call)
    await memory_store.condense_turn_end(wid, cid)

    # 落 entries（五字段恰齐 ＋ [原文入库 2026-10-20] 追溯两键 seq_from/seq_to）
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 6  # 6 块（3 轮 u/a），末块 seq=5，mark=末块+1
    assert len(st["entries"]) == 3
    for e in st["entries"]:
        assert set(e) == {"seq", "conclusion", "evidence", "occurred_at", "fingerprint",
                          "seq_from", "seq_to"}, "条目须带压自哪段原文（追溯链）"
        # 追溯范围＝本批窗口（0..5 闭区间），不是反推
        assert (e["seq_from"], e["seq_to"]) == (0, 5)
    # fingerprint 可复算＝凝练输入（块前缀未凝段全文）sha256 前 16
    # [工具结果落会话 2026-10-17] 改用统一读入门面（带 actor）：_build_condense_user_prompt
    # 现按 actor∈{human,assistant} 过滤，旧读函数 session_store.read_session 不产出
    # actor ⇒ 算出的 fp 不再等于内部 fp。
    prior = await session_facade.read(wid, cid)
    expect_fp = hashlib.sha256(
        memory_store._build_condense_user_prompt(prior).encode("utf-8")).hexdigest()[:16]
    assert all(e["fingerprint"] == expect_fp for e in st["entries"])
    # seq＝该条依据引用的起始块号；不带块号的条目**按窗口内可送块逐条分配**（不重复）
    # [seq 身份修复 2026-10-22] 旧期望 [0, 0, 2] 是 bug 的固化：两条都落窗口首块，
    # 触发「I1 gist 身份重复」⇒ 记忆链拒绝物化。现在第一条用其块号 0，第二条分配 1。
    assert [e["seq"] for e in st["entries"]] == [0, 1, 2]

    # 200 区出现条目（渲染走 render_memory_region 真代码）
    regions = context_assembler.build_context_regions(wid, cid)
    r200 = next(r for r in regions if r["order"] == 200)
    assert r200["entries"] == 3
    assert "首条：用户与助手确立了问题定义。" in r200["text"]
    assert "[块2" in r200["text"]

    # 打点对账（任务 8）：触发＋完成两条在案，完成条含输入块数/产出条数/mark 前后
    msgs = [r.getMessage() for r in caplog.records if "condense_fold" in r.getMessage()]
    assert any("已到线" in m and "未凝token=" in m for m in msgs)
    done = [m for m in msgs if "凝练完成" in m]
    assert len(done) == 1 and "输入块数=6" in done[0] and "产出条数=3" in done[0] \
        and "mark 0→6" in done[0]

    # 开关缺省关＝装配与现状逐字一致（V1 后半；真字节级基准见 T12 套件）


async def test_v1_switch_off_no_slice_no_region(sandbox, monkeypatch):
    """开关关：condense_state None→全量投影＋无 200 区（逐字旧路）。"""
    monkeypatch.delenv("CONDENSE_ENABLED", raising=False)
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 2)
    # 预置 entries（开关关也不该出现在装配里）
    await memory_store.save_memory(wid, cid, {
        "card_id": cid, "mark": 2,
        "entries": [{"seq": 0, "conclusion": "【结论】关闸不得出现", "evidence": "【依据】块0",
                     "occurred_at": "t", "fingerprint": "fp"}]})
    regions = context_assembler.build_context_regions(wid, cid)
    # [卡片职责区 2026-10-14] 迭代新增 order 90（卡片职责）
    # [2026-10-21] 配置演进至 v9 新增 order 92（card_ability）；测试构造的
    # 真实 work/card 会渲染出 92 区，按实测渲染结果同步期望（非照抄 22 区）
    assert [r["order"] for r in regions] == [90, 92, 100, 150, 155]
    assert all("关闸不得出现" not in r["text"] for r in regions)


# ---------------------------------------------------------------------------
# V2 · 幂等：同窗口重复触发→fingerprint 去重、entries 无重复、mark 不重复推进
# ---------------------------------------------------------------------------


async def _run_once(wid, cid, monkeypatch, scripted):
    async def fake(user_prompt: str) -> str:
        return scripted
    monkeypatch.setattr(memory_store, "_condense_call", fake)
    await memory_store.condense_turn_end(wid, cid)
    monkeypatch.undo()


async def test_v2_idempotent_same_window(sandbox, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "1")
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 2)

    async def fake_condense_call(user_prompt: str) -> str:
        return _entry_text(0, "唯一结论。")

    monkeypatch.setattr(memory_store, "_condense_call", fake_condense_call)
    await memory_store.condense_turn_end(wid, cid)
    st1 = memory_store.load_memory(wid, cid)
    assert st1["mark"] == 4 and len(st1["entries"]) == 1  # 2 轮=4 块

    # 同窗口二次触发：mark 已覆盖全窗→窗口空→低于 K 静默零动作（不重凝、不重推、零日志）
    caplog.clear()
    await memory_store.condense_turn_end(wid, cid)
    st2 = memory_store.load_memory(wid, cid)
    assert st2 == st1
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("凝练完成" in m or "幂等" in m or "已到线" in m for m in msgs)


async def test_v2_idempotent_repair_mark(sandbox, monkeypatch, caplog):
    """fingerprint 去重主证：entries 已在而 mark 落后（防御形态）→ 同指纹条目零
    新增＋mark 幂等补推（V2「无重复条目、mark 不重复推进」的管线内判定面）。"""
    caplog.set_level(logging.INFO, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "1")
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 2)
    # [工具结果落会话 2026-10-17] 同 test_v1：fp 须用与内部同源的读入门面算（带 actor）
    prior = await session_facade.read(wid, cid)
    fp = hashlib.sha256(
        memory_store._build_condense_user_prompt(prior).encode("utf-8")).hexdigest()[:16]
    # 手工置成「条目已在、mark 落后」形态
    await memory_store.save_memory(wid, cid, {
        "card_id": cid, "mark": 0,
        "entries": [{"seq": 0, "conclusion": "已在条目", "evidence": "【依据】块0",
                     "occurred_at": "t", "fingerprint": fp}]})

    async def fake_condense_call(user_prompt: str) -> str:
        return _entry_text(0, "不该新增的条目。")

    monkeypatch.setattr(memory_store, "_condense_call", fake_condense_call)
    await memory_store.condense_turn_end(wid, cid)
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 4 and len(st["entries"]) == 1
    assert st["entries"][0]["conclusion"] == "已在条目"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("幂等补推" in m for m in msgs)
    assert not any("凝练完成" in m for m in msgs)


# ---------------------------------------------------------------------------
# V3 · 失败注入：水位不动、warning 留痕一条、不阻断、后续正常
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_return,stage", [
    ("", "产出解析"),                 # 空产出
    ("模型胡言乱语没有任何标记", "产出解析"),  # 无【结论】标记
])
async def test_v3_parse_failure_watermark_untouched(sandbox, monkeypatch, caplog, bad_return, stage):
    caplog.set_level(logging.WARNING, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "1")
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 2)

    async def fake_condense_call(user_prompt: str) -> str:
        return bad_return

    monkeypatch.setattr(memory_store, "_condense_call", fake_condense_call)
    await memory_store.condense_turn_end(wid, cid)  # 不抛——零阻断
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 0 and st["entries"] == []
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
             and "凝练失败" in r.getMessage()]
    assert len(warns) == 1 and f"阶段={stage}" in warns[0] and "水位一分不动" in warns[0]

    # 后续正常调用不受影响
    caplog.clear()

    async def good_condense_call(user_prompt: str) -> str:
        return _entry_text(0, "恢复后的结论。")

    monkeypatch.setattr(memory_store, "_condense_call", good_condense_call)
    await memory_store.condense_turn_end(wid, cid)
    st2 = memory_store.load_memory(wid, cid)
    assert st2["mark"] == 4 and len(st2["entries"]) == 1


async def test_v3_call_exception_watermark_untouched(sandbox, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "1")
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 2)

    async def boom(user_prompt: str) -> str:
        raise RuntimeError("模拟网关异常")

    monkeypatch.setattr(memory_store, "_condense_call", boom)
    await memory_store.condense_turn_end(wid, cid)
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 0 and st["entries"] == []
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
             and "凝练失败" in r.getMessage()]
    assert len(warns) == 1 and "阶段=模型调用" in warns[0] and "模拟网关异常" in warns[0]


# ---------------------------------------------------------------------------
# V5 · 300 区收窄：开关开→装配历史只含 mark 后未凝段＋200 区条目；关→全量
# ---------------------------------------------------------------------------


async def _e2e_capture(wid: str, cid: str, monkeypatch, reply: str = "收束回复。"):
    captured: list[dict] = []

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None, **_):  # [迭代三 S5 同步] model_name kwargs 吸收
        captured.append({
            "system": system_prompt,
            "prior": [dict(m) for m in (prior_messages or [])],
        })
        return ({"content": reply, "tool_calls": []}, "fake")

    monkeypatch.setattr(routes.model_service, "call_main", fake_call_main)
    result = await routes._run_message(wid, cid, "端到端收窄验证提问")
    assert "error" not in result
    return captured


async def test_v5_slice_on_and_off(sandbox, monkeypatch):
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 2)
    # 预置水位：块 0、1 已凝，块 2、3 未凝
    await memory_store.save_memory(wid, cid, {
        "card_id": cid, "mark": 2,
        "entries": [{"seq": 0, "conclusion": "【结论】甲卡已凝要点", "evidence": "【依据】块0",
                     "occurred_at": "t", "fingerprint": "seedfp"}]})

    # 开关开：历史只含 mark 后未凝段（2 条），system 含 200 区条目
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    captured_on = await _e2e_capture(wid, cid, monkeypatch)
    wires = [c for c in captured_on if c["prior"]]
    assert wires, "主循环装配未捕获到历史"
    wire = wires[0]["prior"]
    assert [m["role"] for m in wire] == ["user", "assistant"]
    assert wire[0]["content"] == "讨论内容用户第1块"
    assert any("甲卡已凝要点" in c["system"] for c in captured_on)

    # 开关关（新 work）：全量投影＋无 200 区（逐字旧路）
    wid2, cid2 = await _make_work_with_card("T15关闸剧本")
    await _append_conversation(wid2, cid2, 2)
    await memory_store.save_memory(wid2, cid2, {
        "card_id": cid2, "mark": 2,
        "entries": [{"seq": 0, "conclusion": "【结论】关闸不得出现", "evidence": "【依据】块0",
                     "occurred_at": "t", "fingerprint": "seedfp"}]})
    monkeypatch.delenv("CONDENSE_ENABLED", raising=False)
    captured_off = await _e2e_capture(wid2, cid2, monkeypatch)
    wires_off = [c for c in captured_off if c["prior"]]
    assert wires_off and len(wires_off[0]["prior"]) == 4
    assert all("关闸不得出现" not in c["system"] for c in captured_off)


# ---------------------------------------------------------------------------
# V7 · 同卡互斥：凝练进行中二次触发→跳过留痕、无重复条目
# ---------------------------------------------------------------------------


async def test_v7_mutex_second_trigger_skips(sandbox, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "1")
    wid, cid = await _make_work_with_card()
    await _append_conversation(wid, cid, 2)

    release = asyncio.Event()

    async def slow_condense_call(user_prompt: str) -> str:
        await release.wait()
        return _entry_text(0, "慢凝练唯一结论。")

    monkeypatch.setattr(memory_store, "_condense_call", slow_condense_call)
    first = asyncio.create_task(memory_store.condense_turn_end(wid, cid))
    await asyncio.sleep(0.05)  # 让第一轮进入模型调用（已占互斥位）
    second = asyncio.create_task(memory_store.condense_turn_end(wid, cid))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)

    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 4 and len(st["entries"]) == 1  # 无重复条目
    msgs = [r.getMessage() for r in caplog.records]
    assert any("触发跳过" in m and "互斥" in m for m in msgs)
    assert sum("凝练完成" in m for m in msgs) == 1


# ---------------------------------------------------------------------------
# V6 · 回归面：T12 基础件与解析器机械性质
# ---------------------------------------------------------------------------


def test_parser_block_refs_and_continuation():
    text = "前言杂文应被丢弃\n【结论】第一条结论很长：\n续行也算结论\n【依据】见 块3 与 [块7]\n依据续行\n【结论】第二条\n【依据】无块号"
    parsed = memory_store._parse_condense_output(text, default_seq=9)
    assert len(parsed) == 2
    assert parsed[0]["seq"] == 3 and parsed[1]["seq"] == 9  # 最小块号；无块号→窗口首块兜底
    assert "续行也算结论" in parsed[0]["conclusion"]
    assert "依据续行" in parsed[0]["evidence"]
    assert memory_store._parse_condense_output("", 0) == []
    assert memory_store._parse_condense_output("没有任何标记", 0) == []


def test_parser_same_line_evidence_real_model_shape():
    """真模型实测形态：【依据】写在【结论】同一行（V4 首跑抓到的缺陷）。"""
    text = ("【结论】土地财政是复合议题。【依据】[块5][块15][块16]\n"
            "【结论】股权财政是资产负债表命题。【依据】块45、块46")
    parsed = memory_store._parse_condense_output(text, default_seq=0)
    assert [e["seq"] for e in parsed] == [5, 45]
    assert parsed[0]["conclusion"] == "土地财政是复合议题。"
    assert "【依据】" not in parsed[0]["conclusion"]
    assert parsed[0]["evidence"] == "[块5][块15][块16]"
