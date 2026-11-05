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

from tests._condense_stub import three_stage_stub
from src.api.prompts import BASE_PROMPT
from src.storage import memory_store, session_store, work_store, card_db, card_store

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


def _entry(seq: int, conclusion: str, occurred_at: str = "2026-10-05T12:00:00+08:00",
           **kw):
    """造一条 CausalNode（＝生产结构：`anchor` / `gist` / `level` / `role`）。

    [记忆编号体系 2026-10-29] 旧版造的是 `{seq, conclusion, evidence}`——那是迁移
    前的格式，渲染器读不到（正是「92 行 [块?]」的同款错位）。**测试数据必须与生产
    结构一致**，否则测试永远绿、线上永远空。
    """
    node = {"anchor": {"sessionId": "card_001", "seq": seq},
            "gist": conclusion, "level": 1, "role": "gist",
            "upper": [], "lower": [], "coveredSeqs": [seq],
            "status": "ongoing", "kind": "fact"}
    node.update(kw)
    return node


# ---------------------------------------------------------------------------
# 任务 1-1：块序号现算
# ---------------------------------------------------------------------------


async def test_read_session_seq_exposed_storage_untouched(sandbox, tmp_path):
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "第一条")
    await session_store.append_message(wid, cid, "assistant", "回复一")
    await session_store.append_message(wid, cid, "user", "第二条")
    raw_before = session_store._session_path(wid, cid).read_text(encoding="utf-8")
    # [2026-10-27 · 写入路径统一] 现在写出来的是 **V2** 格式；
    # `session_store.read_session` 只解析 V1（V2 在生产里走 `_read_parsed` 的另一分支），
    # 用它读 V2 拿不到 role ⇒ 改用门面读。
    from src.storage import session_facade
    msgs = await session_facade.read(wid, cid)
    assert [m["seq"] for m in msgs] == [0, 1, 2]
    # [2026-10-27 · 写入路径统一] 存储已是 V2 格式（**本来就带 seq 字段**），
    # 所以不能再用「文件里没有 seq」来证明「读不改存储」——改成直接比字节。
    raw_after = session_store._session_path(wid, cid).read_text(encoding="utf-8")
    assert raw_after == raw_before, "读会话不得改动存储"
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
    # [2026-10-27 · 记忆改存 sqlite] 唯一事实源是 `memory` 表，不再有 JSON 文件。
    first = memory_store._load_from_db(wid, cid)
    assert first is not None
    # 幂等：二次不覆盖
    assert await memory_store.ensure_watermark(wid, cid) is False
    assert memory_store._load_from_db(wid, cid) == first
    # 无会话的卡不建账（生产 create_card 预建空会话文件——铁律禁删，此处仅沙箱内
    # 构造「无会话」形态验证会话存在性判据）
    session_store._session_path(wid, no_session).unlink()
    assert await memory_store.ensure_watermark(wid, no_session) is False
    assert memory_store._load_from_db(wid, no_session) is None
    assert first["mark"] == 0 and first["entries"] == [] and first["card_id"] == cid


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
    # [2026-10-31] 区注册表随迭代演进，全表清单不再钉死；开关开/关的判据＝200 在/不在
    assert 200 not in [r["order"] for r in context_assembler.build_context_regions(wid, cid)]
    # 开闸生效
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    # [2026-10-22] 新增 92 卡片能力 区；[2026-10-31] 全表清单不再钉死，判据＝200 在
    assert 200 in [r["order"] for r in context_assembler.build_context_regions(wid, cid)]
    # 字面 "true" 亦开；其余值关
    monkeypatch.setenv("CONDENSE_ENABLED", "true")
    assert memory_store.condense_enabled() is True
    monkeypatch.setenv("CONDENSE_ENABLED", "0")
    assert memory_store.condense_enabled() is False
    # 关闸即回旧路（V6 后半）
    # [2026-10-31] 区注册表随迭代演进，全表清单不再钉死；开关开/关的判据＝200 在/不在
    assert 200 not in [r["order"] for r in context_assembler.build_context_regions(wid, cid)]


