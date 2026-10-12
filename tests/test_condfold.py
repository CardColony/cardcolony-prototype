# -*- coding: utf-8 -*-
"""[凝练折叠开启 2026-10-07] 机制层单测（V1-V5，沙箱；V7/V9 实弹另见 livefire）。

V1 触发线 env 生效；V2 单批切片宁少不超；V3 模型位改指 condense 绑定（glm-flash）；
V4 归拢游标式触发（失败不推进）；V5 同口径铁律（触发求和与切片累计同尺差 0）。
"""
from __future__ import annotations

import json

import pytest

from src.storage import memory_store


def _mk_msg(seq: int, text: str) -> dict:
    return {"seq": seq, "role": "user", "content": text, "timestamp": "2026-10-07T12:00:00"}


# ---------------------------------------------------------------- V1 触发线/预算/批上限

def test_v1_env_overrides(sandbox, monkeypatch):
    monkeypatch.setenv("CONDENSE_TRIGGER_TOKENS", "800000")   # [2026-10-10] 口径改 token
    assert memory_store.trigger_k_tokens() == 800_000
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "300000")
    assert memory_store.window_budget_tokens() == 300_000
    monkeypatch.setenv("CONDENSE_MAX_BATCHES", "20")
    assert memory_store.condense_max_batches() == 20
    # 缺省链路（不设 env）→ 代码缺省值
    monkeypatch.delenv("CONDENSE_WINDOW_BUDGET_TOKENS", raising=False)
    monkeypatch.delenv("CONDENSE_MAX_BATCHES", raising=False)
    assert memory_store.window_budget_tokens() == memory_store.DEFAULT_WINDOW_BUDGET_TOKENS
    assert memory_store.condense_max_batches() == memory_store.DEFAULT_MAX_BATCHES


# ---------------------------------------------------------------- V2 单批切片

def test_v2_truncate_window_ning_shao_bu_chao(sandbox):
    budget = 100
    # [2026-10-10 口径改 token] 每条 meter = cl100k token 数 + 1（join 分隔符）
    # 实测「六六六六六六」= 12 token → 每条 13；7 条 91 ≤ 100，第 8 条 104 > 100
    pending = [_mk_msg(i, "六六六六六六") for i in range(10)]
    assert memory_store._msg_meter_tokens(pending[0]) == 13
    window = memory_store._truncate_window(pending, budget)
    assert 1 <= len(window) < len(pending)
    assert memory_store._meter_sum(window) <= budget                 # 宁少不超
    # 剩余留切片尾之后（V2：mark 推进到切片尾，剩余未凝段留 mark 之后）
    rest = pending[len(window):]
    assert rest and memory_store._meter_sum(window) + memory_store._meter_sum(rest) \
        == memory_store._meter_sum(pending)
    # 单条即超预算 → 保底 1 条防卡死
    huge = [_mk_msg(0, "巨" * 500)]
    assert memory_store._truncate_window(huge, 100) == huge[:1]
    # 空窗口 → 空
    assert memory_store._truncate_window([], budget) == []


# ---------------------------------------------------------------- V3 模型位

async def test_v3_condense_call_targets_glm_flash(sandbox, monkeypatch):
    seen: dict = {}

    async def fake_call_main(system_prompt, user_prompt, prior_messages=None, tools=None,
                             model_name=None):
        seen["model_name"] = model_name
        return ({"content": "【结论】x【依据】块1"}, "glm-5.3-flash")

    from src.api import model_service
    monkeypatch.setattr(model_service, "call_main", fake_call_main)
    await memory_store._condense_call("hello")
    assert seen["model_name"] == "glm-flash"
    await memory_store._fold_call("world")
    assert seen["model_name"] == "glm-flash"


# ---------------------------------------------------------------- V4 归拢游标

def _state_with(count: int, last: int, n_entries: int = 4) -> dict:
    return {
        "card_id": "card_001", "mark": 10,
        "batch_count": count, "last_fold_check": last, "folded": [],
        "entries": [{"seq": i, "conclusion": f"条目{i}", "evidence": f"【依据】块{i}",
                     "occurred_at": "t", "fingerprint": f"fp{i}"} for i in range(n_entries)],
    }


