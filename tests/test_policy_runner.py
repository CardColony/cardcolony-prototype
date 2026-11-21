# -*- coding: utf-8 -*-
"""接线层（policy_runner）单测：事件进来 → 写数据集 → 喂值班员 → 投递。

守的是三件事：
  ① 每条事件都要进数据集（否则新建策略回溯时会漏掉刚发生的事）；
  ② 只有凑够条件才投递，投递失败不拖垮别的策略；
  ③ 关掉开关就完全不工作（排障用）。
"""
from __future__ import annotations

import time

import pytest

from src.infra import fact_store as fs
from src.infra import harness, policy_runner as runner, policy_set as ps


def _policy(**kw) -> dict:
    base = {
        "id": "p1", "active": True,
        "scope": {"kind": "card"},
        "watch": ["tool_call"],
        "window": "24h",
        "when": {"count": {"types": ["tool_call"]}, "cmp": ">=", "value": 2},
        "then": {"target": "self", "message": "该汇报了:{card_id}", "wake": False},
    }
    base.update(kw)
    return base


def _rec(seq: int, type_: str = "tool_call", card: str = "c1", **data) -> dict:
    return {"seq": seq, "ts": time.time(), "type": type_,
            "data": data or {"tool": "bash"}, "card_id": card}


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """把两个单例都指到临时目录/测试策略上。"""
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    errs = pset.load([_policy()])
    assert errs == []
    ps.reset_set(pset)
    yield store, pset
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


def test_feed_writes_dataset_for_every_event(wired):
    """每条事件都要进数据集——不管有没有策略关心它。"""
    store, pset = wired
    runner.feed("w", _rec(1, "done", "c9"))          # 没策略关心
    assert len(store.query("w")) == 1, "不关心的事件没进数据集"


def test_feed_grows_state_and_fires(wired):
    store, pset = wired
    assert runner.feed("w", _rec(1)) == []
    fired = runner.feed("w", _rec(2))
    assert len(fired) == 1
    assert fired[0]["policy"] == "p1"
    assert fired[0]["ctx"]["card_id"] == "c1"


def test_feed_is_zero_blocking_on_bad_event(wired):
    """坏记录不该抛出去——接线在事件落盘的主链路上。"""
    store, pset = wired
    assert runner.feed("w", {}) == []
    assert runner.feed("", _rec(1)) == []
    assert runner.feed("w", {"type": "", "seq": 1}) == []


def test_off_switch_disables_everything(wired, monkeypatch):
    store, pset = wired
    monkeypatch.setenv("ECO_POLICY_OFF", "1")
    assert runner.feed("w", _rec(1)) == []
    assert store.query("w") == [], "开关关掉了还在写库"
    assert pset.states() == []


async def test_deliver_fired_renders_and_sends(wired, monkeypatch):
    """触发记录 → 真的调用投递，模板占位符被替换。"""
    calls: list[tuple] = []

    async def fake_deliver(work_id, target, text, *, rule_id="", wake=True,
                           mode="message", action_id="", expect=None):
        calls.append((work_id, target, text, rule_id, wake))
        return "mid"

    monkeypatch.setattr(harness, "deliver", fake_deliver)

    n = await runner.deliver_fired("w", [{
        "policy": "p1", "scope": ["c1"],
        "then": {"target": "self", "message": "该汇报了:{card_id}", "wake": False},
        "ctx": {"work_id": "w", "card_id": "c1", "parent": ""},
    }])
    assert n == 1
    work_id, target, text, rule_id, wake = calls[0]
    assert target == "c1"
    assert "c1" in text and "{card_id}" not in text
    assert rule_id == "p1" and wake is False