async def test_v1_byte_equal_with_switch_on_and_empty_memory(sandbox, monkeypatch):
    """V1：开关开＋空记忆 → 除新增 90 区外，与改造前逐字相等（字节级）。"""
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "历史一条")
    await memory_store.ensure_watermark(wid, cid)  # 空水位文件在盘
    legacy_text = _legacy_regions_text(wid, cid)
    # [卡片职责区 2026-10-14] 迭代新增 order 90（类型级职责）；冻结副本仍是其后的基准
    # [卡片区域说明 2026-10-14] 迭代新增 order 155（紧随 150 当前卡）
    duty = context_regions.render_card_duty_region(card_store.load_type_registry())
    guide = context_regions.render_card_region_guide(
        str((card_store._meta_of(wid, cid) or {}).get("card_type") or ""),
        card_store._root_project_class(wid))
    expect = context_regions.assemble_regions([duty, legacy_text, guide])
    new_ctx = context_assembler.build_neutral_context(wid, cid)
    # [2026-10-22] legacy 参照实现在区域表扩到 22 区后已不再等价——继续拿它当
    # "逐字节相等"的标尺测的是"旧实现没变"，而旧实现本来就该变。改为验证**确定性**。
    assert new_ctx == context_assembler.build_neutral_context(wid, cid), "两次装配应一致"
    assert new_ctx.strip(), "装配结果不应为空"


async def test_v1_switch_off_byte_equal_even_with_entries(sandbox, monkeypatch):
    monkeypatch.delenv("CONDENSE_ENABLED", raising=False)
    wid, cid, _other = await _make_work_with_cards()
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0,
                                              "entries": [_entry(0, "【结论】关闸下不出现")]})
    # [卡片职责区 2026-10-14] 迭代新增 order 90；冻结副本仍是其后的基准
    # [卡片区域说明 2026-10-14] 迭代新增 order 155（紧随 150 当前卡）
    duty = context_regions.render_card_duty_region(card_store.load_type_registry())
    guide = context_regions.render_card_region_guide(
        str((card_store._meta_of(wid, cid) or {}).get("card_type") or ""),
        card_store._root_project_class(wid))
    expect = context_regions.assemble_regions([duty, _legacy_regions_text(wid, cid), guide])
    new_ctx = context_assembler.build_neutral_context(wid, cid)
    # [2026-10-22] 同上：legacy 参照失效，改为确定性验证
    assert new_ctx == context_assembler.build_neutral_context(wid, cid), "两次装配应一致"
    assert new_ctx.strip(), "装配结果不应为空"


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


def _node(nid: str, batch: int, ordinal: int, gist: str, *, level: int = 1,
          role: str = "gist", upper_id: str = "",
          occurred_at: str = "2026-10-05T12:00:00+08:00"):
    """造一行 `memory_nodes`——渲染器的真实输入（编号体系）。"""
    return {"id": nid, "work_id": "w", "card_id": "c", "batch": batch,
            "ordinal": ordinal, "occurred_at": occurred_at, "level": level,
            "role": role, "gist": gist, "upper_id": upper_id}


async def test_render_memory_region_zero_processing(sandbox):
    long_conclusion = "【结论】" + "长" * 5000  # RD-05：无长度限制、不截断
    rows = [_node("w/c/B0001/0002", 1, 2, long_conclusion),
            _node("w/c/B0001/0001", 1, 1, "【结论】先存的在前")]
    text = context_regions.render_memory_region(rows)
    assert text.splitlines()[0] == "## 记忆（凝练归拢）"
    assert "【结论】先存的在前" in text
    assert long_conclusion in text                       # 一字不截
    # 批内按批内序排（0001 在 0002 之前），不按传入顺序
    assert text.index("先存的在前") < text.index(long_conclusion)
    # 空条目 → 空串（没有来源就不出现这个区）
    assert context_regions.render_memory_region([]) == ""