async def test_v4_fold_triggers_at_cursor_and_failure_holds(sandbox, work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    calls: list[str] = []

    async def fake_fold(user_prompt: str) -> str:
        calls.append(user_prompt)
        return json.dumps({"merges": [{"covers": [1, 3],
                                       "conclusion": "【结论】合并条",
                                       "evidence": "【依据】块0、块2"}]}, ensure_ascii=False)

    monkeypatch.setattr(memory_store, "_fold_call", fake_fold)
    # 游标差 < 5 → 零触发
    await memory_store.save_memory(wid, cid, _state_with(count=4, last=0))
    await memory_store._maybe_fold(wid, cid)
    assert calls == []
    # 差 ≥ 5 → 触发一次：committed → 推游标、成员入 folded 账、归拢条入列、mark 不动
    await memory_store.save_memory(wid, cid, _state_with(count=5, last=0))
    await memory_store._maybe_fold(wid, cid)
    assert len(calls) == 1
    st = await memory_store.load_memory(wid, cid) if False else memory_store.load_memory(wid, cid)
    assert st["last_fold_check"] == 5
    assert st["mark"] == 10                                    # 提交不动 mark（源侧语义）
    assert len(st["folded"]) == 2
    merged = st["entries"][-1]
    assert merged["seq"] == 0 and merged["conclusion"] == "【结论】合并条"
    assert set(st["folded"]) == {"fp0", "fp2"}
    # 同值防重：游标已推 → 不再触发
    await memory_store._maybe_fold(wid, cid)
    assert len(calls) == 1
    # abort（调用异常）→ 游标不动，账保留下轮重试
    async def boom(user_prompt: str) -> str:
        raise RuntimeError("model down")

    monkeypatch.setattr(memory_store, "_fold_call", boom)
    await memory_store.save_memory(wid, cid, _state_with(count=10, last=5))
    await memory_store._maybe_fold(wid, cid)
    st2 = memory_store.load_memory(wid, cid)
    assert st2["last_fold_check"] == 5                          # 失败不推进游标
    # judge-empty（模型说无合并）→ 合法终态，推游标零落库
    async def empty(user_prompt: str) -> str:
        return '{"merges":[]}'

    monkeypatch.setattr(memory_store, "_fold_call", empty)
    await memory_store._maybe_fold(wid, cid)
    st3 = memory_store.load_memory(wid, cid)
    assert st3["last_fold_check"] == 10 and len(st3["entries"]) == 4


# ---------------------------------------------------------------- V5 同口径铁律

def test_v5_same_meter_for_trigger_and_slice(sandbox):
    pending = [_mk_msg(i, f"内容{i}水水水水水水水水" * (i % 5 + 1)) for i in range(37)]
    budget = 2000
    # 触发读数（condense_turn_end 用 _meter_sum）与切片累计同函数同尺
    total = memory_store._meter_sum(pending)
    taken: list[int] = []
    rest = pending
    while rest:
        w = memory_store._truncate_window(rest, budget)
        if not w:
            break
        taken.append(memory_store._meter_sum(w))
        rest = rest[len(w):]
    assert sum(taken) == total                                  # 差值为 0（tolerance=0）
    assert all(t <= budget for t in taken)                      # 每批宁少不超


# ---------------------------------------------------------------- 批循环（任务 3 行为面）

async def test_batch_loop_multi_batch_and_fail_stop(sandbox, work, monkeypatch):
    wid, cid = work["work_id"], work["work_card_id"]
    monkeypatch.setenv("CONDENSE_WINDOW_BUDGET_TOKENS", "85")
    # [2026-10-10 口径改 token] 每条 meter = 12 token + 1 = 13；窗口 85 → 每批 6 条
    # （6×13=78 ≤ 85，7×13=91 > 85）；30 条 = 5 批
    pending = [_mk_msg(i, "六六六六六六") for i in range(30)]
    outputs = iter([f"【结论】批{n}【依据】块{n}" for n in range(10)])

    async def fake(user_prompt: str) -> str:
        return next(outputs)

    monkeypatch.setattr(memory_store, "_condense_call", fake)
    await memory_store._run_condense_pipeline(wid, cid, pending, 0)
    st = memory_store.load_memory(wid, cid)
    assert st["mark"] == 30 and st["batch_count"] == 5          # 批循环推进到清空
    assert len(st["entries"]) == 5

    # 第 2 批解析失败 → 循环停在第 1 批尾，剩余不重试（零阻断哲学）
    await memory_store.save_memory(wid, cid, {"card_id": cid, "mark": 0, "entries": [],
                                              "batch_count": 0, "last_fold_check": 0, "folded": []})
    outputs = iter(["【结论】好批【依据】块0", "无标记垃圾产出", "【结论】不该出现【依据】块0"])

    async def fake2(user_prompt: str) -> str:
        return next(outputs)

    monkeypatch.setattr(memory_store, "_condense_call", fake2)
    await memory_store._run_condense_pipeline(wid, cid, pending, 0)
    st2 = memory_store.load_memory(wid, cid)
    assert st2["mark"] == 6 and st2["batch_count"] == 1         # 单批失败即停、水位留在批 1 尾
    assert len(st2["entries"]) == 1