async def test_deliver_fired_one_failure_does_not_stop_others(wired, monkeypatch):
    """一条投递失败，后面的照发。"""
    calls: list[str] = []

    async def fake_deliver(work_id, target, text, *, rule_id="", wake=True,
                           mode="message", action_id="", expect=None):
        if target == "boom":
            raise RuntimeError("投递炸了")
        calls.append(target)
        return "mid"

    monkeypatch.setattr(harness, "deliver", fake_deliver)
    n = await runner.deliver_fired("w", [
        {"policy": "p1", "scope": ["boom"], "then": {"target": "self", "message": "x"},
         "ctx": {"work_id": "w", "card_id": "boom", "parent": ""}},
        {"policy": "p1", "scope": ["ok"], "then": {"target": "self", "message": "x"},
         "ctx": {"work_id": "w", "card_id": "ok", "parent": ""}},
    ])
    assert n == 1 and calls == ["ok"]


def test_bootstrap_seeds_from_dataset(tmp_path, monkeypatch):
    """启动回溯：把数据集里窗口内的事实补进本子，补完就满足的立刻触发。"""
    import json

    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    now = time.time()
    events = tmp_path / "events"
    events.mkdir()
    (events / "c1.jsonl").write_text("\n".join(json.dumps({
        "seq": i, "ts": now - 100 + i, "type": "tool_call",
        "data": {"tool": "bash"}, "card_id": "c1"}) for i in range(3)) + "\n",
        encoding="utf-8")

    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    pset.load([_policy()])
    ps.reset_set(pset)

    out = runner.bootstrap("w", since=now - 86400, events_dir=events)
    assert out["facts"] == 3
    assert out["seeded"].get("p1") == 3
    assert out["fired"] == 1, "回溯完已满足却没触发"
    assert pset.states()[0]["entries"] == 0, "触发后没撕本子"
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


def test_goal_block_edit_hits_the_notify_policy(tmp_path, monkeypatch):
    """改 goal ⇒ `card_block_edited(block="goal")` ⇒ 命中「目标已更新」这条策略。

    这条守的是用户 2026-10-28 的口径：「更新保存，它就会收到提醒」。
    """
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    errs = pset.load([{
        "id": "goal_updated_notify", "active": True,
        "scope": {"kind": "card"}, "watch": ["card_block_edited"],
        "window": "1h",
        "when": {"event": {"types": ["card_block_edited"],
                           "where": {"block": "goal"}}},
        "then": {"target": "self", "message": "目标已更新", "wake": True},
    }])
    assert errs == []
    ps.reset_set(pset)

    rec = {"seq": 1, "ts": time.time(), "type": "card_block_edited",
           "data": {"block": "goal", "card_type": "sub_project",
                    "old": "旧目标", "new": "新目标", "by": "card_001"},
           "card_id": "card_009"}
    fired = runner.feed("w", rec)
    assert len(fired) == 1 and fired[0]["policy"] == "goal_updated_notify"

    # 改的是别的块 ⇒ 不该提醒
    rec2 = dict(rec, seq=2, data={**rec["data"], "block": "mission"})
    assert runner.feed("w", rec2) == []
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


def test_goal_block_edit_hits_the_notify_policy(tmp_path, monkeypatch):
    """改 goal ⇒ `card_block_edited(block="goal")` ⇒ 命中「目标已更新」这条策略。

    这条守的是用户 2026-10-28 的口径：「更新保存，它就会收到提醒」。
    """
    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    errs = pset.load([{
        "id": "goal_updated_notify", "active": True,
        "scope": {"kind": "card"}, "watch": ["card_block_edited"],
        "window": "1h",
        "when": {"event": {"types": ["card_block_edited"],
                           "where": {"block": "goal"}}},
        "then": {"target": "self", "message": "目标已更新", "wake": True},
    }])
    assert errs == []
    ps.reset_set(pset)

    rec = {"seq": 1, "ts": time.time(), "type": "card_block_edited",
           "data": {"block": "goal", "card_type": "sub_project",
                    "old": "旧目标", "new": "新目标", "by": "card_001"},
           "card_id": "card_009"}
    fired = runner.feed("w", rec)
    assert len(fired) == 1 and fired[0]["policy"] == "goal_updated_notify"

    # 改的是别的块 ⇒ 不该提醒
    rec2 = dict(rec, seq=2, data={**rec["data"], "block": "mission"})
    assert runner.feed("w", rec2) == []
    store.close()
    fs.reset(fs.FactStore(works_dir=tmp_path))
    ps.reset_set(ps.PolicySet())