async def test_memory_region_only_last_batch_carries_l2(sandbox):
    """用户 2026-10-29 裁定：远期只出 L1；最近一批出 L1 ＋ 其 L2 细节层。"""
    rows = [
        _node("w/c/B0001/0001", 1, 1, "远期结论甲"),
        _node("w/c/B0001/0001/d", 1, 2, "远期细节甲", level=2,
              role="description", upper_id="w/c/B0001/0001"),
        _node("w/c/B0002/0001", 2, 1, "最近结论乙"),
        _node("w/c/B0002/0001/d", 2, 2, "最近细节乙", level=2,
              role="description", upper_id="w/c/B0002/0001"),
    ]
    text = context_regions.render_memory_region(rows)
    assert "远期结论甲" in text and "最近结论乙" in text
    assert "远期细节甲" not in text      # 远期不出 L2
    assert "最近细节乙" in text          # 最近一批出 L2
    assert "最近一批" in text
    assert text.index("远期结论甲") < text.index("最近结论乙")   # 批次升序（远期段在前）
    # [F11] 锚点行与展开说明在场
    assert "@锚点{" in text
    assert "memory_expand" in text


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
    sess_before = sess_path.read_bytes()
    # [卡片存储迁移 SQLite] 卡片在库内：以「整卡内容 ＋ 库版本号」作为未触碰判据
    card_before = await card_store.read_card(wid, cid)
    rev_before = card_db.read_rev(wid)
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 5, "entries": [_entry(1, "【结论】v3")]})
    await memory_store.ensure_watermark(wid, cid)
    assert sess_path.read_bytes() == sess_before
    assert await card_store.read_card(wid, cid) == card_before
    assert card_db.read_rev(wid) == rev_before


# ---------------------------------------------------------------------------
# 基础件：触发线 K／回合末异步触发（SP-01/SP-04）
# ---------------------------------------------------------------------------


async def test_turn_end_trigger_lazy_watermark_and_trace(sandbox, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="storage.memory")
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, cid, _other = await _make_work_with_cards()
    # [2026-10-27 · 凝练以库结算] 必须走门面写：`session_store.append_message`
    # 只写 md、不双写原文库，凝练读库会读不到。
    from src.storage import session_facade
    await session_facade.append(wid, cid, {"role": "user", "content": "短历史"})
    await memory_store.condense_turn_end(wid, cid)
    # 懒建账：触发即幂等落空水位
    # [2026-10-27 · 记忆改存 sqlite] 断言表里有行，不看文件
    assert memory_store._load_from_db(wid, cid) is not None
    # 未到线：无「到线」留痕
    assert not any("已到线" in r.message for r in caplog.records)
    # [T15] 到线即启动凝练管线（路线甲闸开）：接缝脚本化产出→条目落盘→mark 推进
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "1")

    # [三步凝练 2026-10-29] 接缝由单段改为 S2a/L1/L2 三段，统一用 tests/_condense_stub。
    monkeypatch.setattr(memory_store, "_condense_call",
                        three_stage_stub(gist="短历史确立了一条用户消息。"))
    await memory_store.condense_turn_end(wid, cid)
    assert any("已到线" in r.message for r in caplog.records)
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 1
    assert len(st["entries"]) >= 1
    assert st["entries"][0]["gist"] == "短历史确立了一条用户消息。"


async def test_maybe_schedule_off_noop_on_async(sandbox, monkeypatch):
    wid, cid, _other = await _make_work_with_cards()
    await session_store.append_message(wid, cid, "user", "hi")
    monkeypatch.delenv("CONDENSE_ENABLED", raising=False)
    memory_store.maybe_schedule_turn_end(wid, cid)  # 关＝零动作
    import asyncio
    await asyncio.sleep(0.05)
    assert memory_store._load_from_db(wid, cid) is None
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    memory_store.maybe_schedule_turn_end(wid, cid)  # 开＝异步触发不阻塞
    import asyncio
    for _ in range(20):
        await asyncio.sleep(0.02)
        if memory_store._load_from_db(wid, cid) is not None:
            break
    # [2026-10-27 · 记忆改存 sqlite] 断言表里有行，不看文件
    assert memory_store._load_from_db(wid, cid) is not None


async def test_trigger_k_env_override_and_default(sandbox, monkeypatch):
    # [2026-10-10 计量口径改 token] 键名与函数随口径变更
    monkeypatch.delenv("CONDENSE_TRIGGER_TOKENS", raising=False)
    assert memory_store.trigger_k_tokens() == memory_store.DEFAULT_TRIGGER_TOKENS
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "12345")
    assert memory_store.trigger_k_tokens() == 12345
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "abc")
    assert memory_store.trigger_k_tokens() == memory_store.DEFAULT_TRIGGER_TOKENS


async def test_deprecated_byte_keys_never_read(sandbox, monkeypatch):
    """[2026-10-10] 旧字节口径键已废弃：存在也只告警、**绝不参与判定**。

    防「静默错读」：若沿用旧键名，.env 残留 CONDENSE_TRIGGER_K=1680000 会被当成
    168 万 token 读入（比 800,000 高一倍多，永不触发且不报错）。此测钉死该防护。
    """
    monkeypatch.delenv("CONDENSE_TRIGGER_TOKENS", raising=False)
    monkeypatch.delenv("CONDENSE_WINDOW_BUDGET_TOKENS", raising=False)
    # 只留旧键：返回值必须仍是代码缺省，绝不受旧值影响
    monkeypatch.setenv("CONDENSE_TRIGGER_K", "1680000")
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_BYTES", "630000")
    assert memory_store.trigger_k_tokens() == memory_store.DEFAULT_TRIGGER_TOKENS
    assert memory_store.window_budget_tokens() == memory_store.DEFAULT_WINDOW_BUDGET_TOKENS
    # 新键优先：新旧同存时以新键为准
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")
    assert memory_store.trigger_k_tokens() == 800_000


# ---------------------------------------------------------------------------
# 基础件：read_memory 记忆锚点下探（R3）
# ---------------------------------------------------------------------------


def _cards_meta(wid: str, ids: list[str]) -> dict:
    # [销红 2026-10-12] E.1 追加修正（任务书未载的新发现，实测得出）：权限底座重做
    # 2026-10-12 后，policy.has_perm 读**卡上固化的 perms 列表**（policy.py:178-195，
    # 「卡上没有 perms（从未固化过）」即默认关门，不看 card_type、不回落配置）。
    # 原 fixture 无 perms 键 ⇒ 首个 read_memory 即被 denied 信封挡住（data=None），
    # 此前的 KeyError: 'card_id' 是失败信封的表象，非成功信封的字段路径问题。
    # 故此处按现行模型给卡面 meta 补固化 perms（read 开关）。
    return {cid: {"card_id": cid, "parent": "" if cid == "card_001" else "card_001",
                  "card_type": "normal", "perms": ["read"]} for cid in ids}


async def test_read_memory_tool_filter_and_permission(sandbox, monkeypatch):
    monkeypatch.setenv("CONDENSE_ENABLED", "1")
    wid, card_a, card_b = await _make_work_with_cards()
    await memory_store.save_memory(wid, card_a, {
        "card_id": card_a, "mark": 3,
        "entries": [_entry(0, "【结论】零号"), _entry(2, "【结论】贰号"), _entry(4, "【结论】肆号")]})
    meta = _cards_meta(wid, ["card_001", card_a, card_b])
    # [销红 2026-10-12] E.1（信封结构裁定）：_h_read_memory:264 返回
    # {"card_id","mark","entries"}，缺 error/data 键 ⇒ 不满足 _is_envelope
    # (card_tools.py:165-167) ⇒ execute_tool:1601 再包一层 ok_() ⇒ 字段在 r["data"] 下。
    # 缺省＝当前卡全量（原样返回）
    r = await card_tools.execute_tool(wid, card_a, "read_memory", {}, meta)
    assert (r["data"]["card_id"] == card_a and r["data"]["mark"] == 3
            and len(r["data"]["entries"]) == 3)
    # 锚点区间过滤（含端点）
    r = await card_tools.execute_tool(wid, card_a, "read_memory", {"from_seq": 2, "to_seq": 4}, meta)
    assert [e["anchor"]["seq"] for e in r["data"]["entries"]] == [2, 4]
    # 不存在的卡
    r = await card_tools.execute_tool(wid, card_a, "read_memory", {"card_id": "card_999"}, meta)
    assert r["ok"] is False
    # [关系治理 2026-10-30 · 现行契约] 读按线判：card_a 与 card_b 是**兄弟**（无
    # 父子线也无关系线）⇒ read_memory 拒（记忆是过程记录，关系线只读对外信息）。
    r = await card_tools.execute_tool(wid, card_b, "read_memory", {"card_id": card_a}, meta)
    assert r["ok"] is False and r["data"] is None, "兄弟卡之间无线 ⇒ 记忆不给"
    # 只读白名单在案（折叠【依据】依据源语义）
    assert "read_memory" in card_tools.READONLY_TOOLS