# ============================================================
# [补书 §三 · card_648 23:12 实录] handled=runner 排除条件走整链：
# 事件 data → flatten_parts（catalog 契约）→ trigger_data →
# 规则求值 → 点火/投递。两次断点都钉住：契约丢字段（catalog 没
# 声明 handled ⇒ trigger_data 缺席）与运行实例装旧规则。
# ============================================================

def test_handled_runner_suppresses_reinvoke_via_real_chain(tmp_path, monkeypatch):
    """pseudo_call_unparsed 带 handled=runner ⇒ pseudo_call_reinvoke 不点火。

    用**真策略文件**＋**真事件目录**走 feed→ingest→dispatch 全链——
    catalog 若把 handled 拍出零件表，本用例第一断言立即红。
    """
    from pathlib import Path

    from src.infra import intervention_runner, policy_config

    monkeypatch.delenv("ECO_POLICY_OFF", raising=False)
    real_policies = Path(__file__).resolve().parents[1] / "config" / "harness_policies.json"
    monkeypatch.setenv("ECO_POLICIES_PATH", str(real_policies))
    store = fs.FactStore(works_dir=tmp_path)
    fs.reset(store)
    pset = ps.PolicySet()
    errs = pset.load(policy_config.load_raw())
    assert errs == [], "真策略文件必须零错误加载"
    ps.reset_set(pset)
    recorded: list = []
    monkeypatch.setattr(intervention_runner, "record_fired",
                        lambda w, f: recorded.append(f))
    try:
        base = {"seq": 1, "ts": time.time(), "type": "pseudo_call_unparsed",
                "card_id": "card_648",
                # card_648 23:12 实录同形态：runner 已接管（handled 标记）
                "data": {"round": 1, "chars": 975, "origin": "pseudo_compat",
                         "candidates": 0, "proto_hint": "", "recent": 1,
                         "head": "<｜begin▁of▁sentence｜>11 条线",
                         "handled": "runner"}}
        fired = runner.feed("w", dict(base))
        assert fired == [], f"handled=runner 不应再点火：{fired}"
        # 标记必须真到了规则输入——零件表面上看一眼
        fact = store.query("w")[-1]
        assert fact.get("handled") == "runner", \
            f"handled 没进事实零件（catalog 契约丢了它）：{fact}"
        # 对照：同形态但 runner 未接管 ⇒ 现行提醒仍可触发
        rec2 = dict(base, seq=2,
                    data={k: v for k, v in base["data"].items()
                          if k != "handled"})
        fired2 = runner.feed("w", rec2)
        assert any(f["policy"] == "pseudo_call_reinvoke" for f in fired2), \
            "runner 未接管的裸伪调用事实仍应能叫醒（现行提醒兜底）"
    finally:
        store.close()
        fs.reset(fs.FactStore(works_dir=tmp_path))
        ps.reset_set(ps.PolicySet())


def test_fragment_only_hop_yields_no_unparsed_fact(tmp_path, monkeypatch):
    """只剩供方残片的跳次不该产出 pseudo_call_unparsed（无未派发调用可纠）。

    判据钉在「什么算疑似」上：ds_safety 块/CDATAEND>/BOS 标记被遮蔽后，
    extract 的 unresolved 必须为 False——runner 的 pseudo_unparsed 分支
    由它驱动，False ⇒ 零事实 ⇒ 零提醒。
    """
    from src.api import text_tool_rescue

    for sample in (
        "CDATAEND>\n<ds_safety>[用户未成年]否\n[分类]其他\n[判定]…\n[规则]无</ds_safety>",
        "<｜begin▁of▁sentence｜>11 条线\n\n## 通信线",
        "<ds_safety>无边界内嵌</ds_safety>",
    ):
        calls, unresolved, stripped = text_tool_rescue.extract_text_tool_calls(sample)
        assert calls == [] and unresolved is False, \
            f"残片被当成疑似调用候选了：{sample[:60]}"