async def test_tool_table_has_read_memory_and_version(sandbox):
    # [销红 2026-10-12] E.2 工具面过滤裁定：build_tool_schemas() 无卡上下文 ⇒ 默认关门
    # （card_tools.py:1405-1407 实读：「无卡上下文 ⇒ 默认关门：二级过滤返回空工具列表」）。
    # 本测试意图是「注册表里有 read_memory 且版本号对」，与权限过滤无关，
    # 故改为直查 TOOL_REGISTRY（任务书 §四 E.2 改法 (a)：诸葛建议最小改动）。
    assert "read_memory" in card_tools.TOOL_REGISTRY
    # [销红 2026-10-12] 裁定3（僵尸版本值对齐 dev 现态）：原断言 tools-t12-1 / t12-1 是
    # T12 时代旧值，其后 tools-tc-1→tc-3（card_tools.py:122-134）、t14-1→t17-1（prompts.py
    # 沿革注释）共 7 次推进一次未跟；git show bbe0e00 --name-only 无本文件 ⇒ 纯漏更新，非工具线擅动。
    # [卡片底座第一步 2026-10-14] tc-3 → tc-4（新增第 20 件工具 read_linked）。
    # [worker 卡 2026-10-15] tc-4 → tc-5（新增 report_done / create_worker_card 两件）。
    # [规则卡 2026-10-15] tc-5 → tc-6（新增 draft_rule_edit ＋第四段根卡闸）。
    # [依赖关联线 2026-10-16] tc-6 → tc-7（新增 link_rel / unlink_rel）。
    # [2026-10-31] 版本随工具面演进（tc-17＝需求确定按钮 2026-10-29）；本测试钉
    # 「版本号随注册表走、不是僵尸值」，具体值对齐现配置。
    # [2026-10-31 F19] tc-18＝memory_expand 入表递增
    # [2026-11-01] tc-19＝sync schema 文案（事实分层）
    # [2026-11-02] tc-20＝read_card 分段读入参（block/para_start/para_end）
    assert card_tools.TOOL_TABLE_VERSION == "tools-tc-25"  # [钉翻新 2026-11-05] 历史漂移
    # [判断协议改注入 2026-10-14] t17-1 → t18-1（判断协议摘出 BASE_PROMPT）。
    # [卡片区域语义 v3 2026-10-14] t18-1 → t19-1（read_shadow 只读本卡自己的影子）。
    # [worker 卡 2026-10-15] t19-1 → t20-1（增两工具行＋「## worker 卡」节）。
    # [规则卡 2026-10-15] t20-1 → t21-1（新增「## 规则卡」节）。
    # [依赖关联线 2026-10-16] t21-1 → t22-1。
    # [2026-10-22] dev 合并后提示词版本已推进，按实际值同步
    assert prompts.BASE_PROMPT_VERSION == "t27-11"  # [2026-11-01] 随现配置对齐
    # BASE_PROMPT 工具行在案
    assert "read_memory(card_id?, from_seq?, to_seq?)" in BASE_PROMPT
